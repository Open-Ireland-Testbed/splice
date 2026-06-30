# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from splice.netconf.trigger_executor import TriggerValidationError


class _Handler(BaseHTTPRequestHandler):
    def _json_response(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path != "/trigger":
            self._json_response(404, {"error": "not_found"})
            return

        expected = getattr(self.server, "bearer_token", "")  # type: ignore[attr-defined]
        auth = self.headers.get("Authorization", "")
        presented = auth.split(" ", 1)[1].strip() if auth.startswith("Bearer ") else ""
        # Constant-time compare: a plain != leaks token length/prefix via timing.
        if not expected or not hmac.compare_digest(presented, expected):
            self._json_response(401, {"error": "unauthorized"})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0 or content_length > 1_048_576:
            self._json_response(400, {"error": "invalid_content_length"})
            return

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._json_response(400, {"error": "invalid_json"})
            return

        paths = payload.get("paths")
        if not isinstance(paths, list) or not paths:
            self._json_response(400, {"error": "paths must be a non-empty array"})
            return

        publish_kafka = payload.get("publish_kafka")
        auto_onboard = payload.get("auto_onboard")
        interval_sec = payload.get("interval_sec")

        trigger_fn = self.server.trigger_fn  # type: ignore[attr-defined]
        try:
            result = trigger_fn(
                paths=paths,
                publish_kafka=publish_kafka,
                auto_onboard=auto_onboard,
                interval_sec=interval_sec,
                source="http",
            )
        except TriggerValidationError as exc:
            self._json_response(400, {"error": str(exc)})
            return
        except ValueError as exc:
            self._json_response(400, {"error": str(exc)})
            return
        except Exception:
            self._json_response(500, {"error": "trigger_failed"})
            return

        self._json_response(200, result)

    def log_message(self, format, *args):  # noqa: A002
        return


class TriggerAPIServer:
    def __init__(self, bind: str, port: int, bearer_token: str, trigger_fn):
        self._srv = HTTPServer((bind, port), _Handler)
        self._srv.bearer_token = bearer_token  # type: ignore[attr-defined]
        self._srv.trigger_fn = trigger_fn  # type: ignore[attr-defined]
        self._thr = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thr.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thr.join(timeout=2)
