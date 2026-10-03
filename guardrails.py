"""Decides which tool calls run freely, which need your approval, and which are blocked."""
import fnmatch
import json
import logging
import re
from dataclasses import dataclass

import config

ALLOW, ASK, BLOCK = "allow", "ask", "block"


@dataclass
class Verdict:
    decision: str          # allow | ask | block
    reason: str = ""


# Never allowed, even with approval: reading secrets around the vault tool.
BLOCKED_BASH = [
    (r"\bsecurity\s+(find|dump|export)", "Read passwords with the get_password tool, not the shell."),
    (r"Library/Keychains", "Direct keychain file access is blocked."),
    (r"\.ssh/(id_|.*key)", "Private SSH keys are off-limits."),
    (r"\.steward/|\.my-agent/|/\.env\b|\.aws/credentials|\.gnupg/", "Agent secrets and credential files are off-limits."),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)\s*($|;|&)", "Refusing to wipe a root/home directory."),
    (r"\bmkfs|\bdiskutil\s+(erase|partition)", "Disk formatting is blocked."),
    (r":\(\)\s*\{", "Fork bomb pattern blocked."),
]

# Allowed only after you tap Approve.
RISKY_BASH = [
    (r"\bsudo\b", "runs as administrator"),
    (r"\brm\b|\brmdir\b|\bunlink\b|\bshred\b", "deletes files"),
    (r"\bmv\b", "moves/overwrites files"),
    (r"\bdd\b", "raw disk write"),
    (r"\bchmod\b|\bchown\b", "changes permissions"),
    (r"\bkill(all)?\b|\bpkill\b", "kills processes"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "power action"),
    (r"\b(curl|wget)\b.*\|\s*(ba|z)?sh", "pipes internet script into shell"),
    (r"\bgit\s+(push|reset\s+--hard|clean)", "irreversible git operation"),
    (r"\blaunchctl\b|\bdefaults\s+write\b|\bcrontab\b", "changes system configuration"),
    (r"\b(pip|pip3|npm|brew)\s+(install|uninstall|remove)", "installs/removes software"),
    (r"osascript.*(send|delete|empty trash|make new outgoing message)", "sends or deletes via AppleScript"),
    (r"\bmail\b|\bsendmail\b", "sends email"),
    (r">\s*~?/?(etc|Library|System)/", "writes to a system folder"),
]

SENSITIVE_PATHS = [r"/\.ssh/", r"Library/Keychains", r"/\.steward/", r"\.env$", r"/\.aws/", r"/\.gnupg/"]

# Read-only / harmless tools.
SAFE_TOOLS = {
    "WebSearch", "WebFetch", "TodoWrite", "Task",
    "mcp__me__take_screenshot", "mcp__me__send_file_to_me", "mcp__me__request_approval",
    "mcp__me__list_saved_logins", "mcp__me__remember", "mcp__me__notify_me",
    "mcp__sched__list_scheduled_tasks", "mcp__sched__cancel_scheduled_task",
}
# Browser actions that only look, navigate or type are fine; the agent must call
# request_approval itself before a click that pays, sends, submits or deletes.
SAFE_BROWSER = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
    "browser_click", "browser_type", "browser_fill_form", "browser_select_option",
    "browser_hover", "browser_press_key", "browser_wait_for", "browser_tabs",
    "browser_resize", "browser_close", "browser_console_messages", "browser_network_requests",
    "browser_handle_dialog", "browser_drag", "browser_install",
}
RISKY_BROWSER = {"browser_evaluate", "browser_run_code", "browser_file_upload", "browser_pdf_save"}


def _match(patterns, text):
    for pat, why in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return why
    return None


def _in_safe_area(path: str) -> bool:
    p = str(path)
    return p.startswith(str(config.WORKSPACE)) or p.startswith(str(config.MEMORY_DIR))


# ------------------------------------------------------------ your own rules --
def _primary_arg(tool_input: dict) -> str:
    for k in ("command", "file_path", "path", "url", "name", "pattern", "query"):
        if k in tool_input:
            return str(tool_input[k])
    return json.dumps(tool_input)


def _load_rules() -> dict:
    """rules.json: {"allow": [...], "ask": [...], "block": [...]}.
    Each entry is "ToolGlob" or "ToolGlob(argument glob)", e.g. "Bash(git status*)",
    "mcp__gmail__search_*", "mcp__browser__browser_type(*bank*)"."""
    try:
        return json.loads(config.RULES_FILE.read_text()) if config.RULES_FILE.exists() else {}
    except Exception as e:
        logging.getLogger("guardrails").error("rules.json is invalid: %s", e)
        return {}


def _rule_hits(rule: str, tool_name: str, tool_input: dict) -> bool:
    m = re.fullmatch(r"([^()]+)(?:\((.*)\))?", rule.strip())
    if not m or not fnmatch.fnmatchcase(tool_name, m.group(1).strip()):
        return False
    return m.group(2) is None or fnmatch.fnmatch(_primary_arg(tool_input), m.group(2))


def evaluate(tool_name: str, tool_input: dict) -> Verdict:
    """Built-in hard blocks first, then your rules.json, then the defaults."""
    base = _default(tool_name, tool_input)
    if base.decision == BLOCK:
        return base                        # rules.json can never unblock these
    rules = _load_rules()
    for decision in (BLOCK, ASK, ALLOW):
        for rule in rules.get(decision, []):
            if _rule_hits(rule, tool_name, tool_input):
                return Verdict(decision, f"your rule: {rule}")
    return base


# --------------------------------------------------------- read-only "watch" --
WATCH_BASH_OK = re.compile(
    r"^\s*(ls|cat|head|tail|grep|rg|find|mdfind|wc|date|cal|df|du|ps|top -l 1|uptime|"
    r"pmset -g|sw_vers|system_profiler|which|echo|pwd|stat|file|icalbuddy)\b")
WATCH_BROWSER_OK = {"browser_navigate", "browser_navigate_back", "browser_snapshot",
                    "browser_take_screenshot", "browser_click", "browser_wait_for", "browser_tabs",
                    "browser_hover", "browser_close", "browser_resize"}


def evaluate_watch(tool_name: str, tool_input: dict) -> Verdict:
    """Background checks may look, never act. Nothing here ever asks you."""
    v = evaluate(tool_name, tool_input)
    if v.decision != ALLOW:
        return Verdict(BLOCK, "Read-only background check: that needs the owner, so report it instead.")
    if tool_name in ("mcp__me__request_approval", "mcp__me__get_password", "mcp__me__remember"):
        return Verdict(BLOCK, "Not available during background checks.")
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not WATCH_BASH_OK.match(cmd) or re.search(r"[;&|`$>]", cmd):
            return Verdict(BLOCK, "Only simple read-only commands are allowed in background checks.")
    if tool_name.startswith("mcp__browser__") and tool_name.removeprefix("mcp__browser__") not in WATCH_BROWSER_OK:
        return Verdict(BLOCK, "Background checks can browse but not type or submit.")
    if tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        return Verdict(BLOCK, "No edits during background checks.")
    if tool_name == "Write" and not str(tool_input.get("file_path", "")).startswith(str(config.WORKSPACE)):
        return Verdict(BLOCK, "Background checks may only write notes in the workspace.")
    return v


def _default(tool_name: str, tool_input: dict) -> Verdict:
    if tool_name in SAFE_TOOLS:
        return Verdict(ALLOW)

    if tool_name == "mcp__me__get_password":
        return Verdict(ASK if config.APPROVE_PASSWORDS else ALLOW,
                       f"use saved login '{tool_input.get('name')}'")

    if tool_name in ("Read", "Glob", "Grep"):
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if any(re.search(p, path) for p in SENSITIVE_PATHS):
            return Verdict(BLOCK, "That file is protected.")
        return Verdict(ALLOW)

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if why := _match(BLOCKED_BASH, cmd):
            return Verdict(BLOCK, why)
        if why := _match(RISKY_BASH, cmd):
            return Verdict(ASK, why)
        return Verdict(ALLOW)

    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if any(re.search(p, path) for p in SENSITIVE_PATHS):
            return Verdict(BLOCK, "That file is protected.")
        if _in_safe_area(path):
            return Verdict(ALLOW)
        return Verdict(ASK, f"modifies a file outside the agent workspace: {path}")

    if tool_name == "mcp__sched__schedule_task":
        return Verdict(ASK, "sets up an automation that will run on its own")

    if tool_name.startswith("mcp__browser__"):
        short = tool_name.removeprefix("mcp__browser__")
        if short in SAFE_BROWSER:
            return Verdict(ALLOW)
        if short in RISKY_BROWSER:
            return Verdict(ASK, f"runs browser action {short}")
        return Verdict(ASK, f"unrecognised browser action {short}")

    # Integrations (Gmail, GitHub, ...) and anything new: ask, unless rules.json allows it.
    return Verdict(ASK, f"uses {tool_name}")


def describe(tool_name: str, tool_input: dict) -> str:
    """Short human-readable description of a tool call, for approval prompts."""
    if tool_name == "Bash":
        return f"Run command:\n{tool_input.get('command', '')[:800]}"
    if tool_name in ("Write", "Edit", "MultiEdit"):
        return f"{tool_name} file: {tool_input.get('file_path')}"
    if tool_name == "mcp__me__get_password":
        return f"Fetch password for saved login: {tool_input.get('name')}"
    if tool_name == "mcp__sched__schedule_task":
        when = tool_input.get("cron") or tool_input.get("run_at")
        return (f"Schedule '{tool_input.get('name')}' ({tool_input.get('mode', 'task')}, {when}):\n"
                f"{tool_input.get('instructions', '')[:600]}")
    short = tool_name.replace("mcp__browser__", "browser: ").replace("mcp__me__", "")
    detail = ", ".join(f"{k}={str(v)[:120]}" for k, v in tool_input.items())
    return f"{short}({detail})"
