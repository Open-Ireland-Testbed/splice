# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class InMemoryCatalog:
    """In-memory replacement for all DB-backed catalog tables.

    Fields are populated once during ``Device.connect()`` and are
    read-only afterwards, so no locking is needed.
    """

    catalog_map: Dict[str, Dict] = field(default_factory=dict)
    alias_map: Dict[str, str] = field(default_factory=dict)
    job_intervals: Dict[str, int] = field(default_factory=dict)
    type_map: Dict[str, str] = field(default_factory=dict)
    list_keys: Dict[str, List[str]] = field(default_factory=dict)
    description_map: Dict[str, str] = field(default_factory=dict)
    modules: List[Dict] = field(default_factory=list)

    def canonical_paths(self) -> List[str]:
        return list(self.catalog_map.keys())

    def init_default_jobs(self, interval: int) -> None:
        for path in self.catalog_map:
            self.job_intervals.setdefault(path, interval)

    def supported_models(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for m in self.modules:
            out.append({
                "name": str(m.get("name", "")),
                "organization": "",
                "version": str(m.get("revision", "") or ""),
            })
        return out
