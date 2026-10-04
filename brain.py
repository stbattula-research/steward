"""The agent's brain: a long-lived chat session plus one-off runs for scheduled
tasks and proactive checks, all behind the same guardrails."""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, TextBlock, ToolUseBlock, create_sdk_mcp_server, tool,
)

import bridge
import config
import council as council_mod
import websearch
import guardrails
import models
from tools import Channel, Secrets, build_server

log = logging.getLogger("brain")

# Only the built-in tools the agent actually uses. Everything else the CLI ships with
# (sub-agents, plan mode, task lists, notebooks...) is left out so the prompt stays small;
# that matters a lot for a local model.
BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]   # web: our web_search / fetch_page

# Browser actions that are rarely needed; hiding them shrinks every request.
# Same action repeated this many times in one task = the agent is stuck; make it change course.
MAX_REPEATS = 3

CONTEXT_FULL = ("exceed_context_size", "exceeds the available context", "prompt is too long",
                "context length", "maximum context")


def _context_env(window: int) -> dict:
    """Tell the engine the model's real memory size, so it summarises older steps
    before the conversation overflows (it assumes a much bigger window for unknown models)."""
    if window <= 0:
        return {}
    return {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(window),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(int(window * 0.75))}


HIDDEN_BROWSER = ["browser_console_messages", "browser_network_request", "browser_network_requests",
                  "browser_drag", "browser_drop", "browser_emulate_media", "browser_evaluate",
                  "browser_run_code_unsafe", "browser_run_code", "browser_resize", "browser_install",
                  "browser_pdf_save", "browser_find", "browser_hover"]


def _system_prompt() -> str:
    base = (config.ROOT / "prompts" / "system.md").read_text()
    parts = [base.format(workspace=config.WORKSPACE, memory=config.MEMORY_DIR,
                         owner=config.OWNER_NAME, agent=config.AGENT_NAME,
                         today=datetime.now().strftime("%A, %B %d, %Y, %I:%M %p"),
                         timezone=config.TIMEZONE)]
    for name in ("about_me.md", "learned.md"):
        f = config.MEMORY_DIR / name
        if f.exists() and f.read_text().strip():
            parts.append(f"\n## {name}\n{f.read_text()}")
    playbooks = sorted((config.MEMORY_DIR / "playbooks").glob("*.md"))
    if playbooks:
        parts.append("\n## Playbooks available (Read the matching one before starting that kind of task)\n"
                     + "\n".join(f"- {p}" for p in playbooks))
    return "\n".join(parts)


def _integrations() -> dict:
    """Extra MCP servers from integrations.json (keys starting with '_' are ignored)."""
    if not config.INTEGRATIONS_FILE.exists():
        return {}
    try:
        data = json.loads(config.INTEGRATIONS_FILE.read_text())
        return {k: v for k, v in data.get("mcpServers", {}).items() if not k.startswith("_")}
    except Exception as e:
        log.error("integrations.json is invalid: %s", e)
        return {}


class NoModel(Exception):
    """No AI model has been set up yet."""


NO_MODEL_MSG = ("No AI model is set up yet. Open **Models** in the sidebar to download a free model "
                "that runs on your Mac, or connect a provider like Claude, OpenAI or Gemini.")


class BufferChannel:
    """Collects a background run's messages so it only pings you if there's news."""
    def __init__(self, real: Channel):
        self.real, self.texts, self.files = real, [], []

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def send_file(self, path: Path, caption: str = "") -> None:
        self.files.append((path, caption))

    async def ask_approval(self, summary: str) -> bool:
        return False


class Brain:
    def __init__(self, channel: Channel, registry: "models.Registry | None" = None):
        self.channel = channel
        self.registry = registry or models.Registry()
        self.client: ClaudeSDKClient | None = None
        self.lock = asyncio.Lock()            # one model run at a time (one local model in RAM)
        self.busy = False
        self.interrupted = False
        self.repeats: dict[str, int] = {}     # per-task count of identical actions (loop guard)
        self.context_full = False
        self.active: ClaudeSDKClient | None = None   # whichever session is running now
        self.scheduler = None                 # set by telegram_bot after construction
        self.recent_reports: list[str] = []   # automated results to show the chat session
        self.sources = websearch.Sources()    # numbered web sources for the current answer
        self.council = council_mod.Council(
            self, emit=lambda ev: self._hook("council_event", ev, True),
            broadcast=lambda ev: self._hook("council_event", ev, False))

    # --------------------------------------------------------------- config --
    def _brain_env(self, m: dict) -> tuple[str | None, dict, float | None]:
        """(model, env, budget) for one model from the registry."""
        env = {   # no telemetry / helper traffic; helper calls use the same model
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "DISABLE_TELEMETRY": "1",
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",     # never use (or forward) a Claude Code login from this Mac
            **_context_env(models.context_window(m)),
        }
        p = models.provider_of(m)
        if not p.get("key"):
            # Local Ollama: no key, talk to it directly.
            env.update({"ANTHROPIC_BASE_URL": models.base_url(m), "ANTHROPIC_AUTH_TOKEN": "ollama"})
        else:
            # Everything with a key goes through the local bridge, which adds the real key
            # (and translates OpenAI-format providers). Keys never enter this environment.
            env.update({"ANTHROPIC_BASE_URL": bridge.url_for(m["id"]), "ANTHROPIC_AUTH_TOKEN": "steward-bridge"})
        model = m.get("model") or None
        if model:
            env.update({"ANTHROPIC_SMALL_FAST_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model})
        budget = config.MAX_BUDGET_USD if m["provider"] == "anthropic" else None
        return model, env, budget

    def _sched_server(self):
        brain = self

        @tool("schedule_task",
              "Schedule work to run later on its own. mode='task' does the instructions and reports back "
              "(e.g. reminders, daily summaries, recurring chores). mode='watch' is a read-only check that "
              "only messages the owner if something needs attention (e.g. 'tell me when the price drops'). "
              "Use cron for recurring (5 fields, the owner's local time, e.g. '0 8 * * 1-5') or run_at for one "
              "time (ISO, e.g. '2026-10-03T17:30').",
              {"type": "object", "properties": {
                  "name": {"type": "string"}, "instructions": {"type": "string"},
                  "cron": {"type": "string"}, "run_at": {"type": "string"},
                  "mode": {"type": "string", "enum": ["task", "watch"]}},
               "required": ["name", "instructions"]})
        async def schedule_task(args):
            try:
                job = brain.scheduler.add(args["name"], args["instructions"], args.get("cron", ""),
                                          args.get("run_at", ""), args.get("mode", "task"))
            except Exception as e:
                return {"content": [{"type": "text", "text": f"Couldn't schedule: {e}"}]}
            return {"content": [{"type": "text", "text": f"Scheduled [{job['id']}].\n{brain.scheduler.describe()}"}]}

        @tool("list_scheduled_tasks", "List the owner's scheduled tasks and when they run next.", {})
        async def list_scheduled_tasks(args):
            return {"content": [{"type": "text", "text": brain.scheduler.describe()}]}

        @tool("cancel_scheduled_task", "Cancel a scheduled task by its id.", {"id": str})
        async def cancel_scheduled_task(args):
            ok = brain.scheduler.remove(args["id"])
            return {"content": [{"type": "text", "text": "Cancelled." if ok else "No task with that id."}]}

        return create_sdk_mcp_server("sched", tools=[schedule_task, list_scheduled_tasks, cancel_scheduled_task])

    def _model_for(self, mode: str) -> dict:
        return self.registry.background_model() if mode in ("task", "watch") else self.registry.active_model()

    def _options(self, resume: str | None, channel: Channel, mode: str = "chat",
                 m: dict | None = None) -> ClaudeAgentOptions:
        m = m or self._model_for(mode)
        if m is None:
            raise NoModel()
        model, env, budget = self._brain_env(m)
        local = models.is_local(m)
        decide = self._decider(channel, mode)
        browser_args = ["-y", "@playwright/mcp@latest", "--browser", config.BROWSER_CHANNEL,
                        "--user-data-dir", str(config.BROWSER_PROFILE),
                        "--output-dir", str(config.DOWNLOADS)]
        if local:
            # Don't attach a full page dump to every click; the agent asks for one
            # (browser_snapshot) when it needs to look. Keeps local models within memory.
            browser_args += ["--snapshot-mode", "none"]
        servers = {
            "me": build_server(channel),
            "web": websearch.build_web_server(lambda: self.sources),
            "browser": {"type": "stdio", "command": "npx", "args": browser_args},
            **_integrations(),
        }
        if self.scheduler and mode == "chat":
            servers["sched"] = self._sched_server()
        return ClaudeAgentOptions(
            system_prompt=_system_prompt(),
            model=model,
            tools=BUILTIN_TOOLS,
            strict_mcp_config=True,        # only our MCP servers, nothing else installed on the Mac
            thinking={"type": "disabled"} if local else None,
            cwd=str(config.WORKSPACE),
            add_dirs=[str(config.HOME)],
            permission_mode="default",
            can_use_tool=self._permission_handler(decide),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._guard_hook(decide)],
                                              timeout=config.APPROVAL_TIMEOUT_SEC + 60)]},
            setting_sources=None,          # ignore any other Claude settings on this Mac
            disallowed_tools=["AskUserQuestion", "mcp__claude-in-chrome", "mcp__computer-use"]
                             + [f"mcp__browser__{t}" for t in HIDDEN_BROWSER],
            max_turns=config.MAX_TURNS,
            max_budget_usd=budget,
            resume=resume,
            env=env,
            mcp_servers=servers,
        )

    # ------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        if self.registry.active_model() is None:
            self.client = None          # nothing to talk to yet; the app shows setup
            return
        resume = None
        if config.SESSION_FILE.exists():
            resume = json.loads(config.SESSION_FILE.read_text()).get("session_id")
        try:
            self.client = ClaudeSDKClient(self._options(resume, self.channel))
            await self.client.connect()
        except Exception as e:  # stale session id etc.
            log.warning("Resume failed (%s); starting fresh", e)
            self.client = ClaudeSDKClient(self._options(None, self.channel))
            await self.client.connect()

    async def reset(self) -> None:
        """Forget the current conversation (memory files are kept)."""
        if self.client:
            await self.client.disconnect()
        config.SESSION_FILE.unlink(missing_ok=True)
        if self.registry.active_model() is None:
            self.client = None
            return
        self.client = ClaudeSDKClient(self._options(None, self.channel))
        await self.client.connect()

    async def switch_model(self, model_id: str) -> dict:
        """Chat with a different model from now on (starts a fresh conversation)."""
        await self.stop_current()
        async with self.lock:
            m = self.registry.set_active(model_id)
            await self.reset()
        asyncio.ensure_future(self.warm_up())
        return m

    async def stop_current(self) -> None:
        if self.busy:
            await self.council.stop()
        if self.active and self.busy:
            self.interrupted = True
            await self.active.interrupt()

    async def warm_up(self) -> None:
        """Local models: process the (large, fixed) instructions + tool list once at startup,
        so Ollama has them cached and your first real message starts fast."""
        m = self.registry.active_model()
        if m is None or not models.is_local(m):
            return
        async with self.lock:
            self.busy = True
            await self._hook("set_status", True, "Warming up")
            client = ClaudeSDKClient(self._options(None, BufferChannel(self.channel), "chat"))
            try:
                await client.connect()
                await client.query("Warm-up check. Reply with just: ok")
                async for _ in client.receive_response():
                    pass
                log.info("warm-up done")
            except Exception:
                log.exception("warm-up failed")
            finally:
                await client.disconnect()
                self.busy = False
                await self._hook("set_status", False, "")

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()

    # ----------------------------------------------------------- permissions --
    def _decider(self, channel: Channel, mode: str):
        """One place that decides every action: loop guard, then guardrails (allow / ask / block).
        Returns async (tool_name, tool_input) -> (allowed, message)."""
        evaluate = guardrails.evaluate_watch if mode == "watch" else guardrails.evaluate

        async def decide(tool_name: str, tool_input: dict) -> tuple[bool, str]:
            # Loop guard: small models sometimes retry the same failing action forever.
            target = (tool_input.get("element") or tool_input.get("url") or tool_input.get("command")
                      or json.dumps(tool_input, sort_keys=True))
            key = f"{tool_name}:{str(target)[:200]}"
            self.repeats[key] = self.repeats.get(key, 0) + 1
            if self.repeats[key] > MAX_REPEATS and tool_name != "mcp__browser__browser_snapshot":
                log.info("loop guard stopped %s", key)
                return False, (f"You've already tried this {MAX_REPEATS} times and it isn't working. Don't repeat it. "
                               f"Check whether it already succeeded (for downloads, look in {config.DOWNLOADS} "
                               "and ~/Downloads), try a different approach, or stop and tell the owner what's blocking you.")
            v = evaluate(tool_name, tool_input)
            log.info("[%s] tool %s -> %s %s", mode, tool_name, v.decision, v.reason)
            if v.decision == guardrails.ALLOW:
                if config.VERBOSE_STEPS and mode != "watch":
                    await channel.send_text("🔧 " + Secrets.redact(guardrails.describe(tool_name, tool_input))[:300])
                return True, ""
            if v.decision == guardrails.BLOCK:
                return False, f"Blocked by guardrails: {v.reason}"
            ok = await channel.ask_approval(
                f"Needs approval: {v.reason}\n\n{Secrets.redact(guardrails.describe(tool_name, tool_input))}")
            return (True, "") if ok else (False, "The owner denied this action. Do not retry it; explain or ask what to do instead.")
        return decide

    def _guard_hook(self, decide):
        """PreToolUse hook: runs for EVERY action, including the 'read-only' ones the engine
        would otherwise approve on its own (e.g. reading files, `ls`, `cat`)."""
        async def hook(input_data, tool_use_id, context):
            allowed, message = await decide(input_data.get("tool_name", ""), input_data.get("tool_input") or {})
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": message or "Allowed by Steward guardrails",
            }}
        return hook

    def _permission_handler(self, decide):
        """Fallback for anything that still asks for permission after the hook."""
        async def can_use_tool(tool_name, tool_input, context):
            allowed, message = await decide(tool_name, tool_input)
            return PermissionResultAllow() if allowed else PermissionResultDeny(message=message)
        return can_use_tool

    # --------------------------------------------------------------- running --
    async def _stream(self, client: ClaudeSDKClient, channel: Channel, save_session: bool) -> None:
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        if any(k in block.text.lower() for k in CONTEXT_FULL):
                            self.context_full = True      # handled by the caller (fresh start + retry)
                            log.warning("context full: %s", block.text[:200])
                            continue
                        text = block.text
                        if text.startswith("API Error: 400 ") and not text[15:].lstrip().startswith("{"):
                            text = "⚠️ " + text[15:]          # our own readable message from the bridge
                        await channel.send_text(Secrets.redact(text))
                    elif isinstance(block, ToolUseBlock):
                        log.info("call %s %s", block.name, Secrets.redact(json.dumps(block.input)[:500]))
                        if hasattr(channel, "on_tool"):
                            await channel.on_tool(block.name, Secrets.redact(
                                guardrails.describe(block.name, block.input))[:300])
            elif isinstance(msg, ResultMessage):
                if save_session:
                    config.SESSION_FILE.write_text(json.dumps({"session_id": msg.session_id}))
                log.info("done turns=%s cost=$%s", msg.num_turns, msg.total_cost_usd)
                if msg.is_error and self.interrupted:
                    self.interrupted = False          # you pressed Stop; "Stopped." was already shown
                elif msg.is_error and (self.context_full or msg.subtype == "success"):
                    pass                              # already reported (or retried) elsewhere
                elif msg.is_error and msg.subtype == "error_max_turns":
                    await channel.send_text(f"⚠️ I hit my limit of {config.MAX_TURNS} steps on this task. "
                                            "Tell me to continue, or give me a narrower task.")
                elif msg.is_error:
                    await channel.send_text(f"⚠️ Stopped: {msg.subtype}. {'; '.join(msg.errors or [])}".strip())

    async def _hook(self, name: str, *args) -> None:
        fn = getattr(self.channel, name, None)
        if fn:
            res = fn(*args)
            if asyncio.iscoroutine(res):
                await res

    async def _emit_sources(self) -> None:
        if self.sources.items:
            await self._hook("on_sources", self.sources.public())

    async def handle(self, text: str, origin: str = "telegram", mode: str = "auto",
                     team: dict | None = None) -> None:
        """A message from the owner, in the ongoing conversation.
        mode: auto | web | academic | research. team: {"members": [model ids], "rounds": 1-2}."""
        async with self.lock:
            self.busy = True
            self.interrupted = False
            self.sources = websearch.Sources()
            await self._hook("begin", origin)
            await self._hook("set_status", True, "Working")
            try:
                if self.client is not None and team and len(team.get("members") or []) >= 2:
                    await self._hook("set_status", True, "Team is researching")
                    try:
                        result = await self.council.run(text, team["members"], team.get("rounds", 1), mode)
                    except asyncio.CancelledError:
                        await self.channel.send_text("Stopped the team.")
                        return
                    self.sources = result["sources"]
                    await self._hook("set_status", True, "Lead is writing the final answer")
                    text = council_mod.Council.lead_prompt(text, result, mode)
                elif council_mod.mode_hint(mode):
                    text = f"{text}\n\n({council_mod.mode_hint(mode)})"

                if self.recent_reports:
                    text = ("[For context, automated runs since we last talked reported:\n"
                            + "\n".join(self.recent_reports[-5:]) + "]\n\n" + text)
                    self.recent_reports.clear()
                if self.client is None:
                    await self.channel.send_text(NO_MODEL_MSG)
                    return
                self.repeats = {}
                self.context_full = False
                self.active = self.client
                await self.client.query(text)
                await self._stream(self.client, self.channel, save_session=True)
                if self.context_full:
                    # The conversation outgrew the model's memory. Start fresh and retry once.
                    await self.channel.send_text("That one filled up my working memory, so I've started "
                                                 "a fresh conversation and I'm retrying the task now.")
                    await self.reset()
                    self.repeats = {}
                    self.context_full = False
                    self.active = self.client
                    await self.client.query(
                        text + "\n\n(Retry after running out of memory: work in small steps and only take "
                        "browser snapshots when you need to look at the page.)")
                    await self._stream(self.client, self.channel, save_session=True)
                    if self.context_full:
                        self.context_full = False
                        await self.reset()
                        await self.channel.send_text(
                            "⚠️ This task is too big for the local model's memory. Try splitting it into "
                            "smaller steps, or switch to a larger brain with Configure Steward.command.")
                await self._emit_sources()
            except Exception as e:
                log.exception("request failed")
                await self.channel.send_text(f"⚠️ Error: {Secrets.redact(str(e))[:500]}")
            finally:
                self.busy = False
                await self._hook("set_status", False, "")

    async def run_once(self, instructions: str, mode: str = "task", label: str = "Scheduled task") -> bool:
        """A scheduled task or proactive check, in its own fresh session.
        Returns True if anything was reported to the owner."""
        async with self.lock:
            self.busy = True
            await self._hook("begin", "background")
            await self._hook("set_status", True, label)
            if self._model_for(mode) is None:
                self.busy = False
                await self._hook("set_status", False, "")
                log.info("skipping '%s': no model set up", label)
                return False
            self.repeats = {}
            self.context_full = False
            buf = BufferChannel(self.channel)
            out: Channel = buf if mode == "watch" else self.channel
            prompt = (f"[Automated run: '{label}'. {config.OWNER_NAME} is probably away from the Mac.]\n\n{instructions}")
            if mode == "watch":
                prompt += (f"\n\nThis is a READ-ONLY check. If nothing needs {config.OWNER_NAME}'s attention, reply with exactly: "
                           "NOTHING. Otherwise reply with a short heads-up and what he may want to do.")
            else:
                await self.channel.send_text(f"⏰ {label}")
            self.sources = websearch.Sources()
            client = ClaudeSDKClient(self._options(None, out, mode))
            try:
                await client.connect()
                self.active = client
                await client.query(prompt)
                await self._stream(client, out, save_session=False)
                if mode != "watch":
                    await self._emit_sources()
            except Exception as e:
                log.exception("automated run failed")
                await self.channel.send_text(f"⚠️ '{label}' failed: {Secrets.redact(str(e))[:300]}")
            finally:
                await client.disconnect()
                self.busy = False
                await self._hook("set_status", False, "")

            if mode == "watch":
                final = (buf.texts[-1] if buf.texts else "").strip()
                if final and "NOTHING" not in final.upper()[:20]:
                    await self.channel.send_text(f"👀 {label}\n{final}")
                    for path, caption in buf.files:
                        await self.channel.send_file(path, caption)
                    self.recent_reports.append(f"{label}: {final[:400]}")
                    return True
                return False
            self.recent_reports.append(f"{label}: ran at {datetime.now():%I:%M %p}")
            return True
