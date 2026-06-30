# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class QueryTask:
    canonical_path: str
    filter_xml: str
    interval_sec: int


def plan_queries(
    catalog: Dict[str, Dict[str, object]],
    job_intervals: Dict[str, int],
    default_interval_sec: int = 30,
) -> List[QueryTask]:
    """Build query tasks from catalog and job intervals.

    catalog: canonical_path -> {filter_xml: str}
    job_intervals: canonical_path -> interval_sec
    """
    tasks: List[QueryTask] = []
    for path, info in catalog.items():
        interval = int(job_intervals.get(path, default_interval_sec))
        tasks.append(
            QueryTask(
                canonical_path=path,
                filter_xml=str(info.get("filter_xml", "")),
                interval_sec=interval,
            )
        )
    return tasks


def group_tasks_by_interval(tasks: Iterable[QueryTask]) -> Dict[int, List[QueryTask]]:
    grouped: Dict[int, List[QueryTask]] = {}
    for t in tasks:
        grouped.setdefault(t.interval_sec, []).append(t)
    return grouped


def choose_parent_path(path: str) -> Optional[str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) <= 1:
        return None
    return "/" + "/".join(parts[:-1])


def _top_level_path(path: str) -> Optional[str]:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return None
    return "/" + parts[0]


def plan_batched_queries(
    catalog: Dict[str, Dict[str, object]],
    job_intervals: Dict[str, int],
    default_interval_sec: int = 30,
) -> List[QueryTask]:
    """Coalesce queries by top-level container to reduce RPC count.

    When several children of one container carry different job intervals, the
    coalesced parent is polled at the FASTEST (minimum) of them — one task per
    target, not one per (target, interval). Keying by (target, interval) would
    emit duplicate tasks fetching the same subtree at each interval, wasting
    RPCs on session-throttled devices.
    """
    batch: Dict[str, QueryTask] = {}
    for path, info in catalog.items():
        interval = int(job_intervals.get(path, default_interval_sec))
        top = _top_level_path(path)
        target = top if top in catalog else path
        filter_xml = str(catalog.get(target, info).get("filter_xml", ""))
        existing = batch.get(target)
        if existing is None:
            batch[target] = QueryTask(canonical_path=target, filter_xml=filter_xml, interval_sec=interval)
        elif interval < existing.interval_sec:
            batch[target] = QueryTask(canonical_path=target, filter_xml=existing.filter_xml, interval_sec=interval)
    return list(batch.values())


def plan_module_batched_queries(
    catalog: Dict[str, Dict[str, object]],
    job_intervals: Dict[str, int],
    default_interval_sec: int = 30,
) -> List[QueryTask]:
    """Coalesce queries by module and top-level container.

    One task per (module, target), polled at the fastest interval among the
    children that batched into it (see plan_batched_queries).
    """
    batch: Dict[str, QueryTask] = {}
    for path, info in catalog.items():
        interval = int(job_intervals.get(path, default_interval_sec))
        module = info.get("module") or ""
        top = _top_level_path(path)
        target = top if top in catalog else path
        filter_xml = str(catalog.get(target, info).get("filter_xml", ""))
        key = f"{module}:{target}"
        existing = batch.get(key)
        if existing is None:
            batch[key] = QueryTask(canonical_path=target, filter_xml=filter_xml, interval_sec=interval)
        elif interval < existing.interval_sec:
            batch[key] = QueryTask(canonical_path=target, filter_xml=existing.filter_xml, interval_sec=interval)
    return list(batch.values())
