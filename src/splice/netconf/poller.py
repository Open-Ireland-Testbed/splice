# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Iterable, List
from defusedxml import ElementTree as ET

from splice.netconf.client import CircuitBreakerOpen
from splice.planner import QueryTask, group_tasks_by_interval
from splice.pathing import encode_key_value, normalize_path
from splice.expander import strip_predicates

_log = logging.getLogger(__name__)


class NetconfPoller:
    def __init__(
        self,
        fetch: Callable[[str], str],
        parse: Callable[[str], List[Dict[str, object]]],
        ingest: Callable[[List[Dict[str, object]]], None],
        tasks: Iterable[QueryTask],
        task_throttle_sec: float = 0.05,
        task_failure_limit: int = 0,
    ) -> None:
        self.fetch = fetch
        self.parse = parse
        self.ingest = ingest
        self.tasks = list(tasks)
        self._task_throttle_sec = task_throttle_sec
        # After this many consecutive failures a task is dropped from the
        # rotation for this process's lifetime (0 disables the breaker).
        # Circuit-breaker pauses never count: they are device-wide, not
        # evidence against an individual path.
        self._task_failure_limit = max(0, int(task_failure_limit))
        self._fail_counts: Dict[tuple, int] = {}
        self._disabled: Dict[tuple, str] = {}
        self._lock = threading.Lock()
        self._groups: Dict[int, List[QueryTask]] = {}
        self._next_run: Dict[int, float] = {}
        self._sweep_stats: Dict[int, Dict[str, object]] = {}
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None

    @staticmethod
    def _task_key(task: QueryTask) -> tuple:
        # canonical_path alone is not unique (PM filters share a root path),
        # so identity includes the filter payload.
        return (task.canonical_path, task.filter_xml)

    def _rebuild_groups(self) -> None:
        groups = group_tasks_by_interval(self.tasks)
        now = time.time()
        prev_next_run = dict(self._next_run)
        next_run: Dict[int, float] = {}
        for interval in groups:
            prev = prev_next_run.get(interval)
            if prev is not None and prev > now:
                # Preserve active cooldown windows when tasks are updated.
                next_run[interval] = prev
            else:
                next_run[interval] = now
        self._groups = groups
        self._next_run = next_run

    def _run(self) -> None:
        with self._lock:
            self._rebuild_groups()

        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                groups = dict(self._groups)
                next_run = dict(self._next_run)
            for interval, task_list in groups.items():
                if now < next_run.get(interval, now):
                    continue
                pause_until = None
                sweep_start = time.time()
                ok_count = 0
                fail_count = 0
                skipped_disabled = 0
                first = True
                # Failure increments are buffered per sweep and discarded if
                # the sweep ends in a circuit-breaker pause: during a
                # device-wide outage the first few tasks of every sweep fail
                # "normally" before the breaker trips, and task order is
                # stable — committing those would wrongly disable healthy
                # paths.
                pending_fails: Dict[tuple, str] = {}
                for task in task_list:
                    if self._stop.is_set():
                        break
                    key = self._task_key(task)
                    with self._lock:
                        if key in self._disabled:
                            skipped_disabled += 1
                            continue
                    if not first and self._task_throttle_sec > 0:
                        time.sleep(self._task_throttle_sec)
                    first = False
                    try:
                        xml = self.fetch(task.filter_xml)
                        updates = self.parse(xml)
                        if updates:
                            self.ingest(updates)
                        ok_count += 1
                        pending_fails.pop(key, None)
                        with self._lock:
                            self._fail_counts.pop(key, None)
                    except CircuitBreakerOpen as e:
                        cooldown = max(1, int(e.cooldown_remaining))
                        _log.warning(
                            "circuit breaker open; pausing poller interval=%ss for %ss",
                            interval,
                            cooldown,
                        )
                        pause_until = time.time() + cooldown
                        pending_fails.clear()
                        break
                    except Exception as e:
                        fail_count += 1
                        _log.error(
                            "task failed path=%s error=%s", task.canonical_path, e
                        )
                        if self._task_failure_limit:
                            pending_fails[key] = str(e)[:200]
                        continue
                if pending_fails and self._task_failure_limit:
                    newly_disabled = []
                    with self._lock:
                        for key, err in pending_fails.items():
                            count = self._fail_counts.get(key, 0) + 1
                            if count >= self._task_failure_limit:
                                self._disabled[key] = err
                                self._fail_counts.pop(key, None)
                                newly_disabled.append(key[0])
                            else:
                                self._fail_counts[key] = count
                    for path in newly_disabled:
                        _log.warning(
                            "task disabled after %d consecutive failures path=%s "
                            "(re-enabled on restart or task refresh)",
                            self._task_failure_limit,
                            path,
                        )
                with self._lock:
                    self._sweep_stats[interval] = {
                        "tasks": len(task_list),
                        "ok": ok_count,
                        "fail": fail_count,
                        "disabled": skipped_disabled,
                        "duration_sec": round(time.time() - sweep_start, 3),
                        "completed_at": time.time(),
                        "paused_by_circuit_breaker": pause_until is not None,
                    }
                if pause_until is not None:
                    next_run[interval] = pause_until
                else:
                    next_run[interval] = now + interval
            with self._lock:
                self._next_run.update(next_run)
            self._stop.wait(0.2)

    def start(self) -> None:
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="netconf-poller", daemon=True)
        self._thr.start()

    def update_tasks(self, tasks: Iterable[QueryTask]) -> None:
        with self._lock:
            self.tasks = list(tasks)
            # A refreshed task list is a clean slate: jobs may have been
            # re-enabled or filters rebuilt, so prior failure history is stale.
            current = {self._task_key(t) for t in self.tasks}
            self._fail_counts = {k: v for k, v in self._fail_counts.items() if k in current}
            self._disabled = {}
            self._rebuild_groups()

    def stats(self) -> Dict[str, object]:
        with self._lock:
            disabled = [
                {"path": key[0], "error": err}
                for key, err in sorted(self._disabled.items())
            ]
            return {
                "task_count": len(self.tasks),
                "disabled_count": len(disabled),
                "disabled": disabled[:50],
                "failure_limit": self._task_failure_limit,
                "sweeps": {str(k): dict(v) for k, v in self._sweep_stats.items()},
            }

    def stop(self) -> None:
        self._stop.set()
        if self._thr and self._thr.is_alive():
            self._thr.join(timeout=2)


def _cast_value(value: str, yang_type: str | None):
    if value is None:
        return None
    if yang_type is None:
        return value
    t = yang_type.lower()
    if t in ("int8", "int16", "int32", "int64"):
        try:
            return int(value)
        except Exception:
            return value
    if t in ("uint8", "uint16", "uint32", "uint64"):
        try:
            return int(value)
        except Exception:
            return value
    if t in ("decimal64", "float", "double"):
        try:
            return float(value)
        except Exception:
            return value
    if t == "boolean":
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        return value
    return value


def parse_xml_to_kv(
    xml: str,
    type_map: Dict[str, str] | None = None,
    preserve_namespaces: bool = False,
    list_keys: Dict[str, List[str]] | None = None,
) -> List[Dict[str, object]]:
    """Convert XML into list of {path, value, timestamp_ns}.

    Paths are local-name based for compatibility, with namespace-aware paths
    preserved in meta to avoid collisions when needed.

    When ``list_keys`` (a map of normalized list path -> key leaf names) is
    provided, list elements are emitted with their key predicates inline
    (e.g. ``/interfaces/interface[name=eth0]/state/mtu``). The keys are read
    from each list element's own children during the walk, so a reply
    containing multiple instances of the same list yields distinct, correctly
    attributed paths instead of collapsing onto the last-seen key.
    """
    if not xml:
        return []
    root = ET.fromstring(xml)
    data = None
    for el in root.iter():
        if el.tag.endswith("data"):
            data = el
            break
    if data is None:
        return []

    updates: List[Dict[str, object]] = []
    ts = time.time_ns()

    def split_tag(tag: str) -> tuple[str, str]:
        if "}" in tag:
            ns, local = tag[1:].split("}", 1)
            return local, ns
        return tag, ""

    prefix_map: Dict[str, str] = {}

    def ns_prefix(ns: str) -> str:
        if ns not in prefix_map:
            prefix_map[ns] = f"ns{len(prefix_map) + 1}"
        return prefix_map[ns]

    def keyed_segment(child, plain_seg, ns_seg, parent_plain_path):
        """Return (keyed_local_seg, keyed_ns_seg), adding list-key predicates
        read from ``child``'s own children when this element is a known keyed
        list. Falls back to the bare segments when ``list_keys`` is absent or
        the element is not a keyed list."""
        if not list_keys:
            return plain_seg, ns_seg
        norm_list_path = normalize_path("/" + "/".join(parent_plain_path + [plain_seg]))
        keys = list_keys.get(norm_list_path)
        if not keys:
            return plain_seg, ns_seg
        kv = []
        for k in keys:
            for kc in child:
                kname, _ = split_tag(kc.tag)
                if kname == k and kc.text and kc.text.strip():
                    kv.append(f"{k}={encode_key_value(kc.text.strip())}")
                    break
        if not kv:
            return plain_seg, ns_seg
        pred = "[" + ",".join(kv) + "]"
        return plain_seg + pred, ns_seg + pred

    def walk(el, path, ns_path, keyed_path, keyed_ns_path):
        children = list(el)
        if not children:
            if el.text and el.text.strip():
                raw_path = "/" + "/".join(path)
                norm_path = normalize_path("/" + "/".join(keyed_path))
                ns_full = "/" + "/".join(keyed_ns_path)
                primary_path = ns_full if preserve_namespaces and ns_full != "/" else norm_path
                yang_type = None
                if type_map is not None:
                    base_primary = strip_predicates(primary_path)
                    base_norm = strip_predicates(norm_path)
                    yang_type = (
                        type_map.get(primary_path)
                        or type_map.get(norm_path)
                        or type_map.get(base_primary)
                        or type_map.get(base_norm)
                    )
                meta = {"raw_path": raw_path}
                if ns_full:
                    meta["ns_path"] = ns_full
                    meta["namespace_map"] = dict(prefix_map)
                updates.append(
                    {
                        "path": primary_path,
                        "value": _cast_value(el.text.strip(), yang_type),
                        "timestamp_ns": ts,
                        "meta": meta,
                    }
                )
            return
        for c in children:
            name, ns = split_tag(c.tag)
            ns_seg = f"{ns_prefix(ns)}:{name}" if ns else name
            keyed_seg, keyed_ns = keyed_segment(c, name, ns_seg, path)
            walk(
                c,
                path + [name],
                ns_path + [ns_seg],
                keyed_path + [keyed_seg],
                keyed_ns_path + [keyed_ns],
            )

    walk(data, [], [], [], [])
    return updates
