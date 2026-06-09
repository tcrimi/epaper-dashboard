"""Data sources: NASA APOD, NWS weather, Wikipedia 'On this day'.

Each fetch returns a structured dict (or None on hard failure). A simple
in-memory TTL cache prevents hammering the upstream APIs.
"""
from __future__ import annotations

import io
import math
import os
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

# APOD rolls over around midnight US Eastern, so key the cache on the Eastern
# date rather than UTC. Keying on UTC rolls the day over at ~8pm ET — hours
# before the new image is published and before the morning board refresh — so
# the first fetch of the "new" day grabs yesterday's image and caches it.
APOD_TZ = ZoneInfo("America/New_York")

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
        yesterday = (datetime.now(APOD_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
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
    # Date-keyed on US Eastern (see APOD_TZ): a new day forces a synchronous
    # fetch rather than the stale-while-revalidate path, which otherwise serves
    # yesterday's image for the entire morning. Eastern keeps the rollover
    # aligned with APOD's publish time and the morning board refresh.
    today = datetime.now(APOD_TZ).strftime("%Y-%m-%d")
    return _cached(f"apod:{today}", ttl_seconds=6 * 3600, fn=_fetch_apod)


def download_image(url: str) -> Image.Image | None:
    """Download an image URL and return as a PIL RGB image. Cached by URL."""
    def fetch():
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        return img

    return _cached(f"image:{url}", ttl_seconds=24 * 3600, fn=fetch)


# ---------------------------- Museum art ----------------------------

# Public-domain artworks rotate the hero between the daily APOD wakes so the
# extra intraday refreshes show something new. Saturated, high-contrast paintings
# make far better use of the Spectra 6 palette than the dark space photos APOD
# often runs. The Art Institute and the Met need no key; Rijksmuseum joins the
# pool only when RIJKSMUSEUM_KEY is set.
_ART_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

_AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
_AIC_IIIF = "https://www.artic.edu/iiif/2"
# Elasticsearch caps `from + size` at 10000, so with 100 results/page only the
# first 100 pages are reachable — plenty against ~60k public-domain works.
_AIC_MAX_PAGE = 100

# The Met's European Paintings department — a curated, high-color subset.
_MET_DEPT = 11
_MET_OBJECTS = "https://collectionapi.metmuseum.org/public/collection/v1/objects"
_MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"

_RIJKS_COLLECTION = "https://www.rijksmuseum.nl/api/en/collection"
_RIJKS_MAX_PAGE = 100  # ps=100, and p*ps must stay <= 10000


def _art_payload(title: str | None, artist: str | None, url: str) -> dict[str, Any]:
    """Shape a hero payload like the APOD dict (title/artist/url) and prewarm the
    image so the next render doesn't block on the download."""
    try:
        download_image(url)
    except Exception as e:
        print(f"[sources] art image prewarm failed: {e}")
    return {"title": title or "Untitled", "artist": artist or None, "url": url}


def _fetch_aic_art() -> dict[str, Any] | None:
    """Random public-domain artwork from the Art Institute of Chicago (no key)."""
    params = {
        "query[term][is_public_domain]": "true",
        "fields": "id,title,image_id,artist_title",
        "limit": 100,
    }
    # Retry across a few random pages: a page may be light on image_ids, and AIC's
    # edge occasionally 403s a burst request. Tolerate a per-attempt failure.
    iiif = _AIC_IIIF
    for attempt in range(3):
        params["page"] = random.randint(1, _AIC_MAX_PAGE)
        try:
            r = requests.get(_AIC_SEARCH, params=params, timeout=_HTTP_TIMEOUT, headers=_ART_HEADERS)
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"[sources] AIC search attempt {attempt + 1} failed: {e}")
            time.sleep(1)
            continue
        iiif = body.get("config", {}).get("iiif_url") or iiif
        with_images = [a for a in body.get("data", []) if a.get("image_id")]
        if with_images:
            art = random.choice(with_images)
            # 843px is AIC's recommended full-screen width — just over our panel.
            url = f"{iiif}/{art['image_id']}/full/843,/0/default.jpg"
            return _art_payload(art.get("title"), art.get("artist_title"), url)
    return None


def _met_object_ids() -> list[int]:
    """Object IDs in the Met's European Paintings dept, cached for a week."""
    def fetch() -> list[int]:
        r = requests.get(_MET_OBJECTS, params={"departmentIds": _MET_DEPT},
                         timeout=_HTTP_TIMEOUT, headers=_ART_HEADERS)
        r.raise_for_status()
        return r.json().get("objectIDs") or []
    return _cached("met_object_ids", ttl_seconds=7 * 24 * 3600, fn=fetch) or []


def _fetch_met_art() -> dict[str, Any] | None:
    """Random public-domain painting from the Metropolitan Museum (no key)."""
    ids = _met_object_ids()
    if not ids:
        return None
    # Not every object is public-domain or has an image, so retry a few IDs.
    for attempt in range(4):
        try:
            r = requests.get(_MET_OBJECT.format(random.choice(ids)),
                             timeout=_HTTP_TIMEOUT, headers=_ART_HEADERS)
            r.raise_for_status()
            obj = r.json()
        except Exception as e:
            print(f"[sources] Met object attempt {attempt + 1} failed: {e}")
            time.sleep(1)
            continue
        url = obj.get("primaryImageSmall") or obj.get("primaryImage")
        if obj.get("isPublicDomain") and url:
            return _art_payload(obj.get("title"), obj.get("artistDisplayName"), url)
    return None


def _fetch_rijks_art() -> dict[str, Any] | None:
    """Random artwork from the Rijksmuseum. Requires RIJKSMUSEUM_KEY (free, from
    rijksmuseum.nl/en/rijksstudio/my/api); returns None when no key is set."""
    key = os.environ.get("RIJKSMUSEUM_KEY")
    if not key:
        return None
    params = {
        "key": key,
        "imgonly": "true",
        "ps": 100,
        "p": random.randint(1, _RIJKS_MAX_PAGE),
    }
    try:
        r = requests.get(_RIJKS_COLLECTION, params=params, timeout=_HTTP_TIMEOUT, headers=_ART_HEADERS)
        r.raise_for_status()
        objs = [o for o in r.json().get("artObjects", []) if o.get("webImage")]
    except Exception as e:
        print(f"[sources] Rijksmuseum fetch failed: {e}")
        return None
    if not objs:
        return None
    obj = random.choice(objs)
    return _art_payload(obj.get("title"), obj.get("principalOrFirstMaker"), obj["webImage"]["url"])


# No-key providers are always in the pool; Rijksmuseum self-skips without a key.
_ART_PROVIDERS = [_fetch_aic_art, _fetch_met_art, _fetch_rijks_art]


def _fetch_museum_art() -> dict[str, Any] | None:
    """A random public-domain artwork from one of the available museum sources.
    Tries providers in random order, so one source being down (or unkeyed) just
    falls through to the next; returns None only if every provider fails."""
    providers = _ART_PROVIDERS[:]
    random.shuffle(providers)
    for provider in providers:
        try:
            art = provider()
        except Exception as e:
            print(f"[sources] {provider.__name__} failed: {e}")
            continue
        if art:
            return art
    return None


# ---------------------------- Hero rotation ----------------------------

# Hour (Eastern) from which the hero switches from the day's APOD to rotating
# museum art, so midday/evening wakes aren't a re-run of the morning picture.
ART_FROM_HOUR = int(os.environ.get("ART_FROM_HOUR", "11"))

# The hero is chosen ahead of each scheduled wake by refresh_hero() (see the
# prewarmer in app.py) and published here, so the board's frame request reads a
# ready result instead of blocking on a fetch — and any AIC retry/fallback
# happens on our schedule, with slack, well before the board ever pings.
_hero_lock = threading.Lock()
_current_hero: dict[str, Any] | None = None


def select_hero(hour: int) -> dict[str, Any] | None:
    """Pick the hero for a given wake hour: the day's APOD in the morning,
    rotating museum art from ART_FROM_HOUR on. Falls back to APOD if art can't be
    fetched, so the board always has a hero. Both paths prewarm the image."""
    if hour >= ART_FROM_HOUR:
        art = _fetch_museum_art()
        if art:
            return art
    return get_apod()  # get_apod prewarms its own image


def refresh_hero(hour: int) -> dict[str, Any] | None:
    """Select the hero for `hour` and publish it as the current hero. Called
    ahead of each scheduled wake (and at startup). Keeps the previously
    published hero if selection fails entirely."""
    global _current_hero
    hero = select_hero(hour)
    if hero is not None:
        with _hero_lock:
            _current_hero = hero
    return hero


def get_hero() -> dict[str, Any] | None:
    """The hero the board should display now — whatever the prewarmer last
    published. Selects synchronously only on a cold start."""
    with _hero_lock:
        hero = _current_hero
    if hero is not None:
        return hero
    return refresh_hero(datetime.now(APOD_TZ).hour)


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


# ---------------------------- Sun & moon (offline) ----------------------------

# Sunrise/sunset and moon phase for the board location, computed locally with
# `astral` — no API or key. Uses APOD_TZ since the default location is NYC; if you
# relocate the board, change LAT/LON and APOD_TZ together.
def _moon_name(phase: float) -> str:
    # astral.moon.phase: 0=new, 7=first quarter, 14=full, 21=last quarter (0..28).
    if phase < 1 or phase >= 27:
        return "New Moon"
    if phase < 6.5:
        return "Waxing Crescent"
    if phase < 8:
        return "First Quarter"
    if phase < 13.5:
        return "Waxing Gibbous"
    if phase < 15:
        return "Full Moon"
    if phase < 20.5:
        return "Waning Gibbous"
    if phase < 22:
        return "Last Quarter"
    return "Waning Crescent"


def get_sun_moon() -> dict[str, Any] | None:
    """Sunrise, sunset, day length, and moon phase for the board location.

    Computed offline, so no caching is needed. Returns None at extreme latitudes
    where the sun doesn't rise or set on the given day."""
    from astral import Observer, moon
    from astral.sun import sunrise, sunset
    try:
        obs = Observer(latitude=LAT, longitude=LON)
        today = datetime.now(APOD_TZ).date()
        sr = sunrise(obs, date=today, tzinfo=APOD_TZ)
        ss = sunset(obs, date=today, tzinfo=APOD_TZ)
        phase = moon.phase(today)
    except Exception as e:
        print(f"[sources] sun/moon failed: {e}")
        return None
    minutes = max(0, int((ss - sr).total_seconds()) // 60)
    illum = round((1 - math.cos(2 * math.pi * phase / 28)) / 2 * 100)
    return {
        "sunrise": sr,
        "sunset": ss,
        "day_length": f"{minutes // 60}h{minutes % 60:02d}m",
        "moon_name": _moon_name(phase),
        "moon_illum": illum,
        # The 28 weather-icons moon glyphs (f095..f0b0) map 1:1 onto astral phases.
        "moon_glyph_index": int(phase) % 28,
    }


# ---------------------------- Startup ----------------------------


def warmup() -> None:
    """Synchronously populate every cache so the first HTTP request is fast.

    Each fetcher swallows its own errors — a warmup failure just means the
    relevant section will render as '—' until the next background refresh."""
    print("[sources] warmup: hero...", flush=True)
    refresh_hero(datetime.now(APOD_TZ).hour)
    print("[sources] warmup: forecast...", flush=True)
    get_forecast()
    print("[sources] warmup: on-this-day...", flush=True)
    get_on_this_day()
    print("[sources] warmup complete", flush=True)
