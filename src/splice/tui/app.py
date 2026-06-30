# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main TUI application for Splice management."""

from __future__ import annotations

import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Footer, Header, TabbedContent, TabPane

from splice.tui.screens.devices import DevicesScreen
from splice.tui.screens.jobs import JobsScreen
from splice.tui.screens.operations import OperationsScreen, SetupCompleted
from splice.tui.screens.paths import PathsScreen, WatchPath
from splice.tui.screens.status import StatusDashboard
from splice.tui.screens.telemetry import TelemetryScreen
from splice.tui.services.catalog_loader import CatalogLoader
from splice.tui.services.device_registry import DeviceRegistry
from splice.tui.services.health_checker import HealthChecker
from splice.tui.services.job_service import JobService
from splice.tui.services.process_manager import ProcessManager
from splice.tui.services.telemetry_service import TelemetryService
from splice.tui.widgets.status_bar import StatusBar

log = logging.getLogger(__name__)


class SpliceApp(App):
    """Interactive TUI for managing Splice devices."""

    TITLE = "Splice"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", show=False),
        Binding("1", "show_tab('devices')", "Devices", show=False),
        Binding("2", "show_tab('paths')", "Paths", show=False),
        Binding("3", "show_tab('telemetry')", "Telemetry", show=False),
        Binding("4", "show_tab('status')", "Status", show=False),
        Binding("5", "show_tab('operations')", "Operations", show=False),
        Binding("6", "show_tab('jobs')", "Jobs", show=False),
    ]

    selected_device: reactive[str] = reactive("")

    def __init__(self, devices_dir: str = "devices") -> None:
        super().__init__()
        self.device_registry = DeviceRegistry(base_dir=devices_dir)
        self.catalog_loader = CatalogLoader()
        self.telemetry_service = TelemetryService()
        self.health_checker = HealthChecker()
        self.process_manager = ProcessManager()
        self.job_service = JobService()

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="main-tabs"):
            with TabPane("Devices", id="tab-devices"):
                yield DevicesScreen(self.device_registry)
            with TabPane("Paths", id="tab-paths"):
                yield PathsScreen(self.device_registry, self.catalog_loader)
            with TabPane("Telemetry", id="tab-telemetry"):
                yield TelemetryScreen(self.device_registry, self.telemetry_service)
            with TabPane("Status", id="tab-status"):
                yield StatusDashboard(self.device_registry, self.health_checker)
            with TabPane("Operations", id="tab-operations"):
                yield OperationsScreen(self.device_registry, self.process_manager)
            with TabPane("Jobs", id="tab-jobs"):
                yield JobsScreen(self.device_registry, self.job_service)
        yield StatusBar()
        yield Footer()

    def watch_selected_device(self, value: str) -> None:
        status_bar = self.query_one(StatusBar)
        if value:
            entry = self.device_registry.get(value)
            if entry:
                status_bar.selected_device = f" {value} ({entry.host}:{entry.netconf_port}) "
            else:
                status_bar.selected_device = f" {value} "
        else:
            status_bar.selected_device = " No device selected "

        # Notify all screens of device change
        for screen_cls in (PathsScreen, TelemetryScreen, StatusDashboard, OperationsScreen, JobsScreen):
            try:
                self.query_one(screen_cls).on_device_changed(value)
            except Exception:
                log.debug("Failed to notify %s of device change", screen_cls.__name__)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Update status bar when tab changes."""
        tab_id = event.pane.id or ""
        tab_name = tab_id.replace("tab-", "") if tab_id.startswith("tab-") else tab_id
        try:
            self.query_one(StatusBar).active_tab = tab_name
        except Exception:
            log.debug("Failed to update status bar tab to %s", tab_name)

    def on_watch_path(self, event: WatchPath) -> None:
        """Route WatchPath message from PathsScreen to TelemetryScreen."""
        try:
            self.query_one(TelemetryScreen).add_watch(event.path)
            self.notify(f"Added to telemetry: {event.path}")
        except Exception:
            log.debug("Failed to route WatchPath to TelemetryScreen")

    def on_setup_completed(self, event: SetupCompleted) -> None:
        """Auto-refresh paths catalog after setup completes."""
        self.notify(f"Setup completed for {event.device_name}")
        try:
            paths_screen = self.query_one(PathsScreen)
            self.catalog_loader.clear(event.device_name)
            if paths_screen._current_device == event.device_name:
                paths_screen._load_catalog()
        except Exception:
            log.debug("Failed to refresh paths after setup for %s", event.device_name)

    def action_quit(self) -> None:
        """Clean up resources before quitting."""
        self.process_manager.shutdown()
        try:
            self.telemetry_service.close()
        except Exception:
            pass
        super().action_quit()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#main-tabs", TabbedContent).active = f"tab-{tab_id}"

    def action_help(self) -> None:
        self.notify(
            "1:devices  2:paths  3:telemetry  4:status  5:ops  6:jobs  q:quit",
            title="Keybindings",
        )
