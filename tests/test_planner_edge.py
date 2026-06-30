from splice.planner import plan_module_batched_queries


def test_module_batch_with_jobs_filtering():
    catalog = {
        "/p1:system": {"filter_xml": "<f/>", "module": "ietf-system"},
        "/p1:system/p1:state": {"filter_xml": "<g/>", "module": "ietf-system"},
        "/p2:edfas": {"filter_xml": "<h/>", "module": "lumentum-ote-edfa"},
    }
    jobs = {"/p2:edfas": 5}
    # planner itself doesn't filter by jobs; caller filters catalog before planning
    filtered = {k: v for k, v in catalog.items() if k in jobs}
    tasks = plan_module_batched_queries(filtered, jobs, default_interval_sec=30)
    assert len(tasks) == 1
    assert tasks[0].canonical_path == "/p2:edfas"
    assert tasks[0].interval_sec == 5
