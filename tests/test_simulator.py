"""Tests for the simulator module."""
from __future__ import annotations

import os
import tempfile

import pytest

from splice.simulator import (
    OpenROADMProfile,
    SimulatedNetconfClient,
    available_profiles,
    get_profile,
    make_simulated_client,
)
from splice.simulator.backend import _SimulatedReply
from splice.netconf.poller import parse_xml_to_kv


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------


def test_available_profiles():
    profiles = available_profiles()
    assert "openroadm" in profiles


def test_get_profile_valid():
    profile = get_profile("openroadm")
    assert profile.name == "openroadm"


def test_get_profile_case_insensitive():
    profile = get_profile("OpenROADM")
    assert profile.name == "openroadm"


def test_get_profile_unknown():
    with pytest.raises(ValueError, match="unknown simulator profile"):
        get_profile("nonexistent")


# ---------------------------------------------------------------------------
# OpenROADM profile
# ---------------------------------------------------------------------------


class TestOpenROADMProfile:
    def setup_method(self):
        self.profile = OpenROADMProfile()

    def test_name(self):
        assert self.profile.name == "openroadm"

    def test_capabilities(self):
        caps = self.profile.capabilities
        assert len(caps) > 5
        assert any("base:1.0" in c for c in caps)
        assert any("writable-running" in c for c in caps)
        assert any("openroadm-device" in c for c in caps)

    def test_modules(self):
        modules = self.profile.modules
        names = [m["name"] for m in modules]
        assert "org-openroadm-device" in names
        assert "org-openroadm-common-types" in names
        assert "org-openroadm-pm" in names
        assert all("revision" in m for m in modules)
        assert all("namespace" in m for m in modules)

    def test_get_response_device(self):
        xml = self.profile.get_response("<filter/>")
        assert "ROADM-SIM-01" in xml
        assert "org-openroadm-device" in xml

    def test_get_response_alarm(self):
        xml = self.profile.get_response('<filter><active-alarm-list/></filter>')
        assert "alarm-001" in xml
        assert "opticalPowerDegraded" in xml

    def test_get_response_lldp(self):
        xml = self.profile.get_response('<filter><lldp/></filter>')
        assert "ROADM-SIM-01" in xml

    def test_get_response_netconf_monitoring(self):
        xml = self.profile.get_response('<filter><netconf-state/></filter>')
        assert "org-openroadm-device" in xml
        assert "<schemas>" in xml

    def test_get_config_response(self):
        xml = self.profile.get_config_response("running", "<filter/>")
        assert "ROADM-SIM-01" in xml

    def test_get_schema_response_known(self):
        xml = self.profile.get_schema_response("org-openroadm-device", "2023-05-26")
        assert "org-openroadm-device" in xml
        assert "rpc-reply" in xml

    def test_get_schema_response_unknown(self):
        xml = self.profile.get_schema_response("unknown-module", None)
        assert "rpc-reply" in xml
        assert "unknown-module" in xml

    def test_edit_config_response(self):
        xml = self.profile.edit_config_response("running", "<config/>")
        assert "<ok/>" in xml

    def test_netconf_monitoring_xml(self):
        xml = self.profile.netconf_monitoring_xml()
        assert "<schemas>" in xml
        assert "org-openroadm-device" in xml


# ---------------------------------------------------------------------------
# SimulatedNetconfClient
# ---------------------------------------------------------------------------


class TestSimulatedNetconfClient:
    def setup_method(self):
        self.client = make_simulated_client("openroadm")

    def test_connect_and_close(self):
        self.client.connect()
        mgr = self.client.manager()
        assert mgr.connected
        self.client.close()

    def test_context_manager(self):
        with self.client:
            mgr = self.client.manager()
            assert mgr.connected

    def test_get_returns_reply(self):
        self.client.connect()
        reply = self.client.get("<filter/>")
        assert isinstance(reply, _SimulatedReply)
        assert "ROADM-SIM-01" in reply.data_xml

    def test_get_config_returns_reply(self):
        self.client.connect()
        reply = self.client.get_config("running", "<filter/>")
        assert "ROADM-SIM-01" in reply.data_xml

    def test_edit_config(self):
        self.client.connect()
        reply = self.client.edit_config("running", "<config/>")
        assert "<ok/>" in reply.xml

    def test_commit(self):
        self.client.connect()
        reply = self.client.commit()
        assert "<ok/>" in reply.xml

    def test_discard_changes(self):
        self.client.connect()
        reply = self.client.discard_changes()
        assert "<ok/>" in reply.xml

    def test_lock_unlock(self):
        self.client.connect()
        assert "<ok/>" in self.client.lock("running").xml
        assert "<ok/>" in self.client.unlock("running").xml

    def test_manager_capabilities(self):
        self.client.connect()
        mgr = self.client.manager()
        assert len(mgr.server_capabilities) > 5
        assert any("openroadm" in c for c in mgr.server_capabilities)

    def test_dispatch_get_schema(self):
        from defusedxml import ElementTree as ET
        rpc_xml = (
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            '<identifier>org-openroadm-device</identifier>'
            '<version>2023-05-26</version>'
            '<format>yang</format>'
            '</get-schema>'
        )
        self.client.connect()
        # Test with string-based dispatch via Element
        elem = ET.fromstring(rpc_xml)
        reply = self.client.dispatch(elem)
        assert "org-openroadm-device" in reply.xml

    def test_auto_connect_on_get(self):
        """Client should auto-connect when get() is called."""
        reply = self.client.get("<filter/>")
        assert "ROADM-SIM-01" in reply.data_xml


# ---------------------------------------------------------------------------
# parse_xml_to_kv compatibility
# ---------------------------------------------------------------------------


class TestParseXmlCompatibility:
    """Verify that simulated XML responses are parseable by parse_xml_to_kv."""

    def setup_method(self):
        self.profile = OpenROADMProfile()

    def test_device_xml_parses(self):
        xml = self.profile.get_response("<filter/>")
        updates = parse_xml_to_kv(xml)
        assert len(updates) > 0
        paths = [u["path"] for u in updates]
        # Should find device-level fields
        assert any("node-id" in p for p in paths)
        assert any("vendor" in p for p in paths)

    def test_device_xml_has_realistic_values(self):
        xml = self.profile.get_response("<filter/>")
        updates = parse_xml_to_kv(xml)
        values = {u["path"]: u["value"] for u in updates}
        # Check that optical power values exist and are realistic
        power_paths = [p for p in values if "power" in p.lower()]
        assert len(power_paths) > 0, "expected optical power paths"
        for p in power_paths:
            v = values[p]
            # Power values should be numeric strings
            try:
                float(v)
            except (ValueError, TypeError):
                if v not in ("inService", "outOfService", "degraded"):
                    pytest.fail(f"non-numeric power value at {p}: {v}")

    def test_alarm_xml_parses(self):
        xml = self.profile.get_response('<filter><active-alarm-list/></filter>')
        updates = parse_xml_to_kv(xml)
        assert len(updates) > 0
        paths = [u["path"] for u in updates]
        assert any("severity" in p for p in paths)

    def test_lldp_xml_parses(self):
        xml = self.profile.get_response('<filter><lldp/></filter>')
        updates = parse_xml_to_kv(xml)
        assert len(updates) > 0
        paths = [u["path"] for u in updates]
        assert any("system-name" in p for p in paths)

    def test_netconf_monitoring_parses(self):
        xml = self.profile.netconf_monitoring_xml()
        updates = parse_xml_to_kv(xml)
        assert len(updates) > 0
        paths = [u["path"] for u in updates]
        assert any("identifier" in p for p in paths)


# ---------------------------------------------------------------------------
# Discovery pipeline compatibility
# ---------------------------------------------------------------------------


class TestDiscoveryPipeline:
    """Verify that the simulator works with the discovery pipeline."""

    def test_fetch_supported_modules_via_monitoring(self):
        """discovery_client.fetch_supported_modules works with the sim manager."""
        from splice.netconf.discovery_client import fetch_supported_modules

        client = make_simulated_client("openroadm")
        client.connect()
        mgr = client.manager()
        source, modules = fetch_supported_modules(mgr)
        assert modules, "expected modules from discovery"
        names = [m["name"] for m in modules]
        assert "org-openroadm-device" in names

    def test_fetch_yang_schemas_to_disk(self):
        """Schemas fetched via dispatch() are written to disk as .yang files."""
        from splice.simulator import get_profile
        from splice.netconf.client import NetconfClientPool

        profile = get_profile("openroadm")
        client = SimulatedNetconfClient(profile)
        pool = NetconfClientPool(client=client)

        from splice.api._discovery import fetch_yang_schemas

        modules = profile.modules
        with tempfile.TemporaryDirectory() as tmpdir:
            written = fetch_yang_schemas(pool, modules, tmpdir, throttle_sec=0)
            yang_files = [f for f in os.listdir(tmpdir) if f.endswith(".yang")]
            assert written > 0
            assert len(yang_files) > 0
            # Check that the main module was written
            assert any("org-openroadm-device" in f for f in yang_files)

    def test_build_catalog_from_simulated_schemas(self):
        """Full catalog build from simulated YANG schemas succeeds."""
        from splice.netconf.client import NetconfClientPool
        from splice.api._discovery import fetch_yang_schemas, build_catalog_in_memory

        profile = get_profile("openroadm")
        client = SimulatedNetconfClient(profile)
        pool = NetconfClientPool(client=client)

        with tempfile.TemporaryDirectory() as tmpdir:
            fetch_yang_schemas(pool, profile.modules, tmpdir, throttle_sec=0)
            catalog = build_catalog_in_memory(tmpdir, include_config=True)
            assert len(catalog.catalog_map) > 0
            paths = catalog.canonical_paths()
            # Should find device-related paths
            assert any("node-id" in p for p in paths)


# ---------------------------------------------------------------------------
# Device class end-to-end
# ---------------------------------------------------------------------------


class TestDeviceSimulation:
    """End-to-end tests using Device with simulation mode."""

    def test_device_sim_uri(self):
        """Device('sim://openroadm') activates simulation mode."""
        from splice.api.device import Device

        dev = Device("sim://openroadm")
        assert dev._simulate == "openroadm"

    def test_device_simulate_param(self):
        """Device(simulate='openroadm') activates simulation mode."""
        from splice.api.device import Device

        dev = Device("unused", simulate="openroadm")
        assert dev._simulate == "openroadm"

    def test_device_connect_and_discover(self):
        """Full connect pipeline succeeds against the simulator."""
        from splice.api.device import Device

        dev = Device("sim://openroadm")
        try:
            dev.connect()
            assert dev._connected
            assert len(dev.catalog_paths) > 0
        finally:
            dev.close()

    def test_device_context_manager(self):
        """Device as context manager works with simulation."""
        from splice.api.device import Device

        with Device("sim://openroadm") as dev:
            assert dev._connected
            assert len(dev.catalog_paths) > 0

    def test_device_find_paths(self):
        """find_paths works with simulated catalog."""
        from splice.api.device import Device

        with Device("sim://openroadm") as dev:
            paths = dev.find_paths("*node-id*")
            assert len(paths) > 0

    def test_device_connection_status(self):
        """connection_status is populated in simulation mode."""
        from splice.api.device import Device

        with Device("sim://openroadm") as dev:
            status = dev.connection_status
            assert status["connected"]
