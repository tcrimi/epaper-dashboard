#!/bin/bash
# Foreground entry point for launchd. Use start.sh for manual background use.
set -e
cd "$(dirname "$0")"

if [ ! -f .venv/bin/activate ]; then
    /opt/homebrew/bin/uv venv .venv --python 3.14
    /opt/homebrew/bin/uv pip install -r requirements.txt --python .venv/bin/python
fi

exec caffeinate -i .venv/bin/python3 app.py
