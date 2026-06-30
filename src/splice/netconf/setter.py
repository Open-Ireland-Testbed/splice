# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import re
from typing import Mapping, Sequence
from xml.sax.saxutils import escape

from splice.pathing import decode_key_value


NETCONF_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
_ALLOWED_SET_OPS = {"merge", "replace", "delete"}
_PRED_BLOCK_RE = re.compile(r"\[([^\]]+)\]")
# XML NCName, optionally prefixed (QName). Element/key names are interpolated
# into XML tags unescaped, so they MUST be validated here — a name like
# "x><inject/" would otherwise break out of the tag. Values are escape()d.
_NCNAME = r"[A-Za-z_][A-Za-z0-9_.-]*"
_QNAME_RE = re.compile(rf"^(?:{_NCNAME}:)?{_NCNAME}$")


def _require_xml_name(name: str, context: str) -> None:
    if not _QNAME_RE.match(name):
        raise ValueError(f"invalid XML name {name!r} in {context}")


@dataclass(frozen=True)
class SetMutation:
    """Intermediate write intent used by the NETCONF set XML builder."""

    canonical_path: str
    operation: str  # merge|replace|delete
    value: object | None = None
    namespace_map: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ParsedPathSegment:
    tag: str
    prefix: str
    key_values: tuple[tuple[str, str], ...] = ()


@dataclass
class _TreeNode:
    tag: str
    prefix: str
    key_values: tuple[tuple[str, str], ...] = ()
    children: OrderedDict[tuple[str, tuple[tuple[str, str], ...]], "_TreeNode"] = field(
        default_factory=OrderedDict
    )
    operation: str | None = None
    scalar_value: str | None = None


def _normalize_set_op(operation: str) -> str:
    op = (operation or "").strip().lower()
    if op not in _ALLOWED_SET_OPS:
        raise ValueError(f"unsupported set operation: {operation!r}")
    return op


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_path_segment(segment: str) -> ParsedPathSegment:
    seg = (segment or "").strip()
    if not seg:
        raise ValueError("empty path segment")

    base = seg.split("[", 1)[0]
    if not base:
        raise ValueError(f"invalid path segment: {segment!r}")
    _require_xml_name(base, f"element name of segment {segment!r}")

    prefix = ""
    if ":" in base:
        prefix, _ = base.split(":", 1)

    key_values: list[tuple[str, str]] = []
    pred_text = seg[len(base) :]
    for block in _PRED_BLOCK_RE.findall(pred_text):
        for pair in block.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"invalid predicate {pair!r} in segment {segment!r}")
            key, raw_value = pair.split("=", 1)
            key = key.strip()
            if not key:
                raise ValueError(f"empty predicate key in segment {segment!r}")
            _require_xml_name(key, f"predicate key in segment {segment!r}")
            value = decode_key_value(_strip_quotes(raw_value.strip()))
            key_values.append((key, value))

    return ParsedPathSegment(tag=base, prefix=prefix, key_values=tuple(key_values))


def parse_canonical_path(path: str) -> list[ParsedPathSegment]:
    if not path:
        raise ValueError("canonical path cannot be empty")
    out = [_parse_path_segment(seg) for seg in path.strip("/").split("/") if seg]
    if not out:
        raise ValueError("canonical path must contain at least one segment")
    return out


def _scalar_to_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _merge_prefix_map(
    prefix_to_ns: dict[str, str], namespace_map: Mapping[str, str] | None
) -> None:
    if not namespace_map:
        return
    for ns, prefix in namespace_map.items():
        if not prefix:
            continue
        existing = prefix_to_ns.get(prefix)
        if existing is not None and existing != ns:
            raise ValueError(
                f"prefix {prefix!r} maps to multiple namespaces: {existing!r}, {ns!r}"
            )
        prefix_to_ns[prefix] = str(ns)


def _render_node(node: _TreeNode, level: int) -> list[str]:
    indent = "  " * level
    attrs: list[str] = []
    if node.operation is not None:
        attrs.append(f'nc:operation="{node.operation}"')
    attr_text = f" {' '.join(attrs)}" if attrs else ""

    key_lines: list[str] = []
    for key_name, key_value in node.key_values:
        if ":" in key_name:
            key_tag = key_name
        elif node.prefix:
            key_tag = f"{node.prefix}:{key_name}"
        else:
            key_tag = key_name
        key_lines.append(
            f"{indent}  <{key_tag}>{escape(key_value)}</{key_tag}>"
        )

    child_lines: list[str] = []
    for child in node.children.values():
        child_lines.extend(_render_node(child, level + 1))

    if node.scalar_value is not None:
        if key_lines or child_lines:
            raise ValueError(
                f"path {node.tag!r} has scalar value and nested elements"
            )
        return [
            f"{indent}<{node.tag}{attr_text}>"
            f"{escape(node.scalar_value)}"
            f"</{node.tag}>"
        ]

    if not key_lines and not child_lines:
        return [f"{indent}<{node.tag}{attr_text}/>"]

    lines = [f"{indent}<{node.tag}{attr_text}>"]
    lines.extend(key_lines)
    lines.extend(child_lines)
    lines.append(f"{indent}</{node.tag}>")
    return lines


def build_config_xml(mutations: Sequence[SetMutation]) -> str:
    """Build one merged NETCONF <config> subtree from set mutations.

    The input contract is intentionally simple and explicit:
      - each mutation is (canonical_path, operation, scalar value, namespace_map)
      - canonical paths may include predicates for keyed list entries
      - shared ancestors are merged into a single subtree
    """
    if not mutations:
        raise ValueError("at least one mutation is required")

    root = _TreeNode(tag="", prefix="")
    prefix_to_ns: dict[str, str] = {}
    used_prefixes: set[str] = set()

    for mutation in mutations:
        op = _normalize_set_op(mutation.operation)
        segments = parse_canonical_path(mutation.canonical_path)
        _merge_prefix_map(prefix_to_ns, mutation.namespace_map)

        node = root
        for seg in segments:
            if seg.prefix:
                used_prefixes.add(seg.prefix)
            child_key = (seg.tag, seg.key_values)
            child = node.children.get(child_key)
            if child is None:
                child = _TreeNode(
                    tag=seg.tag,
                    prefix=seg.prefix,
                    key_values=seg.key_values,
                )
                node.children[child_key] = child
            node = child

        if op == "delete":
            if mutation.value not in (None, ""):
                raise ValueError(
                    f"delete mutation must not include a value: {mutation.canonical_path}"
                )
            incoming_value = None
        else:
            if mutation.value is None:
                raise ValueError(
                    f"{op} mutation requires a scalar value: {mutation.canonical_path}"
                )
            if isinstance(mutation.value, (dict, list, tuple, set, bytes, bytearray)):
                raise TypeError(
                    f"{op} mutation currently supports scalar values only: {mutation.canonical_path}"
                )
            incoming_value = _scalar_to_text(mutation.value)

        if node.operation is not None:
            if node.operation != op or node.scalar_value != incoming_value:
                raise ValueError(f"conflicting duplicate mutation for path: {mutation.canonical_path}")
            continue

        node.operation = op
        node.scalar_value = incoming_value

    missing_prefixes = sorted(p for p in used_prefixes if p not in prefix_to_ns)
    if missing_prefixes:
        joined = ", ".join(missing_prefixes)
        raise ValueError(f"missing namespace mappings for prefixes: {joined}")

    attrs = [f'xmlns:nc="{NETCONF_BASE_NS}"']
    for prefix in sorted(prefix_to_ns):
        ns = prefix_to_ns[prefix]
        attrs.append(f'xmlns:{prefix}="{_escape_attr(ns)}"')

    lines = [f'<config xmlns="{NETCONF_BASE_NS}" {" ".join(attrs)}>']
    for child in root.children.values():
        lines.extend(_render_node(child, level=1))
    lines.append("</config>")
    return "\n".join(lines)


def build_edit_config_rpc(config_xml: str, target: str) -> str:
    datastore = (target or "").strip()
    if not datastore:
        raise ValueError("edit-config target cannot be empty")
    return (
        f'<edit-config xmlns="{NETCONF_BASE_NS}">'
        f"<target><{datastore}/></target>"
        f"{config_xml}"
        f"</edit-config>"
    )
