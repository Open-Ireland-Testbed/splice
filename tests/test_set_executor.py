import pytest

import splice.gnmi_pb2 as gnmi_pb2
from splice.gnmi_server import _path_from_str
from splice.netconf.set_executor import NetconfSetExecutor, SetPreconditionError


def _catalog_entry(writable: bool = True) -> dict:
    return {"config": writable, "namespace_map": {"urn:example:sys": "p1"}}


class FakePool:
    def __init__(self, capabilities):
        self._caps = list(capabilities)
        self.calls = []
        self.commit_error = None
        self.edit_error = None

    def get_server_capabilities(self):
        self.calls.append(("capabilities",))
        return list(self._caps)

    def lock(self, target):
        self.calls.append(("lock", target))

    def unlock(self, target):
        self.calls.append(("unlock", target))

    def edit_config(self, target, config):
        self.calls.append(("edit_config", target, config))
        if self.edit_error is not None:
            raise self.edit_error
        return "<ok/>"

    def commit(self):
        self.calls.append(("commit",))
        if self.commit_error is not None:
            raise self.commit_error
        return "<ok/>"

    def discard_changes(self):
        self.calls.append(("discard_changes",))
        return "<ok/>"


def test_execute_auto_selects_running_when_writable_running_only():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:writable-running:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    ex = NetconfSetExecutor(pool, catalog, alias_map={}, list_keys={})

    resp = ex.execute(req)
    assert len(resp.response) == 1
    assert resp.response[0].op == gnmi_pb2.UpdateResult.UPDATE
    assert any(c[0] == "edit_config" and c[1] == "running" for c in pool.calls)
    assert not any(c[0] == "commit" for c in pool.calls)


def test_execute_auto_selects_candidate_and_commits():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:candidate:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    ex = NetconfSetExecutor(pool, catalog, alias_map={}, list_keys={})

    ex.execute(req)
    assert any(c[0] == "edit_config" and c[1] == "candidate" for c in pool.calls)
    assert any(c[0] == "commit" for c in pool.calls)


def test_execute_candidate_commit_failure_discards_changes():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:candidate:1.0"])
    pool.commit_error = RuntimeError("commit failed")
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    ex = NetconfSetExecutor(pool, catalog, alias_map={}, list_keys={})

    with pytest.raises(RuntimeError, match="commit failed"):
        ex.execute(req)
    assert any(c[0] == "discard_changes" for c in pool.calls)


def test_execute_with_lock_wraps_transaction():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:writable-running:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    ex = NetconfSetExecutor(pool, catalog, alias_map={}, list_keys={}, use_lock=True)

    ex.execute(req)
    assert ("lock", "running") in pool.calls
    assert ("unlock", "running") in pool.calls


def test_execute_auto_without_supported_datastore_fails():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:base:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    ex = NetconfSetExecutor(pool, catalog, alias_map={}, list_keys={})

    with pytest.raises(SetPreconditionError, match="does not advertise"):
        ex.execute(req)


def test_execute_calls_post_apply_refresh_callback():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:writable-running:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    seen = []

    def on_refresh(mutations):
        seen.extend(mutations)

    ex = NetconfSetExecutor(
        pool, catalog, alias_map={}, list_keys={}, post_apply_refresh=on_refresh
    )
    ex.execute(req)
    assert len(seen) == 1
    assert seen[0].canonical_path == "/p1:system/p1:config/p1:hostname"


def test_execute_ignores_refresh_callback_failure():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    pool = FakePool(["urn:ietf:params:netconf:capability:writable-running:1.0"])
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}

    def bad_refresh(_mutations):
        raise RuntimeError("refresh failed")

    ex = NetconfSetExecutor(
        pool, catalog, alias_map={}, list_keys={}, post_apply_refresh=bad_refresh
    )
    # Set itself should still succeed.
    resp = ex.execute(req)
    assert len(resp.response) == 1
