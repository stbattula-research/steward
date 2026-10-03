"""Chat app connectors: talk to Steward from WhatsApp, iMessage, Discord or Slack.

Every connector works the same way:
  - Only its owner can use it. You prove you're the owner once, by sending the pairing
    code shown in the Mac app (iMessage uses your own number instead).
  - Replies go back to the app you wrote from. Scheduled tasks and heads-ups also go to
    every connector with "Alerts" on (following the Phone alerts setting).
  - Approvals: Steward asks, you reply YES or NO (WhatsApp shows buttons).
  - Commands: /stop, /new, /status, /tasks, /help. Voice notes are transcribed on the Mac.
  - Keys and tokens live in the macOS Keychain, never in a file.

How each one connects (no open ports on your Mac unless noted):
  Discord   bot over Discord's gateway (outgoing connection)
  Slack     Slack app in Socket Mode (outgoing connection)
  iMessage  your Mac's own Messages app (reads Messages' database, sends with AppleScript)
  WhatsApp  official WhatsApp Cloud API. Meta must reach a webhook, so this one uses
            Tailscale Funnel to publish ONE small endpoint (port 8443) that only accepts
            requests signed with your Meta app secret.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

import config
import voice

log = logging.getLogger("connectors")

CONFIG_FILE = config.STATE_DIR / "connectors.json"
SECRETS_FALLBACK = config.STATE_DIR / "connector_secrets.json"   # only off macOS (development)
KEYCHAIN_SERVICE = "steward-connector"
CLAIM_TTL = 30 * 60
YES = {"yes", "y", "approve", "approved", "ok", "okay", "✅", "👍", "sure", "go ahead"}
NO = {"no", "n", "deny", "denied", "stop", "cancel", "❌", "👎"}
AUDIO_EXT = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".caf", ".amr", ".webm")
HELP = ("I'm {agent}, your agent on your Mac. Just tell me what to do.\n"
        "/stop - stop the current task\n/new - start a fresh conversation\n"
        "/status - what I'm doing\n/tasks - scheduled tasks")


# ------------------------------------------------------------------ secrets --
def _keychain() -> bool:
    return shutil.which("security") is not None


def set_secret(kind: str, field: str, value: str) -> None:
    acct = f"{kind}:{field}"
    if _keychain():
        subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", acct,
                        "-l", f"Steward {kind} {field}", "-w", value], check=True, capture_output=True)
    else:
        data = _fallback()
        data[acct] = value
        SECRETS_FALLBACK.write_text(json.dumps(data))
        SECRETS_FALLBACK.chmod(0o600)


def get_secret(kind: str, field: str) -> str:
    acct = f"{kind}:{field}"
    if _keychain():
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", acct, "-w"],
                           capture_output=True, text=True)
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""
    return _fallback().get(acct, "")


def delete_secret(kind: str, field: str) -> None:
    acct = f"{kind}:{field}"
    if _keychain():
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", acct], capture_output=True)
    else:
        data = _fallback()
        data.pop(acct, None)
        SECRETS_FALLBACK.write_text(json.dumps(data))


def _fallback() -> dict:
    try:
        return json.loads(SECRETS_FALLBACK.read_text())
    except Exception:
        return {}


def _chunks(text: str, n: int) -> list[str]:
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


# --------------------------------------------------------------------- base --
class Connector:
    kind = ""
    label = ""
    secret_fields: tuple[str, ...] = ()
    setting_fields: tuple[str, ...] = ()
    max_len = 3500
    claims = True                 # owner proves themselves with a pairing code

    def __init__(self, mgr: "Connectors", cfg: dict):
        self.mgr, self.cfg = mgr, cfg
        self.state = "off"        # off | connecting | on | error
        self.error = ""
        self.pending: asyncio.Future | None = None
        self.task: asyncio.Task | None = None

    # ---- config helpers
    @property
    def owner(self) -> str:
        return str(self.cfg.get("owner") or "")

    @property
    def settings(self) -> dict:
        return self.cfg.setdefault("settings", {})

    def secret(self, field: str) -> str:
        return get_secret(self.kind, field)

    def configured(self) -> bool:
        return all(self.secret(f) for f in self.secret_fields) and all(self.settings.get(f) for f in self.setting_fields)

    def extra_status(self) -> dict:
        return {}

    def format(self, text: str) -> str:
        return text

    # ---- lifecycle (override)
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def _send(self, text: str) -> None: ...
    async def _send_file(self, path: Path, caption: str) -> None:
        await self._send(f"{caption}\n(File saved on your Mac: {path})".strip())

    # ---- channel protocol (used by the router)
    async def send_text(self, text: str) -> None:
        if not self.owner or self.state != "on":
            return
        try:
            for part in _chunks(self.format(text), self.max_len):
                await self._send(part)
        except Exception as e:
            self._fail_send(e)

    async def send_file(self, path: Path, caption: str = "") -> None:
        if not self.owner or self.state != "on":
            return
        try:
            await self._send_file(Path(path), caption)
        except Exception as e:
            self._fail_send(e)

    def _fail_send(self, e: Exception) -> None:
        log.warning("%s: couldn't send: %s", self.kind, e)
        self.error = f"Couldn't send a message: {str(e)[:200]}"
        asyncio.ensure_future(self.mgr.push_status())

    async def ask_approval(self, summary: str) -> bool:
        if not self.owner or self.state != "on":
            await asyncio.sleep(config.APPROVAL_TIMEOUT_SEC)
            return False
        fut = asyncio.get_running_loop().create_future()
        self.pending = fut
        try:
            await self._ask(summary)
            return await asyncio.wait_for(fut, timeout=config.APPROVAL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self.send_text("No answer, so I treated that as denied.")
            return False
        finally:
            if self.pending is fut:
                self.pending = None

    async def _ask(self, summary: str) -> None:
        await self.send_text(f"🔐 {summary[:3000]}\n\nReply YES to approve or NO to deny.")

    # ---- incoming
    async def incoming(self, sender: str, text: str, files: list[tuple[str, bytes]] | None = None,
                       approval: bool | None = None) -> None:
        """A message from the app. files: [(filename, data)]. approval: a button press."""
        text = (text or "").strip()
        if not self.owner:
            if self.claims and self.mgr.check_claim(self.kind, text):
                self.cfg["owner"] = sender
                self.mgr.save()
                await self.send_text(f"Paired ✅ This chat now talks to {config.AGENT_NAME} on your Mac. "
                                     "Send /help to see what you can do.")
                await self.mgr.notify({"type": "toast", "text": f"{self.label} is connected."})
                await self.mgr.push_status()
            return
        if str(sender) != self.owner:
            log.info("%s: ignored a message from someone else (%s)", self.kind, sender)
            return
        # An approval answer?
        if self.pending and not self.pending.done():
            word = text.lower().strip(" .!")
            if approval is not None or word in YES or word in NO:
                self.pending.set_result(approval if approval is not None else word in YES)
                await self._send("Approved." if (approval if approval is not None else word in YES) else "Denied.")
                return
        if approval is not None:
            return                      # a late button press
        # Attachments
        notes = []
        for name, data in files or []:
            safe = re.sub(r"[^\w.\- ]", "_", Path(name).name)[:80] or f"file-{int(time.time())}"
            dest = config.INBOX / f"{int(time.time())}-{safe}"
            dest.write_bytes(data)
            if dest.suffix.lower() in AUDIO_EXT and not text:
                try:
                    text = await voice.transcribe(dest)
                    await self._send(f"🎙 Heard: {text}")
                except Exception as e:
                    await self._send(f"Couldn't transcribe that voice note: {str(e)[:150]}")
                    return
            else:
                notes.append(f"[Attached file saved at: {dest}]")
        text = "\n\n".join([t for t in [text, *notes] if t])
        if not text:
            return
        if await self._command(text):
            return
        brain = self.mgr.brain
        if brain.busy:
            await self._send("Still on the previous task, I'll do this next. Send /stop to cancel it.")
        await self.mgr.router.mirror_user(text, self.kind)
        asyncio.ensure_future(brain.handle(text, origin=self.kind))

    async def _command(self, text: str) -> bool:
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        brain = self.mgr.brain
        if cmd == "/stop":
            await brain.stop_current()
            await self._send("Stopped.")
        elif cmd == "/new":
            await brain.stop_current()
            async with brain.lock:
                await brain.reset()
            await self._send("Fresh conversation started.")
        elif cmd == "/status":
            m = brain.registry.active_model()
            await self._send(f"{'Working' if brain.busy else 'Ready'} · model: {(m or {}).get('label', 'none yet')}")
        elif cmd == "/tasks":
            await self._send(self.mgr.scheduler.describe())
        elif cmd in ("/help", "/start"):
            await self._send(HELP.format(agent=config.AGENT_NAME))
        else:
            return False
        return True

    def public(self) -> dict:
        return {"kind": self.kind, "label": self.label, "configured": self.configured(),
                "enabled": bool(self.cfg.get("enabled")), "state": self.state, "error": self.error,
                "paired": bool(self.owner), "alerts": self.cfg.get("alerts", True),
                "claim": self.mgr.claim_code(self.kind) if (self.claims and not self.owner and self.state == "on") else "",
                "settings": {k: v for k, v in self.settings.items()}, **self.extra_status()}


# ----------------------------------------------------------------- Discord --
class Discord(Connector):
    kind, label = "discord", "Discord"
    secret_fields = ("token",)
    max_len = 1900

    def __init__(self, *a):
        super().__init__(*a)
        self.client = None

    def _client_id(self) -> str:
        try:
            first = self.secret("token").split(".")[0]
            return base64.b64decode(first + "=" * (-len(first) % 4)).decode()
        except Exception:
            return ""

    def extra_status(self) -> dict:
        cid = self._client_id()
        return {"invite_url": f"https://discord.com/oauth2/authorize?client_id={cid}&scope=bot&permissions=3072"
                if cid.isdigit() else ""}

    async def start(self) -> None:
        import discord
        intents = discord.Intents.none()
        intents.dm_messages = True
        client = discord.Client(intents=intents)
        self.client = client
        me = self

        @client.event
        async def on_ready():
            me.state, me.error = "on", ""
            await me.mgr.push_status()

        @client.event
        async def on_message(message):
            if message.author.bot or message.guild is not None:
                return
            files = []
            for a in message.attachments[:5]:
                if a.size <= 25_000_000:
                    files.append((a.filename, await a.read()))
            await me.incoming(str(message.author.id), message.content, files)

        async def run():
            try:
                await client.start(self.secret("token"))
            except Exception as e:
                me.state, me.error = "error", ("That bot token was rejected." if "token" in str(e).lower()
                                               else str(e)[:200])
                await me.mgr.push_status()
        self.task = asyncio.ensure_future(run())

    async def stop(self) -> None:
        if self.client:
            await self.client.close()
        self.client = None

    async def _user(self):
        return await self.client.fetch_user(int(self.owner))

    async def _send(self, text: str) -> None:
        if self.client and self.owner:
            await (await self._user()).send(text)

    async def _send_file(self, path: Path, caption: str) -> None:
        import discord
        await (await self._user()).send(content=caption[:1900] or None, file=discord.File(str(path)))


# ------------------------------------------------------------------- Slack --
SLACK_MANIFEST = {
    "display_information": {"name": "Steward", "description": "Your personal agent on your Mac"},
    "features": {"app_home": {"home_tab_enabled": False, "messages_tab_enabled": True,
                              "messages_tab_read_only_enabled": False},
                 "bot_user": {"display_name": "Steward", "always_online": True}},
    "oauth_config": {"scopes": {"bot": ["chat:write", "im:history", "im:read", "im:write",
                                        "files:read", "files:write"]}},
    "settings": {"event_subscriptions": {"bot_events": ["message.im"]}, "socket_mode_enabled": True,
                 "org_deploy_enabled": False, "token_rotation_enabled": False},
}


class Slack(Connector):
    kind, label = "slack", "Slack"
    secret_fields = ("bot_token", "app_token")

    def __init__(self, *a):
        super().__init__(*a)
        self.app = self.handler = None
        self.dm = ""

    def extra_status(self) -> dict:
        return {"manifest": json.dumps(SLACK_MANIFEST, indent=2)}

    def format(self, text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                 # Slack bold
        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)

    async def start(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp
        bot = self.secret("bot_token")
        try:
            app = AsyncApp(token=bot)
            await app.client.auth_test()
        except Exception as e:
            raise RuntimeError("Slack didn't accept the Bot token (xoxb-…).") from e
        me = self

        @app.event("message")
        async def on_message(event, client):
            if event.get("channel_type") != "im" or event.get("bot_id") or \
                    event.get("subtype") not in (None, "file_share"):
                return
            files = []
            async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {bot}"}) as s:
                for f in (event.get("files") or [])[:5]:
                    url = f.get("url_private_download")
                    if url and f.get("size", 0) <= 25_000_000:
                        async with s.get(url) as r:
                            if r.status == 200:
                                files.append((f.get("name") or "file", await r.read()))
            await me.incoming(event.get("user", ""), event.get("text", ""), files)

        self.app = app
        self.handler = AsyncSocketModeHandler(app, self.secret("app_token"))
        try:
            await self.handler.connect_async()
        except Exception as e:
            raise RuntimeError("Slack didn't accept the App token (xapp-…). It needs connections:write.") from e
        self.state = "on"

    async def stop(self) -> None:
        if self.handler:
            try:
                await self.handler.close_async()
            except Exception:
                pass
        self.app = self.handler = None

    async def _channel(self) -> str:
        if not self.dm:
            r = await self.app.client.conversations_open(users=self.owner)
            self.dm = r["channel"]["id"]
        return self.dm

    async def _send(self, text: str) -> None:
        await self.app.client.chat_postMessage(channel=await self._channel(), text=text)

    async def _send_file(self, path: Path, caption: str) -> None:
        await self.app.client.files_upload_v2(channel=await self._channel(), file=str(path),
                                              filename=path.name, initial_comment=caption or None)


# ---------------------------------------------------------------- iMessage --
ZWSP = "​"     # invisible marker on Steward's own messages, so it never answers itself
APPLE_EPOCH = 978307200


def _decode_attributed(blob) -> str:
    """Newer macOS keeps message text only in attributedBody (an archived NSAttributedString)."""
    if not blob:
        return ""
    b = bytes(blob)
    i = b.find(b"NSString")
    if i < 0:
        return ""
    b = b[i + 8:]
    j = b.find(b"+")
    if j < 0 or j > 8:
        return ""
    b = b[j + 1:]
    n, start = b[0], 1
    if n == 0x81:
        n, start = int.from_bytes(b[1:3], "little"), 3
    elif n == 0x82:
        n, start = int.from_bytes(b[1:4], "little"), 4
    return b[start:start + n].decode("utf-8", "replace")


class IMessage(Connector):
    kind, label = "imessage", "iMessage"
    setting_fields = ("handle",)
    claims = False
    max_len = 3000
    DB = config.HOME / "Library" / "Messages" / "chat.db"
    SEND = ('on run argv\n tell application "Messages"\n  set svc to 1st account whose service type = iMessage\n'
            '  send (item 1 of argv) to participant (item 2 of argv) of svc\n end tell\nend run')
    SEND_FILE = ('on run argv\n tell application "Messages"\n  set svc to 1st account whose service type = iMessage\n'
                 '  send (POSIX file (item 1 of argv)) to participant (item 2 of argv) of svc\n end tell\nend run')

    def __init__(self, *a):
        super().__init__(*a)
        self.last = 0
        self.recent: dict[str, float] = {}

    @property
    def owner(self) -> str:                  # your own number or Apple ID email
        return self.settings.get("handle", "")

    def extra_status(self) -> dict:
        return {"python": sys.executable, "db_ok": self._db_ok()}

    def format(self, text: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"\1", text)

    def _handles(self) -> list[str]:
        h = self.owner.strip()
        if "@" in h:
            return [h.lower()]
        digits = re.sub(r"[^\d+]", "", h)
        out = {digits, digits.lstrip("+")}
        if not digits.startswith("+"):
            out |= {"+" + digits, "+1" + digits}
        return [x for x in out if x]

    def _db(self):
        return sqlite3.connect(f"file:{self.DB}?mode=ro", uri=True, timeout=5)

    def _db_ok(self) -> bool:
        try:
            with self._db() as c:
                c.execute("SELECT 1 FROM message LIMIT 1")
            return True
        except Exception:
            return False

    def _poll(self) -> list[dict]:
        hs = self._handles()
        q = ("SELECT m.ROWID, m.text, m.attributedBody, m.date FROM message m "
             "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID JOIN chat c ON c.ROWID = cmj.chat_id "
             f"WHERE c.chat_identifier IN ({','.join('?' * len(hs))}) AND m.ROWID > ? ORDER BY m.ROWID")
        out = []
        with self._db() as c:
            rows = c.execute(q, (*hs, self.last)).fetchall()
            for rowid, text, body, date in rows:
                self.last = max(self.last, rowid)
                text = text or _decode_attributed(body)
                atts = c.execute("SELECT a.filename, a.transfer_name FROM attachment a JOIN message_attachment_join "
                                 "maj ON maj.attachment_id = a.ROWID WHERE maj.message_id = ?", (rowid,)).fetchall()
                out.append({"id": rowid, "text": text or "", "date": date, "files": [
                    (Path(f.replace("~", str(config.HOME), 1)), n or Path(f).name) for f, n in atts if f]})
        return out

    async def start(self) -> None:
        if not self._db_ok():
            raise RuntimeError("Steward can't read your Messages yet. Give Full Disk Access to the Python app "
                               "shown below (System Settings → Privacy & Security → Full Disk Access).")
        with self._db() as c:
            self.last = c.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()[0]
        self.state = "on"
        self.task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while self.state == "on":
            try:
                for msg in await asyncio.to_thread(self._poll):
                    text = msg["text"]
                    if text.startswith(ZWSP):
                        continue                          # one of Steward's own replies
                    key = f"{text}|{len(msg['files'])}"
                    now = time.time()
                    if now - self.recent.get(key, 0) < 15:  # the same note to self can appear twice
                        continue
                    self.recent = {k: t for k, t in self.recent.items() if now - t < 60}
                    self.recent[key] = now
                    files = []
                    for path, name in msg["files"][:5]:
                        try:
                            files.append((name, path.read_bytes()))
                        except Exception:
                            pass
                    text = text.replace("￼", "").strip()   # attachment placeholder character
                    await self.incoming(self.owner, text, files)
            except Exception as e:
                log.warning("iMessage poll failed: %s", e)
            await asyncio.sleep(2)

    async def stop(self) -> None:
        self.state = "off"
        if self.task:
            self.task.cancel()

    async def _osa(self, script: str, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec("osascript", "-e", script, *args,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), 30)
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip()[:200] or "Messages refused to send.")

    async def _send(self, text: str) -> None:
        await self._osa(self.SEND, ZWSP + text, self.owner)

    async def _send_file(self, path: Path, caption: str) -> None:
        # Messages can only send files from places it's allowed to read, like ~/Pictures.
        dest_dir = config.HOME / "Pictures" / "Steward"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        shutil.copy(path, dest)
        await self._osa(self.SEND_FILE, str(dest), self.owner)
        if caption:
            await self._send(caption)


# ---------------------------------------------------------------- WhatsApp --
GRAPH = "https://graph.facebook.com/v26.0"
WEBHOOK_PORT_OFFSET = 2          # webhook listens on 127.0.0.1:(WEB_PORT + 2)
FUNNEL_PORT = 8443


class WhatsApp(Connector):
    kind, label = "whatsapp", "WhatsApp"
    secret_fields = ("access_token", "app_secret")
    setting_fields = ("phone_number_id",)
    max_len = 4000
    graph = GRAPH

    def __init__(self, *a):
        super().__init__(*a)
        self.runner = None
        self.funnel = {"on": False, "link": "", "url": ""}
        self.window_note = ""

    def verify_token(self) -> str:
        if not self.settings.get("verify_token"):
            self.settings["verify_token"] = secrets.token_urlsafe(18)
            self.mgr.save()
        return self.settings["verify_token"]

    def extra_status(self) -> dict:
        host = self.mgr.web.mobile.host if self.mgr.web else ""
        url = f"https://{host}:{FUNNEL_PORT}/whatsapp" if host.endswith(".ts.net") else ""
        return {"webhook_url": url, "verify_token": self.verify_token(), "funnel": self.funnel,
                "window_note": self.window_note}

    def format(self, text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                 # WhatsApp bold
        return re.sub(r"^#+\s*(.+)$", r"*\1*", text, flags=re.M)

    async def start(self) -> None:
        from aiohttp import web
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_get("/whatsapp", self._verify)
        app.router.add_post("/whatsapp", self._webhook)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", config.WEB_PORT + WEBHOOK_PORT_OFFSET).start()
        # Check the token and phone number id
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{self.graph}/{self.settings['phone_number_id']}",
                             headers=self._auth()) as r:
                if r.status != 200:
                    raise RuntimeError("Meta didn't accept the access token or phone number ID.")
        self.state = "on"
        asyncio.ensure_future(self.ensure_funnel())

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
        self.runner = None

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.secret('access_token')}"}

    # ---- Tailscale Funnel (public HTTPS for Meta's webhook, this one endpoint only)
    async def ensure_funnel(self) -> None:
        import mobile
        exe = mobile.tailscale_bin()
        if not exe or not self.mgr.web or not self.mgr.web.mobile.host.endswith(".ts.net"):
            self.funnel = {"on": False, "link": "", "url": "",
                           "note": "Set up Tailscale first (Phone app panel, steps 1 and 2)."}
            await self.mgr.push_status()
            return
        proc = await asyncio.create_subprocess_exec(
            exe, "funnel", "--bg", f"--https={FUNNEL_PORT}",
            f"http://127.0.0.1:{config.WEB_PORT + WEBHOOK_PORT_OFFSET}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def read():
            async for raw in proc.stdout:
                m = re.search(r"https://login\.tailscale\.com/\S+", raw.decode(errors="replace"))
                if m and not self.funnel.get("link"):
                    self.funnel["link"] = m.group(0)
                    await self.mgr.push_status()
        try:
            await asyncio.wait_for(asyncio.gather(read(), proc.wait()), 600)
        except asyncio.TimeoutError:
            proc.kill()
        self.funnel = {"on": proc.returncode == 0, "link": "", "url": self.extra_status()["webhook_url"]}
        await self.mgr.push_status()

    # ---- webhook
    async def _verify(self, request):
        from aiohttp import web
        q = request.query
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), self.verify_token()):
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403)

    async def _webhook(self, request):
        from aiohttp import web
        raw = await request.read()
        sig = request.headers.get("X-Hub-Signature-256", "")
        good = "sha256=" + hmac.new(self.secret("app_secret").encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            log.warning("WhatsApp webhook with a bad signature was rejected")
            return web.Response(status=403)
        try:
            data = json.loads(raw)
        except Exception:
            return web.Response(status=400)
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for msg in (change.get("value") or {}).get("messages", []):
                    asyncio.ensure_future(self._handle(msg))
        return web.Response(text="ok")

    async def _media(self, media_id: str) -> bytes:
        async with aiohttp.ClientSession(headers=self._auth()) as s:
            async with s.get(f"{self.graph}/{media_id}") as r:
                url = (await r.json()).get("url")
            async with s.get(url) as r:
                return await r.read()

    async def _handle(self, msg: dict) -> None:
        sender, kind = msg.get("from", ""), msg.get("type")
        try:
            if kind == "text":
                await self.incoming(sender, msg["text"].get("body", ""))
            elif kind == "interactive":
                reply = (msg.get("interactive") or {}).get("button_reply") or {}
                rid = reply.get("id", "")
                await self.incoming(sender, reply.get("title", ""),
                                    approval=True if rid.startswith("approve") else False if rid.startswith("deny") else None)
            elif kind == "button":
                await self.incoming(sender, (msg.get("button") or {}).get("text", ""))
            elif kind in ("audio", "image", "document", "video", "sticker"):
                m = msg.get(kind) or {}
                ext = {"audio": ".ogg", "image": ".jpg", "video": ".mp4", "sticker": ".webp"}.get(kind, "")
                name = m.get("filename") or f"whatsapp-{kind}{ext}"
                data = await self._media(m["id"])
                await self.incoming(sender, m.get("caption", ""), [(name, data)])
        except Exception:
            log.exception("WhatsApp message failed")

    # ---- sending
    async def _post(self, payload: dict) -> dict:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": self.owner, **payload}
        async with aiohttp.ClientSession(headers=self._auth()) as s:
            async with s.post(f"{self.graph}/{self.settings['phone_number_id']}/messages", json=payload) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    err = (data or {}).get("error", {})
                    if err.get("code") in (131047, 131026):
                        self.window_note = ("WhatsApp only lets Steward message you within 24 hours of your last "
                                            "message. Send it any message to reopen the chat.")
                        await self.mgr.push_status()
                    raise RuntimeError(err.get("message") or f"HTTP {r.status}")
                self.window_note = ""
                return data

    async def _send(self, text: str) -> None:
        await self._post({"type": "text", "text": {"body": text, "preview_url": False}})

    async def _ask(self, summary: str) -> None:
        body = summary[:1000]
        try:
            await self._post({"type": "interactive", "interactive": {
                "type": "button", "body": {"text": f"🔐 {body}"},
                "action": {"buttons": [{"type": "reply", "reply": {"id": "approve", "title": "Approve"}},
                                       {"type": "reply", "reply": {"id": "deny", "title": "Deny"}}]}}})
        except Exception:
            await super()._ask(summary)

    async def _send_file(self, path: Path, caption: str) -> None:
        import mimetypes
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field("messaging_product", "whatsapp")
        form.add_field("type", mime)
        form.add_field("file", path.read_bytes(), filename=path.name, content_type=mime)
        async with aiohttp.ClientSession(headers=self._auth()) as s:
            async with s.post(f"{self.graph}/{self.settings['phone_number_id']}/media", data=form) as r:
                media = (await r.json(content_type=None)).get("id")
        if not media:
            raise RuntimeError("WhatsApp didn't accept the file.")
        kind = "image" if mime.startswith("image/") else "document"
        obj = {"id": media, "caption": caption[:1000]} if caption else {"id": media}
        if kind == "document":
            obj["filename"] = path.name
        await self._post({"type": kind, kind: obj})


# ----------------------------------------------------------------- manager --
KINDS = {c.kind: c for c in (WhatsApp, IMessage, Discord, Slack)}


class Connectors:
    def __init__(self, router, brain, scheduler, notify, web=None):
        self.router, self.brain, self.scheduler, self.notify, self.web = router, brain, scheduler, notify, web
        self.cfg: dict = {}
        self.items: dict[str, Connector] = {}
        self.claims: dict[str, tuple[str, float]] = {}
        try:
            self.cfg = json.loads(CONFIG_FILE.read_text())
        except Exception:
            self.cfg = {}
        for kind, cls in KINDS.items():
            self.items[kind] = cls(self, self.cfg.setdefault(kind, {}))

    def save(self) -> None:
        CONFIG_FILE.write_text(json.dumps(self.cfg, indent=2))
        CONFIG_FILE.chmod(0o600)

    # ---- pairing codes
    def claim_code(self, kind: str) -> str:
        code, exp = self.claims.get(kind, ("", 0))
        if not code or time.time() > exp:
            code = f"{secrets.randbelow(900000) + 100000}"
            self.claims[kind] = (code, time.time() + CLAIM_TTL)
        return code

    def check_claim(self, kind: str, text: str) -> bool:
        code, exp = self.claims.get(kind, ("", 0))
        given = re.sub(r"\D", "", text or "")
        if code and time.time() < exp and hmac.compare_digest(given, code):
            self.claims.pop(kind, None)
            return True
        return False

    # ---- running
    def running(self) -> list[Connector]:
        return [c for c in self.items.values() if c.state == "on" and c.owner]

    async def start_all(self) -> None:
        for c in self.items.values():
            if c.cfg.get("enabled") and c.configured():
                await self._start(c)

    async def _start(self, c: Connector) -> None:
        c.state, c.error = "connecting", ""
        await self.push_status()
        try:
            await c.start()
            if c.state == "connecting" and c.kind != "discord":
                c.state = "on"
        except Exception as e:
            log.warning("%s didn't start: %s", c.kind, e)
            c.state, c.error = "error", str(e)[:300]
            try:
                await c.stop()
            except Exception:
                pass
        await self.push_status()

    async def stop_all(self) -> None:
        for c in self.items.values():
            try:
                await c.stop()
            except Exception:
                pass

    async def status(self) -> dict:
        tg = self.router.telegram
        host = self.web.mobile.host if self.web else ""
        return {"items": [c.public() for c in self.items.values()],
                "telegram": {"connected": bool(tg)}, "tailscale": host.endswith(".ts.net")}

    async def push_status(self) -> None:
        await self.notify({"type": "connectors", **(await self.status())}, desktop_only=True)

    # ---- changes from the app
    async def configure(self, kind: str, fields: dict) -> None:
        c = self.items.get(kind)
        if not c:
            raise ValueError("Unknown app.")
        for f in c.secret_fields:
            v = str(fields.get(f) or "").strip()
            if v:
                set_secret(kind, f, v)
        for f in c.setting_fields:
            v = str(fields.get(f) or "").strip()
            if v:
                c.settings[f] = v
        if not c.configured():
            missing = [f for f in (*c.secret_fields, *c.setting_fields)
                       if not (c.secret(f) if f in c.secret_fields else c.settings.get(f))]
            raise ValueError("Fill in: " + ", ".join(x.replace("_", " ") for x in missing))
        c.cfg["enabled"] = True
        self.save()
        await c.stop()
        await self._start(c)

    async def disable(self, kind: str) -> None:
        c = self.items[kind]
        c.cfg["enabled"] = False
        self.save()
        await c.stop()
        c.state, c.error = "off", ""
        await self.push_status()

    async def remove(self, kind: str) -> None:
        c = self.items[kind]
        await c.stop()
        for f in c.secret_fields:
            delete_secret(kind, f)
        c.cfg.clear()
        c.state, c.error = "off", ""
        self.save()
        await self.push_status()

    async def unpair(self, kind: str) -> None:
        self.items[kind].cfg.pop("owner", None)
        self.save()
        await self.push_status()

    async def set_alerts(self, kind: str, on: bool) -> None:
        self.items[kind].cfg["alerts"] = bool(on)
        self.save()
        await self.push_status()

    async def test(self, kind: str) -> None:
        c = self.items[kind]
        if c.state != "on" or not c.owner:
            raise ValueError("Connect and pair it first.")
        c.error = ""
        await c.send_text(f"👋 Test message from {config.AGENT_NAME} on your Mac.")
        await self.push_status()
