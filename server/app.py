"""Flask app: browser preview + raw frame buffer for the R4."""
import errno
import hashlib
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from time import sleep, time

from flask import Flask, Response, render_template_string, request
from werkzeug.serving import BaseWSGIServer

# Suppress the EBADF traceback spam from connections torn down before their
# request line arrives. Werkzeug's handler catches only ConnectionError and
# socket.timeout, so OSError(EBADF) escapes to socketserver's handle_error --
# which prints straight to stderr, where no logging filter can reach it.
_orig_handle_error = BaseWSGIServer.handle_error


def _quiet_handle_error(self, request, client_address):
    exc = sys.exc_info()[1]
    if isinstance(exc, OSError) and exc.errno == errno.EBADF:
        return
    _orig_handle_error(self, request, client_address)


BaseWSGIServer.handle_error = _quiet_handle_error

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
    """Milliseconds from now until the next REFRESH_HOURS boundary.

    The board's clock drifts, so it can arrive a little before a boundary. Skip
    any boundary within a couple of minutes — otherwise we'd answer a few
    seconds, which the board rejects (60 s floor) and keeps its stale delay.
    """
    now = datetime.now()
    return int((_next_refresh(now + timedelta(minutes=2)) - now).total_seconds() * 1000)


# macOS doesn't count serving a request as activity. The board's Wake-on-LAN
# only buys ~10s of DarkWake, and a paced frame takes ~25s, so without a power
# assertion the Mac sleeps mid-download (sshd holds one for its sessions, which
# is why those keep it up). Each request starts or extends a time-limited
# caffeinate: -s holds system sleep off on AC power, DarkWake included; -i
# covers idle sleep. The timeout means a hold can never outlive its work by much.
AWAKE_HOLD_S = int(os.environ.get("AWAKE_HOLD_S", "120"))
_awake_until = 0.0
_awake_lock = threading.Lock()


def hold_awake(seconds: int = AWAKE_HOLD_S) -> None:
    global _awake_until
    if sys.platform != "darwin":
        return
    with _awake_lock:
        now = time()
        # A running hold still covers most of the window: leave it be.
        if _awake_until - now > seconds / 2:
            return
        try:
            subprocess.Popen(["caffeinate", "-s", "-i", "-t", str(seconds)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _awake_until = now + seconds
        except OSError as e:
            print(f"[awake] caffeinate failed: {e}", flush=True)


@app.before_request
def _hold_awake_for_request():
    hold_awake()


def _prewarm_loop() -> None:
    """Pick and fetch the hero for each upcoming wake PREWARM_LEAD_MIN minutes
    ahead, so the board reads a ready image and never waits on a fetch."""
    while True:
        now = datetime.now()
        target = _next_refresh(now)
        wait = (target - timedelta(minutes=PREWARM_LEAD_MIN) - now).total_seconds()
        if wait > 0:
            sleep(wait)
        # Only runs if the Mac is awake at this point, but once running, don't
        # let it fall asleep halfway through the fetches.
        hold_awake(300)
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
    <a href="/frame.bin">/frame.bin</a> (192000 bytes, 4bpp) is what the R4 fetches &middot;
    <a href="/log">board log</a>
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


# Failure reports POSTed by the board, which usually has no serial console.
BOARD_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.log")


@app.route("/log", methods=["POST"])
def board_log():
    msg = " ".join(request.get_data(as_text=True)[:1000].split())
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(f"[board] {line}", flush=True)
    with open(BOARD_LOG, "a") as fh:
        fh.write(line + "\n")
    return "", 204


@app.route("/log", methods=["GET"])
def board_log_view():
    try:
        with open(BOARD_LOG) as fh:
            lines = fh.readlines()[-200:]
    except FileNotFoundError:
        lines = ["(no reports yet)\n"]
    return Response("".join(reversed(lines)), mimetype="text/plain")


# The R4 drains the socket through its ESP32 WiFi bridge over a 115200-baud
# UART, so it can't take more than ~11 KB/s. Sent unpaced, the whole frame
# piles up in the bridge, and downloads stall partway through. Pace the body
# just under the drain rate. FRAME_SEND_BPS=0 sends unpaced.
FRAME_SEND_BPS = int(os.environ.get("FRAME_SEND_BPS", "8192"))
FRAME_CHUNK = 1024

# The last frame served, so a board resuming a stalled download with
# Range + If-Range gets the rest of the same bytes even if a fresh render
# would differ.
_served = {"etag": None, "buf": b""}
_served_lock = threading.Lock()


def _paced(buf: bytes):
    delay = FRAME_CHUNK / FRAME_SEND_BPS if FRAME_SEND_BPS > 0 else 0
    for i in range(0, len(buf), FRAME_CHUNK):
        yield buf[i:i + FRAME_CHUNK]
        if delay:
            sleep(delay)


@app.route("/frame.bin")
def frame():
    status, start = 200, 0
    with _served_lock:
        resuming = (request.range is not None and request.if_range.etag is not None
                    and request.if_range.etag == _served["etag"])
        if resuming:
            buf, etag = _served["buf"], _served["etag"]
    if resuming:
        span = request.range.range_for_length(len(buf))
        if span is not None:
            status, start = 206, span[0]
    else:
        buf = render.render_frame_bin()
        etag = hashlib.sha1(buf).hexdigest()[:16]
        with _served_lock:
            _served["etag"], _served["buf"] = etag, buf

    body = buf[start:]
    resp = Response(_paced(body), status=status, mimetype="application/octet-stream")
    # An explicit length keeps Werkzeug from chunk-encoding the generator,
    # which the firmware doesn't parse.
    resp.headers["Content-Length"] = str(len(body))
    resp.headers["Accept-Ranges"] = "bytes"
    resp.set_etag(etag)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{len(buf) - 1}/{len(buf)}"
        print(f"[frame] resuming at {start}/{len(buf)}", flush=True)
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
    app.run(host="0.0.0.0", port=PORT, debug=debug, threaded=True)
