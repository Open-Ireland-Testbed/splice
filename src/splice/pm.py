# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
import time
from typing import Dict, List
from defusedxml import ElementTree as ET

from splice.pathing import encode_key_value


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# Leaf names that identify a list instance in vendor PM trees
# (e.g. ADVA: interface/name, card/name, cpu/index, disk/id, me/entity-name).
_INSTANCE_KEY_LEAVES = ("name", "entity-name", "index", "id")

# "No measurement available" sentinels. PM mon-vals are numeric measurements;
# these placeholders must be dropped (not published as strings) to avoid
# poisoning downstream TSDB field types. Kept tight so genuine string-valued
# telemetry is not silently discarded.
_NO_MEASUREMENT_VALUES = frozenset(
    {"", "na", "n/a", "-", "--", "n.a.", "unavailable", "not-available", "none", "null"}
)


def _instance_keys_for(pm, parent_map) -> Dict[str, str]:
    """Collect {ancestorLocalName: keyValue} for each ancestor of a
    pm-current-data element that carries an identifying key leaf.

    Starts at the PARENT of the pm-current-data element so the entity's own
    "name" (already a path segment) is not duplicated as a key. Nearest
    ancestor wins on local-name collisions.
    """
    keys: Dict[str, str] = {}
    node = parent_map.get(pm)
    while node is not None:
        for child in node:
            if len(child) == 0 and _local_name(child.tag) in _INSTANCE_KEY_LEAVES:
                value = _text(child)
                if value:
                    keys.setdefault(_local_name(node.tag), value)
                break
        node = parent_map.get(node)
    return keys


def _keyed_segment(segment: str, instance: Dict[str, str]) -> str:
    """Render instance keys as a single-bracket, comma-joined, alphabetically
    sorted predicate with percent-encoded values — the exact canonical form
    gnmi_server._path_to_str produces for incoming requests, so cached paths
    and client queries compare equal. Encoding keeps '/', ':' and ',' out of
    the segment, which naive path splitters elsewhere rely on.
    """
    if not instance:
        return segment
    kv = ",".join(f"{k}={encode_key_value(v)}" for k, v in sorted(instance.items()))
    return f"{segment}[{kv}]"


def _text(el) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def parse_selector(expr: str):
    # simple AND/OR grammar with = and ~ (regex). Example: name~'1/2/' and type='transceiver'
    tokens = re.split(r"\s+(and|or)\s+", expr, flags=re.IGNORECASE)
    clauses = []
    ops = []
    for tok in tokens:
        low = tok.lower()
        if low in ("and", "or"):
            ops.append(low)
            continue
        m = re.match(r"\s*([A-Za-z0-9_.:-]+)\s*(=|~)\s*'([^']*)'\s*", tok)
        if not m:
            raise ValueError(f"invalid selector: {tok}")
        key, op, val = m.group(1), m.group(2), m.group(3)
        clauses.append((key, op, val))
    return clauses, ops


def match_selector(inst, clauses, ops) -> bool:
    def eval_clause(c):
        k, op, val = c
        if k not in inst:
            return False
        v = str(inst[k])
        if op == "=":
            return v == val
        return re.search(val, v) is not None

    if not clauses:
        return False
    result = eval_clause(clauses[0])
    for op, clause in zip(ops, clauses[1:]):
        if op == "and":
            result = result and eval_clause(clause)
        else:
            result = result or eval_clause(clause)
    return result

def _domain_from_namespace(ns: str | None) -> str:
    if not ns:
        return "unknown"
    tail = ns.rstrip("/").rsplit("/", 1)[-1]
    parts = tail.split("-")
    if len(parts) > 2 and parts[0] == "aos":
        return "-".join(parts[2:])
    return tail


def _coerce_value(txt: str):
    s = (txt or "").strip()
    if s == "":
        return s
    try:
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
    except Exception:
        pass
    try:
        return float(s)
    except Exception:
        return s


def parse_pm_current_data(
    xml: str,
    live_token: str | None = None,
    path_prefix: str = "/pm",
) -> List[Dict[str, object]]:
    """Parse pm-current-data style telemetry into gNMI updates.

    Heuristics:
    - find elements named pm-current-data
    - filter by bin-interval containing live_token if provided
    - emit records for montype-monval mon-type/mon-val pairs
    """
    if not xml:
        return []
    root = ET.fromstring(xml)
    ts = time.time_ns()
    updates: List[Dict[str, object]] = []
    token_l = (live_token or "").lower()
    # ElementTree has no parent pointers; one O(N) pass gives us ancestor
    # access for instance-key discovery.
    parent_map = {child: parent for parent in root.iter() for child in parent}

    for pm in root.iter():
        if _local_name(pm.tag) != "pm-current-data":
            continue
        ns = ""
        if "}" in pm.tag:
            ns = pm.tag.split("}", 1)[0][1:]
        domain = _domain_from_namespace(ns)

        bin_interval = ""
        name = ""
        suspect_status = ""
        elapsed_time = ""

        for child in pm.iter():
            lname = _local_name(child.tag)
            if lname == "bin-interval" and not bin_interval:
                bin_interval = _text(child)
            elif lname == "name" and not name:
                name = _text(child)
            elif lname == "suspect-status" and not suspect_status:
                suspect_status = _text(child)
            elif lname == "elapsed-time" and not elapsed_time:
                elapsed_time = _text(child)

        if token_l and token_l not in (bin_interval or "").lower():
            continue

        instance = _instance_keys_for(pm, parent_map)
        domain_segment = _keyed_segment(domain, instance)

        for mm in pm.iter():
            if _local_name(mm.tag) != "montype-monval":
                continue
            mon_type = ""
            mon_val = ""
            for leaf in mm.iter():
                lname = _local_name(leaf.tag)
                if lname == "mon-type" and not mon_type:
                    mon_type = _text(leaf)
                elif lname == "mon-val" and not mon_val:
                    mon_val = _text(leaf)
            if not mon_type:
                continue
            if mon_val.strip().lower() in _NO_MEASUREMENT_VALUES:
                # No measurement (unlit line / unprovisioned port). Vendors
                # report this as "" or a sentinel like "NA"/"-"; publishing it
                # as a string would create a string-typed field in downstream
                # TSDBs and block later numeric writes for the same field.
                continue
            path = f"{path_prefix}/{domain_segment}/{name}/{mon_type}"
            updates.append(
                {
                    "path": path,
                    "value": _coerce_value(mon_val),
                    "timestamp_ns": ts,
                    "meta": {
                        "domain": domain,
                        "name": name,
                        "mon_type": mon_type,
                        "instance": dict(instance),
                        "bin_interval": bin_interval,
                        "elapsed_time": elapsed_time,
                        "suspect_status": suspect_status,
                    },
                }
            )
    return updates
