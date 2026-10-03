"""Custom tools the agent gets on top of Claude's built-ins (Bash, files, web)."""
import asyncio
import base64
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from claude_agent_sdk import create_sdk_mcp_server, tool

import config
import vault


class Channel(Protocol):
    """How the agent talks to you (Telegram or local terminal)."""
    async def send_text(self, text: str) -> None: ...
    async def send_file(self, path: Path, caption: str = "") -> None: ...
    async def ask_approval(self, summary: str) -> bool: ...


# ---------------------------------------------------------------- redaction ---
class Secrets:
    """Every password fetched this run is remembered here so it can be scrubbed
    from anything sent to you or written to logs."""
    _values: set[str] = set()

    @classmethod
    def add(cls, value: str) -> None:
        if value and len(value) >= 4:
            cls._values.add(value)

    @classmethod
    def redact(cls, text: str) -> str:
        for v in cls._values:
            text = text.replace(v, "••••••")
        return text


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def build_server(channel: Channel):
    @tool("list_saved_logins",
          "List the website logins saved in the macOS Keychain (names, usernames, URLs; no passwords).",
          {})
    async def list_saved_logins(args):
        logins = vault.list_logins()
        if not logins:
            return _ok("No saved logins yet. Ask the owner to run: python vault.py add <name> --user <username> --url <url>")
        lines = [f"- {n}: user={e['username']} url={e.get('url', '')} {e.get('notes', '')}" for n, e in logins.items()]
        return _ok("\n".join(lines))

    @tool("get_password",
          "Fetch the username and password for a saved login from the macOS Keychain. "
          "Use the value only to type into the matching site's login form. Never repeat it in messages.",
          {"name": str})
    async def get_password(args):
        try:
            user, pw = vault.get_password(args["name"])
        except KeyError as e:
            return _ok(str(e))
        Secrets.add(pw)
        return _ok(f"username: {user}\npassword: {pw}")

    @tool("take_screenshot",
          "Capture the Mac's screen so you can see what's on it. Optionally send it to the owner.",
          {"send_to_owner": bool})
    async def take_screenshot(args):
        path = config.OUTBOX / f"screen-{datetime.now():%Y%m%d-%H%M%S}.png"
        proc = await asyncio.create_subprocess_exec("screencapture", "-x", str(path))
        await proc.wait()
        if not path.exists():
            return _ok("Screenshot failed. Grant Screen Recording permission to Terminal/Python in System Settings.")
        # Downscale so it doesn't eat the context window.
        subprocess.run(["sips", "-Z", "1440", str(path)], capture_output=True)
        if args.get("send_to_owner"):
            await channel.send_file(path, "Screenshot")
        data = base64.b64encode(path.read_bytes()).decode()
        return {"content": [
            {"type": "text", "text": f"Saved to {path}"},
            {"type": "image", "data": data, "mimeType": "image/png"},
        ]}

    @tool("send_file_to_me", "Send a file from this Mac to the owner's phone.",
          {"path": str, "caption": str})
    async def send_file_to_me(args):
        p = Path(args["path"]).expanduser()
        if not p.is_file():
            return _ok(f"Not a file: {p}")
        if any(s in str(p) for s in ("/.ssh/", "Keychains", "/.steward/", "/.env")):
            return _ok("That file is protected and can't be sent.")
        await channel.send_file(p, args.get("caption", ""))
        return _ok(f"Sent {p.name}.")

    @tool("notify_me", "Send the owner a short progress update while you keep working.", {"message": str})
    async def notify_me(args):
        await channel.send_text(Secrets.redact(args["message"]))
        return _ok("Sent.")

    @tool("request_approval",
          "Ask the owner to approve an irreversible or sensitive action BEFORE doing it: paying, buying, "
          "booking, sending a message/email/post, submitting a form, deleting data, changing account "
          "settings. Returns APPROVED or DENIED.",
          {"action": str, "details": str})
    async def request_approval(args):
        approved = await channel.ask_approval(f"{args['action']}\n\n{Secrets.redact(args['details'])}")
        return _ok("APPROVED" if approved else "DENIED. Do not perform this action; ask the owner what to do instead.")

    @tool("remember",
          "Save a durable fact about the owner or how they like a task done, so you do it the same way next time.",
          {"fact": str})
    async def remember(args):
        f = config.MEMORY_DIR / "learned.md"
        with f.open("a") as fh:
            fh.write(f"- {time.strftime('%Y-%m-%d')}: {Secrets.redact(args['fact'])}\n")
        return _ok("Remembered.")

    return create_sdk_mcp_server(
        name="me",
        tools=[list_saved_logins, get_password, take_screenshot, send_file_to_me,
               notify_me, request_approval, remember],
    )
