"""Always-on behaviour: scheduled tasks, reminders and proactive "watch" checks.

- task  : runs the instructions at the scheduled time and messages you the result.
- watch : read-only check. It can look but can't act, and it only messages you when
          something needs your attention.
- heartbeat : a built-in watch job that reviews memory/watchlist.md every
              HEARTBEAT_MINUTES, outside quiet hours.
"""
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config

log = logging.getLogger("scheduler")
TZ = ZoneInfo(config.TIMEZONE)


def in_quiet_hours(now: datetime | None = None) -> bool:
    if not config.QUIET_HOURS:
        return False
    start, end = (int(x) for x in config.QUIET_HOURS.split("-"))
    h = (now or datetime.now(TZ)).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


class Scheduler:
    def __init__(self, brain):
        self.brain = brain
        self.aps = AsyncIOScheduler(timezone=TZ)
        self.jobs: dict[str, dict] = {}
        self.listeners = []          # callbacks run when the task list changes (desktop app)

    def _changed(self) -> None:
        for fn in self.listeners:
            try:
                fn()
            except Exception:
                log.exception("listener failed")

    # ----------------------------------------------------------- persistence --
    def _load(self) -> None:
        if config.SCHEDULES_FILE.exists():
            self.jobs = json.loads(config.SCHEDULES_FILE.read_text())

    def _save(self) -> None:
        config.SCHEDULES_FILE.write_text(json.dumps(self.jobs, indent=2))
        self._changed()

    def _register(self, job: dict) -> None:
        if job.get("cron"):
            trigger = CronTrigger.from_crontab(job["cron"], timezone=TZ)
        else:
            run_at = datetime.fromisoformat(job["run_at"])
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=TZ)
            if run_at < datetime.now(TZ):
                log.info("Skipping past one-time job %s", job["name"])
                return
            trigger = DateTrigger(run_date=run_at)
        self.aps.add_job(self._fire, trigger, args=[job["id"]], id=job["id"],
                         replace_existing=True, misfire_grace_time=3600, coalesce=True)

    # ---------------------------------------------------------------- public --
    def start(self) -> None:
        self._load()
        for job in list(self.jobs.values()):
            try:
                self._register(job)
            except Exception as e:
                log.error("Bad job %s: %s", job.get("name"), e)
        if config.HEARTBEAT_MINUTES > 0:
            self.aps.add_job(self._heartbeat, IntervalTrigger(minutes=config.HEARTBEAT_MINUTES),
                             id="heartbeat", replace_existing=True, coalesce=True)
        self.aps.start()
        log.info("Scheduler started with %d jobs, heartbeat=%s min", len(self.jobs), config.HEARTBEAT_MINUTES)

    def add(self, name: str, instructions: str, cron: str = "", run_at: str = "", mode: str = "task") -> dict:
        if bool(cron) == bool(run_at):
            raise ValueError("Give exactly one of cron (recurring) or run_at (one time).")
        if mode not in ("task", "watch"):
            raise ValueError("mode must be 'task' or 'watch'")
        if cron:
            CronTrigger.from_crontab(cron, timezone=TZ)          # validate
        job = {"id": uuid.uuid4().hex[:6], "name": name, "instructions": instructions,
               "cron": cron, "run_at": run_at, "mode": mode,
               "created": datetime.now(TZ).isoformat(timespec="minutes")}
        self.jobs[job["id"]] = job
        self._register(job)
        self._save()
        return job

    def remove(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        if not job:
            return False
        try:
            self.aps.remove_job(job_id)
        except Exception:
            pass
        self._save()
        return True

    def as_list(self) -> list[dict]:
        out = []
        for j in self.jobs.values():
            aps_job = self.aps.get_job(j["id"]) if self.aps.running else None
            nxt = aps_job.next_run_time.isoformat() if aps_job and aps_job.next_run_time else None
            out.append({**j, "next_run": nxt})
        return sorted(out, key=lambda j: j["next_run"] or "9")

    def describe(self) -> str:
        if not self.jobs:
            return "No scheduled tasks."
        lines = []
        for j in self.jobs.values():
            when = f"cron '{j['cron']}'" if j["cron"] else f"once at {j['run_at']}"
            aps_job = self.aps.get_job(j["id"])
            nxt = aps_job.next_run_time.strftime("%a %b %d %I:%M %p") if aps_job and aps_job.next_run_time else "n/a"
            lines.append(f"[{j['id']}] {j['name']} ({j['mode']}, {when}). Next: {nxt}")
        return "\n".join(lines)

    # ------------------------------------------------------------- execution --
    async def _fire(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        log.info("Running scheduled job %s", job["name"])
        if job["mode"] == "watch" and in_quiet_hours():
            log.info("Quiet hours, skipping watch job %s", job["name"])
        else:
            await self.brain.run_once(job["instructions"], mode=job["mode"], label=job["name"])
        if not job.get("cron"):          # one-time job is done
            self.jobs.pop(job_id, None)
            self._save()

    async def _heartbeat(self) -> None:
        if in_quiet_hours() or self.brain.lock.locked():
            return
        await self._heartbeat_now(silent_if_empty=True)

    async def _heartbeat_now(self, silent_if_empty: bool = False) -> None:
        f = config.MEMORY_DIR / "watchlist.md"
        items = "\n".join(l for l in (f.read_text().splitlines() if f.exists() else [])
                          if l.strip().startswith("-"))
        if not items:
            if not silent_if_empty:
                await self.brain.channel.send_text("Your watchlist is empty. Add lines starting with '-' to memory/watchlist.md, or tell me what to keep an eye on.")
            return
        reported = await self.brain.run_once(
            "Proactive check. Go through each item in this watchlist using read-only actions "
            f"(browser, files, read-only commands). Report only items that need {config.OWNER_NAME}'s attention "
            "right now, and don't repeat anything you already reported earlier today "
            f"(see {config.WORKSPACE / 'heartbeat_seen.md'}; append what you report there).\n\n"
            f"WATCHLIST:\n{items}",
            mode="watch", label="Heads-up")
        if not reported and not silent_if_empty:
            await self.brain.channel.send_text("✅ Checked your watchlist. Nothing needs you right now.")
