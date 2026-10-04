# Contributing to Steward

Thanks for helping. Steward is a small, readable codebase, and contributions of any size are welcome: bug reports, docs, new chat apps, new model providers, or UI polish.

## Good places to start

- Issues labelled **good first issue**: small, well-scoped tasks.
- Docs: clearer setup steps, screenshots, translations.
- **Connectors:** a new chat app goes in `connectors.py`. Subclass `Connector` and implement `start`, `stop` and `_send`; pairing, approvals and commands are shared.
- **Model providers:** add a preset to `PROVIDERS` in `models.py`. OpenAI-format providers work through the bridge automatically.

## Run it from source

```bash
git clone https://github.com/stbattula-research/steward.git
cd steward
bash scripts/install.sh            # or double-click "Start Steward.command"
```

- **Stop the background service while developing:** `launchctl bootout gui/$(id -u)/com.steward.agent`
- **Chat in the terminal:** `.venv/bin/python main.py --cli`
- **Run the server and print the app link:** `.venv/bin/python main.py`, then `.venv/bin/python main.py --url`
- **UI with hot reload:** `cd web && npm install && npm run dev`. The prebuilt `web/dist` is committed, so run `npm run build` before your PR if you changed the UI.

## Code map

| File | What it does |
|---|---|
| `brain.py` | Agent loop (Claude Agent SDK), guardrail hooks, context recovery |
| `guardrails.py` | Allow / ask / block rules for every action |
| `bridge.py` | Local proxy that adds API keys and translates OpenAI-format providers |
| `council.py`, `websearch.py` | Team mode, web search with citations |
| `channels.py` | Routes replies to the Mac app, phone app and chat apps |
| `webserver.py`, `web/` | App backend and React UI |
| `mobile.py`, `connectors.py` | Phone app (Tailscale, push) and chat apps |
| `scheduler.py` | Scheduled tasks and watch checks |

## Ground rules

- **Safety first.** Anything that can change something outside `~/AgentWorkspace` must go through the guardrails and ask for approval. Please don't add ways around that.
- **No secrets in files.** API keys and tokens go in the macOS Keychain (see `models.set_key` and `connectors.set_secret`).
- **Keep it plug and play.** New features should work for a non-technical user from the app, without editing config files.
- Keep PRs focused, describe how you tested them, and include a screenshot for UI changes.

## Reporting security issues

Please don't open a public issue for security problems. Use GitHub's **Report a vulnerability** button on the Security tab instead.
