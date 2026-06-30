import time

from splice.netconf.poller import NetconfPoller, parse_xml_to_kv
from splice.planner import QueryTask


def test_parse_xml_to_kv():
    xml = """
    <rpc-reply>
      <data>
        <system>
          <hostname>r1</hostname>
        </system>
      </data>
    </rpc-reply>
    """
    updates = parse_xml_to_kv(xml, type_map={"/system/hostname": "string"})
    assert any(u["path"] == "/system/hostname" for u in updates)

    xml_ns = """
    <rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
      <data>
        <system xmlns="urn:example:sys">
          <hostname>r1</hostname>
        </system>
      </data>
    </rpc-reply>
    """
    updates = parse_xml_to_kv(xml_ns, type_map={"/system/hostname": "string"})
    rec = next(u for u in updates if u["path"] == "/system/hostname")
    assert rec["meta"]["ns_path"].endswith("/ns1:system/ns1:hostname")
    assert "urn:example:sys" in rec["meta"]["namespace_map"]


def test_poller_runs_and_ingests():
    xml = """
    <rpc-reply><data><system><hostname>r1</hostname></system></data></rpc-reply>
    """
    tasks = [QueryTask(canonical_path="/system", filter_xml="<filter/>", interval_sec=1)]

    calls = {"fetch": 0, "ingest": 0}
    ingested = []

    def fetch(_flt):
        calls["fetch"] += 1
        return xml

    def parse(x):
        return parse_xml_to_kv(x)

    def ingest(updates):
        calls["ingest"] += 1
        ingested.extend(updates)

    p = NetconfPoller(fetch=fetch, parse=parse, ingest=ingest, tasks=tasks)
    p.start()
    time.sleep(1.2)
    p.stop()

    assert calls["fetch"] >= 1
    assert calls["ingest"] >= 1
    assert any(u["path"] == "/system/hostname" for u in ingested)


def test_update_tasks_preserves_future_next_run():
    tasks = [QueryTask(canonical_path="/system", filter_xml="<filter/>", interval_sec=5)]

    def fetch(_flt):
        return "<rpc-reply><data/></rpc-reply>"

    def parse(_xml):
        return []

    def ingest(_updates):
        return None

    p = NetconfPoller(fetch=fetch, parse=parse, ingest=ingest, tasks=tasks)
    with p._lock:
        p._rebuild_groups()
        future = time.time() + 25
        p._next_run[5] = future

    p.update_tasks(tasks)
    assert p._next_run[5] >= future - 0.5


def test_task_failure_limit_disables_task():
    bad = QueryTask(canonical_path="/bad", filter_xml="<f>bad</f>", interval_sec=1)
    good = QueryTask(canonical_path="/good", filter_xml="<f>good</f>", interval_sec=1)
    calls = {"bad": 0, "good": 0}

    def fetch(flt):
        if "bad" in flt:
            calls["bad"] += 1
            raise RuntimeError('Not found node "bad" in path.')
        calls["good"] += 1
        return "<rpc-reply><data><system><hostname>r1</hostname></system></data></rpc-reply>"

    p = NetconfPoller(
        fetch=fetch,
        parse=parse_xml_to_kv,
        ingest=lambda _u: None,
        tasks=[bad, good],
        task_throttle_sec=0,
        task_failure_limit=3,
    )
    p.start()
    deadline = time.time() + 10
    while time.time() < deadline and p.stats()["disabled_count"] < 1:
        time.sleep(0.1)
    # let at least one more sweep run after the disable
    while time.time() < deadline and calls["good"] <= calls["bad"]:
        time.sleep(0.1)
    p.stop()

    stats = p.stats()
    assert stats["disabled_count"] == 1
    assert stats["disabled"][0]["path"] == "/bad"
    assert calls["bad"] == 3  # exactly the limit, then dropped
    assert calls["good"] > calls["bad"]  # good task keeps polling


def test_task_failure_limit_zero_never_disables():
    bad = QueryTask(canonical_path="/bad", filter_xml="<f>bad</f>", interval_sec=1)
    calls = {"bad": 0}

    def fetch(_flt):
        calls["bad"] += 1
        raise RuntimeError("boom")

    p = NetconfPoller(
        fetch=fetch,
        parse=parse_xml_to_kv,
        ingest=lambda _u: None,
        tasks=[bad],
        task_throttle_sec=0,
        task_failure_limit=0,
    )
    p.start()
    deadline = time.time() + 4
    while time.time() < deadline and calls["bad"] < 3:
        time.sleep(0.1)
    p.stop()
    assert calls["bad"] >= 3
    assert p.stats()["disabled_count"] == 0


def test_success_resets_failure_count():
    flaky = QueryTask(canonical_path="/flaky", filter_xml="<f>flaky</f>", interval_sec=1)
    state = {"n": 0}

    def fetch(_flt):
        state["n"] += 1
        if state["n"] % 2 == 1:  # alternate fail/success: never 2 consecutive
            raise RuntimeError("transient")
        return "<rpc-reply><data><system><hostname>r1</hostname></system></data></rpc-reply>"

    p = NetconfPoller(
        fetch=fetch,
        parse=parse_xml_to_kv,
        ingest=lambda _u: None,
        tasks=[flaky],
        task_throttle_sec=0,
        task_failure_limit=2,
    )
    p.start()
    deadline = time.time() + 5
    while time.time() < deadline and state["n"] < 6:
        time.sleep(0.1)
    p.stop()
    assert state["n"] >= 6
    assert p.stats()["disabled_count"] == 0


def test_update_tasks_clears_disabled():
    bad = QueryTask(canonical_path="/bad", filter_xml="<f>bad</f>", interval_sec=1)

    def fetch(_flt):
        raise RuntimeError("boom")

    p = NetconfPoller(
        fetch=fetch,
        parse=parse_xml_to_kv,
        ingest=lambda _u: None,
        tasks=[bad],
        task_throttle_sec=0,
        task_failure_limit=1,
    )
    p.start()
    deadline = time.time() + 5
    while time.time() < deadline and p.stats()["disabled_count"] < 1:
        time.sleep(0.1)
    p.stop()
    assert p.stats()["disabled_count"] == 1

    p.update_tasks([bad])
    assert p.stats()["disabled_count"] == 0


def test_stats_reports_sweeps():
    task = QueryTask(canonical_path="/system", filter_xml="<f/>", interval_sec=1)

    def fetch(_flt):
        return "<rpc-reply><data><system><hostname>r1</hostname></system></data></rpc-reply>"

    p = NetconfPoller(fetch=fetch, parse=parse_xml_to_kv, ingest=lambda _u: None,
                      tasks=[task], task_throttle_sec=0)
    p.start()
    deadline = time.time() + 5
    while time.time() < deadline and not p.stats()["sweeps"]:
        time.sleep(0.1)
    p.stop()
    sweeps = p.stats()["sweeps"]
    assert "1" in sweeps
    assert sweeps["1"]["tasks"] == 1
    assert sweeps["1"]["ok"] >= 1
    assert sweeps["1"]["fail"] == 0


def test_circuit_breaker_sweep_discards_failure_counts():
    from splice.netconf.client import CircuitBreakerOpen

    t1 = QueryTask(canonical_path="/a", filter_xml="<f>a</f>", interval_sec=1)
    t2 = QueryTask(canonical_path="/b", filter_xml="<f>b</f>", interval_sec=1)
    state = {"sweeps": 0}

    def fetch(flt):
        # Every sweep: /a fails normally, then /b trips the breaker.
        if "a" in flt:
            raise RuntimeError("session died")
        state["sweeps"] += 1
        raise CircuitBreakerOpen(0.1, 5)

    p = NetconfPoller(
        fetch=fetch,
        parse=parse_xml_to_kv,
        ingest=lambda _u: None,
        tasks=[t1, t2],
        task_throttle_sec=0,
        task_failure_limit=2,
    )
    p.start()
    deadline = time.time() + 6
    while time.time() < deadline and state["sweeps"] < 4:
        time.sleep(0.1)
    p.stop()

    # /a failed >= limit times overall, but every sweep ended in a breaker
    # pause, so the increments were discarded and /a must NOT be disabled.
    assert state["sweeps"] >= 4
    assert p.stats()["disabled_count"] == 0
