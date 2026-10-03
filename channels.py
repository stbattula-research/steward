"""Routes the agent's replies to wherever the owner is: the desktop app, Telegram, or both.

- A request typed in the desktop app is answered in the desktop app.
- A request sent from Telegram is answered in Telegram (and mirrored into the app's history).
- Scheduled tasks and heads-ups go to the app, and also to Telegram unless the owner has used
  the app in the last few minutes (so you aren't double-pinged at your desk).
- The app's "Phone alerts" switch overrides that: Always = everything also goes to
  Telegram; Off = only replies to Telegram messages (or when the app is closed).
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

    def _to_telegram(self) -> bool:
        if not self.telegram:
            return False
        if self.origin == "telegram":
            return True
        mode = self._phone_mode()
        if mode == "always":
            return True
        if mode == "off":
            return not (self.web and self.web.has_clients())   # never leave you with no way to see it
        if self.origin == "background":
            return not (self.web and self.web.recently_active())
        return not (self.web and self.web.has_clients())   # web request but the app was closed

    # ------------------------------------------------------- Channel protocol --
    async def send_text(self, text: str) -> None:
        if self.web:
            await self.web.emit({"type": "message", "role": "assistant", "text": text})
        if self._to_telegram():
            await self.telegram.send_text(text)

    async def send_file(self, path: Path, caption: str = "") -> None:
        if self.web:
            await self.web.emit_file(path, caption)
        if self._to_telegram():
            await self.telegram.send_file(path, caption)

    async def ask_approval(self, summary: str) -> bool:
        asks = []
        if self.web and (self.web.has_clients() or self.origin == "web"):
            asks.append(self.web.ask_approval(summary))
        if self.telegram and (self.origin == "telegram" or self._to_telegram()
                              or not (self.web and self.web.has_clients())):
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
