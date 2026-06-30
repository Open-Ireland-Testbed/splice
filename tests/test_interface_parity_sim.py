"""Live cross-interface smoke tests against the simulated OpenROADM device.

The static feature-parity matrix (test_feature_parity.py) only checks that
methods/tools/commands EXIST. These tests drive each of the four interfaces
end to end against `sim://openroadm` and assert that a known telemetry value
actually comes back — the kind of runtime breakage (e.g. path-matching bugs)
that an existence check cannot catch.

No hardware, no PostgreSQL, no external binaries.
"""

from __future__ import annotations

import socket
import time

import pytest

from splice.api.device import Device

# A stable, deterministic value the OpenROADM profile always reports.
ANCHOR_PATH = "/org-openroadm-device/node-id"
ANCHOR_VALUE = "ROADM-SIM-01"
# A synthetic keyed/encoded path — guards the percent-encoded list-key
# matching that broke the TUI telemetry tab.
KEYED_PATH = "/pm/otnpm-data[interface=1%2F2%2Fn1%2Fot400,managed-element=1]/FEC/fec-ber"
KEYED_VALUE = 1.2e-7


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# 1. Python API
# ---------------------------------------------------------------------------

def test_python_api_returns_real_value():
    dev = Device("sim://openroadm")
    dev.connect()
    try:
        assert dev.catalog_paths, "catalog should be populated"
        recs = dev.get("/org-openroadm-device")
        by_path = {r["path"]: r["value"] for r in recs}
        assert by_path.get(ANCHOR_PATH) == ANCHOR_VALUE
        # find_paths parity (API uses glob matching)
        hits = dev.find_paths("*node-id*")
        assert any("node-id" in h for h in hits)
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# 2. MCP server
# ---------------------------------------------------------------------------

def test_mcp_server_returns_real_value():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from splice.mcp_server import create_mcp_server

    server = create_mcp_server()

    def call(name, **kw):
        return server._tool_manager.get_tool(name).fn(**kw)

    res = call("connect_device", host="sim://openroadm", device_id="sim1")
    assert not res.get("error"), res
    try:
        tele = call("get_telemetry", device_id="sim1", path="/org-openroadm-device")
        assert not tele.get("error"), tele
        values = {u["path"]: u["value"] for u in tele["updates"]}
        assert values.get(ANCHOR_PATH) == ANCHOR_VALUE
        caps = call("list_capabilities", device_id="sim1")
        assert not caps.get("error"), caps
    finally:
        call("disconnect_device", device_id="sim1")


# ---------------------------------------------------------------------------
# Shared: a sim-backed gNMI server (the exact path `--simulate ... run` serves)
# ---------------------------------------------------------------------------

@pytest.fixture
def served_sim():
    """Serve a sim device over gNMI on an ephemeral port with a pre-warmed
    cache (as the poller would fill it on a real device)."""
    dev = Device("sim://openroadm")
    dev.connect()
    dev._cache.ingest(dev.get("/org-openroadm-device"))
    dev._cache.ingest([{"path": KEYED_PATH, "value": KEYED_VALUE}])
    port = _free_port()
    dev.serve(port=port, bind="127.0.0.1", blocking=False)
    time.sleep(0.5)
    try:
        yield port
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# 3. CLI / gNMI-server stack (what `splice --simulate openroadm run` serves)
# ---------------------------------------------------------------------------

def test_cli_gnmi_server_serves_real_value(served_sim):
    import grpc

    from splice import gnmi_pb2, gnmi_pb2_grpc
    from splice.gnmi_server import _path_from_str

    stub = gnmi_pb2_grpc.gNMIStub(grpc.insecure_channel(f"127.0.0.1:{served_sim}"))

    # aliased (unprefixed) query must resolve against namespace-stripped cache
    resp = stub.Get(
        gnmi_pb2.GetRequest(path=[_path_from_str(ANCHOR_PATH)]), timeout=5
    )
    vals = [u.val.string_val for nt in resp.notification for u in nt.update]
    assert ANCHOR_VALUE in vals

    # keyed, percent-encoded path must round-trip
    resp = stub.Get(
        gnmi_pb2.GetRequest(path=[_path_from_str(KEYED_PATH)]), timeout=5
    )
    n = sum(len(nt.update) for nt in resp.notification)
    assert n == 1


def test_cli_simulate_run_subprocess_serves():
    """Exercise the actual CLI entrypoint as a subprocess (no config -> 9339
    default is avoided by writing a temp config with an ephemeral port)."""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    import grpc

    from splice import gnmi_pb2, gnmi_pb2_grpc
    from splice.gnmi_server import _path_from_str

    port = _free_port()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "sim.yaml"
        cfg.write_text(
            "device:\n  host: sim\n  name: sim\n"
            "db:\n  url: ''\n"
            f"gnmi:\n  bind: 127.0.0.1\n  port: {port}\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "splice.cli", "--simulate", "openroadm",
             "--config", str(cfg), "run"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            # wait for the port to open
            deadline = time.time() + 30
            ready = False
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.3)
            assert ready, "sim gNMI server did not start"
            time.sleep(0.5)
            stub = gnmi_pb2_grpc.gNMIStub(
                grpc.insecure_channel(f"127.0.0.1:{port}")
            )
            # The sim run has no poller, so use the trigger extension for an
            # on-demand read (what a gNMI client does against the sim).
            from splice.github.com.openconfig.gnmi.proto.gnmi_ext import gnmi_ext_pb2
            import json
            from splice.gnmi_server import TRIGGER_EXTENSION_ID

            ext = gnmi_ext_pb2.Extension(
                registered_ext=gnmi_ext_pb2.RegisteredExtension(
                    id=TRIGGER_EXTENSION_ID, msg=json.dumps({"mode": "TRIGGER"}).encode()
                )
            )
            resp = stub.Get(
                gnmi_pb2.GetRequest(path=[_path_from_str(ANCHOR_PATH)], extension=[ext]),
                timeout=10,
            )
            vals = [u.val.string_val for nt in resp.notification for u in nt.update]
            assert ANCHOR_VALUE in vals
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# 4. TUI (TelemetryService — the path the telemetry-matching bug lived in)
# ---------------------------------------------------------------------------

def test_tui_telemetry_service_returns_real_value(served_sim):
    pytest.importorskip("textual")
    from splice.tui.services.device_registry import DeviceEntry
    from splice.tui.services.telemetry_service import TelemetryService

    entry = DeviceEntry(
        name="sim", config_path="", host="sim", netconf_port=830,
        gnmi_port=served_sim, username="x", db_url="", yang_dir="", health_port=0,
    )
    svc = TelemetryService()
    svc.set_device(entry)
    svc.watch(ANCHOR_PATH)
    svc.watch(KEYED_PATH)
    updates = {u.path: u for u in svc.poll_once()}

    anchor = updates[ANCHOR_PATH]
    assert anchor.source == "gnmi"
    assert str(anchor.value) == ANCHOR_VALUE

    keyed = updates[KEYED_PATH]
    assert keyed.source == "gnmi"
    assert float(keyed.value) == pytest.approx(KEYED_VALUE)
