# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from typing import Mapping, Sequence

import splice.gnmi_pb2 as gnmi_pb2
from splice.expander import strip_predicates
from splice.netconf.setter import SetMutation, parse_canonical_path
from splice.pathing import decode_key_value, encode_key_value, normalize_path


class SetValidationError(ValueError):
    """SetRequest is syntactically valid gNMI, but invalid for this target."""


class SetUnsupportedError(NotImplementedError):
    """SetRequest feature/value type is not supported in current implementation."""


def validate_set_request(
    request: gnmi_pb2.SetRequest,
    catalog_map: dict[str, dict],
    alias_map: dict[str, str],
    list_keys: dict[str, list[str]] | None = None,
) -> list[SetMutation]:
    """Validate and normalize SetRequest into an ordered list of mutations.

    This function is pure: it depends only on its inputs and has no side effects.
    """
    if request.union_replace:
        raise SetUnsupportedError("union_replace is not supported in v1")

    list_keys = list_keys or {}
    path_key_order = _path_key_order_map(list_keys)
    mutations: list[SetMutation] = []

    for delete_path in request.delete:
        raw_path = _effective_path(request.prefix, delete_path, path_key_order)
        canonical_path = _resolve_canonical_path(
            raw_path,
            alias_map=alias_map,
            catalog_map=catalog_map,
        )
        entry = _catalog_entry_for_path(canonical_path, catalog_map)
        _validate_writable_path(canonical_path, entry)
        _validate_list_key_completeness(canonical_path, list_keys)
        mutations.append(
            SetMutation(
                canonical_path=canonical_path,
                operation="delete",
                value=None,
                namespace_map=entry.get("namespace_map"),
            )
        )

    for replace in request.replace:
        raw_path = _effective_path(request.prefix, replace.path, path_key_order)
        canonical_path = _resolve_canonical_path(
            raw_path,
            alias_map=alias_map,
            catalog_map=catalog_map,
        )
        entry = _catalog_entry_for_path(canonical_path, catalog_map)
        _validate_writable_path(canonical_path, entry)
        _validate_list_key_completeness(canonical_path, list_keys)
        mutations.append(
            SetMutation(
                canonical_path=canonical_path,
                operation="replace",
                value=_decode_typed_value(replace.val),
                namespace_map=entry.get("namespace_map"),
            )
        )

    for update in request.update:
        raw_path = _effective_path(request.prefix, update.path, path_key_order)
        canonical_path = _resolve_canonical_path(
            raw_path,
            alias_map=alias_map,
            catalog_map=catalog_map,
        )
        entry = _catalog_entry_for_path(canonical_path, catalog_map)
        _validate_writable_path(canonical_path, entry)
        _validate_list_key_completeness(canonical_path, list_keys)
        mutations.append(
            SetMutation(
                canonical_path=canonical_path,
                operation="merge",
                value=_decode_typed_value(update.val),
                namespace_map=entry.get("namespace_map"),
            )
        )

    return mutations


def _path_key_order_map(list_keys: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    # _path_to_str() matches exact cur-path; include both raw and normalized keys.
    out: dict[str, list[str]] = {}
    for path, keys in list_keys.items():
        key_list = list(keys)
        out[path] = key_list
        out[normalize_path(path)] = key_list
    return out


def _effective_path(
    prefix: gnmi_pb2.Path,
    path: gnmi_pb2.Path,
    list_keys: dict[str, list[str]],
) -> str:
    elems = []
    if prefix and getattr(prefix, "elem", None):
        elems.extend(prefix.elem)
    if path and getattr(path, "elem", None):
        elems.extend(path.elem)
    if not elems:
        raise SetValidationError("set operation path is empty after applying prefix")
    return _path_to_str(gnmi_pb2.Path(elem=elems), list_keys)


def _path_to_str(path: gnmi_pb2.Path, list_keys: Mapping[str, Sequence[str]] | None = None) -> str:
    elems = getattr(path, "elem", []) or []
    parts = []
    cur_parts: list[str] = []
    for elem in elems:
        name = elem.name
        cur_parts.append(name)
        if getattr(elem, "key", None):
            items = list(elem.key.items())
            if list_keys:
                key_order = list_keys.get("/" + "/".join(cur_parts))
                if key_order:
                    order_index = {k: i for i, k in enumerate(key_order)}
                    items.sort(key=lambda kv: (order_index.get(kv[0], len(order_index)), kv[0]))
                else:
                    items.sort(key=lambda kv: kv[0])
            else:
                items.sort(key=lambda kv: kv[0])
            pred = ",".join(
                f"{k}={encode_key_value(decode_key_value(str(v)))}" for k, v in items
            )
            parts.append(f"{name}[{pred}]")
        else:
            parts.append(name)
    return "/" + "/".join(parts)


def _local_name(seg: str) -> str:
    if ":" in seg:
        seg = seg.split(":", 1)[1]
    return seg.split("[", 1)[0]


def _render_path(segments: list[str], predicates: dict[int, list[tuple[str, str]]]) -> str:
    out = []
    for idx, seg in enumerate(segments):
        pred_list = predicates.get(idx, [])
        if pred_list:
            pred = ",".join(f"{k}={encode_key_value(v)}" for k, v in pred_list)
            out.append(f"{seg}[{pred}]")
        else:
            out.append(seg)
    return "/" + "/".join(out)


def _requalify_path(original_path: str, canonical_base: str) -> str:
    """Reattach original predicates onto canonical base path."""
    src_segments = parse_canonical_path(original_path)
    dst_segments = parse_canonical_path(canonical_base)
    dst_tags = [seg.tag for seg in dst_segments]

    if len(src_segments) == len(dst_segments):
        predicates: dict[int, list[tuple[str, str]]] = {}
        for idx, src in enumerate(src_segments):
            if src.key_values:
                predicates[idx] = list(src.key_values)
        return _render_path(dst_tags, predicates)

    predicates = {}
    used = set()
    for idx, src in enumerate(src_segments):
        if not src.key_values:
            continue
        target_idx = None
        if idx < len(dst_segments) and _local_name(dst_segments[idx].tag) == _local_name(src.tag):
            target_idx = idx
        else:
            for j, dst in enumerate(dst_segments):
                if j in used:
                    continue
                if _local_name(dst.tag) == _local_name(src.tag):
                    target_idx = j
                    break
        if target_idx is None:
            raise SetValidationError(
                f"cannot reattach predicates from {original_path!r} to {canonical_base!r}"
            )
        predicates[target_idx] = list(src.key_values)
        used.add(target_idx)
    return _render_path(dst_tags, predicates)


def _resolve_canonical_path(
    path: str,
    alias_map: Mapping[str, str],
    catalog_map: Mapping[str, dict],
) -> str:
    base = strip_predicates(path)

    # 1) Direct alias match.
    canonical_base = alias_map.get(base)
    if canonical_base:
        return _requalify_path(path, canonical_base)

    # 2) Direct catalog canonical path (already canonical).
    if base in catalog_map:
        return path

    # 3) Normalized alias fallback when caller omits prefixes.
    base_norm = normalize_path(base)
    alias_matches = [v for k, v in alias_map.items() if normalize_path(k) == base_norm]
    if len(alias_matches) == 1:
        return _requalify_path(path, alias_matches[0])
    if len(alias_matches) > 1:
        raise SetValidationError(f"ambiguous alias path: {path}")

    # 4) Normalized catalog fallback when caller omits prefixes.
    catalog_matches = [k for k in catalog_map if normalize_path(k) == base_norm]
    if len(catalog_matches) == 1:
        return _requalify_path(path, catalog_matches[0])
    if len(catalog_matches) > 1:
        raise SetValidationError(f"ambiguous canonical path: {path}")

    return path


def _catalog_entry_for_path(path: str, catalog_map: Mapping[str, dict]) -> dict:
    base = strip_predicates(path)
    entry = catalog_map.get(base)
    if entry is None:
        raise SetValidationError(f"path not found in catalog: {path}")
    return entry


def _validate_writable_path(path: str, entry: Mapping[str, object]) -> None:
    if not bool(entry.get("config", False)):
        raise SetValidationError(f"path is not writable config: {path}")


def _validate_list_key_completeness(path: str, list_keys: Mapping[str, Sequence[str]]) -> None:
    if not list_keys:
        return
    exact = dict(list_keys)
    norm = {normalize_path(k): list(v) for k, v in list_keys.items()}

    parts = []
    segments = parse_canonical_path(path)
    for seg in segments:
        parts.append(seg.tag)
        cur_path = "/" + "/".join(parts)
        required = exact.get(cur_path)
        if required is None:
            required = norm.get(normalize_path(cur_path))
        if not required:
            continue
        present = {k for k, _ in seg.key_values}
        missing = [k for k in required if k not in present]
        if missing:
            miss = ", ".join(missing)
            raise SetValidationError(f"missing list keys for {cur_path}: {miss}")


def _decode_typed_value(val: gnmi_pb2.TypedValue) -> object:
    which = val.WhichOneof("value")
    if not which:
        raise SetValidationError("typed value is required for update/replace")

    if which == "string_val":
        return str(val.string_val)
    if which == "int_val":
        return int(val.int_val)
    if which == "uint_val":
        return int(val.uint_val)
    if which in ("double_val", "float_val"):
        return float(getattr(val, which))
    if which == "bool_val":
        return bool(val.bool_val)
    if which == "json_ietf_val":
        return _decode_json_ietf_scalar(val.json_ietf_val)

    raise SetUnsupportedError(f"typed value {which} is not supported in v1")


def _decode_json_ietf_scalar(raw: bytes) -> object:
    try:
        txt = raw.decode("utf-8")
    except Exception as exc:
        raise SetValidationError("json_ietf_val must be valid utf-8") from exc
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError as exc:
        raise SetValidationError(f"invalid json_ietf_val: {exc.msg}") from exc

    if isinstance(parsed, (dict, list)):
        raise SetUnsupportedError("json_ietf_val objects/arrays are not supported in v1")
    if parsed is None:
        raise SetUnsupportedError("json_ietf_val null is not supported in v1")
    return parsed
