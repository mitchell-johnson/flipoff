#!/usr/bin/env python3
"""FlipFrame — Split-flap display content generator for Samsung Frame TV.

Uses direct websocket connection to the TV's art channel, bypassing the
samsungtvws library's broken sync open() which fails on 2020 Frame models
(expects ms.channel.ready as first event, but TV sends ms.channel.connect first).
"""

import argparse
import asyncio
import base64
import json
import os
import random
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import websocket

# --- Paths ---
CLI_DIR = Path(__file__).resolve().parent
PROJECT_DIR = CLI_DIR.parent
OUTPUT_DIR = CLI_DIR / "output"
TOKEN_FILE = CLI_DIR / ".tv-token"
ENV_FILE = CLI_DIR / ".env"


def load_env():
    """Load .env file into os.environ (simple key=value, no quotes handling needed)."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


load_env()

# Configuration — override via .env or environment variables
LATITUDE = float(os.environ.get("FLIPFRAME_LATITUDE", "-36.85"))
LONGITUDE = float(os.environ.get("FLIPFRAME_LONGITUDE", "174.76"))
TIMEZONE = os.environ.get("FLIPFRAME_TIMEZONE", "Pacific/Auckland")
LOCATION_NAME = os.environ.get("FLIPFRAME_LOCATION", "AUCKLAND, NZ")
DEFAULT_TV_IP = os.environ.get("FLIPFRAME_TV_IP", "192.168.1.100")

# --- WMO Weather Codes ---
WMO_CODES = {
    0: "CLEAR SKY", 1: "MAINLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
    45: "FOG", 48: "FREEZING FOG",
    51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE",
    56: "FREEZING DRIZZLE", 57: "HEAVY FREEZING DRIZZLE",
    61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    66: "FREEZING RAIN", 67: "HEAVY FREEZING RAIN",
    71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
    80: "LIGHT SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS",
    85: "LIGHT SNOW SHOWERS", 86: "HEAVY SNOW SHOWERS",
    95: "THUNDERSTORM", 96: "THUNDERSTORM W/ HAIL", 99: "SEVERE THUNDERSTORM",
}

# Weather icon characters (Unicode PUA — matched in JS WEATHER_ICONS)
ICON_SUN = "\uE001"           # ☀ clear/mainly clear
ICON_PARTLY_CLOUDY = "\uE002" # ⛅ partly cloudy
ICON_OVERCAST = "\uE003"      # ☁ overcast
ICON_FOG = "\uE004"           # 🌫 fog
ICON_DRIZZLE = "\uE005"       # 🌦 drizzle
ICON_RAIN = "\uE006"          # 🌧 rain
ICON_HEAVY_RAIN = "\uE007"    # 🌧🌧 heavy rain
ICON_SNOW = "\uE008"          # ❄ snow
ICON_THUNDERSTORM = "\uE009"  # ⛈ thunderstorm
ICON_SHOWERS = "\uE00A"       # 🌦 showers

# Temperature-colored digit characters (PUA \uE100+)
# Encoding: \uE100 + (temp_clamped * 10) + digit
# JS decodes: digit = (code - 0xE100) % 10, temp = floor((code - 0xE100) / 10)
def temp_digit(temp_value, digit_char):
    """Return a PUA character encoding both the digit and temperature for coloring."""
    temp_clamped = max(0, min(39, round(temp_value)))
    d = int(digit_char)
    return chr(0xE100 + temp_clamped * 10 + d)


def temp_str(temp_value):
    """Convert a temperature number to colored digit characters."""
    s = str(round(temp_value))
    return "".join(temp_digit(temp_value, c) for c in s)


# Map WMO code → icon character
WMO_ICONS = {
    0: ICON_SUN, 1: ICON_SUN, 2: ICON_PARTLY_CLOUDY, 3: ICON_OVERCAST,
    45: ICON_FOG, 48: ICON_FOG,
    51: ICON_DRIZZLE, 53: ICON_DRIZZLE, 55: ICON_DRIZZLE,
    56: ICON_DRIZZLE, 57: ICON_DRIZZLE,
    61: ICON_RAIN, 63: ICON_RAIN, 65: ICON_HEAVY_RAIN,
    66: ICON_RAIN, 67: ICON_HEAVY_RAIN,
    71: ICON_SNOW, 73: ICON_SNOW, 75: ICON_SNOW, 77: ICON_SNOW,
    80: ICON_SHOWERS, 81: ICON_SHOWERS, 82: ICON_HEAVY_RAIN,
    85: ICON_SNOW, 86: ICON_SNOW,
    95: ICON_THUNDERSTORM, 96: ICON_THUNDERSTORM, 99: ICON_THUNDERSTORM,
}


def wind_direction_str(degrees):
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(degrees / 45) % 8]


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# --- Weather ---

def fetch_weather():
    params = urllib.parse.urlencode({
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "daily": ",".join([
            "temperature_2m_max", "temperature_2m_min", "weathercode",
            "windspeed_10m_max", "winddirection_10m_dominant",
            "precipitation_probability_max",
        ]),
        "timezone": TIMEZONE, "forecast_days": 2,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    print("Fetching weather data...")
    req = urllib.request.Request(url, headers={"User-Agent": "FlipFrame/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


CONTENT_COLS = 22
CONTENT_ROWS = 12
GRID_COLS = 30
GRID_ROWS = 17


def pad_center(text, width):
    """Center text in a field of given width."""
    text = text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (width - left - len(text))


def side_by_side(left, right, col_width=11):
    """Place two strings side by side in the grid."""
    l = pad_center(left[:col_width], col_width)
    r = pad_center(right[:col_width], col_width)
    return l + r


# Abbreviations for weather descriptions that exceed 11 chars
DESC_SHORT = {
    "LIGHT SHOWERS": "LT SHOWERS",
    "HEAVY SHOWERS": "HVY SHOWERS",
    "LIGHT DRIZZLE": "LT DRIZZLE",
    "HEAVY DRIZZLE": "HVY DRIZZLE",
    "LIGHT RAIN": "LIGHT RAIN",
    "HEAVY RAIN": "HEAVY RAIN",
    "FREEZING FOG": "FRZNG FOG",
    "FREEZING RAIN": "FRZNG RAIN",
    "FREEZING DRIZZLE": "FRZNG DRZL",
    "HEAVY FREEZING DRIZZLE": "HVY FRZ DRZ",
    "HEAVY FREEZING RAIN": "HVY FRZ RN",
    "LIGHT SNOW SHOWERS": "LT SNOW SHW",
    "HEAVY SNOW SHOWERS": "HVY SNW SHW",
    "THUNDERSTORM W/ HAIL": "T-STRM HAIL",
    "SEVERE THUNDERSTORM": "SVR T-STORM",
    "THUNDERSTORM": "T-STORM",
    "PARTLY CLOUDY": "PTLY CLOUDY",
    "MAINLY CLEAR": "MOSTLY CLR",
}


def short_desc(desc):
    """Shorten a weather description to fit 11 chars."""
    if len(desc) <= 11:
        return desc
    return DESC_SHORT.get(desc, desc[:11])


def pad_to_grid(lines, content_cols, grid_cols, grid_rows):
    """Center content lines within a larger grid, padding with empty rows/cols."""
    content_rows = len(lines)
    top_pad = (grid_rows - content_rows) // 2
    col_pad = (grid_cols - content_cols) // 2
    pad_str = " " * col_pad

    padded = []
    # Top empty rows
    for _ in range(top_pad):
        padded.append("")
    # Content rows with horizontal padding
    for line in lines:
        # Center the content within the wider grid
        centered = pad_center(line, content_cols)
        padded.append(pad_str + centered + pad_str)
    # Bottom empty rows to fill grid
    while len(padded) < grid_rows:
        padded.append("")

    return padded[:grid_rows]


def generate_content(weather_data=None):
    if weather_data:
        today_date = datetime.strptime(weather_data["daily"]["time"][0], "%Y-%m-%d")
        tomorrow_date = datetime.strptime(weather_data["daily"]["time"][1], "%Y-%m-%d")
    else:
        today_date = datetime.now()
        tomorrow_date = today_date + timedelta(days=1)

    day_name = today_date.strftime("%A").upper()
    date_str = f"{today_date.strftime('%B').upper()} {today_date.day}, {today_date.year}"

    if weather_data:
        daily = weather_data["daily"]

        def weather_for(i):
            return {
                "high": round(daily["temperature_2m_max"][i]),
                "low": round(daily["temperature_2m_min"][i]),
                "desc": WMO_CODES.get(daily["weathercode"][i], "UNKNOWN"),
                "wind": round(daily["windspeed_10m_max"][i]),
                "wdir": wind_direction_str(daily["winddirection_10m_dominant"][i]),
                "rain": round(daily["precipitation_probability_max"][i]),
            }

        t = weather_for(0)
        m = weather_for(1)

        # Get weather icon characters
        t_icon = WMO_ICONS.get(daily["weathercode"][0], ICON_OVERCAST)
        m_icon = WMO_ICONS.get(daily["weathercode"][1], ICON_OVERCAST)

        lines = [
            day_name,
            date_str,
            LOCATION_NAME,
            "",
            side_by_side(t_icon, m_icon),
            side_by_side("TODAY", "TOMORROW"),
            side_by_side(f"HIGH {temp_str(t['high'])}", f"HIGH {temp_str(m['high'])}"),
            side_by_side(f"LOW {temp_str(t['low'])}", f"LOW {temp_str(m['low'])}"),
            side_by_side(short_desc(t["desc"]), short_desc(m["desc"])),
            side_by_side(f"{t['wind']}KM/H {t['wdir']}", f"{m['wind']}KM/H {m['wdir']}"),
            side_by_side(f"RAIN {t['rain']}%", f"RAIN {m['rain']}%"),
        ]
    else:
        lines = [
            day_name,
            date_str,
            "",
            LOCATION_NAME,
            "", "", "", "", "", "", "",
        ]

    # Pad content into larger grid with empty tile border
    padded = pad_to_grid(lines, CONTENT_COLS, GRID_COLS, GRID_ROWS)
    return {"pages": [{"lines": padded}], "interval": 999999}


# --- HTTP Server ---

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def start_server(port=8765, bind="127.0.0.1"):
    os.chdir(PROJECT_DIR)
    httpd = HTTPServer((bind, port), QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

def build_kiosk_url(content, host="127.0.0.1", port=8765):
    data_b64 = base64.b64encode(json.dumps(content).encode()).decode()
    return f"http://{host}:{port}/kiosk.html?data={data_b64}"


# --- Samsung Frame TV Direct Websocket API ---

class FrameTVArt:
    """Direct websocket connection to Samsung Frame TV art channel.

    Bypasses samsungtvws library which has a bug in SamsungTVArt.open()
    that fails on 2020 models (reads one ws message, expects ms.channel.ready
    but gets ms.channel.connect first).
    """

    def __init__(self, tv_ip, token_file=None, timeout=30):
        self.tv_ip = tv_ip
        self.timeout = timeout
        self.token_file = token_file or str(TOKEN_FILE)
        self.ws = None

    def _get_token(self):
        try:
            with open(self.token_file) as f:
                return f.read().strip()
        except OSError:
            return ""

    def connect(self):
        token = self._get_token()
        name = base64.b64encode(b"SamsungTvRemote").decode()
        url = f"wss://{self.tv_ip}:8002/api/v2/channels/com.samsung.art-app?name={name}"
        if token:
            url += f"&token={token}"

        self.ws = websocket.create_connection(
            url,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            timeout=self.timeout,
        )

        # Drain connect + ready events (2020 models send both)
        events_seen = set()
        for _ in range(5):
            try:
                self.ws.settimeout(5)
                data = json.loads(self.ws.recv())
                event = data.get("event", "")
                events_seen.add(event)

                # Save token if provided
                if event == "ms.channel.connect":
                    clients = data.get("data", {}).get("clients", [])
                    for c in clients:
                        t = c.get("attributes", {}).get("token")
                        if t:
                            with open(self.token_file, "w") as f:
                                f.write(t)

                if "ms.channel.ready" in events_seen:
                    break
            except websocket.WebSocketTimeoutException:
                break

        self.ws.settimeout(self.timeout)
        if "ms.channel.ready" not in events_seen:
            raise ConnectionError(f"TV did not send ready event. Got: {events_seen}")

        print(f"  Connected to art channel on {self.tv_ip}")

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _send_request(self, request_data):
        req_id = request_data.get("id") or str(uuid.uuid4())
        request_data["id"] = req_id
        request_data["request_id"] = req_id

        self.ws.send(json.dumps({
            "method": "ms.channel.emit",
            "params": {
                "event": "art_app_request",
                "to": "host",
                "data": json.dumps(request_data),
            }
        }))
        return req_id

    def _wait_for(self, req_id=None, event_name=None):
        while True:
            raw = self.ws.recv()
            resp = json.loads(raw)
            if resp.get("event") == "d2d_service_message":
                d = json.loads(resp["data"])
                sub = d.get("event", "")
                rid = d.get("request_id", d.get("id"))

                if sub == "error":
                    raise RuntimeError(f"TV error: {d.get('error_code')} — {d}")

                if event_name and sub == event_name:
                    return d
                if req_id and rid == req_id and not event_name:
                    return d

    def get_artmode(self):
        req_id = self._send_request({"request": "get_artmode_status"})
        data = self._wait_for(req_id)
        return data.get("value") == "on"

    def set_artmode(self, on=True):
        self._send_request({
            "request": "set_artmode_status",
            "value": "on" if on else "off",
        })

    def list_art(self):
        req_id = self._send_request({"request": "get_content_list", "category": None})
        data = self._wait_for(req_id)
        return json.loads(data["content_list"])

    def upload(self, image_path):
        with open(image_path, "rb") as f:
            img_data = f.read()

        file_type = Path(image_path).suffix.lstrip(".").lower()
        if file_type == "jpeg":
            file_type = "jpg"

        art_uuid = str(uuid.uuid4())
        self._send_request({
            "request": "send_image",
            "file_type": file_type,
            "id": art_uuid,
            "request_id": art_uuid,
            "conn_info": {
                "d2d_mode": "socket",
                "connection_id": random.randrange(4 * 1024 * 1024 * 1024),
                "id": art_uuid,
            },
            "image_date": datetime.now().strftime("%Y:%m:%d %H:%M:%S"),
            "matte_id": "none",
            "portrait_matte_id": "none",
            "file_size": len(img_data),
        })

        # Wait for ready_to_use
        data = self._wait_for(event_name="ready_to_use")
        conn_info = json.loads(data["conn_info"])

        # Upload via raw socket
        header = json.dumps({
            "num": 0, "total": 1,
            "fileLength": len(img_data),
            "fileName": Path(image_path).stem,
            "fileType": file_type,
            "secKey": conn_info["key"],
            "version": "0.0.1",
        })

        secured = conn_info.get("secured", False)
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if secured:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            art_sock = ctx.wrap_socket(raw_sock)
        else:
            art_sock = raw_sock

        art_sock.connect((conn_info["ip"], int(conn_info["port"])))
        art_sock.send(len(header).to_bytes(4, "big"))
        art_sock.send(header.encode("ascii"))

        CHUNK = 64 * 1024
        for i in range(0, len(img_data), CHUNK):
            art_sock.send(img_data[i:i + CHUNK])
        art_sock.close()

        # Wait for confirmation
        data = self._wait_for(event_name="image_added")
        return data["content_id"]

    def select_image(self, content_id, show=True):
        self._send_request({
            "request": "select_image",
            "content_id": content_id,
            "show": show,
        })

    def set_slideshow(self, duration_min=1, shuffle=False, category=2):
        """Enable slideshow. duration_min=0 for off. category: 2=my photos, 4=favourites.

        Tries set_slideshow_status (newer models) then falls back to
        set_auto_rotation_status (2020 and older models).
        """
        req_data = {
            "value": str(duration_min) if duration_min > 0 else "off",
            "category_id": f"MY-C000{category}",
            "type": "shuffleslideshow" if shuffle else "slideshow",
        }

        # Try newer API first
        try:
            req_data["request"] = "set_slideshow_status"
            rid = self._send_request(dict(req_data))
            self._wait_for(rid)
        except RuntimeError:
            # Fall back to older API (2020 models)
            req_data["request"] = "set_auto_rotation_status"
            self._send_request(dict(req_data))

    def delete_list(self, content_ids):
        id_list = [{"content_id": cid} for cid in content_ids]
        self._send_request({
            "request": "delete_image_list",
            "content_id_list": id_list,
        })


# --- Screenshots ---

async def capture_screenshots(content, port=8765):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Error: playwright not installed.")
        print("  pip install playwright && playwright install chromium")
        sys.exit(1)

    OUTPUT_DIR.mkdir(exist_ok=True)
    frozen = {**content, "interval": 999999}
    url = build_kiosk_url(frozen, "127.0.0.1", port)
    screenshots = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 3840, "height": 2160},
            device_scale_factor=1,
        )
        await page.goto(url)
        await page.wait_for_function("window.flipframe !== undefined", timeout=10000)

        total = await page.evaluate("window.flipframe.getTotalPages()")
        print(f"Capturing {total} page(s) at 3840x2160...")

        for i in range(total):
            if i > 0:
                await page.evaluate("window.flipframe.nextPage()")
            await page.wait_for_function("!window.flipframe.isTransitioning()", timeout=15000)
            await page.wait_for_timeout(12000)

            filepath = OUTPUT_DIR / f"flipframe_{i + 1}.png"
            await page.screenshot(path=str(filepath))
            screenshots.append(filepath)
            print(f"  Captured: {filepath.name}")

        await browser.close()

    return screenshots


# --- Commands ---

def cmd_push(args):
    """Generate content, capture screenshots, upload to TV art mode."""
    weather = fetch_weather()
    content = generate_content(weather)
    print(f"Generated {len(content['pages'])} page(s)")

    port = 8765
    httpd = start_server(port)

    try:
        screenshots = asyncio.run(capture_screenshots(content, port))
    finally:
        httpd.shutdown()

    # Connect to TV
    tv = FrameTVArt(args.tv_ip)
    print(f"Connecting to TV at {args.tv_ip}...")
    tv.connect()

    try:
        # Only force art mode if not in quiet mode
        if not getattr(args, 'quiet', False):
            if not tv.get_artmode():
                print("  Switching TV to art mode...")
                tv.set_artmode(True)
                time.sleep(3)
        else:
            # In quiet mode, check if TV is reachable on art channel but don't switch modes
            try:
                tv.get_artmode()
            except Exception:
                pass

        # Clean up old flipframe uploads
        existing = tv.list_art()
        old_ids = [a["content_id"] for a in existing
                   if a["content_id"].startswith("MY_F") and a.get("category_id") == "MY-C0002"]
        if old_ids and args.clean:
            print(f"  Removing {len(old_ids)} old image(s)...")
            tv.delete_list(old_ids)
            time.sleep(1)

        # Upload new screenshot(s)
        uploaded = []
        for ss in screenshots:
            print(f"  Uploading {ss.name}...")
            cid = tv.upload(str(ss))
            uploaded.append(cid)
            print(f"    → {cid}")

        # Select image as current art (so it shows when art mode is next active)
        if uploaded:
            tv.select_image(uploaded[0])
            print(f"  Set {uploaded[0]} as current art")

        # Disable rotation if only one image
        if len(uploaded) == 1:
            try:
                tv.set_slideshow(duration_min=0)
            except Exception:
                pass

        print(f"\n✅ Done! {'Quietly pushed' if getattr(args, 'quiet', False) else 'Pushed to art mode'}.")
    finally:
        tv.close()


def cmd_artmode(args):
    """Wake the TV and switch to art mode (no content generation)."""
    import subprocess

    # Wake-on-LAN — send magic packet to TV MAC
    mac = "f4:fe:fb:ae:6b:64"
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, ("255.255.255.255", 9))
    print("Sent Wake-on-LAN packet")

    # Wait for TV to be reachable
    print(f"Waiting for TV at {args.tv_ip}...")
    for i in range(args.timeout):
        try:
            with socket.create_connection((args.tv_ip, 8002), timeout=2):
                break
        except (ConnectionRefusedError, OSError, TimeoutError):
            time.sleep(1)
    else:
        print(f"TV not reachable after {args.timeout}s — may already be in art mode or fully off")
        sys.exit(1)

    time.sleep(3)  # let websocket service stabilise

    tv = FrameTVArt(args.tv_ip)
    print(f"Connecting to TV at {args.tv_ip}...")
    tv.connect()

    try:
        if tv.get_artmode():
            print("Already in art mode ✅")
        else:
            print("Switching to art mode...")
            tv.set_artmode(True)
            time.sleep(2)
            print("Art mode enabled ✅")
    finally:
        tv.close()


def cmd_live(args):
    """Serve kiosk page on LAN and open in TV browser (animated)."""
    weather = None
    try:
        weather = fetch_weather()
    except Exception as e:
        print(f"Warning: Could not fetch weather ({e}), using date only")

    content = generate_content(weather)
    local_ip = get_local_ip()
    port = args.port

    httpd = start_server(port, bind="0.0.0.0")
    url = build_kiosk_url(content, local_ip, port)

    print(f"Serving on http://{local_ip}:{port}/")

    # Open on TV via samsungtvws remote (this part works fine)
    try:
        from samsungtvws import SamsungTVWS
        tv = SamsungTVWS(host=args.tv_ip, port=8002, token_file=str(TOKEN_FILE))
        tv.open_browser(url)
        print(f"  Opened on TV at {args.tv_ip}")
    except Exception as e:
        print(f"Could not open on TV: {e}")
        print(f"  Open manually: {url}")

    print("\nLive display running. Press Ctrl+C to stop.")

    def handle_signal(sig, frame):
        print("\nStopping...")
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while True:
        time.sleep(1)


def cmd_preview(args):
    """Open kiosk page in local browser."""
    weather = None
    try:
        weather = fetch_weather()
    except Exception as e:
        print(f"Warning: Could not fetch weather ({e}), using date only")

    content = generate_content(weather)
    port = 8765
    httpd = start_server(port)
    url = build_kiosk_url(content, "127.0.0.1", port)

    print(f"Opening: {url}")
    webbrowser.open(url)
    print("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()


def cmd_generate(args):
    """Generate 4K screenshots only."""
    weather = fetch_weather()
    content = generate_content(weather)
    print(f"Generated {len(content['pages'])} page(s)")

    port = 8765
    httpd = start_server(port)
    try:
        screenshots = asyncio.run(capture_screenshots(content, port))
        print(f"\nScreenshots saved to: {OUTPUT_DIR}")
        for s in screenshots:
            print(f"  {s}")
    finally:
        httpd.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="FlipFrame — Split-flap display for Samsung Frame TV",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    push_p = sub.add_parser("push", help="Generate screenshots & push to TV art mode")
    push_p.add_argument("--tv-ip", default=DEFAULT_TV_IP, help=f"TV IP (default: {DEFAULT_TV_IP})")
    push_p.add_argument("--no-clean", dest="clean", action="store_false", help="Don't remove old uploads")
    push_p.add_argument("--quiet", action="store_true", help="Upload without switching TV to art mode")

    live_p = sub.add_parser("live", help="Serve live animated display on TV browser")
    live_p.add_argument("--tv-ip", default=DEFAULT_TV_IP, help=f"TV IP (default: {DEFAULT_TV_IP})")
    live_p.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")

    art_p = sub.add_parser("artmode", help="Wake TV and switch to art mode (no content generation)")
    art_p.add_argument("--tv-ip", default=DEFAULT_TV_IP, help=f"TV IP (default: {DEFAULT_TV_IP})")
    art_p.add_argument("--timeout", type=int, default=30, help="Seconds to wait for TV (default: 30)")

    sub.add_parser("preview", help="Open in local browser")
    sub.add_parser("generate", help="Generate 4K screenshots to cli/output/")

    args = parser.parse_args()

    cmds = {"push": cmd_push, "live": cmd_live, "artmode": cmd_artmode, "preview": cmd_preview, "generate": cmd_generate}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
