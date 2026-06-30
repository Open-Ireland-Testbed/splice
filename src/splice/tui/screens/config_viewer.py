# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Config viewer — browse and edit device config.yaml sections."""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Static,
    Switch,
)
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field definitions — the editable fields per config section
# ---------------------------------------------------------------------------

@dataclass
class FieldDef:
    """Definition of one editable config field."""

    name: str
    type: str  # "int" | "float" | "bool" | "str"
    default: object
    label: str
    choices: list[str] | None = None  # for str fields with fixed choices


SECTION_FIELDS: dict[str, list[FieldDef]] = {
    "gnmi": [
        FieldDef("bind", "str", "0.0.0.0", "Bind address"),
        FieldDef("port", "int", 9339, "gNMI server port"),
        FieldDef("heartbeat_sec", "int", 10, "Heartbeat interval (sec)"),
        FieldDef("expansion_mode", "str", "direct", "Expansion mode",
                 choices=["direct", "full", "lazy"]),
        FieldDef("set_enabled", "bool", False, "Enable gNMI Set"),
        FieldDef("set_datastore", "str", "auto", "Set datastore",
                 choices=["auto", "running", "candidate"]),
        FieldDef("set_auto_refresh", "bool", False, "Auto-refresh after Set"),
        FieldDef("set_use_lock", "bool", False, "Use NETCONF lock for Set"),
    ],
    "poller": [
        FieldDef("default_interval_sec", "int", 30, "Default poll interval (sec)"),
        FieldDef("batch_enabled", "bool", True, "Enable batch polling"),
        FieldDef("batch_mode", "str", "top", "Batch mode",
                 choices=["top", "module"]),
        FieldDef("preserve_namespaces", "bool", False, "Preserve XML namespaces"),
        FieldDef("task_throttle_ms", "int", 50, "Throttle between tasks (ms)"),
        FieldDef("reconnect_delay_sec", "float", 2.0, "Reconnect delay (sec)"),
        FieldDef("circuit_breaker_threshold", "int", 5, "Circuit breaker threshold"),
        FieldDef("circuit_breaker_cooldown_sec", "float", 60.0, "CB cooldown (sec)"),
        FieldDef("auth_failure_cooldown_sec", "float", 300.0, "Auth failure cooldown (sec)"),
    ],
    "cache": [
        FieldDef("ttl_sec", "int", 60, "Cache TTL (sec)"),
        FieldDef("warm_start", "bool", True, "Warm start on boot"),
    ],
    "discovery": [
        FieldDef("inspect_components", "bool", False, "Inspect components"),
        FieldDef("component_limit", "int", 20, "Component limit"),
        FieldDef("resolve_all_lists", "bool", False, "Resolve all lists"),
        FieldDef("list_batch_size", "int", 50, "List batch size"),
        FieldDef("list_sleep_sec", "float", 0.5, "Sleep between list batches (sec)"),
        FieldDef("pm_discovery", "bool", False, "PM discovery"),
        FieldDef("pm_max_paths", "int", 25, "PM max paths"),
    ],
    "health": [
        FieldDef("enabled", "bool", True, "Enable health endpoint"),
        FieldDef("bind", "str", "0.0.0.0", "Health bind address"),
        FieldDef("port", "int", 9809, "Health port"),
    ],
    "history": [
        FieldDef("enabled", "bool", False, "Enable telemetry history"),
        FieldDef("storage", "str", "db", "Storage backend",
                 choices=["db", "file"]),
    ],
    "pm": [
        FieldDef("enabled", "bool", False, "Enable PM polling"),
        FieldDef("interval_sec", "int", 30, "PM poll interval (sec)"),
        FieldDef("path_prefix", "str", "/pm", "PM path prefix"),
    ],
}

SECTION_ORDER = ["gnmi", "poller", "cache", "discovery", "health", "history", "pm"]


# ---------------------------------------------------------------------------
# Edit-section modal
# ---------------------------------------------------------------------------

class EditSectionModal(ModalScreen[dict | None]):
    """Modal for editing a config section's fields."""

    DEFAULT_CSS = """
    EditSectionModal {
        align: center middle;
    }
    #edit-section-dialog {
        width: 65;
        height: auto;
        max-height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    #edit-section-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #edit-section-dialog Input {
        margin-bottom: 0;
    }
    .edit-bool-row {
        height: 3;
        align: left middle;
    }
    .edit-bool-row Label {
        margin-top: 0;
        margin-right: 1;
    }
    #edit-section-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #edit-section-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        section_name: str,
        section_data: dict,
        field_defs: list[FieldDef],
    ) -> None:
        super().__init__()
        self._section_name = section_name
        self._data = section_data
        self._fields = field_defs

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-section-dialog"):
            yield Static(
                f"Edit: [b]{self._section_name}[/b]", classes="title"
            )
            for fd in self._fields:
                current = self._data.get(fd.name, fd.default)
                widget_id = f"field-{fd.name}"

                if fd.type == "bool":
                    with Horizontal(classes="edit-bool-row"):
                        yield Label(fd.label)
                        yield Switch(value=bool(current), id=widget_id)
                elif fd.choices:
                    yield Label(f"{fd.label} ({'/'.join(fd.choices)})")
                    yield Input(value=str(current), id=widget_id)
                else:
                    yield Label(fd.label)
                    yield Input(value=str(current), id=widget_id)

            with Horizontal(id="edit-section-buttons"):
                yield Button("Save", variant="primary", id="btn-section-save")
                yield Button("Cancel", variant="default", id="btn-section-cancel")

    @on(Button.Pressed, "#btn-section-save")
    def on_save(self) -> None:
        result = {}
        for fd in self._fields:
            widget_id = f"field-{fd.name}"
            try:
                if fd.type == "bool":
                    result[fd.name] = self.query_one(f"#{widget_id}", Switch).value
                else:
                    raw = self.query_one(f"#{widget_id}", Input).value.strip()
                    if fd.type == "int":
                        result[fd.name] = int(raw)
                    elif fd.type == "float":
                        result[fd.name] = float(raw)
                    else:
                        if fd.choices and raw not in fd.choices:
                            self.notify(
                                f"{fd.label}: must be one of {fd.choices}",
                                severity="error",
                            )
                            return
                        result[fd.name] = raw
            except (ValueError, TypeError):
                self.notify(
                    f"{fd.label}: invalid {fd.type} value", severity="error"
                )
                return
        self.dismiss(result)

    @on(Button.Pressed, "#btn-section-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Config viewer modal
# ---------------------------------------------------------------------------

class ConfigViewerModal(ModalScreen[bool]):
    """Modal for viewing and editing a device's config.yaml."""

    DEFAULT_CSS = """
    ConfigViewerModal {
        align: center middle;
    }
    #config-viewer-dialog {
        width: 90;
        height: 85%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #config-body {
        height: 1fr;
    }
    #config-sections {
        width: 24;
        height: 1fr;
    }
    #config-detail {
        width: 1fr;
        height: 1fr;
        border-left: solid $primary;
        padding: 0 1;
        overflow-y: auto;
    }
    #config-section-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #config-section-body {
        height: auto;
        color: $text-muted;
    }
    #config-buttons {
        height: 3;
        align: center middle;
    }
    #config-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, config_path: str, device_name: str) -> None:
        super().__init__()
        self._config_path = config_path
        self._device_name = device_name
        self._raw: dict = {}
        self._selected_section: str = ""
        self._modified = False

    def compose(self) -> ComposeResult:
        with Vertical(id="config-viewer-dialog"):
            yield Static(
                f"Config: [b]{self._device_name}[/b]", classes="title"
            )
            with Horizontal(id="config-body"):
                yield OptionList(id="config-sections")
                with Vertical(id="config-detail"):
                    yield Static("Select a section", id="config-section-title")
                    yield Static("", id="config-section-body")
            with Horizontal(id="config-buttons"):
                yield Button(
                    "Edit Section", variant="primary", id="btn-edit-section"
                )
                yield Button("Close", variant="default", id="btn-config-close")

    def on_mount(self) -> None:
        self._load_config()
        if self._load_error:
            self.query_one("#config-section-title", Static).update(
                "[red]Failed to load config[/red]"
            )
            self.query_one("#config-section-body", Static).update(
                f"{self._load_error}\n\n"
                "Fix the config file before editing."
            )
            return

        option_list = self.query_one("#config-sections", OptionList)
        for section in SECTION_ORDER:
            present = section in self._raw
            label = f"{section} {'[dim](default)[/dim]' if not present else ''}"
            option_list.add_option(Option(label, id=section))

        # Auto-select first
        if SECTION_ORDER:
            self._selected_section = SECTION_ORDER[0]
            self._render_section(SECTION_ORDER[0])

    def _load_config(self) -> None:
        self._load_error: str | None = None
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._raw = yaml.safe_load(f) or {}
        except Exception as exc:
            log.warning("Failed to load config %s: %s", self._config_path, exc)
            self._raw = {}
            self._load_error = str(exc)

    def _render_section(self, section_name: str) -> None:
        self._selected_section = section_name
        self.query_one("#config-section-title", Static).update(
            f"[bold]{section_name}[/bold]"
        )

        section_data = self._raw.get(section_name, {})
        field_defs = SECTION_FIELDS.get(section_name)

        if field_defs is None:
            # Show raw YAML for sections without field defs
            if section_data:
                body = yaml.dump(section_data, default_flow_style=False)
            else:
                body = "[dim]Section not configured (using defaults)[/dim]"
            self.query_one("#config-section-body", Static).update(body)
            return

        lines = []
        for fd in field_defs:
            current = section_data.get(fd.name, fd.default)
            is_default = fd.name not in section_data

            if fd.type == "bool":
                val_display = "[green]true[/green]" if current else "[red]false[/red]"
            else:
                val_display = str(current)

            default_mark = " [dim](default)[/dim]" if is_default else ""
            lines.append(f"[bold]{fd.label}[/bold]")
            lines.append(f"  {fd.name}: {val_display}{default_mark}")
            lines.append("")

        self.query_one("#config-section-body", Static).update(
            "\n".join(lines) if lines else "[dim]No fields[/dim]"
        )

    @on(OptionList.OptionHighlighted)
    def on_section_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id:
            self._render_section(str(event.option.id))

    @on(Button.Pressed, "#btn-edit-section")
    def on_edit_section(self) -> None:
        if not self._selected_section:
            return
        if self._load_error:
            self.notify("Cannot edit — config failed to load", severity="error")
            return
        field_defs = SECTION_FIELDS.get(self._selected_section)
        if field_defs is None:
            self.notify(
                "This section cannot be edited here", severity="warning"
            )
            return
        section_data = self._raw.get(self._selected_section, {})
        self.app.push_screen(
            EditSectionModal(self._selected_section, section_data, field_defs),
            callback=self._on_edit_result,
        )

    def _on_edit_result(self, result: dict | None) -> None:
        if result is None:
            return
        # Save to config.yaml
        self._raw.setdefault(self._selected_section, {})
        self._raw[self._selected_section].update(result)
        try:
            # Atomic write: write to temp file, then rename
            config_dir = os.path.dirname(self._config_path)
            fd, tmp_path = tempfile.mkstemp(
                dir=config_dir, suffix=".yaml", prefix=".config_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    yaml.dump(self._raw, f, default_flow_style=False, sort_keys=False)
                os.replace(tmp_path, self._config_path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._modified = True
            self.notify(f"Saved {self._selected_section} config")
            self._render_section(self._selected_section)
        except Exception as exc:
            self.notify(f"Failed to save: {exc}", severity="error")

    @on(Button.Pressed, "#btn-config-close")
    def on_close_btn(self) -> None:
        self.dismiss(self._modified)

    def action_close(self) -> None:
        self.dismiss(self._modified)
