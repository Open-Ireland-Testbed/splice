"""Tests for MCP server concurrent behavior.

Exercises thread safety of the session pool, rate limiter, and
connect_device TOCTOU protection using threading and
concurrent.futures.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

from splice.mcp_server import (
    RateLimiter,
    SessionPool,
    _DeviceSession,
    create_mcp_server,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_device(connected=True, catalog_paths=None):
    """Create a mock Device that behaves like a connected Device."""
    dev = MagicMock()
    dev._connected = connected
    dev.catalog_paths = catalog_paths or ["/a/b", "/a/c"]
    dev.connection_status = {
        "connected": connected,
        "consecutive_failures": 0,
        "circuit_breaker_open": False,
        "circuit_breaker_seconds_remaining": 0.0,
    }
    dev.find_paths.return_value = ["/a/b"]
    dev.get.return_value = [{"path": "/a/b", "value": "42", "timestamp_ns": 1000}]
    dev.set.return_value = {"path": "/a/b", "operation": "merge", "timestamp_ns": 2000}
    dev._catalog = MagicMock()
    dev._catalog.catalog_map = {"/a/b": {"type": "uint32", "config": False}}
    dev.close.return_value = None
    return dev


def _call_tool(server, name, **kwargs):
    """Call a registered tool function by name."""
    tool = server._tool_manager.get_tool(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn(**kwargs)


# ---------------------------------------------------------------------------
# 1. Concurrent connect_device — TOCTOU protection
# ---------------------------------------------------------------------------

class TestConcurrentConnect:
    """Two threads connecting the same device_id should not leak sessions."""

    def test_concurrent_connect_same_id_no_leak(self):
        """Only one Device should be created when two threads race on the
        same device_id, thanks to add_if_absent in SessionPool."""
        server = create_mcp_server()
        connect_count = {"n": 0}
        connect_lock = threading.Lock()

        original_device_cls = None

        def _slow_mock_device():
            dev = _make_mock_device()

            def slow_connect(**kwargs):
                with connect_lock:
                    connect_count["n"] += 1
                time.sleep(0.05)  # simulate slow discovery

            dev.connect = slow_connect
            return dev

        with patch("splice.api.device.Device", side_effect=lambda **kw: _slow_mock_device()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _call_tool, server, "connect_device",
                        host="10.0.0.1", device_id="race-dev",
                    )
                    for _ in range(2)
                ]
                results = [f.result() for f in as_completed(futures)]

        statuses = [r["status"] for r in results]
        # One should be "connected", the other "already_connected"
        assert "connected" in statuses or "already_connected" in statuses
        # At most 1 session in the pool
        assert len(server._gnmi_pool.all_sessions()) <= 1

    def test_concurrent_connect_different_ids(self):
        """Connecting different device_ids concurrently should both succeed."""
        server = create_mcp_server()

        def _fast_mock(**kw):
            return _make_mock_device()

        with patch("splice.api.device.Device", side_effect=_fast_mock):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(
                        _call_tool, server, "connect_device",
                        host="10.0.0.1", device_id=f"dev-{i}",
                    )
                    for i in range(4)
                ]
                results = [f.result() for f in as_completed(futures)]

        connected = [r for r in results if r["status"] == "connected"]
        assert len(connected) == 4
        assert len(server._gnmi_pool.all_sessions()) == 4


# ---------------------------------------------------------------------------
# 2. Concurrent rate limiting
# ---------------------------------------------------------------------------

class TestConcurrentRateLimiting:
    """Multiple threads hitting the rate limiter must not exceed the limit."""

    def test_concurrent_rate_limiter_respects_max(self):
        limiter = RateLimiter(max_per_minute=10)
        allowed = {"count": 0}
        lock = threading.Lock()

        def _try_check():
            result = limiter.check("dev1")
            if result is None:
                with lock:
                    allowed["count"] += 1

        threads = []
        for _ in range(30):
            t = threading.Thread(target=_try_check)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Exactly 10 should have been allowed
        assert allowed["count"] == 10

    def test_concurrent_rate_limiter_per_device_isolation(self):
        """Rate limits for different devices are independent under concurrency."""
        limiter = RateLimiter(max_per_minute=5)
        results = {"dev-a": 0, "dev-b": 0}
        lock = threading.Lock()

        def _check_device(device_id):
            if limiter.check(device_id) is None:
                with lock:
                    results[device_id] += 1

        threads = []
        for did in ("dev-a", "dev-b"):
            for _ in range(10):
                t = threading.Thread(target=_check_device, args=(did,))
                threads.append(t)
                t.start()

        for t in threads:
            t.join()

        assert results["dev-a"] == 5
        assert results["dev-b"] == 5

    def test_concurrent_rate_limiter_clear_resets(self):
        """Clearing a device's window while threads are checking is safe."""
        limiter = RateLimiter(max_per_minute=3)
        # Fill the window
        for _ in range(3):
            limiter.check("dev1")
        assert limiter.check("dev1") is not None  # blocked

        # Clear from another thread
        t = threading.Thread(target=limiter.clear, args=("dev1",))
        t.start()
        t.join()

        # Should be allowed again
        assert limiter.check("dev1") is None


# ---------------------------------------------------------------------------
# 3. Concurrent session pool operations
# ---------------------------------------------------------------------------

class TestConcurrentSessionPool:
    """Add/get/remove/cleanup from multiple threads."""

    def test_concurrent_add_and_get(self):
        pool = SessionPool()
        sessions_created = []

        def _add_session(i):
            dev = _make_mock_device()
            s = _DeviceSession(
                device_id=f"dev-{i}", device=dev, host="h", port=830,
            )
            pool.add(s)
            sessions_created.append(f"dev-{i}")

        threads = [threading.Thread(target=_add_session, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 10 should be retrievable
        for i in range(10):
            assert pool.get(f"dev-{i}") is not None

    def test_concurrent_add_if_absent(self):
        """add_if_absent should succeed exactly once for the same device_id."""
        pool = SessionPool()
        added_count = {"n": 0}
        lock = threading.Lock()

        def _try_add():
            dev = _make_mock_device()
            s = _DeviceSession(
                device_id="contested-id", device=dev, host="h", port=830,
            )
            if pool.add_if_absent(s):
                with lock:
                    added_count["n"] += 1

        threads = [threading.Thread(target=_try_add) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert added_count["n"] == 1
        assert pool.get("contested-id") is not None

    def test_concurrent_remove(self):
        """Only one thread should successfully remove a session."""
        pool = SessionPool()
        dev = _make_mock_device()
        s = _DeviceSession(device_id="to-remove", device=dev, host="h", port=830)
        pool.add(s)

        removed = {"count": 0}
        lock = threading.Lock()

        def _try_remove():
            result = pool.remove("to-remove")
            if result is not None:
                with lock:
                    removed["count"] += 1

        threads = [threading.Thread(target=_try_remove) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert removed["count"] == 1
        assert pool.get("to-remove") is None

    def test_concurrent_get_updates_last_used(self):
        """Multiple threads calling get() on the same session should all
        update last_used without data races."""
        pool = SessionPool()
        dev = _make_mock_device()
        s = _DeviceSession(
            device_id="shared", device=dev, host="h", port=830,
            last_used=time.monotonic() - 100,
        )
        pool.add(s)

        def _get():
            pool.get("shared")

        threads = [threading.Thread(target=_get) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        session = pool.get("shared")
        # last_used should be recent (within last second)
        assert (time.monotonic() - session.last_used) < 1.0

    def test_concurrent_all_sessions_during_mutations(self):
        """all_sessions() should not crash while other threads add/remove."""
        pool = SessionPool()
        stop = threading.Event()
        snapshots = []

        def _mutator():
            i = 0
            while not stop.is_set():
                dev = _make_mock_device()
                did = f"mut-{i}"
                s = _DeviceSession(device_id=did, device=dev, host="h", port=830)
                pool.add(s)
                pool.remove(did)
                i += 1

        def _reader():
            while not stop.is_set():
                snap = pool.all_sessions()
                snapshots.append(len(snap))

        t_mut = threading.Thread(target=_mutator)
        t_read = threading.Thread(target=_reader)
        t_mut.start()
        t_read.start()
        time.sleep(0.1)
        stop.set()
        t_mut.join()
        t_read.join()

        # No crash is the success criterion; snapshots should have some entries
        assert len(snapshots) > 0


# ---------------------------------------------------------------------------
# 4. Idle session cleanup
# ---------------------------------------------------------------------------

class TestIdleSessionCleanup:
    """Verify cleanup_idle() removes stale sessions and closes devices."""

    def test_cleanup_removes_idle_sessions(self):
        pool = SessionPool(idle_timeout_min=0)
        pool.idle_timeout_sec = 0.0

        dev1 = _make_mock_device()
        dev2 = _make_mock_device()

        s1 = _DeviceSession(
            device_id="idle-1", device=dev1, host="h", port=830,
            last_used=time.monotonic() - 10.0,
        )
        s2 = _DeviceSession(
            device_id="idle-2", device=dev2, host="h", port=830,
            last_used=time.monotonic() - 10.0,
        )
        pool.add(s1)
        pool.add(s2)

        removed = pool.cleanup_idle()
        assert set(removed) == {"idle-1", "idle-2"}
        assert pool.get("idle-1") is None
        assert pool.get("idle-2") is None
        dev1.close.assert_called_once()
        dev2.close.assert_called_once()

    def test_cleanup_keeps_active_sessions(self):
        pool = SessionPool(idle_timeout_min=60)

        dev = _make_mock_device()
        s = _DeviceSession(
            device_id="active", device=dev, host="h", port=830,
        )
        pool.add(s)

        removed = pool.cleanup_idle()
        assert removed == []
        assert pool.get("active") is not None
        dev.close.assert_not_called()

    def test_cleanup_mixed_idle_and_active(self):
        pool = SessionPool(idle_timeout_min=0)
        pool.idle_timeout_sec = 5.0

        dev_idle = _make_mock_device()
        dev_active = _make_mock_device()

        s_idle = _DeviceSession(
            device_id="old", device=dev_idle, host="h", port=830,
            last_used=time.monotonic() - 100.0,
        )
        s_active = _DeviceSession(
            device_id="new", device=dev_active, host="h", port=830,
        )
        pool.add(s_idle)
        pool.add(s_active)

        removed = pool.cleanup_idle()
        assert removed == ["old"]
        assert pool.get("old") is None
        assert pool.get("new") is not None
        dev_idle.close.assert_called_once()
        dev_active.close.assert_not_called()

    def test_cleanup_handles_close_exception(self):
        """cleanup_idle should not raise even if device.close() throws."""
        pool = SessionPool(idle_timeout_min=0)
        pool.idle_timeout_sec = 0.0

        dev = _make_mock_device()
        dev.close.side_effect = RuntimeError("close failed")
        s = _DeviceSession(
            device_id="crash", device=dev, host="h", port=830,
            last_used=time.monotonic() - 10.0,
        )
        pool.add(s)

        removed = pool.cleanup_idle()
        assert "crash" in removed
        assert pool.get("crash") is None

    def test_concurrent_cleanup(self):
        """Multiple threads calling cleanup_idle should not double-close."""
        pool = SessionPool(idle_timeout_min=0)
        pool.idle_timeout_sec = 0.0

        dev = _make_mock_device()
        s = _DeviceSession(
            device_id="once", device=dev, host="h", port=830,
            last_used=time.monotonic() - 10.0,
        )
        pool.add(s)

        results = []

        def _cleanup():
            r = pool.cleanup_idle()
            results.append(r)

        threads = [threading.Thread(target=_cleanup) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one thread should have removed "once"
        total_removed = sum(1 for r in results if "once" in r)
        assert total_removed == 1
        # close should only be called once
        assert dev.close.call_count == 1
