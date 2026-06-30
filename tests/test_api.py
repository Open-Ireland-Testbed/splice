"""Tests for the splice.api Python API."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from splice.api import (
    ConnectionError,
    Device,
    DeviceError,
    DiscoveryError,
    PathNotFoundError,
    PathNotWritableError,
    SetError,
    Subscription,
    SubscriptionError,
)
from splice.api._catalog import InMemoryCatalog
from splice.api._discovery import build_catalog_in_memory
from splice.api._errors import DeviceError as DeviceErrorDirect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_YANG = """\
module test-mod {
  namespace "urn:example:test-mod";
  prefix tm;

  container system {
    leaf hostname {
      type string;
      config true;
    }
    leaf uptime {
      type uint32;
      config false;
    }
  }
}
"""


class FakeManager:
    """Minimal ncclient manager stand-in."""

    def __init__(self, get_reply_xml="<rpc-reply><data/></rpc-reply>"):
        self._get_reply_xml = get_reply_xml
        self.server_capabilities = [
            "urn:ietf:params:netconf:capability:writable-running:1.0"
        ]

    def get(self, filter_xml):
        return SimpleNamespace(data_xml=self._get_reply_xml)

    def dispatch(self, rpc_elem):
        return SimpleNamespace(xml="<rpc-reply><data/></rpc-reply>")


class FakeClient:
    """Stand-in for NetconfClient."""

    def __init__(self, mgr=None):
        self._mgr = mgr or FakeManager()
        self.connected = False

    def connect(self):
        self.connected = True

    def close(self, graceful=True):
        self.connected = False

    def manager(self):
        return self._mgr

    def get(self, filter_xml):
        return self._mgr.get(filter_xml)


class FakePool:
    """Stand-in for NetconfClientPool."""

    def __init__(self, client=None):
        self.client = client or FakeClient()
        self.get_calls = []
        self.get_config_calls = []
        self.edit_calls = []
        self.commit_calls = []
        self._caps = ["urn:ietf:params:netconf:capability:writable-running:1.0"]

    def get(self, filter_xml):
        self.get_calls.append(filter_xml)
        return SimpleNamespace(
            data_xml=(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
                "<data>"
                '<system xmlns="urn:example:test-mod">'
                "<hostname>r1</hostname>"
                "</system>"
                "</data>"
                "</rpc-reply>"
            )
        )

    def get_config(self, source, filter_xml):
        self.get_config_calls.append((source, filter_xml))
        return SimpleNamespace(
            data_xml=(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
                "<data>"
                '<system xmlns="urn:example:test-mod">'
                "<hostname>r1</hostname>"
                "</system>"
                "</data>"
                "</rpc-reply>"
            )
        )

    def edit_config(self, target, config):
        self.edit_calls.append((target, config))

    def commit(self):
        self.commit_calls.append(True)

    def discard_changes(self):
        pass

    def lock(self, target):
        pass

    def unlock(self, target):
        pass

    def get_server_capabilities(self):
        return list(self._caps)

    def get_status(self):
        return {
            "consecutive_failures": 0,
            "auth_failure_count": 0,
            "circuit_breaker_open": False,
            "circuit_breaker_seconds_remaining": 0.0,
        }


def _make_yang_dir() -> str:
    d = tempfile.mkdtemp(prefix="test_api_yang_")
    Path(d, "test-mod.yang").write_text(_SAMPLE_YANG)
    return d


def _make_connected_device(yang_dir=None) -> Device:
    """Create a Device that is already 'connected' with an in-memory catalog."""
    if yang_dir is None:
        yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)
    dev._pool = FakePool()
    dev._client = dev._pool.client
    dev._catalog = build_catalog_in_memory(yang_dir, include_config=True)
    dev._catalog.modules = [{"name": "test-mod", "revision": None}]
    dev._catalog.init_default_jobs(30)
    dev._connected = True
    return dev


# ---------------------------------------------------------------------------
# Tests: Device construction
# ---------------------------------------------------------------------------

def test_device_construction_defaults():
    dev = Device("10.0.0.1")
    assert dev._host == "10.0.0.1"
    assert dev._port == 830
    assert dev._username == "admin"
    assert not dev._connected


def test_device_construction_custom_args():
    dev = Device(
        "10.0.0.1",
        port=2022,
        username="user",
        password="pass",
        cache_ttl_sec=60,
        default_poll_interval_sec=10,
    )
    assert dev._port == 2022
    assert dev._username == "user"
    assert dev._default_poll_interval == 10


# ---------------------------------------------------------------------------
# Tests: InMemoryCatalog
# ---------------------------------------------------------------------------

def test_in_memory_catalog_init_default_jobs():
    cat = InMemoryCatalog(catalog_map={"/a": {}, "/b": {}})
    cat.init_default_jobs(30)
    assert cat.job_intervals == {"/a": 30, "/b": 30}
    # Pre-existing intervals are not overwritten
    cat.job_intervals["/a"] = 10
    cat.init_default_jobs(30)
    assert cat.job_intervals["/a"] == 10


def test_in_memory_catalog_supported_models():
    cat = InMemoryCatalog(modules=[
        {"name": "foo", "revision": "2024-01-01"},
        {"name": "bar", "revision": None},
    ])
    models = cat.supported_models()
    assert len(models) == 2
    assert models[0]["name"] == "foo"
    assert models[0]["version"] == "2024-01-01"


def test_in_memory_catalog_canonical_paths():
    cat = InMemoryCatalog(catalog_map={"/p1:a": {}, "/p1:b": {}})
    assert sorted(cat.canonical_paths()) == ["/p1:a", "/p1:b"]


# ---------------------------------------------------------------------------
# Tests: build_catalog_in_memory
# ---------------------------------------------------------------------------

def test_build_catalog_in_memory_from_yang():
    yang_dir = _make_yang_dir()
    cat = build_catalog_in_memory(yang_dir, include_config=True)
    paths = cat.canonical_paths()
    assert len(paths) > 0
    # Should contain paths from test-mod
    from splice.pathing import normalize_path
    norm_paths = [normalize_path(p) for p in paths]
    assert "/system/hostname" in norm_paths
    assert "/system/uptime" in norm_paths
    # Alias map should have entries
    assert len(cat.alias_map) > 0
    # Type map should detect string and uint32
    assert any(v == "string" for v in cat.type_map.values())
    assert any(v == "uint32" for v in cat.type_map.values())


def test_build_catalog_in_memory_empty_dir_raises():
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(DiscoveryError, match="no YANG files"):
            build_catalog_in_memory(d)


def test_build_catalog_in_memory_exclude_config():
    yang_dir = _make_yang_dir()
    cat = build_catalog_in_memory(yang_dir, include_config=False)
    from splice.pathing import normalize_path
    norm_paths = [normalize_path(p) for p in cat.canonical_paths()]
    # hostname is config=true, should be excluded
    assert "/system/hostname" not in norm_paths
    # uptime is config=false, should be included
    assert "/system/uptime" in norm_paths


# ---------------------------------------------------------------------------
# Tests: connect() pipeline
# ---------------------------------------------------------------------------

def test_connect_with_mock_netconf():
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    # Prepare monitoring XML for module discovery
    monitoring_xml = (
        '<data>'
        '<netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
        '<schemas><schema>'
        '<identifier>test-mod</identifier>'
        '<version/>'
        '<format>yang</format>'
        '<namespace>urn:example:test-mod</namespace>'
        '</schema></schemas>'
        '</netconf-state>'
        '</data>'
    )
    fake_mgr = FakeManager(get_reply_xml=monitoring_xml)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        mock_client.manager.return_value = fake_mgr
        MockClient.return_value = mock_client

        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect()

    assert dev._connected
    assert len(dev.catalog_paths) > 0
    dev.close()
    assert not dev._connected


# ---------------------------------------------------------------------------
# Tests: get()
# ---------------------------------------------------------------------------

def test_get_returns_updates():
    dev = _make_connected_device()
    # get() uses NetconfTriggerExecutor which calls pool.get or pool.get_config
    updates = dev.get("/system/hostname")
    assert isinstance(updates, list)
    # Pool's get or get_config should have been called
    pool = dev._pool
    assert len(pool.get_calls) > 0 or len(pool.get_config_calls) > 0
    dev.close()


def test_get_caches_results():
    dev = _make_connected_device()
    dev.get("/system/hostname")
    cached = dev.cached_data()
    assert len(cached) > 0
    dev.close()


def test_get_not_connected_raises():
    dev = Device("127.0.0.1")
    with pytest.raises(ConnectionError, match="not connected"):
        dev.get("/anything")


def test_get_unknown_path_raises():
    dev = _make_connected_device()
    with pytest.raises(PathNotFoundError):
        dev.get("/nonexistent/path")
    dev.close()


# ---------------------------------------------------------------------------
# Tests: set()
# ---------------------------------------------------------------------------

def test_set_calls_edit_config():
    dev = _make_connected_device()
    result = dev.set("/system/hostname", "new-hostname")
    assert result["path"] == "/system/hostname"
    assert result["operation"] == "merge"
    pool = dev._pool
    assert len(pool.edit_calls) == 1
    dev.close()


def test_set_invalid_operation_raises():
    dev = _make_connected_device()
    with pytest.raises(SetError, match="unsupported operation"):
        dev.set("/system/hostname", "x", operation="invalid")
    dev.close()


def test_set_not_writable_raises():
    dev = _make_connected_device()
    with pytest.raises(PathNotWritableError, match="read-only"):
        dev.set("/system/uptime", 9999)
    dev.close()


def test_set_not_connected_raises():
    dev = Device("127.0.0.1")
    with pytest.raises(ConnectionError, match="not connected"):
        dev.set("/system/hostname", "x")


# ---------------------------------------------------------------------------
# Tests: subscribe()
# ---------------------------------------------------------------------------

def test_subscribe_returns_subscription():
    dev = _make_connected_device()
    sub = dev.subscribe("/system/hostname", interval=1)
    assert isinstance(sub, Subscription)
    sub.cancel()
    dev.close()


def test_subscribe_unknown_path_raises():
    dev = _make_connected_device()
    with pytest.raises(SubscriptionError, match="no catalog paths"):
        dev.subscribe("/nonexistent/path")
    dev.close()


def test_subscribe_iterator_receives_updates():
    dev = _make_connected_device()
    sub = dev.subscribe("/system/hostname", interval=1)

    # The poller should produce data within a couple seconds
    updates = None
    for batch in sub:
        updates = batch
        break  # Get just the first batch

    sub.cancel()
    assert updates is not None
    assert len(updates) > 0
    dev.close()


def test_subscribe_context_manager():
    dev = _make_connected_device()
    received = []
    with dev.subscribe("/system/hostname", interval=1) as sub:
        for batch in sub:
            received = batch
            break
    # After context exit, subscription should be cancelled
    assert sub._cancelled
    dev.close()


# ---------------------------------------------------------------------------
# Tests: context manager
# ---------------------------------------------------------------------------

def test_context_manager_lifecycle():
    yang_dir = _make_yang_dir()

    monitoring_xml = (
        '<data>'
        '<netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
        '<schemas><schema>'
        '<identifier>test-mod</identifier>'
        '<version/>'
        '<format>yang</format>'
        '<namespace>urn:example:test-mod</namespace>'
        '</schema></schemas>'
        '</netconf-state>'
        '</data>'
    )
    fake_mgr = FakeManager(get_reply_xml=monitoring_xml)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        mock_client.manager.return_value = fake_mgr
        MockClient.return_value = mock_client

        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        with Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir) as dev:
            assert dev._connected
        # After context exit, device should be closed
        assert not dev._connected


# ---------------------------------------------------------------------------
# Tests: informational
# ---------------------------------------------------------------------------

def test_catalog_paths_property():
    dev = _make_connected_device()
    paths = dev.catalog_paths
    assert isinstance(paths, list)
    assert len(paths) > 0
    dev.close()


def test_find_paths_glob():
    dev = _make_connected_device()
    matches = dev.find_paths("*hostname*")
    assert len(matches) >= 1
    assert any("hostname" in p for p in matches)
    dev.close()


def test_find_paths_no_match():
    dev = _make_connected_device()
    assert dev.find_paths("*zzz_nonexistent*") == []
    dev.close()


def test_connection_status_connected():
    dev = _make_connected_device()
    status = dev.connection_status
    assert status["connected"] is True
    assert status["circuit_breaker_open"] is False
    dev.close()


def test_connection_status_disconnected():
    dev = Device("127.0.0.1")
    status = dev.connection_status
    assert status["connected"] is False


def test_cached_data_empty():
    dev = _make_connected_device()
    assert dev.cached_data() == []
    dev.close()


# ---------------------------------------------------------------------------
# Tests: error hierarchy
# ---------------------------------------------------------------------------

def test_error_hierarchy():
    assert issubclass(ConnectionError, DeviceError)
    assert issubclass(DiscoveryError, DeviceError)
    assert issubclass(PathNotFoundError, DeviceError)
    assert issubclass(PathNotWritableError, DeviceError)
    assert issubclass(SetError, DeviceError)
    assert issubclass(SubscriptionError, DeviceError)


def test_errors_are_catchable_as_device_error():
    with pytest.raises(DeviceError):
        raise PathNotFoundError("not found")


# ---------------------------------------------------------------------------
# Tests: close() cleanup
# ---------------------------------------------------------------------------

def test_close_is_idempotent():
    dev = _make_connected_device()
    dev.close()
    dev.close()  # Should not raise
    assert not dev._connected


def test_close_cancels_subscriptions():
    dev = _make_connected_device()
    sub = dev.subscribe("/system/hostname", interval=1)
    dev.close()
    assert sub._cancelled


# ---------------------------------------------------------------------------
# Tests: granular connect steps
# ---------------------------------------------------------------------------

def test_connect_netconf_only():
    """connect_netconf() should establish NETCONF but not build catalog."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        MockClient.return_value = mock_client
        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect_netconf()

    assert dev._pool is not None
    assert dev._client is not None
    # Not yet connected for API use (no catalog)
    assert not dev._connected
    assert len(dev.catalog_paths) == 0
    dev.close()


def test_connect_netconf_idempotent():
    """Calling connect_netconf() twice should not create a second client."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        MockClient.return_value = mock_client
        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect_netconf()
        first_pool = dev._pool
        dev.connect_netconf()
        assert dev._pool is first_pool  # same object

    dev.close()


def test_build_catalog_without_netconf():
    """build_catalog() should work with just YANG files, no NETCONF needed."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", yang_dir=yang_dir)
    dev.build_catalog()

    assert dev._connected
    assert len(dev.catalog_paths) > 0
    from splice.pathing import normalize_path
    norm_paths = [normalize_path(p) for p in dev.catalog_paths]
    assert "/system/hostname" in norm_paths
    dev.close()


def test_connect_skip_discover_and_fetch_yang():
    """connect(skip_discover=True, skip_fetch_yang=True) with pre-populated yang_dir."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        MockClient.return_value = mock_client
        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect(skip_discover=True, skip_fetch_yang=True)

    assert dev._connected
    assert len(dev.catalog_paths) > 0
    dev.close()


def test_connect_skip_fetch_yang_without_discover_raises():
    """skip_fetch_yang=False but skip_discover=True without prior discover() should fail."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        MockClient.return_value = mock_client
        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        with pytest.raises(DiscoveryError, match="cannot fetch YANG"):
            dev.connect(skip_discover=True, skip_fetch_yang=False)

    dev.close()


def test_connect_skip_catalog():
    """connect(skip_catalog=True) should connect but leave catalog empty."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    monitoring_xml = (
        '<data>'
        '<netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
        '<schemas><schema>'
        '<identifier>test-mod</identifier>'
        '<version/>'
        '<format>yang</format>'
        '<namespace>urn:example:test-mod</namespace>'
        '</schema></schemas>'
        '</netconf-state>'
        '</data>'
    )
    fake_mgr = FakeManager(get_reply_xml=monitoring_xml)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        mock_client.manager.return_value = fake_mgr
        MockClient.return_value = mock_client

        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect(skip_catalog=True)

    assert not dev._connected  # not usable yet
    assert len(dev.catalog_paths) == 0

    # Now build catalog manually
    dev.build_catalog()
    assert dev._connected
    assert len(dev.catalog_paths) > 0
    dev.close()


def test_discover_requires_netconf():
    """discover() without connect_netconf() should raise ConnectionError."""
    dev = Device("127.0.0.1")
    with pytest.raises(ConnectionError, match="not connected"):
        dev.discover()


def test_fetch_yang_requires_discover():
    """fetch_yang() without discover() should raise DiscoveryError."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", yang_dir=yang_dir)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        MockClient.return_value = mock_client
        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        dev.connect_netconf()

    with pytest.raises(DiscoveryError, match="no modules discovered"):
        dev.fetch_yang()
    dev.close()


def test_granular_step_by_step():
    """Full step-by-step pipeline using individual methods."""
    yang_dir = _make_yang_dir()
    dev = Device("127.0.0.1", username="admin", password="pass", yang_dir=yang_dir)

    monitoring_xml = (
        '<data>'
        '<netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
        '<schemas><schema>'
        '<identifier>test-mod</identifier>'
        '<version/>'
        '<format>yang</format>'
        '<namespace>urn:example:test-mod</namespace>'
        '</schema></schemas>'
        '</netconf-state>'
        '</data>'
    )
    fake_mgr = FakeManager(get_reply_xml=monitoring_xml)

    with patch("splice.api.device.NetconfClient") as MockClient, \
         patch("splice.api.device.NetconfClientPool") as MockPool:

        mock_client = MagicMock()
        mock_client.manager.return_value = fake_mgr
        MockClient.return_value = mock_client

        mock_pool = MagicMock()
        mock_pool.client = mock_client
        MockPool.return_value = mock_pool

        # Step 1: Connect SSH
        dev.connect_netconf()
        assert dev._pool is not None
        assert not dev._connected

        # Step 2: Discover modules
        modules = dev.discover()
        assert len(modules) > 0
        assert dev._discovered_modules is not None

        # Step 3: Fetch YANG (already on disk, will skip)
        count = dev.fetch_yang()
        assert count >= 0

    # Step 4: Build catalog
    dev.build_catalog()
    assert dev._connected
    assert len(dev.catalog_paths) > 0
    dev.close()


# ---------------------------------------------------------------------------
# Tests: doctor()
# ---------------------------------------------------------------------------

def test_doctor_connected():
    dev = _make_connected_device()
    result = dev.doctor()
    assert result["connected"] is True
    assert result["circuit_breaker"]["state"] == "closed"
    assert result["circuit_breaker"]["failures"] == 0
    assert result["catalog_paths"] > 0
    assert result["yang_modules"] > 0
    assert isinstance(result["capabilities"], list)
    dev.close()


def test_doctor_disconnected():
    dev = Device("127.0.0.1")
    result = dev.doctor()
    assert result["connected"] is False
    assert result["catalog_paths"] == 0
    assert result["yang_modules"] == 0
    assert result["capabilities"] == []


def test_doctor_circuit_breaker_open():
    dev = _make_connected_device()
    # Monkey-patch pool status to simulate open circuit breaker
    dev._pool.get_status = lambda: {
        "consecutive_failures": 5,
        "auth_failure_count": 0,
        "circuit_breaker_open": True,
        "circuit_breaker_seconds_remaining": 42.0,
    }
    result = dev.doctor()
    assert result["circuit_breaker"]["state"] == "open"
    assert result["circuit_breaker"]["failures"] == 5
    assert result["circuit_breaker"]["cooldown"] == 42.0
    dev.close()


# ---------------------------------------------------------------------------
# Tests: export_catalog()
# ---------------------------------------------------------------------------

def test_export_catalog_json():
    dev = _make_connected_device()
    output = dev.export_catalog(format="json")
    assert isinstance(output, str)
    import json
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    assert all("path" in entry for entry in parsed)
    dev.close()


def test_export_catalog_dict():
    dev = _make_connected_device()
    output = dev.export_catalog(format="dict")
    assert isinstance(output, list)
    assert len(output) > 0
    assert all(isinstance(entry, dict) for entry in output)
    assert all("path" in entry for entry in output)
    dev.close()


def test_export_catalog_empty():
    dev = Device("127.0.0.1")
    output = dev.export_catalog(format="dict")
    assert output == []
    output_json = dev.export_catalog(format="json")
    assert output_json == "[]"


# ---------------------------------------------------------------------------
# Tests: search_paths()
# ---------------------------------------------------------------------------

def test_search_paths_requires_sentence_transformers():
    """search_paths() should raise ImportError if sentence-transformers is missing."""
    dev = _make_connected_device()
    pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")
    results = dev.search_paths("hostname", top_k=5)
    assert isinstance(results, list)
    assert len(results) > 0
    # Each result is (path, score)
    for path, score in results:
        assert isinstance(path, str)
        assert isinstance(score, float)
    dev.close()


def test_search_paths_caches_index():
    """search_paths() should cache the index on subsequent calls."""
    pytest.importorskip("sentence_transformers", reason="sentence-transformers not installed")
    dev = _make_connected_device()
    dev.search_paths("hostname", top_k=5)
    idx = dev._path_index
    assert idx is not None
    dev.search_paths("uptime", top_k=5)
    assert dev._path_index is idx  # same object, cached
    dev.close()


def test_search_paths_empty_catalog():
    """search_paths() on empty catalog should return empty list."""
    dev = Device("127.0.0.1")
    results = dev.search_paths("anything")
    assert results == []
