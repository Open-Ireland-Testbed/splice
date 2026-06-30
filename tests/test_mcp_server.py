"""Tests for MCP server tools, session management, set policy, and rate limiting.

These tests call tool handler functions directly without starting the full
MCP server or connecting to real devices.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from splice.mcp_server import (
    AuditLogger,
    RateLimiter,
    SessionPool,
    SetPolicy,
    _DeviceSession,
    _classify_exception,
    _make_error,
    create_mcp_server,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_device(connected=True, catalog_paths=None, catalog_map=None):
    """Create a mock Device that behaves like a connected Device."""
    dev = MagicMock()
    dev._connected = connected
    dev.catalog_paths = catalog_paths or ["/a/b", "/a/c", "/x/y"]
    dev.connection_status = {
        "connected": connected,
        "consecutive_failures": 0,
        "circuit_breaker_open": False,
        "circuit_breaker_seconds_remaining": 0.0,
    }
    dev.find_paths.return_value = ["/a/b", "/a/c"]
    dev.get.return_value = [
        {"path": "/a/b", "value": "42", "timestamp_ns": 1000000},
    ]
    dev.set.return_value = {"path": "/a/b", "operation": "merge", "timestamp_ns": 2000000}
    dev._catalog = MagicMock()
    dev._catalog.catalog_map = catalog_map or {
        "/a/b": {"type": "uint32", "config": False},
        "/a/c": {"type": "string", "config": True},
        "/x/y": {"type": "boolean", "config": True},
    }
    dev._catalog.alias_map = {}
    dev.close.return_value = None
    return dev


def _inject_session(server, device_id, device):
    """Inject a mock session into the server's pool."""
    session = _DeviceSession(
        device_id=device_id,
        device=device,
        host="10.0.0.1",
        port=830,
    )
    server._gnmi_pool.add(session)
    return session


def _call_tool(server, name, **kwargs):
    """Call a registered tool function by name."""
    manager = server._tool_manager
    tool = manager.get_tool(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn(**kwargs)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    def test_all_tools_registered(self):
        server = create_mcp_server()
        manager = server._tool_manager
        expected = {
            "connect_device",
            "disconnect_device",
            "get_telemetry",
            "set_config",
            "find_paths",
            "list_devices",
            "device_health",
            "list_capabilities",
            "search_paths",
            "subscribe_once",
            "doctor",
        }
        registered = {t.name for t in manager.list_tools()}
        assert expected == registered

    def test_tools_have_descriptions(self):
        server = create_mcp_server()
        for tool in server._tool_manager.list_tools():
            assert tool.description, f"tool {tool.name!r} has no description"


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class TestSessionManagement:
    def test_connect_and_list(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "list_devices")
        assert result["count"] == 1
        assert result["devices"][0]["device_id"] == "dev1"
        assert result["devices"][0]["host"] == "10.0.0.1"

    def test_disconnect(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "disconnect_device", device_id="dev1")
        assert result["status"] == "disconnected"
        dev.close.assert_called_once()

        # After disconnect, list should be empty
        result = _call_tool(server, "list_devices")
        assert result["count"] == 0

    def test_disconnect_unknown_device(self):
        server = create_mcp_server()
        result = _call_tool(server, "disconnect_device", device_id="nonexistent")
        assert result["error"] is True
        assert result["error_code"] == "DEVICE_NOT_CONNECTED"

    def test_get_telemetry_unknown_device(self):
        server = create_mcp_server()
        result = _call_tool(server, "get_telemetry", device_id="nope", path="/a/b")
        assert result["error"] is True
        assert result["error_code"] == "DEVICE_NOT_CONNECTED"

    def test_connect_device_with_mock(self):
        """Test connect_device tool with a mocked Device class."""
        server = create_mcp_server()
        mock_dev = _make_mock_device()

        with patch("splice.api.device.Device", return_value=mock_dev):
            result = _call_tool(
                server, "connect_device",
                host="10.0.0.1", port=830, username="admin", password="secret",
                device_id="test1",
            )

        assert result["device_id"] == "test1"
        assert result["status"] == "connected"
        mock_dev.connect.assert_called_once()

    def test_connect_already_connected(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "connect_device",
            host="10.0.0.1", device_id="dev1",
        )
        assert result["status"] == "already_connected"

    def test_connect_failure_returns_error(self):
        server = create_mcp_server()

        from splice.api._errors import ConnectionError as BridgeConnectionError

        mock_dev = MagicMock()
        mock_dev.connect.side_effect = BridgeConnectionError("NETCONF connect failed: auth error")

        with patch("splice.api.device.Device", return_value=mock_dev):
            result = _call_tool(
                server, "connect_device",
                host="10.0.0.1", device_id="fail1",
            )

        assert result["error"] is True
        assert result["error_code"] == "AUTH_LOCKOUT"


# ---------------------------------------------------------------------------
# Get telemetry
# ---------------------------------------------------------------------------

class TestGetTelemetry:
    def test_get_success(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")
        assert "updates" in result
        assert result["device_id"] == "dev1"
        dev.get.assert_called_once_with("/a/b")

    def test_get_path_not_found(self):
        server = create_mcp_server()
        from splice.api._errors import PathNotFoundError

        dev = _make_mock_device()
        dev.get.side_effect = PathNotFoundError("no such path")
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "get_telemetry", device_id="dev1", path="/bad")
        assert result["error"] is True
        assert result["error_code"] == "PATH_NOT_FOUND"
        assert result["retryable"] is False


# ---------------------------------------------------------------------------
# Set config
# ---------------------------------------------------------------------------

class TestSetConfig:
    def test_deny_all_by_default(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/c", value="hello",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"
        # Device.set should NOT have been called
        dev.set.assert_not_called()

    def test_allowed_path(self):
        policy = SetPolicy(allowed_paths=["/a/*"])
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/c", value="hello",
        )
        assert "error" not in result or result.get("error") is not True
        dev.set.assert_called_once_with("/a/c", "hello", operation="merge")

    def test_forbidden_overrides_allowed(self):
        policy = SetPolicy(
            allowed_paths=["/a/*"],
            forbidden_paths=["/a/c"],
        )
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/c", value="hello",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"

    def test_value_constraint_min(self):
        policy = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"/a/b": {"min": 10, "max": 100}},
        )
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/b", value="5",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"
        assert "below minimum" in result["message"]

    def test_value_constraint_max(self):
        policy = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"/a/b": {"min": 10, "max": 100}},
        )
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/b", value="200",
        )
        assert result["error"] is True
        assert "above maximum" in result["message"]

    def test_value_constraint_passes(self):
        policy = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"/a/b": {"min": 10, "max": 100}},
        )
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/b", value="50",
        )
        assert "error" not in result or result.get("error") is not True

    def test_dry_run(self):
        policy = SetPolicy(allowed_paths=["*"])
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/c", value="test", dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["policy_check"] == "passed"
        dev.set.assert_not_called()

    def test_set_device_error(self):
        from splice.api._errors import SetError

        policy = SetPolicy(allowed_paths=["*"])
        server = create_mcp_server(set_policy=policy)
        dev = _make_mock_device()
        dev.set.side_effect = SetError("edit-config failed: invalid value")
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1", path="/a/c", value="bad",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_REJECTED"


# ---------------------------------------------------------------------------
# Find paths
# ---------------------------------------------------------------------------

class TestFindPaths:
    def test_find_success(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "find_paths", device_id="dev1", pattern="/a/*")
        assert result["count"] == 2
        assert result["matches"] == ["/a/b", "/a/c"]

    def test_find_unknown_device(self):
        server = create_mcp_server()
        result = _call_tool(server, "find_paths", device_id="nope", pattern="*")
        assert result["error"] is True
        assert result["error_code"] == "DEVICE_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# Device health
# ---------------------------------------------------------------------------

class TestDeviceHealth:
    def test_health_success(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "device_health", device_id="dev1")
        assert result["device_id"] == "dev1"
        assert result["connected"] is True
        assert "circuit_breaker_open" in result

    def test_health_unknown_device(self):
        server = create_mcp_server()
        result = _call_tool(server, "device_health", device_id="nope")
        assert result["error"] is True


# ---------------------------------------------------------------------------
# List capabilities
# ---------------------------------------------------------------------------

class TestListCapabilities:
    def test_list_all(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "list_capabilities", device_id="dev1")
        assert result["readable_count"] == 3
        assert result["writable_count"] == 2  # /a/c and /x/y have config=True

    def test_list_with_pattern(self):
        server = create_mcp_server()
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        result = _call_tool(server, "list_capabilities", device_id="dev1", pattern="/a/*")
        assert result["readable_count"] == 2
        assert result["writable_count"] == 1  # only /a/c


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_limiter_allows_under_limit(self):
        limiter = RateLimiter(max_per_minute=5)
        for _ in range(5):
            assert limiter.check("dev1") is None

    def test_limiter_blocks_over_limit(self):
        limiter = RateLimiter(max_per_minute=3)
        for _ in range(3):
            limiter.check("dev1")
        wait = limiter.check("dev1")
        assert wait is not None
        assert wait > 0

    def test_limiter_per_device(self):
        limiter = RateLimiter(max_per_minute=2)
        limiter.check("dev1")
        limiter.check("dev1")
        # dev1 is at limit
        assert limiter.check("dev1") is not None
        # dev2 should be fine
        assert limiter.check("dev2") is None

    def test_limiter_clear(self):
        limiter = RateLimiter(max_per_minute=1)
        limiter.check("dev1")
        assert limiter.check("dev1") is not None
        limiter.clear("dev1")
        assert limiter.check("dev1") is None

    def test_rate_limit_in_tool(self):
        server = create_mcp_server(max_rpcs_per_minute=2)
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        # First two should succeed
        r1 = _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")
        r2 = _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")
        assert "updates" in r1
        assert "updates" in r2

        # Third should be rate-limited
        r3 = _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")
        assert r3["error"] is True
        assert r3["error_code"] == "RATE_LIMITED"
        assert r3["retryable"] is True
        assert r3["retry_after_sec"] > 0


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class TestErrorTaxonomy:
    def test_error_structure(self):
        err = _make_error("AUTH_LOCKOUT", "locked out")
        assert err["error"] is True
        assert err["error_code"] == "AUTH_LOCKOUT"
        assert err["message"] == "locked out"
        assert err["retryable"] is True
        assert err["retry_after_sec"] == 300.0
        assert isinstance(err["remediation_hint"], str)
        assert len(err["remediation_hint"]) > 0

    def test_all_codes_have_structure(self):
        from splice.mcp_server import _ERROR_MAP

        for code, info in _ERROR_MAP.items():
            err = _make_error(code, f"test {code}")
            assert "error_code" in err
            assert "retryable" in err
            assert "retry_after_sec" in err
            assert "remediation_hint" in err

    def test_classify_connection_error(self):
        from splice.api._errors import ConnectionError as BCE
        assert _classify_exception(BCE("auth failure")) == "AUTH_LOCKOUT"
        assert _classify_exception(BCE("circuit breaker open")) == "CIRCUIT_BREAKER_OPEN"
        assert _classify_exception(BCE("cannot reach host")) == "DEVICE_UNREACHABLE"

    def test_classify_path_not_found(self):
        from splice.api._errors import PathNotFoundError
        assert _classify_exception(PathNotFoundError("nope")) == "PATH_NOT_FOUND"

    def test_classify_set_error(self):
        from splice.api._errors import SetError
        assert _classify_exception(SetError("failed")) == "SET_REJECTED"

    def test_classify_discovery_error(self):
        from splice.api._errors import DiscoveryError
        assert _classify_exception(DiscoveryError("timeout waiting")) == "DISCOVERY_TIMEOUT"
        assert _classify_exception(DiscoveryError("module parse failed")) == "DEVICE_UNREACHABLE"

    def test_unknown_error(self):
        err = _make_error("UNKNOWN_CODE", "something")
        assert err["error"] is True
        assert err["error_code"] == "UNKNOWN_CODE"
        assert err["retryable"] is False


# ---------------------------------------------------------------------------
# Set policy unit tests
# ---------------------------------------------------------------------------

class TestSetPolicy:
    def test_deny_all_default(self):
        p = SetPolicy()
        assert p.check("/any/path") is not None

    def test_allow_match(self):
        p = SetPolicy(allowed_paths=["/interfaces/*/admin-status"])
        assert p.check("/interfaces/eth0/admin-status") is None

    def test_allow_no_match(self):
        p = SetPolicy(allowed_paths=["/interfaces/*/admin-status"])
        result = p.check("/system/hostname")
        assert result is not None
        assert "not in allowed" in result

    def test_forbidden_overrides(self):
        p = SetPolicy(
            allowed_paths=["*"],
            forbidden_paths=["/system/shutdown"],
        )
        assert p.check("/interfaces/eth0") is None
        result = p.check("/system/shutdown")
        assert result is not None
        assert "forbidden" in result

    def test_value_constraints(self):
        p = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"/power": {"min": 0, "max": 100}},
        )
        assert p.check("/power", 50) is None
        assert p.check("/power", -1) is not None
        assert p.check("/power", 101) is not None

    def test_from_dict(self):
        d = {
            "allowed_paths": ["/a/*"],
            "forbidden_paths": ["/a/secret"],
            "value_constraints": {"/a/power": {"min": 0}},
        }
        p = SetPolicy.from_dict(d)
        assert p.allowed_paths == ["/a/*"]
        assert p.forbidden_paths == ["/a/secret"]
        assert "/a/power" in p.value_constraints

    def test_from_yaml(self, tmp_path):
        yaml_content = """
set_policy:
  allowed_paths:
    - "/test/*"
  forbidden_paths:
    - "/test/danger"
"""
        f = tmp_path / "policy.yaml"
        f.write_text(yaml_content)
        p = SetPolicy.from_yaml(str(f))
        assert p.allowed_paths == ["/test/*"]
        assert p.forbidden_paths == ["/test/danger"]


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

class TestSessionPool:
    def test_idle_cleanup(self):
        pool = SessionPool(idle_timeout_min=0)  # 0 minutes = instant timeout
        pool.idle_timeout_sec = 0.0  # override to 0 seconds

        dev = _make_mock_device()
        session = _DeviceSession(
            device_id="dev1", device=dev, host="h", port=830,
            last_used=time.monotonic() - 1.0,  # already idle
        )
        pool.add(session)

        removed = pool.cleanup_idle()
        assert "dev1" in removed
        assert pool.get("dev1") is None
        dev.close.assert_called_once()

    def test_active_not_cleaned(self):
        pool = SessionPool(idle_timeout_min=60)
        dev = _make_mock_device()
        session = _DeviceSession(
            device_id="dev1", device=dev, host="h", port=830,
        )
        pool.add(session)

        removed = pool.cleanup_idle()
        assert removed == []
        assert pool.get("dev1") is not None


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

import json
import os


class TestAuditLogger:
    def test_log_call_writes_jsonl(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        logger = AuditLogger(path=log_file)

        entry = logger.log_call(
            "get_telemetry",
            {"device_id": "dev1", "path": "/a/b"},
            {"device_id": "dev1", "path": "/a/b", "updates": []},
            12.5,
        )
        assert entry["tool"] == "get_telemetry"
        assert entry["status"] == "success"
        assert entry["duration_ms"] == 12.5
        assert entry["device_id"] == "dev1"
        assert "timestamp" in entry

        # Verify file contents
        with open(log_file) as f:
            line = f.readline()
        parsed = json.loads(line)
        assert parsed["tool"] == "get_telemetry"

    def test_passwords_not_logged(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        logger = AuditLogger(path=log_file)

        entry = logger.log_call(
            "connect_device",
            {"host": "10.0.0.1", "password": "super_secret", "username": "admin"},
            {"device_id": "dev1", "status": "connected"},
            100.0,
        )
        assert entry["params"]["password"] == "***REDACTED***"
        assert entry["params"]["host"] == "10.0.0.1"
        assert entry["params"]["username"] == "admin"

        # Also verify file on disk
        with open(log_file) as f:
            parsed = json.loads(f.readline())
        assert parsed["params"]["password"] == "***REDACTED***"
        assert "super_secret" not in json.dumps(parsed)

    def test_error_calls_logged_with_error_code(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        logger = AuditLogger(path=log_file)

        error_result = _make_error("PATH_NOT_FOUND", "no such path")
        entry = logger.log_call(
            "get_telemetry",
            {"device_id": "dev1", "path": "/bad"},
            error_result,
            5.0,
        )
        assert entry["status"] == "error"
        assert entry["error_code"] == "PATH_NOT_FOUND"

    def test_duration_is_recorded(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        logger = AuditLogger(path=log_file)

        entry = logger.log_call(
            "list_devices", {}, {"devices": [], "count": 0}, 3.14159,
        )
        assert entry["duration_ms"] == 3.14

    def test_sanitize_multiple_sensitive_keys(self):
        sanitized = AuditLogger._sanitize_params({
            "password": "s3cret",
            "token": "abc123",
            "secret": "xyz",
            "host": "10.0.0.1",
        })
        assert sanitized["password"] == "***REDACTED***"
        assert sanitized["token"] == "***REDACTED***"
        assert sanitized["secret"] == "***REDACTED***"
        assert sanitized["host"] == "10.0.0.1"

    def test_device_id_from_result(self, tmp_path):
        """When device_id is not in params, pull it from result."""
        log_file = str(tmp_path / "audit.jsonl")
        logger = AuditLogger(path=log_file)

        entry = logger.log_call(
            "connect_device",
            {"host": "10.0.0.1"},
            {"device_id": "auto_gen_id", "status": "connected"},
            50.0,
        )
        assert entry["device_id"] == "auto_gen_id"


class TestAuditIntegration:
    """Verify audit entries are written by actual tool calls."""

    def test_get_telemetry_audit_entry(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=str(log_file))
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")

        with open(log_file) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "get_telemetry"
        assert entry["status"] == "success"
        assert entry["device_id"] == "dev1"
        assert entry["params"]["path"] == "/a/b"
        assert entry["duration_ms"] >= 0

    def test_error_tool_call_audit_entry(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=str(log_file))

        _call_tool(server, "get_telemetry", device_id="nope", path="/a/b")

        with open(log_file) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "get_telemetry"
        assert entry["status"] == "error"
        assert entry["error_code"] == "DEVICE_NOT_CONNECTED"

    def test_connect_device_password_redacted(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=str(log_file))
        mock_dev = _make_mock_device()

        with patch("splice.api.device.Device", return_value=mock_dev):
            _call_tool(
                server, "connect_device",
                host="10.0.0.1", password="my_secret", device_id="d1",
            )

        with open(log_file) as f:
            content = f.read()
        entry = json.loads(content.strip())
        assert entry["params"]["password"] == "***REDACTED***"
        assert "my_secret" not in content

    def test_multiple_calls_append(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=str(log_file))
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        _call_tool(server, "get_telemetry", device_id="dev1", path="/a/b")
        _call_tool(server, "find_paths", device_id="dev1", pattern="/a/*")
        _call_tool(server, "list_devices")

        with open(log_file) as f:
            lines = f.readlines()
        assert len(lines) == 3
        tools = [json.loads(l)["tool"] for l in lines]
        assert tools == ["get_telemetry", "find_paths", "list_devices"]

    def test_set_config_policy_denied_audit(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=str(log_file))
        dev = _make_mock_device()
        _inject_session(server, "dev1", dev)

        _call_tool(server, "set_config", device_id="dev1", path="/a/c", value="x")

        with open(log_file) as f:
            entry = json.loads(f.readline())
        assert entry["tool"] == "set_config"
        assert entry["status"] == "error"
        assert entry["error_code"] == "SET_POLICY_DENIED"


class TestSetConfigAliasBypass:
    """Verify that policy is checked against canonical path after alias resolution."""

    def test_forbidden_canonical_denied_via_alias(self):
        """An alias for a forbidden canonical path must still be denied."""
        # Policy forbids the canonical path
        pol = SetPolicy(
            allowed_paths=["*"],
            forbidden_paths=["/org-openroadm:components/component/config/power"],
        )
        server = create_mcp_server(set_policy=pol)

        # Device whose alias_map maps the alias to the forbidden canonical path
        dev = _make_mock_device(catalog_map={
            "/org-openroadm:components/component/config/power": {
                "type": "decimal64",
                "config": True,
            },
        })
        dev._catalog.alias_map = {
            "/components/component/config/power": "/org-openroadm:components/component/config/power",
        }
        _inject_session(server, "dev1", dev)

        result = _call_tool(
            server, "set_config",
            device_id="dev1",
            path="/components/component/config/power",
            value="-5.0",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"
        dev.set.assert_not_called()


def test_set_policy_check_accepts_path_list_fail_closed():
    from splice.mcp_server import SetPolicy

    pol = SetPolicy(allowed_paths=["/system/*"])
    # every candidate form must be allowed
    assert pol.check(["/system/hostname", "/system/hostname"]) is None
    # one form outside the allowlist -> denied (apply path could differ)
    assert pol.check(["/system/hostname", "/interfaces/eth0/enabled"]) is not None


def test_set_policy_forbidden_matches_any_form():
    from splice.mcp_server import SetPolicy

    pol = SetPolicy(allowed_paths=["*"], forbidden_paths=["*/admin-state"])
    # forbidden if ANY equivalent form matches, even if another form doesn't
    assert pol.check(["/iface[name=x]/admin-state", "/iface/admin-state"]) is not None


def test_set_policy_alias_form_cannot_bypass_allowlist():
    from splice.mcp_server import SetPolicy

    # The apply path and its alias-resolved canonical are BOTH checked: a form
    # outside the allowlist must be denied even if another form matches.
    pol = SetPolicy(allowed_paths=["/aliased/*"])
    assert pol.check(["/aliased/leaf", "/real/forbidden/leaf"]) is not None


def test_set_policy_value_constraint_matches_any_form():
    from splice.mcp_server import SetPolicy

    pol = SetPolicy(
        allowed_paths=["*"],
        value_constraints={"/p1:power/p1:level": {"max": 10}},
    )
    denial = pol.check(["/power/level", "/p1:power/p1:level"], value=99)
    assert denial is not None and "above maximum" in denial
