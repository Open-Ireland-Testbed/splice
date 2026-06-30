# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterator, List

from splice.api._errors import SubscriptionError
from splice.cache_store import MemoryCache
from splice.netconf.poller import NetconfPoller
from splice.planner import QueryTask


class Subscription:
    """Iterator over NETCONF polling updates.

    Each ``Subscription`` owns a dedicated poller thread and a private
    ``MemoryCache``.  Updates are also ingested into the device's shared
    cache so that ``device.cached_data()`` stays current.

    Usage::

        sub = device.subscribe("/interfaces/interface", interval=10)
        for update in sub:
            print(update)
        sub.cancel()

    Also supports context-manager usage::

        with device.subscribe("/interfaces/interface") as sub:
            for update in sub:
                ...
    """

    def __init__(
        self,
        *,
        fetch,
        parse,
        tasks: List[QueryTask],
        shared_cache: MemoryCache,
        task_throttle_sec: float = 0.05,
    ) -> None:
        self._shared_cache = shared_cache
        self._local_cache = MemoryCache(ttl_sec=0)  # no TTL expiry for subscription
        self._last_version = self._local_cache.get_version()
        self._cancelled = False

        def _ingest(updates: List[Dict]) -> None:
            self._local_cache.ingest(updates)
            self._shared_cache.ingest(updates)

        try:
            self._poller = NetconfPoller(
                fetch=fetch,
                parse=parse,
                ingest=_ingest,
                tasks=tasks,
                task_throttle_sec=task_throttle_sec,
            )
            self._poller.start()
        except Exception as exc:
            raise SubscriptionError(f"failed to start subscription poller: {exc}") from exc

    def __iter__(self) -> Iterator[List[Dict]]:
        return self

    def __next__(self) -> List[Dict]:
        if self._cancelled:
            raise StopIteration
        while not self._cancelled:
            new_version = self._local_cache.wait_for_update(
                self._last_version, timeout=1.0
            )
            if new_version != self._last_version:
                self._last_version = new_version
                return self._local_cache.snapshot()
        raise StopIteration

    def cancel(self) -> None:
        self._cancelled = True
        self._poller.stop()

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *args) -> bool:
        self.cancel()
        return False
