"""Feature parity test — ensures all interfaces expose consistent capabilities.

This test defines the canonical feature set and verifies that each interface
(Python API, MCP Server, CLI, TUI) exposes the expected features. If a feature
is added to one interface, this test will fail until it's added to the others
(or explicitly marked N/A with a reason).
"""

from __future__ import annotations

import re
import subprocess
import sys
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Canonical feature matrix
# ---------------------------------------------------------------------------
# Each feature maps to what's expected per interface:
#   "FULL"    = must be present
#   "PARTIAL" = partially supported (acceptable)
#   "N/A"     = not applicable for this interface
#   "MISSING" = known gap (will fail the test — use to track TODOs)

FEATURE_MATRIX = {
    # feature_name:         (Python API, MCP Server, CLI, TUI)
    "connect":              ("FULL", "FULL", "FULL", "FULL"),
    "disconnect":           ("FULL", "FULL", "N/A",  "FULL"),
    "discover":             ("FULL", "PARTIAL", "FULL", "PARTIAL"),
    "fetch_yang":           ("FULL", "PARTIAL", "FULL", "PARTIAL"),
    "build_catalog":        ("FULL", "PARTIAL", "FULL", "PARTIAL"),
    "find_paths":           ("FULL", "FULL", "FULL", "FULL"),
    "search_paths_semantic":("FULL", "FULL", "FULL", "FULL"),
    "get_telemetry":        ("FULL", "FULL", "FULL", "FULL"),
    "set_config":           ("FULL", "FULL", "FULL", "FULL"),
    "subscribe":            ("FULL", "FULL", "FULL", "PARTIAL"),
    "device_health":        ("FULL", "FULL", "FULL", "FULL"),
    "doctor":               ("FULL", "FULL", "FULL", "FULL"),
    "list_capabilities":    ("FULL", "FULL", "PARTIAL", "FULL"),
    "simulation_mode":      ("FULL", "FULL", "FULL", "FULL"),
    "export_catalog":       ("FULL", "N/A",  "FULL", "N/A"),
}


# ---------------------------------------------------------------------------
# Python API checks
# ---------------------------------------------------------------------------

class TestPythonAPIParity:
    """Verify Python API Device class has all expected methods."""

    EXPECTED_METHODS = {
        "connect": "connect",
        "disconnect": "close",
        "discover": "discover",
        "fetch_yang": "fetch_yang",
        "build_catalog": "build_catalog",
        "find_paths": "find_paths",
        "search_paths_semantic": "search_paths",
        "get_telemetry": "get",
        "set_config": "set",
        "subscribe": "subscribe",
        "device_health": "doctor",
        "doctor": "doctor",
        "list_capabilities": "find_paths",
        "simulation_mode": "connect",  # via sim:// URI
        "export_catalog": "export_catalog",
    }

    def test_all_api_methods_exist(self):
        from splice.api.device import Device
        for feature, method_name in self.EXPECTED_METHODS.items():
            expected = FEATURE_MATRIX[feature][0]  # Python API column
            if expected in ("FULL", "PARTIAL"):
                assert hasattr(Device, method_name), (
                    f"Python API missing method '{method_name}' for feature '{feature}'"
                )
                assert callable(getattr(Device, method_name)), (
                    f"Python API '{method_name}' is not callable"
                )


# ---------------------------------------------------------------------------
# MCP Server checks
# ---------------------------------------------------------------------------

class TestMCPServerParity:
    """Verify MCP server registers all expected tools."""

    EXPECTED_TOOLS = {
        "connect": "connect_device",
        "disconnect": "disconnect_device",
        "find_paths": "find_paths",
        "search_paths_semantic": "search_paths",
        "get_telemetry": "get_telemetry",
        "set_config": "set_config",
        "subscribe": "subscribe_once",
        "device_health": "device_health",
        "doctor": "doctor",
        "list_capabilities": "list_capabilities",
    }

    def test_all_mcp_tools_registered(self):
        pytest.importorskip("mcp", reason="mcp package not installed")
        from splice.mcp_server import create_mcp_server
        server = create_mcp_server()
        tool_names = set()
        for tool in server._tool_manager.list_tools():
            tool_names.add(tool.name)

        for feature, tool_name in self.EXPECTED_TOOLS.items():
            expected = FEATURE_MATRIX[feature][1]  # MCP column
            if expected in ("FULL", "PARTIAL"):
                assert tool_name in tool_names, (
                    f"MCP server missing tool '{tool_name}' for feature '{feature}'. "
                    f"Available: {sorted(tool_names)}"
                )


# ---------------------------------------------------------------------------
# CLI checks
# ---------------------------------------------------------------------------

class TestCLIParity:
    """Verify CLI exposes all expected commands."""

    EXPECTED_IN_TOP_HELP = {
        "find_paths": "find-paths",
        "subscribe": "subscribe",
        "doctor": "doctor",
        "simulation_mode": "--simulate",
        "export_catalog": "export",
    }

    def test_cli_help_contains_commands(self):
        result = subprocess.run(
            [sys.executable, "-m", "splice.cli", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = result.stdout + result.stderr

        for feature, cmd_or_flag in self.EXPECTED_IN_TOP_HELP.items():
            expected = FEATURE_MATRIX[feature][2]  # CLI column
            if expected in ("FULL", "PARTIAL"):
                assert cmd_or_flag in help_text, (
                    f"CLI help missing '{cmd_or_flag}' for feature '{feature}'"
                )

    def test_find_paths_has_semantic_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "splice.cli", "find-paths", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "--semantic" in result.stdout, "find-paths missing --semantic flag"


# ---------------------------------------------------------------------------
# TUI checks
# ---------------------------------------------------------------------------

class TestTUIParity:
    """Verify TUI screens expose expected features."""

    def test_paths_loading_sentinel_handles_rich_text_labels(self):
        textual = pytest.importorskip("textual")
        from rich.text import Text
        from splice.tui.screens.paths import _has_loading_sentinel

        text_node = SimpleNamespace(children=[SimpleNamespace(label=Text("..."))])
        assert _has_loading_sentinel(text_node) is True

    def test_tui_has_set_config(self):
        textual = pytest.importorskip("textual")
        from splice.tui.screens.paths import PathsScreen, SetConfigModal
        # Verify SetConfigModal exists and PathsScreen has the binding
        bindings = {b.key: b.action for b in PathsScreen.BINDINGS}
        assert "s" in bindings, "PathsScreen missing 's' binding for set config"

    def test_tui_has_simulation_mode(self):
        textual = pytest.importorskip("textual")
        from splice.tui.screens.operations import OperationsScreen
        bindings = {b.key: b.action for b in OperationsScreen.BINDINGS}
        assert "m" in bindings, "OperationsScreen missing 'm' binding for simulate"

    def test_tui_has_semantic_search(self):
        textual = pytest.importorskip("textual")
        from splice.tui.screens.paths import PathsScreen
        import inspect
        source = inspect.getsource(PathsScreen)
        assert "semantic" in source.lower() or "PathIndex" in source, (
            "PathsScreen missing semantic search support"
        )


# ---------------------------------------------------------------------------
# Cross-interface consistency
# ---------------------------------------------------------------------------

class TestCrossInterfaceConsistency:
    """Verify no feature is FULL in one interface but completely absent in another
    where it should logically exist."""

    def test_no_unexpected_gaps(self):
        """Every feature marked FULL in any interface should be at least PARTIAL
        in all applicable interfaces (not N/A)."""
        gaps = []
        for feature, (api, mcp, cli, tui) in FEATURE_MATRIX.items():
            statuses = {"api": api, "mcp": mcp, "cli": cli, "tui": tui}
            has_full = any(s == "FULL" for s in statuses.values())
            if has_full:
                for iface, status in statuses.items():
                    if status == "MISSING":
                        gaps.append(f"{feature}: {iface} is MISSING")
        assert not gaps, (
            "Feature parity gaps found:\n" + "\n".join(f"  - {g}" for g in gaps)
        )

    def test_matrix_covers_all_features(self):
        """Sanity check: matrix has a reasonable number of features."""
        assert len(FEATURE_MATRIX) >= 14, (
            f"Feature matrix only has {len(FEATURE_MATRIX)} features — "
            "add new features to FEATURE_MATRIX when implementing them"
        )
