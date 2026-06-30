import json

import pytest

import splice.gnmi_pb2 as gnmi_pb2
from splice.gnmi_server import _path_from_str
from splice.netconf.set_request import (
    SetUnsupportedError,
    SetValidationError,
    validate_set_request,
)


def _catalog_entry(*, writable: bool = True, nsmap: dict | None = None) -> dict:
    return {
        "config": writable,
        "namespace_map": nsmap or {"urn:example": "p1"},
    }


def test_validate_set_request_preserves_operation_order():
    req = gnmi_pb2.SetRequest(
        delete=[_path_from_str("/p1:system/p1:config/p1:legacy")],
        replace=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ],
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:domain-name"),
                val=gnmi_pb2.TypedValue(string_val="example.net"),
            )
        ],
    )
    catalog = {
        "/p1:system/p1:config/p1:legacy": _catalog_entry(),
        "/p1:system/p1:config/p1:hostname": _catalog_entry(),
        "/p1:system/p1:config/p1:domain-name": _catalog_entry(),
    }

    muts = validate_set_request(req, catalog, alias_map={})
    assert [m.operation for m in muts] == ["delete", "replace", "merge"]
    assert [m.canonical_path for m in muts] == [
        "/p1:system/p1:config/p1:legacy",
        "/p1:system/p1:config/p1:hostname",
        "/p1:system/p1:config/p1:domain-name",
    ]


def test_validate_set_request_prefix_only_path():
    req = gnmi_pb2.SetRequest(
        prefix=_path_from_str("/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:description"),
        delete=[gnmi_pb2.Path()],
    )
    catalog = {
        "/p1:interfaces/p1:interface/p1:config/p1:description": _catalog_entry(),
    }
    list_keys = {"/p1:interfaces/p1:interface": ["name"]}

    muts = validate_set_request(req, catalog, alias_map={}, list_keys=list_keys)
    assert len(muts) == 1
    assert muts[0].operation == "delete"
    assert muts[0].canonical_path == "/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:description"


def test_validate_set_request_alias_requalify_with_predicates():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/interfaces/interface[name=eth0]/config/mtu"),
                val=gnmi_pb2.TypedValue(int_val=9000),
            )
        ]
    )
    alias_map = {
        "/interfaces/interface/config/mtu": "/p1:interfaces/p1:interface/p1:config/p1:mtu",
    }
    catalog = {
        "/p1:interfaces/p1:interface/p1:config/p1:mtu": _catalog_entry(),
    }
    list_keys = {"/p1:interfaces/p1:interface": ["name"]}

    muts = validate_set_request(req, catalog, alias_map=alias_map, list_keys=list_keys)
    assert len(muts) == 1
    assert muts[0].canonical_path == "/p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu"
    assert muts[0].value == 9000


def test_validate_set_request_rejects_unknown_path():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    with pytest.raises(SetValidationError, match="path not found in catalog"):
        validate_set_request(req, catalog_map={}, alias_map={})


def test_validate_set_request_rejects_non_config_path():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:state/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    catalog = {
        "/p1:system/p1:state/p1:hostname": _catalog_entry(writable=False),
    }
    with pytest.raises(SetValidationError, match="not writable config"):
        validate_set_request(req, catalog_map=catalog, alias_map={})


def test_validate_set_request_rejects_missing_list_key():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:interfaces/p1:interface/p1:config/p1:enabled"),
                val=gnmi_pb2.TypedValue(bool_val=True),
            )
        ]
    )
    catalog = {
        "/p1:interfaces/p1:interface/p1:config/p1:enabled": _catalog_entry(),
    }
    list_keys = {"/p1:interfaces/p1:interface": ["name"]}

    with pytest.raises(SetValidationError, match="missing list keys"):
        validate_set_request(req, catalog_map=catalog, alias_map={}, list_keys=list_keys)


def test_validate_set_request_rejects_union_replace():
    req = gnmi_pb2.SetRequest(
        union_replace=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            )
        ]
    )
    with pytest.raises(SetUnsupportedError, match="union_replace"):
        validate_set_request(req, catalog_map={}, alias_map={})


def test_validate_set_request_rejects_unsupported_typed_value():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(bytes_val=b"r1"),
            )
        ]
    )
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    with pytest.raises(SetUnsupportedError, match="not supported"):
        validate_set_request(req, catalog_map=catalog, alias_map={})


def test_validate_set_request_json_ietf_scalar_supported():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(json_ietf_val=json.dumps("r1").encode("utf-8")),
            )
        ]
    )
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}

    muts = validate_set_request(req, catalog_map=catalog, alias_map={})
    assert len(muts) == 1
    assert muts[0].value == "r1"


def test_validate_set_request_json_ietf_object_rejected():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(
                    json_ietf_val=json.dumps({"hostname": "r1"}).encode("utf-8")
                ),
            )
        ]
    )
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    with pytest.raises(SetUnsupportedError, match="objects/arrays"):
        validate_set_request(req, catalog_map=catalog, alias_map={})


def test_validate_set_request_json_ietf_array_rejected():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(json_ietf_val=json.dumps(["r1"]).encode("utf-8")),
            )
        ]
    )
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}
    with pytest.raises(SetUnsupportedError, match="objects/arrays"):
        validate_set_request(req, catalog_map=catalog, alias_map={})


def test_validate_set_request_duplicate_paths_preserved():
    req = gnmi_pb2.SetRequest(
        update=[
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            ),
            gnmi_pb2.Update(
                path=_path_from_str("/p1:system/p1:config/p1:hostname"),
                val=gnmi_pb2.TypedValue(string_val="r1"),
            ),
        ]
    )
    catalog = {"/p1:system/p1:config/p1:hostname": _catalog_entry()}

    muts = validate_set_request(req, catalog_map=catalog, alias_map={})
    assert len(muts) == 2
    assert muts[0].canonical_path == muts[1].canonical_path
    assert muts[0].value == muts[1].value == "r1"
