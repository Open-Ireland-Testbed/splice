import logging
from types import SimpleNamespace

import splice.cli as cli


IFACE_LIST = "/p1:bulk-data/p2:managed-element/p3:interface"
ME_LIST = "/p1:bulk-data/p2:managed-element"

NS_MAP = {
    "http://example/bulk": "p1",
    "http://example/me": "p2",
    "http://example/fac": "p3",
}

CATALOG = {
    ME_LIST: {"namespace_map": NS_MAP},
    IFACE_LIST: {"namespace_map": NS_MAP},
    f"{IFACE_LIST}/p3:physical-interface/p3:opt/p3:pm-current-data/p3:montype-monval/p3:mon-type": {
        "namespace_map": NS_MAP
    },
    f"{IFACE_LIST}/p3:logical-interface/p3:odu/p3:pm-current-data/p3:montype-monval/p3:mon-val": {
        "namespace_map": NS_MAP
    },
    "/p1:bulk-data/p2:managed-element/p3:card/p3:pm-current-data/p3:montype-monval/p3:mon-type": {
        "namespace_map": NS_MAP
    },
}

LIST_KEYS = {
    ME_LIST: ["entity-name"],
    IFACE_LIST: ["name"],
    "/p1:bulk-data/p2:managed-element/p3:card": ["name"],
}

LIST_INSTANCES = {
    ME_LIST: [{"entity-name": "1"}],
    IFACE_LIST: [
        {"name": "1/1/n1/ot500"},
        {"name": "1/2/n1/ot400"},
        {"name": "1/cem/m1"},
    ],
    "/p1:bulk-data/p2:managed-element/p3:card": [{"name": "1/1"}, {"name": "1/2"}],
}


def _cfg(selector="name~'/ot[0-9]+$'", selector_list=IFACE_LIST, fanout=True, limit=0):
    return SimpleNamespace(
        discovery=SimpleNamespace(
            pm_max_paths=200,
            pm_selector=selector,
            pm_selector_list_path=selector_list,
            pm_fanout=fanout,
            pm_fanout_limit=limit,
            pm_adapter_modules=[],
        ),
    )


def _patch(monkeypatch):
    monkeypatch.setattr(cli, "fetch_catalog_map", lambda s, d: dict(CATALOG))
    monkeypatch.setattr(cli, "fetch_list_keys", lambda s, d: dict(LIST_KEYS))
    monkeypatch.setattr(cli, "fetch_list_instances", lambda s, d: {
        k: list(v) for k, v in LIST_INSTANCES.items()
    })


def _discover(monkeypatch, cfg):
    _patch(monkeypatch)
    dev = SimpleNamespace(id=1)
    return cli._discover_pm_filters(cfg, None, dev)


def test_roots_under_fanout_list_collapse_to_one_filter_per_instance(monkeypatch):
    filters = _discover(monkeypatch, _cfg())
    fat = [f for f in filters if f["key_values"]]
    # two interface PM roots collapse into one filter per matched instance
    assert len(fat) == 2
    assert sorted(f["key_values"]["name"] for f in fat) == [
        "1/1/n1/ot500",
        "1/2/n1/ot400",
    ]
    for f in fat:
        assert f["root_path"] == IFACE_LIST
        assert f["list_path"] == IFACE_LIST
        # the filter XML is keyed to the instance and the pinned ME ancestor
        assert f["key_values"]["name"] in f["filter_xml"]
        assert "<p2:entity-name>1</p2:entity-name>" in f["filter_xml"]
        # cut at the list element: no deeper PM structure in the filter
        assert "pm-current-data" not in f["filter_xml"]


def test_roots_outside_fanout_list_stay_narrow(monkeypatch):
    filters = _discover(monkeypatch, _cfg())
    narrow = [f for f in filters if not f["key_values"]]
    assert len(narrow) == 1
    (card,) = narrow
    assert card["root_path"].startswith("/p1:bulk-data/p2:managed-element/p3:card")
    # ancestor card list pinned to its first instance
    assert "1/1" in card["filter_xml"]


def test_selector_zero_match_falls_back_to_narrow_and_warns(monkeypatch, caplog):
    cfg = _cfg(selector="name~'/ot999$'")
    with caplog.at_level(logging.WARNING):
        filters = _discover(monkeypatch, cfg)
    assert all(f["key_values"] is None for f in filters)
    # one narrow filter per PM root (no collapse, nothing dropped)
    assert len(filters) == 3
    assert any("matched 0 of 3 instances" in r.message for r in caplog.records)
    # never an unkeyed filter cut at the fanout list element
    assert all(f["root_path"] != IFACE_LIST for f in filters)


def test_no_selector_list_keeps_all_narrow(monkeypatch):
    filters = _discover(monkeypatch, _cfg(selector=None, selector_list=None))
    assert len(filters) == 3
    assert all(f["key_values"] is None for f in filters)


def test_fanout_limit_caps_instances(monkeypatch):
    filters = _discover(monkeypatch, _cfg(selector=None, limit=1))
    fat = [f for f in filters if f["key_values"]]
    assert len(fat) == 1
    assert fat[0]["key_values"]["name"] == "1/1/n1/ot500"


def test_fanout_disabled_takes_first_instance_only(monkeypatch):
    filters = _discover(monkeypatch, _cfg(selector=None, fanout=False))
    fat = [f for f in filters if f["key_values"]]
    assert len(fat) == 1


def test_db_uniqueness_invariant(monkeypatch):
    import json as _json

    filters = _discover(monkeypatch, _cfg())
    keys = [
        (
            f["root_path"],
            f["list_path"],
            _json.dumps(f["key_values"], sort_keys=True) if f["key_values"] else None,
        )
        for f in filters
    ]
    assert len(set(keys)) == len(keys)
