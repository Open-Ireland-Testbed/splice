# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import threading
import time
from typing import Dict, Iterable, List

from splice.pathing import is_path_prefix, normalize_path


class MemoryCache:
    def __init__(self, ttl_sec: int = 60):
        self._ttl = ttl_sec
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._store: Dict[str, dict] = {}
        self._version = 0

    def ingest(self, updates: List[dict]) -> None:
        now = time.time()
        changed = False
        with self._lock:
            for u in updates:
                path = u.get("path")
                if not path:
                    continue
                self._store[path] = {**u, "_ts": now}
                changed = True
            if changed:
                self._version += 1
                self._cond.notify_all()

    def snapshot(self) -> List[dict]:
        now = time.time()
        out: List[dict] = []
        with self._lock:
            for p, v in list(self._store.items()):
                if self._ttl and (now - v.get("_ts", now)) > self._ttl:
                    self._store.pop(p, None)
                    continue
                out.append({k: v[k] for k in v if k != "_ts"})
        return out

    def match_prefix(self, prefix: str) -> List[dict]:
        now = time.time()
        out: List[dict] = []
        # Normalize both sides (strip YANG module prefixes) so a query in
        # alias/canonical form matches stored namespace-stripped paths — the
        # same matching the CLI stack's CacheView uses. Without this the API
        # stack's gNMI server returns nothing for any prefixed/aliased query.
        prefix_norm = normalize_path(prefix)
        with self._lock:
            for p, v in list(self._store.items()):
                if self._ttl and (now - v.get("_ts", now)) > self._ttl:
                    self._store.pop(p, None)
                    continue
                if is_path_prefix(normalize_path(p), prefix_norm):
                    out.append({k: v[k] for k in v if k != "_ts"})
        return out

    def all_updates(self) -> List[dict]:
        return self.snapshot()

    def get_version(self) -> int:
        with self._lock:
            return self._version

    def wait_for_update(self, last_version: int, timeout: float) -> int:
        with self._lock:
            if self._version != last_version:
                return self._version
            self._cond.wait(timeout=timeout)
            return self._version

    def evict_paths(self, paths: Iterable[str], include_descendants: bool = True) -> int:
        targets = [normalize_path(p) for p in paths if p]
        if not targets:
            return 0
        removed = 0
        with self._lock:
            for path in list(self._store.keys()):
                path_norm = normalize_path(path)
                for target in targets:
                    if path_norm == target or (
                        include_descendants and path_norm.startswith(target + "/")
                    ):
                        self._store.pop(path, None)
                        removed += 1
                        break
            if removed > 0:
                self._version += 1
                self._cond.notify_all()
        return removed
