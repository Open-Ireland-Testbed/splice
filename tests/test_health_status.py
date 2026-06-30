"""Coverage for the /health endpoint payload wiring and the `status` command."""

import json
import urllib.request
from types import SimpleNamespace

from splice.health import HealthServer


def _poller_stats():
    return {
        "task_count": 21,
        "disabled_count": 1,
        "disabled": [{"path": "/bad", "error": "Not found node"}],
        "failure_limit": 5,
        "sweeps": {
            "5": {
                "tasks": 21,
                "ok": 20,
                "fail": 1,
                "disabled": 0,
                "duration_sec": 50.3,
                "completed_at": 1718100000.0,
                "paused_by_circuit_breaker": False,
            }
        },
    }


def _start_health_server(ok=True, payload=None):
    def health_fn():
        return ok, payload or {}

    srv = HealthServer("127.0.0.1", 0, health_fn)
    port = srv._srv.server_address[1]
    srv.start()
    return srv, port


def test_health_endpoint_serves_poller_stats():
    payload = {"db": True, "netconf": True, "gnmi": True, "poller": _poller_stats()}
    srv, port = _start_health_server(payload=payload)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["poller"]["task_count"] == 21
        assert body["poller"]["sweeps"]["5"]["ok"] == 20
        assert body["poller"]["disabled"][0]["path"] == "/bad"
    finally:
        srv.stop()


def test_health_endpoint_degraded_returns_503_with_body():
    payload = {"db": False, "netconf": True, "gnmi": True}
    srv, port = _start_health_server(ok=False, payload=payload)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert json.loads(exc.read().decode("utf-8"))["db"] is False
    finally:
        srv.stop()


def _status_args(json_out=False):
    return SimpleNamespace(json=json_out, config=None)


def _status_cfg(port, enabled=True, bind="127.0.0.1"):
    return SimpleNamespace(health=SimpleNamespace(enabled=enabled, bind=bind, port=port))


def test_cmd_status_renders_poller_stats(capsys):
    from splice.cli import _cmd_status

    payload = {
        "db": True,
        "netconf": True,
        "gnmi": True,
        "netconf_status": {"circuit_breaker_open": False},
        "poller": _poller_stats(),
    }
    srv, port = _start_health_server(payload=payload)
    try:
        rc = _cmd_status(_status_args(), _status_cfg(port), session=None, dev=None)
    finally:
        srv.stop()
    out = capsys.readouterr().out
    assert rc == 0
    assert "db: OK" in out
    assert "tasks=21" in out
    assert "disabled: /bad" in out
    assert "50.3" in out  # sweep duration rendered


def test_cmd_status_degraded_payload_still_renders(capsys):
    from splice.cli import _cmd_status

    payload = {"db": False, "netconf": True, "gnmi": True}
    srv, port = _start_health_server(ok=False, payload=payload)
    try:
        rc = _cmd_status(_status_args(), _status_cfg(port), session=None, dev=None)
    finally:
        srv.stop()
    out = capsys.readouterr().out
    assert rc == 0
    assert "db: DEGRADED" in out


def test_cmd_status_json_passthrough(capsys):
    from splice.cli import _cmd_status

    payload = {"db": True, "netconf": True, "gnmi": True, "poller": _poller_stats()}
    srv, port = _start_health_server(payload=payload)
    try:
        rc = _cmd_status(_status_args(json_out=True), _status_cfg(port), session=None, dev=None)
    finally:
        srv.stop()
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["poller"]["failure_limit"] == 5


def test_cmd_status_no_device_row_fallback(capsys):
    from splice.cli import _cmd_status

    # health disabled -> DB fallback; dev=None must not crash or create rows
    cfg = _status_cfg(port=1, enabled=False)
    rc = _cmd_status(_status_args(), cfg, session=None, dev=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "no device row" in out
