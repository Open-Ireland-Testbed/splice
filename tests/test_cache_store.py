from splice.cache_store import MemoryCache
import time


def test_cache_store_ttl():
    c = MemoryCache(ttl_sec=1)
    c.ingest([{"path": "/a", "value": 1}])
    assert len(c.snapshot()) == 1
    time.sleep(1.1)
    assert len(c.snapshot()) == 0


def test_cache_store_evict_paths_exact_and_descendants():
    c = MemoryCache(ttl_sec=60)
    c.ingest(
        [
            {"path": "/system/config/hostname", "value": "r1"},
            {"path": "/system/config/domain-name", "value": "example.net"},
            {"path": "/interfaces/interface[name=eth0]/config/enabled", "value": True},
            {"path": "/interfaces/interface[name=eth1]/config/enabled", "value": False},
        ]
    )
    v0 = c.get_version()
    removed = c.evict_paths(
        ["/system/config/hostname", "/interfaces/interface[name=eth0]"],
        include_descendants=True,
    )
    assert removed == 2
    assert c.get_version() > v0
    paths = {u["path"] for u in c.snapshot()}
    assert "/system/config/hostname" not in paths
    assert "/interfaces/interface[name=eth0]/config/enabled" not in paths
    assert "/system/config/domain-name" in paths
    assert "/interfaces/interface[name=eth1]/config/enabled" in paths


def test_cache_store_match_prefix_no_false_positive():
    """Prefix '/system' must not match '/system2'."""
    c = MemoryCache(ttl_sec=60)
    c.ingest([
        {"path": "/system/state", "value": "up"},
        {"path": "/system2/state", "value": "down"},
    ])
    matched = c.match_prefix("/system")
    paths = [m["path"] for m in matched]
    assert "/system/state" in paths
    assert "/system2/state" not in paths


def test_cache_store_evict_paths_is_normalized():
    c = MemoryCache(ttl_sec=60)
    c.ingest([{"path": "/system/config/hostname", "value": "r1"}])
    removed = c.evict_paths(["/p1:system/p2:config/p3:hostname"])
    assert removed == 1
    assert c.snapshot() == []
