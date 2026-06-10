#!/bin/bash
# Start the dashboard server detached. Logs to server.log.
set -e
cd "$(dirname "$0")"

# Bootstrap venv with uv if not present, then activate.
if [ ! -f .venv/bin/activate ]; then
    echo "creating venv..."
    uv venv .venv
    uv pip install -r requirements.txt --python .venv/bin/python
fi
source .venv/bin/activate

FLASK_DEBUG=0 nohup python3 app.py > server.log 2>&1 < /dev/null &
echo "started (pid $!), logging to server.log"
