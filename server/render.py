"""Render the dashboard image and quantize it to the Waveshare 7.3" Spectra 6 palette."""
from datetime import datetime
import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

import sources

WIDTH = 800
HEIGHT = 480

# Layout regions (x0, y0, x1, y1).
HEADER = (0, 0, WIDTH, 50)
IMAGE_BOX = (0, 50, 500, 440)
SIDEBAR = (500, 50, WIDTH, 440)
FOOTER = (0, 440, WIDTH, HEIGHT)

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


def _temp_color(temp_f: int | None):
    if temp_f is None:
        return BLACK
    if temp_f <= 35:
        return BLUE
    if temp_f >= 80:
        return RED
    return BLACK


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


def _draw_header(draw: ImageDraw.ImageDraw, weather: dict | None) -> None:
    now = datetime.now()
    date_str = now.strftime("%a · %b %-d")
    font_date = _font(32)
    draw.text((20, 6), date_str, fill=BLACK, font=font_date)

    # Right side: location + temp + condition
    font_temp = _font(32)
    font_cond = _font(22)
    if weather:
        temp = weather["temp_f"]
        cond = weather["short"]
        temp_color = _temp_color(temp)
        loc_text = f"{sources.LOCATION_LABEL}"
        temp_text = f"{temp}°F"

        # Build from the right edge.
        cond_w = draw.textlength(cond, font=font_cond)
        temp_w = draw.textlength(temp_text, font=font_temp)
        loc_w = draw.textlength(loc_text, font=font_temp)
        gap = 16

        right = WIDTH - 20
        cond_x = right - cond_w
        draw.text((cond_x, 14), cond, fill=BLACK, font=font_cond)

        temp_x = cond_x - gap - temp_w
        draw.text((temp_x, 6), temp_text, fill=temp_color, font=font_temp)

        loc_x = temp_x - gap - loc_w
        draw.text((loc_x, 6), loc_text, fill=BLACK, font=font_temp)
    else:
        font_dash = _font(28)
        draw.text((WIDTH - 80, 8), "—", fill=BLACK, font=font_dash)

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


def _draw_footer(draw: ImageDraw.ImageDraw, apod_title: str | None) -> None:
    fx0, fy0, fx1, fy1 = FOOTER
    draw.rectangle([fx0, fy0, fx1, fy1], fill=BLACK)
    title = apod_title or "No image today"
    font = _font(20)
    draw.text((fx0 + 16, fy0 + 8), f"NASA: {title}", fill=WHITE, font=font)


def render_rgb() -> Image.Image:
    """Render the dashboard as a full-color RGB image — this is the source of truth."""
    img = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    apod = sources.get_apod()
    weather = sources.get_weather()
    events = sources.get_on_this_day()

    _draw_header(draw, weather)

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
    _draw_footer(draw, apod.get("title") if apod else None)

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
