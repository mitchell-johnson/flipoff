#!/usr/bin/env python3
"""Tests for FlipFrame quiet-push behaviour.

Run:
  cd flipframe/cli && /opt/homebrew/bin/python3 -m pytest test_flipframe_push.py -q
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import flipframe


def sample_weather():
    return {
        "daily": {
            "time": ["2026-04-20", "2026-04-21"],
            "temperature_2m_max": [17, 18],
            "temperature_2m_min": [12, 11],
            "weathercode": [61, 3],
            "windspeed_10m_max": [14, 22],
            "winddirection_10m_dominant": [45, 270],
            "precipitation_probability_max": [100, 40],
        },
        "hourly": {
            "time": [
                "2026-04-20T06:00", "2026-04-20T09:00", "2026-04-20T12:00", "2026-04-20T15:00", "2026-04-20T18:00",
                "2026-04-21T06:00", "2026-04-21T09:00", "2026-04-21T12:00", "2026-04-21T15:00", "2026-04-21T18:00",
            ],
            "weathercode": [0, 2, 63, 80, 95, 45, 48, 51, 71, 0],
        },
    }


def make_args(*, quiet):
    return SimpleNamespace(tv_ip="192.168.1.81", quiet=quiet, clean=True)


def run_cmd_push(*, quiet, artmode_on=None, artmode_error=None):
    httpd = MagicMock()
    tv = MagicMock()
    tv.list_art.return_value = []
    tv.upload.return_value = "MY_F_TEST"

    if artmode_error is not None:
        tv.get_artmode.side_effect = artmode_error
    else:
        tv.get_artmode.return_value = artmode_on

    fake_capture = object()

    def fake_capture_screenshots(*args, **kwargs):
        return fake_capture

    with patch.object(flipframe, "fetch_weather", return_value={"daily": {"time": ["2026-04-06", "2026-04-07"]}}), \
         patch.object(flipframe, "generate_content", return_value={"pages": [{"lines": []}]}), \
         patch.object(flipframe, "start_server", return_value=httpd), \
         patch.object(flipframe, "capture_screenshots", new=fake_capture_screenshots), \
         patch.object(flipframe.asyncio, "run", return_value=[Path("/tmp/flipframe_1.png")]), \
         patch.object(flipframe, "FrameTVArt", return_value=tv), \
         patch.object(flipframe.time, "sleep"):
        flipframe.cmd_push(make_args(quiet=quiet))

    return tv, httpd


def test_quiet_push_does_not_interrupt_media_when_not_in_art_mode():
    tv, httpd = run_cmd_push(quiet=True, artmode_on=False)

    tv.set_artmode.assert_not_called()
    tv.select_image.assert_called_once_with("MY_F_TEST", show=False)
    httpd.shutdown.assert_called_once()


def test_quiet_push_updates_visible_art_when_art_mode_already_on():
    tv, _ = run_cmd_push(quiet=True, artmode_on=True)

    tv.set_artmode.assert_not_called()
    tv.select_image.assert_called_once_with("MY_F_TEST", show=True)


def test_quiet_push_fails_safe_when_art_mode_state_cannot_be_read():
    tv, _ = run_cmd_push(quiet=True, artmode_error=RuntimeError("boom"))

    tv.set_artmode.assert_not_called()
    tv.select_image.assert_called_once_with("MY_F_TEST", show=False)


def test_normal_push_still_forces_art_mode_before_showing_image():
    tv, _ = run_cmd_push(quiet=False, artmode_on=False)

    tv.set_artmode.assert_called_once_with(True)
    tv.select_image.assert_called_once_with("MY_F_TEST", show=True)


def test_build_weather_lines_puts_hour_labels_next_to_icons():
    lines = flipframe.build_weather_lines(sample_weather())

    assert lines[4] == ""
    assert lines[5] == flipframe.side_by_side("TODAY", "TOMORROW")
    assert lines[10] == flipframe.side_by_side("RAIN 100%", "RAIN 40%")
    assert lines[11] == flipframe.side_by_side(
        flipframe.format_forecast_icon_time_slots([
            flipframe.ICON_SUN,
            flipframe.ICON_PARTLY_CLOUDY,
            flipframe.ICON_SHOWERS,
            flipframe.ICON_THUNDERSTORM,
        ], flipframe.HOURLY_FORECAST_HOURS),
        flipframe.format_forecast_icon_time_slots([
            flipframe.ICON_FOG,
            flipframe.ICON_FOG,
            flipframe.ICON_SNOW,
            flipframe.ICON_SUN,
        ], flipframe.HOURLY_FORECAST_HOURS),
    )
