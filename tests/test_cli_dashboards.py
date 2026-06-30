"""Tests for the `dashboards` CLI command and the setup dashboard step."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from splice.cli import _cmd_setup, main


class _DummyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyManager:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close_session(self):
        return None


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_cfg():
    return SimpleNamespace(
        device=SimpleNamespace(host="10.0.0.10", port=830, username="admin", name="roadm-a"),
        db=SimpleNamespace(url="postgresql+psycopg://localhost/test"),
        poller=SimpleNamespace(default_interval_sec=30, rules=[]),
        discovery=SimpleNamespace(
            resolve_all_lists=False,
            list_batch_size=50,
            list_sleep_sec=0.5,
            pm_discovery=False,
        ),
    )


def _make_args(config="devices/roadm-a/config.yaml"):
    return SimpleNamespace(
        interval=None,
        include_config=False,
        skip_migrate=False,
        skip_list_instances=False,
        prune=False,
        force=False,
        config=config,
    )


def _setup_patches(generate_mock_target):
    """Patch the setup pipeline down to fast no-op stubs."""
    return [
        patch("splice.db.session.make_session_factory", return_value=lambda: _DummySession()),
        patch("splice.db.session.make_engine", return_value=object()),
        patch("socket.create_connection", return_value=_DummyConnection()),
        patch("splice.cli.ensure_database", return_value=False),
        patch("splice.cli.run_migrations", return_value=1),
        patch("splice.cli.get_or_create_device", return_value=SimpleNamespace(id=7)),
        patch("splice.cli._make_manager", return_value=_DummyManager()),
        patch("splice.cli._make_manager_fn", return_value=lambda: _DummyManager()),
        patch("splice.cli.run_discovery", return_value=2),
        patch("splice.cli.run_fetch_yang", return_value=3),
        patch("splice.cli.run_build_catalog_with_paths", return_value=(12, ["/a"])),
        patch("splice.cli.fetch_catalog_paths", return_value=["/a"]),
        patch("splice.cli.run_build_aliases", return_value=1),
        patch("splice.cli._init_jobs_curated", return_value="4 jobs"),
        generate_mock_target,
    ]


class TestSetupDashboardStep:
    def test_setup_generates_dashboards_for_current_device(self, capsys):
        generate_patch = patch(
            "splice.grafana_dashboards.generate_dashboards_for_config",
            return_value=["a.json", "b.json"],
        )
        patches = _setup_patches(generate_patch)
        mocks = [p.start() for p in patches]
        try:
            rc = _cmd_setup(_make_args(), _make_cfg())
        finally:
            for p in patches:
                p.stop()

        assert rc == 0
        generate_mock = mocks[-1]
        generate_mock.assert_called_once_with("devices/roadm-a/config.yaml")

        out = capsys.readouterr().out
        assert "[8/8] Generating Grafana dashboards... 2 files" in out
        assert "SETUP COMPLETE" in out

    def test_setup_succeeds_when_dashboard_generation_fails(self, capsys):
        generate_patch = patch(
            "splice.grafana_dashboards.generate_dashboards_for_config",
            side_effect=RuntimeError("boom"),
        )
        patches = _setup_patches(generate_patch)
        for p in patches:
            p.start()
        try:
            rc = _cmd_setup(_make_args(), _make_cfg())
        finally:
            for p in patches:
                p.stop()

        # Dashboard generation is best-effort: the pipeline already
        # succeeded, so setup must still report success.
        assert rc == 0
        out = capsys.readouterr().out
        assert "[8/8] Generating Grafana dashboards... skipped" in out
        assert "Warning: dashboard generation failed: boom" in out
        assert "SETUP COMPLETE" in out


class TestDashboardsCommand:
    def test_dashboards_command_end_to_end(self, tmp_path, capsys):
        device_dir = tmp_path / "devices" / "alpha"
        yang_dir = device_dir / "yang"
        yang_dir.mkdir(parents=True)
        (yang_dir / "t.yang").write_text(
            'module t { namespace "urn:t"; prefix "t"; '
            "container state { config false; "
            "leaf power { type decimal64 { fraction-digits 2; } } } }",
            encoding="utf-8",
        )
        config_path = device_dir / "config.yaml"
        config_path.write_text(
            "device:\n  name: Alpha\n  host: 10.0.0.1\n  port: 830\n  username: admin\n"
            "db:\n  url: postgresql+psycopg://localhost/test\n",
            encoding="utf-8",
        )
        out_dir = tmp_path / "out"

        rc = main([
            "--config", str(config_path),
            "dashboards",
            "--out", str(out_dir),
            "--bucket", "custom",
        ])

        assert rc == 0
        assert (out_dir / "alpha-live.json").is_file()
        assert (out_dir / "alpha-inventory.json").is_file()
        out = capsys.readouterr().out
        assert "alpha-live.json" in out
        live_text = (out_dir / "alpha-live.json").read_text(encoding="utf-8")
        assert 'from(bucket: \\"custom\\")' in live_text

    def test_dashboards_command_missing_config(self, tmp_path, capsys):
        missing = tmp_path / "nope.yaml"
        args = SimpleNamespace(
            config=str(missing), out=str(tmp_path), bucket="gnmi", measurement="telemetry"
        )
        from splice.cli import _cmd_dashboards

        rc = _cmd_dashboards(args)
        assert rc == 1
        assert "Error:" in capsys.readouterr().out
