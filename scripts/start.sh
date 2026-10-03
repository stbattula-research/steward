#!/bin/bash
# What "Start Steward.command" runs: setup wizard (first time) → install/update → open the app.
cd "$(dirname "$0")/.."
clear
cat <<'BANNER'
  ____  _                                 _
 / ___|| |_ _____      ____ _ _ __ __| |
 \___ \| __/ _ \ \ /\ / / _` | '__/ _` |
  ___) | ||  __/\ V  V / (_| | | | (_| |
 |____/ \__\___| \_/\_/ \__,_|_|  \__,_|   your private agent, on your Mac
BANNER

fail() { echo; echo "Setup stopped: $1"; echo "Fix the problem above and double-click Start Steward.command again."; read -n1 -r -p "Press any key to close."; exit 1; }

bash scripts/configure.sh "$@" || fail "the setup wizard didn't finish."
bash scripts/install.sh || fail "installation hit an error."

PORT=$(grep -E '^WEB_PORT=' .env | cut -d= -f2); PORT=${PORT:-8765}
NAME=$(grep -E '^AGENT_NAME=' .env | cut -d= -f2-); NAME=${NAME:-Steward}
printf "\n\033[1m==> Waiting for %s to start\033[0m\n" "$NAME"
for i in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 2; done

if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
  open "$(cat "$HOME/.steward/app_path" 2>/dev/null)" 2>/dev/null || open "$(.venv/bin/python main.py --url)"
  cat <<MSG

All set. $NAME is running and opening now.
 • It keeps running in the background and starts again when you log in.
 • The first reply takes a minute while the model warms up.
 • macOS will ask for permissions the first time it needs them (Screen Recording,
   Accessibility, Automation). Allow them in System Settings → Privacy & Security.
 • Change brain/name/Telegram: Configure Steward.command · Remove: Uninstall Steward.command

You can close this window.
MSG
else
  echo "$NAME didn't start. Last lines of the log:"
  tail -n 25 "$HOME/.steward/stderr.log" 2>/dev/null
fi
echo
read -n1 -r -p "Press any key to close this window."
