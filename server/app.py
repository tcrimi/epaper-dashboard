"""Flask app: browser preview + raw frame buffer for the R4."""
import os
from datetime import datetime, timedelta
from time import time

from flask import Flask, Response, render_template_string

import render
import sources

app = Flask(__name__)

# Hour of the day (server local time) when the dashboard should refresh.
DAILY_REFRESH_HOUR = int(os.environ.get("DAILY_REFRESH_HOUR", "5"))


def ms_until_next_refresh() -> int:
    """Milliseconds from now until the next DAILY_REFRESH_HOUR boundary."""
    now = datetime.now()
    target = now.replace(hour=DAILY_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int((target - now).total_seconds() * 1000)

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
    # In Flask debug mode the script is run twice: once as the reloader parent
    # and once as the worker (only the worker has WERKZEUG_RUN_MAIN set).
    # Warm the cache only in the worker so we don't pay for it twice.
    debug = True
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        sources.warmup()
    app.run(host="0.0.0.0", port=5002, debug=debug)
