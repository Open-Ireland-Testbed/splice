"""Tests for simulator write path, concurrent connections, and dispatch.

Exercises the SimulatedNetconfClient's edit-config, commit, lock/unlock,
concurrent access, and stateless behavior (set does not affect get).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from splice.simulator import (
    OpenROADMProfile,
    SimulatedNetconfClient,
    make_simulated_client,
)
from splice.simulator.backend import _SimulatedManager, _SimulatedReply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OK_FRAGMENT = "<ok/>"
_NS_NC = "urn:ietf:params:xml:ns:netconf:base:1.0"


def _make_client() -> SimulatedNetconfClient:
    return make_simulated_client("openroadm")


# ---------------------------------------------------------------------------
# 1. edit_config returns OK
# ---------------------------------------------------------------------------

class TestEditConfig:

    def test_edit_config_returns_ok(self):
        client = _make_client()
        client.connect()
        reply = client.edit_config("running", "<config><node-id>NEW</node-id></config>")
        assert _OK_FRAGMENT in reply.xml

    def test_edit_config_candidate_returns_ok(self):
        client = _make_client()
        client.connect()
        reply = client.edit_config("candidate", "<config><node-id>X</node-id></config>")
        assert _OK_FRAGMENT in reply.xml

    def test_edit_config_auto_connects(self):
        """edit_config should auto-connect if not already connected."""
        client = _make_client()
        reply = client.edit_config("running", "<config/>")
        assert _OK_FRAGMENT in reply.xml

    def test_edit_config_returns_simulated_reply(self):
        client = _make_client()
        reply = client.edit_config("running", "<config/>")
        assert isinstance(reply, _SimulatedReply)
        assert reply.xml == reply.data_xml


# ---------------------------------------------------------------------------
# 2. commit returns OK
# ---------------------------------------------------------------------------

class TestCommit:

    def test_commit_returns_ok(self):
        client = _make_client()
        client.connect()
        reply = client.commit()
        assert _OK_FRAGMENT in reply.xml
        assert _NS_NC in reply.xml

    def test_discard_changes_returns_ok(self):
        client = _make_client()
        client.connect()
        reply = client.discard_changes()
        assert _OK_FRAGMENT in reply.xml


# ---------------------------------------------------------------------------
# 3. lock/unlock lifecycle
# ---------------------------------------------------------------------------

class TestLockUnlock:

    def test_lock_running(self):
        client = _make_client()
        client.connect()
        reply = client.lock("running")
        assert _OK_FRAGMENT in reply.xml

    def test_unlock_running(self):
        client = _make_client()
        client.connect()
        reply = client.unlock("running")
        assert _OK_FRAGMENT in reply.xml

    def test_lock_candidate(self):
        client = _make_client()
        client.connect()
        reply = client.lock("candidate")
        assert _OK_FRAGMENT in reply.xml

    def test_full_lock_edit_commit_unlock_lifecycle(self):
        """Simulate a full NETCONF edit transaction lifecycle."""
        client = _make_client()
        client.connect()

        # Lock
        assert _OK_FRAGMENT in client.lock("candidate").xml

        # Edit
        config = "<config><node-id>UPDATED</node-id></config>"
        assert _OK_FRAGMENT in client.edit_config("candidate", config).xml

        # Commit
        assert _OK_FRAGMENT in client.commit().xml

        # Unlock
        assert _OK_FRAGMENT in client.unlock("candidate").xml

    def test_lock_edit_discard_unlock_lifecycle(self):
        """Lock -> edit -> discard -> unlock should all return OK."""
        client = _make_client()
        client.connect()

        assert _OK_FRAGMENT in client.lock("candidate").xml
        assert _OK_FRAGMENT in client.edit_config("candidate", "<config/>").xml
        assert _OK_FRAGMENT in client.discard_changes().xml
        assert _OK_FRAGMENT in client.unlock("candidate").xml


# ---------------------------------------------------------------------------
# 4. Concurrent connections
# ---------------------------------------------------------------------------

class TestConcurrentConnections:

    def test_multiple_clients_same_profile(self):
        """Multiple SimulatedNetconfClient instances to the same backend
        profile should work independently."""
        profile = OpenROADMProfile()
        clients = [SimulatedNetconfClient(profile) for _ in range(5)]

        for c in clients:
            c.connect()

        # All should be able to get data
        for c in clients:
            reply = c.get("<filter/>")
            assert "ROADM-SIM-01" in reply.xml

        for c in clients:
            c.close()

    def test_concurrent_get_operations(self):
        """Multiple threads doing get() on the same client should not crash."""
        client = _make_client()
        client.connect()
        results = []
        lock = threading.Lock()

        def _do_get():
            reply = client.get("<filter/>")
            with lock:
                results.append("ROADM-SIM-01" in reply.xml)

        threads = [threading.Thread(target=_do_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)
        assert len(results) == 10

    def test_concurrent_edit_config(self):
        """Multiple threads doing edit_config concurrently should all succeed."""
        client = _make_client()
        client.connect()

        def _do_edit(i):
            reply = client.edit_config("running", f"<config><val>{i}</val></config>")
            return _OK_FRAGMENT in reply.xml

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_do_edit, i) for i in range(10)]
            results = [f.result() for f in as_completed(futures)]

        assert all(results)

    def test_concurrent_mixed_operations(self):
        """Mix of get, edit_config, commit, lock, unlock from multiple threads."""
        client = _make_client()
        client.connect()
        errors = []

        def _mixed_ops(op_type):
            try:
                if op_type == "get":
                    r = client.get("<filter/>")
                    assert "rpc-reply" in r.xml
                elif op_type == "edit":
                    r = client.edit_config("running", "<config/>")
                    assert _OK_FRAGMENT in r.xml
                elif op_type == "commit":
                    r = client.commit()
                    assert _OK_FRAGMENT in r.xml
                elif op_type == "lock":
                    r = client.lock("running")
                    assert _OK_FRAGMENT in r.xml
                elif op_type == "unlock":
                    r = client.unlock("running")
                    assert _OK_FRAGMENT in r.xml
            except Exception as e:
                errors.append(str(e))

        ops = ["get", "edit", "commit", "lock", "unlock"] * 4
        threads = [threading.Thread(target=_mixed_ops, args=(op,)) for op in ops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"concurrent ops had errors: {errors}"


# ---------------------------------------------------------------------------
# 5. Stateless behavior — set does not affect get
# ---------------------------------------------------------------------------

class TestStatelessBehavior:

    def test_get_after_edit_returns_same_data(self):
        """The simulator is stateless: edit_config does not change get results."""
        client = _make_client()
        client.connect()

        # Baseline get
        before = client.get("<filter/>").xml

        # Perform an edit
        client.edit_config("running", "<config><node-id>CHANGED</node-id></config>")
        client.commit()

        # Get again — should be identical (stateless)
        after = client.get("<filter/>").xml
        assert before == after

    def test_get_still_returns_original_node_id(self):
        """After setting a new node-id, get should still return ROADM-SIM-01."""
        client = _make_client()
        client.connect()

        client.edit_config("running", "<config><node-id>NEW-NAME</node-id></config>")
        client.commit()

        reply = client.get("<filter/>")
        assert "ROADM-SIM-01" in reply.xml
        # The new name should NOT appear in get output
        assert "NEW-NAME" not in reply.xml

    def test_profile_edit_config_response_ignores_content(self):
        """The profile's edit_config_response always returns OK regardless
        of the config content."""
        profile = OpenROADMProfile()
        r1 = profile.edit_config_response("running", "<config><a>1</a></config>")
        r2 = profile.edit_config_response("candidate", "<config><b>2</b></config>")
        r3 = profile.edit_config_response("running", "")
        assert _OK_FRAGMENT in r1
        assert _OK_FRAGMENT in r2
        assert _OK_FRAGMENT in r3


# ---------------------------------------------------------------------------
# 6. Dispatch for get-schema
# ---------------------------------------------------------------------------

class TestDispatch:

    def test_dispatch_get_schema_known_module(self):
        """get-schema for a known module returns YANG text."""
        client = _make_client()
        client.connect()

        # Build an element for dispatch
        from defusedxml.ElementTree import fromstring
        rpc_xml = (
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            "<identifier>org-openroadm-device</identifier>"
            "<version>2023-05-26</version>"
            "</get-schema>"
        )
        elem = fromstring(rpc_xml)
        reply = client.dispatch(elem)
        assert "org-openroadm-device" in reply.xml
        assert "rpc-reply" in reply.xml

    def test_dispatch_get_schema_unknown_module(self):
        """get-schema for an unknown module returns a stub."""
        client = _make_client()
        client.connect()

        from defusedxml.ElementTree import fromstring
        rpc_xml = (
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            "<identifier>totally-unknown-module</identifier>"
            "</get-schema>"
        )
        elem = fromstring(rpc_xml)
        reply = client.dispatch(elem)
        assert "rpc-reply" in reply.xml
        assert "totally-unknown-module" in reply.xml

    def test_dispatch_string_based(self):
        """dispatch() works with a raw XML string too."""
        client = _make_client()
        client.connect()

        rpc_str = (
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            "<identifier>org-openroadm-pm</identifier>"
            "</get-schema>"
        )
        reply = client.dispatch(rpc_str)
        assert "org-openroadm-pm" in reply.xml

    def test_dispatch_empty_identifier_returns_empty(self):
        """dispatch() with no <identifier> element returns empty data."""
        client = _make_client()
        client.connect()

        from defusedxml.ElementTree import fromstring
        rpc_xml = '<some-rpc xmlns="urn:example"/>'
        elem = fromstring(rpc_xml)
        reply = client.dispatch(elem)
        assert "<data/>" in reply.xml

    def test_dispatch_auto_connects(self):
        """dispatch() should auto-connect if needed."""
        client = _make_client()
        # Not connected yet
        reply = client.dispatch(
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            "<identifier>org-openroadm-device</identifier>"
            "</get-schema>"
        )
        assert "org-openroadm-device" in reply.xml

    def test_manager_dispatch_version_extraction(self):
        """_SimulatedManager.dispatch extracts version from namespaced tags."""
        profile = OpenROADMProfile()
        mgr = _SimulatedManager(profile)

        from defusedxml.ElementTree import fromstring
        rpc_xml = (
            '<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">'
            "<identifier>org-openroadm-alarm</identifier>"
            "<version>2023-05-26</version>"
            "</get-schema>"
        )
        elem = fromstring(rpc_xml)
        reply = mgr.dispatch(elem)
        assert "org-openroadm-alarm" in reply.xml
