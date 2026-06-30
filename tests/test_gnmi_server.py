import json
import queue
import threading
import time

import grpc
import pytest

from splice.cache_store import MemoryCache
from splice.gnmi_server import CacheView, _path_from_str
import splice.gnmi_pb2 as gnmi_pb2
import splice.gnmi_server as server
from splice.github.com.openconfig.gnmi.proto.gnmi_ext import gnmi_ext_pb2
from splice.netconf.set_executor import SetPreconditionError
from splice.netconf.set_request import SetUnsupportedError, SetValidationError


def test_cache_view_alias_match():
    canonical = {
        "/p1:system/state": {"path": "/p1:system/state", "value": 1},
        "/p1:system/config": {"path": "/p1:system/config", "value": 2},
    }
    alias_map = {"/system": "/p1:system"}
    cv = CacheView(canonical, alias_map)
    res = cv.match_prefix("/system")
    assert len(res) == 2


def test_gnmi_get_uses_cache():
    canonical = {
        "/system/hostname": {"path": "/system/hostname", "value": "r1"},
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(cache_view=cv)

    req = gnmi_pb2.GetRequest(path=[_path_from_str("/system")])
    resp = svc.Get(req, None)
    assert len(resp.notification) == 1
    assert len(resp.notification[0].update) == 1
    assert resp.notification[0].update[0].val.string_val == "r1"


def _trigger_ext(payload: dict) -> object:
    return gnmi_ext_pb2.Extension(
        registered_ext=gnmi_ext_pb2.RegisteredExtension(
            id=server.TRIGGER_EXTENSION_ID,
            msg=str.encode(json.dumps(payload)),
        )
    )


def test_gnmi_get_trigger_calls_handler():
    svc = server.GNMIServicer(
        cache_view=CacheView({}, {}),
        trigger_handler=lambda **kwargs: {
            "updates": [{"path": "/system/hostname", "value": "fresh"}],
            "errors": [],
        },
    )
    req = gnmi_pb2.GetRequest(
        path=[_path_from_str("/system/hostname")],
        extension=[_trigger_ext({"mode": "TRIGGER"})],
    )
    resp = svc.Get(req, _AbortContext())
    assert len(resp.notification[0].update) == 1
    assert resp.notification[0].update[0].val.string_val == "fresh"


def test_gnmi_get_trigger_disabled_returns_unimplemented():
    svc = server.GNMIServicer(cache_view=CacheView({}, {}))
    req = gnmi_pb2.GetRequest(
        path=[_path_from_str("/system/hostname")],
        extension=[_trigger_ext({"mode": "TRIGGER"})],
    )
    with pytest.raises(_AbortError) as ei:
        svc.Get(req, _AbortContext())
    assert ei.value.code == grpc.StatusCode.UNIMPLEMENTED


def test_gnmi_get_trigger_invalid_payload_returns_invalid_argument():
    ext = gnmi_ext_pb2.Extension(
        registered_ext=gnmi_ext_pb2.RegisteredExtension(
            id=server.TRIGGER_EXTENSION_ID,
            msg=b'{"mode":"BAD"}',
        )
    )
    svc = server.GNMIServicer(
        cache_view=CacheView({}, {}),
        trigger_handler=lambda **_kwargs: {"updates": []},
    )
    req = gnmi_pb2.GetRequest(path=[_path_from_str("/system")], extension=[ext])
    with pytest.raises(_AbortError) as ei:
        svc.Get(req, _AbortContext())
    assert ei.value.code == grpc.StatusCode.INVALID_ARGUMENT


def test_gnmi_get_keyed_list_order_normalized():
    canonical = {
        "/interfaces/interface[name=eth0,id=1]/state/mtu": {
            "path": "/interfaces/interface[name=eth0,id=1]/state/mtu",
            "value": 9000,
        },
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(
        cache_view=cv, list_keys={"/interfaces/interface": ["name", "id"]}
    )

    req = gnmi_pb2.GetRequest(
        path=[_path_from_str("/interfaces/interface[id=1,name=eth0]/state")]
    )
    resp = svc.Get(req, None)
    assert len(resp.notification) == 1
    assert len(resp.notification[0].update) == 1
    assert resp.notification[0].update[0].val.int_val == 9000


def test_subscribe_on_change_streams_updates():
    class Context:
        def is_active(self):
            return True

    class TestCache(CacheView):
        def __init__(self, store):
            super().__init__({}, {})
            self._store = store

        def match_prefix(self, prefix: str):
            return self._store.snapshot()

        def get_version(self) -> int:
            return self._store.get_version()

        def wait_for_update(self, last_version: int, timeout: float) -> int:
            return self._store.wait_for_update(last_version, timeout)

    store = MemoryCache(ttl_sec=60)
    cache = TestCache(store)
    svc = server.GNMIServicer(cache_view=cache)

    sub = gnmi_pb2.Subscription(
        path=_path_from_str("/system"),
        mode=gnmi_pb2.SubscriptionMode.ON_CHANGE,
    )
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.STREAM,
        subscription=[sub],
    )
    req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)

    out_q = queue.Queue()

    def consume_updates():
        gen = svc.Subscribe(iter([req]), Context())
        # Skip initial data snapshot and sync_response
        next(gen)  # initial data
        next(gen)  # sync_response
        # Now wait for the ON_CHANGE update
        out_q.put(next(gen))

    t = threading.Thread(target=consume_updates, daemon=True)
    t.start()
    time.sleep(0.1)
    store.ingest([{"path": "/system/hostname", "value": "r2"}])
    resp = out_q.get(timeout=2)
    assert resp.update.update[0].val.string_val == "r2"


def test_subscribe_on_change_filters_multiple_paths():
    class Context:
        def is_active(self):
            return True

    class TestCache(CacheView):
        def __init__(self, store):
            super().__init__({}, {})
            self._store = store

        def match_prefix(self, prefix: str):
            return [u for u in self._store.snapshot() if u["path"].startswith(prefix)]

        def get_version(self) -> int:
            return self._store.get_version()

        def wait_for_update(self, last_version: int, timeout: float) -> int:
            return self._store.wait_for_update(last_version, timeout)

    store = MemoryCache(ttl_sec=60)
    cache = TestCache(store)
    svc = server.GNMIServicer(cache_view=cache)

    sub_a = gnmi_pb2.Subscription(
        path=_path_from_str("/system"),
        mode=gnmi_pb2.SubscriptionMode.ON_CHANGE,
    )
    sub_b = gnmi_pb2.Subscription(
        path=_path_from_str("/interfaces"),
        mode=gnmi_pb2.SubscriptionMode.ON_CHANGE,
    )
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.STREAM,
        subscription=[sub_a, sub_b],
    )
    req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)

    out_q = queue.Queue()

    def consume_updates():
        gen = svc.Subscribe(iter([req]), Context())
        # Skip initial data snapshot and sync_response
        next(gen)  # initial data
        next(gen)  # sync_response
        # Now wait for the ON_CHANGE update
        out_q.put(next(gen))

    t = threading.Thread(target=consume_updates, daemon=True)
    t.start()
    time.sleep(0.1)
    store.ingest(
        [
            {"path": "/system/hostname", "value": "r2"},
            {"path": "/interfaces/interface/state/mtu", "value": 9000},
        ]
    )
    resp = out_q.get(timeout=2)
    assert len(resp.update.update) == 2


def test_cache_view_pm_path_matching():
    """PM data under /pm should be returned when querying a PM root catalog path."""
    from splice.pathing import normalize_path

    canonical = {
        "/pm/otnpm-data/FEC/fec-ber": {"path": "/pm/otnpm-data/FEC/fec-ber", "value": 0.001},
        "/pm/eqpm-data/Card/temperature": {"path": "/pm/eqpm-data/Card/temperature", "value": 36.2},
        "/bulk-data/managed-element/card/name": {"path": "/bulk-data/managed-element/card/name", "value": "card-1"},
    }

    class PMCacheView(CacheView):
        def __init__(self, canonical_map, alias_map, pm_root_paths, pm_path_prefix="/pm"):
            super().__init__(canonical_map, alias_map)
            self._pm_root_paths = pm_root_paths or set()
            self._pm_prefix_norm = normalize_path(pm_path_prefix) if pm_root_paths else ""

        def match_prefix(self, prefix):
            canonical_prefix = self._alias_map.get(prefix, prefix)
            pref_norm = self._normalize(canonical_prefix)
            include_pm = False
            if self._pm_root_paths and self._pm_prefix_norm:
                for pm_root in self._pm_root_paths:
                    if pm_root.startswith(pref_norm) or pref_norm.startswith(pm_root):
                        include_pm = True
                        break
            out = []
            for p, rec in self._canonical_map.items():
                p_norm = self._normalize(p)
                if p_norm.startswith(pref_norm):
                    out.append(rec)
                elif include_pm and p_norm.startswith(self._pm_prefix_norm):
                    out.append(rec)
            return out

    pm_roots = {
        normalize_path("/p1:bulk-data/p2:managed-element/p3:interface/p3:logical-interface/p4:otsia/p4:otsi/p4:pm-monitored-entity/p4:pm-current-data"),
        normalize_path("/p1:bulk-data/p2:managed-element/p3:card/p3:pm-monitored-entity/p3:pm-current-data"),
    }
    cv = PMCacheView(canonical, {}, pm_root_paths=pm_roots, pm_path_prefix="/pm")

    # Query a PM root path ancestor -> should include PM data + regular matches
    res = cv.match_prefix("/bulk-data/managed-element/card")
    paths = {r["path"] for r in res}
    assert "/pm/otnpm-data/FEC/fec-ber" in paths
    assert "/pm/eqpm-data/Card/temperature" in paths
    assert "/bulk-data/managed-element/card/name" in paths

    # Query exact PM root path -> should include PM data
    res = cv.match_prefix("/bulk-data/managed-element/interface/logical-interface/otsia/otsi/pm-monitored-entity/pm-current-data")
    paths = {r["path"] for r in res}
    assert "/pm/otnpm-data/FEC/fec-ber" in paths

    # Query unrelated path -> should NOT include PM data
    res = cv.match_prefix("/system")
    paths = {r["path"] for r in res}
    assert "/pm/otnpm-data/FEC/fec-ber" not in paths
    assert "/pm/eqpm-data/Card/temperature" not in paths

    # Root path -> should return everything
    res = cv.match_prefix("/")
    assert len(res) == 3


class _AbortError(Exception):
    def __init__(self, code, details):
        super().__init__(f"{code} {details}")
        self.code = code
        self.details = details


class _AbortContext:
    def abort(self, code, details):
        raise _AbortError(code, details)


def test_set_unimplemented_when_no_handler():
    svc = server.GNMIServicer(cache_view=CacheView({}, {}))
    with pytest.raises(_AbortError) as ei:
        svc.Set(gnmi_pb2.SetRequest(), _AbortContext())
    assert ei.value.code == grpc.StatusCode.UNIMPLEMENTED


def test_set_maps_validation_error_to_invalid_argument():
    def bad_handler(_request):
        raise SetValidationError("bad request")

    svc = server.GNMIServicer(cache_view=CacheView({}, {}), set_handler=bad_handler)
    with pytest.raises(_AbortError) as ei:
        svc.Set(gnmi_pb2.SetRequest(), _AbortContext())
    assert ei.value.code == grpc.StatusCode.INVALID_ARGUMENT


def test_set_maps_unsupported_error_to_unimplemented():
    def unsupported(_request):
        raise SetUnsupportedError("not yet")

    svc = server.GNMIServicer(cache_view=CacheView({}, {}), set_handler=unsupported)
    with pytest.raises(_AbortError) as ei:
        svc.Set(gnmi_pb2.SetRequest(), _AbortContext())
    assert ei.value.code == grpc.StatusCode.UNIMPLEMENTED


def test_set_maps_precondition_error_to_failed_precondition():
    def precondition(_request):
        raise SetPreconditionError("missing capability")

    svc = server.GNMIServicer(cache_view=CacheView({}, {}), set_handler=precondition)
    with pytest.raises(_AbortError) as ei:
        svc.Set(gnmi_pb2.SetRequest(), _AbortContext())
    assert ei.value.code == grpc.StatusCode.FAILED_PRECONDITION


def test_set_returns_handler_response():
    expected = gnmi_pb2.SetResponse(timestamp=123)

    def ok_handler(_request):
        return expected

    svc = server.GNMIServicer(cache_view=CacheView({}, {}), set_handler=ok_handler)
    resp = svc.Set(gnmi_pb2.SetRequest(), _AbortContext())
    assert resp.timestamp == 123


class _SimpleContext:
    """Minimal context that is always active."""
    def is_active(self):
        return True


def _make_once_subscribe(path_str="/system"):
    sub = gnmi_pb2.Subscription(path=_path_from_str(path_str))
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.ONCE,
        subscription=[sub],
    )
    return gnmi_pb2.SubscribeRequest(subscribe=sub_list)


def test_subscribe_once_sends_sync():
    """ONCE mode must send sync_response=True after data (gNMI spec 3.5.2.3)."""
    canonical = {
        "/system/hostname": {"path": "/system/hostname", "value": "r1"},
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(cache_view=cv)

    req = _make_once_subscribe("/system")
    responses = list(svc.Subscribe(iter([req]), _SimpleContext()))

    assert len(responses) == 2
    # First response: data
    assert responses[0].HasField("update")
    assert len(responses[0].update.update) == 1
    assert responses[0].update.update[0].val.string_val == "r1"
    # Second response: sync_response
    assert responses[1].sync_response is True


def test_subscribe_poll_mode():
    """POLL mode: initial data + sync, then respond to each Poll request."""
    canonical = {
        "/system/hostname": {"path": "/system/hostname", "value": "r1"},
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(cache_view=cv)

    # Build initial subscribe request with POLL mode
    sub = gnmi_pb2.Subscription(path=_path_from_str("/system"))
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.POLL,
        subscription=[sub],
    )
    initial_req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)

    # Build two poll requests
    poll_req1 = gnmi_pb2.SubscribeRequest(poll=gnmi_pb2.Poll())
    poll_req2 = gnmi_pb2.SubscribeRequest(poll=gnmi_pb2.Poll())

    request_stream = iter([initial_req, poll_req1, poll_req2])
    responses = list(svc.Subscribe(request_stream, _SimpleContext()))

    # Expected: initial data, sync, poll1 data, poll2 data = 4 responses
    assert len(responses) == 4
    # Initial data
    assert responses[0].HasField("update")
    assert len(responses[0].update.update) == 1
    # Initial sync
    assert responses[1].sync_response is True
    # Poll 1 data
    assert responses[2].HasField("update")
    assert len(responses[2].update.update) == 1
    # Poll 2 data
    assert responses[3].HasField("update")
    assert len(responses[3].update.update) == 1


def test_subscribe_poll_mode_inactive_context():
    """POLL mode should stop when context becomes inactive."""
    canonical = {
        "/system/hostname": {"path": "/system/hostname", "value": "r1"},
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(cache_view=cv)

    class InactiveContext:
        """Context that is never active — poll loop should exit immediately."""
        def is_active(self):
            return False

    sub = gnmi_pb2.Subscription(path=_path_from_str("/system"))
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.POLL,
        subscription=[sub],
    )
    initial_req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)
    poll_req = gnmi_pb2.SubscribeRequest(poll=gnmi_pb2.Poll())

    request_stream = iter([initial_req, poll_req])
    responses = list(svc.Subscribe(request_stream, InactiveContext()))

    # Initial data + sync are always sent; poll loop exits immediately
    assert len(responses) == 2
    assert responses[0].HasField("update")
    assert responses[1].sync_response is True


def test_subscribe_stream_sends_initial_sync():
    """STREAM mode must send sync_response after initial data snapshot."""
    canonical = {
        "/system/hostname": {"path": "/system/hostname", "value": "r1"},
    }
    cv = CacheView(canonical, {})
    svc = server.GNMIServicer(cache_view=cv, heartbeat_sec=3600)
    # Limit runtime so the stream terminates quickly
    svc.max_runtime_sec = 0.1

    sub = gnmi_pb2.Subscription(
        path=_path_from_str("/system"),
        mode=gnmi_pb2.SubscriptionMode.SAMPLE,
        sample_interval=60_000_000_000,  # 60s — won't fire during test
    )
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.STREAM,
        subscription=[sub],
    )
    req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)

    responses = list(svc.Subscribe(iter([req]), _SimpleContext()))

    # First two responses should be: data notification, then sync_response
    assert len(responses) >= 2
    assert responses[0].HasField("update")
    assert len(responses[0].update.update) == 1
    assert responses[1].sync_response is True


def test_serve_raises_on_bind_failure(monkeypatch):
    import splice.gnmi_server as gs

    class _FakeServer:
        def add_insecure_port(self, addr):
            return 0  # grpc's failure sentinel

        def start(self):
            raise AssertionError("must not start after a failed bind")

        def __getattr__(self, _name):
            # tolerate grpc's servicer-registration calls (add_generic_*,
            # add_registered_method_handlers, ...)
            return lambda *a, **k: None

    import grpc
    monkeypatch.setattr(grpc, "server", lambda *a, **k: _FakeServer())
    view = gs.CacheView({}, {})
    try:
        gs.serve(view, bind="127.0.0.1", port=9, max_runtime_sec=1)
        assert False, "expected OSError on bind failure"
    except OSError as exc:
        assert "failed to bind" in str(exc)


def test_parse_predicates_skips_empty_key():
    from splice.gnmi_server import _parse_predicates

    name, kv = _parse_predicates("iface[=eth0]")
    assert name == "iface"
    assert kv == {}


def test_subscribe_stream_no_immediate_heartbeat():
    """The heartbeat must NOT fire right after sync_response (last_hb was
    anchored to epoch, firing an empty notification on the first loop)."""
    import itertools

    class TimedContext:
        def __init__(self, active_for_sec):
            self._deadline = time.time() + active_for_sec

        def is_active(self):
            return time.time() < self._deadline

    store = MemoryCache(ttl_sec=60)
    store.ingest([{"path": "/system/hostname", "value": "r1"}])

    class TestCache(CacheView):
        def __init__(self):
            super().__init__({}, {})

        def match_prefix(self, prefix):
            return store.snapshot()

        def get_version(self):
            return store.get_version()

        def wait_for_update(self, last_version, timeout):
            return store.wait_for_update(last_version, timeout)

    svc = server.GNMIServicer(cache_view=TestCache(), heartbeat_sec=10)
    sub = gnmi_pb2.Subscription(
        path=_path_from_str("/system"),
        mode=gnmi_pb2.SubscriptionMode.ON_CHANGE,
    )
    sub_list = gnmi_pb2.SubscriptionList(
        mode=gnmi_pb2.SubscriptionList.STREAM, subscription=[sub]
    )
    req = gnmi_pb2.SubscribeRequest(subscribe=sub_list)

    responses = list(svc.Subscribe(iter([req]), TimedContext(0.8)))
    # initial snapshot + sync_response, then NOTHING (no change, heartbeat 10s away)
    assert len(responses) == 2
    assert responses[0].update.update  # data snapshot
    assert responses[1].sync_response is True
