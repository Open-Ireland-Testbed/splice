"""Tests for run_fetch_yang resilience: skip-existing, throttle, reconnect, auth failure, consecutive failure cap."""

import os
from unittest.mock import MagicMock, patch, call

from ncclient.transport.errors import AuthenticationError

from splice.runtime import run_fetch_yang


def _make_cfg(yang_dir):
    """Return a minimal AppConfig-like object."""
    cfg = MagicMock()
    cfg.device.host = "127.0.0.1"
    cfg.device.port = 830
    cfg.device.username = "u"
    cfg.device.name = "test"
    cfg.yang_dir = str(yang_dir)
    return cfg


def _make_module(name, revision="2024-01-01", fmt="yang"):
    m = MagicMock()
    m.id = 1
    m.name = name
    m.revision = revision
    m.format = fmt
    return m


@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_skip_existing_yang_file(mock_dev, mock_fetch_modules, mock_fetch_schema, tmp_path):
    mock_dev.return_value = MagicMock(id=1)

    mod = _make_module("mod-a", "2024-01-01")
    mock_fetch_modules.return_value = [mod]

    # Place an existing file
    yang_file = tmp_path / "mod-a@2024-01-01.yang"
    yang_file.write_text("module mod-a { }")

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(cfg, MagicMock(), session, throttle_sec=0)
    assert wrote == 0
    mock_fetch_schema.assert_not_called()


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_fetch_throttle(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    mock_dev.return_value = MagicMock(id=1)

    modules = [_make_module(f"mod-{i}", f"2024-0{i+1}-01") for i in range(3)]
    mock_fetch_modules.return_value = modules
    mock_fetch_schema.return_value = "/some/path"

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(cfg, MagicMock(), session, throttle_sec=0.5)
    assert wrote == 3
    # Throttle sleeps after first successful write (between RPCs 2 and 3)
    throttle_calls = [c for c in mock_sleep.call_args_list if c == call(0.5)]
    assert len(throttle_calls) == 2  # between fetch 1→2 and 2→3


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_reconnect_on_failure(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    mock_dev.return_value = MagicMock(id=1)

    mod = _make_module("mod-a")
    mock_fetch_modules.return_value = [mod]

    # First call fails, second (after reconnect) succeeds
    mock_fetch_schema.side_effect = [Exception("session dead"), "/path/to/mod-a.yang"]

    new_mgr = MagicMock()
    reconnect_fn = MagicMock(return_value=new_mgr)

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(cfg, MagicMock(), session, throttle_sec=0, reconnect_fn=reconnect_fn)
    assert wrote == 1
    reconnect_fn.assert_called_once()


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_continue_after_reconnect_failure(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    mock_dev.return_value = MagicMock(id=1)

    modules = [_make_module("mod-a"), _make_module("mod-b", "2024-02-01")]
    mock_fetch_modules.return_value = modules

    # mod-a: both attempts fail; mod-b: succeeds
    mock_fetch_schema.side_effect = [
        Exception("fail1"),   # mod-a first attempt
        Exception("fail2"),   # mod-a reconnect attempt
        "/path/mod-b.yang",   # mod-b succeeds
    ]

    reconnect_fn = MagicMock(return_value=MagicMock())

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(cfg, MagicMock(), session, throttle_sec=0, reconnect_fn=reconnect_fn)
    # mod-a skipped after both failures, mod-b succeeded
    assert wrote == 1


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_fail_fast_on_auth_failure(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    """Authentication failures should fail fast to prevent device lockout."""
    mock_dev.return_value = MagicMock(id=1)

    mod = _make_module("mod-a")
    mock_fetch_modules.return_value = [mod]

    # First attempt fails with auth error
    mock_fetch_schema.side_effect = AuthenticationError("Authentication failed")

    reconnect_fn = MagicMock()

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    # Should raise AuthenticationError immediately
    try:
        run_fetch_yang(cfg, MagicMock(), session, throttle_sec=0, reconnect_fn=reconnect_fn)
        assert False, "should have raised AuthenticationError"
    except AuthenticationError:
        pass

    # Reconnect should NOT be attempted after auth failure
    reconnect_fn.assert_not_called()


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_consecutive_failure_cap(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    """Should stop after max_consecutive_failures to avoid reconnect storm."""
    mock_dev.return_value = MagicMock(id=1)

    # Create many modules that will all fail
    modules = [_make_module(f"mod-{i}") for i in range(20)]
    mock_fetch_modules.return_value = modules
    mock_fetch_schema.side_effect = Exception("network error")

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(
        cfg, MagicMock(), session, throttle_sec=0,
        reconnect_fn=None, max_consecutive_failures=5,
    )

    # Should stop after 5 consecutive failures (not process all 20)
    assert wrote == 0
    # Only 5 attempts before hitting the cap
    assert mock_fetch_schema.call_count == 5


@patch("splice.runtime.time.sleep")
@patch("splice.runtime.fetch_and_store_schema")
@patch("splice.runtime.fetch_modules")
@patch("splice.runtime.get_or_create_device")
def test_reset_consecutive_failures_on_success(mock_dev, mock_fetch_modules, mock_fetch_schema, mock_sleep, tmp_path):
    """Consecutive failure counter should reset on success."""
    mock_dev.return_value = MagicMock(id=1)

    modules = [_make_module(f"mod-{i}") for i in range(5)]
    mock_fetch_modules.return_value = modules

    # First 2 fail, next 3 succeed
    mock_fetch_schema.side_effect = [
        Exception("fail1"),
        Exception("fail2"),
        "/path/mod-2.yang",
        "/path/mod-3.yang",
        "/path/mod-4.yang",
    ]

    cfg = _make_cfg(tmp_path)
    session = MagicMock()

    wrote = run_fetch_yang(
        cfg, MagicMock(), session, throttle_sec=0,
        reconnect_fn=None, max_consecutive_failures=3,
    )

    # All 5 should be processed because failures were reset by successes
    assert wrote == 3
    assert mock_fetch_schema.call_count == 5
