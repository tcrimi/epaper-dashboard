#!/bin/bash
# Compile and flash the board with the git version baked in. The firmware
# reports it as fw=<version> on every board.log line, e.g. v0.1.0-3-g454a435,
# with -dirty appended when the working tree has uncommitted changes.
#
#   ./flash.sh                # auto-detect the UNO R4 WiFi's port
#   ./flash.sh /dev/cu.usbmodem1101
#
# The Arduino IDE can't run git at build time, so IDE builds report
# fw=unversioned. Use this script for anything you want to identify later.
set -euo pipefail
cd "$(dirname "$0")"

FQBN="arduino:renesas_uno:unor4wifi"
SKETCH="epaper_dashboard"
VERSION_H="$SKETCH/src/version.h"

command -v arduino-cli >/dev/null || { echo "arduino-cli not found (brew install arduino-cli)"; exit 1; }

PORT="${1:-$(arduino-cli board list | awk '/unor4wifi/ { print $1; exit }')}"
if [ -z "$PORT" ]; then
    echo "no UNO R4 WiFi found; pass the port, e.g. ./flash.sh /dev/cu.usbmodem1101"
    exit 1
fi

VERSION="$(git describe --tags --always --dirty)"

# Generated for this build only, then removed, so a later IDE build can't
# pick up a stale version.
trap 'rm -f "$VERSION_H"' EXIT
printf '#define FW_VERSION "%s"\n' "$VERSION" > "$VERSION_H"

echo "flashing $VERSION to $PORT"
arduino-cli compile --fqbn "$FQBN" --upload --port "$PORT" "$SKETCH"
