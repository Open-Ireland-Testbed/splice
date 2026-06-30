from splice.planner import plan_queries, plan_batched_queries, group_tasks_by_interval, choose_parent_path


def test_plan_queries_defaults():
    catalog = {
        "/p1:system": {"filter_xml": "<f/>"},
        "/p1:system/p1:state": {"filter_xml": "<g/>"},
    }
    jobs = {"/p1:system/p1:state": 5}
    tasks = plan_queries(catalog, jobs, default_interval_sec=30)
    by_path = {t.canonical_path: t for t in tasks}
    assert by_path["/p1:system"].interval_sec == 30
    assert by_path["/p1:system/p1:state"].interval_sec == 5


def test_group_tasks_by_interval():
    catalog = {"/a": {"filter_xml": "<f/>"}, "/b": {"filter_xml": "<f/>"}}
    tasks = plan_queries(catalog, {"/a": 10, "/b": 10}, default_interval_sec=30)
    grouped = group_tasks_by_interval(tasks)
    assert 10 in grouped and len(grouped[10]) == 2


def test_plan_batched_queries_top_level():
    catalog = {
        "/p1:system": {"filter_xml": "<f/>"},
        "/p1:system/p1:state": {"filter_xml": "<g/>"},
    }
    tasks = plan_batched_queries(catalog, {}, default_interval_sec=30)
    assert len(tasks) == 1
    assert tasks[0].canonical_path == "/p1:system"


def test_choose_parent_path():
    assert choose_parent_path("/a/b/c") == "/a/b"
    assert choose_parent_path("/a") is None


def test_batched_queries_dedupe_siblings_to_min_interval():
    from splice.planner import plan_batched_queries

    catalog = {
        "/system": {"filter_xml": "<s/>"},
        "/system/hostname": {"filter_xml": "<h/>"},
        "/system/clock": {"filter_xml": "<c/>"},
    }
    jobs = {"/system/hostname": 10, "/system/clock": 30}
    tasks = plan_batched_queries(catalog, jobs, default_interval_sec=30)
    sysk = [t for t in tasks if t.canonical_path == "/system"]
    assert len(sysk) == 1                  # no duplicate parent tasks
    assert sysk[0].interval_sec == 10      # polled at the fastest sibling rate


def test_module_batched_queries_dedupe_to_min_interval():
    from splice.planner import plan_module_batched_queries

    catalog = {
        "/system": {"filter_xml": "<s/>", "module": "m"},
        "/system/a": {"filter_xml": "<a/>", "module": "m"},
        "/system/b": {"filter_xml": "<b/>", "module": "m"},
    }
    jobs = {"/system/a": 5, "/system/b": 60}
    tasks = plan_module_batched_queries(catalog, jobs, default_interval_sec=60)
    sysk = [t for t in tasks if t.canonical_path == "/system"]
    assert len(sysk) == 1
    assert sysk[0].interval_sec == 5
