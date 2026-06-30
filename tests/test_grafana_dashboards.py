"""Tests for the Grafana dashboard generator."""

from __future__ import annotations

import json
import re

import pytest

from splice.grafana_dashboards import (
    _read_device_meta,
    _regex_union,
    _resolve_yang_dir,
    _slug,
    generate_dashboards_for_config,
)

OPTICS_YANG = """
module optics-test {
  namespace "urn:test:optics";
  prefix "ot";
  container optics {
    container state {
      config false;
      leaf input-power { type decimal64 { fraction-digits 2; } }
      leaf output-power { type decimal64 { fraction-digits 2; } }
      leaf module-temperature { type decimal64 { fraction-digits 2; } }
      leaf laser-bias-current { type decimal64 { fraction-digits 2; } }
      leaf pre-fec-ber { type decimal64 { fraction-digits 18; } }
      leaf frequency { type uint64; }
      leaf description { type string; }
    }
  }
}
"""


def _write_device(tmp_path, dirname, name, host, *, with_yang=True):
    device_dir = tmp_path / "devices" / dirname
    device_dir.mkdir(parents=True)
    if with_yang:
        yang_dir = device_dir / "yang"
        yang_dir.mkdir()
        (yang_dir / "optics-test.yang").write_text(OPTICS_YANG, encoding="utf-8")
    config_path = device_dir / "config.yaml"
    config_path.write_text(
        "\n".join([
            "device:",
            f"  name: {name}",
            f"  host: {host}",
            "  port: 830",
            "  username: admin",
            "db:",
            "  url: postgresql+psycopg://localhost/test",
        ]) + "\n",
        encoding="utf-8",
    )
    return config_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestRegexUnion:
    def test_segment_boundaries(self):
        regex = _regex_union(["osc1", "input-power"], literal=True, segment=True)
        assert re.search(regex, "optics/osc1/power") is not None
        assert re.search(regex, "optics/input-power") is not None
        # must not match partial segments
        assert re.search(regex, "optics/osc10/power") is None

    def test_empty_patterns_match_nothing(self):
        regex = _regex_union([], literal=True, segment=True)
        assert re.search(regex, "optics/osc1/power") is None

    def test_literal_escapes_metacharacters(self):
        regex = _regex_union(["a.b"], literal=True)
        assert re.search(regex, "x/a.b/y") is not None
        assert re.search(regex, "x/aXb/y") is None


class TestSlugAndMeta:
    def test_slug(self):
        assert _slug("Alpha Device") == "alpha-device"
        assert _slug("ROADM_11") == "roadm-11"

    def test_read_device_meta_defaults_name_to_dirname(self, tmp_path):
        config_path = _write_device(tmp_path, "alpha", "", "10.0.0.1")
        # blank name: fall back to directory name
        config_path.write_text("device:\n  host: 10.0.0.1\n", encoding="utf-8")
        meta = _read_device_meta(config_path)
        assert meta["name"] == "alpha"
        assert meta["host"] == "10.0.0.1"

    def test_resolve_yang_dir_prefers_config_relative(self, tmp_path):
        config_path = _write_device(tmp_path, "alpha", "Alpha", "10.0.0.1")
        resolved = _resolve_yang_dir(config_path, "")
        assert resolved == config_path.parent / "yang"

    def test_resolve_yang_dir_absolute_wins(self, tmp_path):
        config_path = _write_device(tmp_path, "alpha", "Alpha", "10.0.0.1")
        absolute = tmp_path / "elsewhere"
        resolved = _resolve_yang_dir(config_path, str(absolute))
        assert resolved == absolute


# ---------------------------------------------------------------------------
# End-to-end generation
# ---------------------------------------------------------------------------

class TestGenerateDashboards:
    def test_writes_only_selected_device(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        _write_device(tmp_path, "beta", "Beta Device", "10.0.0.2")

        written = generate_dashboards_for_config(target, out_dir=out_dir)

        assert [p.name for p in written] == [
            "alpha-device-live.json",
            "alpha-device-inventory.json",
        ]
        assert all(p.is_file() for p in written)
        assert not (out_dir / "beta-device-live.json").exists()

    def test_live_dashboard_structure(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        generate_dashboards_for_config(target, out_dir=out_dir)

        live = json.loads((out_dir / "alpha-device-live.json").read_text(encoding="utf-8"))
        assert live["title"] == "Alpha Device Live Telemetry"
        assert live["uid"] == "alpha-device-live"
        assert live["id"] is None  # portable import

        panel_types = [p["type"] for p in live["panels"]]
        titles = [p.get("title", "") for p in live["panels"]]
        assert "stat" in panel_types
        assert "bargauge" in panel_types
        assert "timeseries" in panel_types
        assert panel_types.count("table") >= 2
        assert any(t.endswith("Trends") for t in titles)
        assert any(t.endswith("Latest Values") for t in titles)
        assert any(t.endswith("Detail") for t in titles)

    def test_category_detection_from_yang(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        generate_dashboards_for_config(target, out_dir=out_dir)

        live = (out_dir / "alpha-device-live.json").read_text(encoding="utf-8")
        # YANG fixture has power, temperature, bias-current, BER, frequency leaves
        assert "Power, Gain, and Loss" in live
        assert "Temperature" in live
        assert "Optical Quality" in live
        # string leaf must not become a metric
        assert "/description" not in live

    def test_datasource_is_template_variable(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        generate_dashboards_for_config(target, out_dir=out_dir)

        live = json.loads((out_dir / "alpha-device-live.json").read_text(encoding="utf-8"))
        variables = live["templating"]["list"]
        assert any(v["type"] == "datasource" for v in variables)
        for panel in live["panels"]:
            if "datasource" in panel:
                assert panel["datasource"]["uid"] == "${datasource}"

    def test_queries_use_custom_bucket_and_measurement(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        generate_dashboards_for_config(
            target, out_dir=out_dir, bucket="mybucket", measurement="mymeas"
        )

        live = (out_dir / "alpha-device-live.json").read_text(encoding="utf-8")
        assert 'from(bucket: \\"mybucket\\")' in live
        assert '\\"_measurement\\"] == \\"mymeas\\"' in live
        assert '"gnmi"' not in live

    def test_queries_filter_on_device_host(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.20.30.40")
        generate_dashboards_for_config(target, out_dir=out_dir)

        live = (out_dir / "alpha-device-live.json").read_text(encoding="utf-8")
        assert "10.20.30.40" in live

    def test_inventory_dashboard_lists_modules(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1")
        generate_dashboards_for_config(target, out_dir=out_dir)

        inventory = json.loads(
            (out_dir / "alpha-device-inventory.json").read_text(encoding="utf-8")
        )
        assert inventory["title"] == "Alpha Device YANG Inventory"
        content = json.dumps(inventory)
        assert "optics-test" in content
        assert "input-power" in content

    def test_missing_yang_dir_still_generates(self, tmp_path):
        out_dir = tmp_path / "out"
        target = _write_device(tmp_path, "alpha", "Alpha Device", "10.0.0.1", with_yang=False)

        written = generate_dashboards_for_config(target, out_dir=out_dir)

        assert len(written) == 2
        inventory = (out_dir / "alpha-device-inventory.json").read_text(encoding="utf-8")
        assert "No YANG files were found" in inventory

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_dashboards_for_config(tmp_path / "nope.yaml", out_dir=tmp_path)
