# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Footer status bar showing selected device and keybindings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class StatusBar(Widget):
    """Persistent footer bar for the TUI."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $primary-darken-2;
        color: $text;
    }
    StatusBar .status-bar-device {
        width: auto;
        padding: 0 1;
        background: $secondary;
        color: $text;
        text-style: bold;
    }
    StatusBar .status-bar-keys {
        width: 1fr;
        padding: 0 1;
        text-align: right;
        color: $text-muted;
    }
    """

    selected_device: reactive[str] = reactive("No device selected")
    active_tab: reactive[str] = reactive("devices")

    TAB_KEYS = {
        "devices": "a:add  e:edit  v:config  d:del  t:test  r:refresh  1-6:tabs",
        "paths": "/:search  r:refresh  c:copy  f:config  w:watch  1-6:tabs",
        "telemetry": "a:add  d:del  t:thresh  h:hist  e:export  r:refresh  p:pause  x:clear  1-6:tabs",
        "status": "r:refresh  1-6:tabs",
        "operations": "d:doctor  s:setup  r:run  x:stop  c:clear  1-6:tabs",
        "jobs": "e:edit  r:refresh  E:enable-all  D:disable-all  i:interval  1-6:tabs",
    }

    def compose(self) -> ComposeResult:
        yield Static(self.selected_device, classes="status-bar-device", id="sb-device")
        yield Static(
            self.TAB_KEYS.get("devices", ""),
            classes="status-bar-keys",
            id="sb-keys",
        )

    def watch_selected_device(self, value: str) -> None:
        try:
            self.query_one("#sb-device", Static).update(value)
        except Exception:
            pass

    def watch_active_tab(self, value: str) -> None:
        try:
            self.query_one("#sb-keys", Static).update(
                self.TAB_KEYS.get(value, "")
            )
        except Exception:
            pass
