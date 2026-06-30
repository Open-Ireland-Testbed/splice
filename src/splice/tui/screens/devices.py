# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Devices screen — list, add, remove, edit, and test devices."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceRegistry


def _parse_port(value: str, default: int) -> int | None:
    """Parse a port string, returning None if invalid (range 1-65535)."""
    if not value.strip():
        return default
    try:
        port = int(value)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Add-device modal
# ---------------------------------------------------------------------------

class AddDeviceModal(ModalScreen[dict | None]):
    """Modal form for adding a new device."""

    DEFAULT_CSS = """
    AddDeviceModal {
        align: center middle;
    }
    #add-device-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #add-device-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #add-device-dialog Input {
        margin-bottom: 0;
    }
    #add-device-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #add-device-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="add-device-dialog"):
            yield Static("Add Device", classes="title")
            yield Label("Device name")
            yield Input(placeholder="e.g. teraflex", id="inp-name")
            yield Label("Host / IP")
            yield Input(placeholder="e.g. 192.0.2.120", id="inp-host")
            yield Label("NETCONF port")
            yield Input(value="830", id="inp-port")
            yield Label("Username")
            yield Input(value="admin", id="inp-username")
            yield Label("Password")
            yield Input(password=True, id="inp-password")
            yield Label("gNMI server port")
            yield Input(value="9339", id="inp-gnmi-port")
            yield Label("Database URL")
            yield Input(
                value="postgresql+psycopg://postgres:postgres@localhost:5432/splice",
                id="inp-db-url",
            )
            with Horizontal(id="add-device-buttons"):
                yield Button("Create", variant="primary", id="btn-create")
                yield Button("Cancel", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-create")
    def on_create(self) -> None:
        name = self.query_one("#inp-name", Input).value.strip()
        host = self.query_one("#inp-host", Input).value.strip()
        if not name or not host:
            self.notify("Name and host are required", severity="error")
            return
        port = _parse_port(self.query_one("#inp-port", Input).value, 830)
        gnmi_port = _parse_port(self.query_one("#inp-gnmi-port", Input).value, 9339)
        if port is None or gnmi_port is None:
            self.notify("Ports must be integers between 1 and 65535", severity="error")
            return
        self.dismiss(
            {
                "name": name,
                "host": host,
                "port": port,
                "username": self.query_one("#inp-username", Input).value.strip() or "admin",
                "password": self.query_one("#inp-password", Input).value,
                "gnmi_port": gnmi_port,
                "db_url": self.query_one("#inp-db-url", Input).value.strip(),
            }
        )

    @on(Button.Pressed, "#btn-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Confirm-delete modal
# ---------------------------------------------------------------------------

class ConfirmDeleteModal(ModalScreen[bool]):
    """Simple yes/no confirmation for device deletion."""

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, device_name: str) -> None:
        super().__init__()
        self._device_name = device_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(
                f"Delete device [b]{self._device_name}[/b]?\n\n"
                "This removes the device directory and all its files.",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="btn-yes")
                yield Button("Cancel", variant="default", id="btn-no")

    @on(Button.Pressed, "#btn-yes")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def on_no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Edit-device modal
# ---------------------------------------------------------------------------

class EditDeviceModal(ModalScreen[dict | None]):
    """Modal form for editing an existing device."""

    DEFAULT_CSS = """
    EditDeviceModal {
        align: center middle;
    }
    #edit-device-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #edit-device-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #edit-device-dialog Input {
        margin-bottom: 0;
    }
    #edit-device-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #edit-device-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, entry: "DeviceEntry") -> None:
        super().__init__()
        self._entry = entry
        self._current_password = self._read_password(entry)

    @staticmethod
    def _read_password(entry: "DeviceEntry") -> str:
        try:
            pw_file = Path(entry.config_path).parent / "password"
            if pw_file.is_file():
                return pw_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return ""

    def compose(self) -> ComposeResult:
        e = self._entry
        with Vertical(id="edit-device-dialog"):
            yield Static(f"Edit Device: [b]{e.name}[/b]", classes="title")
            yield Label("Host / IP")
            yield Input(value=e.host, id="inp-edit-host")
            yield Label("NETCONF port")
            yield Input(value=str(e.netconf_port), id="inp-edit-port")
            yield Label("Username")
            yield Input(value=e.username, id="inp-edit-username")
            yield Label("Password (leave blank to keep current)")
            yield Input(password=True, value="", id="inp-edit-password")
            yield Label("gNMI server port")
            yield Input(value=str(e.gnmi_port), id="inp-edit-gnmi-port")
            yield Label("Health port")
            yield Input(value=str(e.health_port), id="inp-edit-health-port")
            yield Label("Database URL")
            yield Input(value=e.db_url, id="inp-edit-db-url")
            with Horizontal(id="edit-device-buttons"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-edit-cancel")

    @on(Button.Pressed, "#btn-save")
    def on_save(self) -> None:
        host = self.query_one("#inp-edit-host", Input).value.strip()
        if not host:
            self.notify("Host is required", severity="error")
            return
        port = _parse_port(self.query_one("#inp-edit-port", Input).value, 830)
        gnmi_port = _parse_port(self.query_one("#inp-edit-gnmi-port", Input).value, 9339)
        health_port = _parse_port(self.query_one("#inp-edit-health-port", Input).value, 9809)
        if port is None or gnmi_port is None or health_port is None:
            self.notify("Ports must be integers between 1 and 65535", severity="error")
            return
        password = self.query_one("#inp-edit-password", Input).value
        self.dismiss(
            {
                "host": host,
                "port": port,
                "username": self.query_one("#inp-edit-username", Input).value.strip() or "admin",
                "password": password if password else None,
                "gnmi_port": gnmi_port,
                "health_port": health_port,
                "db_url": self.query_one("#inp-edit-db-url", Input).value.strip(),
            }
        )

    @on(Button.Pressed, "#btn-edit-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Devices screen
# ---------------------------------------------------------------------------

class DevicesScreen(Static):
    """Main devices view — a DataTable of all registered devices."""

    DEFAULT_CSS = """
    DevicesScreen {
        height: 1fr;
    }
    DevicesScreen DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("a", "add_device", "Add device"),
        Binding("e", "edit_device", "Edit device"),
        Binding("v", "view_config", "Config"),
        Binding("d", "delete_device", "Delete device"),
        Binding("t", "test_device", "Test connectivity"),
        Binding("r", "refresh", "Refresh list"),
    ]

    def __init__(self, registry: DeviceRegistry) -> None:
        super().__init__()
        self._registry = registry

    def compose(self) -> ComposeResult:
        table = DataTable(id="devices-table")
        table.cursor_type = "row"
        table.zebra_stripes = True
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#devices-table", DataTable)
        table.add_columns("Name", "Host", "NETCONF", "gNMI", "User", "DB URL")
        self._refresh_table()
        table.focus()

    def _refresh_table(self) -> None:
        table = self.query_one("#devices-table", DataTable)
        table.clear()
        for entry in self._registry.scan():
            db_display = entry.db_url
            if len(db_display) > 50:
                db_display = "..." + db_display[-47:]
            table.add_row(
                entry.name,
                entry.host,
                str(entry.netconf_port),
                str(entry.gnmi_port),
                entry.username,
                db_display,
                key=entry.name,
            )

    def _get_selected_name(self) -> str | None:
        table = self.query_one("#devices-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    # -- Actions -----------------------------------------------------------

    def action_add_device(self) -> None:
        self.app.push_screen(AddDeviceModal(), callback=self._on_add_result)

    def _on_add_result(self, result: dict | None) -> None:
        if result is None:
            return
        try:
            self._registry.add(
                name=result["name"],
                host=result["host"],
                port=result["port"],
                username=result["username"],
                password=result["password"],
                gnmi_port=result["gnmi_port"],
                db_url=result["db_url"],
            )
            self._refresh_table()
            self.notify(f"Device '{result['name']}' created")
        except Exception as exc:
            self.notify(f"Failed to add device: {exc}", severity="error")

    def action_edit_device(self) -> None:
        name = self._get_selected_name()
        if name is None:
            self.notify("No device selected", severity="warning")
            return
        entry = self._registry.get(name)
        if entry is None:
            self.notify(f"Device '{name}' not found", severity="error")
            return
        self._pending_edit_name = name
        self.app.push_screen(
            EditDeviceModal(entry), callback=self._on_edit_result
        )

    def _on_edit_result(self, result: dict | None) -> None:
        if result is None:
            return
        name = self._pending_edit_name
        if name is None:
            return
        try:
            self._registry.update(
                name,
                host=result["host"],
                port=result["port"],
                username=result["username"],
                password=result["password"],
                gnmi_port=result["gnmi_port"],
                db_url=result["db_url"],
                health_port=result["health_port"],
            )
            self._refresh_table()
            self.notify(f"Device '{name}' updated")
        except Exception as exc:
            self.notify(f"Failed to update device: {exc}", severity="error")

    def action_view_config(self) -> None:
        name = self._get_selected_name()
        if name is None:
            self.notify("No device selected", severity="warning")
            return
        entry = self._registry.get(name)
        if entry is None:
            self.notify(f"Device '{name}' not found", severity="error")
            return
        from splice.tui.screens.config_viewer import ConfigViewerModal

        self.app.push_screen(
            ConfigViewerModal(entry.config_path, name),
            callback=self._on_config_closed,
        )

    def _on_config_closed(self, modified: bool) -> None:
        if modified:
            self._refresh_table()

    def action_delete_device(self) -> None:
        name = self._get_selected_name()
        if name is None:
            self.notify("No device selected", severity="warning")
            return
        self._pending_delete_name = name
        self.app.push_screen(
            ConfirmDeleteModal(name), callback=self._on_delete_result
        )

    def _on_delete_result(self, confirmed: bool) -> None:
        if not confirmed:
            return
        name = self._pending_delete_name
        if name and self._registry.remove(name):
            self._refresh_table()
            self.notify(f"Device '{name}' removed")
            # Clear selection if deleted device was selected
            from splice.tui.app import SpliceApp

            app = self.app
            if isinstance(app, SpliceApp) and app.selected_device == name:
                app.selected_device = ""

    def action_test_device(self) -> None:
        name = self._get_selected_name()
        if name is None:
            self.notify("No device selected", severity="warning")
            return
        self._run_test(name)

    @work(thread=True)
    def _run_test(self, name: str) -> None:
        ok, msg = self._registry.test_connectivity(name)
        self.app.call_from_thread(
            self.notify,
            msg,
            severity="information" if ok else "error",
        )

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Select device when Enter is pressed on a table row."""
        name = str(event.row_key.value)
        from splice.tui.app import SpliceApp

        app = self.app
        if isinstance(app, SpliceApp):
            app.selected_device = name
            self.notify(
                f"Selected [bold]{name}[/bold] — press 2 for Paths, 3 for Telemetry, 4 for Status",
                title="Device selected",
            )

    def action_refresh(self) -> None:
        self._refresh_table()
        self.notify("Refreshed")
