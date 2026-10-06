#!/bin/bash
# Start the dashboard server. Uses launchctl when the LaunchAgent is installed
# (so launchd owns the process lifecycle); otherwise starts a detached background
# process directly (useful on machines without the LaunchAgent).
set -e
cd "$(dirname "$0")"

LABEL="com.tcrimi.epaper-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST" ] && launchctl list "$LABEL" &>/dev/null 2>&1; then
    launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "started ($LABEL via launchctl)"
    exit 0
fi

# Fallback: no LaunchAgent — start a detached process.
if [ ! -f .venv/bin/activate ]; then
    echo "creating venv..."
    uv venv .venv --python 3.14
    uv pip install -r requirements.txt --python .venv/bin/python
fi
source .venv/bin/activate

# The macOS app firewall blocks inbound connections to the Homebrew Python that
# runs app.py, so the board can't reach Flask directly. relay.py takes over
# PORT -- the port the board already targets -- and forwards to Flask on
# FLASK_PORT over loopback, which the firewall doesn't filter. That keeps the
# firmware pointed at the same port it always was.
PORT="${PORT:-$(grep -E '^\s*PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')}"
PORT="${PORT:-5000}"
FLASK_PORT="${FLASK_PORT:-5012}"

# PORT is set inline so it wins over .env: python-dotenv does not override
# variables already present in the environment.
PORT="$FLASK_PORT" FLASK_DEBUG=0 nohup python3 app.py > server.log 2>&1 < /dev/null &
echo "started (pid $!) on internal port $FLASK_PORT, logging to server.log"

# /usr/bin/python3 is allowlisted by the firewall; use that absolute path,
# because the venv's python3 on PATH is the blocked interpreter.
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "relay already running on port $PORT"
else
    FLASK_PORT="$FLASK_PORT" nohup /usr/bin/python3 relay.py > relay.log 2>&1 < /dev/null &
    echo "relay started (pid $!) on port $PORT, logging to relay.log"
fi
