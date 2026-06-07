"""Render the dashboard image and quantize it to the Waveshare 7.3" Spectra 6 palette."""
from datetime import datetime
from pathlib import Path
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

import sources

WIDTH = 800
HEIGHT = 480

# Layout regions (x0, y0, x1, y1).
HEADER = (0, 0, WIDTH, 50)
IMAGE_BOX = (0, 50, 500, 380)
SIDEBAR = (500, 50, WIDTH, 380)
FORECAST = (0, 380, WIDTH, HEIGHT)

# Spectra 6 4-bit codes the panel expects on the wire.
BLACK_CODE = 0x0
WHITE_CODE = 0x1
YELLOW_CODE = 0x2
RED_CODE = 0x3
BLUE_CODE = 0x5
GREEN_CODE = 0x6

# RGB approximations of the actual pigment colors — tune by eye against the panel.
PALETTE_RGB = [
    (0,   0,   0),    # BLACK
    (255, 255, 255),  # WHITE
    (255, 243, 56),   # YELLOW
    (191, 0,   0),    # RED
    (60,  90,  200),  # BLUE
    (67,  138, 28),   # GREEN
]
INDEX_TO_CODE = [BLACK_CODE, WHITE_CODE, YELLOW_CODE, RED_CODE, BLUE_CODE, GREEN_CODE]

# Named colors for drawing (must be values that exist in the palette so they
# survive quantization cleanly).
BLACK = PALETTE_RGB[0]
WHITE = PALETTE_RGB[1]
YELLOW = PALETTE_RGB[2]
RED = PALETTE_RGB[3]
BLUE = PALETTE_RGB[4]
GREEN = PALETTE_RGB[5]


def _build_palette_image() -> Image.Image:
    pal = Image.new("P", (1, 1))
    flat: list[int] = []
    for rgb in PALETTE_RGB:
        flat.extend(rgb)
    flat.extend([0] * (768 - len(flat)))
    pal.putpalette(flat)
    return pal


_PAL_IMG = _build_palette_image()

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


_WI_FONT_PATH = Path(__file__).parent / "fonts" / "weathericons-regular-webfont.ttf"
_wi_font_cache: dict[int, ImageFont.ImageFont] = {}


def _wi_font(size: int) -> ImageFont.ImageFont:
    f = _wi_font_cache.get(size)
    if f is None:
        f = ImageFont.truetype(str(_WI_FONT_PATH), size)
        _wi_font_cache[size] = f
    return f


# NWS icon key (the "tsra_sct" part of the icon URL) -> (weather-icons glyph, color).
# Codepoints from erikflowers/weather-icons CSS.
WEATHER_ICONS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "skc":             ("", YELLOW),  # wi-day-sunny
    "few":             ("", YELLOW),
    "sct":             ("", YELLOW),  # wi-day-cloudy
    "bkn":             ("", BLACK),   # wi-day-cloudy-high
    "ovc":             ("", BLACK),   # wi-cloudy
    "wind_skc":        ("", YELLOW),  # wi-day-windy
    "wind_few":        ("", YELLOW),
    "wind_sct":        ("", YELLOW),
    "wind_bkn":        ("", BLACK),   # wi-strong-wind
    "wind_ovc":        ("", BLACK),
    "rain":            ("", BLUE),    # wi-rain
    "rain_showers":    ("", BLUE),    # wi-showers
    "rain_showers_hi": ("", BLUE),
    "tsra":            ("", RED),     # wi-thunderstorm
    "tsra_sct":        ("", RED),
    "tsra_hi":         ("", RED),
    "snow":            ("", BLUE),    # wi-snow
    "sleet":           ("", BLUE),    # wi-sleet
    "snow_sleet":      ("", BLUE),
    "snow_fzra":       ("", BLUE),    # wi-rain-mix
    "rain_snow":       ("", BLUE),
    "rain_sleet":      ("", BLUE),
    "rain_fzra":       ("", BLUE),
    "fzra":            ("", BLUE),
    "fog":             ("", BLACK),   # wi-fog
    "dust":            ("", BLACK),   # wi-dust
    "smoke":           ("", BLACK),   # wi-smoke
    "haze":            ("", BLACK),   # wi-day-haze
    "hot":             ("", RED),     # wi-hot
    "cold":            ("", BLUE),    # wi-snowflake-cold
    "blizzard":        ("", BLUE),    # wi-snow-wind
    "tornado":         ("", RED),     # wi-tornado
    "hurricane":       ("", RED),     # wi-hurricane
    "tropical_storm":  ("", BLUE),    # wi-storm-showers
}  # type: ignore[no-redef]
_WI_FALLBACK = ("", BLACK)  # wi-na (not available)


def _temp_color(temp_f: int | None):
    if temp_f is None:
        return BLACK
    if temp_f <= 35:
        return BLUE
    if temp_f >= 80:
        return RED
    return BLACK


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


def _fit_image_into(box: tuple[int, int, int, int], img: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """Letterbox `img` into `box` (preserving aspect ratio). Returns the resized
    image and its top-left paste position inside the box."""
    bx0, by0, bx1, by1 = box
    bw, bh = bx1 - bx0, by1 - by0
    iw, ih = img.size
    scale = min(bw / iw, bh / ih)
    new_size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
    resized = img.resize(new_size, Image.LANCZOS)
    paste_x = bx0 + (bw - new_size[0]) // 2
    paste_y = by0 + (bh - new_size[1]) // 2
    return resized, (paste_x, paste_y)


def _draw_header(draw: ImageDraw.ImageDraw, apod: dict | None) -> None:
    now = datetime.now()
    date_str = now.strftime("%a · %b %-d")
    font_date = _font(32)
    date_w = draw.textlength(date_str, font=font_date)
    draw.text((20, 6), date_str, fill=BLACK, font=font_date)

    # APOD title on the right.
    if apod and apod.get("title"):
        font_title = _font(20)
        max_w = WIDTH - 40 - int(date_w) - 30  # 20px padding each side + gap
        title = _truncate_to_width(draw, apod["title"], font_title, max_w)
        title_w = draw.textlength(title, font=font_title)
        draw.text((WIDTH - 20 - title_w, 16), title, fill=BLACK, font=font_title)

    # Hairline under header.
    draw.line([(0, HEADER[3] - 1), (WIDTH, HEADER[3] - 1)], fill=BLACK, width=1)


def _draw_sidebar(draw: ImageDraw.ImageDraw, events: list[dict]) -> None:
    sx0, sy0, sx1, sy1 = SIDEBAR
    pad = 16

    title_font = _font(20)
    year_font = _font(22)
    event_font = _font(16)

    draw.text((sx0 + pad, sy0 + 10), "ON THIS DAY", fill=RED, font=title_font)
    cursor_y = sy0 + 40

    # Width available for wrapping.
    text_width_px = (sx1 - sx0) - 2 * pad

    if not events:
        draw.text((sx0 + pad, cursor_y), "—", fill=BLACK, font=event_font)
        return

    for ev in events:
        if cursor_y > sy1 - 30:
            break
        year_str = str(ev["year"])
        draw.text((sx0 + pad, cursor_y), year_str, fill=BLUE, font=year_font)
        cursor_y += 26

        # Wrap event text. textbbox-based wrapping is fiddly; approximate by
        # character count tuned to the font and box width.
        chars_per_line = max(10, text_width_px // 8)
        wrapped = textwrap.wrap(ev["text"], width=chars_per_line)
        for line in wrapped[:3]:
            if cursor_y > sy1 - 20:
                break
            draw.text((sx0 + pad + 8, cursor_y), line, fill=BLACK, font=event_font)
            cursor_y += 18
        cursor_y += 10

    # Vertical separator on the left edge of the sidebar.
    draw.line([(sx0, sy0), (sx0, sy1)], fill=BLACK, width=1)


def _draw_condition_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int,
                         icon_key: str | None, size: int = 36) -> None:
    """Render a Weather Icons glyph at (cx, cy). Color from the WEATHER_ICONS map."""
    glyph, color = WEATHER_ICONS.get(icon_key or "", _WI_FALLBACK)
    font = _wi_font(size)
    bbox = draw.textbbox((0, 0), glyph, font=font)
    gw = bbox[2] - bbox[0]
    gh = bbox[3] - bbox[1]
    draw.text((cx - gw // 2 - bbox[0], cy - gh // 2 - bbox[1]),
              glyph, fill=color, font=font)


def _draw_forecast(draw: ImageDraw.ImageDraw, forecast: list[dict]) -> None:
    fx0, fy0, fx1, fy1 = FORECAST
    draw.line([(0, fy0), (WIDTH, fy0)], fill=BLACK, width=1)

    if not forecast:
        draw.text((20, fy0 + 38), "Forecast unavailable", fill=BLACK, font=_font(22))
        return

    n = min(len(forecast), 7)
    day_font = _font(20)
    temp_font = _font(26)

    for i in range(n):
        day = forecast[i]
        box_x0 = (WIDTH * i) // n
        box_x1 = (WIDTH * (i + 1)) // n
        cx = (box_x0 + box_x1) // 2

        if i > 0:
            draw.line([(box_x0, fy0 + 10), (box_x0, fy1 - 10)], fill=BLACK, width=1)

        label = day["label"]
        day_color = BLUE if day.get("is_weekend") else BLACK
        lw = draw.textlength(label, font=day_font)
        draw.text((cx - lw / 2, fy0 + 6), label, fill=day_color, font=day_font)

        _draw_condition_icon(draw, cx, fy0 + 48, day.get("icon_key"))

        high = day.get("high")
        low = day.get("low")
        if high is not None and low is not None:
            temp_str = f"{high}°/{low}°"
        elif high is not None:
            temp_str = f"{high}°"
        else:
            temp_str = "—"
        temp_color = _temp_color(high)
        tw = draw.textlength(temp_str, font=temp_font)
        draw.text((cx - tw / 2, fy0 + 70), temp_str, fill=temp_color, font=temp_font)


def render_rgb() -> Image.Image:
    """Render the dashboard as a full-color RGB image — this is the source of truth."""
    img = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    apod = sources.get_apod()
    forecast = sources.get_forecast()
    events = sources.get_on_this_day()

    _draw_header(draw, apod)

    # Hero image area.
    apod_image = None
    if apod and apod.get("url"):
        apod_image = sources.download_image(apod["url"])
    if apod_image:
        resized, pos = _fit_image_into(IMAGE_BOX, apod_image)
        img.paste(resized, pos)
    else:
        # No image: gray-ish placeholder text.
        draw.text(
            (IMAGE_BOX[0] + 40, IMAGE_BOX[1] + 160),
            "NASA APOD unavailable",
            fill=BLACK,
            font=_font(28),
        )

    _draw_sidebar(draw, events)
    _draw_forecast(draw, forecast)

    return img


def render_quantized() -> Image.Image:
    """RGB render passed through Floyd-Steinberg to the Spectra 6 palette. Mode 'P'."""
    return render_rgb().quantize(palette=_PAL_IMG, dither=Image.Dither.FLOYDSTEINBERG)


def render_preview_png() -> bytes:
    """PNG bytes of the quantized image — what the browser shows."""
    buf = io.BytesIO()
    render_quantized().convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def render_frame_bin() -> bytes:
    """800*480/2 = 192000 bytes, 4bpp packed, in Spectra 6 codes. What the R4 streams."""
    q = render_quantized()
    pixels = q.tobytes()

    table = bytearray(256)
    for i, code in enumerate(INDEX_TO_CODE):
        table[i] = code
    codes = pixels.translate(bytes(table))

    packed = bytearray(len(codes) // 2)
    for i in range(0, len(codes), 2):
        packed[i // 2] = (codes[i] << 4) | codes[i + 1]
    return bytes(packed)
