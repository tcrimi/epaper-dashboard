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
    # Not managed by launchd — kill by port + caffeinate wrapper.
    pids=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN)
    cafe_pids=$(pgrep -f "caffeinate.*app.py" 2>/dev/null)
    all_pids=$(echo "$pids $cafe_pids" | tr ' ' '\n' | sort -u | tr '\n' ' ')
    if [ -n "$(echo $all_pids | tr -d ' ')" ]; then
        kill $all_pids 2>/dev/null && echo "stopped (port $PORT)"
    else
        echo "nothing listening on port $PORT"
    fi
fi
