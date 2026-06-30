# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generate Grafana dashboards for a device from its YANG catalog.

Produces two dashboards per device:

- ``<slug>-live.json`` — live telemetry panels grouped into metric
  families (power, temperature, quality, ...) inferred from the device's
  YANG catalog. Queries assume Telegraf writes gNMI telemetry to an
  InfluxDB bucket (default ``gnmi``) under one measurement (default
  ``telemetry``), tagging each series with ``device_host``.
- ``<slug>-inventory.json`` — a schema inventory (text panels)
  summarizing modules, top-level subtrees, and candidate metrics.

Dashboards reference InfluxDB through a ``datasource`` template variable
so they import cleanly into any Grafana instance — no hardcoded
datasource UIDs.

The generated dashboards are a starting point: the metric-family
detection is heuristic (keyword matching on YANG paths), so panels may
need per-device curation.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import yaml

from splice.catalog import build_catalog

DEFAULT_OUT_DIR = Path("dashboards")
DEFAULT_BUCKET = "gnmi"
DEFAULT_MEASUREMENT = "telemetry"

# Max catalog rows rendered per module in the inventory dashboard.
_MAX_MODULE_ROWS = 240

_COLORS = [
    "#73BF69",
    "#5794F2",
    "#FF9830",
    "#F2495C",
    "#8F3BB8",
    "#56A64B",
]

# Metric families inferred from YANG path keywords. `patterns` are
# substring-matched against lowercased paths.
CATEGORY_DEFS = [
    {
        "key": "power",
        "title": "Power, Gain, and Loss",
        "description": "Power-related telemetry including optical power, gain, attenuation, and loss.",
        "patterns": ["power", "gain", "loss", "atten", "voa"],
    },
    {
        "key": "temperature",
        "title": "Temperature",
        "description": "Temperature and thermal telemetry.",
        "patterns": ["temp", "temperature", "thermal"],
    },
    {
        "key": "current_voltage",
        "title": "Current, Voltage, and Bias",
        "description": "Electrical telemetry such as current, voltage, and bias values.",
        "patterns": ["current", "volt", "voltage", "bias"],
    },
    {
        "key": "quality",
        "title": "Optical Quality",
        "description": "Quality metrics such as BER, FEC, OSNR, SNR, and Q factor.",
        "patterns": ["ber", "fec", "osnr", "snr", "q-factor", "q_factor", "quality"],
    },
    {
        "key": "traffic",
        "title": "Traffic and Utilization",
        "description": "Traffic counters and utilization metrics.",
        "patterns": ["octet", "packet", "pkts", "bytes", "util", "throughput", "bandwidth", "rate"],
    },
    {
        "key": "errors",
        "title": "Errors and Discards",
        "description": "Error, discard, and drop counters.",
        "patterns": ["error", "discard", "drop"],
    },
    {
        "key": "timing",
        "title": "Frequency and Timing",
        "description": "Frequency, wavelength, offset, delay, and dispersion metrics.",
        "patterns": ["freq", "frequency", "wavelength", "offset", "delay", "latency", "dgd", "dispersion", "chromatic"],
    },
]

# Terms too generic to identify a metric (PM bin structure, aggregates).
_GENERIC_METRIC_TERMS = {
    "avg", "average", "bin", "bin_interval", "count", "current", "elapsed",
    "elapsed_time", "instant", "interval", "max", "mean", "min", "seconds",
    "sum", "time", "total", "value",
}

# Terms that indicate a non-metric leaf (identity/config/status).
_EXCLUDED_METRIC_TERMS = {
    "active", "admin", "admin_status", "capability", "config", "description",
    "enabled", "id", "index", "name", "oper_status", "pm_current_data",
    "state", "status", "type",
}

_NON_NUMERIC_TYPE_TOKENS = (
    "string", "boolean", "enumeration", "identityref", "leafref",
    "instance-identifier", "empty", "binary", "bits", "union", "uri",
    "xpath", "date-and-time", "severity",
)

_NUMERIC_TYPE_TOKENS = (
    "int", "uint", "decimal", "float", "double", "counter", "gauge",
    "power", "current", "voltage", "temperature", "frequency", "ratio",
    "percent", "db", "hz", "watt", "amp", "volt", "time", "seconds",
    "fec", "ber", "snr", "osnr", "q-factor", "q_factor",
)


# ---------------------------------------------------------------------------
# Catalog analysis
# ---------------------------------------------------------------------------

def _strip_ns(segment: str) -> str:
    return segment.split(":", 1)[1] if ":" in segment else segment


def _top_level_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    return "/" + _strip_ns(parts[0])


def _path_parts(path: str) -> list[str]:
    return [_strip_ns(part).replace("-", "_") for part in path.split("/") if part]


def _metric_term_from_path(path: str) -> str | None:
    """Pick the most specific path segment that names the metric."""
    for part in reversed(_path_parts(path)):
        if part in _EXCLUDED_METRIC_TERMS or part in _GENERIC_METRIC_TERMS:
            continue
        if any(token in part for token in ("status", "config", "description")):
            continue
        return part
    return None


def _is_numeric_type(data_type: str | None) -> bool:
    if not data_type:
        return False
    lowered = data_type.lower()
    if any(token in lowered for token in _NON_NUMERIC_TYPE_TOKENS):
        return False
    return any(token in lowered for token in _NUMERIC_TYPE_TOKENS)


def _metric_candidate(path: str, keyword: str, config: bool, data_type: str | None) -> bool:
    """Is this catalog entry a plausible numeric telemetry series?"""
    if config or keyword not in {"leaf", "leaf-list"} or not _is_numeric_type(data_type):
        return False
    lowered = path.lower()
    exclude = ("config", "name", "description", "type", "enabled", "admin", "password", "username")
    return not lowered.endswith(tuple("/" + item for item in exclude))


def _regex_union(patterns: Iterable[str], literal: bool = False, segment: bool = False) -> str:
    """Build a case-insensitive alternation regex for Flux `=~` filters.

    With ``segment=True`` each alternative must span a full /-delimited
    path segment, so e.g. "osc1" does not match "osc10".
    """
    normalized = sorted(set(patterns))
    if literal:
        normalized = [re.escape(pattern) for pattern in normalized]
    if not normalized:
        return "(?i)(?!)"  # matches nothing
    joined = "|".join(normalized)
    if segment:
        return f"(?i)(?:^|\\/)(?:{joined})(?:$|\\/)"
    return f"(?i)(?:{joined})"


# ---------------------------------------------------------------------------
# Markdown / misc helpers
# ---------------------------------------------------------------------------

def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        safe = [cell.replace("\n", " ").replace("|", "\\|") for cell in row]
        out.append("| " + " | ".join(safe) + " |")
    return "\n".join(out)


def _chunked(seq: list, size: int) -> list[list]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")


def _read_device_meta(config_path: Path) -> dict[str, str]:
    """Read device name/host and yang_dir from a bridge config file.

    Deliberately not ``config.load_config``: that resolves credentials
    (and raises when ``device.password_file`` is missing), while
    dashboard generation must work without any.
    """
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    device = raw.get("device") or {}
    return {
        "name": str(device.get("name") or config_path.parent.name),
        "host": str(device.get("host") or ""),
        "yang_dir": str(raw.get("yang_dir", "")),
    }


def _resolve_yang_dir(config_path: Path, configured: str) -> Path:
    """Resolve the device's YANG directory.

    Preference order: an explicit absolute ``yang_dir``; the configured
    (or conventional ``yang``) directory relative to the config file;
    the same relative to the current working directory (how `run`/`setup`
    resolve the default ``./yang``).
    """
    candidates = []
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute():
            return configured_path
        candidates = [config_path.parent / configured_path, Path.cwd() / configured_path]
    else:
        candidates = [config_path.parent / "yang", Path.cwd() / "yang"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


# ---------------------------------------------------------------------------
# Flux queries
# ---------------------------------------------------------------------------

class _QueryBuilder:
    """Builds Flux queries scoped to one device's telemetry."""

    def __init__(self, bucket: str, measurement: str, device_host: str):
        self.bucket = bucket
        self.measurement = measurement
        self.device_host = device_host

    def _base(self) -> list[str]:
        return [
            f'from(bucket: {json.dumps(self.bucket)})',
            "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)",
            f'  |> filter(fn: (r) => r["_measurement"] == {json.dumps(self.measurement)})',
            f'  |> filter(fn: (r) => r["device_host"] == {json.dumps(self.device_host)})',
        ]

    def timeseries(self, regex: str, name: str) -> str:
        return "\n".join(self._base() + [
            f'  |> filter(fn: (r) => r["_field"] =~ /{regex}/)',
            "  |> toFloat()",
            "  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)",
            f'  |> yield(name: {json.dumps(name)})',
        ])

    def latest_values(self, regex: str, limit: int | None = None) -> str:
        lines = self._base() + [
            f'  |> filter(fn: (r) => r["_field"] =~ /{regex}/)',
            "  |> toFloat()",
            '  |> group(columns: ["_field"])',
            "  |> last()",
            "  |> group()",
            '  |> sort(columns: ["_value"], desc: true)',
        ]
        if limit is not None:
            lines.append(f"  |> limit(n: {limit})")
        return "\n".join(lines)

    def summary_stat(self, regex: str) -> str:
        return "\n".join(self._base() + [
            f'  |> filter(fn: (r) => r["_field"] =~ /{regex}/)',
            "  |> toFloat()",
            '  |> group(columns: ["_field"])',
            "  |> last()",
            "  |> group()",
            '  |> mean(column: "_value")',
        ])

    def latest_per_field(self) -> str:
        return "\n".join(self._base() + [
            '  |> keep(columns: ["_time", "_field", "_value", "device", "device_host"])',
            '  |> group(columns: ["_field"])',
            "  |> last()",
            "  |> group()",
            '  |> sort(columns: ["_time"], desc: true)',
        ])


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

# Resolved by the `datasource` dashboard variable — keeps dashboards
# portable across Grafana instances.
_DATASOURCE = {"type": "influxdb", "uid": "${datasource}"}


def _target(query: str) -> list[dict]:
    return [{"datasource": dict(_DATASOURCE), "query": query, "refId": "A"}]


def _text_panel(panel_id: int, title: str, markdown: str, x: int, y: int, w: int, h: int) -> dict:
    return {
        "type": "text",
        "title": title,
        "id": panel_id,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "markdown", "content": markdown},
        "transparent": False,
    }


def _row_panel(panel_id: int, title: str, y: int) -> dict:
    return {
        "type": "row",
        "title": title,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": panel_id,
        "collapsed": False,
        "panels": [],
    }


def _timeseries_panel(
    panel_id: int,
    title: str,
    description: str,
    query: str,
    color: str,
    x: int,
    y: int,
    *,
    w: int = 12,
    h: int = 8,
    unit: str = "short",
) -> dict:
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "datasource": dict(_DATASOURCE),
        "fieldConfig": {
            "defaults": {
                "color": {"fixedColor": color, "mode": "fixed"},
                "custom": {
                    "drawStyle": "line",
                    "lineInterpolation": "smooth",
                    "lineWidth": 2,
                    "fillOpacity": 8,
                    "gradientMode": "opacity",
                    "pointSize": 3,
                    "showPoints": "auto",
                    "spanNulls": True,
                    "axisCenteredZero": False,
                    "scaleDistribution": {"type": "linear"},
                },
                "unit": unit,
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["lastNotNull", "mean", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": _target(query),
    }


def _stat_panel(
    panel_id: int,
    title: str,
    description: str,
    query: str,
    x: int,
    y: int,
    w: int,
    h: int,
    unit: str = "short",
) -> dict:
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "datasource": dict(_DATASOURCE),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "orange", "value": 0},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value_and_name",
        },
        "targets": _target(query),
    }


def _bargauge_panel(
    panel_id: int,
    title: str,
    description: str,
    query: str,
    x: int,
    y: int,
    w: int,
    h: int,
    unit: str = "short",
) -> dict:
    return {
        "type": "bargauge",
        "title": title,
        "description": description,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "datasource": dict(_DATASOURCE),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "continuous-GrYlRd"},
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": False,
            "text": {},
        },
        "targets": _target(query),
    }


def _table_panel(
    panel_id: int,
    title: str,
    description: str,
    query: str,
    x: int,
    y: int,
    w: int,
    h: int,
) -> dict:
    return {
        "type": "table",
        "title": title,
        "description": description,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": panel_id,
        "datasource": dict(_DATASOURCE),
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False},
            },
            "overrides": [],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False},
            "sortBy": [{"displayName": "Time", "desc": True}],
        },
        "targets": _target(query),
    }


def _dashboard(
    title: str,
    uid: str,
    tags: list[str],
    panels: list[dict],
    *,
    refresh: str = "",
    live_now: bool = False,
    graph_tooltip: int = 0,
) -> dict:
    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": graph_tooltip,
        "id": None,
        "links": [],
        "liveNow": live_now,
        "panels": panels,
        "refresh": refresh,
        "schemaVersion": 39,
        "style": "dark",
        "tags": tags,
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "label": "InfluxDB datasource",
                    "type": "datasource",
                    "query": "influxdb",
                    "current": {},
                    "hide": 0,
                    "refresh": 1,
                }
            ]
        },
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": 1,
        "weekStart": "",
    }


# ---------------------------------------------------------------------------
# Dashboard builders
# ---------------------------------------------------------------------------

def _build_inventory_dashboard(device_name: str, yang_dir: Path, catalog: list[dict] | None) -> dict:
    tags = [_slug(device_name), "yang", "inventory"]
    title = f"{device_name} YANG Inventory"
    uid = f"{_slug(device_name)}-inventory"

    if catalog is None:
        message = "\n".join([
            f"# {title}",
            "",
            f"No YANG files were found under `{yang_dir}`.",
            "",
            "Run device discovery/setup first, then regenerate this dashboard.",
        ])
        return _dashboard(title, uid, tags, [_text_panel(1, "Overview", message, 0, 0, 24, 10)])

    catalog = sorted(catalog, key=lambda item: (str(item["module"]), str(item["path"])))

    module_counts = Counter(str(item["module"]) for item in catalog)
    root_counts = Counter(_top_level_path(str(item["path"])) for item in catalog)
    keyword_counts = Counter(str(item["keyword"]) for item in catalog)
    metric_candidates = [
        item for item in catalog
        if _metric_candidate(
            str(item["path"]),
            str(item["keyword"]),
            bool(item["config"]),
            str(item.get("data_type") or ""),
        )
    ]

    overview = "\n".join([
        f"# {title}",
        "",
        f"This dashboard is generated from `{yang_dir}`.",
        "",
        f"- Total catalog entries: **{len(catalog)}**",
        f"- Candidate operational metrics: **{len(metric_candidates)}**",
        f"- Modules: **{len(module_counts)}**",
        f"- Top-level subtrees: **{len(root_counts)}**",
        "",
        "Use this as a schema inventory for Grafana and telemetry planning.",
        "The matching `-live.json` dashboard contains live charts.",
        "",
        "## Node Types",
        "",
        _markdown_table(
            ["Keyword", "Count"],
            [[name, str(count)] for name, count in keyword_counts.most_common()],
        ),
    ])

    module_summary = "\n".join([
        "## Modules",
        "",
        _markdown_table(
            ["Module", "Paths"],
            [[name, str(count)] for name, count in module_counts.most_common()],
        ),
    ])

    root_summary = "\n".join([
        "## Top-Level Subtrees",
        "",
        _markdown_table(
            ["Subtree", "Paths"],
            [[name, str(count)] for name, count in root_counts.most_common()],
        ),
    ])

    candidate_rows = [
        [str(item["module"]), str(item["path"]), str(item.get("data_type") or "")]
        for item in metric_candidates[:150]
    ]
    candidate_summary = "\n".join([
        "## Candidate Operational Metrics",
        "",
        "These are non-config leaf and leaf-list nodes that are plausible Grafana series candidates.",
        f"Showing the first **{len(candidate_rows)}** of **{len(metric_candidates)}**.",
        "",
        _markdown_table(["Module", "Path", "Type"], candidate_rows),
    ])

    panels = []
    panel_id = 1
    y = 0
    panels.append(_text_panel(panel_id, "Overview", overview, 0, y, 12, 12))
    panel_id += 1
    panels.append(_text_panel(panel_id, "Module Summary", module_summary, 12, y, 12, 12))
    panel_id += 1
    y += 12
    panels.append(_text_panel(panel_id, "Subtree Summary", root_summary, 0, y, 12, 12))
    panel_id += 1
    panels.append(_text_panel(panel_id, "Metric Candidates", candidate_summary, 12, y, 12, 12))
    panel_id += 1
    y += 12

    module_groups: dict[str, list[dict]] = defaultdict(list)
    for item in catalog:
        module_groups[str(item["module"])].append(item)

    for module_name, items in sorted(module_groups.items()):
        rows = [
            [
                str(item["path"]),
                str(item["keyword"]),
                "yes" if bool(item["config"]) else "no",
                str(item.get("data_type") or ""),
                ",".join(item.get("list_keys") or []),
            ]
            for item in items
        ]
        # Cap per-module tables: large devices have tens of thousands of
        # paths and the resulting multi-MB dashboard can exceed Grafana
        # import limits. The full catalog is available via `export`.
        truncated_from = None
        if len(rows) > _MAX_MODULE_ROWS:
            truncated_from = len(rows)
            rows = rows[:_MAX_MODULE_ROWS]
        chunks = _chunked(rows, 60)
        for idx, chunk in enumerate(chunks, start=1):
            panel_title = module_name if len(chunks) == 1 else f"{module_name} ({idx}/{len(chunks)})"
            header_lines = [f"## {module_name}", ""]
            if truncated_from is not None and idx == len(chunks):
                header_lines = [
                    f"## {module_name}",
                    "",
                    f"Showing the first **{len(rows)}** of **{truncated_from}** paths. "
                    "Export the full catalog with `splice export --what catalog`.",
                    "",
                ]
            markdown = "\n".join(header_lines + [
                _markdown_table(["Path", "Keyword", "Config", "Type", "List Keys"], chunk),
            ])
            x = 0 if idx % 2 == 1 else 12
            if x == 0 and idx > 1:
                y += 14
            panels.append(_text_panel(panel_id, panel_title, markdown, x, y, 12, 14))
            panel_id += 1
        y += 14

    return _dashboard(title, uid, tags, panels)


def _categorize_metrics(metric_candidates: list[dict]) -> list[tuple[int, dict, list[str]]]:
    """Group metric candidates into (hit count, category, ranked terms)."""
    category_hits: list[tuple[int, dict, list[str]]] = []
    for category in CATEGORY_DEFS:
        matched_terms: list[str] = []
        for item in metric_candidates:
            lowered = str(item["path"]).lower()
            if any(pattern in lowered for pattern in category["patterns"]):
                term = _metric_term_from_path(str(item["path"]))
                if term:
                    matched_terms.append(term)
        if matched_terms:
            term_counts = Counter(matched_terms)
            # Most frequent first; ties broken by shorter, then alphabetical
            ranked = [
                name for name, _count in sorted(
                    term_counts.items(),
                    key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
                )
            ]
            category_hits.append((len(matched_terms), category, ranked[:10]))
    category_hits.sort(key=lambda hit: (-hit[0], hit[1]["title"]))
    return category_hits


def _build_live_dashboard(
    device_name: str,
    device_host: str,
    yang_dir: Path,
    catalog: list[dict] | None,
    queries: _QueryBuilder,
) -> dict:
    tags = [_slug(device_name), "live", "telemetry"]
    title = f"{device_name} Live Telemetry"
    uid = f"{_slug(device_name)}-live"

    catalog = catalog or []
    metric_candidates = [
        item for item in catalog
        if _metric_candidate(
            str(item["path"]),
            str(item["keyword"]),
            bool(item["config"]),
            str(item.get("data_type") or ""),
        )
    ]
    category_hits = _categorize_metrics(metric_candidates)

    panels = []
    panel_id = 1
    y = 0
    panels.append(_row_panel(panel_id, f"{device_name} Live Telemetry", y))
    panel_id += 1
    y += 1

    summary_lines = [
        f"# {title}",
        "",
        f"- Device host filter: **{device_host or 'set your Telegraf device_host tag'}**",
        f"- YANG source: `{yang_dir}`",
        f"- Candidate live metrics found in YANG: **{len(metric_candidates)}**",
        "",
        f"This dashboard assumes Telegraf writes to InfluxDB bucket `{queries.bucket}`,",
        f"measurement `{queries.measurement}`, and tags each series with `device_host`.",
    ]
    if not category_hits:
        summary_lines.extend([
            "",
            "No strong live-metric categories were inferred from the current YANG files.",
            "The inventory dashboard was still generated.",
        ])
        panels.append(_text_panel(panel_id, "Overview", "\n".join(summary_lines), 0, y, 24, 10))
        panel_id += 1
        y += 10
    else:
        summary_lines.extend(["", "Detected metric families:", ""])
        summary_lines.extend(
            f"- {category['title']}: **{hits}** numeric YANG candidates"
            for hits, category, _terms in category_hits[:6]
        )
        panels.append(_text_panel(panel_id, "Overview", "\n".join(summary_lines), 0, y, 24, 7))
        panel_id += 1
        y += 7

    panels.append(_row_panel(panel_id, "Latest Values", y))
    panel_id += 1
    y += 1
    panels.append(
        _table_panel(
            panel_id,
            "Latest Telemetry Values",
            "Most recent value per telemetry field for this device.",
            queries.latest_per_field(),
            0, y, 24, 10,
        )
    )
    panel_id += 1
    y += 10

    if category_hits:
        panels.append(_row_panel(panel_id, "Metrics", y))
        panel_id += 1
        y += 1

        summary_count = min(6, len(category_hits))
        for idx, (hits, category, metric_terms) in enumerate(category_hits[:summary_count]):
            regex = _regex_union(metric_terms[:6], literal=True, segment=True)
            panels.append(
                _stat_panel(
                    panel_id,
                    category["title"],
                    f"Average of the latest values across the strongest "
                    f"{min(6, len(metric_terms))} derived telemetry fields. "
                    f"Matched {hits} numeric candidates.",
                    queries.summary_stat(regex),
                    idx * (24 // summary_count),
                    y,
                    24 // summary_count,
                    5,
                )
            )
            panel_id += 1
        y += 5

        for idx, (hits, category, metric_terms) in enumerate(category_hits[:6]):
            color = _COLORS[idx % len(_COLORS)]
            trend_terms = metric_terms[:4]
            detail_terms = metric_terms[:8]
            trend_regex = _regex_union(trend_terms, literal=True, segment=True)
            detail_regex = _regex_union(detail_terms, literal=True, segment=True)

            panels.append(_row_panel(panel_id, category["title"], y))
            panel_id += 1
            y += 1

            panels.append(
                _timeseries_panel(
                    panel_id,
                    f"{category['title']} Trends",
                    f"Trend view for the top {len(trend_terms)} derived telemetry "
                    f"fields in this family. Narrowed from {hits} candidate "
                    f"matches to reduce overlap.",
                    queries.timeseries(trend_regex, _slug(category["title"])),
                    color,
                    0, y,
                )
            )
            panel_id += 1

            panels.append(
                _bargauge_panel(
                    panel_id,
                    f"{category['title']} Latest Values",
                    f"Latest values for the top {len(detail_terms)} derived "
                    f"telemetry fields in this family.",
                    queries.latest_values(detail_regex, limit=8),
                    12, y, 12, 8,
                )
            )
            panel_id += 1
            y += 8

            panels.append(
                _table_panel(
                    panel_id,
                    f"{category['title']} Detail",
                    f"Latest numeric values in this family, matched from "
                    f"{len(detail_terms)} derived telemetry terms.",
                    queries.latest_values(detail_regex, limit=12),
                    0, y, 24, 8,
                )
            )
            panel_id += 1
            y += 8

    return _dashboard(
        title, uid, tags, panels,
        refresh="10s", live_now=True, graph_tooltip=2,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_dashboards_for_config(
    config_path: str | Path,
    *,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    bucket: str = DEFAULT_BUCKET,
    measurement: str = DEFAULT_MEASUREMENT,
) -> list[Path]:
    """Generate live + inventory dashboards for one device config.

    Returns the list of written file paths.
    """
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Device config not found: {config_path}")

    meta = _read_device_meta(config_path)
    device_name = meta["name"]
    yang_dir = _resolve_yang_dir(config_path, meta["yang_dir"])
    queries = _QueryBuilder(bucket=bucket, measurement=measurement, device_host=meta["host"])

    # Parse YANG once and share the catalog between both dashboards —
    # build_catalog over a large device takes seconds.
    catalog: list[dict] | None = None
    if yang_dir.is_dir() and any(yang_dir.iterdir()):
        catalog = build_catalog(str(yang_dir), include_config=True)

    live = _build_live_dashboard(device_name, meta["host"], yang_dir, catalog, queries)
    inventory = _build_inventory_dashboard(device_name, yang_dir, catalog)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(device_name)

    live_path = out_dir / f"{slug}-live.json"
    inventory_path = out_dir / f"{slug}-inventory.json"
    live_path.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return [live_path, inventory_path]
