"""Local models with Ollama: status, one-click install, downloads with progress, removal.
Everything here is what the "Local models" tab in the app drives."""
import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess

import aiohttp

import config

log = logging.getLogger("local")

# Models with tool calling that suit a Mac, from Ollama's library page for Gemma 4.
RECOMMENDED = [
    {"name": "gemma4:e4b", "title": "Gemma 4 · small", "size": "about 7–10 GB", "min_ram": 8,
     "about": "Fastest. Good for quick questions and simple tasks."},
    {"name": "gemma4:12b", "title": "Gemma 4 · medium", "size": "about 8 GB", "min_ram": 16,
     "about": "The best balance on most Macs. Can see screenshots."},
    {"name": "gemma4:26b", "title": "Gemma 4 · large", "size": "about 16–19 GB", "min_ram": 32,
     "about": "Smartest, for Macs with plenty of memory."},
]


def ram_gb() -> int:
    try:
        if platform.system() == "Darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip()) // (1024 ** 3)
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) // (1024 ** 2)
    except Exception:
        return 16


def best_fit(ram: int) -> str:
    fit = [m["name"] for m in RECOMMENDED if m["min_ram"] <= ram]
    return fit[-1] if fit else RECOMMENDED[0]["name"]


def ollama_env() -> dict:
    """Ollama settings for speed (same as the installer): fits the model on the GPU and halves
    the memory each conversation needs."""
    return {"OLLAMA_CONTEXT_LENGTH": str(config.OLLAMA_CONTEXT), "OLLAMA_KEEP_ALIVE": "30m",
            "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NUM_PARALLEL": "1"}


def ollama_bin() -> str | None:
    for p in (shutil.which("ollama"), "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
              "/Applications/Ollama.app/Contents/Resources/ollama"):
        if p and os.path.exists(p):
            return p
    return None


def brew_bin() -> str | None:
    for p in (shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if p and os.path.exists(p):
            return p
    return None


class LocalModels:
    def __init__(self, notify):
        """notify(event: dict) sends progress to the app."""
        self.notify = notify
        self.pulls: dict[str, asyncio.Task] = {}
        self.installing = False

    def _session(self, total=10):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total))

    async def status(self) -> dict:
        ram = ram_gb()
        st = {"installed": bool(ollama_bin()) or os.path.isdir("/Applications/Ollama.app"),
              "running": False, "version": "", "models": [], "ram_gb": ram, "best": best_fit(ram),
              "recommended": RECOMMENDED, "can_install": bool(brew_bin()), "installing": self.installing,
              "pulling": list(self.pulls)}
        try:
            async with self._session(3) as s:
                st["version"] = (await (await s.get(f"{config.OLLAMA_URL}/api/version")).json()).get("version", "")
                st["running"] = True
                tags = await (await s.get(f"{config.OLLAMA_URL}/api/tags")).json()
                st["models"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)}
                                for m in tags.get("models", [])]
        except Exception:
            pass
        return st

    async def _run(self, *cmd) -> int:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if text:
                await self.notify({"type": "local_log", "text": text[:300]})
        return await proc.wait()

    async def install(self) -> None:
        """Install Ollama with Homebrew and start it in the background."""
        brew = brew_bin()
        if not brew:
            await self.notify({"type": "toast", "error": True,
                               "text": "Homebrew isn't installed. Run Start Steward.command first."})
            return
        self.installing = True
        await self.notify({"type": "local_status", **(await self.status())})
        try:
            for k, v in ollama_env().items():
                subprocess.run(["launchctl", "setenv", k, v], check=False)
            if not ollama_bin():
                if await self._run(brew, "install", "ollama") != 0:
                    raise RuntimeError("Homebrew couldn't install Ollama (details above).")
            await self.start()
            await self.notify({"type": "toast", "text": "Ollama is installed and running."})
        except Exception as e:
            await self.notify({"type": "toast", "error": True, "text": str(e)})
        finally:
            self.installing = False
            await self.notify({"type": "local_status", **(await self.status())})

    async def start(self) -> None:
        if os.path.isdir("/Applications/Ollama.app") and not shutil.which("ollama"):
            subprocess.run(["open", "-a", "Ollama"], check=False)
        elif brew_bin():
            await self._run(brew_bin(), "services", "start", "ollama")
        for _ in range(30):
            if (await self.status())["running"]:
                break
            await asyncio.sleep(1)
        await self.notify({"type": "local_status", **(await self.status())})

    def pull(self, name: str, on_done) -> None:
        if name in self.pulls:
            return
        self.pulls[name] = asyncio.ensure_future(self._pull(name, on_done))

    async def _pull(self, name: str, on_done) -> None:
        last_pct = -1
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600)) as s:
                async with s.post(f"{config.OLLAMA_URL}/api/pull", json={"model": name, "stream": True}) as r:
                    if r.status >= 400:
                        raise RuntimeError((await r.text())[:200])
                    async for raw in r.content:
                        if not raw.strip():
                            continue
                        ev = json.loads(raw)
                        if ev.get("error"):
                            raise RuntimeError(ev["error"])
                        total, done = ev.get("total") or 0, ev.get("completed") or 0
                        pct = int(done * 100 / total) if total else None
                        if pct is None or pct != last_pct:
                            last_pct = pct if pct is not None else last_pct
                            await self.notify({"type": "local_progress", "name": name, "status": ev.get("status", ""),
                                               "pct": pct, "done_gb": round(done / 1e9, 2), "total_gb": round(total / 1e9, 2)})
            await self.notify({"type": "local_progress", "name": name, "status": "success", "pct": 100})
            await on_done(name)
        except asyncio.CancelledError:
            await self.notify({"type": "local_progress", "name": name, "status": "cancelled", "pct": None})
        except Exception as e:
            await self.notify({"type": "local_progress", "name": name, "status": "error", "pct": None,
                               "error": f"Download failed: {str(e)[:200]}"})
        finally:
            self.pulls.pop(name, None)
            await self.notify({"type": "local_status", **(await self.status())})

    def cancel(self, name: str) -> None:
        task = self.pulls.get(name)
        if task:
            task.cancel()

    async def delete(self, name: str) -> None:
        async with self._session(30) as s:
            r = await s.delete(f"{config.OLLAMA_URL}/api/delete", json={"model": name})
            if r.status >= 400:
                raise RuntimeError((await r.text())[:200])
        await self.notify({"type": "local_status", **(await self.status())})
