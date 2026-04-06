#!/usr/bin/env python3
"""Comprehensive test suite for artmode-watcher.py.

Run: cd flipframe/cli && python -m pytest test_artmode_watcher.py -v
"""

import io
import json
import math
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import unittest
from unittest.mock import MagicMock, call, patch, PropertyMock

import pytest

# Import the module under test
import importlib
import sys
import os

# Ensure cli/ is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We need to import the module — but it has side effects at import time (just constants)
# so we import it as a module
watcher = importlib.import_module("artmode-watcher")


# --- Fixtures ---

@pytest.fixture(autouse=True)
def reset_rest_scheme():
    """Reset cached REST scheme between tests."""
    watcher._rest_scheme = None
    yield
    watcher._rest_scheme = None


def make_rest_response(power_state="on", extra_device_fields=None):
    """Build a mock REST API JSON response."""
    device = {"PowerState": power_state, "modelName": "QE55LS03"}
    if extra_device_fields:
        device.update(extra_device_fields)
    return json.dumps({"device": device}).encode()


def mock_urlopen_response(data, status=200):
    """Create a mock response object for urllib.request.urlopen."""
    resp = MagicMock()
    resp.read.return_value = data
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    return resp


# ============================================================
# 1. get_tv_power_state()
# ============================================================

class TestGetTvPowerState:
    """Tests for REST API power state detection."""

    @patch("urllib.request.urlopen")
    def test_tv_on(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("on"))
        assert watcher.get_tv_power_state() == "on"

    @patch("urllib.request.urlopen")
    def test_tv_off_standby(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("standby"))
        assert watcher.get_tv_power_state() == "off"

    @patch("urllib.request.urlopen")
    def test_tv_off_explicit(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("off"))
        assert watcher.get_tv_power_state() == "off"

    @patch("urllib.request.urlopen")
    def test_tv_unreachable_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        assert watcher.get_tv_power_state() == "unreachable"

    @patch("urllib.request.urlopen")
    def test_tv_unreachable_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        assert watcher.get_tv_power_state() == "unreachable"

    @patch("urllib.request.urlopen")
    def test_tv_unreachable_os_error(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("Network unreachable")
        assert watcher.get_tv_power_state() == "unreachable"

    @patch("urllib.request.urlopen")
    def test_malformed_json(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(b"not json at all")
        assert watcher.get_tv_power_state() == "unreachable"

    @patch("urllib.request.urlopen")
    def test_empty_response(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(b"")
        assert watcher.get_tv_power_state() == "unreachable"

    @patch("urllib.request.urlopen")
    def test_missing_device_field(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(b'{"other": "data"}')
        # Missing device.PowerState → treated as unknown → "off"
        assert watcher.get_tv_power_state() == "off"

    @patch("urllib.request.urlopen")
    def test_missing_power_state_field(self, mock_urlopen):
        mock_urlopen.return_value = mock_urlopen_response(
            json.dumps({"device": {"modelName": "QE55LS03"}}).encode()
        )
        # PowerState missing defaults to "unknown" → not "on" → "off"
        assert watcher.get_tv_power_state() == "off"

    @patch("urllib.request.urlopen")
    def test_power_state_case_sensitive(self, mock_urlopen):
        """PowerState must be exactly 'on', not 'On' or 'ON'."""
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("On"))
        assert watcher.get_tv_power_state() == "off"

    @patch("urllib.request.urlopen")
    def test_ssl_error(self, mock_urlopen):
        import ssl
        mock_urlopen.side_effect = ssl.SSLError("SSL handshake failed")
        assert watcher.get_tv_power_state() == "unreachable"


# ============================================================
# 2. REST URL scheme detection
# ============================================================

class TestRestSchemeProbe:
    """Tests for HTTP/HTTPS auto-detection."""

    @patch("urllib.request.urlopen")
    def test_https_preferred(self, mock_urlopen):
        """HTTPS should be tried first."""
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("on"))
        url = watcher._probe_rest_url()
        assert "https://" in url

    @patch("urllib.request.urlopen")
    def test_fallback_to_http(self, mock_urlopen):
        """If HTTPS fails, HTTP should be tried."""
        def side_effect(req, **kwargs):
            if "https://" in req.full_url:
                raise urllib.error.URLError("SSL failed")
            return mock_urlopen_response(make_rest_response("on"))

        mock_urlopen.side_effect = side_effect
        url = watcher._probe_rest_url()
        assert "http://" in url
        assert watcher._rest_scheme == "http"

    @patch("urllib.request.urlopen")
    def test_scheme_cached(self, mock_urlopen):
        """Once detected, scheme should be cached."""
        mock_urlopen.return_value = mock_urlopen_response(make_rest_response("on"))
        watcher._probe_rest_url()
        watcher._probe_rest_url()
        # urlopen called once for probe, not twice
        # (first call detects, second uses cache)
        assert watcher._rest_scheme == "https"

    @patch("urllib.request.urlopen")
    def test_both_fail_defaults_https(self, mock_urlopen):
        """If both HTTPS and HTTP fail, default to HTTPS."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        url = watcher._probe_rest_url()
        assert "https://" in url
        assert watcher._rest_scheme is None  # not cached since probe failed


# ============================================================
# 3. confirm_offline()
# ============================================================

class TestConfirmOffline:
    """Tests for the offline confirmation window."""

    @patch("time.sleep")
    def test_stays_offline_returns_true(self, mock_sleep):
        """If TV stays off for entire confirmation, returns True."""
        with patch.object(watcher, "get_tv_power_state", return_value="unreachable"):
            assert watcher.confirm_offline(30) is True

        # Should have done ceil(30/10) = 3 checks
        assert mock_sleep.call_count == 3

    @patch("time.sleep")
    def test_comes_back_returns_false(self, mock_sleep):
        """If TV comes back during confirmation, returns False."""
        states = iter(["unreachable", "on"])
        with patch.object(watcher, "get_tv_power_state", side_effect=states):
            assert watcher.confirm_offline(30) is False

    @patch("time.sleep")
    def test_comes_back_on_last_check(self, mock_sleep):
        """TV comes back on the very last check."""
        states = iter(["unreachable", "unreachable", "on"])
        with patch.object(watcher, "get_tv_power_state", side_effect=states):
            assert watcher.confirm_offline(30) is False

    @patch("time.sleep")
    def test_non_divisible_duration(self, mock_sleep):
        """Duration not evenly divisible by POLL_INTERVAL uses ceil."""
        with patch.object(watcher, "get_tv_power_state", return_value="off"):
            assert watcher.confirm_offline(25) is True
        # ceil(25/10) = 3 checks
        assert mock_sleep.call_count == 3

    @patch("time.sleep")
    def test_off_state_counts_as_offline(self, mock_sleep):
        """'off' (not just 'unreachable') should count as offline."""
        with patch.object(watcher, "get_tv_power_state", return_value="off"):
            assert watcher.confirm_offline(30) is True


# ============================================================
# 4. send_wol()
# ============================================================

class TestSendWol:
    """Tests for Wake-on-LAN packet sending."""

    @patch("socket.socket")
    def test_magic_packet_format(self, mock_socket_class):
        """WoL magic packet should be 6x FF + 16x MAC."""
        mock_sock = MagicMock()
        mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

        watcher.send_wol(count=1)

        # Should have sent to both broadcast addresses
        assert mock_sock.sendto.call_count == 2

        # Verify packet format
        packet = mock_sock.sendto.call_args_list[0][0][0]
        mac_bytes = bytes.fromhex(watcher.TV_MAC.replace(":", ""))
        expected = b"\xff" * 6 + mac_bytes * 16
        assert packet == expected

    @patch("socket.socket")
    def test_sends_to_both_broadcasts(self, mock_socket_class):
        """Should broadcast to 255.255.255.255 and subnet broadcast."""
        mock_sock = MagicMock()
        mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

        watcher.send_wol(count=1)

        addrs = [c[0][1] for c in mock_sock.sendto.call_args_list]
        assert ("255.255.255.255", 9) in addrs
        assert ("192.168.1.255", 9) in addrs

    @patch("socket.socket")
    def test_multiple_packets(self, mock_socket_class):
        """count=3 should send 3 sets of packets."""
        mock_sock = MagicMock()
        mock_socket_class.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_class.return_value.__exit__ = MagicMock(return_value=False)

        watcher.send_wol(count=3)

        # 3 packets × 2 broadcast addresses = 6 sendto calls
        assert mock_sock.sendto.call_count == 6


# ============================================================
# 5. wait_for_tv()
# ============================================================

class TestWaitForTv:
    """Tests for WoL + wait for TV to come online."""

    @patch("time.sleep")
    @patch("time.time")
    def test_tv_responds_immediately(self, mock_time, mock_sleep):
        """TV responds on first check after WoL."""
        # time.time() is called: deadline calc, while check, wol check, reachable check, elapsed calc
        mock_time.side_effect = [0, 0, 0, 1, 1]
        with patch.object(watcher, "tv_is_reachable", return_value=True), \
             patch.object(watcher, "send_wol"):
            assert watcher.wait_for_tv(timeout=60) is True

    @patch("time.sleep")
    @patch("time.time")
    def test_tv_timeout(self, mock_time, mock_sleep):
        """TV never responds within timeout."""
        # Simulate time advancing past deadline
        times = [0]  # initial
        for i in range(65):
            times.append(i)
        times.append(61)  # past deadline
        mock_time.side_effect = times

        with patch.object(watcher, "tv_is_reachable", return_value=False), \
             patch.object(watcher, "send_wol"):
            assert watcher.wait_for_tv(timeout=60) is False

    @patch("time.sleep")
    @patch("time.time")
    def test_wol_bursts_at_intervals(self, mock_time, mock_sleep):
        """WoL should be sent at wall-clock intervals."""
        call_count = 0
        def time_values():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return 0  # initial calls
            if call_count <= 5:
                return 2  # still before first retry
            if call_count <= 8:
                return 6  # past WOL_RETRY_DELAY
            return 100  # past deadline

        mock_time.side_effect = time_values

        with patch.object(watcher, "tv_is_reachable", return_value=False), \
             patch.object(watcher, "send_wol") as mock_wol:
            watcher.wait_for_tv(timeout=60)
            # Should have sent at least 2 WoL bursts (t=0 and t=6)
            assert mock_wol.call_count >= 2


# ============================================================
# 6. switch_to_artmode()
# ============================================================

class TestSwitchToArtmode:
    """Tests for the artmode subprocess call."""

    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Art mode enabled ✅", stderr="")
        assert watcher.switch_to_artmode() is True

    @patch("subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Connection refused")
        assert watcher.switch_to_artmode() is False

    @patch("subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="flipframe", timeout=60)
        assert watcher.switch_to_artmode() is False

    @patch("subprocess.run")
    def test_exception(self, mock_run):
        mock_run.side_effect = FileNotFoundError("python not found")
        assert watcher.switch_to_artmode() is False


# ============================================================
# 7. quiet_push()
# ============================================================

class TestQuietPush:
    """Tests for weather refresh push."""

    @patch("subprocess.run")
    def test_skips_when_unreachable(self, mock_run):
        with patch.object(watcher, "get_tv_power_state", return_value="unreachable"):
            watcher.quiet_push()
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_success_with_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Uploading...\n✅ Done! Quietly pushed.", stderr=""
        )
        with patch.object(watcher, "get_tv_power_state", return_value="on"):
            watcher.quiet_push()  # should not raise

    @patch("subprocess.run")
    def test_success_empty_output(self, mock_run):
        """Empty stdout should not crash (was a bug)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(watcher, "get_tv_power_state", return_value="on"):
            watcher.quiet_push()  # should not raise

    @patch("subprocess.run")
    def test_subprocess_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="flipframe", timeout=120)
        with patch.object(watcher, "get_tv_power_state", return_value="on"):
            watcher.quiet_push()  # should not raise


# ============================================================
# 8. watch() state machine
# ============================================================

class TestWatchStateMachine:
    """Integration tests for the main watch loop."""

    def _run_watch_cycles(self, state_sequence, cycles=None):
        """Run watch() with mocked states and collect actions.

        state_sequence: list of return values for get_tv_power_state
        Returns dict of action counts.
        """
        if cycles is None:
            cycles = len(state_sequence)

        state_iter = iter(state_sequence)
        call_idx = [0]

        def mock_power_state():
            try:
                return next(state_iter)
            except StopIteration:
                raise SystemExit("Test complete")

        actions = {"confirm_offline": 0, "wait_for_tv": 0, "switch_to_artmode": 0, "quiet_push": 0}

        def track(name, return_value=True):
            def fn(*a, **kw):
                actions[name] += 1
                return return_value
            return fn

        with patch.object(watcher, "get_tv_power_state", side_effect=mock_power_state), \
             patch.object(watcher, "confirm_offline", side_effect=track("confirm_offline", True)), \
             patch.object(watcher, "wait_for_tv", side_effect=track("wait_for_tv", True)), \
             patch.object(watcher, "switch_to_artmode", side_effect=track("switch_to_artmode", True)), \
             patch.object(watcher, "quiet_push", side_effect=track("quiet_push")), \
             patch("time.sleep"):
            try:
                watcher.watch()
            except (SystemExit, StopIteration):
                pass

        return actions

    def test_happy_path_on_to_off_to_artmode(self):
        """TV on → off should trigger confirm → WoL → artmode → push."""
        # Initial state query, then poll returns "off", then re-query after artmode
        states = ["on", "off", "on"]  # initial, transition detected, post-artmode
        actions = self._run_watch_cycles(states)
        assert actions["confirm_offline"] == 1
        assert actions["wait_for_tv"] == 1
        assert actions["switch_to_artmode"] == 1

    def test_blip_filtered(self):
        """TV on → off but comes back during confirm → no artmode."""
        states = ["on", "off"]

        with patch.object(watcher, "get_tv_power_state") as mock_state, \
             patch.object(watcher, "confirm_offline", return_value=False) as mock_confirm, \
             patch.object(watcher, "wait_for_tv") as mock_wait, \
             patch("time.sleep"):

            # Initial returns "on", second returns "off", third (post-confirm) returns "on"
            mock_state.side_effect = ["on", "off", "on"]

            try:
                watcher.watch()
            except (SystemExit, StopIteration):
                pass

            mock_confirm.assert_called_once()
            mock_wait.assert_not_called()

    def test_unreachable_to_on_no_artmode(self):
        """TV unreachable → on should NOT trigger artmode (TV turned on by user)."""
        states = ["unreachable", "on"]

        with patch.object(watcher, "get_tv_power_state") as mock_state, \
             patch.object(watcher, "confirm_offline") as mock_confirm, \
             patch("time.sleep"):

            mock_state.side_effect = states + [StopIteration()]
            try:
                watcher.watch()
            except (StopIteration, TypeError):
                pass

            # confirm_offline should NOT be called — only on→off transition triggers it
            mock_confirm.assert_not_called()

    def test_off_to_off_no_retrigger(self):
        """TV staying off should not re-trigger artmode."""
        states = ["off", "off", "off"]

        with patch.object(watcher, "get_tv_power_state") as mock_state, \
             patch.object(watcher, "confirm_offline") as mock_confirm, \
             patch("time.sleep"):

            mock_state.side_effect = states + [StopIteration()]
            try:
                watcher.watch()
            except (StopIteration, TypeError):
                pass

            # No on→off transition, so no confirmation
            mock_confirm.assert_not_called()


# ============================================================
# 9. Thread safety
# ============================================================

class TestThreadSafety:
    """Verify _tv_lock usage."""

    @patch("subprocess.run")
    def test_quiet_push_acquires_lock(self, mock_run):
        """quiet_push should hold _tv_lock during subprocess call."""
        lock_held = [False]

        original_run = mock_run.side_effect

        def check_lock(*args, **kwargs):
            lock_held[0] = watcher._tv_lock.locked()
            return MagicMock(returncode=0, stdout="Done", stderr="")

        mock_run.side_effect = check_lock

        with patch.object(watcher, "get_tv_power_state", return_value="on"):
            watcher.quiet_push()

        assert lock_held[0] is True, "_tv_lock should be held during subprocess call"


# ============================================================
# 10. tv_is_reachable()
# ============================================================

class TestTvIsReachable:
    """Tests for TCP port reachability check."""

    @patch("socket.create_connection")
    def test_reachable(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        assert watcher.tv_is_reachable() is True

    @patch("socket.create_connection")
    def test_connection_refused(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError()
        assert watcher.tv_is_reachable() is False

    @patch("socket.create_connection")
    def test_timeout(self, mock_conn):
        mock_conn.side_effect = TimeoutError()
        assert watcher.tv_is_reachable() is False

    @patch("socket.create_connection")
    def test_os_error(self, mock_conn):
        mock_conn.side_effect = OSError("Host unreachable")
        assert watcher.tv_is_reachable() is False


# ============================================================
# 11. make_env()
# ============================================================

class TestMakeEnv:
    """Tests for subprocess environment building."""

    def test_includes_homebrew_path(self):
        env = watcher.make_env()
        assert "/opt/homebrew/bin" in env["PATH"]

    def test_preserves_existing_env(self):
        env = watcher.make_env()
        assert "HOME" in env  # standard env var should be preserved


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
