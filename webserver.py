"""The desktop app's backend: a local-only web server (127.0.0.1) with a WebSocket.

Security: it listens on 127.0.0.1 only, every request needs a secret session cookie
(set by opening the app's launch link, which contains a token from ~/.steward/web_token),
and WebSocket connections from any other website are rejected by Origin/Host checks.
"""
import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import WSMsgType, web
import aiohttp


def aiohttp_client():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

import bridge
import config
import models
import voice

log = logging.getLogger("web")
COOKIE = "agent_session"
STATIC = config.ROOT / "web" / "dist"


def load_token() -> str:
    if config.WEB_TOKEN_FILE.exists():
        return config.WEB_TOKEN_FILE.read_text().strip()
    tok = secrets.token_urlsafe(24)
    config.WEB_TOKEN_FILE.write_text(tok)
    config.WEB_TOKEN_FILE.chmod(0o600)
    return tok


def app_url() -> str:
    return f"http://127.0.0.1:{config.WEB_PORT}/?t={load_token()}"


class WebUI:
    def __init__(self, router, brain, scheduler):
        self.router, self.brain, self.scheduler = router, brain, scheduler
        self.token = load_token()
        self.clients: set[web.WebSocketResponse] = set()
        self.history: deque = deque(maxlen=400)
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.files: dict[str, Path] = {}
        self.status = {"type": "status", "busy": False, "label": ""}
        self.last_active = 0.0
        self.prefs = {"phone_mode": "auto", "theme": "auto"}   # phone: auto|always|off · theme: auto|light|dark
        try:
            if config.PREFS_FILE.exists():
                self.prefs.update(json.loads(config.PREFS_FILE.read_text()))
        except Exception:
            log.exception("could not load prefs")
        self._load_history()
        scheduler.listeners.append(lambda: asyncio.ensure_future(self.push_tasks()))

    # ------------------------------------------------------------ history ----
    def _load_history(self) -> None:
        try:
            if config.WEB_HISTORY_FILE.exists():
                self.history.extend(json.loads(config.WEB_HISTORY_FILE.read_text()))
                for ev in self.history:
                    if ev.get("type") == "file" and Path(ev.get("path", "")).exists():
                        self.files[ev["id"]] = Path(ev["path"])
        except Exception:
            log.exception("could not load web history")

    def _save_history(self) -> None:
        config.WEB_HISTORY_FILE.write_text(json.dumps(list(self.history)))
        config.WEB_HISTORY_FILE.chmod(0o600)

    # ------------------------------------------------------------- sending ---
    def has_clients(self) -> bool:
        return bool(self.clients)

    def recently_active(self, seconds: int = 600) -> bool:
        return self.has_clients() and time.time() - self.last_active < seconds

    async def _broadcast(self, ev: dict) -> None:
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(ev)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def emit(self, ev: dict) -> None:
        ev = {"id": uuid.uuid4().hex[:12], "ts": datetime.now().isoformat(timespec="seconds"), **ev}
        self.history.append(ev)
        self._save_history()
        await self._broadcast(ev)

    async def emit_file(self, path: Path, caption: str = "") -> None:
        fid = uuid.uuid4().hex[:12]
        self.files[fid] = Path(path)
        is_img = Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
        await self.emit({"type": "file", "fid": fid, "id": fid, "path": str(path),
                         "name": Path(path).name, "caption": caption, "image": is_img})

    async def set_status(self, busy: bool, label: str = "") -> None:
        self.status = {"type": "status", "busy": busy, "label": label}
        await self._broadcast(self.status)

    async def push_tasks(self) -> None:
        await self._broadcast({"type": "tasks", "items": self.scheduler.as_list()})

    async def ask_approval(self, summary: str) -> bool:
        aid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        ev = {"type": "approval", "aid": aid, "summary": summary}
        self.pending[aid] = (fut, ev)
        await self.emit(ev)
        try:
            return await asyncio.wait_for(fut, timeout=config.APPROVAL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self.emit({"type": "message", "role": "system", "text": "No answer, so I treated that as denied."})
            return False
        finally:
            self.pending.pop(aid, None)
            await self._mark_resolved(aid, fut.result() if fut.done() and not fut.cancelled() else None)

    async def _mark_resolved(self, aid: str, approved) -> None:
        for ev in self.history:
            if ev.get("type") == "approval" and ev.get("aid") == aid and "approved" not in ev:
                ev["approved"] = approved
        self._save_history()
        await self._broadcast({"type": "approval_resolved", "aid": aid, "approved": approved})

    async def resolve_all_approvals(self, approved: bool) -> None:
        """Called when an approval was answered elsewhere (e.g. Telegram)."""
        for aid, (fut, _) in list(self.pending.items()):
            if not fut.done():
                fut.set_result(approved)

    # ------------------------------------------------------------ memory -----
    MEMORY_NAME = re.compile(r"^(about_me|learned|watchlist)\.md$|^playbooks/[A-Za-z0-9 _-]{1,60}\.md$")

    def memory_files(self) -> list[dict]:
        out = []
        for name in ("about_me.md", "watchlist.md", "learned.md"):
            f = config.MEMORY_DIR / name
            out.append({"name": name, "content": f.read_text() if f.exists() else ""})
        for f in sorted((config.MEMORY_DIR / "playbooks").glob("*.md")):
            out.append({"name": f"playbooks/{f.name}", "content": f.read_text()})
        return out

    async def push_memory(self, ws=None) -> None:
        ev = {"type": "memory", "files": self.memory_files(), "dir": str(config.MEMORY_DIR)}
        if ws:
            await ws.send_json(ev)
        else:
            await self._broadcast(ev)

    async def save_memory(self, name: str, content: str) -> None:
        if not self.MEMORY_NAME.match(name or ""):
            await self._broadcast({"type": "toast", "text": "That file name isn't allowed.", "error": True})
            return
        path = (config.MEMORY_DIR / name).resolve()
        if config.MEMORY_DIR.resolve() not in path.parents:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        # The running conversation loaded memory at its start; point it at the new version.
        self.brain.recent_reports.append(f"{config.OWNER_NAME} edited {path} in the desktop app. Re-read it before relying on it.")
        await self.push_memory()
        await self._broadcast({"type": "toast", "text": f"Saved {name}"})

    async def delete_playbook(self, name: str) -> None:
        if not name.startswith("playbooks/") or not self.MEMORY_NAME.match(name):
            return
        path = config.MEMORY_DIR / name
        if path.exists():
            archive = config.STATE_DIR / "deleted-playbooks"
            archive.mkdir(exist_ok=True)
            path.rename(archive / f"{int(time.time())}-{path.name}")
        await self.push_memory()
        await self._broadcast({"type": "toast", "text": f"Removed {name}"})

    async def set_pref(self, key: str, value) -> None:
        allowed = {"phone_mode": ("auto", "always", "off"), "theme": ("auto", "light", "dark")}
        if value in allowed.get(key, ()):
            self.prefs[key] = value
            config.PREFS_FILE.write_text(json.dumps(self.prefs))
            await self._broadcast({"type": "prefs", "prefs": self.prefs})

    # -------------------------------------------------------------- auth -----
    def _authed(self, request: web.Request) -> bool:
        host_ok = request.host in (f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}")
        return host_ok and secrets.compare_digest(request.cookies.get(COOKIE, ""), self.token)

    # ------------------------------------------------------------- routes ----
    async def index(self, request: web.Request):
        if request.query.get("t"):
            if not secrets.compare_digest(request.query["t"], self.token):
                return web.Response(status=403, text=f"Bad link. Open the {config.AGENT_NAME} app again.")
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 365)
            return resp
        if not self._authed(request):
            return web.Response(status=403, text=f"Open the {config.AGENT_NAME} app (or run: python main.py --url) to sign in.")
        index = STATIC / "index.html"
        if not index.exists():
            return web.Response(status=500, text="The UI hasn't been built. Run: bash setup.sh")
        return web.FileResponse(index)

    async def ws(self, request: web.Request):
        origin = request.headers.get("Origin", "")
        allowed = (f"http://127.0.0.1:{config.WEB_PORT}", f"http://localhost:{config.WEB_PORT}")
        if not self._authed(request) or origin not in allowed:
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.clients.add(ws)
        self.last_active = time.time()
        await ws.send_json({"type": "hello", "agent": config.AGENT_NAME, "avatar": config.AGENT_AVATAR,
                            "model": self.brain.registry.active_model()["label"],
                            "telegram": bool(self.router.telegram), "prefs": self.prefs})
        await ws.send_json({"type": "history", "events": list(self.history)})
        await ws.send_json(self.status)
        await ws.send_json({"type": "tasks", "items": self.scheduler.as_list()})
        await self.push_memory(ws)
        await ws.send_json({"type": "models", **self.brain.registry.public()})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        await self._on_client(json.loads(msg.data))
                    except Exception:
                        log.exception("bad client message")
        finally:
            self.clients.discard(ws)
        return ws

    async def _on_client(self, m: dict) -> None:
        kind = m.get("type")
        self.last_active = time.time()
        if kind == "send" and m.get("text", "").strip():
            text = m["text"].strip()
            await self.emit({"type": "message", "role": "user", "text": text, "source": "web"})
            asyncio.ensure_future(self.brain.handle(text, origin="web"))
        elif kind == "approve":
            entry = self.pending.get(m.get("aid"))
            if entry and not entry[0].done():
                entry[0].set_result(bool(m.get("approved")))
        elif kind == "stop":
            for fut, _ in list(self.pending.values()):
                if not fut.done():
                    fut.set_result(False)
            await self.brain.stop_current()
            await self.emit({"type": "message", "role": "system", "text": "Stopped."})
        elif kind == "new":
            await self.brain.stop_current()
            async with self.brain.lock:
                await self.brain.reset()
            await self.emit({"type": "divider", "text": "New conversation"})
        elif kind == "screen":
            path = config.OUTBOX / f"screen-{datetime.now():%Y%m%d-%H%M%S}.png"
            proc = await asyncio.create_subprocess_exec("screencapture", "-x", str(path))
            await proc.wait()
            if path.exists():
                await self.emit_file(path, "Your screen right now")
        elif kind == "watch":
            asyncio.ensure_future(self.scheduler._heartbeat_now())
        elif kind == "cancel_task":
            self.scheduler.remove(m.get("id", ""))
        elif kind == "memory_get":
            await self.push_memory()
        elif kind == "memory_save":
            await self.save_memory(m.get("name", ""), m.get("content", ""))
        elif kind == "memory_delete":
            await self.delete_playbook(m.get("name", ""))
        elif kind == "set_pref":
            await self.set_pref(m.get("key"), m.get("value"))
        elif kind.startswith("model") or kind == "ollama_tags":
            await self._on_models(m)
        elif kind == "presence":
            pass   # last_active already updated

    # ------------------------------------------------------------ models -----
    async def push_models(self) -> None:
        await self._broadcast({"type": "models", **self.brain.registry.public()})
        await self._broadcast({"type": "hello_update", "model": self.brain.registry.active_model()["label"]})

    async def _on_models(self, m: dict) -> None:
        reg = self.brain.registry
        kind = m.get("type")
        try:
            if kind == "models_get":
                await self.push_models()
            elif kind == "model_select":
                if self.brain.busy:
                    raise ValueError("Wait for the current task to finish (or press Stop) before switching.")
                chosen = await self.brain.switch_model(m.get("id", ""))
                await self.emit({"type": "divider", "text": f"New conversation · {chosen['label']}"})
                await self.push_models()
            elif kind == "model_save":
                saved = reg.save_model(m.get("model") or {})
                if m.get("background"):
                    reg.set_background(saved["id"])
                await self.push_models()
                await self._broadcast({"type": "toast", "text": f"Saved {saved['label']}"})
                await self._broadcast({"type": "model_saved", "id": saved["id"]})
            elif kind == "model_delete":
                was_active = reg.active == m.get("id")
                reg.delete(m.get("id", ""))
                if was_active:
                    await self.brain.switch_model(reg.active)
                await self.push_models()
                await self._broadcast({"type": "toast", "text": "Model removed"})
            elif kind == "model_background":
                reg.set_background(m.get("id", ""))
                await self.push_models()
            elif kind == "model_test":
                draft = m.get("model") or {}
                saved = reg.get(draft.get("id", "")) or {}
                test = {**saved, **{k: v for k, v in draft.items() if k != "api_key"}}
                test.setdefault("id", "draft")
                key = (draft.get("api_key") or "").strip() or None
                ok, msg = await bridge.test_model(test, key)
                await self._broadcast({"type": "model_test_result", "ok": ok, "text": msg})
            elif kind == "ollama_tags":
                names = []
                try:
                    async with aiohttp_client() as s:
                        r = await s.get(f"{config.OLLAMA_URL}/api/tags")
                        names = [x["name"] for x in (await r.json()).get("models", [])]
                except Exception:
                    pass
                await self._broadcast({"type": "ollama_tags", "names": names})
        except ValueError as e:
            await self._broadcast({"type": "toast", "text": str(e), "error": True})
        except Exception as e:
            log.exception("model action failed")
            await self._broadcast({"type": "toast", "text": f"Something went wrong: {str(e)[:200]}", "error": True})

    async def upload(self, request: web.Request):
        if not self._authed(request):
            return web.Response(status=403)
        reader = await request.multipart()
        field = await reader.next()
        name = Path(field.filename or f"upload-{int(time.time())}").name
        dest = config.INBOX / name
        with open(dest, "wb") as fh:
            while chunk := await field.read_chunk():
                fh.write(chunk)
        return web.json_response({"path": str(dest), "name": name})

    async def voice_note(self, request: web.Request):
        if not self._authed(request):
            return web.Response(status=403)
        dest = config.INBOX / f"voice-{int(time.time())}.webm"
        dest.write_bytes(await request.read())
        try:
            text = await voice.transcribe(dest)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"text": text})

    async def file(self, request: web.Request):
        if not self._authed(request):
            return web.Response(status=403)
        path = self.files.get(request.match_info["fid"])
        if not path or not path.exists():
            return web.Response(status=404)
        return web.FileResponse(path)

    # -------------------------------------------------------------- start ----
    async def start(self) -> None:
        app = web.Application(client_max_size=200 * 1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws)
        app.router.add_post("/upload", self.upload)
        app.router.add_post("/voice", self.voice_note)
        app.router.add_get("/files/{fid}", self.file)
        self.bridge = bridge.Bridge(self.brain.registry)
        self.bridge.routes(app)
        if (STATIC / "assets").exists():
            app.router.add_static("/assets", STATIC / "assets")
        for extra in ("favicon.svg", "manifest.webmanifest"):
            if (STATIC / extra).exists():
                app.router.add_get(f"/{extra}", lambda r, p=STATIC / extra: web.FileResponse(p))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.WEB_PORT).start()
        log.info("Desktop app at %s", app_url())
