from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from splice.doctor import check_db_lock_blockers


def _mock_engine_with_fetch(rows):
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = rows
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_ctx
    return mock_engine


def test_check_db_lock_blockers_passes_when_none():
    engine = _mock_engine_with_fetch([])
    result = check_db_lock_blockers(engine)
    assert result.passed is True
    assert "No long-lived idle transactions" in result.message


def test_check_db_lock_blockers_fails_when_rows_present():
    rows = [SimpleNamespace(pid=1234, age_sec=95, usename="postgres")]
    engine = _mock_engine_with_fetch(rows)
    result = check_db_lock_blockers(engine)
    assert result.passed is False
    assert "pid=1234" in result.message
    assert result.fix is not None


def test_check_db_lock_blockers_skips_without_permission():
    mock_conn = MagicMock()
    mock_conn.execute.side_effect = RuntimeError(
        'permission denied for view "pg_stat_activity"'
    )
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    engine = MagicMock()
    engine.connect.return_value = mock_ctx

    result = check_db_lock_blockers(engine)
    assert result.passed is True
    assert "skipped" in result.message.lower()


def _pm_drift_cfg(selector="name~'/ot[0-9]+$'"):
    from types import SimpleNamespace

    return SimpleNamespace(
        device=SimpleNamespace(host="h", port=830, username="u", password="p"),
        discovery=SimpleNamespace(pm_discovery=True, pm_selector=selector),
    )


def _pm_filter_row(list_path, key_values):
    from types import SimpleNamespace

    return SimpleNamespace(
        root_path=list_path or "/p1:root",
        filter_xml="<filter/>",
        list_path=list_path,
        key_values=key_values,
    )


def _patch_pm_drift(monkeypatch, pm_rows, device_instances):
    import splice.db.pm_filter_repository as pmrepo
    import splice.db.catalog_query as catq
    import splice.netconf.list_instance_resolver as resolver
    from unittest.mock import MagicMock
    import ncclient.manager

    monkeypatch.setattr(pmrepo, "fetch_pm_filters", lambda s, d: pm_rows)
    monkeypatch.setattr(
        catq,
        "fetch_list_nodes",
        lambda s, d: [
            {"canonical_path": lp, "list_keys": ["name"], "namespace_map": {}}
            for lp in device_instances
        ],
    )
    monkeypatch.setattr(
        resolver, "discover_list_instances", lambda fetch, list_nodes: device_instances
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(ncclient.manager, "connect", lambda **kw: ctx)


def test_pm_filters_fresh_passes_when_instances_match(monkeypatch):
    from splice.doctor import check_pm_filters_fresh

    lp = "/p1:bulk-data/p2:managed-element/p3:interface"
    rows = [_pm_filter_row(lp, {"name": "1/1/n1/ot500"})]
    _patch_pm_drift(monkeypatch, rows, {lp: [{"name": "1/1/n1/ot500"}]})
    result = check_pm_filters_fresh(_pm_drift_cfg(), None, 1)
    assert result.passed is True


def test_pm_filters_fresh_fails_on_ghost_instances(monkeypatch):
    from splice.doctor import check_pm_filters_fresh

    lp = "/p1:bulk-data/p2:managed-element/p3:interface"
    rows = [_pm_filter_row(lp, {"name": "1/2/n2/ot200"})]  # re-provisioned away
    _patch_pm_drift(monkeypatch, rows, {lp: [{"name": "1/2/n2/ot400"}]})
    result = check_pm_filters_fresh(_pm_drift_cfg(), None, 1)
    assert result.passed is False
    assert "no longer on the device" in result.message
    assert "ot200" in result.message


def test_pm_filters_fresh_fails_on_zero_selector_match(monkeypatch):
    from splice.doctor import check_pm_filters_fresh

    lp = "/p1:bulk-data/p2:managed-element/p3:interface"
    rows = [_pm_filter_row(lp, {"name": "1/1/n1/ot500"})]
    _patch_pm_drift(monkeypatch, rows, {lp: [{"name": "1/1/n1/ot500"}]})
    result = check_pm_filters_fresh(_pm_drift_cfg(selector="name~'/ot999$'"), None, 1)
    assert result.passed is False
    assert "matches 0 of" in result.message


def test_pm_filters_fresh_skips_when_no_filters(monkeypatch):
    from splice.doctor import check_pm_filters_fresh
    import splice.db.pm_filter_repository as pmrepo

    monkeypatch.setattr(pmrepo, "fetch_pm_filters", lambda s, d: [])
    result = check_pm_filters_fresh(_pm_drift_cfg(), None, 1)
    assert result.passed is True
    assert "not run" in result.message
