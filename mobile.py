"""The phone app: paired phones, pairing codes, Tailscale access and push notifications.

How your phone reaches your Mac
  Tailscale is a free private network between your own devices. `tailscale serve` makes
  the app reachable at https://<your-mac>.<tailnet>.ts.net, only to devices signed in to
  your Tailscale account. Nothing is opened to the public internet, and there is no
  server in the middle.

How a phone signs in
  The Mac app shows a short one-time code (valid for 10 minutes). The phone enters it
  once and gets its own long random key in a cookie. Only a hash of that key is stored
  here, and each phone can be removed from the Mac app at any time.

Push notifications
  Standard Web Push (works on iPhone with iOS 16.4+ once the app is on the Home Screen,
  and on Android). Messages are end-to-end encrypted to the phone; Apple and Google only
  relay them.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid

import config

log = logging.getLogger("mobile")

DEVICES_FILE = config.STATE_DIR / "devices.json"
VAPID_FILE = config.STATE_DIR / "vapid_private.pem"
PHONE_FILE = config.STATE_DIR / "phone.json"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_TTL = 600
MAX_ATTEMPTS = 10
TAILSCALE_DOWNLOAD = "https://tailscale.com/download"
VAPID_SUB = os.getenv("STEWARD_PUSH_CONTACT", "https://github.com")   # push services want a contact URL or mailto:


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tailscale_bin() -> str | None:
    for p in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"):
        if p and os.path.exists(p):
            return p
    return None


class Mobile:
    def __init__(self, notify):
        """notify(event) sends updates to the open apps."""
        self.notify = notify
        self.devices: list[dict] = []
        self.code = ""
        self.code_expires = 0.0
        self.attempts = 0
        self.enabling = False
        self.enable_link = ""
        self.host = os.getenv("STEWARD_PHONE_HOST", "").strip()   # developer override for tests
        self._ts_cache: dict = {}
        self._load()
        if not self.host:
            try:
                self.host = json.loads(PHONE_FILE.read_text()).get("host", "")
            except Exception:
                pass
        self._vapid = None
        self.public_key = ""
        self._init_vapid()

    # --------------------------------------------------------------- devices --
    def _load(self) -> None:
        try:
            self.devices = json.loads(DEVICES_FILE.read_text()).get("devices", [])
        except Exception:
            self.devices = []

    def _save(self) -> None:
        DEVICES_FILE.write_text(json.dumps({"devices": self.devices}, indent=2))
        DEVICES_FILE.chmod(0o600)

    def device_for(self, token: str) -> dict | None:
        if not token:
            return None
        h = _hash(token)
        for d in self.devices:
            if secrets.compare_digest(d["token_hash"], h):
                return d
        return None

    def touch(self, device: dict) -> None:
        now = int(time.time())
        if now - device.get("last_seen", 0) > 300:
            device["last_seen"] = now
            self._save()

    def public_devices(self) -> list[dict]:
        return [{"id": d["id"], "name": d["name"], "created": d["created"], "last_seen": d.get("last_seen", 0),
                 "push": bool(d.get("push"))} for d in self.devices]

    def remove(self, device_id: str) -> None:
        self.devices = [d for d in self.devices if d["id"] != device_id]
        self._save()

    def rename(self, device_id: str, name: str) -> None:
        for d in self.devices:
            if d["id"] == device_id:
                d["name"] = name.strip()[:40] or d["name"]
        self._save()

    # --------------------------------------------------------------- pairing --
    def new_code(self) -> str:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self.code = f"{raw[:4]}-{raw[4:]}"
        self.code_expires = time.time() + CODE_TTL
        self.attempts = 0
        return self.code

    def current_code(self) -> str:
        if not self.code or time.time() > self.code_expires:
            return self.new_code()
        return self.code

    def pair(self, code: str, name: str) -> str:
        """Check a pairing code. Returns the new phone's secret key, or raises ValueError."""
        if not self.code or time.time() > self.code_expires:
            raise ValueError("That code has expired. Open Phone app on your Mac for a new one.")
        if self.attempts >= MAX_ATTEMPTS:
            raise ValueError("Too many wrong codes. Open Phone app on your Mac for a new one.")
        given = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
        if not secrets.compare_digest(given, self.code.replace("-", "")):
            self.attempts += 1
            raise ValueError("That code doesn't match. Check the code on your Mac.")
        self.code = ""                       # one use only
        token = secrets.token_urlsafe(32)
        self.devices.append({"id": uuid.uuid4().hex[:10], "name": (name or "My phone").strip()[:40],
                             "token_hash": _hash(token), "created": int(time.time()),
                             "last_seen": int(time.time()), "push": None})
        self._save()
        return token

    # ------------------------------------------------------------ tailscale --
    async def _ts(self, *args, timeout=10) -> tuple[int, str]:
        exe = tailscale_bin()
        if not exe:
            return 127, "Tailscale isn't installed."
        proc = await asyncio.create_subprocess_exec(exe, *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "Tailscale didn't answer."
        return proc.returncode, out.decode(errors="replace")

    def _serving(self, serve_json: str) -> bool:
        try:
            cfg = json.loads(serve_json or "{}")
        except Exception:
            return False
        for site in (cfg.get("Web") or {}).values():
            for h in (site.get("Handlers") or {}).values():
                if re.search(rf"(127\.0\.0\.1|localhost):{config.WEB_PORT}\b", h.get("Proxy", "")):
                    return True
        return False

    async def status(self) -> dict:
        st = {"installed": bool(tailscale_bin()) or os.path.isdir("/Applications/Tailscale.app"),
              "running": False, "signed_in": False, "host": self.host, "serving": False,
              "enabling": self.enabling, "enable_link": self.enable_link, "url": "",
              "download": TAILSCALE_DOWNLOAD, "devices": self.public_devices(),
              "push_ready": bool(self.public_key)}
        if os.getenv("STEWARD_PHONE_HOST"):          # test mode: pretend Tailscale is set up
            st.update(installed=True, running=True, signed_in=True, serving=True)
        elif tailscale_bin():
            code, out = await self._ts("status", "--json")
            try:
                data = json.loads(out)
                state = data.get("BackendState", "")
                st["running"] = state not in ("", "Stopped", "NoState")
                st["signed_in"] = state == "Running"
                dns = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
                if dns:
                    st["host"] = dns
                    if dns != self.host:
                        self.host = dns
                        PHONE_FILE.write_text(json.dumps({"host": dns}))
            except Exception:
                pass
            if st["signed_in"]:
                _, out = await self._ts("serve", "status", "--json")
                st["serving"] = self._serving(out)
        if st["serving"] and st["host"]:
            scheme = "https" if st["host"].endswith(".ts.net") else "http"
            st["url"] = f"{scheme}://{st['host']}/"
            st["code"] = self.current_code()
            st["code_expires"] = int(self.code_expires)
            st["pair_url"] = f"{st['url']}?code={st['code'].replace('-', '')}"
            try:
                import segno
                st["qr_svg"] = segno.make(st["pair_url"], error="m").svg_inline(
                    scale=5, border=2, dark="#111111", light="#ffffff")
            except Exception:
                st["qr_svg"] = ""
        return st

    async def open_tailscale(self) -> None:
        subprocess.run(["open", "-a", "Tailscale"], check=False)

    async def enable(self) -> None:
        """`tailscale serve` the app over HTTPS, inside your tailnet only.
        If HTTPS isn't enabled for the tailnet yet, Tailscale prints a link to turn it on and
        waits; we show that link in the app and finish once it's approved."""
        if self.enabling:
            return
        exe = tailscale_bin()
        if not exe:
            raise RuntimeError("Install Tailscale first.")
        self.enabling, self.enable_link = True, ""
        await self.notify({"type": "phone_status", **(await self.status())})
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "serve", "--bg", f"http://127.0.0.1:{config.WEB_PORT}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            lines = []

            async def read():
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    lines.append(line)
                    m = re.search(r"https://login\.tailscale\.com/\S+", line)
                    if m and not self.enable_link:
                        self.enable_link = m.group(0)
                        await self.notify({"type": "phone_status", **(await self.status())})
            try:
                await asyncio.wait_for(asyncio.gather(read(), proc.wait()), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("Timed out waiting for Tailscale. Try again.")
            if proc.returncode != 0:
                tail = " ".join(lines[-3:])[:300]
                raise RuntimeError(f"Tailscale couldn't turn on phone access: {tail}")
        finally:
            self.enabling, self.enable_link = False, ""
            await self.notify({"type": "phone_status", **(await self.status())})

    async def disable(self) -> None:
        await self._ts("serve", "--https=443", "off")
        await self.notify({"type": "phone_status", **(await self.status())})

    # ------------------------------------------------------------------ push --
    def _init_vapid(self) -> None:
        try:
            from cryptography.hazmat.primitives import serialization
            from py_vapid import Vapid
        except Exception:
            log.warning("pywebpush isn't installed; phone notifications are off.")
            return
        try:
            if VAPID_FILE.exists():
                v = Vapid.from_file(str(VAPID_FILE))
            else:
                v = Vapid()
                v.generate_keys()
                v.save_key(str(VAPID_FILE))
                VAPID_FILE.chmod(0o600)
            raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
            self.public_key = base64.urlsafe_b64encode(raw).decode().rstrip("=")
            self._vapid = v
        except Exception:
            log.exception("could not set up push keys")

    def subscribe(self, device: dict, sub: dict) -> None:
        if not (isinstance(sub, dict) and str(sub.get("endpoint", "")).startswith("https://")
                and (sub.get("keys") or {}).get("p256dh") and (sub.get("keys") or {}).get("auth")):
            raise ValueError("That notification subscription looks wrong.")
        device["push"] = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"],
                                                                "auth": sub["keys"]["auth"]}}
        self._save()

    def unsubscribe(self, device: dict) -> None:
        device["push"] = None
        self._save()

    def has_push(self) -> bool:
        return bool(self._vapid) and any(d.get("push") for d in self.devices)

    def _send_one(self, device: dict, payload: dict, urgency: str) -> str:
        from pywebpush import WebPushException, webpush
        try:
            webpush(device["push"], json.dumps(payload), vapid_private_key=self._vapid,
                    vapid_claims={"sub": VAPID_SUB}, ttl=12 * 3600,
                    headers={"Urgency": urgency}, timeout=10)
            return "ok"
        except WebPushException as e:
            code = getattr(e.response, "status_code", 0)
            if code in (404, 410):
                return "gone"
            log.warning("push failed (%s): %s", code, str(e)[:200])
            return "error"
        except Exception as e:
            log.warning("push failed: %s", str(e)[:200])
            return "error"

    async def push(self, payload: dict, urgency: str = "normal", only: str | None = None) -> int:
        """Send a notification to every phone with notifications on. Returns how many got it."""
        if not self._vapid:
            return 0
        targets = [d for d in self.devices if d.get("push") and (only is None or d["id"] == only)]
        sent = 0
        for d in targets:
            res = await asyncio.to_thread(self._send_one, d, payload, urgency)
            if res == "gone":                 # the phone turned notifications off or removed the app
                d["push"] = None
                self._save()
            sent += res == "ok"
        return sent
