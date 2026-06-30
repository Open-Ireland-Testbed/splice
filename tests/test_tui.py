"""Tests for the TUI service layer.

These tests cover the service classes used by the Textual TUI screens.
No real database, network, or Textual widget rendering is needed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest

textual = pytest.importorskip("textual")

from splice.tui.services.device_registry import (
    DeviceEntry,
    DeviceRegistry,
    _validate_name,
)
from splice.tui.services.catalog_loader import CatalogLoader, CatalogResult
from splice.tui.services.telemetry_service import (
    HISTORY_SIZE,
    TelemetryService,
    TelemetryUpdate,
    Threshold,
    _paths_match,
)
from splice.tui.services.health_checker import HealthChecker, HealthStatus
from splice.tui.services.process_manager import ProcessManager, ProcessState
from splice.tui.services.job_service import JobInfo, JobService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    name: str = "test-device",
    host: str = "10.0.0.1",
    netconf_port: int = 830,
    gnmi_port: int = 9339,
    db_url: str = "postgresql+psycopg://localhost/test",
    yang_dir: str = "/tmp/yang",
    health_port: int = 9809,
    config_path: str = "/tmp/config.yaml",
) -> DeviceEntry:
    return DeviceEntry(
        name=name,
        config_path=config_path,
        host=host,
        netconf_port=netconf_port,
        gnmi_port=gnmi_port,
        username="admin",
        db_url=db_url,
        yang_dir=yang_dir,
        health_port=health_port,
    )


def _write_device_config(base_dir: Path, name: str, host: str = "10.0.0.1", port: int = 830) -> Path:
    """Write a minimal device config.yaml and return the config path."""
    import yaml

    device_dir = base_dir / name
    device_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "device": {"host": host, "port": port, "username": "admin"},
        "gnmi": {"port": 9339},
        "db": {"url": "postgresql+psycopg://localhost/test"},
        "health": {"port": 9809},
    }
    config_path = device_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


# ===========================================================================
# DeviceRegistry
# ===========================================================================

class TestValidateName:
    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name("")

    def test_rejects_slash(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name("foo/bar")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name("foo\\bar")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name("..")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name("../etc")

    def test_rejects_dot(self):
        with pytest.raises(ValueError, match="Invalid device name"):
            _validate_name(".")

    def test_accepts_valid_names(self):
        _validate_name("my-device")
        _validate_name("device_1")
        _validate_name("test.device")


class TestDeviceRegistry:
    def test_scan_empty_dir(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        assert registry.scan() == []

    def test_scan_nonexistent_dir(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path / "nonexistent"))
        assert registry.scan() == []

    def test_scan_finds_devices(self, tmp_path):
        _write_device_config(tmp_path, "dev-a", host="10.0.0.1")
        _write_device_config(tmp_path, "dev-b", host="10.0.0.2")
        registry = DeviceRegistry(str(tmp_path))
        entries = registry.scan()
        assert len(entries) == 2
        names = {e.name for e in entries}
        assert names == {"dev-a", "dev-b"}

    def test_scan_skips_missing_host(self, tmp_path):
        """A config with no host is skipped."""
        import yaml
        device_dir = tmp_path / "bad-dev"
        device_dir.mkdir()
        config = {"device": {"port": 830}, "gnmi": {"port": 9339}}
        with open(device_dir / "config.yaml", "w") as f:
            yaml.dump(config, f)
        registry = DeviceRegistry(str(tmp_path))
        assert registry.scan() == []

    def test_scan_skips_no_config(self, tmp_path):
        """A directory without config.yaml is skipped."""
        (tmp_path / "empty-dir").mkdir()
        registry = DeviceRegistry(str(tmp_path))
        assert registry.scan() == []

    def test_get_existing(self, tmp_path):
        _write_device_config(tmp_path, "mydev", host="1.2.3.4", port=8830)
        registry = DeviceRegistry(str(tmp_path))
        entry = registry.get("mydev")
        assert entry is not None
        assert entry.host == "1.2.3.4"
        assert entry.netconf_port == 8830

    def test_get_nonexistent(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        assert registry.get("nope") is None

    def test_add_creates_device(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        entry = registry.add("new-dev", host="10.0.0.5", port=830, password="secret")
        assert entry.name == "new-dev"
        assert entry.host == "10.0.0.5"
        # Verify files on disk
        assert (tmp_path / "new-dev" / "config.yaml").is_file()
        assert (tmp_path / "new-dev" / "password").is_file()
        assert (tmp_path / "new-dev" / "yang").is_dir()
        pw = (tmp_path / "new-dev" / "password").read_text().strip()
        assert pw == "secret"

    def test_add_rejects_traversal(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid device name"):
            registry.add("../escape", host="10.0.0.1")

    def test_update_device(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        registry.add("upd-dev", host="10.0.0.1")
        updated = registry.update("upd-dev", host="10.0.0.99", gnmi_port=9999)
        assert updated is not None
        assert updated.host == "10.0.0.99"
        assert updated.gnmi_port == 9999

    def test_update_nonexistent(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        assert registry.update("no-exist", host="1.2.3.4") is None

    def test_update_password(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        registry.add("pw-dev", host="10.0.0.1", password="old")
        registry.update("pw-dev", password="new-secret")
        pw = (tmp_path / "pw-dev" / "password").read_text().strip()
        assert pw == "new-secret"

    def test_remove_device(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        registry.add("rm-dev", host="10.0.0.1")
        assert (tmp_path / "rm-dev").is_dir()
        assert registry.remove("rm-dev") is True
        assert not (tmp_path / "rm-dev").exists()

    def test_remove_nonexistent(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        assert registry.remove("no-exist") is False

    def test_remove_rejects_traversal(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid device name"):
            registry.remove("../bad")

    def test_get_rejects_traversal(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid device name"):
            registry.get("../etc")

    def test_remove_rejects_dot(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid device name"):
            registry.remove(".")

    def test_test_connectivity_device_not_found(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        ok, msg = registry.test_connectivity("nonexistent")
        assert ok is False
        assert "not found" in msg

    @patch("splice.tui.services.device_registry.socket.create_connection")
    def test_test_connectivity_success(self, mock_conn, tmp_path):
        _write_device_config(tmp_path, "conn-dev", host="10.0.0.1")
        registry = DeviceRegistry(str(tmp_path))
        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        ok, msg = registry.test_connectivity("conn-dev")
        assert ok is True
        assert "Connected" in msg

    @patch("splice.tui.services.device_registry.socket.create_connection")
    def test_test_connectivity_failure(self, mock_conn, tmp_path):
        _write_device_config(tmp_path, "fail-dev", host="10.0.0.1")
        registry = DeviceRegistry(str(tmp_path))
        mock_conn.side_effect = OSError("Connection refused")
        ok, msg = registry.test_connectivity("fail-dev")
        assert ok is False
        assert "Cannot reach" in msg

    def test_base_dir_property(self, tmp_path):
        registry = DeviceRegistry(str(tmp_path))
        assert registry.base_dir == tmp_path.resolve()


# ===========================================================================
# CatalogLoader
# ===========================================================================

class TestCatalogLoader:
    def test_cache_hit(self):
        loader = CatalogLoader()
        entry = _make_entry()
        cached = CatalogResult(source="cached", path_count=5)
        loader._cache["test-device"] = cached
        result = loader.load(entry)
        assert result.source == "cached"
        assert result.path_count == 5

    def test_cache_bypass_on_force(self):
        loader = CatalogLoader()
        entry = _make_entry(db_url="", yang_dir="/nonexistent")
        loader._cache["test-device"] = CatalogResult(source="cached")
        result = loader.load(entry, force=True)
        # Force bypasses cache, but no DB or YANG available -> empty result
        assert result.source == "none"

    def test_clear_specific_device(self):
        loader = CatalogLoader()
        loader._cache["dev-a"] = CatalogResult(source="a")
        loader._cache["dev-b"] = CatalogResult(source="b")
        loader.clear("dev-a")
        assert "dev-a" not in loader._cache
        assert "dev-b" in loader._cache

    def test_clear_all(self):
        loader = CatalogLoader()
        loader._cache["dev-a"] = CatalogResult(source="a")
        loader._cache["dev-b"] = CatalogResult(source="b")
        loader.clear()
        assert len(loader._cache) == 0

    @patch("splice.tui.services.catalog_loader.CatalogLoader._load_from_db")
    def test_db_first_priority(self, mock_db):
        loader = CatalogLoader()
        entry = _make_entry()
        mock_db.return_value = CatalogResult(source="db", path_count=10)
        result = loader.load(entry)
        assert result.source == "db"
        mock_db.assert_called_once_with(entry)

    @patch("splice.tui.services.catalog_loader.CatalogLoader._load_from_yang")
    @patch("splice.tui.services.catalog_loader.CatalogLoader._load_from_db")
    def test_fallback_to_yang(self, mock_db, mock_yang):
        loader = CatalogLoader()
        entry = _make_entry()
        mock_db.return_value = None  # DB fails
        mock_yang.return_value = CatalogResult(source="yang", path_count=7)
        result = loader.load(entry)
        assert result.source == "yang"
        mock_yang.assert_called_once_with(entry)

    @patch("splice.tui.services.catalog_loader.CatalogLoader._load_from_yang")
    @patch("splice.tui.services.catalog_loader.CatalogLoader._load_from_db")
    def test_both_fail_returns_empty(self, mock_db, mock_yang):
        loader = CatalogLoader()
        entry = _make_entry()
        mock_db.return_value = None
        mock_yang.return_value = None
        result = loader.load(entry)
        assert result.source == "none"
        assert result.path_count == 0

    def test_load_from_yang_no_dir(self):
        loader = CatalogLoader()
        entry = _make_entry(yang_dir="/nonexistent/path")
        result = loader._load_from_yang(entry)
        assert result is None

    def test_load_from_yang_empty_dir(self, tmp_path):
        loader = CatalogLoader()
        entry = _make_entry(yang_dir=str(tmp_path))
        result = loader._load_from_yang(entry)
        assert result is None

    def test_load_from_db_no_url(self):
        loader = CatalogLoader()
        entry = _make_entry(db_url="")
        # When db_url is empty, load skips DB and goes to YANG
        result = loader.load(entry, force=True)
        # With no yang_dir either, should get empty result
        assert result.source in ("none",) or result.error is not None


# ===========================================================================
# TelemetryService
# ===========================================================================

class TestTelemetryService:
    def test_watch_new_path(self):
        svc = TelemetryService()
        assert svc.watch("/a/b") is True
        assert "/a/b" in svc.watched_paths

    def test_watch_duplicate(self):
        svc = TelemetryService()
        svc.watch("/a/b")
        assert svc.watch("/a/b") is False

    def test_unwatch_existing(self):
        svc = TelemetryService()
        svc.watch("/a/b")
        assert svc.unwatch("/a/b") is True
        assert "/a/b" not in svc.watched_paths

    def test_unwatch_nonexistent(self):
        svc = TelemetryService()
        assert svc.unwatch("/x/y") is False

    def test_unwatch_cleans_up_state(self):
        svc = TelemetryService()
        svc.watch("/a/b")
        svc.set_threshold("/a/b", low=0, high=100)
        svc.record_value("/a/b", 42.0)
        svc.get_trend("/a/b", 50.0)
        svc.unwatch("/a/b")
        assert svc.get_threshold("/a/b") is None
        assert svc.get_history("/a/b") == []
        assert svc.get_current_value("/a/b") is None

    def test_clear_removes_all(self):
        svc = TelemetryService()
        svc.watch("/a/b")
        svc.watch("/c/d")
        svc.set_threshold("/a/b", high=10)
        svc.record_value("/a/b", 5.0)
        svc.clear()
        assert svc.watched_paths == []
        assert svc.get_threshold("/a/b") is None

    def test_trend_up(self):
        svc = TelemetryService()
        svc.get_trend("/a", 10.0)  # set previous
        trend = svc.get_trend("/a", 20.0)
        assert trend == "\u2191"  # up arrow

    def test_trend_down(self):
        svc = TelemetryService()
        svc.get_trend("/a", 20.0)
        trend = svc.get_trend("/a", 10.0)
        assert trend == "\u2193"  # down arrow

    def test_trend_steady(self):
        svc = TelemetryService()
        svc.get_trend("/a", 10.0)
        trend = svc.get_trend("/a", 10.0)
        assert trend == "\u2192"  # right arrow

    def test_trend_no_previous(self):
        svc = TelemetryService()
        trend = svc.get_trend("/a", 10.0)
        assert trend == " "

    def test_trend_none_current(self):
        svc = TelemetryService()
        svc.get_trend("/a", 10.0)
        trend = svc.get_trend("/a", None)
        assert trend == " "

    def test_trend_non_numeric_change(self):
        svc = TelemetryService()
        svc.get_trend("/a", "hello")
        trend = svc.get_trend("/a", "world")
        assert trend == "\u0394"  # delta

    def test_trend_non_numeric_same(self):
        svc = TelemetryService()
        svc.get_trend("/a", "hello")
        trend = svc.get_trend("/a", "hello")
        assert trend == "\u2192"

    def test_record_value_and_history(self):
        svc = TelemetryService()
        svc.record_value("/a", 1.0)
        svc.record_value("/a", 2.0)
        svc.record_value("/a", 3.0)
        history = svc.get_history("/a")
        assert history == [1.0, 2.0, 3.0]

    def test_record_value_non_numeric_ignored(self):
        svc = TelemetryService()
        svc.record_value("/a", "not-a-number")
        assert svc.get_history("/a") == []

    def test_history_ring_buffer(self):
        svc = TelemetryService()
        for i in range(HISTORY_SIZE + 10):
            svc.record_value("/a", float(i))
        history = svc.get_history("/a")
        assert len(history) == HISTORY_SIZE
        assert history[-1] == float(HISTORY_SIZE + 9)

    def test_threshold_set_and_get(self):
        svc = TelemetryService()
        svc.set_threshold("/a", low=5.0, high=95.0)
        th = svc.get_threshold("/a")
        assert th is not None
        assert th.low == 5.0
        assert th.high == 95.0

    def test_threshold_clear(self):
        svc = TelemetryService()
        svc.set_threshold("/a", high=100.0)
        svc.clear_threshold("/a")
        assert svc.get_threshold("/a") is None

    def test_check_threshold_high(self):
        svc = TelemetryService()
        svc.set_threshold("/a", high=90.0)
        assert svc.check_threshold("/a", 95.0) == "high"
        assert svc.check_threshold("/a", 90.0) == "high"
        assert svc.check_threshold("/a", 89.9) is None

    def test_check_threshold_low(self):
        svc = TelemetryService()
        svc.set_threshold("/a", low=10.0)
        assert svc.check_threshold("/a", 5.0) == "low"
        assert svc.check_threshold("/a", 10.0) == "low"
        assert svc.check_threshold("/a", 10.1) is None

    def test_check_threshold_no_threshold(self):
        svc = TelemetryService()
        assert svc.check_threshold("/a", 50.0) is None

    def test_check_threshold_non_numeric(self):
        svc = TelemetryService()
        svc.set_threshold("/a", high=100.0)
        assert svc.check_threshold("/a", "not-a-number") is None

    def test_set_device_resets_channel(self):
        svc = TelemetryService()
        svc._stub = MagicMock()
        mock_channel = MagicMock()
        svc._channel = mock_channel
        entry = _make_entry()
        svc.set_device(entry)
        assert svc._device == entry
        assert svc._stub is None
        assert svc._channel is None
        mock_channel.close.assert_called_once()

    def test_close_cleans_channel(self):
        svc = TelemetryService()
        mock_channel = MagicMock()
        svc._channel = mock_channel
        svc._stub = MagicMock()
        svc.close()
        assert svc._channel is None
        assert svc._stub is None
        mock_channel.close.assert_called_once()

    def test_get_status_no_device(self):
        svc = TelemetryService()
        assert svc.get_status() == "No device"

    def test_get_status_gnmi(self):
        svc = TelemetryService()
        svc._device = _make_entry(gnmi_port=9340)
        svc._stub = MagicMock()
        assert svc.get_status() == "gNMI :9340"

    def test_get_status_db_fallback(self):
        svc = TelemetryService()
        svc._device = _make_entry(db_url="postgresql://localhost/test")
        assert svc.get_status() == "DB fallback"

    def test_get_status_disconnected(self):
        svc = TelemetryService()
        svc._device = _make_entry(db_url="")
        assert svc.get_status() == "Disconnected"

    def test_poll_once_no_device(self):
        svc = TelemetryService()
        svc.watch("/a/b")
        assert svc.poll_once() == []

    def test_poll_once_no_watched(self):
        svc = TelemetryService()
        svc._device = _make_entry()
        assert svc.poll_once() == []

    def test_fetch_db_history_no_device(self):
        svc = TelemetryService()
        assert svc.fetch_db_history("/a/b") == []


class TestPathsMatch:
    def test_same_path(self):
        assert _paths_match("/a/b", "/a/b") is True

    def test_leading_slash_difference(self):
        assert _paths_match("/a/b", "a/b") is True

    def test_trailing_slash(self):
        assert _paths_match("/a/b/", "/a/b") is True

    def test_different_paths(self):
        assert _paths_match("/a/b", "/a/c") is False

    def test_namespace_prefix_ignored(self):
        assert _paths_match("/p1:system/state", "/system/state") is True

    def test_encoded_vs_decoded_key_value(self):
        # the bridge serves keys percent-ENCODED; a watched path may be DECODED.
        # Both must reconcile or telemetry shows no value (regression guard).
        watched = "/pm/otnpm-data[interface=1%2F2%2Fn1%2Fot400,managed-element=1]/FEC/acor-factt:fec-ber"
        served = "/pm/otnpm-data[interface=1/2/n1/ot400,managed-element=1]/FEC/acor-factt:fec-ber"
        assert _paths_match(watched, served) is True

    def test_keyed_predicate_colon_in_value_not_stripped(self):
        # a decoded value like 'eth0:1' must not have 'eth0:' treated as a prefix
        assert _paths_match("/if[name=eth0:1]/state", "/p1:if[name=eth0:1]/state") is True
        assert _paths_match("/if[name=eth0:1]/state", "/if[name=eth0:2]/state") is False


class TestTelemetryPathRoundTrip:
    def test_keyed_pm_path_round_trips_to_server_canonical_form(self):
        # TUI must parse/serialize keyed paths identically to the gNMI server,
        # else the Get response never matches the watched key.
        from splice.tui.services.telemetry_service import _path_from_str, _path_to_str

        watched = "/pm/otnpm-data[interface=1%2F2%2Fn1%2Fot400,managed-element=1,otsi=1]/CarrierTF/acor-factt:opt-rcv-pwr"
        assert _path_to_str(_path_from_str(watched)) == watched


# ===========================================================================
# HealthChecker
# ===========================================================================

class TestHealthChecker:
    @patch("splice.tui.services.health_checker.socket.create_connection")
    @patch("splice.tui.services.health_checker.urlopen")
    def test_check_all_healthy(self, mock_urlopen, mock_conn):
        import json
        health_data = json.dumps({"status": "ok"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = health_data
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        checker = HealthChecker()
        entry = _make_entry(db_url="")  # skip DB checks
        status = checker.check(entry)
        assert status.health_endpoint_ok is True
        assert status.gnmi_reachable is True
        assert status.netconf_reachable is True

    @patch("splice.tui.services.health_checker.socket.create_connection")
    @patch("splice.tui.services.health_checker.urlopen")
    def test_check_all_unreachable(self, mock_urlopen, mock_conn):
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("unreachable")
        mock_conn.side_effect = OSError("refused")

        checker = HealthChecker()
        entry = _make_entry(db_url="")
        status = checker.check(entry)
        assert status.health_endpoint_ok is False
        assert status.gnmi_reachable is False
        assert status.netconf_reachable is False

    @patch("splice.tui.services.health_checker.socket.create_connection")
    def test_tcp_probe_success(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock()
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        checker = HealthChecker()
        assert checker._tcp_probe("localhost", 9339) is True

    @patch("splice.tui.services.health_checker.socket.create_connection")
    def test_tcp_probe_failure(self, mock_conn):
        mock_conn.side_effect = OSError("refused")
        checker = HealthChecker()
        assert checker._tcp_probe("localhost", 9339) is False

    @patch("splice.tui.services.health_checker.urlopen")
    def test_health_endpoint_json_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        checker = HealthChecker()
        status = HealthStatus()
        entry = _make_entry()
        # JSON decode will fail, health_endpoint_ok should be False
        checker._check_health_endpoint(entry, status)
        assert status.health_endpoint_ok is False


# ===========================================================================
# ProcessManager
# ===========================================================================

class TestProcessManager:
    def test_initial_state(self):
        pm = ProcessManager()
        state = pm.get_state()
        assert state.status == "stopped"
        assert state.pid is None
        assert pm.is_running is False

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_start_setup(self, mock_popen):
        block_event = threading.Event()
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None

        def blocking_readline():
            block_event.wait()
            return ""

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = blocking_readline
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        result = pm.start_setup("/path/to/config.yaml")
        assert result is True
        with pm._lock:
            assert pm._state.status == "setup"
            assert pm._state.pid == 12345
        assert pm.mode == "setup"

        # Cleanup: unblock reader thread, then stop
        block_event.set()
        mock_proc.poll.return_value = 0
        pm.stop()

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_start_run(self, mock_popen):
        block_event = threading.Event()
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.poll.return_value = None

        def blocking_readline():
            block_event.wait()
            return ""

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = blocking_readline
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        result = pm.start_run("/path/to/config.yaml")
        assert result is True
        with pm._lock:
            assert pm._state.status == "running"
        assert pm.mode == "run"

        block_event.set()
        mock_proc.poll.return_value = 0
        pm.stop()

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_cannot_start_while_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        pm.start_run("/cfg.yaml")
        result = pm.start_setup("/cfg.yaml")
        assert result is False

        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        pm.stop()

    def test_start_popen_failure(self):
        pm = ProcessManager()
        with patch(
            "splice.tui.services.process_manager.subprocess.Popen",
            side_effect=OSError("boom"),
        ):
            result = pm.start_setup("/cfg.yaml")
        assert result is False
        state = pm.get_state()
        assert state.status == "crashed"
        assert state.exit_code == -1

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_stop_process(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        pm.start_run("/cfg.yaml")
        pm.stop()
        mock_proc.terminate.assert_called_once()
        state = pm.get_state()
        assert state.status == "stopped"

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_stop_already_stopped(self, mock_popen):
        pm = ProcessManager()
        pm.stop()  # Should not raise

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_shutdown_stops_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        pm.start_run("/cfg.yaml")
        pm.shutdown()
        mock_proc.terminate.assert_called_once()

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_read_output(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        pm.start_run("/cfg.yaml")
        # Manually add output
        with pm._lock:
            pm._output_buffer.append("line1\n")
            pm._output_buffer.append("line2\n")
        lines = pm.read_output()
        assert lines == ["line1\n", "line2\n"]
        # Should be drained
        assert pm.read_output() == []

        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        pm.stop()

    @patch("splice.tui.services.process_manager.subprocess.Popen")
    def test_get_state_detects_crash(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = 1  # non-zero exit
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""
        mock_popen.return_value = mock_proc

        pm = ProcessManager()
        pm._process = mock_proc
        pm._state = ProcessState(status="running", pid=1)
        state = pm.get_state()
        assert state.status == "crashed"
        assert state.exit_code == 1

    def test_step_regex_parsing(self):
        """The reader loop parses [N/M] steps from output."""
        from splice.tui.services.process_manager import _STEP_RE
        m = _STEP_RE.search("[3/7] Running discovery...")
        assert m is not None
        assert m.group(1) == "3"
        assert m.group(2) == "7"
        assert m.group(3) == "Running discovery..."

    def test_step_regex_no_match(self):
        from splice.tui.services.process_manager import _STEP_RE
        m = _STEP_RE.search("Just a normal line")
        assert m is None


# ===========================================================================
# JobService
# ===========================================================================

class TestJobService:
    @patch("splice.tui.services._db.lookup_device")
    @patch("splice.tui.services._db.get_session")
    def test_fetch_jobs_device_not_found(self, mock_get_session, mock_lookup):
        mock_sess = MagicMock()
        mock_get_session.return_value = mock_sess
        mock_lookup.return_value = None

        svc = JobService()
        entry = _make_entry()
        jobs = svc.fetch_jobs(entry)
        assert jobs == []
        mock_sess.close.assert_called_once()

    @patch("splice.tui.services._db.lookup_device")
    @patch("splice.tui.services._db.get_session")
    def test_update_job_device_not_found(self, mock_get_session, mock_lookup):
        mock_sess = MagicMock()
        mock_get_session.return_value = mock_sess
        mock_lookup.return_value = None

        svc = JobService()
        entry = _make_entry()
        result = svc.update_job(entry, job_id=1, interval_sec=60)
        assert result is False
        mock_sess.close.assert_called_once()

    @patch("splice.tui.services._db.lookup_device")
    @patch("splice.tui.services._db.get_session")
    def test_bulk_enable_device_not_found(self, mock_get_session, mock_lookup):
        mock_sess = MagicMock()
        mock_get_session.return_value = mock_sess
        mock_lookup.return_value = None

        svc = JobService()
        entry = _make_entry()
        count = svc.bulk_enable(entry, enabled=True)
        assert count == 0

    @patch("splice.tui.services._db.lookup_device")
    @patch("splice.tui.services._db.get_session")
    def test_bulk_set_interval_device_not_found(self, mock_get_session, mock_lookup):
        mock_sess = MagicMock()
        mock_get_session.return_value = mock_sess
        mock_lookup.return_value = None

        svc = JobService()
        entry = _make_entry()
        count = svc.bulk_set_interval(entry, interval_sec=30)
        assert count == 0

    def test_fetch_jobs_db_import_fails(self):
        """If DB connection fails entirely, return empty list gracefully."""
        svc = JobService()
        entry = _make_entry(db_url="bad://url")
        # The lazy imports inside fetch_jobs will try to import get_session
        # from _db module. We patch the _db module's get_session.
        with patch(
            "splice.tui.services._db.get_session",
            side_effect=Exception("connection failed"),
        ):
            jobs = svc.fetch_jobs(entry)
        assert jobs == []

    def test_update_job_db_fails(self):
        svc = JobService()
        entry = _make_entry(db_url="bad://url")
        with patch(
            "splice.tui.services._db.get_session",
            side_effect=Exception("connection failed"),
        ):
            result = svc.update_job(entry, job_id=1, interval_sec=60)
        assert result is False


# ===========================================================================
# Data classes
# ===========================================================================

class TestDataClasses:
    def test_device_entry(self):
        entry = _make_entry()
        assert entry.name == "test-device"
        assert entry.health_port == 9809

    def test_health_status_defaults(self):
        status = HealthStatus()
        assert status.gnmi_reachable is False
        assert status.error is None
        assert status.catalog_path_count == 0

    def test_process_state_defaults(self):
        state = ProcessState()
        assert state.status == "stopped"
        assert state.pid is None
        assert state.server_ready is False

    def test_catalog_result_defaults(self):
        result = CatalogResult()
        assert result.source == "none"
        assert result.path_count == 0
        assert result.error is None

    def test_telemetry_update_defaults(self):
        update = TelemetryUpdate(path="/a/b")
        assert update.value is None
        assert update.source == "none"
        assert update.timestamp_ns == 0

    def test_threshold_defaults(self):
        th = Threshold()
        assert th.low is None
        assert th.high is None

    def test_job_info(self):
        from datetime import datetime
        now = datetime.now()
        job = JobInfo(id=1, canonical_path="/a/b", interval_sec=30,
                      enabled=True, mode="poll", updated_at=now)
        assert job.id == 1
        assert job.enabled is True
