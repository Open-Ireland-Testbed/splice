"""Integration tests: MCP server tools against the simulated NETCONF backend.

These tests exercise the full pipeline — MCP tool function -> Device ->
SimulatedNetconfClient -> OpenROADM profile — with no mocks.  The simulated
backend provides realistic OpenROADM XML responses so we can verify that
catalog paths, telemetry values, and config operations propagate end-to-end.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

from splice.mcp_server import (
    SetPolicy,
    _DeviceSession,
    create_mcp_server,
)


@pytest.fixture
def server():
    """Create an MCP server and clean up all sessions after the test."""
    s = create_mcp_server()
    yield s
    for session in s._gnmi_pool.all_sessions():
        try:
            session.device.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIM_HOST = "sim://openroadm"


def _call_tool(server, name, **kwargs):
    """Call a registered MCP tool function by name."""
    manager = server._tool_manager
    tool = manager.get_tool(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn(**kwargs)


def _connect_sim(server, device_id="sim1"):
    """Connect a simulated OpenROADM device and return the result dict."""
    return _call_tool(
        server,
        "connect_device",
        host=SIM_HOST,
        device_id=device_id,
    )


# ---------------------------------------------------------------------------
# 1. Full lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    """Connect -> list -> telemetry -> find_paths -> capabilities -> health -> disconnect."""

    def test_full_lifecycle(self):
        server = create_mcp_server()

        # connect
        result = _connect_sim(server, device_id="lifecycle1")
        assert result["status"] == "connected", result
        assert result["catalog_paths"] > 0
        device_id = result["device_id"]

        # list_devices
        result = _call_tool(server, "list_devices")
        assert result["count"] == 1
        assert result["devices"][0]["device_id"] == device_id
        assert result["devices"][0]["connected"] is True

        # get_telemetry — read the root device container
        result = _call_tool(
            server, "get_telemetry",
            device_id=device_id,
            path="/org-openroadm-device:org-openroadm-device",
        )
        assert "error" not in result or result.get("error") is not True
        assert len(result["updates"]) > 0

        # find_paths
        result = _call_tool(
            server, "find_paths",
            device_id=device_id,
            pattern="*node-id*",
        )
        assert result["count"] > 0

        # list_capabilities
        result = _call_tool(server, "list_capabilities", device_id=device_id)
        assert result["readable_count"] > 0

        # device_health
        result = _call_tool(server, "device_health", device_id=device_id)
        assert result["connected"] is True

        # disconnect
        result = _call_tool(server, "disconnect_device", device_id=device_id)
        assert result["status"] == "disconnected"

        # verify no devices remain
        result = _call_tool(server, "list_devices")
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# 2. Telemetry returns realistic simulated data
# ---------------------------------------------------------------------------

class TestGetTelemetryIntegration:

    def test_telemetry_has_optical_power_values(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(
            server, "get_telemetry",
            device_id=device_id,
            path="/org-openroadm-device:org-openroadm-device",
        )
        assert "error" not in result or result.get("error") is not True
        updates = result["updates"]
        assert len(updates) > 0

        # Collect paths and values
        values = {u["path"]: u["value"] for u in updates}

        # Should contain optical power readings
        power_paths = [p for p in values if "power" in p.lower()]
        assert len(power_paths) > 0, "expected optical power paths in telemetry"

        # Optical power values should be realistic dBm numbers
        for p in power_paths:
            v = values[p]
            try:
                dbm = float(v)
                # Typical optical power range: -30 dBm to +10 dBm
                assert -40.0 <= dbm <= 20.0, (
                    f"optical power {dbm} dBm at {p} is outside realistic range"
                )
            except (ValueError, TypeError):
                pass  # non-numeric power path (e.g. target-output-power as string)

        # Should also have node-id identifying the simulated ROADM
        node_id_paths = [p for p in values if "node-id" in p]
        assert len(node_id_paths) > 0
        assert any(values[p] == "ROADM-SIM-01" for p in node_id_paths)

        _call_tool(server, "disconnect_device", device_id=device_id)


# ---------------------------------------------------------------------------
# 3. Find paths returns real OpenROADM catalog paths
# ---------------------------------------------------------------------------

class TestFindPathsIntegration:

    def test_find_paths_returns_openroadm_paths(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(
            server, "find_paths",
            device_id=device_id,
            pattern="*openroadm*",
        )
        assert result["count"] > 0
        # All matches should contain openroadm (case-insensitive via the YANG namespace)
        for path in result["matches"]:
            assert "openroadm" in path.lower(), f"unexpected path: {path}"

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_find_paths_node_id(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(
            server, "find_paths",
            device_id=device_id,
            pattern="*node-id*",
        )
        assert result["count"] >= 1
        assert any("node-id" in p for p in result["matches"])

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_find_paths_no_match(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(
            server, "find_paths",
            device_id=device_id,
            pattern="*completely-nonexistent-path*",
        )
        assert result["count"] == 0
        assert result["matches"] == []

        _call_tool(server, "disconnect_device", device_id=device_id)


# ---------------------------------------------------------------------------
# 4. List capabilities shows readable and writable paths
# ---------------------------------------------------------------------------

class TestListCapabilitiesIntegration:

    def test_has_readable_and_writable(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(server, "list_capabilities", device_id=device_id)
        assert result["readable_count"] > 0
        assert result["writable_count"] > 0
        # Writable should be a subset of readable
        assert result["writable_count"] <= result["readable_count"]

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_writable_includes_config_paths(self):
        """Config-true leaves like node-id and administrative-state should be writable."""
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(server, "list_capabilities", device_id=device_id)
        writable_paths = [e["path"] for e in result["writable"]]
        # The OpenROADM profile has config=true on node-id, ipAddress,
        # administrative-state, target-output-power, description
        assert any("node-id" in p for p in writable_paths), (
            f"node-id not in writable paths: {writable_paths}"
        )

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_capabilities_with_pattern_filter(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        all_result = _call_tool(server, "list_capabilities", device_id=device_id)
        filtered = _call_tool(
            server, "list_capabilities",
            device_id=device_id,
            pattern="*alarm*",
        )
        # Filtered count should be less than or equal to unfiltered
        assert filtered["readable_count"] <= all_result["readable_count"]

        _call_tool(server, "disconnect_device", device_id=device_id)


# ---------------------------------------------------------------------------
# 5. Set config with explicit allow policy
# ---------------------------------------------------------------------------

class TestSetConfigAllowedIntegration:

    def test_set_config_allowed_reaches_simulator(self):
        """With an allow-all policy, set_config should reach the simulator and succeed."""
        policy = SetPolicy(allowed_paths=["*"])
        server = create_mcp_server(set_policy=policy)
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        # Find a writable path from the catalog
        caps = _call_tool(server, "list_capabilities", device_id=device_id)
        writable = caps["writable"]
        assert len(writable) > 0, "need at least one writable path"

        target_path = writable[0]["path"]
        result = _call_tool(
            server, "set_config",
            device_id=device_id,
            path=target_path,
            value="test-value",
        )
        # The simulator returns <ok/>, so set should succeed
        assert "error" not in result or result.get("error") is not True
        assert result["path"] == target_path

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_set_config_dry_run_with_sim(self):
        """Dry-run should pass policy but not call the device."""
        policy = SetPolicy(allowed_paths=["*"])
        server = create_mcp_server(set_policy=policy)
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        caps = _call_tool(server, "list_capabilities", device_id=device_id)
        target_path = caps["writable"][0]["path"]

        result = _call_tool(
            server, "set_config",
            device_id=device_id,
            path=target_path,
            value="dry-value",
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["policy_check"] == "passed"

        _call_tool(server, "disconnect_device", device_id=device_id)


# ---------------------------------------------------------------------------
# 6. Set config with deny-all default policy
# ---------------------------------------------------------------------------

class TestSetConfigDenyAllIntegration:

    def test_deny_all_blocks_set(self):
        """Default policy (no allowed_paths) should block all set operations."""
        server = create_mcp_server()  # default deny-all
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        # Try to set a path
        result = _call_tool(
            server, "set_config",
            device_id=device_id,
            path="/org-openroadm-device:org-openroadm-device/node-id",
            value="new-name",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"
        assert "deny-all" in result["message"]

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_forbidden_path_blocks_even_when_allowed(self):
        """A path matching forbidden_paths should be blocked even if allowed_paths matches."""
        policy = SetPolicy(
            allowed_paths=["*"],
            forbidden_paths=["*node-id*"],
        )
        server = create_mcp_server(set_policy=policy)
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        # Find the node-id path
        paths = _call_tool(
            server, "find_paths",
            device_id=device_id,
            pattern="*node-id*",
        )
        assert paths["count"] > 0
        node_id_path = paths["matches"][0]

        result = _call_tool(
            server, "set_config",
            device_id=device_id,
            path=node_id_path,
            value="blocked-name",
        )
        assert result["error"] is True
        assert result["error_code"] == "SET_POLICY_DENIED"
        assert "forbidden" in result["message"]

        _call_tool(server, "disconnect_device", device_id=device_id)


# ---------------------------------------------------------------------------
# 7. Health check on simulated device
# ---------------------------------------------------------------------------

class TestDeviceHealthIntegration:

    def test_health_shows_connected(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        result = _call_tool(server, "device_health", device_id=device_id)
        assert result["connected"] is True
        assert result["device_id"] == device_id
        assert "uptime_sec" in result
        assert "idle_sec" in result

        _call_tool(server, "disconnect_device", device_id=device_id)

    def test_health_after_disconnect_fails(self):
        server = create_mcp_server()
        conn = _connect_sim(server)
        device_id = conn["device_id"]

        _call_tool(server, "disconnect_device", device_id=device_id)

        result = _call_tool(server, "device_health", device_id=device_id)
        assert result["error"] is True
        assert result["error_code"] == "DEVICE_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# 8. Multiple simulated devices
# ---------------------------------------------------------------------------

class TestMultipleDevicesIntegration:

    def test_two_simulated_devices(self):
        server = create_mcp_server()

        # Connect two independent simulated devices
        conn1 = _connect_sim(server, device_id="roadm-a")
        conn2 = _connect_sim(server, device_id="roadm-b")
        assert conn1["status"] == "connected"
        assert conn2["status"] == "connected"

        # Both should appear in list_devices
        listing = _call_tool(server, "list_devices")
        assert listing["count"] == 2
        device_ids = {d["device_id"] for d in listing["devices"]}
        assert device_ids == {"roadm-a", "roadm-b"}

        # Each device has its own catalog
        caps_a = _call_tool(server, "list_capabilities", device_id="roadm-a")
        caps_b = _call_tool(server, "list_capabilities", device_id="roadm-b")
        assert caps_a["readable_count"] > 0
        assert caps_b["readable_count"] > 0

        # Telemetry from each device works independently
        tel_a = _call_tool(
            server, "get_telemetry",
            device_id="roadm-a",
            path="/org-openroadm-device:org-openroadm-device",
        )
        tel_b = _call_tool(
            server, "get_telemetry",
            device_id="roadm-b",
            path="/org-openroadm-device:org-openroadm-device",
        )
        assert len(tel_a["updates"]) > 0
        assert len(tel_b["updates"]) > 0

        # Disconnect one, the other should remain
        _call_tool(server, "disconnect_device", device_id="roadm-a")
        listing = _call_tool(server, "list_devices")
        assert listing["count"] == 1
        assert listing["devices"][0]["device_id"] == "roadm-b"

        # Health of disconnected device should fail
        result = _call_tool(server, "device_health", device_id="roadm-a")
        assert result["error"] is True

        # Other device still healthy
        result = _call_tool(server, "device_health", device_id="roadm-b")
        assert result["connected"] is True

        _call_tool(server, "disconnect_device", device_id="roadm-b")

    def test_reconnect_same_id(self):
        """Reconnecting with the same device_id returns already_connected."""
        server = create_mcp_server()
        conn1 = _connect_sim(server, device_id="same-id")
        assert conn1["status"] == "connected"

        conn2 = _connect_sim(server, device_id="same-id")
        assert conn2["status"] == "already_connected"

        _call_tool(server, "disconnect_device", device_id="same-id")


# ---------------------------------------------------------------------------
# 9. Rate limiting integration
# ---------------------------------------------------------------------------

class TestRateLimitingIntegration:
    """Verify rate limiting works end-to-end with simulated devices."""

    def test_rate_limit_blocks_after_max(self):
        server = create_mcp_server(max_rpcs_per_minute=3)
        conn = _connect_sim(server, device_id="rate-test")
        assert conn["status"] == "connected"

        # First 3 calls should succeed
        for _ in range(3):
            r = _call_tool(server, "get_telemetry", device_id="rate-test",
                           path="/org-openroadm-device:org-openroadm-device")
            assert r.get("error") is not True

        # 4th call should be rate limited
        r = _call_tool(server, "get_telemetry", device_id="rate-test",
                       path="/org-openroadm-device:org-openroadm-device")
        assert r["error"] is True
        assert r["error_code"] == "RATE_LIMITED"
        assert r["retryable"] is True
        assert r["retry_after_sec"] > 0

        _call_tool(server, "disconnect_device", device_id="rate-test")

    def test_rate_limit_per_device(self):
        server = create_mcp_server(max_rpcs_per_minute=2)
        _connect_sim(server, device_id="dev-a")
        _connect_sim(server, device_id="dev-b")

        # Exhaust dev-a's limit
        for _ in range(2):
            _call_tool(server, "get_telemetry", device_id="dev-a",
                       path="/org-openroadm-device:org-openroadm-device")

        # dev-a is limited
        r = _call_tool(server, "get_telemetry", device_id="dev-a",
                       path="/org-openroadm-device:org-openroadm-device")
        assert r["error_code"] == "RATE_LIMITED"

        # dev-b should still work
        r = _call_tool(server, "get_telemetry", device_id="dev-b",
                       path="/org-openroadm-device:org-openroadm-device")
        assert r.get("error") is not True

        _call_tool(server, "disconnect_device", device_id="dev-a")
        _call_tool(server, "disconnect_device", device_id="dev-b")


# ---------------------------------------------------------------------------
# 10. Audit logging integration
# ---------------------------------------------------------------------------

class TestAuditLoggingIntegration:
    """Verify audit log captures tool calls with simulated devices."""

    def test_audit_log_records_calls(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=log_file)
        _connect_sim(server, device_id="audit-test")

        _call_tool(server, "get_telemetry", device_id="audit-test",
                   path="/org-openroadm-device:org-openroadm-device")
        _call_tool(server, "disconnect_device", device_id="audit-test")

        with open(log_file) as f:
            lines = f.readlines()

        assert len(lines) >= 3  # connect + get + disconnect
        entries = [json.loads(line) for line in lines]

        # Verify structure
        for entry in entries:
            assert "timestamp" in entry
            assert "tool" in entry
            assert "status" in entry
            assert "duration_ms" in entry

        # Verify tool names captured
        tools = [e["tool"] for e in entries]
        assert "connect_device" in tools
        assert "get_telemetry" in tools
        assert "disconnect_device" in tools

    def test_audit_redacts_password(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=log_file)

        _call_tool(server, "connect_device", host=SIM_HOST,
                   device_id="pw-test", password="super-secret")

        with open(log_file) as f:
            entries = [json.loads(line) for line in f.readlines()]

        connect_entry = next(e for e in entries if e["tool"] == "connect_device")
        assert connect_entry["params"]["password"] == "***REDACTED***"
        assert "super-secret" not in json.dumps(connect_entry)

        _call_tool(server, "disconnect_device", device_id="pw-test")

    def test_audit_logs_errors(self, tmp_path):
        log_file = str(tmp_path / "audit.jsonl")
        server = create_mcp_server(audit_log_path=log_file)

        # Call with non-existent device
        _call_tool(server, "get_telemetry", device_id="no-such",
                   path="/anything")

        with open(log_file) as f:
            entries = [json.loads(line) for line in f.readlines()]

        error_entry = next(e for e in entries if e["status"] == "error")
        assert error_entry["error_code"] == "DEVICE_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# 11. Value constraints integration
# ---------------------------------------------------------------------------

class TestValueConstraintsIntegration:
    """Verify value constraints with glob patterns work end-to-end."""

    def test_value_constraint_blocks_out_of_range(self):
        policy = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"*/target-output-power": {"min": -20.0, "max": 5.0}},
        )
        server = create_mcp_server(set_policy=policy)
        _connect_sim(server, device_id="vc-test")

        # Value too high — should be blocked by policy
        r = _call_tool(server, "set_config", device_id="vc-test",
                       path="/interfaces/interface/optical-power/target-output-power",
                       value="10.0")
        assert r["error"] is True
        assert r["error_code"] == "SET_POLICY_DENIED"
        assert "above maximum" in r["message"]

        _call_tool(server, "disconnect_device", device_id="vc-test")

    def test_value_constraint_allows_in_range(self):
        policy = SetPolicy(
            allowed_paths=["*"],
            value_constraints={"*/target-output-power": {"min": -20.0, "max": 5.0}},
        )
        server = create_mcp_server(set_policy=policy)
        _connect_sim(server, device_id="vc-ok")

        # Value in range — policy check should pass (use dry_run to verify
        # policy alone, since actual set requires exact catalog paths)
        r = _call_tool(server, "set_config", device_id="vc-ok",
                       path="/interfaces/interface/optical-power/target-output-power",
                       value="2.0", dry_run=True)
        assert r.get("error") is not True
        assert r["dry_run"] is True
        assert r["policy_check"] == "passed"

        _call_tool(server, "disconnect_device", device_id="vc-ok")
