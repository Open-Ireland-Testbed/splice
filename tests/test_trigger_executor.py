from types import SimpleNamespace

import pytest

from splice.netconf.trigger_executor import (
    NetconfTriggerExecutor,
    TriggerValidationError,
)


class _FakePool:
    def __init__(self):
        self.get_calls = []
        self.get_config_calls = []
        self.fail_paths = set()

    def get(self, filter_xml: str):
        self.get_calls.append(filter_xml)
        if "FAIL_STATE" in filter_xml:
            raise RuntimeError("boom")
        return SimpleNamespace(data_xml="state_xml")

    def get_config(self, source: str, filter_xml: str):
        self.get_config_calls.append((source, filter_xml))
        if "FAIL_CONFIG" in filter_xml:
            raise RuntimeError("boom")
        return SimpleNamespace(data_xml="config_xml")


def test_trigger_executor_reads_config_paths_via_get_config():
    pool = _FakePool()
    catalog = {
        "/p1:edfas/p1:edfa/p1:config/p1:target-gain": {
            "config": True,
            "namespace_map": {"urn:example:p1": "p1"},
            "filter_xml": "<unused/>",
        }
    }
    alias = {
        "/edfas/edfa/config/target-gain": "/p1:edfas/p1:edfa/p1:config/p1:target-gain"
    }

    def parse_reply(_xml: str):
        return [
            {"path": "/edfas/edfa/config/target-gain", "value": "20", "timestamp_ns": 1},
            {"path": "/system/hostname", "value": "r1", "timestamp_ns": 1},
        ]

    ex = NetconfTriggerExecutor(
        pool=pool,
        catalog_map=catalog,
        alias_map=alias,
        list_keys={},
        parse_reply=parse_reply,
        max_paths_per_request=50,
    )
    result = ex.execute(["/edfas/edfa/config/target-gain"])
    assert result.errors == []
    assert result.requested_paths == ["/p1:edfas/p1:edfa/p1:config/p1:target-gain"]
    assert len(result.updates) == 1
    assert result.updates[0]["path"] == "/edfas/edfa/config/target-gain"
    assert len(pool.get_config_calls) == 1
    assert pool.get_calls == []


def test_trigger_executor_enforces_max_paths():
    ex = NetconfTriggerExecutor(
        pool=_FakePool(),
        catalog_map={"/x": {"config": False}},
        alias_map={},
        list_keys={},
        parse_reply=lambda _xml: [],
        max_paths_per_request=1,
    )
    with pytest.raises(TriggerValidationError, match="exceeds max paths"):
        ex.execute(["/x", "/y"])


def test_trigger_executor_reports_per_path_errors():
    pool = _FakePool()
    catalog = {
        "/state/ok": {"config": False, "namespace_map": {}, "filter_xml": "<unused/>"},
        "/state/FAIL_STATE": {"config": False, "namespace_map": {}, "filter_xml": "<unused/>"},
    }

    def parse_reply(_xml: str):
        return [{"path": "/state/ok/leaf", "value": 1, "timestamp_ns": 1}]

    ex = NetconfTriggerExecutor(
        pool=pool,
        catalog_map=catalog,
        alias_map={},
        list_keys={},
        parse_reply=parse_reply,
        max_paths_per_request=10,
    )
    result = ex.execute(["/state/ok", "/state/FAIL_STATE"])
    assert len(result.updates) == 1
    assert len(result.errors) == 1
    assert result.errors[0]["path"] == "/state/FAIL_STATE"


def _keyed_two_instance_reply(_xml: str):
    # What parse_xml_to_kv(..., list_keys=...) now emits for a 2-instance list.
    return [
        {"path": "/interfaces/interface[name=eth0]/name", "value": "eth0", "timestamp_ns": 1},
        {"path": "/interfaces/interface[name=eth0]/state/mtu", "value": 1500, "timestamp_ns": 1},
        {"path": "/interfaces/interface[name=eth1]/name", "value": "eth1", "timestamp_ns": 1},
        {"path": "/interfaces/interface[name=eth1]/state/mtu", "value": 9000, "timestamp_ns": 1},
    ]


def test_trigger_executor_predicateless_request_returns_all_instances():
    """A predicate-less request for a keyed list returns every instance,
    distinctly keyed (no collapse)."""
    pool = _FakePool()
    catalog = {"/interfaces/interface": {"config": False, "namespace_map": {}}}
    ex = NetconfTriggerExecutor(
        pool=pool,
        catalog_map=catalog,
        alias_map={},
        list_keys={"/interfaces/interface": ["name"]},
        parse_reply=_keyed_two_instance_reply,
        max_paths_per_request=10,
    )
    result = ex.execute(["/interfaces/interface"])
    paths = {u["path"] for u in result.updates}
    assert "/interfaces/interface[name=eth0]/state/mtu" in paths
    assert "/interfaces/interface[name=eth1]/state/mtu" in paths
    assert len(result.updates) == 4


def test_trigger_executor_instance_pinned_request_returns_only_that_instance():
    """Pinning an instance ([name=eth0]) must return only that instance's leaves."""
    pool = _FakePool()
    catalog = {"/interfaces/interface": {"config": False, "namespace_map": {}}}
    ex = NetconfTriggerExecutor(
        pool=pool,
        catalog_map=catalog,
        alias_map={},
        list_keys={"/interfaces/interface": ["name"]},
        parse_reply=_keyed_two_instance_reply,
        max_paths_per_request=10,
    )
    result = ex.execute(["/interfaces/interface[name=eth0]"])
    paths = {u["path"] for u in result.updates}
    assert paths == {
        "/interfaces/interface[name=eth0]/name",
        "/interfaces/interface[name=eth0]/state/mtu",
    }
    assert all("[name=eth1]" not in p for p in paths)
