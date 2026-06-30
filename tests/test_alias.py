from splice.alias import canonical_to_alias, ensure_unique_aliases


def test_canonical_to_alias():
    assert canonical_to_alias("/p1:system/p1:state") == "/system/state"
    assert canonical_to_alias("/if:interfaces/if:interface/if:name") == "/interfaces/interface/name"


def test_unique_aliases_suffix():
    paths = [
        "/p1:system/p1:state",
        "/p2:system/p2:state",
        "/p3:system/p3:state",
    ]
    m = ensure_unique_aliases(paths)
    assert m[paths[0]] == "/system/state"
    assert m[paths[1]] == "/system/state-2"
    assert m[paths[2]] == "/system/state-3"
