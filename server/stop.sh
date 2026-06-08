#!/bin/bash
# Stop the dashboard server by killing whatever is listening on its port.
cd "$(dirname "$0")"

# Match the port app.py uses: PORT from the environment or .env, else 5000.
PORT="${PORT:-$(grep -E '^\s*PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')}"
PORT="${PORT:-5000}"

pids=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN)
if [ -n "$pids" ]; then
    kill $pids && echo "stopped (port $PORT)"
else
    echo "nothing listening on port $PORT"
fi
