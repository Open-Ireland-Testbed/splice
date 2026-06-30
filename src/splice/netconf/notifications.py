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
from typing import Callable


class NotificationListener:
    def __init__(self, manager, parse, ingest, poll_sec: int = 1):
        self.manager = manager
        self.parse = parse
        self.ingest = ingest
        self.poll_sec = poll_sec
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None

    def _run(self):
        while not self._stop.is_set():
            try:
                n = self.manager.take_notification(timeout=self.poll_sec)
                if n is None:
                    continue
                xml = getattr(n, "notification_xml", "") or ""
                updates = self.parse(xml)
                if updates:
                    self.ingest(updates)
            except Exception:
                time.sleep(self.poll_sec)

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="netconf-notify", daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        if self._thr and self._thr.is_alive():
            self._thr.join(timeout=self.poll_sec + 1)
