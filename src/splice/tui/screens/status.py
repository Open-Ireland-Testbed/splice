# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Status dashboard — at-a-glance health of the bridge instance."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from splice.tui.widgets.status_card import StatusCard

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceRegistry
    from splice.tui.services.health_checker import HealthChecker

log = logging.getLogger(__name__)


class StatusDashboard(Static):
    """Health dashboard for the selected device's bridge instance."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        registry: DeviceRegistry,
        health_checker: HealthChecker,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._checker = health_checker
        self._current_device: str = ""
        self._refresh_timer = None
        self._checking: bool = False
        self._load_generation: int = 0  # incremented on device change

    def compose(self) -> ComposeResult:
        with Vertical(id="status-root"):
            with Horizontal(id="status-cards"):
                yield StatusCard("gNMI Server", id="card-gnmi")
                yield StatusCard("NETCONF", id="card-netconf")
                yield StatusCard("Database", id="card-db")
            with Horizontal(id="status-details"):
                with Vertical(id="status-health"):
                    yield Static("[bold]Health Details[/bold]", id="health-title")
                    yield Static(
                        "Select a device to view health status",
                        id="health-detail-text",
                        classes="empty-message",
                    )
                with Vertical(id="status-counts"):
                    yield Static("[bold]Resource Counts[/bold]", id="counts-title")
                    yield DataTable(id="counts-table")

    def on_mount(self) -> None:
        table = self.query_one("#counts-table", DataTable)
        self._col_resource, self._col_count = table.add_columns("Resource", "Count")
        table.add_row("Catalog Paths", "—", key="catalog")
        table.add_row("Jobs", "—", key="jobs")
        table.add_row("Modules", "—", key="modules")
        table.add_row("List Instances", "—", key="lists")
        # Start auto-refresh
        self._refresh_timer = self.set_interval(10.0, self._refresh_tick)

    def on_unmount(self) -> None:
        if hasattr(self, "_refresh_timer") and self._refresh_timer is not None:
            self._refresh_timer.stop()

    # -- Device change -------------------------------------------------------

    def on_device_changed(self, name: str) -> None:
        """Called by app when selected device changes."""
        if not name:
            # Device was cleared (e.g. deleted) — reset screen
            self._current_device = ""
            self._load_generation += 1
            self.query_one("#card-gnmi", StatusCard).update_status(healthy=False, details="")
            self.query_one("#card-netconf", StatusCard).update_status(healthy=False, details="")
            self.query_one("#card-db", StatusCard).update_status(healthy=False, details="")
            self.query_one("#health-detail-text", Static).update(
                "Select a device to view health status"
            )
            table = self.query_one("#counts-table", DataTable)
            col = self._col_count
            for key in ("catalog", "jobs", "modules", "lists"):
                table.update_cell(key, col, "—")
            return
        if name == self._current_device:
            return
        self._current_device = name
        self._load_generation += 1
        self._checking = True
        self._run_check()

    # -- Health checking -----------------------------------------------------

    def _refresh_tick(self) -> None:
        if self._current_device and not self._checking:
            self._checking = True
            self._run_check()

    @work(thread=True)
    def _run_check(self) -> None:
        """Run health checks in background thread."""
        gen = self._load_generation
        try:
            name = self._current_device
            entry = self._registry.get(name)
            if entry is None:
                return
            status = self._checker.check(entry)
            # Discard stale result if device changed during check
            if self._load_generation != gen:
                return
            self.app.call_from_thread(self._apply_status, status)
        finally:
            self._checking = False
            if self._load_generation != gen:
                # Device changed during check — schedule recheck for new device
                self.app.call_from_thread(self._refresh_tick)

    def _apply_status(self, status) -> None:
        """Update UI with health check results."""
        from splice.tui.services.health_checker import HealthStatus

        s: HealthStatus = status

        # gNMI card
        gnmi_card = self.query_one("#card-gnmi", StatusCard)
        gnmi_details = f"Port: {s.gnmi_port}"
        if s.health_endpoint_ok:
            gnmi_details += "\nHealth endpoint: OK"
        gnmi_card.update_status(
            healthy=s.gnmi_reachable,
            details=gnmi_details,
        )

        # NETCONF card
        netconf_card = self.query_one("#card-netconf", StatusCard)
        netconf_details = ""
        cb_status = s.health_detail.get("netconf_status", {})
        if cb_status:
            cb_open = cb_status.get("circuit_breaker_open", False)
            failures = cb_status.get("consecutive_failures", 0)
            netconf_details = f"Failures: {failures}"
            if cb_open:
                remaining = cb_status.get("circuit_breaker_seconds_remaining", 0)
                netconf_details += f"\nCircuit breaker: OPEN ({remaining:.0f}s)"
            else:
                netconf_details += "\nCircuit breaker: closed"
        netconf_card.update_status(
            healthy=s.netconf_reachable,
            details=netconf_details,
        )

        # Database card
        db_card = self.query_one("#card-db", StatusCard)
        db_details = ""
        if s.db_reachable:
            db_details = f"Paths: {s.catalog_path_count}  Jobs: {s.job_count}"
        else:
            db_details = "Not reachable"
        db_card.update_status(healthy=s.db_reachable, details=db_details)

        # Health detail panel
        if s.health_detail:
            detail_text = json.dumps(s.health_detail, indent=2)
        elif s.health_endpoint_ok:
            detail_text = "Health endpoint returned empty response"
        else:
            detail_text = "Health endpoint not available\n(Is the server running?)"
        self.query_one("#health-detail-text", Static).update(detail_text)

        # Counts table
        table = self.query_one("#counts-table", DataTable)
        col = self._col_count
        if s.db_reachable:
            table.update_cell("catalog", col, str(s.catalog_path_count))
            table.update_cell("jobs", col, str(s.job_count))
            table.update_cell("modules", col, str(s.module_count))
            table.update_cell("lists", col, str(s.list_instance_count))
        else:
            for key in ("catalog", "jobs", "modules", "lists"):
                table.update_cell(key, col, "—")

    # -- Actions -------------------------------------------------------------

    def action_refresh(self) -> None:
        if self._current_device and not self._checking:
            self._checking = True
            self._run_check()
            self.notify("Refreshing...")
