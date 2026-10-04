"""Local model bridge, served by the desktop app's server on 127.0.0.1.

The agent engine is pointed at  http://127.0.0.1:<port>/bridge/<secret>/<model-id>, where the
secret is random per run (it lives in the URL so no other credential the engine happens to
carry is ever relied on, or passed along). The bridge then:
  - Claude-format providers: forwards the request as-is, adding the real API key.
  - OpenAI-format providers: translates Claude <-> OpenAI (streaming and tool calls) with LiteLLM.
So real API keys stay in the Keychain and this process, never in the agent's environment.
"""
import asyncio
import json
import logging
import re
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


class RateLimited:
    """A provider said "too many requests" (HTTP 429). Nothing was sent to the engine yet."""
    def __init__(self, m: dict, status: int, text: str, retry_after: float | None):
        self.m, self.status, self.text, self.retry_after = m, status, text, retry_after
        self.daily = bool(re.search(r"per.?day|daily|quota exceeded|exceeded your current quota|insufficient_quota",
                                    text, re.I))


def _retry_after(text: str, headers=None) -> float | None:
    if headers and headers.get("retry-after"):
        try:
            return float(headers["retry-after"])
        except ValueError:
            pass
    m = re.search(r"retry in ([\d.]+)\s*s", text, re.I) or re.search(r'"retryDelay":\s*"([\d.]+)s"', text)
    return float(m.group(1)) if m else None


def _limit_message(rl: RateLimited) -> str:
    label = rl.m.get("label") or models.provider_of(rl.m)["label"]
    lim = re.search(r"limit:\s*(\d+)", rl.text)
    detail = f" (limit: {lim.group(1)} requests)" if lim else ""
    if rl.daily:
        return (f"{label}'s usage limit for {rl.m.get('model') or 'this model'} is used up{detail}. Free tiers reset "
                "after a while (Gemini's daily limits reset at midnight Pacific time). To keep going now, pick another "
                "model in the chat box, add billing with the provider, or set a fallback model in Models → My models.")
    return (f"{label} is rate-limiting requests right now{detail}. Try again in a minute, or pick another model.")


class Bridge:
    def __init__(self, registry: models.Registry):
        self.registry = registry
        self.session: aiohttp.ClientSession | None = None
        self.notify = None                          # set by the web server: send a toast to the app
        self.cooldown: dict[str, float] = {}        # model id -> time its limit should have reset

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
        """Send the request to the model. If the provider says "too many requests": wait and retry
        when the wait is short; otherwise continue with the fallback model (Models → My models)."""
        m, err = self._model(request)
        if err:
            return err
        body = await request.read()
        tried, original, waits = {m["id"]}, m, 0
        if self.cooldown.get(m["id"], 0) > time.time():
            fb = self.registry.fallback_for(m, exclude=tried | self._cooling())
            if fb:
                m = fb
                tried.add(m["id"])
        last = None
        for _ in range(6):
            res = await self._call(request, m, body)
            if not isinstance(res, RateLimited):
                return res
            last = res
            log.warning("rate limited by %s (%s): %s", m["id"], res.retry_after, res.text[:200])
            if not res.daily and res.retry_after is not None and res.retry_after <= 65 and waits < 2:
                waits += 1
                await self._toast(f"{models.provider_of(m)['label']} asked to slow down. Waiting "
                                  f"{int(res.retry_after) + 1}s, then continuing.")
                await asyncio.sleep(res.retry_after + 1)
                continue
            self.cooldown[m["id"]] = time.time() + (3600 if res.daily else 120)
            fb = self.registry.fallback_for(original, exclude=tried | self._cooling())
            if not fb:
                break
            await self._toast(f"{m['label']} hit its usage limit, so Steward is continuing with {fb['label']}.")
            m = fb
            tried.add(m["id"])
            waits = 0
        return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                                             "message": _limit_message(last)}}, status=400)

    def _cooling(self) -> set:
        now = time.time()
        return {mid for mid, t in self.cooldown.items() if t > now}

    async def _toast(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify({"type": "toast", "text": text})
            except Exception:
                pass

    async def _call(self, request: web.Request, m: dict, body: bytes):
        if models.provider_of(m)["kind"] == "openai":
            return await self._translate(request, m, body)
        return await self._forward(request, m, "/v1/messages", body)

    async def count_tokens(self, request: web.Request):
        m, err = self._model(request)
        if err:
            return err
        if models.provider_of(m)["kind"] == "anthropic" and m["provider"] == "anthropic":
            return await self._forward(request, m, "/v1/messages/count_tokens")
        body = await request.read()                      # rough estimate is enough here
        return web.json_response({"input_tokens": max(1, len(body) // 4)})

    # ------------------------------------------------- Claude-format: pass --
    async def _forward(self, request: web.Request, m: dict, path: str, body: bytes | None = None):
        body = await request.read() if body is None else body
        try:                                             # use this model's own name (matters for fallbacks)
            data = json.loads(body)
            if m.get("model") and data.get("model") != m["model"]:
                data["model"] = m["model"]
                body = json.dumps(data).encode()
        except Exception:
            pass
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
        if upstream.status == 429:
            text = await upstream.text()
            upstream.release()
            return RateLimited(m, 429, text, _retry_after(text, upstream.headers))
        resp = web.StreamResponse(status=upstream.status, headers={
            "Content-Type": upstream.headers.get("Content-Type", "application/json")})
        await resp.prepare(request)
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        upstream.release()
        return resp

    # ---------------------------------------------- OpenAI-format: translate --
    async def _translate(self, request: web.Request, m: dict, raw: bytes | None = None):
        try:
            import litellm
            from litellm.anthropic_interface import messages as anthropic_messages
            litellm.suppress_debug_info = True
            litellm.drop_params = True                   # skip options a provider doesn't support
        except ImportError:
            return _error(500, "The translator isn't installed. Run Start Steward.command to update.")
        body = json.loads(raw) if raw is not None else await request.json()
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
            if status == 429 or "RateLimitError" in type(e).__name__:
                return RateLimited(m, 429, str(e), _retry_after(str(e)))
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


# ------------------------------------------------------------ model lists --
# Where each provider lists its models (this also checks that the key works).
_LIST_URLS = {
    "anthropic": "https://api.anthropic.com/v1/models?limit=100",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "deepseek": "https://api.deepseek.com/models",
    "ollama-cloud": "https://ollama.com/api/tags",
}


async def list_models(provider: str, key: str, base: str = "") -> tuple[bool, list[dict], str]:
    """Returns (ok, [{id, note}], error). Used by "Connect a provider" so people pick from a
    list instead of typing model IDs."""
    p = models.PROVIDERS.get(provider)
    if not p:
        return False, [], "Unknown provider."
    if p["kind"] == "openai":
        url = (base or p["base"]).rstrip("/") + "/models"
    elif provider in _LIST_URLS:
        url = _LIST_URLS[provider]
    else:
        url = (base or p["base"]).rstrip("/") + "/v1/models"
    headers = {"anthropic-version": "2023-06-01"}
    if provider == "anthropic":
        headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            r = await s.get(url, headers=headers)
            text = await r.text()
            if r.status in (401, 403):
                return False, [], "That key wasn't accepted. Check you copied all of it."
            if r.status >= 400:
                return False, [], f"{p['label']} replied with an error ({r.status})."
            data = json.loads(text)
    except Exception as e:
        return False, [], f"Couldn't reach {p['label']}: {e}"
    if provider == "openrouter" and key:
        # The model list is public, so check the key separately.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                r = await s.get("https://openrouter.ai/api/v1/key", headers=headers)
                if r.status in (401, 403):
                    return False, [], "That key wasn't accepted. Check you copied all of it."
        except Exception:
            pass
    items = data.get("data") or data.get("models") or []
    out = []
    for m in items:
        mid = m.get("id") or m.get("name") or m.get("model") or ""
        mid = mid.removeprefix("models/")              # Gemini lists "models/<id>"
        if not mid:
            continue
        note = m.get("display_name") or ""
        if provider == "openrouter":
            pricing = m.get("pricing") or {}
            if str(pricing.get("prompt", "1")) in ("0", "0.0") and str(pricing.get("completion", "1")) in ("0", "0.0"):
                note = "free"
            if "tools" not in (m.get("supported_parameters") or ["tools"]):
                continue                                 # Steward needs tool calling
        if provider == "gemini" and any(x in mid for x in ("embedding", "imagen", "veo", "tts", "aqa")):
            continue
        out.append({"id": mid, "note": note})
    out.sort(key=lambda x: x["id"])
    if not out:
        return False, [], "The key works, but no models were listed. You can type a model ID instead."
    return True, out, ""
