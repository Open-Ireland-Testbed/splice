# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Telemetry screen — live-updating watched path values with sparklines."""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Sparkline,
    Static,
    Switch,
)

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceRegistry
    from splice.tui.services.telemetry_service import TelemetryService

log = logging.getLogger(__name__)


def _format_age(timestamp_ns: int) -> str:
    """Format a nanosecond timestamp as relative age."""
    if timestamp_ns == 0:
        return "-"
    age_s = time.time() - (timestamp_ns / 1e9)
    if age_s < 0:
        return "0s"
    if age_s < 60:
        return f"{age_s:.0f}s"
    if age_s < 3600:
        return f"{age_s / 60:.0f}m"
    return f"{age_s / 3600:.1f}h"


def _format_value(value: object) -> str:
    """Format a telemetry value for display."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    s = str(value)
    if len(s) > 60:
        return s[:57] + "..."
    return s


def _value_type(value: object) -> str:
    """Return short type name for display."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, dict):
        return "json"
    if isinstance(value, list):
        return "list"
    return "str"


# ---------------------------------------------------------------------------
# Threshold modal
# ---------------------------------------------------------------------------

class ThresholdModal(ModalScreen[dict | None]):
    """Set high/low thresholds for a path."""

    DEFAULT_CSS = """
    ThresholdModal { align: center middle; }
    #threshold-dialog {
        width: 55; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #threshold-dialog Label { margin-top: 1; color: $text-muted; }
    #threshold-buttons { margin-top: 1; height: 3; align: center middle; }
    #threshold-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, path: str, current_low: str = "", current_high: str = "") -> None:
        super().__init__()
        self._path = path
        self._low = current_low
        self._high = current_high

    def compose(self) -> ComposeResult:
        from textual.widgets import Label

        display = self._path
        if len(display) > 50:
            display = "..." + display[-47:]
        with Vertical(id="threshold-dialog"):
            yield Static(f"Threshold: [b]{display}[/b]", classes="title")
            yield Label("Low threshold (blank = none)")
            yield Input(value=self._low, placeholder="e.g. 0.0", id="th-low")
            yield Label("High threshold (blank = none)")
            yield Input(value=self._high, placeholder="e.g. 100.0", id="th-high")
            with Horizontal(id="threshold-buttons"):
                yield Button("Set", variant="primary", id="btn-th-set")
                yield Button("Clear", variant="warning", id="btn-th-clear")
                yield Button("Cancel", variant="default", id="btn-th-cancel")

    @on(Button.Pressed, "#btn-th-set")
    def on_set(self) -> None:
        low_s = self.query_one("#th-low", Input).value.strip()
        high_s = self.query_one("#th-high", Input).value.strip()
        low = None
        high = None
        if low_s:
            try:
                low = float(low_s)
            except ValueError:
                self.notify("Low threshold must be a number", severity="error")
                return
        if high_s:
            try:
                high = float(high_s)
            except ValueError:
                self.notify("High threshold must be a number", severity="error")
                return
        self.dismiss({"low": low, "high": high})

    @on(Button.Pressed, "#btn-th-clear")
    def on_clear(self) -> None:
        self.dismiss({"low": None, "high": None, "clear": True})

    @on(Button.Pressed, "#btn-th-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Export modal
# ---------------------------------------------------------------------------

class ExportModal(ModalScreen[None]):
    """Export watched telemetry to CSV or JSON."""

    DEFAULT_CSS = """
    ExportModal { align: center middle; }
    #export-dialog {
        width: 55; height: auto; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #export-dialog Label { margin-top: 1; color: $text-muted; }
    .export-format-row { height: 3; align: left middle; }
    .export-format-row Label { margin-top: 0; margin-right: 1; }
    #export-buttons { margin-top: 1; height: 3; align: center middle; }
    #export-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, service: TelemetryService) -> None:
        super().__init__()
        self._service = service

    def compose(self) -> ComposeResult:
        from textual.widgets import Label

        with Vertical(id="export-dialog"):
            yield Static("Export Telemetry", classes="title")
            with Horizontal(classes="export-format-row"):
                yield Label("JSON format")
                yield Switch(value=False, id="sw-json")
            yield Label("Output file")
            yield Input(value="telemetry_export.csv", id="export-path")
            with Horizontal(id="export-buttons"):
                yield Button("Export", variant="primary", id="btn-export")
                yield Button("Cancel", variant="default", id="btn-export-cancel")

    def watch_sw_json(self) -> None:
        """Update filename extension when format toggles."""
        pass  # handled in on_switch_changed

    @on(Switch.Changed, "#sw-json")
    def on_format_changed(self, event: Switch.Changed) -> None:
        inp = self.query_one("#export-path", Input)
        current = inp.value
        if event.value:
            inp.value = current.rsplit(".", 1)[0] + ".json"
        else:
            inp.value = current.rsplit(".", 1)[0] + ".csv"

    @on(Button.Pressed, "#btn-export")
    def on_export(self) -> None:
        path = self.query_one("#export-path", Input).value.strip()
        if not path:
            self.notify("File path required", severity="error")
            return
        use_json = self.query_one("#sw-json", Switch).value
        try:
            self._write_export(path, use_json)
            self.notify(f"Exported to {path}")
            self.dismiss(None)
        except Exception as exc:
            self.notify(f"Export failed: {exc}", severity="error")

    def _write_export(self, file_path: str, use_json: bool) -> None:
        watched = self._service.watched_paths
        records = []
        for p in watched:
            prev = self._service.get_current_value(p)
            history = self._service.get_history(p)
            records.append({
                "path": p,
                "value": prev,
                "type": _value_type(prev),
                "history_values": history,
                "history_count": len(history),
            })
        if use_json:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, default=str)
        else:
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["path", "value", "type", "history_count"])
                for r in records:
                    writer.writerow([r["path"], r["value"], r["type"], r["history_count"]])

    @on(Button.Pressed, "#btn-export-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# History modal
# ---------------------------------------------------------------------------

class HistoryModal(ModalScreen[None]):
    """Show DB telemetry history for a path."""

    DEFAULT_CSS = """
    HistoryModal { align: center middle; }
    #history-dialog {
        width: 80; height: 75%; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    #history-table { height: 1fr; }
    #history-buttons { height: 3; align: center middle; }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, path: str, service: TelemetryService) -> None:
        super().__init__()
        self._path = path
        self._service = service

    def compose(self) -> ComposeResult:
        display = self._path
        if len(display) > 70:
            display = "..." + display[-67:]
        with Vertical(id="history-dialog"):
            yield Static(f"History: [b]{display}[/b]", classes="title")
            yield DataTable(id="history-table")
            with Horizontal(id="history-buttons"):
                yield Button("Close", variant="default", id="btn-hist-close")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.zebra_stripes = True
        table.add_columns("Timestamp", "Value", "Created")
        self._load_history()

    @work(thread=True)
    def _load_history(self) -> None:
        rows = self._service.fetch_db_history(self._path, limit=100)
        self.app.call_from_thread(self._populate, rows)

    def _populate(self, rows: list[dict]) -> None:
        try:
            table = self.query_one("#history-table", DataTable)
        except Exception:
            # Modal was dismissed before worker completed
            return
        if not rows:
            table.add_row("No history", "-", "-")
            return
        for r in rows:
            ts_ns = r.get("timestamp_ns", 0)
            ts_str = _format_age(ts_ns) if ts_ns else "-"
            val = r.get("value")
            val_str = _format_value(val)
            created = r.get("created_at", "-")
            table.add_row(ts_str, val_str, str(created))

    @on(Button.Pressed, "#btn-hist-close")
    def on_close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Telemetry screen
# ---------------------------------------------------------------------------

class TelemetryScreen(Static):
    """Live telemetry viewer for watched paths."""

    BINDINGS = [
        Binding("a", "focus_input", "Add path"),
        Binding("d", "remove_path", "Remove"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("x", "clear_all", "Clear all"),
        Binding("t", "set_threshold", "Threshold"),
        Binding("h", "show_history", "History"),
        Binding("e", "export", "Export"),
    ]

    def __init__(
        self,
        registry: DeviceRegistry,
        telemetry_service: TelemetryService,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._service = telemetry_service
        self._current_device: str = ""
        self._paused: bool = False
        self._poll_timer = None
        self._polling: bool = False  # guard against overlapping polls
        self._poll_generation: int = 0  # incremented on device change to discard stale polls

    def compose(self) -> ComposeResult:
        with Vertical(id="telemetry-root"):
            with Horizontal(id="telemetry-toolbar"):
                yield Input(
                    placeholder="Add path to watch...",
                    id="telemetry-path-input",
                )
                yield Button("Add", id="btn-add-watch", variant="primary")
                yield Static("No device", id="telemetry-status")
            yield DataTable(id="telemetry-table")
            with Horizontal(id="telemetry-detail"):
                yield Sparkline(data=[], id="telemetry-sparkline")
                yield Static(
                    "Select a path to view sparkline",
                    id="telemetry-detail-stats",
                )
            yield Static("No paths watched", id="telemetry-stats")

    def on_mount(self) -> None:
        table = self.query_one("#telemetry-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        (self._col_path, self._col_value, self._col_type,
         self._col_age, self._col_trend) = table.add_columns(
            "Path", "Value", "Type", "Age", "Trend"
        )
        # Start polling timer
        self._poll_timer = self.set_interval(3.0, self._poll_tick)

    def on_unmount(self) -> None:
        if hasattr(self, "_poll_timer") and self._poll_timer is not None:
            self._poll_timer.stop()

    # -- Device change -------------------------------------------------------

    def on_device_changed(self, name: str) -> None:
        """Called by app when selected device changes."""
        self._poll_generation += 1
        if not name:
            # Device was cleared (e.g. deleted) — reset screen
            self._current_device = ""
            self._service.clear()
            table = self.query_one("#telemetry-table", DataTable)
            table.clear()
            self.query_one("#telemetry-sparkline", Sparkline).data = []
            self.query_one("#telemetry-detail-stats", Static).update(
                "Select a path to view sparkline"
            )
            self.query_one("#telemetry-stats", Static).update("No paths watched")
            self.query_one("#telemetry-status", Static).update("No device")
            return
        if name == self._current_device:
            return
        self._current_device = name
        entry = self._registry.get(name)
        if entry:
            self._service.set_device(entry)  # clears watched, thresholds, trends, history
            # Clear table rows and sparkline to match the now-empty watch list
            table = self.query_one("#telemetry-table", DataTable)
            table.clear()
            self.query_one("#telemetry-sparkline", Sparkline).data = []
            self.query_one("#telemetry-detail-stats", Static).update(
                "Select a path to view sparkline"
            )
            self.query_one("#telemetry-stats", Static).update("No paths watched")
            self.query_one("#telemetry-status", Static).update(
                f"{name} | {self._service.get_status()}"
            )

    # -- Watch list management -----------------------------------------------

    def add_watch(self, path: str) -> None:
        """Add a path to the watch list (called from app or input)."""
        path = path.strip()
        if not path:
            return
        if self._service.watch(path):
            self._refresh_table()
            self.notify(f"Watching: {path}")
        else:
            self.notify(f"Already watching: {path}", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add-watch":
            inp = self.query_one("#telemetry-path-input", Input)
            self.add_watch(inp.value)
            inp.value = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "telemetry-path-input":
            self.add_watch(event.input.value)
            event.input.value = ""

    # -- Sparkline detail panel ----------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update sparkline when cursor moves."""
        if event.row_key is None:
            return
        path = str(event.row_key.value)
        history = self._service.get_history(path)
        sparkline = self.query_one("#telemetry-sparkline", Sparkline)
        stats = self.query_one("#telemetry-detail-stats", Static)
        if history and len(history) >= 2:
            sparkline.data = history
            mn, mx, avg = min(history), max(history), sum(history) / len(history)
            stats.update(
                f"Min: {mn:.4g}  Max: {mx:.4g}  Avg: {avg:.4g}  Samples: {len(history)}"
            )
        else:
            sparkline.data = []
            stats.update("Not enough numeric data for chart")

    # -- Polling -------------------------------------------------------------

    def _poll_tick(self) -> None:
        """Timer callback — triggers a background poll."""
        if self._paused or self._polling or not self._service.watched_paths:
            return
        self._polling = True
        self._do_poll(self._poll_generation)

    @work(thread=True)
    def _do_poll(self, gen: int) -> None:
        """Fetch telemetry in background thread."""
        try:
            updates = self._service.poll_once()
            self.app.call_from_thread(self._apply_updates, updates, gen)
        finally:
            self._polling = False

    def _apply_updates(self, updates, gen: int) -> None:
        """Apply polled updates to the DataTable."""
        if self._poll_generation != gen:
            return  # device changed during poll, discard stale results
        table = self.query_one("#telemetry-table", DataTable)
        for update in updates:
            trend = self._service.get_trend(update.path, update.value)
            self._service.record_value(update.path, update.value)
            row_key = update.path

            # Check threshold alert
            alert = self._service.check_threshold(update.path, update.value)
            if alert == "high":
                val_display = Text(_format_value(update.value), style="bold red")
                trend = trend + " \u26a0"  # ⚠
            elif alert == "low":
                val_display = Text(_format_value(update.value), style="bold yellow")
                trend = trend + " \u26a0"
            else:
                val_display = _format_value(update.value)

            type_str = _value_type(update.value)
            age_str = _format_age(update.timestamp_ns)

            # Shorten path for display
            display_path = update.path
            if len(display_path) > 50:
                display_path = "..." + display_path[-47:]

            try:
                # Update existing row
                table.get_row_index(row_key)
                table.update_cell(row_key, self._col_value, val_display)
                table.update_cell(row_key, self._col_type, type_str)
                table.update_cell(row_key, self._col_age, age_str)
                table.update_cell(row_key, self._col_trend, trend)
            except Exception:
                log.debug("Failed to update row for %s", update.path)

        # Update sparkline for currently highlighted row
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            path = str(row_key.value)
            history = self._service.get_history(path)
            sparkline = self.query_one("#telemetry-sparkline", Sparkline)
            stats_widget = self.query_one("#telemetry-detail-stats", Static)
            if history and len(history) >= 2:
                sparkline.data = history
                mn, mx, avg = min(history), max(history), sum(history) / len(history)
                stats_widget.update(
                    f"Min: {mn:.4g}  Max: {mx:.4g}  Avg: {avg:.4g}  Samples: {len(history)}"
                )
        except (KeyError, IndexError):
            pass  # No row selected yet
        except Exception:
            log.debug("Failed to update sparkline", exc_info=True)

        # Update status
        status = self._service.get_status()
        paused_label = " [PAUSED]" if self._paused else ""
        self.query_one("#telemetry-status", Static).update(
            f"{self._current_device} | {status}{paused_label}"
        )
        count = len(self._service.watched_paths)
        self.query_one("#telemetry-stats", Static).update(
            f"Watching {count} path{'s' if count != 1 else ''}"
        )

    def _refresh_table(self) -> None:
        """Rebuild the table from the current watch list."""
        table = self.query_one("#telemetry-table", DataTable)
        table.clear()
        for path in self._service.watched_paths:
            display_path = path
            if len(display_path) > 50:
                display_path = "..." + display_path[-47:]
            table.add_row(display_path, "-", "-", "-", " ", key=path)

        count = len(self._service.watched_paths)
        self.query_one("#telemetry-stats", Static).update(
            f"Watching {count} path{'s' if count != 1 else ''}"
            if count > 0
            else "No paths watched"
        )

    # -- Helpers -------------------------------------------------------------

    def _get_selected_path(self) -> str | None:
        """Return path string of currently highlighted row."""
        table = self.query_one("#telemetry-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    # -- Actions -------------------------------------------------------------

    def action_focus_input(self) -> None:
        self.query_one("#telemetry-path-input", Input).focus()

    def action_remove_path(self) -> None:
        table = self.query_one("#telemetry-table", DataTable)
        if table.row_count == 0:
            self.notify("No paths to remove", severity="warning")
            return
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            path = str(row_key.value)
            self._service.unwatch(path)
            table.remove_row(row_key)
            self.notify(f"Unwatched: {path}")
            count = len(self._service.watched_paths)
            self.query_one("#telemetry-stats", Static).update(
                f"Watching {count} path{'s' if count != 1 else ''}"
                if count > 0
                else "No paths watched"
            )
        except Exception:
            self.notify("Select a path first", severity="warning")

    def action_refresh(self) -> None:
        if self._service.watched_paths and not self._polling:
            self._polling = True
            self._do_poll(self._poll_generation)

    def action_toggle_pause(self) -> None:
        self._paused = not self._paused
        label = "Polling paused" if self._paused else "Polling resumed"
        self.notify(label)

    def action_clear_all(self) -> None:
        self._service.clear()
        self._refresh_table()
        self.query_one("#telemetry-sparkline", Sparkline).data = []
        self.query_one("#telemetry-detail-stats", Static).update(
            "Select a path to view sparkline"
        )
        self.notify("Cleared all watches")

    def action_set_threshold(self) -> None:
        path = self._get_selected_path()
        if path is None:
            self.notify("Select a path first", severity="warning")
            return
        self._pending_threshold_path = path
        current = self._service.get_threshold(path)
        low_s = str(current.low) if current and current.low is not None else ""
        high_s = str(current.high) if current and current.high is not None else ""
        self.app.push_screen(
            ThresholdModal(path, low_s, high_s),
            callback=self._on_threshold_result,
        )

    def _on_threshold_result(self, result: dict | None) -> None:
        if result is None:
            return
        path = self._pending_threshold_path
        if path is None:
            return
        if result.get("clear"):
            self._service.clear_threshold(path)
            self.notify("Threshold cleared")
        else:
            self._service.set_threshold(path, result["low"], result["high"])
            self.notify("Threshold set")

    def action_show_history(self) -> None:
        path = self._get_selected_path()
        if path is None:
            self.notify("Select a path first", severity="warning")
            return
        entry = self._registry.get(self._current_device)
        if not entry or not entry.db_url:
            self.notify("No database configured for history", severity="warning")
            return
        self.app.push_screen(HistoryModal(path, self._service))

    def action_export(self) -> None:
        if not self._service.watched_paths:
            self.notify("No paths to export", severity="warning")
            return
        self.app.push_screen(ExportModal(self._service))
