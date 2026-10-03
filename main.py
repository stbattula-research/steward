"""Entry point.

    python main.py          # run Steward: desktop app + Telegram (what launchd starts)
    python main.py --cli    # chat with the agent in this Terminal window (good for testing)
    python main.py --url    # print the desktop app's sign-in link
"""
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
from tools import Secrets


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return Secrets.redact(super().format(record))


def setup_logging():
    handler = RotatingFileHandler(config.LOG_FILE, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config.LOG_FILE.chmod(0o600)


class CliChannel:
    async def send_text(self, text: str) -> None:
        print(f"\n🤖 {text}\n")

    async def send_file(self, path: Path, caption: str = "") -> None:
        print(f"\n📎 {caption}: {path}\n")

    async def ask_approval(self, summary: str) -> bool:
        ans = await asyncio.to_thread(input, f"\n🔐 {summary}\nApprove? [y/N] ")
        return ans.strip().lower() in ("y", "yes")


async def run_cli():
    from brain import Brain
    brain = Brain(CliChannel())
    await brain.start()
    print("Agent ready. Type a task, '/new' to reset, 'exit' to quit.")
    try:
        while True:
            text = (await asyncio.to_thread(input, "you › ")).strip()
            if text in ("exit", "quit"):
                break
            if text == "/new":
                await brain.reset(); print("Fresh conversation."); continue
            if text:
                await brain.handle(text)
    finally:
        await brain.close()


async def serve():
    import signal
    import subprocess

    import telegram_bot
    import webserver
    from brain import Brain
    from channels import Router
    from scheduler import Scheduler
    from telegram import BotCommand

    import models
    router = Router()
    brain = Brain(router, models.Registry())
    scheduler = Scheduler(brain)
    brain.scheduler = scheduler
    router.web = webserver.WebUI(router, brain, scheduler)

    tg_app = None
    if telegram_bot.enabled():
        tg_app, router.telegram, commands = telegram_bot.build(router, brain, scheduler)
    else:
        logging.info("Telegram not configured; desktop app only.")

    await brain.start()
    scheduler.start()
    await router.web.start()
    asyncio.ensure_future(brain.warm_up())
    if tg_app:
        try:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(allowed_updates=["message", "callback_query"])
            await tg_app.bot.set_my_commands([BotCommand(c, d) for c, d in commands])
            await router.telegram.send_text(f"🟢 {config.AGENT_NAME} is online on your Mac.")
        except Exception as e:
            # A bad token or no network shouldn't take down the desktop app.
            logging.error("Telegram couldn't start (%s). Check the bot token with Configure Steward.command.", e)
            router.telegram = None
            tg_app = None
    if config.OPEN_APP_ON_START:
        subprocess.Popen(["open", webserver.app_url()])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
    await brain.close()
    await router.web.bridge.close()


if __name__ == "__main__":
    if "--url" in sys.argv:
        import webserver
        print(webserver.app_url())
        sys.exit(0)
    setup_logging()
    import models
    _m = models.Registry().active_model()
    if _m is not None and models.is_local(_m):
        import urllib.request
        try:
            urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        except Exception:
            # Start anyway: the app stays reachable, and you can switch to another model there.
            logging.warning("Ollama isn't reachable at %s. Start the Ollama app, or pick another "
                            "model in Manage models.", config.OLLAMA_URL)
    if "--cli" in sys.argv:
        asyncio.run(run_cli())
    else:
        asyncio.run(serve())
