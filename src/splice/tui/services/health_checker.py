# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Health checker — queries health endpoint, TCP probes, and DB counts."""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.request import urlopen
from urllib.error import URLError

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceEntry

log = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """Aggregated health status for a device's bridge instance."""

    gnmi_reachable: bool = False
    gnmi_port: int = 0
    health_endpoint_ok: bool = False
    health_detail: dict = field(default_factory=dict)
    netconf_reachable: bool = False
    db_reachable: bool = False
    catalog_path_count: int = 0
    job_count: int = 0
    module_count: int = 0
    list_instance_count: int = 0
    error: str | None = None


class HealthChecker:
    """Run health checks against a device's bridge infrastructure."""

    def check(self, entry: DeviceEntry) -> HealthStatus:
        """Run all health checks. Call from @work(thread=True)."""
        status = HealthStatus(gnmi_port=entry.gnmi_port)

        # Health endpoint (HTTP)
        self._check_health_endpoint(entry, status)

        # TCP probes
        status.gnmi_reachable = self._tcp_probe("localhost", entry.gnmi_port)
        status.netconf_reachable = self._tcp_probe(entry.host, entry.netconf_port)

        # DB counts
        if entry.db_url:
            self._check_db(entry, status)

        return status

    def _check_health_endpoint(self, entry: DeviceEntry, status: HealthStatus) -> None:
        """Query the /health HTTP endpoint."""
        try:
            url = f"http://localhost:{entry.health_port}/health"
            with urlopen(url, timeout=3) as resp:
                body = resp.read().decode("utf-8")
                status.health_detail = json.loads(body)
                status.health_endpoint_ok = resp.status == 200
        except (URLError, OSError, json.JSONDecodeError) as exc:
            log.debug("Health endpoint check failed: %s", exc)
            status.health_endpoint_ok = False

    def _tcp_probe(self, host: str, port: int, timeout: float = 3.0) -> bool:
        """Test TCP connectivity."""
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _check_db(self, entry: DeviceEntry, status: HealthStatus) -> None:
        """Query DB for resource counts."""
        try:
            from sqlalchemy import func, select

            from splice.db.models import (
                CatalogPath,
                Job,
                ListInstance,
                Module,
            )
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)

                status.db_reachable = True

                if device is None:
                    return

                dev_id = device.id

                status.catalog_path_count = session.execute(
                    select(func.count()).select_from(CatalogPath).where(
                        CatalogPath.device_id == dev_id
                    )
                ).scalar() or 0

                status.job_count = session.execute(
                    select(func.count()).select_from(Job).where(
                        Job.device_id == dev_id
                    )
                ).scalar() or 0

                status.module_count = session.execute(
                    select(func.count()).select_from(Module).where(
                        Module.device_id == dev_id
                    )
                ).scalar() or 0

                status.list_instance_count = session.execute(
                    select(func.count()).select_from(ListInstance).where(
                        ListInstance.device_id == dev_id
                    )
                ).scalar() or 0

            finally:
                session.close()
        except Exception as exc:
            log.debug("DB health check failed: %s", exc)
            status.db_reachable = False
