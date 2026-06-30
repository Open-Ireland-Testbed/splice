# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operations screen — server control panel with log viewer."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, RichLog, Static

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceRegistry
    from splice.tui.services.process_manager import ProcessManager

log = logging.getLogger(__name__)


class SetupCompleted(Message):
    """Posted when a setup subprocess finishes successfully."""

    def __init__(self, device_name: str) -> None:
        super().__init__()
        self.device_name = device_name


def _format_uptime(started_at: float | None) -> str:
    if started_at is None:
        return ""
    elapsed = time.time() - started_at
    if elapsed < 60:
        return f"{elapsed:.0f}s"
    if elapsed < 3600:
        return f"{elapsed / 60:.0f}m"
    return f"{elapsed / 3600:.1f}h"


class OperationsScreen(Static):
    """Server control panel with subprocess management and log viewer."""

    BINDINGS = [
        Binding("d", "run_doctor", "Doctor"),
        Binding("s", "run_setup", "Setup"),
        Binding("r", "run_server", "Run"),
        Binding("m", "run_simulate", "Simulate"),
        Binding("x", "stop_server", "Stop"),
        Binding("c", "clear_log", "Clear log"),
    ]

    def __init__(
        self,
        registry: DeviceRegistry,
        process_manager: ProcessManager,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._pm = process_manager
        self._current_device: str = ""
        self._process_device: str | None = None  # device owning the running process
        self._output_timer = None
        self._status_timer = None
        self._line_count: int = 0
        self._error_count: int = 0
        self._previous_status: str = "stopped"

    def compose(self) -> ComposeResult:
        with Vertical(id="ops-root"):
            with Horizontal(id="ops-toolbar"):
                yield Button("Doctor", id="btn-doctor", variant="default")
                yield Button("Setup", id="btn-setup", variant="primary")
                yield Button("Run", id="btn-run", variant="success")
                yield Button("Stop", id="btn-stop", variant="error", disabled=True)
                yield Static("Stopped | No device selected", id="ops-server-status")
            yield RichLog(id="ops-log", highlight=True, markup=True, wrap=True)
            with Horizontal(id="ops-footer"):
                yield Static("Lines: 0 | Errors: 0", id="ops-stats")

    def on_mount(self) -> None:
        self._output_timer = self.set_interval(0.25, self._read_output_tick)
        self._status_timer = self.set_interval(1.0, self._update_status)

    def on_unmount(self) -> None:
        if hasattr(self, "_output_timer") and self._output_timer is not None:
            self._output_timer.stop()
        if hasattr(self, "_status_timer") and self._status_timer is not None:
            self._status_timer.stop()

    # -- Device change -------------------------------------------------------

    def on_device_changed(self, name: str) -> None:
        if not name:
            # Device was cleared (e.g. deleted) — reset screen
            self._current_device = ""
            self._update_status_label()
            return
        if name == self._current_device:
            return
        self._current_device = name
        self._update_status_label()
        # Warn if a process is running for a different device
        if self._process_device and self._process_device != name and self._pm.is_running:
            self.notify(
                f"Process running for '{self._process_device}', not '{name}'",
                severity="warning",
            )

    # -- Actions -------------------------------------------------------------

    def action_run_doctor(self) -> None:
        if not self._current_device:
            self.notify("Select a device first", severity="warning")
            return
        if self._pm.is_running:
            self.notify("A process is running — stop it first", severity="warning")
            return
        entry = self._registry.get(self._current_device)
        if entry is None:
            self.notify("Device not found", severity="error")
            return
        self._clear_log()
        log_widget = self.query_one("#ops-log", RichLog)
        log_widget.write(f"[bold]--- Doctor: {self._current_device} ---[/bold]")
        self._do_doctor(entry.config_path)

    @work(thread=True)
    def _do_doctor(self, config_path: str) -> None:
        """Run splice doctor as subprocess."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "splice.cli",
                 "--config", config_path, "doctor"],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            lines = (result.stdout or "").splitlines() + (result.stderr or "").splitlines()
        except subprocess.TimeoutExpired:
            lines = ["[red]Doctor timed out after 30 seconds[/red]"]
        except Exception as exc:
            lines = [f"[red]Failed to run doctor: {exc}[/red]"]

        def _show() -> None:
            log_w = self.query_one("#ops-log", RichLog)
            for line in lines:
                if "PASS" in line:
                    log_w.write(f"[green]{line}[/green]")
                elif "FAIL" in line:
                    log_w.write(f"[red]{line}[/red]")
                elif line.strip().startswith("Fix:") or line.strip().startswith("→"):
                    log_w.write(f"[yellow]  {line}[/yellow]")
                else:
                    log_w.write(line)
            self._line_count += len(lines)
            self.query_one("#ops-stats", Static).update(
                f"Lines: {self._line_count} | Errors: {self._error_count}"
            )

        self.app.call_from_thread(_show)

    def action_run_setup(self) -> None:
        if not self._current_device:
            self.notify("Select a device first", severity="warning")
            return
        if self._pm.is_running:
            self.notify("A process is already running — stop it first", severity="warning")
            return
        entry = self._registry.get(self._current_device)
        if entry is None:
            self.notify("Device not found", severity="error")
            return

        self._clear_log()
        log_widget = self.query_one("#ops-log", RichLog)
        log_widget.write(f"[bold]--- Setup: {self._current_device} ---[/bold]")

        if self._pm.start_setup(entry.config_path):
            self._process_device = self._current_device
            self._toggle_buttons(running=True)
        else:
            self.notify("Failed to start setup", severity="error")

    def action_run_server(self) -> None:
        if not self._current_device:
            self.notify("Select a device first", severity="warning")
            return
        if self._pm.is_running:
            self.notify("A process is already running — stop it first", severity="warning")
            return
        entry = self._registry.get(self._current_device)
        if entry is None:
            self.notify("Device not found", severity="error")
            return

        self._clear_log()
        log_widget = self.query_one("#ops-log", RichLog)
        log_widget.write(f"[bold]--- Run: {self._current_device} ---[/bold]")

        if self._pm.start_run(entry.config_path):
            self._process_device = self._current_device
            self._toggle_buttons(running=True)
        else:
            self.notify("Failed to start server", severity="error")

    def action_run_simulate(self) -> None:
        if self._pm.is_running:
            self.notify("A process is already running — stop it first", severity="warning")
            return

        self._clear_log()
        log_widget = self.query_one("#ops-log", RichLog)
        log_widget.write("[bold]--- Simulate: openroadm ---[/bold]")

        # Use device config for gNMI port settings if a device is selected
        config_path = None
        if self._current_device:
            entry = self._registry.get(self._current_device)
            if entry is not None:
                config_path = entry.config_path

        if self._pm.start_simulate("openroadm", config_path=config_path):
            self._process_device = self._current_device or "(simulated)"
            self._toggle_buttons(running=True)
        else:
            self.notify("Failed to start simulation", severity="error")

    def action_stop_server(self) -> None:
        if not self._pm.is_running:
            self.notify("No process running", severity="warning")
            return
        self.notify("Stopping...")
        self._do_stop()

    @work(thread=True)
    def _do_stop(self) -> None:
        self._pm.stop()
        self.app.call_from_thread(self._on_stopped)

    def _on_stopped(self) -> None:
        self._process_device = None
        self._toggle_buttons(running=False)
        self.notify("Process stopped")

    def action_clear_log(self) -> None:
        self._clear_log()

    # -- Polling output and status -------------------------------------------

    def _read_output_tick(self) -> None:
        lines = self._pm.read_output()
        if not lines:
            return
        log_widget = self.query_one("#ops-log", RichLog)
        for line in lines:
            stripped = line.rstrip()
            log_widget.write(stripped)
            self._line_count += 1
            if "ERROR" in stripped or "FAILED" in stripped:
                self._error_count += 1
        self.query_one("#ops-stats", Static).update(
            f"Lines: {self._line_count} | Errors: {self._error_count}"
        )

    def _update_status(self) -> None:
        state = self._pm.get_state()

        # Detect setup completion — attribute to the process owner, not current selection
        if (
            self._previous_status == "setup"
            and state.status in ("stopped",)
            and state.exit_code == 0
            and self._process_device
        ):
            self.post_message(SetupCompleted(self._process_device))

        # Detect process ending
        if self._previous_status in ("setup", "running") and state.status in (
            "stopped",
            "crashed",
        ):
            self._process_device = None
            self._toggle_buttons(running=False)

        self._previous_status = state.status
        self._update_status_label(state)

    def _update_status_label(self, state=None) -> None:
        if state is None:
            state = self._pm.get_state()

        if state.status == "stopped":
            if state.exit_code is not None and state.exit_code != 0:
                label = f"Stopped (exit {state.exit_code})"
            else:
                label = "Stopped"
        elif state.status == "setup":
            step = state.setup_step or "starting..."
            label = f"Setup: {step}"
        elif state.status == "running":
            uptime = _format_uptime(state.started_at)
            if state.server_ready:
                label = f"Running (PID {state.pid}, {uptime})"
            else:
                label = f"Starting (PID {state.pid})..."
        elif state.status == "crashed":
            label = f"Crashed (exit {state.exit_code})"
        else:
            label = state.status

        device_label = self._current_device or "No device"
        # Show warning if process belongs to a different device
        if self._process_device and self._process_device != self._current_device:
            label = f"[yellow]Process for '{self._process_device}'[/yellow] | {label}"
        self.query_one("#ops-server-status", Static).update(
            f"{device_label} | {label}"
        )

    # -- Helpers -------------------------------------------------------------

    def _toggle_buttons(self, running: bool) -> None:
        self.query_one("#btn-setup", Button).disabled = running
        self.query_one("#btn-run", Button).disabled = running
        self.query_one("#btn-stop", Button).disabled = not running

    def _clear_log(self) -> None:
        self.query_one("#ops-log", RichLog).clear()
        self._line_count = 0
        self._error_count = 0
        self.query_one("#ops-stats", Static).update("Lines: 0 | Errors: 0")
