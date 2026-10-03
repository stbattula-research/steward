"""Saved logins backed by the macOS Keychain.

Passwords live ONLY in the Keychain (encrypted by macOS). This file keeps a small
index of names, usernames and URLs so the agent knows which logins exist.

Usage (run in Terminal on your Mac):
    python vault.py add chase --user you@email.com --url https://chase.com
    python vault.py list
    python vault.py remove chase
"""
import argparse
import getpass
import json
import subprocess
import sys

import config


def _service(name: str) -> str:
    return f"{config.KEYCHAIN_PREFIX}:{name.lower()}"


def _load_index() -> dict:
    if config.VAULT_INDEX.exists():
        return json.loads(config.VAULT_INDEX.read_text())
    return {}


def _save_index(idx: dict) -> None:
    config.VAULT_INDEX.write_text(json.dumps(idx, indent=2))
    config.VAULT_INDEX.chmod(0o600)


def list_logins() -> dict:
    """{name: {"username": ..., "url": ..., "notes": ...}} without passwords."""
    return _load_index()


def get_password(name: str) -> tuple[str, str]:
    """Return (username, password) for a saved login. Raises KeyError if missing."""
    idx = _load_index()
    key = name.lower()
    if key not in idx:
        raise KeyError(f"No saved login named '{name}'. Saved: {', '.join(idx) or 'none'}")
    user = idx[key]["username"]
    out = subprocess.run(
        ["security", "find-generic-password", "-s", _service(key), "-a", user, "-w"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise KeyError(f"Keychain lookup failed for '{name}': {out.stderr.strip()}")
    return user, out.stdout.rstrip("\n")


def add(name: str, username: str, url: str = "", notes: str = "") -> None:
    pw = getpass.getpass(f"Password for {name} ({username}): ")
    if not pw:
        sys.exit("Empty password, nothing saved.")
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _service(name), "-a", username,
         "-l", f"Steward - {name}", "-w", pw],
        check=True,
    )
    idx = _load_index()
    idx[name.lower()] = {"username": username, "url": url, "notes": notes}
    _save_index(idx)
    print(f"Saved '{name}' to Keychain.")


def remove(name: str) -> None:
    idx = _load_index()
    entry = idx.pop(name.lower(), None)
    if not entry:
        sys.exit(f"No saved login named '{name}'.")
    subprocess.run(["security", "delete-generic-password", "-s", _service(name), "-a", entry["username"]],
                   capture_output=True)
    _save_index(idx)
    print(f"Removed '{name}'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Manage the agent's Keychain logins")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("--user", required=True)
    a.add_argument("--url", default=""); a.add_argument("--notes", default="")
    sub.add_parser("list")
    r = sub.add_parser("remove"); r.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "add":
        add(args.name, args.user, args.url, args.notes)
    elif args.cmd == "list":
        for n, e in list_logins().items():
            print(f"{n:20} {e['username']:30} {e.get('url', '')}")
    elif args.cmd == "remove":
        remove(args.name)
