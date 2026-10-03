"""Telegram front end. Long-polling, so no open ports or public URL on your Mac."""
import asyncio
import logging
import uuid
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

import config
import voice

log = logging.getLogger("telegram")
MAX_LEN = 4000


class TelegramChannel:
    def __init__(self, app: Application):
        self.app = app
        self.chat_id = config.TELEGRAM_OWNER_ID   # private chat id == user id
        self.pending: dict[str, asyncio.Future] = {}

    # Telegram problems (no network, or the owner hasn't tapped Start in the bot yet)
    # must never take the agent down: log them and keep going.
    def _warn(self, e: Exception) -> None:
        hint = " Open your bot in Telegram and tap Start." if "chat not found" in str(e).lower() else ""
        log.warning("Couldn't reach you on Telegram: %s.%s", e, hint)

    async def send_text(self, text: str) -> bool:
        try:
            for i in range(0, len(text), MAX_LEN):
                await self.app.bot.send_message(self.chat_id, text[i:i + MAX_LEN])
            return True
        except Exception as e:
            self._warn(e)
            return False

    async def send_file(self, path: Path, caption: str = "") -> None:
        try:
            with open(path, "rb") as fh:
                if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    await self.app.bot.send_photo(self.chat_id, fh, caption=caption[:1000])
                else:
                    await self.app.bot.send_document(self.chat_id, fh, caption=caption[:1000])
        except Exception as e:
            self._warn(e)

    async def ask_approval(self, summary: str) -> bool:
        key = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"appr:{key}:y"),
            InlineKeyboardButton("❌ Deny", callback_data=f"appr:{key}:n"),
        ]])
        try:
            await self.app.bot.send_message(self.chat_id, f"🔐 {summary[:3500]}", reply_markup=kb)
        except Exception as e:
            # Couldn't ask on the phone: let the desktop app answer, or time out as denied.
            self._warn(e)
            self.pending.pop(key, None)
            await asyncio.sleep(config.APPROVAL_TIMEOUT_SEC)
            return False
        try:
            return await asyncio.wait_for(fut, timeout=config.APPROVAL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self.send_text("⏱ No answer, so I treated that as denied.")
            return False
        finally:
            self.pending.pop(key, None)


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_OWNER_ID)


def build(router, brain, scheduler):
    """Create the Telegram bot. Returns (app, channel, commands). main.py starts it."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    channel = TelegramChannel(app)
    owner = filters.User(user_id=config.TELEGRAM_OWNER_ID)

    async def _keep_typing():
        while True:
            await app.bot.send_chat_action(channel.chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        text = msg.text or msg.caption or ""
        # Save any photo/document into the inbox and tell the agent where it is.
        attachment = None
        if msg.photo:
            attachment = await msg.photo[-1].get_file()
            dest = config.INBOX / f"photo-{msg.message_id}.jpg"
        elif msg.document:
            attachment = await msg.document.get_file()
            dest = config.INBOX / (msg.document.file_name or f"file-{msg.message_id}")
        elif msg.voice or msg.audio:
            # Voice note: transcribe on the Mac, then treat it as a typed message.
            f = await (msg.voice or msg.audio).get_file()
            dest = config.INBOX / f"voice-{msg.message_id}.ogg"
            await f.download_to_drive(dest)
            try:
                text = await voice.transcribe(dest)
            except Exception as e:
                await msg.reply_text(f"Couldn't transcribe that: {e}")
                return
            await msg.reply_text(f"🎙 Heard: {text}")
        if attachment:
            await attachment.download_to_drive(dest)
            text = f"{text}\n\n[Attached file saved at: {dest}]".strip()
        if not text:
            return
        if brain.busy:
            await msg.reply_text("Still on the previous task, I'll pick this up next. Send /stop to cancel it.")
        await router.mirror_user(text, "telegram")
        typing = asyncio.create_task(_keep_typing())
        try:
            await brain.handle(text, origin="telegram")
        finally:
            typing.cancel()

    async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if q.from_user.id != config.TELEGRAM_OWNER_ID:
            await q.answer("Not authorised.")
            return
        _, key, ans = q.data.split(":")
        fut = channel.pending.get(key)
        if fut and not fut.done():
            fut.set_result(ans == "y")
            await q.edit_message_text(q.message.text + ("\n\n✅ Approved" if ans == "y" else "\n\n❌ Denied"))
        await q.answer()

    COMMANDS = [
        ("stop", "Cancel the current task"),
        ("new", "Start a fresh conversation"),
        ("screen", "Screenshot of the Mac right now"),
        ("status", "Busy or idle?"),
        ("tasks", "List scheduled tasks"),
        ("cancel", "Cancel a scheduled task: /cancel <id>"),
        ("watch", "Run the watchlist check now"),
        ("memory", "Show what I know about you"),
    ]

    async def cmd_start(update: Update, ctx):
        await update.message.reply_text(
            f"Hi {config.OWNER_NAME}, I'm {config.AGENT_NAME}, running on your Mac. Text or voice-note me a task.\n\n"
            + "\n".join(f"/{c}  {d}" for c, d in COMMANDS))

    async def cmd_tasks(update: Update, ctx):
        await update.message.reply_text(scheduler.describe())

    async def cmd_cancel(update: Update, ctx):
        if not ctx.args:
            await update.message.reply_text("Usage: /cancel <id>  (see /tasks)")
            return
        ok = scheduler.remove(ctx.args[0])
        await update.message.reply_text("Cancelled." if ok else "No task with that id.")

    async def cmd_watch(update: Update, ctx):
        await update.message.reply_text("👀 Checking your watchlist…")
        await scheduler._heartbeat_now()

    async def cmd_memory(update: Update, ctx):
        parts = []
        for name in ("about_me.md", "learned.md", "watchlist.md"):
            f = config.MEMORY_DIR / name
            if f.exists() and f.read_text().strip():
                parts.append(f"— {name} —\n{f.read_text().strip()}")
        await channel.send_text("\n\n".join(parts) or "Nothing saved yet.")
        await channel.send_text(f"Edit these files on your Mac in {config.MEMORY_DIR}, or just tell me what to change.")

    async def cmd_stop(update: Update, ctx):
        for fut in list(channel.pending.values()):
            if not fut.done():
                fut.set_result(False)
        await brain.stop_current()
        await update.message.reply_text("🛑 Stopped.")

    async def cmd_new(update: Update, ctx):
        await brain.stop_current()
        async with brain.lock:
            await brain.reset()
        await update.message.reply_text("🧹 Fresh conversation. My memory files are kept.")

    async def cmd_screen(update: Update, ctx):
        path = config.OUTBOX / "quick-screen.png"
        proc = await asyncio.create_subprocess_exec("screencapture", "-x", str(path))
        await proc.wait()
        await channel.send_file(path, "Your screen right now")

    async def cmd_status(update: Update, ctx):
        await update.message.reply_text("Working on a task." if brain.busy else "Idle and ready.")

    app.add_handler(CommandHandler("start", cmd_start, filters=owner))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=owner))
    app.add_handler(CommandHandler("new", cmd_new, filters=owner))
    app.add_handler(CommandHandler("screen", cmd_screen, filters=owner))
    app.add_handler(CommandHandler("status", cmd_status, filters=owner))
    app.add_handler(CommandHandler("tasks", cmd_tasks, filters=owner))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=owner))
    app.add_handler(CommandHandler("watch", cmd_watch, filters=owner))
    app.add_handler(CommandHandler("memory", cmd_memory, filters=owner))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^appr:"))
    app.add_handler(MessageHandler(
        owner & ~filters.COMMAND & (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO), on_message))

    return app, channel, COMMANDS
