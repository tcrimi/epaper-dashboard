#include <WiFiS3.h>
#include "Arduino_LED_Matrix.h"
#include "secrets.h"
#include "src/Config/DEV_Config.h"
#include "src/e-Paper/EPD_7in3e.h"

#define FRAME_BYTES        (EPD_7IN3E_WIDTH * EPD_7IN3E_HEIGHT / 2)
#define DEFAULT_REFRESH_MS (24UL * 60UL * 60UL * 1000UL)
#define MIN_REFRESH_MS     (60UL * 1000UL)

static unsigned long nextRefreshDelayMs = DEFAULT_REFRESH_MS;

WiFiClient wifi;
ArduinoLEDMatrix matrix;

// ─── LED matrix animation ─────────────────────────────────────────────────────
// Runs on the Arduino R4's built-in 8×12 matrix while WiFi/HTTP/SPI are busy.
// Portrait orientation: 8 pixels wide (rows in memory), 12 pixels tall (cols).
// frame[x][y]: x ∈ [0,ANIM_W) = horizontal,  y ∈ [0,ANIM_H) = vertical (0=top).

#define ANIM_RAIN   0
#define ANIM_TETRIS 1
#define ANIMATION   ANIM_RAIN   // ← swap to ANIM_TETRIS for Tetris demo

#define ANIM_W 8
#define ANIM_H 12

// ─── Matrix Rain ──────────────────────────────────────────────────────────────
#if ANIMATION == ANIM_RAIN

#define RAIN_TICK_MS 100
#define RAIN_TAIL    4     // lit pixels trailing the head

struct RainDrop {
    int8_t  y;      // head y; negative = off-screen, waiting to enter
    uint8_t speed;  // extra ticks to hold between steps (0 = fastest)
    uint8_t wait;   // ticks remaining before next step
};

static RainDrop     rainDrops[ANIM_W];
static bool         rainInited    = false;
static unsigned long lastAnimTick = 0;

static void animStart() {
    rainInited    = false;
    lastAnimTick  = 0;   // 0 forces an immediate tick on the first animTick() call
}

static void animTick() {
    if (!rainInited) {
        for (int x = 0; x < ANIM_W; x++) {
            rainDrops[x].y     = -(int8_t)random(0, ANIM_H + 1);
            rainDrops[x].speed = (uint8_t)random(0, 4);
            rainDrops[x].wait  = 0;
        }
        rainInited = true;
    }
    if (millis() - lastAnimTick < RAIN_TICK_MS) return;
    lastAnimTick = millis();

    uint8_t frame[ANIM_W][ANIM_H] = {};
    for (int x = 0; x < ANIM_W; x++) {
        RainDrop &d = rainDrops[x];
        if (d.wait > 0) {
            d.wait--;
        } else {
            d.y++;
            if (d.y > (int8_t)(ANIM_H + RAIN_TAIL)) {
                d.y     = -(int8_t)random(1, ANIM_H + 1);
                d.speed = (uint8_t)random(0, 4);
            }
            d.wait = d.speed;
        }
        for (int t = 0; t <= RAIN_TAIL; t++) {
            int py = (int)d.y - t;
            if (py >= 0 && py < ANIM_H)
                frame[x][ANIM_H - 1 - py] = 1;
        }
    }
    matrix.renderBitmap(frame, ANIM_W, ANIM_H);
}

// ─── Tetris Demo ──────────────────────────────────────────────────────────────
// Pieces fall from the top and stack; complete rows are cleared automatically.
#else

#define TET_TICK_MS 200

// (dx, dy) cell offsets from spawn origin. dy increases downward.
static const int8_t TET_PIECES[][4][2] PROGMEM = {
    {{0,0},{0,1},{0,2},{0,3}},  // I vertical
    {{0,0},{1,0},{2,0},{3,0}},  // I horizontal
    {{0,0},{1,0},{0,1},{1,1}},  // O
    {{0,0},{1,0},{2,0},{1,1}},  // T
    {{0,1},{1,0},{1,1},{2,0}},  // S
    {{0,0},{1,0},{1,1},{2,1}},  // Z
    {{0,0},{0,1},{0,2},{1,2}},  // L
    {{1,0},{1,1},{1,2},{0,2}},  // J
};
#define TET_NUM_PIECES 8

static uint8_t  tetBoard[ANIM_H][ANIM_W];
static int8_t   tetPX[4], tetPY[4];
static bool     tetHasPiece   = false;
static bool     tetInited     = false;
static unsigned long lastAnimTick = 0;

static bool tetFits(int8_t px[], int8_t py[]) {
    for (int i = 0; i < 4; i++) {
        if (px[i] < 0 || px[i] >= ANIM_W || py[i] < 0 || py[i] >= ANIM_H) return false;
        if (tetBoard[py[i]][px[i]]) return false;
    }
    return true;
}

static void tetRender() {
    uint8_t frame[ANIM_W][ANIM_H] = {};
    for (int y = 0; y < ANIM_H; y++)
        for (int x = 0; x < ANIM_W; x++)
            frame[x][ANIM_H - 1 - y] = tetBoard[y][x];
    if (tetHasPiece)
        for (int i = 0; i < 4; i++)
            if (tetPX[i] >= 0 && tetPX[i] < ANIM_W && tetPY[i] >= 0 && tetPY[i] < ANIM_H)
                frame[tetPX[i]][ANIM_H - 1 - tetPY[i]] = 1;
    matrix.renderBitmap(frame, ANIM_W, ANIM_H);
}

static void tetSpawn() {
    int type = (int)random(TET_NUM_PIECES);
    int8_t maxDX = 0;
    for (int i = 0; i < 4; i++) {
        int8_t dx = (int8_t)pgm_read_byte(&TET_PIECES[type][i][0]);
        if (dx > maxDX) maxDX = dx;
    }
    int range = ANIM_W - maxDX;
    int8_t offx = (range > 1) ? (int8_t)random(0, range) : 0;
    for (int i = 0; i < 4; i++) {
        tetPX[i] = offx + (int8_t)pgm_read_byte(&TET_PIECES[type][i][0]);
        tetPY[i] =        (int8_t)pgm_read_byte(&TET_PIECES[type][i][1]);
    }
    tetHasPiece = tetFits(tetPX, tetPY);
    if (!tetHasPiece) {
        // Board is full — reset and try again
        memset(tetBoard, 0, sizeof(tetBoard));
        tetSpawn();
    }
}

static void animStart() {
    tetInited    = false;
    lastAnimTick = 0;
}

static void animTick() {
    if (!tetInited) {
        memset(tetBoard, 0, sizeof(tetBoard));
        tetInited = true;
        tetSpawn();
        tetRender();
        return;
    }
    if (millis() - lastAnimTick < TET_TICK_MS) return;
    lastAnimTick = millis();

    if (!tetHasPiece) {
        tetSpawn();
        tetRender();
        return;
    }

    // Try to drop the current piece one row
    int8_t nx[4], ny[4];
    for (int i = 0; i < 4; i++) { nx[i] = tetPX[i]; ny[i] = tetPY[i] + 1; }

    if (tetFits(nx, ny)) {
        memcpy(tetPX, nx, 4);
        memcpy(tetPY, ny, 4);
    } else {
        // Lock piece into board
        for (int i = 0; i < 4; i++)
            tetBoard[tetPY[i]][tetPX[i]] = 1;
        // Clear complete rows (scan bottom-up; shift rows down when one clears)
        for (int y = ANIM_H - 1; y >= 0; y--) {
            bool full = true;
            for (int x = 0; x < ANIM_W; x++) if (!tetBoard[y][x]) { full = false; break; }
            if (full) {
                memmove(tetBoard[1], tetBoard[0], (size_t)y * ANIM_W);
                memset(tetBoard[0], 0, ANIM_W);
                y++;  // recheck the same row index, which now holds the shifted row
            }
        }
        tetHasPiece = false;
    }
    tetRender();
}

#endif  // ANIMATION

// ─── WiFi ─────────────────────────────────────────────────────────────────────

static const char* wifiStatusStr(int s) {
    switch (s) {
        case WL_IDLE_STATUS:     return "IDLE";
        case WL_NO_SSID_AVAIL:   return "NO_SSID";
        case WL_SCAN_COMPLETED:  return "SCAN_DONE";
        case WL_CONNECTED:       return "CONNECTED";
        case WL_CONNECT_FAILED:  return "CONNECT_FAILED";
        case WL_CONNECTION_LOST: return "CONNECTION_LOST";
        case WL_DISCONNECTED:    return "DISCONNECTED";
        default:                 return "?";
    }
}

static bool connectWiFi() {
    Serial.print("WiFi: module fw=");
    Serial.println(WiFi.firmwareVersion());
    Serial.print("WiFi: connecting to '");
    Serial.print(WIFI_SSID);
    Serial.println("'");

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    const unsigned long deadline = millis() + 20000;
    IPAddress zero(0, 0, 0, 0);
    while (millis() < deadline) {
        animTick();
        delay(200);
        Serial.print(".");
        if (WiFi.status() == WL_CONNECTED && WiFi.localIP() != zero) {
            Serial.println();
            Serial.print("WiFi: connected, IP=");
            Serial.println(WiFi.localIP());
            return true;
        }
    }
    Serial.println();
    Serial.print("WiFi: timeout, status=");
    Serial.print(wifiStatusStr(WiFi.status()));
    Serial.print(" IP=");
    Serial.println(WiFi.localIP());
    return false;
}

// ─── HTTP fetch + SPI stream ──────────────────────────────────────────────────

// The server renders the frame inside the request handler, so the first
// response byte can take many seconds on a cache miss. Stream's default 1 s
// readStringUntil timeout would return an empty line during that wait, which
// looks identical to the blank line terminating the headers — the panel then
// consumed the real headers (~230 bytes ≈ 460 px) as pixel data, shifting the
// image right by about half the screen.
#define HEADER_BYTE_TIMEOUT_MS 30000UL

// Read one CRLF-terminated header line (CR/LF stripped). Returns false if the
// connection closes or no byte arrives within HEADER_BYTE_TIMEOUT_MS.
static bool readHeaderLine(String &line) {
    line = "";
    unsigned long lastByteAt = millis();
    while (millis() - lastByteAt < HEADER_BYTE_TIMEOUT_MS) {
        if (wifi.available() <= 0) {
            if (!wifi.connected()) return false;
            animTick();
            delay(1);
            continue;
        }
        char c = (char)wifi.read();
        lastByteAt = millis();
        if (c == '\n') return true;
        if (c != '\r') line += c;
    }
    Serial.println("HTTP: header timeout");
    return false;
}

static bool fetchAndStream() {
    Serial.print("HTTP: GET http://");
    Serial.print(SERVER_HOST);
    Serial.print(":");
    Serial.print(SERVER_PORT);
    Serial.println("/frame.bin");

    if (!wifi.connect(SERVER_HOST, SERVER_PORT)) {
        Serial.println("HTTP: connect failed");
        return false;
    }

    wifi.print("GET /frame.bin HTTP/1.1\r\nHost: ");
    wifi.print(SERVER_HOST);
    wifi.print("\r\nConnection: close\r\nUser-Agent: epaper-dashboard/1\r\n\r\n");

    String line;
    if (!readHeaderLine(line) || line.indexOf(" 200") < 0) {
        Serial.print("HTTP: bad status line: ");
        Serial.println(line);
        wifi.stop();
        return false;
    }
    bool headersDone = false;
    while (readHeaderLine(line)) {
        if (line.length() == 0) { headersDone = true; break; }
        if (line.startsWith("X-Next-Refresh-Ms:")) {
            long v = line.substring(18).toInt();
            if (v >= (long)MIN_REFRESH_MS) {
                nextRefreshDelayMs = (unsigned long)v;
                Serial.print("HTTP: next refresh in ");
                Serial.print(v / 1000);
                Serial.println("s");
            }
        }
    }
    if (!headersDone) {
        Serial.println("HTTP: headers truncated");
        wifi.stop();
        return false;
    }

    EPD_7IN3E_SendCommand(0x10);

    long got = 0;
    unsigned long lastByteAt = millis();
    while (got < FRAME_BYTES) {
        int avail = wifi.available();
        if (avail > 0) {
            while (avail-- > 0 && got < FRAME_BYTES) {
                EPD_7IN3E_SendData((UBYTE)wifi.read());
                got++;
            }
            lastByteAt = millis();
        } else if (!wifi.connected()) {
            break;
        } else if (millis() - lastByteAt > 10000) {
            Serial.println("HTTP: stalled, giving up");
            break;
        }
        animTick();
    }
    wifi.stop();

    Serial.print("HTTP: streamed ");
    Serial.print(got);
    Serial.print(" / ");
    Serial.println(FRAME_BYTES);
    return got == FRAME_BYTES;
}

// ─── Refresh cycle ────────────────────────────────────────────────────────────

static unsigned long lastRefreshAt = 0;

static void doRefresh() {
    Serial.println("refresh: starting");

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("refresh: WiFi dropped, reconnecting");
        connectWiFi();
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("refresh: skipped — no WiFi");
        lastRefreshAt = millis();
        return;
    }

    EPD_7IN3E_Init();

    bool ok = fetchAndStream();
    if (ok) {
        animStart();  // reset animation as a visual "refresh complete" cue
        EPD_7IN3E_TurnOnDisplay();
    } else {
        Serial.println("refresh: skipped — partial frame");
    }
    // CRITICAL: sleep the panel immediately. Leaving driver transistors energised
    // destroys the diaphragm within months.
    EPD_7IN3E_Sleep();

    lastRefreshAt = millis();
    Serial.println("refresh: done");
}

// ─── Arduino entry points ─────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(2000);
    Serial.println("\nepaper-dashboard boot");

    randomSeed(analogRead(A0));   // different animation sequence each boot

    matrix.begin();
    animStart();

    bool wifiOk = connectWiFi();

    DEV_Module_Init();

    if (wifiOk) {
        doRefresh();
    } else {
        Serial.println("setup: no WiFi, will retry on next interval");
        lastRefreshAt = millis();
    }
}

void loop() {
    animTick();
    if (millis() - lastRefreshAt >= nextRefreshDelayMs) {
        doRefresh();
    }
    delay(50);
}
