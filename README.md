# Claude Remote Bot

Drive **Claude Code** from **Telegram**, with a playful **macOS menu bar** companion.

- Send a message on Telegram → Claude Code works on it in your project dir (memory kept across messages).
- `/remote` launches an interactive **remote-control** session you steer from the **Claude mobile/web app** (runs locally in `tmux`, no GUI needed).
- Menu bar app: a rolling-robot icon (RunCat-style) showing live session count, with pause/resume, "stay awake while running", and per-session stop.

> macOS only. Uses your existing **Claude Code subscription** (no API key). Everything runs **locally on your Mac**.

---

## Requirements

- macOS (Apple Silicon paths assumed; tweak `PATH_PREFIX` for Intel)
- [Claude Code](https://docs.claude.com/claude-code) installed and logged in (`claude`), on a Pro/Max/Team/Enterprise plan
- [Homebrew](https://brew.sh), Python 3.11+, and `tmux`:
  ```bash
  brew install tmux
  # (menu bar icons regeneration only) brew install librsvg
  ```

---

## Setup (step by step)

### 1. Get the code & install
```bash
git clone <YOUR_REPO_URL> claude-telegram-bot
cd claude-telegram-bot
./setup.sh          # creates venv, installs deps, writes the LaunchAgent
```

### 2. Create your Telegram bot
1. In Telegram, open **[@BotFather](https://t.me/BotFather)** → send `/newbot`.
2. Pick a name and a username ending in `bot`.
3. Copy the **token** it gives you (looks like `1234567890:AA...`).

### 3. Configure `.env`
```bash
# edit .env
TELEGRAM_BOT_TOKEN=<paste your token>
WORKDIR=~/Developer        # the folder Claude works in
```

### 4. Start the bot & find your chat id
```bash
launchctl load -w ~/Library/LaunchAgents/com.claude.remotebot.plist
```
Now message your bot anything in Telegram. With `ALLOWED_CHAT_IDS` still empty, it replies with **your chat_id**.

### 5. Lock it to you
Put that id in `.env`:
```bash
ALLOWED_CHAT_IDS=<your chat_id>
```
Restart the bot:
```bash
launchctl kickstart -k gui/$(id -u)/com.claude.remotebot
```
Done — only your Telegram account can command it now.

### 6. (Optional) Menu bar app
Run it directly:
```bash
./venv/bin/python menubar.py
```
…or build a real `.app` (shows up in Spotlight, launch at login):
```bash
./venv/bin/pip install py2app
./venv/bin/python setup_app.py py2app -A
cp -R "dist/Claude Remote Bot.app" ~/Applications/
open ~/Applications/"Claude Remote Bot.app"
```
On first launch it offers to set up **launch-at-login** and the **stay-awake** permission (a one-time `pmset` sudoers grant via the native admin prompt).

---

## Telegram commands

| Command | What it does |
|---|---|
| *(any message)* | Claude works on it; session memory is kept |
| `/remote [repo] [auto\|full] [label…]` | Start a remote-control session (parallel sessions OK). e.g. `/remote my-repo auto fix login bug` |
| `/remotes` | List sessions + tap a button to stop one |
| `/killremote <number\|name>` | Stop a specific session |
| `/pause` `/resume` | Stop / resume accepting commands |
| `/session` `/new` | Show / reset the conversation session |
| `/whoami` | Your chat_id and access |
| `/how` `/help` | Command list |

When Claude needs you to choose between options, it sends **inline buttons** (single or multi-select).

---

## Permission modes

`PERMISSION_MODE` (headless tasks) and `REMOTE_PERMISSION_MODE` (`/remote`):

- `acceptEdits` — auto-accept file edits, still asks for risky commands *(default)*
- `auto` — autonomous, but a background safety classifier blocks dangerous actions
- `bypassPermissions` — no checks *(use only on trusted work)*

In `/remote`, append `auto` or `full` (= bypass) to override per session.

---

## Security notes

- Only chat ids in `ALLOWED_CHAT_IDS` can command the bot. Keep your **bot token** secret (it lives in `.env`, which is git-ignored).
- The bot can edit files and run dev commands in `WORKDIR`. Scope `WORKDIR` to what you're comfortable with and prefer `acceptEdits`/`auto` over `bypassPermissions`.
- Anyone with physical access to an unlocked, logged-in Mac running this can use it. On a work machine, confirm this is allowed by policy.

---

## How it works

- `bot.py` — Telegram bot. Each message runs `claude -p … --resume <session>` (streaming JSON), so memory persists. `/remote` spawns `claude --remote-control` in a `tmux` session.
- `menubar.py` — `rumps` menu bar app. Manages the bot via its LaunchAgent, ties "stay awake" (`pmset disablesleep`) to the running state, and animates the icon.
- `gen_icons.py` — regenerate the icon PNGs (needs `librsvg`).
- Runtime state (`sessions.json`, `remotes.json`, `paused`, …) is git-ignored.

---

🤖 Built with [Claude Code](https://claude.com/claude-code).
