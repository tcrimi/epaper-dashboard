"""Flask app: browser preview + raw frame buffer for the R4."""
import logging
import os
import sys
import threading
from datetime import datetime, timedelta
from time import sleep, time

from flask import Flask, Response, render_template_string

# Suppress the "Bad file descriptor" noise logged when the R4 drops the TCP
# connection mid-request — EBADF on a client socket is harmless.
logging.getLogger("werkzeug").addFilter(
    lambda r: "Bad file descriptor" not in r.getMessage()
)

import render
import sources

app = Flask(__name__)

# Hours of the day (server local time) when the dashboard should refresh.
# Comma-separated, e.g. REFRESH_HOURS=5,12,18. Of the three data sources only
# the NWS forecast changes intraday (APOD and "on this day" are fixed per date),
# so a few set times keeps the weather current without flashing the panel all
# day. DAILY_REFRESH_HOUR is still honored as a single-time fallback.
def _refresh_hours() -> list[int]:
    raw = os.environ.get("REFRESH_HOURS") or os.environ.get("DAILY_REFRESH_HOUR", "5,12,18")
    hours = sorted({int(h) for h in raw.split(",") if h.strip() != ""})
    return [h for h in hours if 0 <= h <= 23] or [5]


REFRESH_HOURS = _refresh_hours()

# Port the server listens on. Defaults to 5000, but macOS hands that port to the
# AirPlay Receiver, so set PORT (e.g. 5002) in server/.env on a Mac.
PORT = int(os.environ.get("PORT", "5000"))

# How many minutes before a scheduled wake to pick + fetch the next hero, so any
# AIC retry/APOD fallback finishes with slack before the board requests a frame.
PREWARM_LEAD_MIN = int(os.environ.get("PREWARM_LEAD_MIN", "15"))


def _next_refresh(now: datetime) -> datetime:
    """The soonest upcoming REFRESH_HOURS boundary at or after `now`."""
    candidates = [
        now.replace(hour=h, minute=0, second=0, microsecond=0) for h in REFRESH_HOURS
    ]
    # Roll any boundary already passed today into tomorrow, then take the soonest.
    candidates = [t + timedelta(days=1) if t <= now else t for t in candidates]
    return min(candidates)


def ms_until_next_refresh() -> int:
    """Milliseconds from now until the next REFRESH_HOURS boundary."""
    now = datetime.now()
    return int((_next_refresh(now) - now).total_seconds() * 1000)


def _prewarm_loop() -> None:
    """Pick and fetch the hero for each upcoming wake PREWARM_LEAD_MIN minutes
    ahead, so the board reads a ready image and never waits on a fetch."""
    while True:
        now = datetime.now()
        target = _next_refresh(now)
        wait = (target - timedelta(minutes=PREWARM_LEAD_MIN) - now).total_seconds()
        if wait > 0:
            sleep(wait)
        try:
            hero = sources.refresh_hero(target.hour)
            print(f"[prewarm] hero for {target:%H:%M}: {hero and hero.get('title')}", flush=True)
        except Exception as e:
            print(f"[prewarm] failed: {e}", flush=True)
        # Sleep past the boundary so the next pass computes the following slot.
        sleep(max(60.0, (target - datetime.now()).total_seconds() + 30))

INDEX_HTML = """<!doctype html>
<html>
<head>
  <title>e-Paper Dashboard preview</title>
  <style>
    body { background:#1a1a1a; color:#eee; font-family:system-ui, sans-serif;
           text-align:center; margin:0; padding:24px; }
    h2 { font-weight:400; margin:0 0 12px; }
    .frame { display:inline-block; padding:12px; background:#333;
             border-radius:8px; box-shadow:0 4px 24px rgba(0,0,0,0.5); }
    img { display:block; max-width:90vw; height:auto; image-rendering:pixelated;
          background:white; }
    .meta { margin-top:16px; color:#888; font-size:13px; }
    .meta a { color:#9bf; text-decoration:none; }
    button { background:#2d6cdf; color:white; border:none; padding:8px 16px;
             border-radius:4px; cursor:pointer; font-size:14px; margin-top:16px; }
    button:hover { background:#3a7be8; }
  </style>
</head>
<body>
  <h2>e-Paper Dashboard preview</h2>
  <div class="frame">
    <img id="preview" src="/preview.png?t={{ t }}" width="800" height="480">
  </div>
  <div>
    <button onclick="location.reload()">Re-render</button>
  </div>
  <p class="meta">
    Quantized to Spectra 6 with Floyd-Steinberg dither &middot;
    <a href="/frame.bin">/frame.bin</a> (192000 bytes, 4bpp) is what the R4 fetches
  </p>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML, t=int(time()))


@app.route("/preview.png")
def preview():
    return Response(render.render_preview_png(), mimetype="image/png")


@app.route("/frame.bin")
def frame():
    buf = render.render_frame_bin()
    resp = Response(buf, mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(len(buf))
    # Tell the board when to come back for the next frame.
    resp.headers["X-Next-Refresh-Ms"] = str(ms_until_next_refresh())
    return resp


if __name__ == "__main__":
    # Debug mode runs Werkzeug's auto-reloader, which tries to manipulate the
    # controlling terminal — that fails (termios.error) when we have no TTY
    # (e.g., running under nohup). Default to interactive=on, but force it off
    # when stdin isn't a tty, or when FLASK_DEBUG=0 is set explicitly.
    debug = sys.stdin.isatty() and os.environ.get("FLASK_DEBUG", "1") != "0"

    # In Flask debug mode the script is run twice: once as the reloader parent
    # and once as the worker (only the worker has WERKZEUG_RUN_MAIN set).
    # Warm the cache only in the worker so we don't pay for it twice.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        sources.warmup()
        threading.Thread(target=_prewarm_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=debug)
