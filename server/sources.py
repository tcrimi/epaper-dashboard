"""Data sources: NASA APOD, NWS weather, Wikipedia 'On this day'.

Each fetch returns a structured dict (or None on hard failure). A simple
in-memory TTL cache prevents hammering the upstream APIs.
"""
from __future__ import annotations

import io
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from PIL import Image

# Load server/.env regardless of CWD so `python3 app.py` works from any dir.
load_dotenv(Path(__file__).parent / ".env")

# Manhattan / Central Park. Change to your location.
LAT = 40.7831
LON = -73.9712
LOCATION_LABEL = "NYC"

USER_AGENT = "epaper-dashboard/0.1 (https://github.com/example/epaper-dashboard)"

# Grab one for free at https://api.nasa.gov — 1000 req/hr instead of DEMO_KEY's 30.
NASA_API_KEY = os.environ.get("NASA_API_KEY", "DEMO_KEY")

_HTTP_TIMEOUT = 30
_cache: dict[str, tuple[float, Any]] = {}
_refreshing: set[str] = set()
_refresh_lock = threading.Lock()


def _schedule_refresh(key: str, ttl_seconds: float, fn) -> None:
    """Refresh `key` in a daemon thread. Coalesces concurrent calls per key."""
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def refresh():
        try:
            value = fn()
            _cache[key] = (time.time(), value)
        except Exception as e:
            print(f"[sources] {key} background refresh failed: {e}")
            # Push expiry out by 60s so we don't hammer on a flapping upstream.
            if key in _cache:
                _, value = _cache[key]
                _cache[key] = (time.time() - ttl_seconds + 60, value)
        finally:
            with _refresh_lock:
                _refreshing.discard(key)

    threading.Thread(target=refresh, daemon=True).start()


def _cached(key: str, ttl_seconds: float, fn):
    """Stale-while-revalidate: expired entries return immediately and trigger a
    background refresh. Only a cold cache miss blocks the caller."""
    now = time.time()
    if key in _cache:
        ts, value = _cache[key]
        if now - ts < ttl_seconds:
            return value
        _schedule_refresh(key, ttl_seconds, fn)
        return value
    # Cold cache — must block.
    try:
        value = fn()
        _cache[key] = (now, value)
        return value
    except Exception as e:
        print(f"[sources] {key} fetch failed: {e}")
        return None


# ---------------------------- NASA APOD ----------------------------


def _fetch_apod_for(date_str: str | None) -> dict[str, Any]:
    params = {"api_key": NASA_API_KEY}
    if date_str:
        params["date"] = date_str
    r = requests.get(
        "https://api.nasa.gov/planetary/apod",
        params=params,
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    r.raise_for_status()
    return r.json()


def _fetch_apod() -> dict[str, Any] | None:
    """Return today's APOD; fall back to yesterday's if today is a video.

    Also pre-warms the image cache so the next render doesn't block on a fresh
    image download (APOD images can be tens of MB)."""
    today = _fetch_apod_for(None)
    apod: dict[str, Any] | None = today if today.get("media_type") == "image" else None
    if apod is None:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        older = _fetch_apod_for(yesterday)
        if older.get("media_type") == "image":
            apod = older

    if apod and apod.get("url"):
        try:
            download_image(apod["url"])
        except Exception as e:
            print(f"[sources] apod image prewarm failed: {e}")

    return apod


def get_apod() -> dict[str, Any] | None:
    # Date-keyed: a new UTC day forces a synchronous fetch rather than the
    # stale-while-revalidate path, which otherwise serves yesterday's image
    # for the entire morning.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _cached(f"apod:{today}", ttl_seconds=6 * 3600, fn=_fetch_apod)


def download_image(url: str) -> Image.Image | None:
    """Download an image URL and return as a PIL RGB image. Cached by URL."""
    def fetch():
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        return img

    return _cached(f"image:{url}", ttl_seconds=24 * 3600, fn=fetch)


# ---------------------------- NWS weather ----------------------------


def _nws_icon_key(icon_url: str | None) -> str | None:
    """Extract the icon name from an NWS icon URL.

    NWS URLs look like:
        https://api.weather.gov/icons/land/day/tsra_sct,40?size=medium
        https://api.weather.gov/icons/land/day/rain,50/snow?size=medium  (mixed)
    We take the primary (first) icon and strip the optional ",probability" tail."""
    if not icon_url:
        return None
    path = icon_url.split("?", 1)[0]
    parts = path.rstrip("/").split("/")
    idx = -1
    for tod in ("day", "night"):
        if tod in parts:
            idx = parts.index(tod)
            break
    if idx == -1 or idx + 1 >= len(parts):
        return None
    return parts[idx + 1].split(",", 1)[0]


def _nws_grid() -> dict[str, str]:
    """One-shot lookup of grid endpoint for our lat/lon."""
    r = requests.get(
        f"https://api.weather.gov/points/{LAT},{LON}",
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    )
    r.raise_for_status()
    props = r.json()["properties"]
    return {"forecast": props["forecast"]}


def _fetch_forecast() -> list[dict[str, Any]]:
    """Up to 7 days of daily forecast. Pairs each daytime period with the
    following nighttime period for the low temperature."""
    grid = _cached("nws_grid", ttl_seconds=30 * 24 * 3600, fn=_nws_grid)
    if not grid:
        return []
    r = requests.get(
        grid["forecast"],
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
    )
    r.raise_for_status()
    periods = r.json()["properties"]["periods"]

    today = datetime.now().date()
    days: list[dict[str, Any]] = []
    for i, p in enumerate(periods):
        if not p.get("isDaytime"):
            continue
        low: int | None = None
        for q in periods[i + 1:]:
            if not q.get("isDaytime"):
                low = q.get("temperature")
                break
        try:
            dt = datetime.fromisoformat(p["startTime"])
        except (KeyError, ValueError):
            continue
        days.append({
            "label": "TODAY" if dt.date() == today else dt.strftime("%a").upper(),
            "high": p.get("temperature"),
            "low": low,
            "short": p.get("shortForecast", ""),
            "icon_key": _nws_icon_key(p.get("icon")),
            "is_weekend": dt.weekday() >= 5,
        })
    return days


def get_forecast() -> list[dict[str, Any]]:
    cached = _cached("forecast", ttl_seconds=30 * 60, fn=_fetch_forecast)
    return cached or []


# ---------------------------- Wikipedia: On this day ----------------------------


def _fetch_on_this_day() -> list[dict[str, Any]]:
    today = datetime.now()
    mm = f"{today.month:02d}"
    dd = f"{today.day:02d}"
    r = requests.get(
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{mm}/{dd}",
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    r.raise_for_status()
    events = r.json().get("events", [])
    # Sort by year descending (more recent first — usually more recognizable).
    # Then take the three with shortest descriptions so they fit in the sidebar.
    events.sort(key=lambda e: -int(e.get("year", 0)))
    picked = sorted(events[:15], key=lambda e: len(e.get("text", "")))[:3]
    picked.sort(key=lambda e: -int(e.get("year", 0)))
    return [{"year": e["year"], "text": e["text"]} for e in picked]


def get_on_this_day() -> list[dict[str, Any]]:
    # Date-keyed for the same reason as get_apod — otherwise the 5am refresh
    # sees the previous day's events served stale from cache.
    today = datetime.now().strftime("%m-%d")
    cached = _cached(f"otd:{today}", ttl_seconds=6 * 3600, fn=_fetch_on_this_day)
    return cached or []


# ---------------------------- Startup ----------------------------


def warmup() -> None:
    """Synchronously populate every cache so the first HTTP request is fast.

    Each fetcher swallows its own errors — a warmup failure just means the
    relevant section will render as '—' until the next background refresh."""
    print("[sources] warmup: apod...", flush=True)
    get_apod()
    print("[sources] warmup: forecast...", flush=True)
    get_forecast()
    print("[sources] warmup: on-this-day...", flush=True)
    get_on_this_day()
    print("[sources] warmup complete", flush=True)
