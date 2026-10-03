# Steward

**A private AI agent that lives on your Mac and works only for you.**

Ask it in a desktop app or from your phone (Telegram), by text or voice, and Steward does the task on your computer the way you would. It runs commands, opens apps, uses websites with your logins, takes screenshots, and sends you files. It also keeps working while you're away: it runs scheduled tasks and watches the things you care about, messaging you only when something needs you.

You choose what powers it: a **free local model** that never leaves your Mac (Ollama), **Claude** through an API key, or any compatible hosted model.

![Steward desktop app](docs/approval.png)

---

## What it can do

| | |
|---|---|
| 🖥️ **Operate your Mac** | Runs shell commands, opens and controls apps (Mail, Calendar, Reminders, Notes, Music, Finder via AppleScript), moves the mouse and keyboard, finds and organizes files. |
| 🌐 **Use the web like you do** | Drives a real Chrome profile that stays signed in, so it can navigate, fill forms, download statements and compare prices on sites you already use. |
| 🔐 **Log in safely** | Passwords live in the **macOS Keychain**, never in a file. It asks before using one, types it only into the matching site, and scrubs it from every message and log. |
| 💬 **Talk from anywhere** | A desktop app for your laptop, plus an optional Telegram bot for your phone. One conversation that you can continue on either. |
| 🎙️ **Voice** | Hold a conversation with voice notes, transcribed on-device with Whisper. |
| ⏰ **Scheduled tasks** | "Every weekday at 8am send me my calendar", "Remind me at 5pm to call the bank", "Every Sunday clean up my Downloads". |
| 👀 **Proactive watching** | "Tell me when the flight price drops", plus an hourly watchlist. These checks are read-only and only ping you when something needs attention. |
| 🧠 **Learns your way** | Memory about you, things it learned, and **playbooks**: step-by-step instructions for tasks you repeat, written in plain English. |
| 🧩 **Extensible** | Add any MCP server (GitHub, Notion, Gmail, databases…) in one JSON file. |
| ✅ **Asks before anything risky** | Paying, sending, submitting, deleting, `sudo` and installs pause for your **Approve / Deny**. |

---

## Quick start

**Requirements:** macOS 13 or later, preferably on Apple Silicon (M1–M4). Allow about 15 GB of disk space for a local model. Everything else installs automatically.

```bash
git clone https://github.com/stbattula-research/steward.git
```

Then open the `steward` folder in Finder and **double-click `Start Steward.command`**.

> If macOS says the file "can't be opened", right-click it → **Open** → **Open**. This only happens for downloaded ZIPs, not git clones.

The first run takes about 10–20 minutes, mostly downloading the model. It will:

1. **Ask a few questions:** your name, a name for your agent, which brain to use, and optionally a Telegram bot.
2. **Install what's missing:** Homebrew, Python 3.12, Node, ffmpeg, cliclick, Ollama and the model you chose, Playwright, and on-device Whisper.
3. **Start Steward in the background:** it restarts on crashes and starts at login.
4. **Create the Steward app** in `~/Applications` and open it. Drag the app to your Dock.

The first reply takes about a minute while the model warms up. After that, open the app anytime, or message your Telegram bot.

To change the brain, name or Telegram later, double-click **`Configure Steward.command`**. To remove Steward, use **`Uninstall Steward.command`**.

---

## Choose your brain

| Option | Best for | Cost | Privacy | Notes |
|---|---|---|---|---|
| **Local (Ollama)** | Trying it out, privacy, simple tasks | Free | Nothing leaves your Mac | The wizard picks a model for your RAM: 8 GB → `gemma4:e4b`, 16 GB → `gemma4:12b`, 40 GB+ → `gemma4:26b`. Slower and less reliable on long multi-step tasks. |
| **Claude (Anthropic API)** | Complex, multi-step tasks across websites | Pay per use | Requests go to Anthropic | Get a key at [console.anthropic.com](https://console.anthropic.com) and set a monthly spend limit. Per-task cap: `MAX_BUDGET_USD`. |
| **Ollama Cloud** | Big open models without the RAM | Ollama plan | Requests go to Ollama | Key at [ollama.com/settings/keys](https://ollama.com/settings/keys). |
| **Custom** | Your own gateway, or OpenRouter | Varies | Varies | Any endpoint that speaks the Anthropic Messages API (`BRAIN_BASE_URL`, `BRAIN_API_KEY`, `BRAIN_MODEL`), e.g. OpenRouter at `https://openrouter.ai/api`. Set `BRAIN_CONTEXT_TOKENS` to the model's context size. Free tiers allow only a few dozen requests a day, and one browser task can use 20–30. |

All four use the same agent loop, tools and safety rules (built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)). Ollama provides an [Anthropic-compatible API](https://docs.ollama.com/integrations/claude-code), which is why local models work too.

---

## Use any model, switch any time

Add as many models as you like, then choose one for each chat from the **model picker in the chat box**. Open **Models** in the sidebar (or "Add or manage models…" in the picker) to add, test or remove them. You can also mark one model as the one for **scheduled tasks and heads-ups**, for example a free local model for background checks and a stronger hosted model for hands-on work. From your phone, `/model` lists them and `/model 2` switches.

| Works directly (Claude format) | Translated automatically (OpenAI format) |
|---|---|
| Ollama on this Mac, Claude (Anthropic), OpenRouter, DeepSeek, Ollama Cloud, or any other Claude-format endpoint | Google Gemini, OpenAI, Groq, Mistral, NVIDIA NIM, or any other OpenAI-format endpoint |

- **Keys stay safe.** API keys are stored in the **macOS Keychain**. The agent never sees them: its requests go through a small local bridge that adds the key, and translates OpenAI-format providers using [LiteLLM](https://github.com/BerriAI/litellm).
- **Test before you chat.** **Test connection** sends a tiny request, so a wrong key or model ID shows up straight away.
- **Switching models starts a fresh conversation.** Your memory, playbooks and scheduled tasks are shared by all models.
- **Free tiers have limits.** Agent tasks use many requests (a browser task can use 20–30), so free tiers run out quickly. Some free tiers, like Gemini's, may also use your data to improve the provider's products.

## Using it

### Desktop app
![Chat](docs/chat.png)

- **Chat:** type, or click 🎙 to talk. Drop or paste files and screenshots in.
- **Live activity:** an animated orb shows what it's doing (browsing, running a command, writing), with the exact step underneath. Finished steps fold into a "3 steps" row you can expand.
- **Approvals:** risky actions show up as glowing cards with **Approve / Deny** buttons.
- **Sidebar:** status, quick actions (screenshot, check watchlist, new chat, stop), scheduled tasks you can cancel, and the **Phone alerts** switch.
- **Appearance:** choose **Auto** (follows your Mac), **Light** or **Dark** at the bottom of the sidebar. You get desktop notifications when the window is in the background.

### From your phone (Telegram, optional)
Text or voice-note your bot. Commands: `/screen` `/stop` `/new` `/status` `/tasks` `/cancel <id>` `/watch` `/memory`.

The app and Telegram share **one conversation**. Messages from your phone appear in the app's history.

**Phone alerts** (set in the sidebar) controls what reaches your phone:

- **Auto:** heads-ups go to your phone only when you haven't used the app for 10 minutes.
- **Always:** everything also goes to your phone.
- **Off:** your phone only gets replies to messages you sent from it.

### Things to try
- *What's using the most space on my Mac?*
- *Download my latest bank statement and send it to me*
- *Every weekday at 7:45am send me today's calendar and any urgent email*
- *Tell me if the price of this flight drops below $900: <link>*
- *Open Spotify and play my Discover Weekly*
- *Rename the screenshots on my Desktop by date and move them to ~/Pictures/Screenshots*

---

## Teach it how you work

Open **Memory & playbooks** in the sidebar:

![Memory](docs/memory.png)

- **About me:** facts it reads at the start of every conversation.
- **Watchlist:** lines starting with `-` are checked every hour, outside quiet hours.
- **Learned:** things it saved when you said "always do X".
- **Playbooks:** your step-by-step way of doing a recurring task, such as paying a bill or filing expenses. It reads the matching playbook first, and offers to write one after a task you're likely to repeat.

These are plain Markdown files in `memory/` (git-ignored, so they stay personal).

### Saved logins (macOS Keychain)
```bash
.venv/bin/python vault.py add chase --user you@email.com --url https://chase.com
.venv/bin/python vault.py list
```
For most sites you don't need this: tell Steward to open the site once, sign in yourself (including 2FA), and the session stays signed in.

### More apps (MCP)
Add servers to `integrations.json`; examples for GitHub and a filesystem folder are included. New tools ask for approval until you allow them in `rules.json`, for example `"allow": ["mcp__github__get_*"]`.

---

## Safety model

| Runs freely | Asks you first (Approve / Deny) | Always blocked |
|---|---|---|
| Reading files, web search, browsing, typing in pages, opening apps, screenshots | Paying, buying, sending, posting, submitting forms, deleting (`rm`), `mv`, `sudo`, installs, `kill`, git push, editing files outside `~/AgentWorkspace`, using any saved password, running JavaScript in pages, creating automations | Reading the Keychain through the shell, SSH keys, `.env`, the agent's own state folder, disk erase |

- **Background checks are read-only:** they can look, but can't type, submit, use passwords or run anything except simple read-only commands.
- **Your own rules:** `rules.json` adds allow, ask or block rules, for example `"block": ["Bash(*git push --force*)"]`. Built-in blocks can't be overridden.
- **Prompt-injection aware:** text on web pages and in emails is treated as information, never as instructions.
- **Locked down:** the desktop app listens on `127.0.0.1` only, needs a secret sign-in link (the app handles it), and refuses other websites. Telegram answers only your user ID.
- **Every action is checked:** the rules run as a hook on every action the agent takes, including "read-only" ones the engine would otherwise allow on its own.
- **Stuck-loop guard:** if the agent repeats the same action 3 times, the next attempt is refused and it's told to change approach or report back.
- **Limits:** approvals time out after 10 minutes (counted as denied), and each task has a step limit and, for the Anthropic API, a spend cap.

> Steward can do anything you can do on your Mac. Read approval cards before tapping Approve, and start with small tasks.

---

## Configuration

Settings live in `.env`. The wizard writes it; `.env.example` documents every option.

| Setting | Default | What it does |
|---|---|---|
| `BRAIN_PROVIDER` | `ollama` | `ollama`, `anthropic`, `ollama-cloud` or `custom` |
| `OLLAMA_MODEL` | by RAM | Any local model with tool calling |
| `AGENT_NAME` / `AGENT_AVATAR` | `Steward` / `droid` | Name and face in the app (`droid`, `cat`, `ghost`, `cloud`, `star`, …) |
| `HEARTBEAT_MINUTES` | `60` | How often the watchlist is checked (`0` = off) |
| `QUIET_HOURS` | `22-7` | No heads-up pings during these hours |
| `APPROVE_PASSWORDS` | `true` | Ask before each saved-password use |
| `MAX_TURNS` / `MAX_BUDGET_USD` | `60` / `2.0` | Per-task step limit and spend cap |
| `WEB_PORT` | `8765` | Local port for the desktop app |

---

## How it works

```
Desktop app (React, 127.0.0.1) ─► webserver.py ─┐
Telegram (optional)          ──► telegram_bot.py ┴► channels.py ─► brain.py ─► model (Ollama / Claude / …)
scheduler.py (tasks, watchlist) ────────────────────────────────┘      │
                                     guardrails.py (allow / ask / block every action)
                                     tools: shell, files, browser (Playwright MCP), screenshot,
                                            Keychain logins, approvals, memory, schedules, your MCP servers
```

| Path | Purpose |
|---|---|
| `Start Steward.command` | One-click install, update and open |
| `Configure Steward.command` / `Uninstall Steward.command` | Change settings / remove |
| `scripts/` | Setup wizard, installer, uninstaller |
| `brain.py` | Agent sessions on the Claude Agent SDK; brain selection; prompt size kept small for local models |
| `guardrails.py`, `rules.json` | Permission rules |
| `scheduler.py` | Scheduled tasks, watch jobs, hourly watchlist |
| `channels.py`, `webserver.py`, `telegram_bot.py` | Routing between the app and your phone |
| `tools.py`, `vault.py`, `voice.py` | Custom tools, Keychain logins, Whisper transcription |
| `web/` | Desktop app (React + Vite; `web/dist` is prebuilt) |
| `memory.example/` | Templates copied to `memory/` on first run |
| `~/.steward/` | Logs, chat history, browser profile, schedules (outside the repo) |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "This task is too big for the local model's memory" | Long browser tasks can outgrow a local model's context. Steward now summarises older steps and retries automatically. If it still fails, split the task, raise `OLLAMA_CONTEXT_LENGTH` (uses more RAM), or use a larger brain. |
| First reply is slow | Normal for local models while the model warms up (about 1 minute after start). Smaller model: `Configure Steward.command`. For complex tasks, use Claude. |
| "Reconnecting…" in the app | Steward isn't running. Double-click `Start Steward.command`, and check `~/.steward/stderr.log`. |
| Screenshots are blank, or clicks don't work | System Settings → Privacy & Security → allow **Screen Recording** and **Accessibility** for `.venv/bin/python` (and Terminal). |
| "Ollama isn't reachable" | Open the Ollama app or run `brew services start ollama`. |
| The Mac sleeps and Steward stops answering | Steward keeps the Mac awake while it's on, but closing the lid sleeps it (unless it's plugged into power and an external display). |
| Logs | `tail -f ~/.steward/agent.log` (passwords are redacted) |

Developers: `.venv/bin/python main.py --cli` chats in the terminal (stop the service first with `launchctl bootout gui/$(id -u)/com.steward.agent`). `cd web && npm install && npm run dev` gives hot reload for the UI.

---

## Limitations

- macOS only for now.
- It handles one task at a time; scheduled jobs wait for the current task to finish.
- Local models in the 8–12B range are good for short tasks but can get lost in long multi-page web flows. Use Claude for those.
- It can't solve CAPTCHAs, and it asks you for 2FA codes.

## Credits

- [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview): agent loop and tools. Use it under Anthropic's terms.
- [Ollama](https://ollama.com) (local and cloud models), [Playwright MCP](https://github.com/microsoft/playwright-mcp) (browser), [MLX Whisper](https://github.com/ml-explore/mlx-examples) (voice), [python-telegram-bot](https://python-telegram-bot.org).
- UI components from [libraries.dev](https://libraries.dev): bot-avatars, thinking-orbs, border-beam and voice-glow.

## License

[MIT](LICENSE) © Sai Teja Battula
