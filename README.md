# e-Paper Dashboard

A daily wall dashboard for a **Waveshare 7.3" Spectra 6 (7.3e)** e-paper panel,
driven by an **Arduino UNO R4 WiFi**.

A small Flask server does all the heavy lifting: it renders an 800×480 frame in
full color, quantizes it down to the panel's 6-color palette with
Floyd–Steinberg dithering, and serves it as a raw 4bpp buffer
(`/frame.bin`, 192000 bytes). The R4 has no display library and no image
decoding to do — it just fetches that buffer and streams the bytes straight to
the panel over SPI. Once a day (5am by default) it wakes, pulls a fresh frame,
draws it, and puts the panel back to sleep.

The dashboard shows:

- 🛰️ **NASA Astronomy Picture of the Day** as the hero image (falls back to
  yesterday's if today's APOD is a video)
- 📅 **"On this day"** historical events from Wikipedia
- 🌦️ A **7-day weather forecast** from the US National Weather Service, with
  Weather Icons glyphs and high/low temperatures

```
┌──────────────────────────────────────────────────────┐
│ Sat · Jun 7                         APOD title here    │  header
├───────────────────────────────┬──────────────────────┤
│                               │  ON THIS DAY          │
│                               │  1893                 │
│        NASA APOD image        │  Some event…          │  hero + sidebar
│         (letterboxed)         │  1969                 │
│                               │  Another event…       │
├───────┬───────┬───────┬───────┴──────┬───────┬───────┤
│ TODAY │  SUN  │  MON  │  TUE  │  WED  │  THU  │  FRI  │
│   ☀   │   ⛅  │   🌧  │   ☀   │   ☀   │   ⛅  │   🌧  │  forecast strip
│ 78°/61°│ 75°/59│ 70°/58│  ...                          │
└───────┴───────┴───────┴───────┴───────┴───────┴───────┘
```

## How it works

```
  ┌─────────────────┐   GET /frame.bin    ┌────────────────────┐   SPI    ┌──────────┐
  │  Flask server   │ ◀───────────────────│  Arduino UNO R4    │ ───────▶ │ 7.3"     │
  │  (your machine) │                     │  WiFi              │  4bpp    │ Spectra 6│
  │                 │ ───────────────────▶│                    │  stream  │ panel    │
  │  render + dither│   192000 bytes      │  no decode, just   │          │          │
  └─────────────────┘   X-Next-Refresh-Ms │  pipe bytes to SPI │          └──────────┘
        │                                 └────────────────────┘
        ▼ fetches
  NASA APOD · NWS forecast · Wikipedia "On this day"
```

The server response carries an **`X-Next-Refresh-Ms`** header telling the board
how long to sleep before its next fetch — computed as the time until the next
daily refresh hour. The board honors it (with a 60s safety floor) so the refresh
schedule lives entirely on the server; reflashing the firmware isn't needed to
change it.

The server keeps an in-memory **stale-while-revalidate** cache for every upstream
source, so renders are fast and the external APIs never get hammered: an expired
entry is served immediately while a background thread refreshes it. Only a cold
cache miss blocks.

## Repository layout

```
server/                       Flask render server (runs on your machine / a Pi)
  app.py                      Routes: / (preview), /preview.png, /frame.bin
  render.py                   Layout + Spectra 6 quantization and 4bpp packing
  sources.py                  NASA APOD, NWS weather, Wikipedia "On this day"
  fonts/                      Weather Icons font for forecast glyphs
  start.sh / stop.sh          Run the server detached via nohup
  .env.example                Copy to .env, add your NASA API key
firmware/epaper_dashboard/    Arduino sketch for the UNO R4 WiFi
  epaper_dashboard.ino        Fetch loop, WiFi, SPI streaming, status LED heart
  secrets.h.example           Copy to secrets.h, add WiFi + server address
  src/                        Vendored Waveshare panel + config drivers
```

## Server setup

Requires Python 3.10+.

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then add your NASA API key (optional)
python3 app.py
```

Open <http://localhost:5000> to see a live browser preview of the quantized
frame. The raw buffer the board fetches is at
[`/frame.bin`](http://localhost:5000/frame.bin).

> **macOS:** port 5000 is taken by the AirPlay Receiver, so set `PORT=5002` (or
> any free port) in `server/.env` and use that port everywhere below.

A free **NASA API key** (from <https://api.nasa.gov>) is optional but
recommended — `DEMO_KEY` is limited to ~30 requests/hour. Drop it into
`server/.env`:

```
NASA_API_KEY=your_key_here
```

### Configuration

| Setting             | Where                              | Default        |
|---------------------|------------------------------------|----------------|
| Location (lat/lon)  | `server/sources.py` (`LAT`/`LON`)  | NYC / Central Park |
| Daily refresh hour  | `DAILY_REFRESH_HOUR` env var       | `5` (5am, server local time) |
| Server port         | `PORT` env var / `.env`            | `5000` (use `5002` on macOS) |
| NASA API key        | `NASA_API_KEY` env var / `.env`    | `DEMO_KEY`     |

### Running detached

`start.sh` launches the server under `nohup` (logging to `server.log`);
`stop.sh` kills it. Suitable for leaving running on a Raspberry Pi or always-on
machine on your LAN.

```bash
cd server
./start.sh    # background, FLASK_DEBUG=0
./stop.sh
```

## Firmware setup

1. Open `firmware/epaper_dashboard/epaper_dashboard.ino` in the Arduino IDE.
2. Install board support for the **Arduino UNO R4 WiFi** (Renesas) and the
   `Arduino_LED_Matrix` library.
3. Copy `secrets.h.example` to `secrets.h` and fill in your WiFi credentials and
   the **LAN IP and port** of the machine running the server:

   ```c
   #define WIFI_SSID     "your-ssid"
   #define WIFI_PASSWORD "your-password"
   #define SERVER_HOST   "192.168.1.100"   // your server's LAN IP
   #define SERVER_PORT   5000              // match the server's PORT (5002 on macOS)
   ```

   > **Note:** `SERVER_PORT` must match the port the server actually listens on
   (the `PORT` env var, default 5000 — but 5002 if you set it for macOS).
   `secrets.h` is gitignored so your credentials stay out of the repo.

4. Flash the board. The built-in 12×8 LED matrix shows a **beating heart**
   while the R4 is busy on WiFi / HTTP / SPI, so you can tell at a glance that
   it's working.

### Wiring (panel → Arduino UNO R4 WiFi)

Pin assignments are defined in
[`firmware/epaper_dashboard/src/Config/DEV_Config.h`](firmware/epaper_dashboard/src/Config/DEV_Config.h).

| Panel pin   | R4 pin        | Function                                              |
|-------------|---------------|------------------------------------------------------|
| VCC         | 5V (or 3.3V)  | Power                                                 |
| GND         | GND           | Ground                                                |
| DIN (SDA)   | D11 / MOSI    | SPI serial data in                                   |
| CLK (SCL)   | D13 / SCK     | SPI serial clock                                      |
| CS (CSB)    | D10           | Chip select, active low                              |
| DC          | D9            | Data/command — low = command, high = data            |
| RST         | D8            | Reset, active low                                    |
| BUSY        | D7            | Status output — the only line that's an MCU **input** |

> **D6 (PWR):** This panel exposes 8 pins and has no `PWR` line, so D6 is left
> unconnected. The firmware still configures D6 as `EPD_PWR_PIN` and toggles it,
> but with nothing wired to it that's harmless.

> ⚠️ **Panel longevity:** the firmware puts the panel back to deep sleep
> immediately after every refresh (`EPD_7IN3E_Sleep()`). Leaving the driver
> transistors energized can destroy the display diaphragm within months — don't
> remove that call.

## The frame format

`/frame.bin` is exactly **192000 bytes** = `800 × 480 / 2`, two pixels per byte
(4 bits each). Each nibble is a Spectra 6 color code the panel expects on the
wire:

| Code | Color  |
|------|--------|
| 0x0  | Black  |
| 0x1  | White  |
| 0x2  | Yellow |
| 0x3  | Red    |
| 0x5  | Blue   |
| 0x6  | Green  |

The server renders in full RGB first (the source of truth), then quantizes to
these six colors with Floyd–Steinberg dithering and packs the result two pixels
to a byte — see `render.py`.

## License

Released under [CC0 1.0 Universal](LICENSE) — public domain dedication. Do
whatever you like with it.
