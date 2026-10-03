#!/bin/bash
# Steward setup wizard: writes .env from your answers.
#   bash scripts/configure.sh            # only if .env doesn't exist yet
#   bash scripts/configure.sh --force    # change settings (brain, name, Telegram...)
set -e
cd "$(dirname "$0")/.."

if [ -f .env ] && [ "$1" != "--force" ]; then
  exit 0
fi

bold=$(tput bold 2>/dev/null || true); dim=$(tput dim 2>/dev/null || true); reset=$(tput sgr0 2>/dev/null || true)

# Keep current values as defaults when reconfiguring.
get() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }

ask() {   # ask "Question" default -> $REPLY
  local q="$1" def="$2"
  if [ -n "$def" ]; then read -r -p "$q ${dim}[$def]${reset}: " REPLY; else read -r -p "$q: " REPLY; fi
  REPLY="${REPLY:-$def}"
}
ask_secret() {
  local q="$1" def="$2"
  if [ -n "$def" ]; then read -r -s -p "$q ${dim}[keep current]${reset}: " REPLY; else read -r -s -p "$q: " REPLY; fi
  echo; REPLY="${REPLY:-$def}"
}

echo
echo "${bold}Steward setup${reset}"
echo "Answer a few questions. Press Enter to accept the value in [brackets]."
echo "You can change these later with ${bold}Configure Steward.command${reset}."
echo

# --- who --------------------------------------------------------------------
DEFAULT_OWNER=$(get OWNER_NAME); DEFAULT_OWNER=${DEFAULT_OWNER:-$(id -F 2>/dev/null | awk '{print $1}')}
ask "Your first name (so the agent knows who it works for)" "$DEFAULT_OWNER"; OWNER_NAME="$REPLY"
DEFAULT_AGENT=$(get AGENT_NAME); ask "Name for your agent" "${DEFAULT_AGENT:-Steward}"; AGENT_NAME="$REPLY"

# --- brain ------------------------------------------------------------------
RAM_GB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 17179869184) / 1073741824 ))
ARCH=$(uname -m)
if   [ "$RAM_GB" -ge 40 ]; then REC_MODEL="gemma4:26b"
elif [ "$RAM_GB" -ge 14 ]; then REC_MODEL="gemma4:12b"
else                            REC_MODEL="gemma4:e4b"; fi

echo
echo "${bold}Which brain should power the agent?${reset}"
echo "  1) Local model with Ollama    free, private, runs on this Mac (${RAM_GB} GB RAM → ${REC_MODEL})"
echo "  2) Claude via Anthropic API   strongest at multi-step tasks, pay per use (needs an API key)"
echo "  3) Ollama Cloud               hosted open models, no local RAM needed (needs an Ollama API key)"
echo "  4) Custom                     any Anthropic-compatible endpoint (base URL + key + model)"
case "$(get BRAIN_PROVIDER)" in anthropic) D=2;; ollama-cloud) D=3;; custom) D=4;; *) D=1;; esac
ask "Choose 1-4" "$D"
BRAIN_PROVIDER=ollama; OLLAMA_MODEL=$(get OLLAMA_MODEL); ANTHROPIC_API_KEY=$(get ANTHROPIC_API_KEY); CLAUDE_MODEL=$(get CLAUDE_MODEL)
BRAIN_BASE_URL=$(get BRAIN_BASE_URL); BRAIN_API_KEY=$(get BRAIN_API_KEY); BRAIN_MODEL=$(get BRAIN_MODEL)
case "$REPLY" in
  2) BRAIN_PROVIDER=anthropic
     echo "Get a key at https://console.anthropic.com (set a monthly spend limit there)."
     ask_secret "Anthropic API key" "$ANTHROPIC_API_KEY"; ANTHROPIC_API_KEY="$REPLY"
     ask "Claude model (blank = SDK default)" "$CLAUDE_MODEL"; CLAUDE_MODEL="$REPLY" ;;
  3) BRAIN_PROVIDER=ollama-cloud; BRAIN_BASE_URL="https://ollama.com"
     echo "Create a key at https://ollama.com/settings/keys and pick a cloud model at https://ollama.com/search?c=cloud"
     ask_secret "Ollama API key" "$BRAIN_API_KEY"; BRAIN_API_KEY="$REPLY"
     ask "Model name" "${BRAIN_MODEL:-gpt-oss:120b}"; BRAIN_MODEL="$REPLY" ;;
  4) BRAIN_PROVIDER=custom
     ask "Base URL (Anthropic Messages API compatible)" "$BRAIN_BASE_URL"; BRAIN_BASE_URL="$REPLY"
     ask_secret "API key / token" "$BRAIN_API_KEY"; BRAIN_API_KEY="$REPLY"
     ask "Model name" "$BRAIN_MODEL"; BRAIN_MODEL="$REPLY" ;;
  *) BRAIN_PROVIDER=ollama
     [ "$ARCH" != "arm64" ] && echo "Note: this Mac has an Intel chip; local models will be slow. Options 2-4 work better."
     ask "Ollama model (any model with tool calling)" "${OLLAMA_MODEL:-$REC_MODEL}"; OLLAMA_MODEL="$REPLY" ;;
esac

# --- Telegram (optional) ----------------------------------------------------
echo
echo "${bold}Telegram (optional)${reset} lets you message the agent from your phone."
echo "${dim}Create a bot: message @BotFather → /newbot. Your user ID: message @userinfobot. Leave blank to skip.${reset}"
ask_secret "Telegram bot token" "$(get TELEGRAM_BOT_TOKEN)"; TELEGRAM_BOT_TOKEN="$REPLY"
TELEGRAM_OWNER_ID=$(get TELEGRAM_OWNER_ID)
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then ask "Your numeric Telegram user ID" "$TELEGRAM_OWNER_ID"; TELEGRAM_OWNER_ID="$REPLY"; fi

# --- time zone --------------------------------------------------------------
TZ_DEFAULT=$(get TIMEZONE); TZ_DEFAULT=${TZ_DEFAULT:-$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')}
TIMEZONE=${TZ_DEFAULT:-America/Chicago}

# --- write .env (keeps any other settings you added) ------------------------
EXTRA=""
if [ -f .env ]; then
  EXTRA=$(grep -vE '^(OWNER_NAME|AGENT_NAME|BRAIN_PROVIDER|OLLAMA_MODEL|ANTHROPIC_API_KEY|CLAUDE_MODEL|BRAIN_BASE_URL|BRAIN_API_KEY|BRAIN_MODEL|TELEGRAM_BOT_TOKEN|TELEGRAM_OWNER_ID|TIMEZONE)=' .env | grep -v '^# --- written by configure' || true)
fi
umask 077
{
  echo "# --- written by configure.sh (edit freely, or re-run Configure Steward.command)"
  echo "OWNER_NAME=$OWNER_NAME"
  echo "AGENT_NAME=$AGENT_NAME"
  echo "TIMEZONE=$TIMEZONE"
  echo "BRAIN_PROVIDER=$BRAIN_PROVIDER"
  echo "OLLAMA_MODEL=$OLLAMA_MODEL"
  echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  echo "CLAUDE_MODEL=$CLAUDE_MODEL"
  echo "BRAIN_BASE_URL=$BRAIN_BASE_URL"
  echo "BRAIN_API_KEY=$BRAIN_API_KEY"
  echo "BRAIN_MODEL=$BRAIN_MODEL"
  echo "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN"
  echo "TELEGRAM_OWNER_ID=$TELEGRAM_OWNER_ID"
  if [ -n "$EXTRA" ]; then echo; echo "$EXTRA"; else echo; sed -n '/^# --- Optional/,$p' .env.example; fi
} > .env.tmp
mv .env.tmp .env
chmod 600 .env
echo
echo "Saved your settings to .env (private to your user account)."
