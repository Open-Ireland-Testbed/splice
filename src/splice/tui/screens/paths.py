# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Paths screen — tree browser with detail panel and search/filter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Tree
from textual.widgets.tree import TreeNode as TextualTreeNode

from splice.path_index import PathEntry, TreeNode, build_tree, is_search_available

if TYPE_CHECKING:
    from splice.tui.services.catalog_loader import CatalogLoader, CatalogResult
    from splice.tui.services.device_registry import DeviceRegistry

log = logging.getLogger(__name__)


def _node_label(node: TreeNode) -> str:
    """Build a Rich-markup label for a tree node."""
    seg = node.segment
    if not node.entry:
        return f"[dim]{seg}[/dim]"
    kw = node.entry.keyword
    if kw == "container":
        return f"[dim]{seg}[/dim]"
    elif kw == "list":
        return f"[cyan]{seg}[/cyan]"
    elif kw == "leaf-list":
        return f"[green]{seg}[/green]"
    else:
        return seg


def _has_loading_sentinel(text_node: TextualTreeNode) -> bool:
    """Return True when the node still contains only the lazy-load placeholder."""
    return [child.label.plain for child in text_node.children] == ["..."]


def _detail_text(node: TreeNode) -> str:
    """Build detail panel text for a tree node."""
    entry = node.entry
    if entry is None:
        child_count = len(node.children)
        return (
            f"[bold]Path:[/bold]         {node.full_path}\n"
            f"[bold]Children:[/bold]     {child_count}"
        )

    lines = [f"[bold]Path:[/bold]         {entry.path}"]
    if entry.module:
        lines.append(f"[bold]Module:[/bold]       {entry.module}")
    lines.append(f"[bold]Keyword:[/bold]      {entry.keyword}")
    if entry.data_type:
        lines.append(f"[bold]Type:[/bold]         {entry.data_type}")
    lines.append(f"[bold]Config:[/bold]       {entry.config}")
    if entry.list_keys:
        lines.append(f"[bold]List keys:[/bold]    {', '.join(entry.list_keys)}")
    if entry.description:
        lines.append(f"[bold]Description:[/bold]  {entry.description}")
    return "\n".join(lines)


class WatchPath(Message):
    """Posted when user wants to watch a path in the telemetry tab."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path


# ---------------------------------------------------------------------------
# Set Config modal
# ---------------------------------------------------------------------------

class SetConfigModal(ModalScreen[dict | None]):
    """Modal form for setting a config value on a NETCONF device."""

    DEFAULT_CSS = """
    SetConfigModal {
        align: center middle;
    }
    #set-config-dialog {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #set-config-dialog Label {
        margin-top: 1;
        color: $text-muted;
    }
    #set-config-dialog Input {
        margin-bottom: 0;
    }
    #set-config-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }
    #set-config-buttons Button {
        margin: 0 1;
    }
    #set-config-result {
        margin-top: 1;
        height: auto;
        max-height: 6;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, path: str, device_name: str, registry: "DeviceRegistry") -> None:
        super().__init__()
        self._path = path
        self._device_name = device_name
        self._registry = registry

    def compose(self) -> ComposeResult:
        with Vertical(id="set-config-dialog"):
            yield Static(f"Set Config: [b]{self._device_name}[/b]", classes="title")
            yield Label("Path")
            yield Input(value=self._path, id="inp-set-path", disabled=True)
            yield Label("Value")
            yield Input(placeholder="Enter value to set", id="inp-set-value")
            yield Label("Operation")
            yield Select(
                [("merge", "merge"), ("replace", "replace"), ("delete", "delete")],
                value="merge",
                id="sel-set-operation",
            )
            yield Static("", id="set-config-result")
            with Horizontal(id="set-config-buttons"):
                yield Button("Apply", variant="primary", id="btn-set-apply")
                yield Button("Cancel", variant="default", id="btn-set-cancel")

    @on(Button.Pressed, "#btn-set-apply")
    def on_apply(self) -> None:
        value = self.query_one("#inp-set-value", Input).value.strip()
        if not value:
            self.notify("Value is required", severity="error")
            return
        operation = self.query_one("#sel-set-operation", Select).value
        if operation is Select.BLANK:
            operation = "merge"
        # Disable button to prevent double-click
        self.query_one("#btn-set-apply", Button).disabled = True
        self.query_one("#set-config-result", Static).update("[dim]Applying...[/dim]")
        self._do_set(self._path, value, str(operation))

    @work(thread=True)
    def _do_set(self, path: str, value: str, operation: str) -> None:
        """Execute the set operation via the Python API Device class."""
        try:
            entry = self._registry.get(self._device_name)
            if entry is None:
                self.app.call_from_thread(self._show_result, False, "Device not found")
                return

            # Read password from the device's password file
            password = ""
            pw_file = Path(entry.config_path).parent / "password"
            if pw_file.is_file():
                password = pw_file.read_text(encoding="utf-8").strip()

            from splice.api.device import Device

            dev = Device(
                host=entry.host,
                port=entry.netconf_port,
                username=entry.username,
                password=password,
            )
            dev.connect()
            try:
                result = dev.set(path, value, operation=operation)
                msg = f"Set OK: {result.get('path', path)} ({result.get('operation', operation)})"
                self.app.call_from_thread(self._show_result, True, msg)
            finally:
                dev.close()

        except Exception as exc:
            self.app.call_from_thread(self._show_result, False, str(exc))

    def _show_result(self, success: bool, message: str) -> None:
        try:
            self.query_one("#btn-set-apply", Button).disabled = False
            result_widget = self.query_one("#set-config-result", Static)
        except Exception:
            # Modal was dismissed before worker completed
            return
        if success:
            result_widget.update(f"[green]{message}[/green]")
            self.notify(message, severity="information")
        else:
            result_widget.update(f"[red]Error: {message}[/red]")
            self.notify(f"Set failed: {message}", severity="error")

    @on(Button.Pressed, "#btn-set-cancel")
    def on_cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PathsScreen(Static):
    """Path browser with tree navigation, detail panel, and search."""

    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "copy_path", "Copy path"),
        Binding("f", "toggle_config", "Config only"),
        Binding("w", "watch_path", "Watch"),
        Binding("s", "set_config", "Set config"),
    ]

    def __init__(
        self,
        registry: DeviceRegistry,
        catalog_loader: CatalogLoader,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._catalog_loader = catalog_loader
        self._current_device: str = ""
        self._catalog_result: CatalogResult | None = None
        self._config_only: bool = False
        self._search_timer: object | None = None
        self._path_index = None  # Optional PathIndex for semantic search
        self._load_generation: int = 0  # incremented on device change

    def compose(self) -> ComposeResult:
        with Vertical(id="paths-root"):
            with Horizontal(id="path-toolbar"):
                yield Input(
                    placeholder="Filter paths... (prefix / for semantic search)",
                    id="path-search",
                )
                yield Static("No device", id="path-stats")
            with Horizontal(id="path-body"):
                with Vertical(id="tree-panel"):
                    yield Static(
                        "Select a device first (press 1)",
                        id="path-empty-msg",
                        classes="empty-message",
                    )
                    tree = Tree("/", id="path-tree")
                    tree.display = False
                    yield tree
                yield Vertical(
                    Static("", id="path-detail"),
                    id="detail-panel",
                )

    # -- Device change -------------------------------------------------------

    def on_device_changed(self, name: str) -> None:
        """Called by app when the selected device changes."""
        if not name:
            # Device was cleared (e.g. deleted) — reset screen
            self._current_device = ""
            self._catalog_result = None
            self._path_index = None
            self._load_generation += 1
            self._show_empty("Select a device first (press 1)")
            return
        if name == self._current_device:
            return
        self._current_device = name
        self._path_index = None  # clear stale semantic index from previous device
        self._load_generation += 1
        self._load_catalog()

    @work(thread=True)
    def _load_catalog(self) -> None:
        """Load catalog in background thread."""
        gen = self._load_generation
        name = self._current_device
        entry = self._registry.get(name)
        if entry is None:
            if self._load_generation == gen:
                self.app.call_from_thread(self._show_empty, "Device not found")
            return

        if self._load_generation == gen:
            self.app.call_from_thread(self._show_loading)

        result = self._catalog_loader.load(entry)

        # Discard stale result if device changed during load
        if self._load_generation != gen:
            return

        self._catalog_result = result

        if result.path_count == 0:
            msg = "No catalog paths found"
            if result.error:
                msg += f"\n{result.error}"
            else:
                msg += "\nRun `splice setup` to discover paths"
            self.app.call_from_thread(self._show_empty, msg)
            return

        self.app.call_from_thread(self._populate_tree, result)

        # Build semantic index in background if available
        if is_search_available() and result.entries:
            self._build_search_index(result.entries)

    @work(thread=True)
    def _build_search_index(self, entries: list[PathEntry]) -> None:
        """Build semantic search index in background."""
        gen = self._load_generation
        try:
            from splice.path_index import PathIndex

            idx = PathIndex(entries)
            idx.build()
            if self._load_generation == gen:
                self._path_index = idx
            # else: discard, device changed during build
        except Exception:
            log.debug("Failed to build semantic search index", exc_info=True)

    # -- UI state transitions ------------------------------------------------

    def _show_loading(self) -> None:
        """Show loading state — hide tree, show empty message."""
        self.query_one("#path-tree", Tree).display = False
        empty_msg = self.query_one("#path-empty-msg", Static)
        empty_msg.update(f"Loading {self._current_device}...")
        empty_msg.display = True
        self.query_one("#path-stats", Static).update(f"Loading {self._current_device}...")
        self.query_one("#path-detail", Static).update("")

    def _show_empty(self, message: str) -> None:
        """Show an empty-state message in the tree area."""
        self.query_one("#path-tree", Tree).display = False
        empty_msg = self.query_one("#path-empty-msg", Static)
        empty_msg.update(message)
        empty_msg.display = True
        self.query_one("#path-stats", Static).update(self._current_device or "No device")
        self.query_one("#path-detail", Static).update("")

    def _populate_tree(self, result: CatalogResult) -> None:
        """Build the Textual Tree widget from CatalogResult."""
        # Hide empty message, show tree
        self.query_one("#path-empty-msg", Static).display = False

        tree_widget = self.query_one("#path-tree", Tree)
        tree_widget.clear()
        tree_widget.root.data = result.tree
        tree_widget.root.expand()

        # Add top-level children
        for seg in sorted(result.tree.children):
            child_node = result.tree.children[seg]
            label = _node_label(child_node)
            has_children = bool(child_node.children)
            text_node = tree_widget.root.add(
                label, data=child_node, allow_expand=has_children
            )
            if has_children:
                # Sentinel so the expand arrow shows
                text_node.add_leaf("...")

        tree_widget.display = True
        source_label = f"[{result.source}]" if result.source != "none" else ""
        self.query_one("#path-stats", Static).update(
            f"{self._current_device}: {result.path_count} paths {source_label}"
        )

    # -- Tree events ---------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Lazy-load children when a node is expanded."""
        text_node: TextualTreeNode = event.node
        data: TreeNode | None = text_node.data

        if data is None:
            return

        # Remove sentinel children (the "..." placeholder)
        if _has_loading_sentinel(text_node):
            text_node.remove_children()

            for seg in sorted(data.children):
                child_data = data.children[seg]
                if self._config_only and child_data.entry and not child_data.entry.config:
                    continue
                label = _node_label(child_data)
                has_children = bool(child_data.children)
                new_node = text_node.add(
                    label, data=child_data, allow_expand=has_children
                )
                if has_children:
                    new_node.add_leaf("...")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """Update detail panel when a node is highlighted."""
        text_node: TextualTreeNode = event.node
        data: TreeNode | None = text_node.data
        if data is None:
            self.query_one("#path-detail", Static).update("")
            return
        self.query_one("#path-detail", Static).update(_detail_text(data))

    # -- Search / filter -----------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Debounced search handler."""
        if event.input.id != "path-search":
            return
        # Cancel previous timer
        if self._search_timer is not None:
            try:
                self._search_timer.stop()
            except Exception:
                log.debug("Failed to stop search timer")
        self._search_timer = self.set_timer(0.3, self._run_search)

    def _run_search(self) -> None:
        """Execute the search/filter."""
        result = self._catalog_result
        if result is None or result.path_count == 0:
            return

        query = self.query_one("#path-search", Input).value.strip()

        if not query:
            # Reset to full tree
            self._populate_tree(result)
            return

        # Semantic search with / prefix
        if query.startswith("/") and len(query) > 1:
            if self._path_index is None:
                self._show_empty("Building semantic index... try again in a few seconds")
                return
            semantic_query = query[1:]
            try:
                results = self._path_index.search(semantic_query, top_k=30)
                filtered_entries = [entry for entry, _score in results]
            except Exception:
                log.debug("Semantic search failed", exc_info=True)
                filtered_entries = []
        else:
            # Substring filter
            filtered_entries = [
                e for e in result.entries if query.lower() in e.path.lower()
            ]

        if not filtered_entries:
            self._show_empty(f"No paths matching '{query}'")
            return

        from splice.tui.services.catalog_loader import CatalogResult

        filtered_tree = build_tree(filtered_entries)
        filtered_result = CatalogResult(
            entries=filtered_entries,
            tree=filtered_tree,
            source=result.source,
            path_count=len(filtered_entries),
        )
        self._populate_tree(filtered_result)

    # -- Actions -------------------------------------------------------------

    def action_focus_search(self) -> None:
        search_input = self.query_one("#path-search", Input)
        search_input.value = "/"
        search_input.cursor_position = 1
        search_input.focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#path-search", Input)
        search.value = ""
        search.blur()
        if self._catalog_result:
            self._populate_tree(self._catalog_result)

    def action_refresh(self) -> None:
        if not self._current_device:
            return
        self._catalog_loader.clear(self._current_device)
        self._path_index = None
        self._load_catalog()

    def _get_selected_path(self) -> str | None:
        """Return the path string of the currently highlighted tree node."""
        try:
            tree_widget = self.query_one("#path-tree", Tree)
            node = tree_widget.cursor_node
            if node and node.data and isinstance(node.data, TreeNode):
                return node.data.entry.path if node.data.entry else node.data.full_path
        except Exception:
            log.debug("No path tree available")
        return None

    def action_copy_path(self) -> None:
        path = self._get_selected_path()
        if path is None:
            self.notify("No path selected", severity="warning")
            return

        # Try pyperclip first, then xclip fallback
        try:
            import pyperclip

            pyperclip.copy(path)
            self.notify(f"Copied: {path}")
            return
        except ImportError:
            pass

        try:
            import subprocess

            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=path.encode(),
                check=True,
            )
            self.notify(f"Copied: {path}")
            return
        except Exception:
            log.debug("xclip copy failed", exc_info=True)

        self.notify("Copy requires pyperclip or xclip", severity="warning")

    def action_watch_path(self) -> None:
        path = self._get_selected_path()
        if path is None:
            self.notify("No path selected", severity="warning")
            return
        self.post_message(WatchPath(path))

    def action_set_config(self) -> None:
        path = self._get_selected_path()
        if path is None:
            self.notify("No path selected", severity="warning")
            return
        if not self._current_device:
            self.notify("No device selected", severity="warning")
            return
        self.app.push_screen(
            SetConfigModal(path, self._current_device, self._registry)
        )

    def action_toggle_config(self) -> None:
        self._config_only = not self._config_only
        label = "config-only ON" if self._config_only else "config-only OFF"
        self.notify(label)
        if self._catalog_result and self._catalog_result.path_count > 0:
            if self._config_only:
                filtered = [e for e in self._catalog_result.entries if e.config]
                from splice.tui.services.catalog_loader import CatalogResult

                filtered_tree = build_tree(filtered)
                self._populate_tree(CatalogResult(
                    entries=filtered,
                    tree=filtered_tree,
                    source=self._catalog_result.source,
                    path_count=len(filtered),
                ))
            else:
                self._populate_tree(self._catalog_result)
