from splice.jobs import apply_rules


def test_apply_rules():
    paths = ["/edfas", "/alarm-list", "/system/state"]
    rules = [
        {"pattern": "edfa", "interval": 5},
        {"pattern": "alarm", "interval": 30},
    ]
    out = apply_rules(paths, rules)
    assert out["/edfas"] == 5
    assert out["/alarm-list"] == 30


def test_curate_jobs_substring_matches_qualified_and_stripped():
    from splice.jobs import curate_jobs

    paths = [
        "/p1:edfas/p1:edfa/p1:output-power",
        "/p1:components/p1:component/p1:temperature",
        "/p1:system/p1:hostname",
    ]
    curated = curate_jobs(paths, ["/edfas", "temperature"], 10)
    assert curated == {
        "/p1:edfas/p1:edfa/p1:output-power": 10,
        "/p1:components/p1:component/p1:temperature": 10,
    }


def test_curate_jobs_regex_and_per_pattern_interval():
    from splice.jobs import curate_jobs

    paths = ["/p1:a/p1:fec-ber", "/p1:a/p1:q-factor", "/p1:a/p1:other"]
    curated = curate_jobs(
        paths,
        [
            {"pattern": r"(fec-ber|q-factor)$", "regex": True, "interval_sec": 5},
            {"pattern": "other"},
        ],
        30,
    )
    assert curated["/p1:a/p1:fec-ber"] == 5
    assert curated["/p1:a/p1:q-factor"] == 5
    assert curated["/p1:a/p1:other"] == 30


def test_curate_jobs_first_pattern_wins():
    from splice.jobs import curate_jobs

    paths = ["/p1:a/p1:fec-ber"]
    curated = curate_jobs(
        paths,
        [{"pattern": "fec", "interval_sec": 5}, {"pattern": "ber", "interval_sec": 60}],
        30,
    )
    assert curated == {"/p1:a/p1:fec-ber": 5}


def test_curate_jobs_empty_include():
    from splice.jobs import curate_jobs

    assert curate_jobs(["/p1:a"], None, 10) == {}
    assert curate_jobs(["/p1:a"], [], 10) == {}


def _curation_cfg(enabled=True, include=None, disable_uncurated=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        metrics=SimpleNamespace(
            enabled=enabled,
            include=include,
            default_interval_sec=10,
            disable_uncurated=disable_uncurated,
        ),
        poller=SimpleNamespace(rules=None, default_interval_sec=30),
    )


def test_init_jobs_curated_upserts_only_matches(monkeypatch):
    import splice.cli as cli

    calls = {}
    monkeypatch.setattr(
        cli, "upsert_jobs", lambda s, d, jobs, **kw: calls.setdefault("jobs", jobs) and len(jobs) or len(jobs)
    )
    monkeypatch.setattr(
        cli, "disable_jobs_except", lambda s, d, keep: calls.setdefault("disabled_keep", set(keep)) and 0 or 7
    )
    cfg = _curation_cfg(include=["fec-ber"], disable_uncurated=True)
    paths = ["/p1:a/p1:fec-ber", "/p1:a/p1:other"]
    summary = cli._init_jobs_curated(cfg, None, 1, paths, 30)
    assert calls["jobs"] == {"/p1:a/p1:fec-ber": 10}
    assert calls["disabled_keep"] == {"/p1:a/p1:fec-ber"}
    assert "1 curated jobs" in summary and "7 uncurated jobs disabled" in summary


def test_init_jobs_curated_zero_match_warns_and_skips_db(monkeypatch):
    import splice.cli as cli

    monkeypatch.setattr(
        cli, "upsert_jobs",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not upsert")),
    )
    cfg = _curation_cfg(include=["nomatch-zzz"], disable_uncurated=True)
    summary = cli._init_jobs_curated(cfg, None, 1, ["/p1:a/p1:b"], 30)
    assert "matched nothing" in summary


def test_init_jobs_curated_disabled_falls_through_to_bulk(monkeypatch):
    import splice.cli as cli

    called = {}
    monkeypatch.setattr(
        cli, "run_init_jobs", lambda s, d, paths, interval: called.setdefault("n", len(paths)) or len(paths)
    )
    cfg = _curation_cfg(enabled=False)
    summary = cli._init_jobs_curated(cfg, None, 1, ["/a", "/b"], 30)
    assert called["n"] == 2
    assert "2 jobs (30s interval)" in summary
