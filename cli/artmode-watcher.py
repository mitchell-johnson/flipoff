#!/usr/bin/env python3
"""Watch Samsung Frame TV power state and switch to art mode when turned off.

Uses the TV's REST API (same approach as Home Assistant's Samsung TV integration)
to reliably detect power state changes. Polls http(s)://{TV_IP}:8002/api/v2/ for
device.PowerState — this is far more reliable than TCP port probing + ping,
which can false-trigger during WiFi blips while the TV is actively in use.

State detection flow:
  1. Poll REST API every POLL_INTERVAL seconds
  2. PowerState == "on" → TV is on, do nothing
  3. PowerState != "on" or unreachable → start offline confirmation timer
  4. If TV stays non-"on" for OFFLINE_CONFIRM seconds → send WoL + switch to art mode

Also refreshes weather content every 30 minutes (quiet push — no mode switch).

Run as a launchd daemon — see com.flipframe.artmode-watcher.plist
"""

import json
import math
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# --- Config ---
TV_IP = os.environ.get("FLIPFRAME_TV_IP", "192.168.1.81")
TV_MAC = os.environ.get("FLIPFRAME_TV_MAC", "f4:fe:fb:ae:6b:64")
TV_PORT = 8002

POLL_INTERVAL = 10         # seconds between REST API checks (matches HA's SCAN_INTERVAL)
OFFLINE_CONFIRM = 30       # seconds TV must stay non-"on" before we act
WOL_ATTEMPTS = 5           # number of WoL packets to send
WOL_RETRY_DELAY = 5        # seconds between WoL bursts (wall-clock)
WAKE_TIMEOUT = 60          # max seconds to wait for TV to come back after WoL
COOLDOWN_SECONDS = 120     # ignore repeated off→on cycles within this window
ARTMODE_SETTLE = 15        # seconds to wait after TV reachable before art mode command
ARTMODE_RETRIES = 3        # retry art mode if TV isn't fully ready yet
ARTMODE_RETRY_DELAY = 10   # seconds between retries
REFRESH_INTERVAL = 30 * 60 # 30 minutes
REST_TIMEOUT = 5           # seconds for REST API request timeout

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tv-token")
CLI_DIR = os.path.dirname(os.path.abspath(__file__))
FLIPFRAME = os.path.join(CLI_DIR, "flipframe.py")

# Thread safety: prevents watch loop and refresh loop from hitting the TV simultaneously
_tv_lock = threading.Lock()

# Cached REST API scheme (HTTPS or HTTP) — detected on first successful probe
_rest_scheme = None


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --- REST API State Detection (Home Assistant approach) ---

def _make_ssl_context():
    """Create a permissive SSL context for the TV's self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _probe_rest_url():
    """Detect whether the TV serves REST API over HTTPS or HTTP. Cache the result."""
    global _rest_scheme
    if _rest_scheme:
        return f"{_rest_scheme}://{TV_IP}:{TV_PORT}/api/v2/"

    ctx = _make_ssl_context()
    for scheme in ("https", "http"):
        try:
            url = f"{scheme}://{TV_IP}:{TV_PORT}/api/v2/"
            req = urllib.request.Request(url, headers={"User-Agent": "FlipFrame/1.0"})
            urllib.request.urlopen(req, timeout=3, context=ctx)
            _rest_scheme = scheme
            log(f"REST API detected at {scheme}://{TV_IP}:{TV_PORT}")
            return url
        except Exception:
            continue

    # Default to HTTPS if probe fails (TV may be off)
    return f"https://{TV_IP}:{TV_PORT}/api/v2/"


def get_tv_power_state():
    """Query the TV's REST API for PowerState.

    Returns:
        "on" if TV is on and REST API reports PowerState as "on"
        "off" if REST API is reachable but PowerState is not "on"
        "unreachable" if REST API cannot be reached (TV is off or network issue)

    This mirrors Home Assistant's SamsungTVWSBridge.async_is_on() which checks
    device_info["device"]["PowerState"] == "on" as the primary detection method.
    """
    rest_url = _probe_rest_url()
    ctx = _make_ssl_context()

    try:
        req = urllib.request.Request(rest_url, headers={"User-Agent": "FlipFrame/1.0"})
        with urllib.request.urlopen(req, timeout=REST_TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, TimeoutError):
        return "unreachable"
    except (json.JSONDecodeError, ValueError) as e:
        log(f"WARNING: REST API returned unparseable response: {e}")
        return "unreachable"

    power_state = data.get("device", {}).get("PowerState", "unknown")
    return "on" if power_state == "on" else "off"


def tv_is_reachable():
    """Check if the TV's websocket port is accepting connections."""
    try:
        with socket.create_connection((TV_IP, TV_PORT), timeout=3):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


# --- WoL & Art Mode ---

def send_wol(count=1):
    """Send Wake-on-LAN magic packet(s)."""
    mac_bytes = bytes.fromhex(TV_MAC.replace(":", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    for _ in range(count):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(magic, ("255.255.255.255", 9))
            s.sendto(magic, ("192.168.1.255", 9))


def make_env():
    """Build environment with correct PATH for subprocess calls."""
    return {
        **os.environ,
        "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH', '')}",
    }


def switch_to_artmode():
    """Connect to TV and switch to art mode."""
    log("Switching to art mode...")
    try:
        result = subprocess.run(
            [sys.executable, FLIPFRAME, "artmode", "--tv-ip", TV_IP, "--timeout", "30"],
            capture_output=True, text=True, timeout=60, env=make_env(),
        )
        if result.returncode == 0:
            log(f"Art mode activated: {result.stdout.strip()}")
            return True
        else:
            log(f"Art mode failed (exit {result.returncode}): {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        log("Art mode command timed out after 60s")
        return False
    except Exception as e:
        log(f"Error running artmode: {e}")
        return False


def quiet_push():
    """Regenerate weather content and push to TV without switching to art mode."""
    state = get_tv_power_state()
    if state == "unreachable":
        log("TV unreachable, skipping weather refresh (will retry next cycle)")
        return
    log("Refreshing weather content (quiet push)...")
    try:
        with _tv_lock:
            result = subprocess.run(
                [sys.executable, FLIPFRAME, "push", "--tv-ip", TV_IP, "--quiet"],
                capture_output=True, text=True, timeout=120, env=make_env(),
            )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            log(f"Weather refreshed: {lines[-1] if lines else '(no output)'}")
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


def wait_for_tv(timeout=WAKE_TIMEOUT):
    """Send WoL and wait for TV to become reachable. Returns True if TV came up."""
    log(f"Sending WoL packets and waiting up to {timeout}s for TV...")
    deadline = time.time() + timeout
    next_wol = 0

    while time.time() < deadline:
        now = time.time()
        if now >= next_wol:
            log(f"  WoL burst ({WOL_ATTEMPTS} packets)")
            send_wol(count=WOL_ATTEMPTS)
            next_wol = now + WOL_RETRY_DELAY

        if tv_is_reachable():
            elapsed = int(time.time() + timeout - deadline)
            log(f"  TV responded after {elapsed}s")
            return True
        time.sleep(1)

    log(f"  TV did not respond within {timeout}s")
    return False


def confirm_offline(seconds=OFFLINE_CONFIRM):
    """Confirm TV stays non-"on" for the given duration using REST API.

    Returns True if TV stayed off the entire time (real power-off).
    Returns False if TV came back to "on" state (was just a blip).
    """
    log(f"  Confirming TV stays off for {seconds}s...")
    checks = math.ceil(seconds / POLL_INTERVAL)
    for i in range(checks):
        time.sleep(POLL_INTERVAL)
        state = get_tv_power_state()
        if state == "on":
            elapsed = (i + 1) * POLL_INTERVAL
            log(f"  TV came back to 'on' after {elapsed}s — was just a blip")
            return False
        log(f"  Confirmation check {i + 1}/{checks}: state={state}")
    return True


# --- Main Watch Loop ---

def watch():
    """Main polling loop — detect TV power off via REST API and switch to art mode."""
    rest_url = _probe_rest_url()
    log(f"Polling TV REST API at {rest_url} every {POLL_INTERVAL}s")
    log(f"Offline confirmation: {OFFLINE_CONFIRM}s before acting")

    prev_state = get_tv_power_state()
    last_artmode_time = 0
    heartbeat_counter = 0
    log(f"Initial state: {prev_state}")

    while True:
        time.sleep(POLL_INTERVAL)
        heartbeat_counter += 1

        # Hourly heartbeat log
        if heartbeat_counter % 360 == 0:
            log(f"Heartbeat: running, state={prev_state}")

        state = get_tv_power_state()

        # Detect transition: "on" → not "on" (TV may have been turned off)
        if prev_state == "on" and state != "on":
            now = time.time()
            log(f"TV state changed: on → {state}")

            if now - last_artmode_time < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - (now - last_artmode_time))
                log(f"  Cooldown active ({remaining}s left), skipping")
                prev_state = state
                continue

            # Confirm the TV stays off — filters out brief blips
            if not confirm_offline():
                prev_state = get_tv_power_state()
                continue

            log("  TV confirmed off — attempting wake to art mode")

            # TV has been off for OFFLINE_CONFIRM seconds — wake it
            with _tv_lock:
                if wait_for_tv():
                    log(f"  Waiting {ARTMODE_SETTLE}s for art channel to stabilise...")
                    time.sleep(ARTMODE_SETTLE)

                    success = False
                    for attempt in range(1, ARTMODE_RETRIES + 1):
                        if switch_to_artmode():
                            success = True
                            break
                        if attempt < ARTMODE_RETRIES:
                            log(f"  Retry {attempt}/{ARTMODE_RETRIES} in {ARTMODE_RETRY_DELAY}s...")
                            time.sleep(ARTMODE_RETRY_DELAY)

                    if success:
                        last_artmode_time = time.time()
                        time.sleep(5)
                        quiet_push()
                    else:
                        log("  Art mode switch failed after all retries")
                else:
                    log("  Could not wake TV — WoL may be disabled or TV is fully powered off")

            state = get_tv_power_state()

        elif prev_state != "on" and state == "on":
            log(f"TV came online: {prev_state} → on")

        prev_state = state


def main():
    log("FlipFrame art mode watcher starting (REST API polling mode)")

    refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
    refresh_thread.start()

    try:
        watch()
    except KeyboardInterrupt:
        log("Shutting down")
    except Exception as e:
        log(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
