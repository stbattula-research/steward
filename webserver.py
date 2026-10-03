"""The app's backend: a local-only web server (127.0.0.1) with a WebSocket.

Security: it listens on 127.0.0.1 only. The Mac app signs in with a secret session cookie
(set by opening the app's launch link, which contains a token from ~/.steward/web_token).
Paired phones reach it through `tailscale serve` (your private Tailscale network only) and
sign in with their own per-phone key (see mobile.py). WebSocket connections from any other
website are rejected by Origin/Host checks.
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
import localmodels
import mobile
import models
import voice

log = logging.getLogger("web")
COOKIE = "agent_session"
PHONE_COOKIE = "steward_phone"
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
        self.clients: dict[web.WebSocketResponse, dict] = {}   # ws -> {"kind", "device", "visible"}
        self.history: deque = deque(maxlen=400)
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.files: dict[str, Path] = {}
        self.status = {"type": "status", "busy": False, "label": ""}
        self.last_active = 0.0
        self.local = localmodels.LocalModels(self._broadcast)
        self.mobile = mobile.Mobile(self._broadcast)
        self.prefs = {"phone_mode": "auto", "theme": "auto", "onboarded": False}   # phone: auto|always|off · theme: auto|light|dark
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
        """Is the Mac app open?"""
        return any(c["kind"] == "desktop" for c in self.clients.values())

    def recently_active(self, seconds: int = 600) -> bool:
        return self.has_clients() and time.time() - self.last_active < seconds

    def phone_visible(self) -> bool:
        """Is the phone app open on screen right now (so it sees replies without a notification)?"""
        return any(c["kind"] == "phone" and c.get("visible") for c in self.clients.values())

    async def _broadcast(self, ev: dict, kind: str | None = None) -> None:
        dead = []
        for ws, meta in list(self.clients.items()):
            if kind and meta["kind"] != kind:
                continue
            try:
                await ws.send_json(ev)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)

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

    async def ask_approval(self, summary: str, push: bool = False) -> bool:
        aid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        ev = {"type": "approval", "aid": aid, "summary": summary}
        self.pending[aid] = (fut, ev)
        await self.emit(ev)
        if push:
            asyncio.ensure_future(self.mobile.push(
                {"title": f"{config.AGENT_NAME} needs your OK", "body": " ".join(re.sub(r"^Needs approval:\s*", "", summary).split())[:200],
                 "tag": f"approval-{aid}",
                 "aid": aid, "approval": True}, urgency="high"))
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
        allowed = {"phone_mode": ("auto", "always", "off"), "theme": ("auto", "light", "dark"),
                   "onboarded": (True, False)}
        if any(value is v or value == v for v in allowed.get(key, ())):
            self.prefs[key] = value
            config.PREFS_FILE.write_text(json.dumps(self.prefs))
            await self._broadcast({"type": "prefs", "prefs": self.prefs})

    # -------------------------------------------------------------- auth -----
    def _local_host(self, request: web.Request) -> bool:
        return request.host in (f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}")

    def _phone_host(self, request: web.Request) -> bool:
        # tailscale serve passes the phone's Host (your-mac.tailnet.ts.net) through.
        h = self.mobile.host
        return bool(h) and (request.host == h or
                            (request.headers.get("X-Forwarded-Host") == h and self._local_host(request)))

    def _who(self, request: web.Request) -> tuple[str | None, dict | None]:
        """("desktop", None), ("phone", device) or (None, None)."""
        if self._local_host(request) and secrets.compare_digest(request.cookies.get(COOKIE, ""), self.token):
            return "desktop", None
        if self._phone_host(request) or self._local_host(request):
            dev = self.mobile.device_for(request.cookies.get(PHONE_COOKIE, ""))
            if dev:
                self.mobile.touch(dev)
                return "phone", dev
        return None, None

    def _authed(self, request: web.Request) -> bool:
        return self._who(request)[0] is not None

    def _origin_ok(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin", "")
        allowed = {f"http://127.0.0.1:{config.WEB_PORT}", f"http://localhost:{config.WEB_PORT}"}
        if self.mobile.host:
            allowed.add(f"{'https' if self.mobile.host.endswith('.ts.net') else 'http'}://{self.mobile.host}")
        return origin in allowed

    # ------------------------------------------------------------- routes ----
    async def index(self, request: web.Request):
        if request.query.get("t"):
            if not secrets.compare_digest(request.query["t"], self.token):
                return web.Response(status=403, text=f"Bad link. Open the {config.AGENT_NAME} app again.")
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 365)
            return resp
        if not self._authed(request):
            if self._phone_host(request) and (STATIC / "pair.html").exists():
                return web.FileResponse(STATIC / "pair.html", headers={"Cache-Control": "no-store"})
            return web.Response(status=403, text=f"Open the {config.AGENT_NAME} app (or run: python main.py --url) to sign in.")
        index = STATIC / "index.html"
        if not index.exists():
            return web.Response(status=500, text="The UI hasn't been built. Run: bash setup.sh")
        return web.FileResponse(index, headers={"Cache-Control": "no-store"})

    async def ws(self, request: web.Request):
        kind, device = self._who(request)
        if not kind or not self._origin_ok(request):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        meta = {"kind": kind, "device": device["id"] if device else None, "visible": True}
        self.clients[ws] = meta
        if kind == "desktop":
            self.last_active = time.time()
        hello = {"type": "hello", "agent": config.AGENT_NAME, "avatar": config.AGENT_AVATAR,
                 "model": (self.brain.registry.active_model() or {}).get("label", "No model yet"),
                 "telegram": bool(self.router.telegram), "prefs": self.prefs, "client": kind,
                 "phones": len(self.mobile.devices)}
        if device:
            hello.update(device={"id": device["id"], "name": device["name"], "push": bool(device.get("push"))},
                         vapid=self.mobile.public_key)
        await ws.send_json(hello)
        await ws.send_json({"type": "history", "events": list(self.history)})
        await ws.send_json(self.status)
        await ws.send_json({"type": "tasks", "items": self.scheduler.as_list()})
        await self.push_memory(ws)
        await ws.send_json({"type": "models", **self.brain.registry.public()})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        await self._on_client(json.loads(msg.data), meta, ws)
                    except Exception:
                        log.exception("bad client message")
        finally:
            self.clients.pop(ws, None)
        return ws

    async def _on_client(self, m: dict, meta: dict | None = None, ws=None) -> None:
        kind = m.get("type") or ""
        meta = meta or {"kind": "desktop", "device": None}
        on_phone = meta["kind"] == "phone"
        if not on_phone:
            self.last_active = time.time()
        if on_phone and (kind.startswith("model") or kind.startswith("local_") or kind.startswith("phone_")
                         or kind in ("memory_save", "memory_delete", "ollama_tags", "provider_models")):
            # Setup stays on the Mac: a lost phone can chat, but can't change keys, models or pairing.
            if kind not in ("model_select", "models_get"):
                await ws.send_json({"type": "toast", "error": True, "text": "Do that on your Mac."})
                return
        if kind == "send" and m.get("text", "").strip():
            text = m["text"].strip()
            source = "phone" if on_phone else "web"
            await self.emit({"type": "message", "role": "user", "text": text, "source": source})
            asyncio.ensure_future(self.brain.handle(text, origin=source))
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
        elif kind.startswith("model") or kind in ("ollama_tags", "provider_models"):
            await self._on_models(m)
        elif kind.startswith("local_"):
            await self._on_local(m)
        elif kind == "presence":
            meta["visible"] = bool(m.get("visible", True))   # desktop: last_active already updated
        elif kind == "push_subscribe" and on_phone:
            dev = next((d for d in self.mobile.devices if d["id"] == meta["device"]), None)
            if dev:
                if m.get("subscription"):
                    self.mobile.subscribe(dev, m["subscription"])
                    await self.mobile.push({"title": config.AGENT_NAME, "body": "Notifications are on.",
                                            "tag": "welcome"}, only=dev["id"])
                else:
                    self.mobile.unsubscribe(dev)
                await ws.send_json({"type": "device", "device": {"id": dev["id"], "name": dev["name"],
                                                                  "push": bool(dev.get("push"))}})
                await self.push_phone_status()
        elif kind.startswith("phone_"):
            await self._on_phone(m)

    # ------------------------------------------------------------ models -----
    async def push_models(self) -> None:
        await self._broadcast({"type": "models", **self.brain.registry.public()})
        await self._broadcast({"type": "hello_update",
                               "model": (self.brain.registry.active_model() or {}).get("label", "No model yet")})

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
                if m.get("use_now") or self.brain.client is None:
                    await self.brain.switch_model(saved["id"])
                    await self.emit({"type": "divider", "text": f"New conversation · {saved['label']}"})
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
            elif kind == "provider_models":
                key = (m.get("api_key") or "").strip() or (models.get_key(m["id"]) if m.get("id") else "")
                ok, items, err = await bridge.list_models(m.get("provider", ""), key, m.get("base_url", ""))
                await self._broadcast({"type": "provider_models", "ok": ok, "items": items[:500], "error": err,
                                       "provider": m.get("provider")})
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

    async def _local_done(self, name: str) -> None:
        """A local model finished downloading: add it to the model list."""
        reg = self.brain.registry
        existing = next((x for x in reg.models if x["provider"] == "ollama" and x["model"] == name), None)
        if not existing:
            existing = reg.save_model({"provider": "ollama", "model": name, "label": name,
                                       "context_tokens": config.OLLAMA_CONTEXT})
        if self.brain.client is None:
            await self.brain.switch_model(existing["id"])
        await self.push_models()
        await self._broadcast({"type": "toast", "text": f"{name} is ready. Pick it in the model menu to chat."})

    async def _on_local(self, m: dict) -> None:
        kind = m.get("type")
        try:
            if kind == "local_status":
                await self._broadcast({"type": "local_status", **(await self.local.status())})
            elif kind == "local_install":
                asyncio.ensure_future(self.local.install())
            elif kind == "local_start":
                asyncio.ensure_future(self.local.start())
            elif kind == "local_pull":
                name = (m.get("name") or "").strip()
                if not name:
                    raise ValueError("Enter a model name, for example gemma4:12b.")
                self.local.pull(name, self._local_done)
                await self._broadcast({"type": "local_status", **(await self.local.status())})
            elif kind == "local_cancel":
                self.local.cancel(m.get("name", ""))
            elif kind == "local_delete":
                name = m.get("name", "")
                await self.local.delete(name)
                reg = self.brain.registry
                for x in [x for x in reg.models if x["provider"] == "ollama" and x["model"] == name]:
                    if len(reg.models) > 1:
                        was_active = reg.active == x["id"]
                        reg.delete(x["id"])
                        if was_active:
                            await self.brain.switch_model(reg.active)
                await self.push_models()
                await self._broadcast({"type": "toast", "text": f"Removed {name}"})
        except ValueError as e:
            await self._broadcast({"type": "toast", "text": str(e), "error": True})
        except Exception as e:
            log.exception("local model action failed")
            await self._broadcast({"type": "toast", "text": f"Something went wrong: {str(e)[:200]}", "error": True})

    # ------------------------------------------------------------- phone ----
    async def push_phone_status(self) -> None:
        await self._broadcast({"type": "phone_status", **(await self.mobile.status())}, kind="desktop")

    async def _on_phone(self, m: dict) -> None:
        kind = m["type"]
        try:
            if kind == "phone_status":
                pass
            elif kind == "phone_new_code":
                self.mobile.new_code()
            elif kind == "phone_open_tailscale":
                await self.mobile.open_tailscale()
            elif kind == "phone_enable":
                asyncio.ensure_future(self._phone_enable())
                return
            elif kind == "phone_disable":
                await self.mobile.disable()
            elif kind == "phone_remove":
                dev_id = m.get("id", "")
                self.mobile.remove(dev_id)
                for ws, meta in list(self.clients.items()):
                    if meta.get("device") == dev_id:
                        await ws.close()
                await self._broadcast({"type": "toast", "text": "Phone removed. It's signed out now."}, kind="desktop")
            elif kind == "phone_rename":
                self.mobile.rename(m.get("id", ""), m.get("name", ""))
            elif kind == "phone_test":
                n = await self.mobile.push({"title": config.AGENT_NAME, "body": "Test notification from your Mac.",
                                            "tag": "test"}, urgency="high")
                await self._broadcast({"type": "toast", "error": not n,
                                       "text": f"Sent to {n} phone{'s' if n != 1 else ''}." if n else
                                       "No phone has notifications on yet. Turn them on in the phone app."},
                                      kind="desktop")
        except Exception as e:
            log.exception("phone action failed")
            await self._broadcast({"type": "toast", "error": True, "text": str(e)[:200]}, kind="desktop")
        await self.push_phone_status()

    async def _phone_enable(self) -> None:
        try:
            await self.mobile.enable()
            await self._broadcast({"type": "toast", "text": "Phone access is on."}, kind="desktop")
        except Exception as e:
            await self._broadcast({"type": "toast", "error": True, "text": str(e)[:300]}, kind="desktop")
        await self.push_phone_status()

    async def pair(self, request: web.Request):
        """A phone enters the one-time code shown on the Mac and gets its own key."""
        if not (self._phone_host(request) or self._local_host(request)) or not self._origin_ok(request):
            return web.json_response({"error": "Not allowed."}, status=403)
        try:
            body = await request.json()
            token = self.mobile.pair(body.get("code", ""), body.get("name", ""))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            return web.json_response({"error": "Something went wrong."}, status=400)
        resp = web.json_response({"ok": True})
        resp.set_cookie(PHONE_COOKIE, token, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 400,
                        secure=request.host.endswith(".ts.net"))
        dev = self.mobile.devices[-1]
        await self._broadcast({"type": "toast", "text": f"Paired {dev['name']}."}, kind="desktop")
        await self.emit({"type": "message", "role": "system", "text": f"Paired a new phone: {dev['name']}."})
        await self.push_phone_status()
        return resp

    async def approve_http(self, request: web.Request):
        """Approve / Deny buttons on an Android notification (sent by the service worker)."""
        kind, _ = self._who(request)
        if kind != "phone" or not self._origin_ok(request):
            return web.Response(status=403)
        body = await request.json()
        entry = self.pending.get(str(body.get("aid", "")))
        if not entry or entry[0].done():
            return web.json_response({"ok": False, "error": "Already answered."})
        entry[0].set_result(bool(body.get("approved")))
        return web.json_response({"ok": True})

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
        ctype = request.headers.get("Content-Type", "")
        ext = ".m4a" if "mp4" in ctype or "aac" in ctype else ".ogg" if "ogg" in ctype else ".webm"   # iPhone records mp4
        dest = config.INBOX / f"voice-{int(time.time())}{ext}"
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
        app.router.add_post("/pair", self.pair)
        app.router.add_post("/approve", self.approve_http)
        self.bridge = bridge.Bridge(self.brain.registry)
        self.bridge.routes(app)
        if (STATIC / "assets").exists():
            app.router.add_static("/assets", STATIC / "assets")
        # Public files the phone needs before it's paired (no personal data in any of them).
        for extra in ("favicon.svg", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png",
                      "icon-maskable-512.png", "apple-touch-icon.png"):
            if (STATIC / extra).exists():
                headers = {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"} if extra == "sw.js" else None
                app.router.add_get(f"/{extra}", lambda r, p=STATIC / extra, h=headers: web.FileResponse(p, headers=h))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.WEB_PORT).start()
        log.info("Desktop app at %s", app_url())
