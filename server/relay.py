"""TCP relay that lets the R4 reach the dashboard past the macOS app firewall.

The firewall blocks inbound connections to the Homebrew Python that runs
app.py, and that per-app rule can't be cleared while this Mac has a managed
firewall profile (see README). /usr/bin/python3 *is* allowlisted, so run this
under it: the relay accepts on the LAN and forwards to Flask over loopback,
which the firewall doesn't filter.

    /usr/bin/python3 relay.py

It listens on PORT (what the board targets) and forwards to FLASK_PORT.

Run it with that absolute path and no venv. The venv's python3 resolves to the
blocked interpreter, and `python3 -m venv` copies its binary to a path the
firewall hasn't allowlisted -- either way the relay would be blocked too. That
constraint is also why this file is stdlib-only and 3.9-compatible:
/usr/bin/python3 is Python 3.9.6 and gets no packages installed into it.
"""
import os
import socket
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))


def _env_port(name, default):
    """PORT as app.py sees it: the environment, else .env, else the default.

    app.py reads .env through python-dotenv, which isn't available to
    /usr/bin/python3, so parse the one key we need by hand.
    """
    if os.environ.get(name):
        return int(os.environ[name])
    try:
        with open(os.path.join(_HERE, ".env")) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(name + "="):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return default


# The relay owns PORT -- the port the board already targets -- so the firmware
# needs no reflash. Flask retreats to FLASK_PORT, reachable only over loopback.
LISTEN_HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
LISTEN_PORT = _env_port("PORT", 5000)
TARGET_HOST = "127.0.0.1"
TARGET_PORT = _env_port("FLASK_PORT", 5012)

# Generous enough to outlast a cold /frame.bin render, tight enough that a
# half-open socket from a board that slept mid-request frees its thread.
TIMEOUT = float(os.environ.get("RELAY_TIMEOUT", "120"))
BUF = 65536


def _pump(src, dst):
    """Copy src -> dst until EOF, then half-close dst so it sees the EOF too."""
    try:
        while True:
            chunk = src.recv(BUF)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass  # peer vanished mid-transfer; the finally below still half-closes
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client, addr):
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        upstream.settimeout(TIMEOUT)
        try:
            upstream.connect((TARGET_HOST, TARGET_PORT))
        except OSError as e:
            print("[relay] %s:%d -> upstream %s:%d unreachable: %s"
                  % (addr[0], addr[1], TARGET_HOST, TARGET_PORT, e), flush=True)
            return
        client.settimeout(TIMEOUT)
        # Pump each direction on its own thread so they can close
        # independently: the board half-closes after its request, then reads
        # 192000 bytes of frame back.
        up = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
        up.start()
        _pump(upstream, client)
        up.join()
    finally:
        client.close()
        upstream.close()


def main():
    if LISTEN_PORT == TARGET_PORT:
        sys.exit("[relay] refusing to start: PORT and FLASK_PORT are both %d, "
                 "which would forward the relay to itself" % LISTEN_PORT)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(16)
    print("[relay] listening %s:%d -> %s:%d (python %s)"
          % (LISTEN_HOST, LISTEN_PORT, TARGET_HOST, TARGET_PORT,
             sys.version.split()[0]), flush=True)
    while True:
        try:
            client, addr = srv.accept()
        except OSError as e:
            print("[relay] accept failed: %s" % e, flush=True)
            continue
        threading.Thread(target=_handle, args=(client, addr), daemon=True).start()


if __name__ == "__main__":
    main()
