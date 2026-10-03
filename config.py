"""Central configuration, loaded from .env next to this file."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Identity -----------------------------------------------------------------
OWNER_NAME = os.getenv("OWNER_NAME", "").strip() or "the owner"   # who the agent works for
SERVICE_LABEL = "com.steward.agent"                                # launchd label

# --- Secrets ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Only this Telegram user ID can talk to the agent. Everyone else is ignored.
TELEGRAM_OWNER_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0") or 0)

# --- Brain --------------------------------------------------------------------
# Which model powers the agent:
#   ollama        local model on this Mac via Ollama (free, private, needs RAM)
#   anthropic     Claude via an Anthropic API key (strongest, pay per use)
#   ollama-cloud  Ollama's hosted models via an Ollama API key (no local RAM needed)
#   custom        any other Anthropic-compatible endpoint (BRAIN_BASE_URL + BRAIN_API_KEY + BRAIN_MODEL)
BRAIN_PROVIDER = os.getenv("BRAIN_PROVIDER", "ollama").strip().lower()
BRAIN_BASE_URL = os.getenv("BRAIN_BASE_URL", "").strip()
BRAIN_API_KEY = os.getenv("BRAIN_API_KEY", "").strip()
BRAIN_MODEL = os.getenv("BRAIN_MODEL", "").strip()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")   # any tool-calling model from `ollama list`
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or None          # None = SDK default
MAX_TURNS = int(os.getenv("MAX_TURNS", "60"))            # tool steps per request
MAX_BUDGET_USD = float(os.getenv("MAX_BUDGET_USD", "2.0"))  # spend cap per request

# --- Paths --------------------------------------------------------------------
HOME = Path.home()
WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", str(HOME / "AgentWorkspace"))).expanduser()
INBOX = WORKSPACE / "inbox"            # files you send it from your phone land here
OUTBOX = WORKSPACE / "outbox"          # screenshots / files it produces
MEMORY_DIR = ROOT / "memory"           # about_me.md, learned.md, playbooks/
STATE_DIR = HOME / ".steward"          # session id, logs, browser profile
BROWSER_PROFILE = STATE_DIR / "browser-profile"
LOG_FILE = STATE_DIR / "agent.log"
SESSION_FILE = STATE_DIR / "session.json"

# Keychain entries created by vault.py are stored under this service prefix.
KEYCHAIN_PREFIX = "steward"
VAULT_INDEX = STATE_DIR / "vault_index.json"   # names/usernames/URLs only, never passwords

# --- Guardrails ---------------------------------------------------------------
APPROVE_PASSWORDS = _bool("APPROVE_PASSWORDS", True)   # ask before every password fetch
APPROVAL_TIMEOUT_SEC = int(os.getenv("APPROVAL_TIMEOUT_SEC", "600"))
VERBOSE_STEPS = _bool("VERBOSE_STEPS", False)          # message you each tool step

RULES_FILE = ROOT / "rules.json"                       # your own allow / ask / block rules

# --- Always-on --------------------------------------------------------------
TIMEZONE = os.getenv("TIMEZONE", "America/Chicago")
SCHEDULES_FILE = STATE_DIR / "schedules.json"
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))  # proactive watchlist check; 0 = off
QUIET_HOURS = os.getenv("QUIET_HOURS", "22-7")                 # no heads-up pings 10pm-7am

# --- Integrations & voice ---------------------------------------------------
INTEGRATIONS_FILE = ROOT / "integrations.json"   # extra MCP servers (Gmail, GitHub, Notion, ...)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")

# --- Desktop app ---------------------------------------------------------------
AGENT_NAME = os.getenv("AGENT_NAME", "Steward")
AGENT_AVATAR = os.getenv("AGENT_AVATAR", "droid")     # any bot-avatars type: droid, clover, cat, ghost...
WEB_PORT = int(os.getenv("WEB_PORT", "8765"))
WEB_TOKEN_FILE = STATE_DIR / "web_token"
WEB_HISTORY_FILE = STATE_DIR / "web_history.json"
PREFS_FILE = STATE_DIR / "prefs.json"
OPEN_APP_ON_START = _bool("OPEN_APP_ON_START", False)

# Browser: Playwright MCP with a persistent profile so your logins stick.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")  # chrome | msedge | chromium

for d in (WORKSPACE, INBOX, OUTBOX, STATE_DIR, BROWSER_PROFILE, MEMORY_DIR / "playbooks"):
    d.mkdir(parents=True, exist_ok=True)

# First run: seed memory/ from the templates (memory/ is personal and never committed).
import shutil as _shutil
_TEMPLATES = ROOT / "memory.example"
if _TEMPLATES.exists():
    for _src in _TEMPLATES.rglob("*.md"):
        _dst = MEMORY_DIR / _src.relative_to(_TEMPLATES)
        if not _dst.exists() and not (_dst.parent == MEMORY_DIR / "playbooks" and any((MEMORY_DIR / "playbooks").iterdir())):
            _dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy(_src, _dst)


def model_label() -> str:
    """Short description of the active brain, shown in the app."""
    if BRAIN_PROVIDER == "ollama":
        return OLLAMA_MODEL
    if BRAIN_PROVIDER == "anthropic":
        return CLAUDE_MODEL or "Claude (API)"
    return f"{BRAIN_MODEL} ({BRAIN_PROVIDER})"
