# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from typing import Dict, List


def apply_rules(paths: List[str], rules: List[dict]) -> Dict[str, int]:
    """Build per-path intervals using regex rules.

    rules: [{pattern: str, interval: int}]
    """
    out: Dict[str, int] = {}
    for r in rules:
        pattern = r.get("pattern")
        interval = r.get("interval")
        if not pattern or interval is None:
            continue
        rx = re.compile(pattern)
        for p in paths:
            if rx.search(p):
                out[p] = int(interval)
    return out


def curate_jobs(
    paths: List[str],
    include: List[dict | str] | None,
    default_interval_sec: int,
) -> Dict[str, int]:
    """Select catalog paths matching curated metric patterns.

    Patterns match case-insensitively against BOTH the module-qualified path
    (/p1:components/p1:component) and its prefix-stripped form
    (/components/component) — anchored patterns like "/edfas" would otherwise
    silently miss every qualified path. The first matching pattern's interval
    wins, so order include entries most-specific first.
    """
    from splice.pathing import normalize_path

    out: Dict[str, int] = {}
    for item in include or []:
        if isinstance(item, str):
            item = {"pattern": item}
        pattern = item.get("pattern")
        if not pattern:
            continue
        interval = int(item.get("interval_sec", default_interval_sec))
        rx = re.compile(pattern, re.IGNORECASE) if item.get("regex") else None
        needle = str(pattern).lower()
        for p in paths:
            forms = (p, normalize_path(p))
            if rx is not None:
                hit = any(rx.search(f) for f in forms)
            else:
                hit = any(needle in f.lower() for f in forms)
            if hit:
                out.setdefault(p, interval)
    return out
