# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        status, payload = self.server.health_fn()  # type: ignore[attr-defined]
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200 if status else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


class HealthServer:
    def __init__(self, bind: str, port: int, health_fn: Callable[[], tuple[bool, Dict]]):
        self._srv = HTTPServer((bind, port), _Handler)
        self._srv.health_fn = health_fn  # type: ignore[attr-defined]
        self._thr = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()
        self._thr.join(timeout=2)
