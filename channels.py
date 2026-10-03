"""Routes the agent's replies to wherever the owner is: the Mac app, the phone app, Telegram.

- A request typed in the Mac app is answered there; one from the phone app is answered in
  the phone app (with a notification if the app isn't open on screen); one from Telegram is
  answered in Telegram. Everything is mirrored into the shared conversation history.
- Scheduled tasks and heads-ups go to the apps, and also to your phone unless you've used
  the Mac app in the last few minutes (so you aren't double-pinged at your desk).
- The "Phone alerts" switch overrides that: Always = everything also goes to your phone;
  Off = only replies to things you asked from your phone (or when the Mac app is closed).
- "Your phone" means the phone app's notifications when a phone is paired with
  notifications on; otherwise Telegram, if it's set up.
"""
import asyncio
import logging
from pathlib import Path

log = logging.getLogger("router")


class Router:
    def __init__(self):
        self.web = None        # webserver.WebUI
        self.telegram = None   # telegram_bot.TelegramChannel
        self.origin = "web"    # web | telegram | background

    def begin(self, origin: str) -> None:
        self.origin = origin

    def _phone_mode(self) -> str:
        return self.web.prefs.get("phone_mode", "auto") if self.web else "always"

    def _away(self) -> bool:
        """Should this also reach the owner's phone?"""
        if self.origin in ("telegram", "phone"):
            return True
        desk = bool(self.web and self.web.has_clients())
        mode = self._phone_mode()
        if mode == "always":
            return True
        if mode == "off":
            return not desk                    # never leave you with no way to see it
        if self.origin == "background":
            return not (self.web and self.web.recently_active())
        return not desk                        # Mac request, but the Mac app was closed

    def _push_ready(self) -> bool:
        return bool(self.web and self.web.mobile.has_push())

    def _to_push(self) -> bool:
        return (self._push_ready() and self.origin != "telegram" and self._away()
                and not self.web.phone_visible())

    def _to_telegram(self) -> bool:
        if not self.telegram:
            return False
        if self.origin == "telegram":
            return True
        # The phone app replaces Telegram for alerts once it has notifications on.
        return self.origin != "phone" and not self._push_ready() and self._away()

    def _notify_phone(self, payload: dict, urgency: str = "normal") -> None:
        asyncio.ensure_future(self.web.mobile.push(payload, urgency))

    @staticmethod
    def _snippet(text: str, n: int = 180) -> str:
        text = " ".join(str(text).replace("**", "").split())
        return text if len(text) <= n else text[: n - 1] + "…"

    # ------------------------------------------------------- Channel protocol --
    async def send_text(self, text: str) -> None:
        if self.web:
            await self.web.emit({"type": "message", "role": "assistant", "text": text})
        if self._to_push():
            from config import AGENT_NAME
            self._notify_phone({"title": AGENT_NAME, "body": self._snippet(text), "tag": "reply"})
        if self._to_telegram():
            await self.telegram.send_text(text)

    async def send_file(self, path: Path, caption: str = "") -> None:
        if self.web:
            await self.web.emit_file(path, caption)
        if self._to_push():
            from config import AGENT_NAME
            self._notify_phone({"title": AGENT_NAME, "body": self._snippet(caption or f"Sent you {path.name}"),
                                "tag": "file"})
        if self._to_telegram():
            await self.telegram.send_file(path, caption)

    async def ask_approval(self, summary: str) -> bool:
        asks = []
        phone = self._push_ready() and self.origin != "telegram" and (self._away() or not self.web.has_clients())
        if self.web and (self.web.clients or self.origin in ("web", "phone") or phone):
            asks.append(self.web.ask_approval(summary, push=phone and not self.web.phone_visible()))
        if self.telegram and (self.origin == "telegram" or self._to_telegram()
                              or not (self.web and (self.web.clients or phone))):
            asks.append(self.telegram.ask_approval(summary))
        if not asks:
            return False
        tasks = [asyncio.ensure_future(a) for a in asks]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:            # first answer wins; withdraw the other prompt
            t.cancel()
        result = next(iter(done)).result()
        if self.web:
            await self.web.resolve_all_approvals(result)
        return result

    # ------------------------------------------------------------ extras ----
    async def on_tool(self, name: str, summary: str) -> None:
        if self.web:
            await self.web.emit({"type": "tool", "name": name, "summary": summary})

    async def set_status(self, busy: bool, label: str = "") -> None:
        if self.web:
            await self.web.set_status(busy, label)

    async def mirror_user(self, text: str, source: str) -> None:
        if self.web:
            await self.web.emit({"type": "message", "role": "user", "text": text, "source": source})
