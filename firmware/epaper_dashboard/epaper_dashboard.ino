#include <WiFiS3.h>
#include "Arduino_LED_Matrix.h"
#include "secrets.h"
#include "src/Config/DEV_Config.h"
#include "src/e-Paper/EPD_7in3e.h"

#define FRAME_BYTES (EPD_7IN3E_WIDTH * EPD_7IN3E_HEIGHT / 2)  // 192000
#define DEFAULT_REFRESH_MS (24UL * 60UL * 60UL * 1000UL)      // fallback if server header missing
#define MIN_REFRESH_MS     (60UL * 1000UL)                    // safety floor against runaway server values
#define HEART_FRAME_MS 1500

// Updated from the server's X-Next-Refresh-Ms header on every successful fetch.
static unsigned long nextRefreshDelayMs = DEFAULT_REFRESH_MS;

WiFiClient wifi;
ArduinoLEDMatrix matrix;

// 12-wide x 8-tall heart frames driven onto the R4's built-in LED matrix.
// We toggle between BIG and SMALL every ~500ms via beatTick() to get a beating
// heart while we're blocked on WiFi / HTTP / SPI.
//
// Not const: the Arduino_LED_Matrix renderBitmap() macro forwards to
// loadPixels(uint8_t*, size_t), which won't bind to const arrays.
static uint8_t HEART_BIG[8][12] = {
    {0,0,1,1,0,0,0,0,1,1,0,0},
    {0,1,1,1,1,0,0,1,1,1,1,0},
    {1,1,1,1,1,1,1,1,1,1,1,1},
    {1,1,1,1,1,1,1,1,1,1,1,1},
    {0,1,1,1,1,1,1,1,1,1,1,0},
    {0,0,1,1,1,1,1,1,1,1,0,0},
    {0,0,0,1,1,1,1,1,1,0,0,0},
    {0,0,0,0,1,1,1,1,0,0,0,0},
};

static uint8_t HEART_SMALL[8][12] = {
    {0,0,0,0,0,0,0,0,0,0,0,0},
    {0,0,0,1,1,0,0,1,1,0,0,0},
    {0,0,1,1,1,1,1,1,1,1,0,0},
    {0,0,1,1,1,1,1,1,1,1,0,0},
    {0,0,0,1,1,1,1,1,1,0,0,0},
    {0,0,0,0,1,1,1,1,0,0,0,0},
    {0,0,0,0,0,1,1,0,0,0,0,0},
    {0,0,0,0,0,0,0,0,0,0,0,0},
};

static uint8_t MATRIX_OFF[8][12] = {{0}};

static unsigned long lastBeatAt = 0;
static bool beatBig = true;

static void beatTick() {
    if (millis() - lastBeatAt >= HEART_FRAME_MS) {
        lastBeatAt = millis();
        beatBig = !beatBig;
        // renderBitmap is a macro that doesn't parenthesize its first arg, so
        // an inline ternary mis-parses. Pick the frame first, then pass it in.
        uint8_t (*frame)[12] = beatBig ? HEART_BIG : HEART_SMALL;
        matrix.renderBitmap(frame, 8, 12);
    }
}

static void showHeart() {
    beatBig = true;
    matrix.renderBitmap(HEART_BIG, 8, 12);
    lastBeatAt = millis();
}

static void clearMatrix() {
    matrix.renderBitmap(MATRIX_OFF, 8, 12);
}

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

    // Wait for BOTH association and a DHCP lease. begin() returns as soon as
    // it's associated; localIP() can still be 0.0.0.0 for a beat after that.
    const unsigned long deadline = millis() + 20000;
    IPAddress zero(0, 0, 0, 0);
    while (millis() < deadline) {
        beatTick();
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

// Fetch /frame.bin from the dashboard server and stream every byte straight to
// the panel. Returns true iff we got the full frame and the panel still needs a refresh.
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

    // Read response headers. We pull X-Next-Refresh-Ms out and discard the rest.
    while (wifi.connected()) {
        String line = wifi.readStringUntil('\n');
        if (line.length() <= 1) break;  // "\r" or ""
        if (line.startsWith("X-Next-Refresh-Ms:")) {
            long v = line.substring(18).toInt();  // toInt() skips leading whitespace
            if (v >= (long)MIN_REFRESH_MS) {
                nextRefreshDelayMs = (unsigned long)v;
                Serial.print("HTTP: next refresh in ");
                Serial.print(v / 1000);
                Serial.println("s");
            }
        }
    }

    // Begin the single 0x10 data-write transaction; from here every received
    // body byte goes straight out the SPI bus.
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
        beatTick();
    }
    wifi.stop();

    Serial.print("HTTP: streamed ");
    Serial.print(got);
    Serial.print(" / ");
    Serial.println(FRAME_BYTES);
    return got == FRAME_BYTES;
}

static unsigned long lastRefreshAt = 0;

// One full refresh cycle: ensure WiFi, wake panel, fetch, draw, sleep panel.
static void doRefresh() {
    Serial.println("refresh: starting");

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("refresh: WiFi dropped, reconnecting");
        connectWiFi();
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("refresh: skipped — no WiFi");
        lastRefreshAt = millis();  // try again in REFRESH_INTERVAL_MS
        return;
    }

    EPD_7IN3E_Init();  // wakes the panel from the deep sleep we left it in

    bool ok = fetchAndStream();
    if (ok) {
        showHeart();
        EPD_7IN3E_TurnOnDisplay();
    } else {
        Serial.println("refresh: skipped — partial frame");
    }
    // CRITICAL: sleep the panel immediately, before anything else. Leaving the
    // driver transistors energized destroys the diaphragm within months.
    EPD_7IN3E_Sleep();

    lastRefreshAt = millis();
    Serial.println("refresh: done");
}

void setup() {
    Serial.begin(115200);
    delay(2000);
    Serial.println("\nepaper-dashboard boot");

    matrix.begin();
    showHeart();

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
    beatTick();
    if (millis() - lastRefreshAt >= nextRefreshDelayMs) {
        doRefresh();
    }
    delay(50);
}
