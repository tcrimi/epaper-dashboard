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

# caffeinate -i keeps the machine from idle-sleeping so the R4's requests don't
# stall waiting for a wake — it exits automatically when the server process does.
FLASK_DEBUG=0 nohup caffeinate -i python3 app.py > server.log 2>&1 < /dev/null &
echo "started (pid $!), logging to server.log"
