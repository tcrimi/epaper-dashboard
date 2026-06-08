#!/bin/bash
# Start the dashboard server detached. Logs to server.log.
set -e
cd "$(dirname "$0")"

# Activate the local venv if it exists (created via: python3 -m venv .venv).
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

FLASK_DEBUG=0 nohup python3 app.py > server.log 2>&1 < /dev/null &
echo "started (pid $!), logging to server.log"
