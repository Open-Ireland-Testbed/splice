# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from xml.sax.saxutils import escape

from splice.pathing import decode_key_value, encode_key_value

_PRED_RE = re.compile(r"(\[[^\]]+\])")

# Note: keep regex ASCII-safe; matches bracketed predicate blocks.


def strip_predicates(path: str) -> str:
    if not path:
        return path
    # Remove predicate blocks BEFORE splitting on '/': a key value can itself
    # contain '/' (e.g. interface[name=1/2/n1/ot400]), and splitting first
    # would turn those into bogus path segments.
    bare = _PRED_RE.sub("", path)
    parts = [p for p in bare.strip("/").split("/") if p]
    return "/" + "/".join(parts)


def parse_predicates(path: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    if not path:
        return out
    parts = [p for p in path.strip("/").split("/") if p]
    cur_parts: List[str] = []
    for part in parts:
        cur_parts.append(part.split("[", 1)[0])
        if "[" not in part:
            continue
        preds = part[part.find("[") :].strip()
        kv = {}
        for block in _PRED_RE.findall(preds):
            inner = block.strip("[]")
            if not inner:
                continue
            for pair in inner.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    kv[k.strip()] = decode_key_value(v.strip())
        if kv:
            out["/" + "/".join(cur_parts)] = kv
    return out


def _is_safe_key_value(value: str) -> bool:
    # Brackets confuse predicate parsing even when encoded.
    return "[" not in value and "]" not in value


def _filter_instances(instances: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    for inst in instances:
        if all(_is_safe_key_value(str(v)) for v in inst.values()):
            out.append(inst)
    return out


def _cartesian(instances: List[List[Dict[str, str]]]) -> List[List[Dict[str, str]]]:
    if not instances:
        return [[]]
    out = [[]]
    for group in instances:
        next_out = []
        for prev in out:
            for item in group:
                next_out.append(prev + [item])
        out = next_out
    return out


def expand_path(
    base_path: str,
    list_keys: Dict[str, List[str]],
    list_instances: Dict[str, List[Dict[str, str]]],
) -> List[Tuple[str, Dict[str, Dict[str, str]]]]:
    if not list_keys or not list_instances:
        return [(base_path, {})]
    parts = [p for p in base_path.strip("/").split("/") if p]
    list_paths: List[str] = []
    cur_parts: List[str] = []
    for part in parts:
        cur_parts.append(part)
        cur_path = "/" + "/".join(cur_parts)
        if cur_path in list_keys:
            list_paths.append(cur_path)
    if not list_paths:
        return [(base_path, {})]
    groups = []
    for lp in list_paths:
        inst = list_instances.get(lp)
        if not inst:
            return [(base_path, {})]
        inst = _filter_instances(inst)
        if not inst:
            return [(base_path, {})]
        groups.append(inst)
    combos = _cartesian(groups)
    expanded = []
    for combo in combos:
        instance_map: Dict[str, Dict[str, str]] = {}
        for lp, kv in zip(list_paths, combo):
            instance_map[lp] = kv
        expanded.append((apply_predicates(base_path, list_paths, instance_map), instance_map))
    return expanded


@dataclass
class ExpansionResult:
    """Result of a guarded path expansion."""

    base_path: str
    expanded_paths: List[Tuple[str, Dict[str, Dict[str, str]]]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None
    estimated_size: int = 0
    actual_size: int = 0


def estimate_expansion_size(
    base_path: str,
    list_keys: Dict[str, List[str]],
    list_instances: Dict[str, List[Dict[str, str]]],
) -> int:
    """Compute product of instance counts for nested lists without generating paths.

    Returns 1 if the path has no keyed lists or any list has no instances.
    """
    if not list_keys or not list_instances:
        return 1
    parts = [p for p in base_path.strip("/").split("/") if p]
    list_paths: List[str] = []
    cur_parts: List[str] = []
    for part in parts:
        cur_parts.append(part)
        cur_path = "/" + "/".join(cur_parts)
        if cur_path in list_keys:
            list_paths.append(cur_path)
    if not list_paths:
        return 1
    size = 1
    for lp in list_paths:
        inst = list_instances.get(lp)
        if not inst:
            return 1
        inst = _filter_instances(inst)
        if not inst:
            return 1
        size *= len(inst)
    return size


def expand_path_guarded(
    base_path: str,
    list_keys: Dict[str, List[str]],
    list_instances: Dict[str, List[Dict[str, str]]],
    max_per_base: int = 10000,
) -> ExpansionResult:
    """Expand path with guardrails to prevent cartesian explosion.

    If the estimated expansion size exceeds max_per_base, returns a skipped
    result with a reason instead of generating paths.
    """
    estimated = estimate_expansion_size(base_path, list_keys, list_instances)
    if estimated > max_per_base:
        return ExpansionResult(
            base_path=base_path,
            expanded_paths=[(base_path, {})],
            skipped=True,
            skip_reason=f"expansion_size_{estimated}_exceeds_max_{max_per_base}",
            estimated_size=estimated,
            actual_size=0,
        )
    paths = expand_path(base_path, list_keys, list_instances)
    return ExpansionResult(
        base_path=base_path,
        expanded_paths=paths,
        skipped=False,
        estimated_size=estimated,
        actual_size=len(paths),
    )


def apply_predicates(
    base_path: str,
    list_paths: Iterable[str],
    instance_map: Dict[str, Dict[str, str]],
) -> str:
    list_paths_set = set(list_paths)
    parts = [p for p in base_path.strip("/").split("/") if p]
    cur_parts: List[str] = []
    new_parts: List[str] = []
    for part in parts:
        cur_parts.append(part)
        cur_path = "/" + "/".join(cur_parts)
        if cur_path in list_paths_set and cur_path in instance_map:
            kv = instance_map[cur_path]
            if kv:
                pred = ",".join([f"{k}={encode_key_value(str(v))}" for k, v in kv.items()])
                new_parts.append(f"{part}[{pred}]")
            else:
                new_parts.append(part)
        else:
            new_parts.append(part)
    return "/" + "/".join(new_parts)


def build_filter_xml_for_path(
    canonical_path: str,
    namespace_map: Dict[str, str] | None,
    list_keys: Dict[str, List[str]],
    instance_map: Dict[str, Dict[str, str]],
) -> str:
    # build filter with key values inserted where available
    if not canonical_path:
        return ""
    parts = [p for p in canonical_path.strip("/").split("/") if p]
    prefix_to_ns = {}
    if namespace_map:
        prefix_to_ns = {v: k for k, v in namespace_map.items()}
    path_elems: List[Tuple[str, str, str]] = []
    for part in parts:
        name_part = part.split("[", 1)[0]
        if ":" in name_part:
            prefix, name = name_part.split(":", 1)
            ns = prefix_to_ns.get(prefix, "")
            path_elems.append((name, ns, prefix))
        else:
            path_elems.append((name_part, "", ""))

    namespaces: List[str] = []
    for _, ns, _ in path_elems:
        if ns not in namespaces:
            namespaces.append(ns)
    prefix_map = {ns: f"p{idx+1}" for idx, ns in enumerate(namespaces)}
    if namespace_map:
        for ns, prefix in namespace_map.items():
            prefix_map[ns] = prefix

    def tag(name: str, ns: str) -> str:
        return f"{prefix_map[ns]}:{name}" if ns else name

    xmlns_attrs = " ".join([f"xmlns:{prefix_map[ns]}=\"{ns}\"" for ns in namespaces if ns])
    lines = ["<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"]
    indent = "  "
    cur_parts: List[str] = []
    for i, (name, ns, _) in enumerate(path_elems):
        cur_parts.append(name_part := name)
        cur_path = "/" + "/".join([p for p in canonical_path.strip("/").split("/")[: i + 1]])
        t = tag(name, ns)
        if i == 0 and xmlns_attrs:
            lines.append(f"{indent}<{t} {xmlns_attrs}>")
        else:
            lines.append(f"{indent}<{t}>")
        indent += "  "
        # insert key leaves if this is a list node
        list_path = strip_predicates(cur_path)
        keys = list_keys.get(list_path)
        values = instance_map.get(list_path)
        if keys and values:
            for k in keys:
                if k in values:
                    kt = tag(k, ns)
                    lines.append(f"{indent}<{kt}>{escape(decode_key_value(str(values[k])))}</{kt}>")
    for i in range(len(path_elems) - 1, -1, -1):
        indent = indent[:-2]
        name, ns, _ = path_elems[i]
        t = tag(name, ns)
        lines.append(f"{indent}</{t}>")
    lines.append("</filter>")
    return "\n".join(lines)
