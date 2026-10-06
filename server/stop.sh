#!/bin/bash
# Stop the dashboard server. Uses launchctl when the service is loaded so
# launchd's KeepAlive doesn't immediately restart a process we just killed.
cd "$(dirname "$0")"

LABEL="com.tcrimi.epaper-dashboard"
PORT="${PORT:-$(grep -E '^\s*PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')}"
PORT="${PORT:-5000}"

if launchctl list "$LABEL" &>/dev/null 2>&1; then
    # launchctl kickstart -k stops the running instance; KeepAlive restarts it.
    # For a clean stop without restart use 'launchctl bootout gui/$(id -u) <plist>'.
    launchctl kill SIGTERM "gui/$(id -u)/$LABEL" 2>/dev/null
    echo "stopped ($LABEL)"
else
    # Not managed by launchd — kill by port. PORT is the relay (board-facing);
    # FLASK_PORT is app.py behind it.
    FLASK_PORT="${FLASK_PORT:-5012}"
    for p in "$PORT" "$FLASK_PORT"; do
        pids=$(lsof -tiTCP:"$p" -sTCP:LISTEN)
        if [ -n "$pids" ]; then
            kill $pids && echo "stopped (port $p)"
        else
            echo "nothing listening on port $p"
        fi
    done
fi
