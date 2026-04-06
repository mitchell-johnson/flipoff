#!/usr/bin/env python3
"""Tests for FlipFrame quiet-push behaviour.

Run:
  cd flipframe/cli && /opt/homebrew/bin/python3 -m pytest test_flipframe_push.py -q
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import flipframe


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
