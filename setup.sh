#!/bin/bash
# Claude Remote Bot — one-shot setup (venv + deps + LaunchAgent)
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="${BOT_LABEL:-com.claude.remotebot}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Creating venv + installing deps"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "==> Preparing .env"
[ -f .env ] || cp .env.example .env

echo "==> Writing LaunchAgent: $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/venv/bin/python</string>
        <string>$DIR/bot.py</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/claude-remote-bot.log</string>
    <key>StandardErrorPath</key><string>/tmp/claude-remote-bot.err</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/opt/node@20/bin:/opt/homebrew/opt/libpq/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
EOF

cat <<DONE

==> Done. Next:
  1) Edit .env  -> set TELEGRAM_BOT_TOKEN (and WORKDIR)
  2) Start the bot:   launchctl load -w "$PLIST"
  3) Message your bot on Telegram once -> it replies with your chat_id
  4) Put that id in .env (ALLOWED_CHAT_IDS), then restart:
        launchctl kickstart -k gui/\$(id -u)/$LABEL
  5) (optional) Menu bar app: ./venv/bin/python menubar.py
        or build a .app: ./venv/bin/pip install py2app && ./venv/bin/python setup_app.py py2app -A
DONE
