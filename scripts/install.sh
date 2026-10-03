#!/bin/bash
# Installs everything Steward needs and (re)starts it. Safe to run again any time:
# steps that are already done are skipped. Normally run by "Start Steward.command".
set -e
cd "$(dirname "$0")/.."
DIR="$(pwd)"
LABEL="com.steward.agent"
STATE="$HOME/.steward"
step() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
envget() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

# ---------------------------------------------------------------- checks ---
[ "$(uname)" = "Darwin" ] || { echo "Steward runs on macOS."; exit 1; }
case "$DIR" in *"'"*|*'"'*) echo "Please move this folder to a path without quote characters."; exit 1;; esac

# --------------------------------------------------- migrate older installs ---
for OLD in com.saiteja.myagent; do
  if launchctl print "gui/$(id -u)/$OLD" >/dev/null 2>&1; then
    step "Stopping an older version of this agent ($OLD)"
    launchctl bootout "gui/$(id -u)/$OLD" 2>/dev/null || true
  fi
  rm -f "$HOME/Library/LaunchAgents/$OLD.plist"
done
if [ -d "$HOME/.my-agent" ] && [ ! -d "$STATE" ]; then
  step "Moving settings from ~/.my-agent to ~/.steward"
  mv "$HOME/.my-agent" "$STATE"
fi
rm -rf "$HOME/Applications/My Agent.app"

# --------------------------------------------------------------- homebrew ---
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null 2>&1; then
  step "Installing Homebrew (the standard macOS package manager). It will ask for your Mac password."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
BREW_PREFIX="$(brew --prefix)"

step "Checking tools (Python, Node, ffmpeg, cliclick)"
need() { command -v "$1" >/dev/null 2>&1 || brew install "${2:-$1}"; }
need python3.12 python@3.12
need node
need cliclick          # mouse and keyboard control
need ffmpeg            # voice notes

# ------------------------------------------------------------------ python ---
step "Python environment"
[ -x .venv/bin/python ] || python3.12 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
if [ "$(uname -m)" = "arm64" ]; then
  .venv/bin/pip install -q mlx-whisper || echo "   Voice notes skipped (mlx-whisper failed to install)."
else
  echo "   Voice notes need an Apple Silicon Mac; skipped."
fi

# ---------------------------------------------------------------- web UI ---
step "Desktop app UI"
if [ ! -f web/dist/index.html ]; then
  (cd web && npm ci --no-audit --no-fund && npm run build)
else
  echo "   Prebuilt UI found."
fi

# --------------------------------------------------------------- browser ---
step "Browser automation"
if [ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "   Using Google Chrome."
else
  echo "   Google Chrome not found; installing Playwright's Chromium instead."
  sed -i '' 's/^BROWSER_CHANNEL=.*/BROWSER_CHANNEL=chromium/' .env 2>/dev/null || true
  grep -q '^BROWSER_CHANNEL=' .env || echo "BROWSER_CHANNEL=chromium" >> .env
  npx -y playwright@latest install chromium
fi
npx -y @playwright/mcp@latest --help >/dev/null

# ------------------------------------------------------------------ brain ---
PROVIDER=$(envget BRAIN_PROVIDER); PROVIDER=${PROVIDER:-ollama}
if [ "$PROVIDER" = "ollama" ]; then
  MODEL=$(envget OLLAMA_MODEL); MODEL=${MODEL:-gemma4:12b}
  step "Local model: Ollama + $MODEL"
  # The agent's instructions and tools need a large context window; keep the model loaded between messages.
  launchctl setenv OLLAMA_CONTEXT_LENGTH 65536
  launchctl setenv OLLAMA_KEEP_ALIVE 30m
  if [ -d "/Applications/Ollama.app" ]; then
    echo "   Using the Ollama app (restarting it so the settings apply)."
    osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true; sleep 3; open -a Ollama
  else
    need ollama
    brew services restart ollama >/dev/null 2>&1 || (OLLAMA_CONTEXT_LENGTH=65536 nohup ollama serve >/dev/null 2>&1 &)
  fi
  for i in $(seq 1 30); do curl -s -o /dev/null http://localhost:11434/api/tags && break; sleep 1; done
  OLLAMA_BIN=$(command -v ollama || echo /Applications/Ollama.app/Contents/Resources/ollama)
  echo "   Downloading the model if needed (several GB the first time)…"
  "$OLLAMA_BIN" pull "$MODEL"
else
  step "Brain: $PROVIDER (no local model needed)"
fi

# --------------------------------------------------------------- service ---
step "Starting Steward in the background (starts at login, restarts if it crashes)"
mkdir -p "$HOME/Library/LaunchAgents" "$STATE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>$DIR/.venv/bin/python</string><string>$DIR/main.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BREW_PREFIX/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$STATE/stdout.log</string>
  <key>StandardErrorPath</key><string>$STATE/stderr.log</string>
</dict></plist>
EOF
plutil -lint -s "$PLIST" || { echo "   The service file is invalid: $PLIST"; exit 1; }
DOMAIN="gui/$(id -u)"
# Stop the running copy and wait until macOS has fully unloaded it (it can take a few seconds).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for i in $(seq 1 20); do launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
# Start it, retrying a few times if macOS is still busy.
started=no
for i in 1 2 3 4 5; do
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then started=yes; break; fi
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "$DOMAIN/$LABEL" && { started=yes; break; }
  fi
  sleep 2
done
[ "$started" = yes ] || { echo "   macOS refused to start the service. Restart your Mac, then double-click Start Steward.command again."; exit 1; }
echo "   Running."

# ------------------------------------------------------------ mac app -----
AGENT_NAME=$(envget AGENT_NAME); AGENT_NAME=${AGENT_NAME:-Steward}
APP="$HOME/Applications/$AGENT_NAME.app"
step "Creating the $AGENT_NAME app"
mkdir -p "$HOME/Applications"
[ -f "$STATE/app_path" ] && [ "$(cat "$STATE/app_path")" != "$APP" ] && rm -rf "$(cat "$STATE/app_path")"
osacompile -o "$APP" \
  -e "set u to do shell script \"cd '$DIR' && .venv/bin/python main.py --url\"" \
  -e 'try' \
  -e '  do shell script "open -na \"Google Chrome\" --args --app=" & quoted form of u' \
  -e 'on error' \
  -e '  open location u' \
  -e 'end try'
[ -f web/public/AppIcon.icns ] && cp web/public/AppIcon.icns "$APP/Contents/Resources/applet.icns" && touch "$APP"
echo "$APP" > "$STATE/app_path"
echo "   $APP (drag it to your Dock to keep it handy)"
