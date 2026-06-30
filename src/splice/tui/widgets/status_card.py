# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reusable status card widget for the status dashboard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class StatusCard(Widget):
    """Compact card showing a service's health status."""

    DEFAULT_CSS = """
    StatusCard {
        width: 1fr;
        height: auto;
        min-height: 5;
        border: solid $primary;
        padding: 1;
        margin: 0 1;
    }
    StatusCard .card-title {
        text-style: bold;
        margin-bottom: 1;
    }
    StatusCard .card-indicator {
        width: auto;
    }
    StatusCard .card-details {
        color: $text-muted;
    }
    """

    title: reactive[str] = reactive("Service")
    healthy: reactive[bool] = reactive(True)
    details: reactive[str] = reactive("")

    def __init__(
        self,
        title: str = "Service",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.title = title

    def compose(self) -> ComposeResult:
        yield Static(self.title, classes="card-title", id=f"{self.id}-title" if self.id else None)
        yield Static("", classes="card-indicator", id=f"{self.id}-indicator" if self.id else None)
        yield Static("", classes="card-details", id=f"{self.id}-details" if self.id else None)

    def watch_title(self, value: str) -> None:
        try:
            self.query_one(".card-title", Static).update(value)
        except Exception:
            pass

    def watch_healthy(self, value: bool) -> None:
        try:
            indicator = self.query_one(".card-indicator", Static)
            if value:
                indicator.update("[green bold]● Healthy[/green bold]")
            else:
                indicator.update("[red bold]● Unhealthy[/red bold]")
        except Exception:
            pass

    def watch_details(self, value: str) -> None:
        try:
            self.query_one(".card-details", Static).update(value)
        except Exception:
            pass

    def update_status(self, healthy: bool, details: str) -> None:
        """Convenience method to update both fields at once."""
        self.healthy = healthy
        self.details = details
