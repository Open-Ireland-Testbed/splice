from splice.pathing import normalize_path, apply_list_keys, is_path_prefix


def test_is_path_prefix_exact_match():
    assert is_path_prefix("/system", "/system") is True


def test_is_path_prefix_descendant():
    assert is_path_prefix("/system/state", "/system") is True


def test_is_path_prefix_no_false_positive():
    """'/system' must NOT match '/system2'."""
    assert is_path_prefix("/system2", "/system") is False
    assert is_path_prefix("/system2/foo", "/system") is False


def test_is_path_prefix_root():
    assert is_path_prefix("/anything", "/") is True
    assert is_path_prefix("/", "/") is True


def test_normalize_path():
    assert normalize_path("/p1:system/p1:state") == "/system/state"


def test_apply_list_keys():
    list_keys = {"/interfaces/interface": ["name"]}
    values = {"/interfaces/interface/name": "eth0"}
    out = apply_list_keys("/interfaces/interface/name", values, list_keys)
    assert out.startswith("/interfaces/interface[name=eth0]")


def test_is_path_prefix_boundary_aware():
    from splice.pathing import is_path_prefix

    # exact + child element
    assert is_path_prefix("/system", "/system")
    assert is_path_prefix("/system/host", "/system")
    # no false positive on a longer sibling name
    assert not is_path_prefix("/system-backup", "/system")
    # predicate on the prefix's last element counts as a descendant
    assert is_path_prefix("/pm/otnpm-data[interface=x]/L/m", "/pm/otnpm-data")
    assert is_path_prefix("/pm/otnpm-data[a=1,b=2]/L/m", "/pm")
    # sibling domain must not match
    assert not is_path_prefix("/pm/eqpm-data/S/t", "/pm/otnpm-data")
    # root matches everything
    assert is_path_prefix("/anything/here", "/")


def test_is_path_prefix_namespace_qualified_edges():
    from splice.pathing import is_path_prefix

    # qualified sibling names must not false-positive
    assert not is_path_prefix("/p1:system-backup/state", "/p1:system")
    assert is_path_prefix("/p1:system/state", "/p1:system")
    # comma-joined multi-key predicate counts as a descendant boundary
    assert is_path_prefix("/pm/d[a=1,b=2]/leaf", "/pm/d")
    # a prefix that itself carries keys only matches the same-keyed subtree
    assert is_path_prefix("/pm/d[a=1]/leaf", "/pm/d[a=1]")
    assert not is_path_prefix("/pm/d[a=2]/leaf", "/pm/d[a=1]")
