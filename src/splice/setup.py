# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class SetupReport:
    """Report generated after setup phase completes."""

    device_name: str
    device_host: str
    started_at: str
    completed_at: Optional[str] = None
    duration_sec: float = 0.0

    # Discovery stats
    list_paths_attempted: int = 0
    list_paths_succeeded: int = 0
    list_paths_empty: int = 0
    list_paths_failed: int = 0
    total_instances_discovered: int = 0
    discovery_results: List[Dict[str, Any]] = field(default_factory=list)
    top_errors: List[Dict[str, Any]] = field(default_factory=list)
    discovery_source: str = "none"  # "fresh", "cached", "none"

    # Discovered lists with sample keys (top successful discoveries)
    discovered_lists_top: List[Dict[str, Any]] = field(default_factory=list)

    # Expansion stats
    base_paths_expanded: int = 0
    base_paths_skipped: int = 0
    total_expanded_paths: int = 0
    skipped_paths: List[Dict[str, Any]] = field(default_factory=list)
    biggest_expansions: List[Dict[str, Any]] = field(default_factory=list)

    # Polling info
    polled_base_paths: List[str] = field(default_factory=list)
    polled_expanded_paths_sample: List[str] = field(default_factory=list)

    # Final
    polling_tasks: int = 0
    ready: bool = False


def write_setup_report(report: SetupReport, path: str) -> None:
    """Write setup report to JSON file."""
    data = asdict(report)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def create_setup_report(
    device_name: str,
    device_host: str,
    started_at: datetime,
    discovery_results: List[Dict[str, Any]],
    skipped_paths: List[Dict[str, Any]],
    total_expanded_paths: int,
    polling_tasks: int,
    base_paths_expanded: int = 0,
    discovery_source: str = "none",
    cached_instance_count: int = 0,
    discovered_lists_top: Optional[List[Dict[str, Any]]] = None,
    polled_base_paths: Optional[List[str]] = None,
    polled_expanded_paths_sample: Optional[List[str]] = None,
) -> SetupReport:
    """Create a SetupReport from discovery and expansion results."""
    completed_at = datetime.now()
    duration_sec = (completed_at - started_at).total_seconds()

    # Compute discovery stats
    list_paths_attempted = len(discovery_results)
    list_paths_succeeded = sum(1 for r in discovery_results if r.get("status") == "success")
    list_paths_empty = sum(1 for r in discovery_results if r.get("status") == "empty")
    list_paths_failed = sum(1 for r in discovery_results if r.get("status") == "failed")
    total_instances = sum(r.get("instance_count", 0) for r in discovery_results)

    # If using cached instances, report that count
    if discovery_source == "cached" and total_instances == 0:
        total_instances = cached_instance_count

    # Collect top errors
    errors = [r for r in discovery_results if r.get("rpc_error")]
    top_errors = errors[:10]

    # Compute expansion stats - use provided base_paths_expanded if given
    base_paths_skipped = len(skipped_paths)
    biggest_expansions: List[Dict[str, Any]] = []

    # Build biggest_expansions from discovery_results if available
    for r in discovery_results:
        if r.get("status") == "success" and r.get("instance_count", 0) > 0:
            biggest_expansions.append({
                "path": r.get("list_path"),
                "instances": r.get("instance_count", 0),
            })

    # Sort biggest expansions by instance count
    biggest_expansions.sort(key=lambda x: x.get("instances", 0), reverse=True)
    biggest_expansions = biggest_expansions[:10]

    return SetupReport(
        device_name=device_name,
        device_host=device_host,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        duration_sec=duration_sec,
        list_paths_attempted=list_paths_attempted,
        list_paths_succeeded=list_paths_succeeded,
        list_paths_empty=list_paths_empty,
        list_paths_failed=list_paths_failed,
        total_instances_discovered=total_instances,
        discovery_results=discovery_results,
        top_errors=top_errors,
        discovery_source=discovery_source,
        discovered_lists_top=discovered_lists_top or [],
        base_paths_expanded=base_paths_expanded,
        base_paths_skipped=base_paths_skipped,
        total_expanded_paths=total_expanded_paths,
        skipped_paths=skipped_paths,
        biggest_expansions=biggest_expansions,
        polled_base_paths=polled_base_paths or [],
        polled_expanded_paths_sample=polled_expanded_paths_sample or [],
        polling_tasks=polling_tasks,
        ready=True,
    )
