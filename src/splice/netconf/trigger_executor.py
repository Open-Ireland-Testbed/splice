# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from splice.expander import build_filter_xml_for_path, parse_predicates, strip_predicates
from splice.netconf.client import NetconfClientPool
from splice.netconf.setter import parse_canonical_path
from splice.pathing import encode_key_value, is_path_prefix, normalize_path


class TriggerValidationError(ValueError):
    """Invalid trigger request."""


@dataclass(frozen=True)
class TriggerExecutionResult:
    requested_paths: list[str]
    updates: list[dict]
    errors: list[dict]


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
    src_segments = parse_canonical_path(original_path)
    dst_segments = parse_canonical_path(canonical_base)
    dst_tags = [seg.tag for seg in dst_segments]

    if len(src_segments) == len(dst_segments):
        predicates: dict[int, list[tuple[str, str]]] = {}
        for idx, src in enumerate(src_segments):
            if src.key_values:
                predicates[idx] = list(src.key_values)
        return _render_path(dst_tags, predicates)

    predicates: dict[int, list[tuple[str, str]]] = {}
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
            raise TriggerValidationError(
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

    canonical_base = alias_map.get(base)
    if canonical_base:
        return _requalify_path(path, canonical_base)

    if base in catalog_map:
        return path

    base_norm = normalize_path(base)
    alias_matches = [v for k, v in alias_map.items() if normalize_path(k) == base_norm]
    if len(alias_matches) == 1:
        return _requalify_path(path, alias_matches[0])
    if len(alias_matches) > 1:
        raise TriggerValidationError(f"ambiguous alias path: {path}")

    catalog_matches = [k for k in catalog_map if normalize_path(k) == base_norm]
    if len(catalog_matches) == 1:
        return _requalify_path(path, catalog_matches[0])
    if len(catalog_matches) > 1:
        raise TriggerValidationError(f"ambiguous canonical path: {path}")

    raise TriggerValidationError(f"path not found in catalog: {path}")


def _dedupe_paths(paths: Sequence[str]) -> list[str]:
    seen = set()
    out = []
    for p in paths:
        key = normalize_path(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


class NetconfTriggerExecutor:
    """Execute direct NETCONF reads for requested trigger paths."""

    def __init__(
        self,
        *,
        pool: NetconfClientPool,
        catalog_map: Mapping[str, dict],
        alias_map: Mapping[str, str],
        list_keys: Mapping[str, list[str]] | None,
        parse_reply: Callable[[str], list[dict]],
        max_paths_per_request: int = 50,
    ) -> None:
        self._pool = pool
        self._catalog_map = dict(catalog_map)
        self._alias_map = dict(alias_map)
        self._list_keys = dict(list_keys or {})
        self._parse_reply = parse_reply
        self._max_paths = int(max_paths_per_request)

    def _read_path(self, canonical_path: str) -> list[dict]:
        base = strip_predicates(canonical_path)
        entry = self._catalog_map.get(base)
        if entry is None:
            raise TriggerValidationError(f"path not found in catalog: {canonical_path}")

        namespace_map = entry.get("namespace_map")
        instance_map = parse_predicates(canonical_path)
        filter_xml = build_filter_xml_for_path(
            canonical_path,
            namespace_map,
            self._list_keys,
            instance_map,
        )
        if bool(entry.get("config", False)):
            reply = self._pool.get_config("running", filter_xml)
        else:
            reply = self._pool.get(filter_xml)

        updates = self._parse_reply(reply.data_xml)
        # parse_reply may emit per-instance key predicates (e.g.
        # /interfaces/interface[name=eth0]/...). Match predicate-aware: a request
        # that pins an instance ([name=eth0]) keeps only that instance, while a
        # predicate-less request matches every instance of the list (compare with
        # predicates stripped so keyed reply paths still match).
        pref_norm = normalize_path(canonical_path)
        pref_base = strip_predicates(pref_norm)
        request_pins_instance = pref_norm != pref_base
        kept = []
        for u in updates:
            upath = normalize_path(str(u.get("path", "")))
            if request_pins_instance:
                if is_path_prefix(upath, pref_norm):
                    kept.append(u)
            elif is_path_prefix(strip_predicates(upath), pref_base):
                kept.append(u)
        return kept

    def execute(self, paths: Sequence[str]) -> TriggerExecutionResult:
        if not paths:
            raise TriggerValidationError("trigger request requires at least one path")
        if len(paths) > self._max_paths:
            raise TriggerValidationError(
                f"trigger request exceeds max paths ({len(paths)} > {self._max_paths})"
            )

        resolved = []
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip():
                raise TriggerValidationError("trigger paths must be non-empty strings")
            resolved.append(
                _resolve_canonical_path(
                    raw.strip(),
                    alias_map=self._alias_map,
                    catalog_map=self._catalog_map,
                )
            )
        resolved = _dedupe_paths(resolved)

        updates: list[dict] = []
        errors: list[dict] = []
        for path in resolved:
            try:
                updates.extend(self._read_path(path))
            except Exception as exc:
                errors.append({"path": path, "error": str(exc)})

        return TriggerExecutionResult(
            requested_paths=resolved,
            updates=updates,
            errors=errors,
        )
