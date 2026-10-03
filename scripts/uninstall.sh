#!/bin/bash
# Removes Steward's background service and app. Your project folder is left in place.
cd "$(dirname "$0")/.."
LABEL="com.steward.agent"
STATE="$HOME/.steward"

echo "This stops Steward and removes its background service and app."
read -r -p "Continue? [y/N] " ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || { echo "Nothing changed."; exit 0; }

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
[ -f "$STATE/app_path" ] && rm -rf "$(cat "$STATE/app_path")"
echo "Stopped and removed the service and app."

HAS_LOGINS=no; [ -s "$STATE/vault_index.json" ] && HAS_LOGINS=yes

read -r -p "Also delete saved data (chat history, browser logins, logs in ~/.steward)? [y/N] " wipe
if [ "$wipe" = "y" ] || [ "$wipe" = "Y" ]; then
  rm -rf "$STATE"
  echo "Deleted ~/.steward."
fi

if [ "$HAS_LOGINS" = yes ]; then
  echo "Saved passwords stay in your Keychain (named 'Steward - <name>'). Remove them in Keychain Access,"
  echo "or before deleting ~/.steward with: .venv/bin/python vault.py remove <name>"
fi
echo "Done. You can delete this folder to remove Steward completely."
