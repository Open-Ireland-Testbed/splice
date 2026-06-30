from __future__ import annotations

import json
import time
from urllib import request, error

from splice.trigger_api import TriggerAPIServer


def _post_json(url: str, payload: dict, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(
        url=url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    try:
        with request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_ready(url: str, token: str, timeout_sec: float = 1.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            _post_json(url, {"paths": ["/health"]}, token=token)
            return
        except Exception:
            time.sleep(0.02)
    raise RuntimeError("trigger API did not become ready in time")


def test_trigger_api_auth_and_success():
    called = {}

    def handler(**kwargs):
        called.update(kwargs)
        return {"updates": [], "errors": []}

    srv = TriggerAPIServer("127.0.0.1", 0, "abc123", handler)
    srv.start()
    try:
        port = srv._srv.server_port  # type: ignore[attr-defined]
        base = f"http://127.0.0.1:{port}/trigger"
        _wait_ready(base, token="abc123")

        code, body = _post_json(base, {"paths": ["/x"]}, token=None)
        assert code == 401
        assert body["error"] == "unauthorized"

        code, body = _post_json(
            base,
            {"paths": ["/x"], "publish_kafka": True},
            token="abc123",
        )
        assert code == 200
        assert body["updates"] == []
        assert called["paths"] == ["/x"]
        assert called["source"] == "http"
        assert called["publish_kafka"] is True
    finally:
        srv.stop()


def test_trigger_api_validates_paths():
    srv = TriggerAPIServer("127.0.0.1", 0, "abc123", lambda **_kwargs: {})
    srv.start()
    try:
        port = srv._srv.server_port  # type: ignore[attr-defined]
        base = f"http://127.0.0.1:{port}/trigger"
        _wait_ready(base, token="abc123")

        code, body = _post_json(base, {"paths": []}, token="abc123")
        assert code == 400
        assert "paths must be a non-empty array" in body["error"]
    finally:
        srv.stop()
