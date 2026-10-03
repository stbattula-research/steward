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
import re
from pathlib import Path

log = logging.getLogger("router")


class Router:
    def __init__(self):
        self.web = None        # webserver.WebUI
        self.telegram = None   # telegram_bot.TelegramChannel
        self.connectors = None # connectors.Connectors (WhatsApp, iMessage, Discord, Slack)
        self.origin = "web"    # web | telegram | background

    def begin(self, origin: str) -> None:
        self.origin = origin

    def _phone_mode(self) -> str:
        return self.web.prefs.get("phone_mode", "auto") if self.web else "always"

    def _chat_origins(self) -> set:
        return {"telegram", "phone", *((self.connectors.items.keys()) if self.connectors else ())}

    def _to_connectors(self) -> list:
        """Chat apps that should get this: the one you wrote from, plus every app with Alerts on
        when this is a heads-up or scheduled task (following the Phone alerts setting)."""
        if not self.connectors:
            return []
        out = []
        for c in self.connectors.running():
            if self.origin == c.kind:
                out.append(c)
            elif c.cfg.get("alerts", True) and self.origin not in self._chat_origins() and self._away():
                out.append(c)
        return out

    def _away(self) -> bool:
        """Should this also reach the owner's phone?"""
        if self.origin in self._chat_origins():
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
        return (self._push_ready() and self.origin not in self._chat_origins() - {"phone"} and self._away()
                and not self.web.phone_visible())

    def _to_telegram(self) -> bool:
        if not self.telegram:
            return False
        if self.origin == "telegram":
            return True
        # The phone app replaces Telegram for alerts once it has notifications on.
        return self.origin not in self._chat_origins() and not self._push_ready() and self._away()

    def _notify_phone(self, payload: dict, urgency: str = "normal") -> None:
        asyncio.ensure_future(self.web.mobile.push(payload, urgency))

    @staticmethod
    def _snippet(text: str, n: int = 180) -> str:
        text = " ".join(str(text).replace("**", "").split())
        return text if len(text) <= n else text[: n - 1] + "…"

    @staticmethod
    def _split_related(text: str) -> tuple[str, list[str]]:
        """Pull the 'Related: a? | b? | c?' line (suggested follow-ups) off the end of an answer."""
        m = re.search(r"\n?[ \t>*_`]*Related:[ \t]*(.+?)[`*_ \t]*\s*$", text, re.I)
        if not m or "|" not in m.group(1):
            return text, []
        items = [q.strip(" *_`-") for q in m.group(1).split("|")]
        return text[:m.start()].rstrip(), [q for q in items if 3 < len(q) < 200][:3]

    # ------------------------------------------------------- Channel protocol --
    async def send_text(self, text: str) -> None:
        text, related = self._split_related(text)
        if not text:
            if related and self.web:
                await self.web.emit({"type": "related", "items": related})
            return
        if self.web:
            ev = {"type": "message", "role": "assistant", "text": text}
            if related:
                ev["related"] = related
            await self.web.emit(ev)
        if self._to_push():
            from config import AGENT_NAME
            self._notify_phone({"title": AGENT_NAME, "body": self._snippet(text), "tag": "reply"})
        if self._to_telegram():
            await self.telegram.send_text(text)
        for c in self._to_connectors():
            await c.send_text(text)

    async def send_file(self, path: Path, caption: str = "") -> None:
        if self.web:
            await self.web.emit_file(path, caption)
        if self._to_push():
            from config import AGENT_NAME
            self._notify_phone({"title": AGENT_NAME, "body": self._snippet(caption or f"Sent you {path.name}"),
                                "tag": "file"})
        if self._to_telegram():
            await self.telegram.send_file(path, caption)
        for c in self._to_connectors():
            await c.send_file(path, caption)

    async def ask_approval(self, summary: str) -> bool:
        asks = []
        phone = (self._push_ready() and self.origin not in self._chat_origins() - {"phone"}
                 and (self._away() or not self.web.has_clients()))
        if self.web and (self.web.clients or self.origin in ("web", "phone") or phone):
            asks.append(self.web.ask_approval(summary, push=phone and not self.web.phone_visible()))
        if self.telegram and self.origin not in self._chat_origins() - {"telegram", "phone"} and (
                self.origin == "telegram" or self._to_telegram()
                              or not (self.web and (self.web.clients or phone))):
            asks.append(self.telegram.ask_approval(summary))
        for c in self._to_connectors():
            asks.append(c.ask_approval(summary))
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

    async def on_sources(self, items: list[dict]) -> None:
        if self.web:
            await self.web.emit({"type": "sources", "items": items})
        lines = "Sources:\n" + "\n".join(f"[{s['n']}] {s['url']}" for s in items[:10])
        if self._to_telegram() and items:
            await self.telegram.send_text(lines)
        for c in self._to_connectors():
            if items:
                await c.send_text(lines)

    async def council_event(self, ev: dict, persist: bool = True) -> None:
        if not self.web:
            return
        if persist:
            await self.web.emit(ev)
        else:
            await self.web._broadcast(ev)

    async def set_status(self, busy: bool, label: str = "") -> None:
        if self.web:
            await self.web.set_status(busy, label)

    async def mirror_user(self, text: str, source: str) -> None:
        if self.web:
            await self.web.emit({"type": "message", "role": "user", "text": text, "source": source})
