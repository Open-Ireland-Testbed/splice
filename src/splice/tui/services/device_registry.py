# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Device registry — scans devices/ directory and manages device configs."""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class DeviceEntry:
    """Lightweight representation of a registered device."""

    name: str
    config_path: str
    host: str
    netconf_port: int
    gnmi_port: int
    username: str
    db_url: str
    yang_dir: str
    health_port: int = 9809


def _validate_name(name: str) -> None:
    """Reject device names that could escape the registry directory."""
    if not name or '/' in name or '\\' in name or '..' in name or name == '.':
        raise ValueError(f"Invalid device name: {name!r}")


class DeviceRegistry:
    """Scans and manages devices in a devices/ directory.

    Each device is a subdirectory containing a ``config.yaml`` file.
    """

    def __init__(self, base_dir: str = "devices") -> None:
        self._base_dir = Path(base_dir).resolve()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def scan(self) -> list[DeviceEntry]:
        """Walk ``devices/*/config.yaml`` and return parsed entries."""
        entries: list[DeviceEntry] = []
        if not self._base_dir.is_dir():
            return entries

        for child in sorted(self._base_dir.iterdir()):
            if not child.is_dir():
                continue
            config_path = child / "config.yaml"
            if not config_path.is_file():
                continue
            entry = self._load_entry(child.name, str(config_path))
            if entry is not None:
                entries.append(entry)
        return entries

    def get(self, name: str) -> DeviceEntry | None:
        """Load a single device by name."""
        _validate_name(name)
        config_path = self._base_dir / name / "config.yaml"
        if not config_path.is_file():
            return None
        return self._load_entry(name, str(config_path))

    def add(
        self,
        name: str,
        host: str,
        port: int = 830,
        username: str = "admin",
        password: str = "",
        gnmi_port: int = 9339,
        db_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/splice",
    ) -> DeviceEntry:
        """Create a new device directory with config.yaml and password file."""
        _validate_name(name)
        device_dir = self._base_dir / name
        device_dir.mkdir(parents=True, exist_ok=True)
        yang_dir = device_dir / "yang"
        yang_dir.mkdir(parents=True, exist_ok=True)

        # Write password file
        password_file = device_dir / "password"
        password_file.write_text(password + "\n", encoding="utf-8")
        password_file.chmod(0o600)

        config = {
            "device": {
                "host": host,
                "port": port,
                "username": username,
                "password_file": str(password_file),
                "name": name,
            },
            "db": {"url": db_url},
            "yang_dir": str(yang_dir.resolve()),
            "gnmi": {
                "bind": "0.0.0.0",
                "port": gnmi_port,
                "heartbeat_sec": 10,
                "expansion_mode": "direct",
                "max_expanded_paths_per_base": 10000,
                "max_total_expanded_paths": 100000,
            },
            "poller": {
                "default_interval_sec": 30,
                "batch_enabled": True,
                "batch_mode": "top",
                "preserve_namespaces": False,
                "rules": [],
            },
            "discovery": {
                "resolve_all_lists": False,
                "list_batch_size": 50,
                "list_sleep_sec": 0.5,
            },
            "cache": {"ttl_sec": 60, "warm_start": False},
            "health": {"enabled": True, "bind": "0.0.0.0", "port": 9809},
        }

        config_path = device_dir / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return DeviceEntry(
            name=name,
            config_path=str(config_path),
            host=host,
            netconf_port=port,
            gnmi_port=gnmi_port,
            username=username,
            db_url=db_url,
            yang_dir=str(yang_dir.resolve()),
            health_port=9809,
        )

    def update(
        self,
        name: str,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        gnmi_port: int | None = None,
        db_url: str | None = None,
        health_port: int | None = None,
    ) -> DeviceEntry | None:
        """Update an existing device's config.yaml, patching only provided fields."""
        _validate_name(name)
        config_path = self._base_dir / name / "config.yaml"
        if not config_path.is_file():
            return None

        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        raw.setdefault("device", {})
        raw.setdefault("gnmi", {})
        raw.setdefault("db", {})
        raw.setdefault("health", {})

        if host is not None:
            raw["device"]["host"] = host
        if port is not None:
            raw["device"]["port"] = port
        if username is not None:
            raw["device"]["username"] = username
        if gnmi_port is not None:
            raw["gnmi"]["port"] = gnmi_port
        if db_url is not None:
            raw["db"]["url"] = db_url
        if health_port is not None:
            raw["health"]["port"] = health_port

        if password is not None and password != "":
            pw_file = self._base_dir / name / "password"
            pw_file.write_text(password + "\n", encoding="utf-8")
            pw_file.chmod(0o600)
            raw["device"]["password_file"] = str(pw_file)

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

        return self.get(name)

    def remove(self, name: str) -> bool:
        """Remove a device directory. Returns True if removed."""
        _validate_name(name)
        device_dir = self._base_dir / name
        if not device_dir.is_dir():
            return False
        shutil.rmtree(device_dir)
        return True

    def test_connectivity(self, name: str, timeout: float = 5.0) -> tuple[bool, str]:
        """Test TCP connectivity to a device's NETCONF port."""
        _validate_name(name)
        entry = self.get(name)
        if entry is None:
            return False, f"Device '{name}' not found"
        try:
            with socket.create_connection(
                (entry.host, entry.netconf_port), timeout=timeout
            ):
                pass
            return True, f"Connected to {entry.host}:{entry.netconf_port}"
        except OSError as exc:
            return False, f"Cannot reach {entry.host}:{entry.netconf_port}: {exc}"

    # ------------------------------------------------------------------

    def _load_entry(self, name: str, config_path: str) -> DeviceEntry | None:
        """Parse a config.yaml into a DeviceEntry, returning None on error."""
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except Exception as exc:
            import logging

            logging.getLogger(__name__).debug(
                "Failed to load device config %s: %s", config_path, exc
            )
            return None

        dev = raw.get("device", {})
        gnmi = raw.get("gnmi", {})
        db = raw.get("db", {})
        health = raw.get("health", {})

        host = dev.get("host", "")
        if not host:
            return None

        return DeviceEntry(
            name=name,
            config_path=os.path.abspath(config_path),
            host=str(host),
            netconf_port=int(dev.get("port", 830)),
            gnmi_port=int(gnmi.get("port", 9339)),
            username=str(dev.get("username", "netconf")),
            db_url=str(db.get("url", "")),
            yang_dir=str(raw.get("yang_dir", "")),
            health_port=int(health.get("port", 9809)),
        )
