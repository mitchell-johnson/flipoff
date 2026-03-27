#!/usr/bin/env python3
"""Watch Home Assistant for TV power-off events and switch to art mode.
Also refreshes weather content every 30 minutes (quiet push — no mode switch).

Connects to HA's websocket API, subscribes to state changes for the
media_player entity, and triggers art mode when the TV turns off
(e.g. via Apple TV CEC power-off).

Run as a launchd daemon — see com.flipframe.artmode-watcher.plist
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import websocket

# --- Config ---
HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJiM2Q1OWUyZGMyNzQ0NjRmOTRkMThmMTc0MWYxZWQ3NyIsImlhdCI6MTc3MTYxNzU3MSwiZXhwIjoyMDg2OTc3NTcxfQ.TybEr4MGGnpKG_X2lj8OVlifJtliKkeZcnrbuAr_eY0")
TV_ENTITY = os.environ.get("TV_ENTITY", "media_player.master_bedroom_tv")
TV_IP = os.environ.get("FLIPFRAME_TV_IP", "192.168.1.100")
TV_MAC = "f4:fe:fb:ae:6b:64"
DELAY_SECONDS = 8  # wait after TV off before switching to art mode
COOLDOWN_SECONDS = 60  # ignore repeated off events within this window
REFRESH_INTERVAL = 30 * 60  # 30 minutes in seconds

CLI_DIR = os.path.dirname(os.path.abspath(__file__))
FLIPFRAME = os.path.join(CLI_DIR, "flipframe.py")


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def send_wol():
    """Send Wake-on-LAN magic packet."""
    mac_bytes = bytes.fromhex(TV_MAC.replace(":", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, ("255.255.255.255", 9))


def make_env():
    """Build environment with correct PATH/PYTHONPATH for subprocess calls."""
    return {
        **os.environ,
        "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"/Users/mitchell/Library/Python/3.14/lib/python/site-packages:{os.environ.get('PYTHONPATH', '')}",
    }


def switch_to_artmode():
    """Wake TV and switch to art mode."""
    log("Sending WoL and switching to art mode...")
    try:
        result = subprocess.run(
            [sys.executable, FLIPFRAME, "artmode", "--tv-ip", TV_IP, "--timeout", "30"],
            capture_output=True, text=True, timeout=60, env=make_env(),
        )
        if result.returncode == 0:
            log(f"Art mode activated: {result.stdout.strip()}")
        else:
            log(f"Art mode failed (exit {result.returncode}): {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log("Art mode command timed out after 60s")
    except Exception as e:
        log(f"Error running artmode: {e}")


def tv_is_reachable():
    """Quick check if the TV's websocket port is open."""
    try:
        with socket.create_connection((TV_IP, 8002), timeout=3):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def quiet_push():
    """Regenerate weather content and push to TV without switching to art mode.
    Skips if TV is off/unreachable — the image will be pushed on next cycle when TV is on."""
    if not tv_is_reachable():
        log("TV unreachable, skipping weather refresh (will retry next cycle)")
        return
    log("Refreshing weather content (quiet push)...")
    try:
        result = subprocess.run(
            [sys.executable, FLIPFRAME, "push", "--tv-ip", TV_IP, "--quiet"],
            capture_output=True, text=True, timeout=120, env=make_env(),
        )
        if result.returncode == 0:
            log(f"Weather refreshed: {result.stdout.strip().splitlines()[-1]}")
        else:
            log(f"Quiet push failed (exit {result.returncode}): {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        log("Quiet push timed out after 120s")
    except Exception as e:
        log(f"Error during quiet push: {e}")


def refresh_loop():
    """Background thread: refresh weather content every REFRESH_INTERVAL seconds."""
    log(f"Weather refresh loop started (every {REFRESH_INTERVAL // 60} min)")
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            quiet_push()
        except Exception as e:
            log(f"Refresh loop error: {e}")


def watch():
    """Connect to HA websocket and watch for TV state changes."""
    ws_url = HA_URL.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
    log(f"Connecting to {ws_url}")

    ws = websocket.create_connection(ws_url, timeout=30)
    msg_id = 1
    last_artmode = 0

    try:
        # Step 1: Receive auth_required
        resp = json.loads(ws.recv())
        if resp.get("type") != "auth_required":
            log(f"Unexpected first message: {resp}")
            return

        # Step 2: Authenticate
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        resp = json.loads(ws.recv())
        if resp.get("type") != "auth_ok":
            log(f"Auth failed: {resp}")
            return
        log("Authenticated with Home Assistant")

        # Step 3: Subscribe to state changes
        ws.send(json.dumps({
            "id": msg_id,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))
        resp = json.loads(ws.recv())
        if not resp.get("success"):
            log(f"Subscribe failed: {resp}")
            return
        log(f"Subscribed to state_changed events, watching {TV_ENTITY}")
        msg_id += 1

        # Step 4: Listen for events
        while True:
            raw = ws.recv()
            msg = json.loads(raw)

            if msg.get("type") != "event":
                continue

            event = msg.get("event", {})
            data = event.get("data", {})

            if data.get("entity_id") != TV_ENTITY:
                continue

            old_state = data.get("old_state", {}).get("state", "unknown")
            new_state = data.get("new_state", {}).get("state", "unknown")

            if old_state == new_state:
                continue

            log(f"TV state: {old_state} → {new_state}")

            if new_state == "off" and old_state != "off":
                now = time.time()
                if now - last_artmode < COOLDOWN_SECONDS:
                    log(f"Cooldown active ({int(COOLDOWN_SECONDS - (now - last_artmode))}s left), skipping")
                    continue

                log(f"TV turned off — waiting {DELAY_SECONDS}s before switching to art mode...")
                time.sleep(DELAY_SECONDS)
                switch_to_artmode()
                last_artmode = time.time()
                # Push fresh weather now that TV is awake in art mode
                time.sleep(5)
                quiet_push()

    except (websocket.WebSocketConnectionClosedException, ConnectionError) as e:
        log(f"Connection lost: {e}")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def main():
    log("FlipFrame art mode watcher starting")

    # Start weather refresh loop in background thread
    refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
    refresh_thread.start()

    while True:
        try:
            watch()
        except Exception as e:
            log(f"Watcher error: {e}")
        log("Reconnecting in 10s...")
        time.sleep(10)


if __name__ == "__main__":
    main()
