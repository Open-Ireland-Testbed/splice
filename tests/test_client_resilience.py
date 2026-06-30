"""Tests for NetconfClient.close(graceful) and NetconfClientPool retry behaviour."""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from splice.netconf.client import CircuitBreakerOpen, NetconfClient, NetconfClientPool, _force_close_session


# ---------------------------------------------------------------------------
# NetconfClient.close() tests
# ---------------------------------------------------------------------------

def _make_client_with_mock_mgr():
    """Return a NetconfClient whose _mgr is a mock (skip real SSH)."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    mgr = MagicMock()
    client._mgr = mgr
    return client, mgr


def test_close_graceful_sends_rpc():
    client, mgr = _make_client_with_mock_mgr()
    client.close(graceful=True)
    mgr.close_session.assert_called_once()
    assert client._mgr is None


def test_close_not_graceful_skips_rpc():
    client, mgr = _make_client_with_mock_mgr()
    # Give the mock a session with a transport to verify force-close path
    transport = MagicMock()
    session_obj = MagicMock()
    session_obj._transport = transport
    mgr._session = session_obj

    client.close(graceful=False)
    mgr.close_session.assert_not_called()
    # _force_close_session calls session.close() (handles transport+channel+state)
    session_obj.close.assert_called_once()
    assert client._mgr is None


def test_close_graceful_on_dead_session_no_hang():
    """close_session() raises on a dead session — session still force-closed, no exception."""
    client, mgr = _make_client_with_mock_mgr()
    mgr.close_session.side_effect = Exception("session dead")
    session_obj = MagicMock()
    mgr._session = session_obj

    # Should not raise
    client.close(graceful=True)
    mgr.close_session.assert_called_once()
    # _force_close_session calls session.close() as fallback
    session_obj.close.assert_called_once()
    assert client._mgr is None


def test_close_none_mgr_is_noop():
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    assert client._mgr is None
    client.close()  # should not raise


# ---------------------------------------------------------------------------
# NetconfClientPool: reconnect-once semantics
# ---------------------------------------------------------------------------

def test_pool_success_no_reconnect():
    """Successful RPC — no close, no reconnect."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(return_value="<ok/>")
    client.close = MagicMock()

    result = pool.get("<filter/>")
    assert result == "<ok/>"
    client.get.assert_called_once()
    client.close.assert_not_called()


def test_pool_reconnects_once_on_failure():
    """First attempt fails, reconnect once, retry succeeds."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(side_effect=[Exception("session dead"), "<ok/>"])
    client.close = MagicMock()

    result = pool.get("<filter/>")
    assert result == "<ok/>"
    assert client.get.call_count == 2  # original + 1 retry
    assert client.close.call_count == 1  # closed the dead session once


def test_pool_edit_config_reconnects_once_on_failure():
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.edit_config = MagicMock(side_effect=[Exception("session dead"), "<ok/>"])
    client.close = MagicMock()

    result = pool.edit_config("running", "<config/>")
    assert result == "<ok/>"
    assert client.edit_config.call_count == 2
    assert client.close.call_count == 1


def test_pool_server_capabilities_cached_once():
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    mgr.server_capabilities = ["urn:ietf:params:netconf:capability:writable-running:1.0"]
    client.manager = MagicMock(return_value=mgr)
    client.close = MagicMock()

    caps1 = pool.get_server_capabilities()
    caps2 = pool.get_server_capabilities()

    assert caps1 == caps2
    client.manager.assert_called_once()
    client.close.assert_not_called()


def test_pool_only_two_attempts_max():
    """Both attempts fail — exactly 2 calls to client.get, then raise."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(side_effect=Exception("fail"))
    client.close = MagicMock()

    try:
        pool.get("<filter/>")
        assert False, "should have raised"
    except Exception:
        pass

    # Exactly 2 attempts: original + 1 reconnect retry
    assert client.get.call_count == 2
    # 2 close calls: one after first failure, one after retry failure
    assert client.close.call_count == 2


def test_pool_close_uses_non_graceful():
    """All close() calls on failure should use graceful=False."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(side_effect=Exception("fail"))
    client.close = MagicMock()

    try:
        pool.get("<filter/>")
    except Exception:
        pass

    for call in client.close.call_args_list:
        assert call.kwargs.get("graceful") is False


def test_pool_reconnect_delay():
    """Verify sleep is called once with reconnect_delay_sec before the retry."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=2.0)

    client.get = MagicMock(side_effect=Exception("fail"))
    client.close = MagicMock()

    with patch("splice.netconf.client.time.sleep") as mock_sleep:
        try:
            pool.get("<filter/>")
        except Exception:
            pass

        # Only 1 sleep — before the single reconnect attempt
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args_list[0][0][0] == 2.0


def test_pool_logs_warning(caplog):
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(side_effect=Exception("test-error"))
    client.close = MagicMock()

    with caplog.at_level(logging.WARNING):
        try:
            pool.get("<filter/>")
        except Exception:
            pass

    assert any("NETCONF get failed" in r.message for r in caplog.records)


def test_pool_auth_failure_no_retry():
    """Auth errors should raise immediately — no reconnect attempt."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    client.get = MagicMock(side_effect=Exception("AuthenticationException('Authentication failed.')"))
    client.close = MagicMock()

    with patch("splice.netconf.client.time.sleep") as mock_sleep:
        try:
            pool.get("<filter/>")
        except Exception:
            pass

        mock_sleep.assert_not_called()
        # Only 1 call — no retry after auth failure
        assert client.get.call_count == 1


def test_pool_auth_failure_long_cooldown():
    """Auth failures should immediately trigger long cooldown."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(
        client, reconnect_delay_sec=0,
        auth_failure_cooldown_sec=300.0,
    )

    client.get = MagicMock(side_effect=Exception("Authentication failed"))
    client.close = MagicMock()

    # First auth failure
    try:
        pool.get("<filter/>")
    except Exception:
        pass

    # Circuit should be open with auth cooldown
    status = pool.get_status()
    assert status["circuit_breaker_open"] is True
    assert status["auth_failure_count"] == 1
    assert status["circuit_breaker_seconds_remaining"] > 250  # Near 300s

    # Next call should fail fast without touching client.get
    client.get.reset_mock()
    try:
        pool.get("<filter/>")
        assert False, "should have raised RuntimeError while breaker open"
    except RuntimeError as e:
        assert "Circuit breaker open" in str(e)
    client.get.assert_not_called()


def test_pool_passive_status_no_network_calls():
    """get_status() should not trigger any network activity."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client)

    # get_status should work without calling client.connect or client.get
    client.connect = MagicMock()
    client.get = MagicMock()

    status = pool.get_status()

    assert "consecutive_failures" in status
    assert "circuit_breaker_open" in status
    assert "circuit_breaker_seconds_remaining" in status
    assert "auth_failure_count" in status
    client.connect.assert_not_called()
    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def test_circuit_breaker_opens_after_consecutive_failures():
    """After N consecutive failures, further calls should fail-fast without connecting."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(
        client, reconnect_delay_sec=0,
        circuit_breaker_threshold=3, circuit_breaker_cooldown_sec=60.0,
    )

    client.get = MagicMock(side_effect=Exception("session dead"))
    client.close = MagicMock()

    # Exhaust the circuit breaker threshold
    # Each pool.get() = 2 attempts (original + retry) = 1 failure recorded
    for _ in range(3):
        try:
            pool.get("<filter/>")
        except Exception:
            pass

    assert pool._consecutive_failures >= 3

    # Now the circuit breaker should be open — next call fails immediately
    client.get.reset_mock()
    try:
        pool.get("<filter/>")
    except RuntimeError as e:
        assert "Circuit breaker open" in str(e)
    # client.get should NOT have been called — circuit breaker blocked it
    client.get.assert_not_called()


def test_circuit_breaker_resets_on_success():
    """A successful call should reset the failure counter."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(
        client, reconnect_delay_sec=0,
        circuit_breaker_threshold=3,
    )

    client.close = MagicMock()

    # 2 failures (each pool.get records 1 failure)
    client.get = MagicMock(side_effect=Exception("fail"))
    for _ in range(2):
        try:
            pool.get("<filter/>")
        except Exception:
            pass
    assert pool._consecutive_failures == 2

    # 1 success — should reset counter
    client.get = MagicMock(return_value="<ok/>")
    pool.get("<filter/>")
    assert pool._consecutive_failures == 0


# ---------------------------------------------------------------------------
# Concurrency tests
# ---------------------------------------------------------------------------

def test_concurrent_connect_get_calls_manager_connect_once():
    """Concurrent connect/get should only call manager.connect once on first attach."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    connect_call_count = 0

    def counting_connect(*args, **kwargs):
        nonlocal connect_call_count
        connect_call_count += 1
        mgr = MagicMock()
        mgr.connected = True
        mgr.timeout = 10
        mgr.server_capabilities = []
        mgr.get = MagicMock(return_value="<ok/>")
        return mgr

    with patch("splice.netconf.client.manager.connect", side_effect=counting_connect):
        threads = []
        errors = []
        results = []

        def concurrent_call():
            try:
                result = client.get("<filter/>")
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Launch multiple threads
        for _ in range(5):
            t = threading.Thread(target=concurrent_call)
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Verify no errors
        assert not errors, f"errors: {errors}"

        # Verify all threads got results
        assert len(results) == 5

        # Verify manager.connect was called only once (first attach)
        assert connect_call_count == 1

    client.close()


def test_pool_serializes_parallel_get_calls():
    """Pool should serialize parallel get calls (max in-flight = 1)."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)

    in_flight_count = 0
    max_in_flight = 0
    in_flight_lock = threading.Lock()

    def slow_get(filter_xml):
        nonlocal in_flight_count, max_in_flight
        with in_flight_lock:
            in_flight_count += 1
            if in_flight_count > max_in_flight:
                max_in_flight = in_flight_count

        # Simulate slow operation
        time.sleep(0.1)

        with in_flight_lock:
            in_flight_count -= 1

        return f"<ok for {filter_xml}/>"

    client.get = MagicMock(side_effect=slow_get)

    threads = []
    results = []

    def concurrent_get():
        try:
            result = pool.get("<filter/>")
            results.append(result)
        except Exception as e:
            results.append(f"ERROR: {e}")

    # Launch multiple threads
    for _ in range(3):
        t = threading.Thread(target=concurrent_get)
        threads.append(t)
        t.start()

    # Wait for all threads
    for t in threads:
        t.join()

    # Verify all threads got results
    assert len(results) == 3
    assert all("ERROR" not in r for r in results)

    # Verify max in-flight was 1 (serialized)
    assert max_in_flight == 1

    # Verify client.get was called 3 times (once per thread)
    assert client.get.call_count == 3


# ---------------------------------------------------------------------------
# _force_close_session() tests
# ---------------------------------------------------------------------------

def test_force_close_session_none_is_noop():
    """Passing None should not raise."""
    _force_close_session(None)


def test_force_close_session_calls_session_close():
    """Should call SSHSession.close() on the manager's _session."""
    mgr = MagicMock()
    session_obj = MagicMock()
    mgr._session = session_obj

    _force_close_session(mgr)
    session_obj.close.assert_called_once()


def test_force_close_session_falls_back_to_transport():
    """If session.close() fails, should fall back to transport.close()."""
    mgr = MagicMock()
    session_obj = MagicMock()
    transport = MagicMock()
    session_obj.close.side_effect = Exception("close failed")
    session_obj._transport = transport
    mgr._session = session_obj

    _force_close_session(mgr)
    transport.close.assert_called_once()


def test_force_close_session_swallows_all_exceptions():
    """Should not raise even if everything fails."""
    mgr = MagicMock()
    session_obj = MagicMock()
    session_obj.close.side_effect = Exception("close failed")
    session_obj._transport = None  # no transport to fall back to
    mgr._session = session_obj

    # Should not raise
    _force_close_session(mgr)


def test_force_close_session_no_session_attr():
    """Should handle manager with no _session attribute."""
    mgr = object()  # no _session attribute at all
    _force_close_session(mgr)  # should not raise


# ---------------------------------------------------------------------------
# NetconfClient.connect() — stale session cleanup
# ---------------------------------------------------------------------------

def test_connect_closes_stale_session_before_reconnecting():
    """connect() should force-close a stale manager before creating a new one."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    # Simulate a stale manager (connected=False)
    old_mgr = MagicMock()
    old_mgr.connected = False
    old_session = MagicMock()
    old_mgr._session = old_session
    client._mgr = old_mgr

    new_mgr = MagicMock()
    new_mgr.connected = True
    new_mgr.timeout = 10
    new_mgr.server_capabilities = []

    with patch("splice.netconf.client.manager.connect", return_value=new_mgr):
        client.connect()

    # Old session should have been force-closed
    old_session.close.assert_called_once()
    # New manager should be set
    assert client._mgr is new_mgr


def test_connect_skips_close_when_already_connected():
    """connect() should not close or reconnect if already connected."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    existing_mgr = MagicMock()
    existing_mgr.connected = True
    existing_session = MagicMock()
    existing_mgr._session = existing_session
    client._mgr = existing_mgr

    with patch("splice.netconf.client.manager.connect") as mock_connect:
        client.connect()

    # Should not have called manager.connect (already connected)
    mock_connect.assert_not_called()
    # Session should NOT have been closed
    existing_session.close.assert_not_called()
    assert client._mgr is existing_mgr


# ---------------------------------------------------------------------------
# NetconfClient.close() — _force_close_session fallback
# ---------------------------------------------------------------------------

def test_close_graceful_failure_falls_back_to_force_close():
    """When close_session() RPC fails, close() should use _force_close_session."""
    client, mgr = _make_client_with_mock_mgr()
    mgr.close_session.side_effect = Exception("RPC failed on dead session")
    session_obj = MagicMock()
    mgr._session = session_obj

    client.close(graceful=True)

    mgr.close_session.assert_called_once()
    # Force close should have been called as fallback
    session_obj.close.assert_called_once()
    assert client._mgr is None


def test_close_graceful_success_does_not_force_close():
    """When close_session() succeeds, close() should return without force-closing."""
    client, mgr = _make_client_with_mock_mgr()
    session_obj = MagicMock()
    mgr._session = session_obj

    client.close(graceful=True)

    mgr.close_session.assert_called_once()
    # Session.close() should NOT have been called (close_session handles it)
    session_obj.close.assert_not_called()
    assert client._mgr is None


# ---------------------------------------------------------------------------
# NetconfClient context manager
# ---------------------------------------------------------------------------

def test_context_manager_calls_close_on_exit():
    """__exit__ should call close()."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    mgr = MagicMock()
    mgr.connected = True
    mgr.timeout = 10
    mgr.server_capabilities = []

    with patch("splice.netconf.client.manager.connect", return_value=mgr):
        with client:
            assert client._mgr is mgr
    # After exiting context, close_session should have been called
    mgr.close_session.assert_called_once()
    assert client._mgr is None


def test_context_manager_calls_close_on_exception():
    """__exit__ should call close() even if an exception occurs."""
    client = NetconfClient(
        host="127.0.0.1", port=830, username="u", password="p",
    )
    mgr = MagicMock()
    mgr.connected = True
    mgr.timeout = 10
    mgr.server_capabilities = []

    with patch("splice.netconf.client.manager.connect", return_value=mgr):
        try:
            with client:
                raise ValueError("test error")
        except ValueError:
            pass

    mgr.close_session.assert_called_once()
    assert client._mgr is None


# ---------------------------------------------------------------------------
# runtime._safe_close_manager fallback
# ---------------------------------------------------------------------------

def test_safe_close_manager_uses_force_close_on_failure():
    """_safe_close_manager should fall back to _force_close_session when close_session fails."""
    from splice.runtime import _safe_close_manager

    mgr = MagicMock()
    mgr.close_session.side_effect = Exception("dead session")
    session_obj = MagicMock()
    mgr._session = session_obj

    _safe_close_manager(mgr)

    mgr.close_session.assert_called_once()
    # Force close fallback should have closed the session
    session_obj.close.assert_called_once()


def test_safe_close_manager_success_no_force_close():
    """_safe_close_manager should not force-close when close_session succeeds."""
    from splice.runtime import _safe_close_manager

    mgr = MagicMock()
    session_obj = MagicMock()
    mgr._session = session_obj

    _safe_close_manager(mgr)

    mgr.close_session.assert_called_once()
    session_obj.close.assert_not_called()


def test_safe_close_manager_none_is_noop():
    """_safe_close_manager(None) should not raise."""
    from splice.runtime import _safe_close_manager
    _safe_close_manager(None)


# ---------------------------------------------------------------------------
# safe_connect() circuit breaker integration
# ---------------------------------------------------------------------------

def test_safe_connect_records_failure_on_connect_error():
    """safe_connect() should increment failure count when connect() raises."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(
        client, reconnect_delay_sec=0,
        circuit_breaker_threshold=3, circuit_breaker_cooldown_sec=60.0,
    )

    client.connect = MagicMock(side_effect=Exception("connection refused"))

    for i in range(1, 4):
        try:
            pool.safe_connect()
        except Exception:
            pass
        assert pool._consecutive_failures == i

    # After threshold, circuit breaker should be open
    try:
        pool.safe_connect()
        assert False, "should have raised CircuitBreakerOpen"
    except CircuitBreakerOpen:
        pass
    # client.connect should NOT have been called on the 4th attempt
    assert client.connect.call_count == 3


def test_safe_connect_records_success():
    """safe_connect() should reset failure count on successful connect."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0, circuit_breaker_threshold=5)

    # Simulate 2 failures then a success
    client.connect = MagicMock(side_effect=[Exception("fail"), Exception("fail")])
    for _ in range(2):
        try:
            pool.safe_connect()
        except Exception:
            pass
    assert pool._consecutive_failures == 2

    client.connect = MagicMock()  # succeeds
    pool.safe_connect()
    assert pool._consecutive_failures == 0


@pytest.mark.parametrize("message", [
    "RPC error: access denied",
    "session limit exceeded",
    "Too many sessions for this user",
    "permission denied",
    "lock-denied by another session",
])
def test_pool_lockout_signatures_treated_as_auth(message):
    """Device-side access/session-limit errors must NOT be retried and must
    open the long auth cooldown — retrying them risks the very lockout the
    breaker exists to prevent."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(
        client, reconnect_delay_sec=0, auth_failure_cooldown_sec=300.0,
    )
    client.get = MagicMock(side_effect=Exception(message))
    client.close = MagicMock()

    with patch("splice.netconf.client.time.sleep") as mock_sleep:
        try:
            pool.get("<filter/>")
        except Exception:
            pass
        mock_sleep.assert_not_called()       # no retry
        assert client.get.call_count == 1

    status = pool.get_status()
    assert status["circuit_breaker_open"] is True
    assert status["auth_failure_count"] == 1
    assert status["circuit_breaker_seconds_remaining"] > 250  # long cooldown


def test_pool_generic_error_still_retries_once():
    """A genuinely transient error must still get the reconnect-once retry —
    the broadened auth matching must not swallow ordinary failures."""
    client, mgr = _make_client_with_mock_mgr()
    pool = NetconfClientPool(client, reconnect_delay_sec=0)
    client.get = MagicMock(side_effect=Exception("connection reset by peer"))
    client.close = MagicMock()

    with patch("splice.netconf.client.time.sleep") as mock_sleep:
        try:
            pool.get("<filter/>")
        except Exception:
            pass
        mock_sleep.assert_called_once()      # retried
        assert client.get.call_count == 2
