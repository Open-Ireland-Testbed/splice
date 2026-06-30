# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Jobs screen — view and manage polling jobs for the selected device."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static, Switch

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceRegistry
    from splice.tui.services.job_service import JobInfo, JobService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Edit-job modal
# ---------------------------------------------------------------------------

class EditJobModal(ModalScreen[dict | None]):
    """Modal form for editing a single job's interval and enabled state."""

    DEFAULT_CSS = """
    EditJobModal {
        align: center middle;
    }
    #edit-job-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-job-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #edit-job-dialog Input {
        margin-bottom: 0;
    }
    #edit-job-path {
        margin-bottom: 1;
    }
    #edit-job-enabled-row {
        height: 3;
        align: left middle;
    }
    #edit-job-enabled-row Label {
        margin-top: 0;
        margin-right: 1;
    }
    #edit-job-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #edit-job-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, job: JobInfo) -> None:
        super().__init__()
        self._job = job

    def compose(self) -> ComposeResult:
        j = self._job
        display_path = j.canonical_path
        if len(display_path) > 55:
            display_path = "..." + display_path[-52:]
        with Vertical(id="edit-job-dialog"):
            yield Static("Edit Job", classes="title")
            yield Static(f"[dim]{display_path}[/dim]", id="edit-job-path")
            yield Label("Interval (seconds)")
            yield Input(value=str(j.interval_sec), id="inp-job-interval")
            with Horizontal(id="edit-job-enabled-row"):
                yield Label("Enabled")
                yield Switch(value=j.enabled, id="sw-job-enabled")
            with Horizontal(id="edit-job-buttons"):
                yield Button("Save", variant="primary", id="btn-job-save")
                yield Button("Cancel", variant="default", id="btn-job-cancel")

    @on(Button.Pressed, "#btn-job-save")
    def on_save(self) -> None:
        raw = self.query_one("#inp-job-interval", Input).value.strip()
        try:
            interval = int(raw)
            if interval < 1:
                raise ValueError
        except ValueError:
            self.notify("Interval must be a positive integer", severity="error")
            return
        enabled = self.query_one("#sw-job-enabled", Switch).value
        self.dismiss(
            {"id": self._job.id, "interval_sec": interval, "enabled": enabled}
        )

    @on(Button.Pressed, "#btn-job-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Bulk-interval modal
# ---------------------------------------------------------------------------

class BulkIntervalModal(ModalScreen[int | None]):
    """Modal for setting interval on all enabled jobs."""

    DEFAULT_CSS = """
    BulkIntervalModal {
        align: center middle;
    }
    #bulk-interval-dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #bulk-interval-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #bulk-interval-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #bulk-interval-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="bulk-interval-dialog"):
            yield Static("Set Interval for All Enabled Jobs", classes="title")
            yield Label("Interval (seconds)")
            yield Input(placeholder="e.g. 30", id="inp-bulk-interval")
            with Horizontal(id="bulk-interval-buttons"):
                yield Button("Apply", variant="primary", id="btn-bulk-apply")
                yield Button("Cancel", variant="default", id="btn-bulk-cancel")

    @on(Button.Pressed, "#btn-bulk-apply")
    def on_apply(self) -> None:
        raw = self.query_one("#inp-bulk-interval", Input).value.strip()
        try:
            interval = int(raw)
            if interval < 1:
                raise ValueError
        except ValueError:
            self.notify("Interval must be a positive integer", severity="error")
            return
        self.dismiss(interval)

    @on(Button.Pressed, "#btn-bulk-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Jobs screen
# ---------------------------------------------------------------------------

class JobsScreen(Static):
    """Job management view — DataTable of polling jobs for the selected device."""

    BINDINGS = [
        Binding("e", "edit_job", "Edit"),
        Binding("r", "refresh", "Refresh"),
        Binding("E", "enable_all", "Enable all"),
        Binding("D", "disable_all", "Disable all"),
        Binding("i", "set_interval", "Set interval"),
    ]

    def __init__(
        self,
        registry: DeviceRegistry,
        job_service: JobService,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._service = job_service
        self._current_device: str = ""
        self._jobs: list[JobInfo] = []
        self._loading: bool = False
        self._load_generation: int = 0  # incremented on device change

    def compose(self) -> ComposeResult:
        with Vertical(id="jobs-root"):
            with Horizontal(id="jobs-toolbar"):
                yield Static("No device selected", id="jobs-status")
            yield DataTable(id="jobs-table")
            yield Static("No jobs", id="jobs-stats")

    def on_mount(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Path", "Interval", "Enabled", "Mode", "Updated")

    # -- Device change -------------------------------------------------------

    def on_device_changed(self, name: str) -> None:
        """Called by app when selected device changes."""
        if not name:
            # Device was cleared (e.g. deleted) — reset screen
            self._current_device = ""
            self._load_generation += 1
            self._jobs = []
            self._show_empty("No device selected")
            return
        if name == self._current_device:
            return
        self._current_device = name
        self._load_generation += 1
        if not self._loading:
            self._loading = True
            self._load_jobs()

    # -- Data loading --------------------------------------------------------

    @work(thread=True)
    def _load_jobs(self) -> None:
        """Fetch jobs from DB in background thread."""
        gen = self._load_generation
        try:
            name = self._current_device
            entry = self._registry.get(name)
            if entry is None or not entry.db_url:
                if self._load_generation == gen:
                    self.app.call_from_thread(
                        self._show_empty, "No database configured for this device"
                    )
                return
            jobs = self._service.fetch_jobs(entry)
            # Discard stale result if device changed during load
            if self._load_generation != gen:
                return
            self._jobs = jobs
            self.app.call_from_thread(self._populate_table, jobs)
        finally:
            self._loading = False
            if self._load_generation != gen:
                # Device changed during load — schedule reload for new device
                self.app.call_from_thread(self.action_refresh)

    def _populate_table(self, jobs: list[JobInfo]) -> None:
        """Rebuild the DataTable from job list."""
        table = self.query_one("#jobs-table", DataTable)
        table.clear()
        for j in jobs:
            display_path = j.canonical_path
            if len(display_path) > 60:
                display_path = "..." + display_path[-57:]
            enabled_str = "[green]Yes[/]" if j.enabled else "[red]No[/]"
            updated_str = (
                j.updated_at.strftime("%Y-%m-%d %H:%M")
                if j.updated_at
                else "-"
            )
            table.add_row(
                display_path,
                f"{j.interval_sec}s",
                enabled_str,
                j.mode,
                updated_str,
                key=str(j.id),
            )

        # Update status and stats
        self.query_one("#jobs-status", Static).update(
            f"{self._current_device}: {len(jobs)} jobs"
        )
        enabled_count = sum(1 for j in jobs if j.enabled)
        disabled_count = len(jobs) - enabled_count
        if jobs:
            self.query_one("#jobs-stats", Static).update(
                f"{len(jobs)} jobs ({enabled_count} enabled, {disabled_count} disabled)"
            )
        else:
            self.query_one("#jobs-stats", Static).update("No jobs")

    def _show_empty(self, message: str) -> None:
        """Show empty state with a message."""
        table = self.query_one("#jobs-table", DataTable)
        table.clear()
        self._jobs = []
        self.query_one("#jobs-status", Static).update(message)
        self.query_one("#jobs-stats", Static).update("No jobs")

    # -- Helpers -------------------------------------------------------------

    def _get_selected_job(self) -> JobInfo | None:
        """Return the JobInfo for the currently highlighted row."""
        table = self.query_one("#jobs-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            job_id = int(row_key.value)
            for j in self._jobs:
                if j.id == job_id:
                    return j
        except Exception:
            log.debug("Failed to get selected job")
        return None

    # -- Actions: single-job edit --------------------------------------------

    def action_edit_job(self) -> None:
        job = self._get_selected_job()
        if job is None:
            self.notify("No job selected", severity="warning")
            return
        self.app.push_screen(EditJobModal(job), callback=self._on_edit_result)

    def _on_edit_result(self, result: dict | None) -> None:
        if result is None:
            return
        self._save_job(result)

    @work(thread=True)
    def _save_job(self, result: dict) -> None:
        entry = self._registry.get(self._current_device)
        if entry is None:
            return
        ok = self._service.update_job(
            entry, result["id"], result["interval_sec"], result["enabled"]
        )
        if ok:
            jobs = self._service.fetch_jobs(entry)
            self._jobs = jobs
            self.app.call_from_thread(self._populate_table, jobs)
            self.app.call_from_thread(self.notify, "Job updated")
        else:
            self.app.call_from_thread(
                self.notify, "Failed to update job", severity="error"
            )

    # -- Actions: bulk operations --------------------------------------------

    def action_enable_all(self) -> None:
        if not self._current_device:
            self.notify("No device selected", severity="warning")
            return
        self._bulk_toggle(True)

    def action_disable_all(self) -> None:
        if not self._current_device:
            self.notify("No device selected", severity="warning")
            return
        self._bulk_toggle(False)

    @work(thread=True)
    def _bulk_toggle(self, enabled: bool) -> None:
        entry = self._registry.get(self._current_device)
        if entry is None:
            return
        count = self._service.bulk_enable(entry, enabled)
        label = "enabled" if enabled else "disabled"
        jobs = self._service.fetch_jobs(entry)
        self._jobs = jobs
        self.app.call_from_thread(self._populate_table, jobs)
        self.app.call_from_thread(self.notify, f"{count} jobs {label}")

    def action_set_interval(self) -> None:
        if not self._current_device:
            self.notify("No device selected", severity="warning")
            return
        self.app.push_screen(
            BulkIntervalModal(), callback=self._on_bulk_interval_result
        )

    def _on_bulk_interval_result(self, result: int | None) -> None:
        if result is None:
            return
        self._apply_bulk_interval(result)

    @work(thread=True)
    def _apply_bulk_interval(self, interval: int) -> None:
        entry = self._registry.get(self._current_device)
        if entry is None:
            return
        count = self._service.bulk_set_interval(entry, interval)
        jobs = self._service.fetch_jobs(entry)
        self._jobs = jobs
        self.app.call_from_thread(self._populate_table, jobs)
        self.app.call_from_thread(
            self.notify, f"Set {count} enabled jobs to {interval}s interval"
        )

    # -- Actions: refresh ----------------------------------------------------

    def action_refresh(self) -> None:
        if self._current_device and not self._loading:
            self._loading = True
            self._load_jobs()
            self.notify("Refreshing...")
