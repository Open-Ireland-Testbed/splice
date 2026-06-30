# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Catalog loading service — DB-first with YANG fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from splice.path_index import PathEntry, TreeNode, build_path_entries, build_tree

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceEntry

log = logging.getLogger(__name__)


@dataclass
class CatalogResult:
    """Result of loading a device's catalog."""

    entries: list[PathEntry] = field(default_factory=list)
    tree: TreeNode = field(default_factory=lambda: TreeNode(segment="", full_path=""))
    source: str = "none"
    path_count: int = 0
    error: str | None = None


class CatalogLoader:
    """Load catalog data for a device, trying DB first then YANG fallback."""

    def __init__(self) -> None:
        self._cache: dict[str, CatalogResult] = {}

    def load(self, entry: DeviceEntry, force: bool = False) -> CatalogResult:
        """Load catalog synchronously — call from @work(thread=True)."""
        if not force and entry.name in self._cache:
            return self._cache[entry.name]

        result = None
        if entry.db_url:
            result = self._load_from_db(entry)

        if result is None:
            result = self._load_from_yang(entry)

        if result is None:
            result = CatalogResult()

        self._cache[entry.name] = result
        return result

    def clear(self, device_name: str | None = None) -> None:
        """Clear cached results."""
        if device_name is None:
            self._cache.clear()
        else:
            self._cache.pop(device_name, None)

    def _load_from_db(self, entry: DeviceEntry) -> CatalogResult | None:
        """Try loading catalog from PostgreSQL."""
        try:
            from splice.db.catalog_query import fetch_catalog_map_with_module
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)
                if device is None:
                    return None

                catalog_map = fetch_catalog_map_with_module(session, device.id)
                if not catalog_map:
                    return None

                entries = build_path_entries(catalog_map)
                tree = build_tree(entries)
                return CatalogResult(
                    entries=entries,
                    tree=tree,
                    source="db",
                    path_count=len(entries),
                )
            finally:
                session.close()
        except Exception as exc:
            log.debug("DB catalog load failed for %s: %s", entry.name, exc)
            return None

    def _load_from_yang(self, entry: DeviceEntry) -> CatalogResult | None:
        """Try loading catalog from YANG files on disk."""
        try:
            yang_path = Path(entry.yang_dir)
            if not yang_path.is_dir():
                return None
            yang_files = list(yang_path.glob("*.yang"))
            if not yang_files:
                return None

            from splice.catalog import build_catalog

            catalog_list = build_catalog(entry.yang_dir)
            if not catalog_list:
                return None

            entries = build_path_entries(catalog_list)
            tree = build_tree(entries)
            return CatalogResult(
                entries=entries,
                tree=tree,
                source="yang",
                path_count=len(entries),
            )
        except Exception as exc:
            log.debug("YANG catalog load failed for %s: %s", entry.name, exc)
            return CatalogResult(error=str(exc))
