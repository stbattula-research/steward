"""Local model bridge, served by the desktop app's server on 127.0.0.1.

The agent engine is pointed at  http://127.0.0.1:<port>/bridge/<secret>/<model-id>, where the
secret is random per run (it lives in the URL so no other credential the engine happens to
carry is ever relied on, or passed along). The bridge then:
  - Claude-format providers: forwards the request as-is, adding the real API key.
  - OpenAI-format providers: translates Claude <-> OpenAI (streaming and tool calls) with LiteLLM.
So real API keys stay in the Keychain and this process, never in the agent's environment.
"""
import json
import logging
import secrets
import time

import aiohttp
from aiohttp import web

import config
import models

log = logging.getLogger("bridge")
TOKEN = secrets.token_urlsafe(24)          # new every run; only the agent engine gets it
_PASS_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")


def url_for(model_id: str) -> str:
    return f"http://127.0.0.1:{config.WEB_PORT}/bridge/{TOKEN}/{model_id}"


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"type": "error", "error": {"type": "api_error", "message": message}}, status=status)


def _authorized(request: web.Request) -> bool:
    if request.remote not in ("127.0.0.1", "::1"):
        return False
    return secrets.compare_digest(request.match_info.get("secret", ""), TOKEN)


class Bridge:
    def __init__(self, registry: models.Registry):
        self.registry = registry
        self.session: aiohttp.ClientSession | None = None

    def routes(self, app: web.Application) -> None:
        app.router.add_post("/bridge/{secret}/{mid}/v1/messages", self.messages)
        app.router.add_post("/bridge/{secret}/{mid}/v1/messages/count_tokens", self.count_tokens)

    async def _http(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600))
        return self.session

    def _model(self, request: web.Request):
        if not _authorized(request):
            return None, _error(401, "Not authorized")
        m = self.registry.get(request.match_info["mid"])
        if not m:
            return None, _error(404, "Unknown model. It may have been removed in Manage models.")
        return m, None

    # ---------------------------------------------------------- endpoints --
    async def messages(self, request: web.Request):
        m, err = self._model(request)
        if err:
            return err
        p = models.provider_of(m)
        if p["kind"] == "openai":
            return await self._translate(request, m)
        return await self._forward(request, m, "/v1/messages")

    async def count_tokens(self, request: web.Request):
        m, err = self._model(request)
        if err:
            return err
        if models.provider_of(m)["kind"] == "anthropic" and m["provider"] == "anthropic":
            return await self._forward(request, m, "/v1/messages/count_tokens")
        body = await request.read()                      # rough estimate is enough here
        return web.json_response({"input_tokens": max(1, len(body) // 4)})

    # ------------------------------------------------- Claude-format: pass --
    async def _forward(self, request: web.Request, m: dict, path: str):
        body = await request.read()
        headers = {h: request.headers[h] for h in _PASS_HEADERS if h in request.headers}
        key = models.get_key(m["id"])
        if m["provider"] == "anthropic":
            headers["x-api-key"] = key
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        url = models.base_url(m) + path
        if request.query_string:
            url += "?" + request.query_string
        try:
            upstream = await (await self._http()).post(url, data=body, headers=headers)
        except Exception as e:
            return _error(502, f"Couldn't reach {models.provider_of(m)['label']}: {e}")
        resp = web.StreamResponse(status=upstream.status, headers={
            "Content-Type": upstream.headers.get("Content-Type", "application/json")})
        await resp.prepare(request)
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        upstream.release()
        return resp

    # ---------------------------------------------- OpenAI-format: translate --
    async def _translate(self, request: web.Request, m: dict):
        try:
            import litellm
            from litellm.anthropic_interface import messages as anthropic_messages
            litellm.suppress_debug_info = True
            litellm.drop_params = True                   # skip options a provider doesn't support
        except ImportError:
            return _error(500, "The translator isn't installed. Run Start Steward.command to update.")
        body = await request.json()
        system = body.get("system")
        if isinstance(system, list):                     # Claude allows a list of text blocks
            system = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        kwargs = {k: body[k] for k in ("max_tokens", "messages", "tools", "tool_choice", "temperature",
                                        "stop_sequences", "top_p") if k in body}
        stream = bool(body.get("stream"))
        try:
            result = await anthropic_messages.acreate(
                model=f"custom_openai/{m['model']}", api_base=models.base_url(m),
                api_key=models.get_key(m["id"]) or "none", system=system or None, stream=stream, **kwargs)
        except Exception as e:
            status = getattr(e, "status_code", 502) or 502
            log.warning("bridge %s error: %s", m["id"], str(e)[:300])
            return _error(status, f"{models.provider_of(m)['label']}: {str(e)[:500]}")
        if not stream:
            data = result if isinstance(result, dict) else getattr(result, "model_dump", lambda: result)()
            return web.json_response(data)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        try:
            async for chunk in result:
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                elif not isinstance(chunk, (bytes, bytearray)):
                    chunk = f"data: {json.dumps(chunk, default=str)}\n\n".encode()
                await resp.write(chunk)
        except Exception as e:
            log.warning("bridge stream error: %s", e)
            await resp.write(f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)[:300]}})}\n\n".encode())
        await resp.write_eof()
        return resp

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()


# -------------------------------------------------------- connection test --
async def test_model(m: dict, key: str | None = None) -> tuple[bool, str]:
    """Send a tiny request straight to the provider. Returns (ok, message)."""
    p = models.provider_of(m)
    key = key if key is not None else models.get_key(m["id"])
    base = models.base_url(m)
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
            if p["kind"] == "openai":
                r = await s.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                                 json={"model": m["model"], "max_tokens": 16,
                                       "messages": [{"role": "user", "content": "Reply with: OK"}]})
            else:
                headers = {"anthropic-version": "2023-06-01"}
                if m["provider"] == "anthropic":
                    headers["x-api-key"] = key
                elif key:
                    headers["Authorization"] = f"Bearer {key}"
                if m["model"]:
                    r = await s.post(f"{base}/v1/messages", headers=headers, json={
                        "model": m["model"], "max_tokens": 16,
                        "messages": [{"role": "user", "content": "Reply with: OK"}]})
                else:   # no model chosen: just check the key works
                    r = await s.get(f"{base}/v1/models", headers=headers)
            text = await r.text()
            if r.status >= 400:
                try:
                    msg = json.loads(text).get("error", {})
                    msg = msg.get("message") if isinstance(msg, dict) else str(msg)
                except Exception:
                    msg = text[:200]
                return False, f"{p['label']} said: {msg or r.status}"
        return True, f"Connected in {time.time() - t0:.1f}s."
    except Exception as e:
        return False, f"Couldn't connect: {e}"
