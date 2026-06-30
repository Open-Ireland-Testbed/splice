from splice.planner import plan_module_batched_queries


def test_plan_module_batched_queries():
    catalog = {
        "/p1:system": {"filter_xml": "<f/>", "module": "ietf-system"},
        "/p1:system/p1:state": {"filter_xml": "<g/>", "module": "ietf-system"},
        "/p2:edfas": {"filter_xml": "<h/>", "module": "lumentum-ote-edfa"},
    }
    tasks = plan_module_batched_queries(catalog, {}, default_interval_sec=30)
    paths = sorted(t.canonical_path for t in tasks)
    assert "/p1:system" in paths
    assert "/p2:edfas" in paths
