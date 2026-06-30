# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Job service — CRUD and bulk operations on polling jobs via DB."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceEntry

log = logging.getLogger(__name__)


@dataclass
class JobInfo:
    """Presentation-layer representation of a polling job."""

    id: int
    canonical_path: str
    interval_sec: int
    enabled: bool
    mode: str
    updated_at: datetime | None


class JobService:
    """Backend service for job management. All methods are synchronous
    and should be called from ``@work(thread=True)`` workers."""

    def fetch_jobs(self, entry: DeviceEntry) -> list[JobInfo]:
        """Fetch all jobs for a device, ordered by path."""
        try:
            from sqlalchemy import select

            from splice.db.models import Job
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)
                if device is None:
                    return []

                rows = (
                    session.execute(
                        select(Job)
                        .where(Job.device_id == device.id)
                        .order_by(Job.canonical_path)
                    )
                    .scalars()
                    .all()
                )
                return [
                    JobInfo(
                        id=r.id,
                        canonical_path=r.canonical_path,
                        interval_sec=r.interval_sec,
                        enabled=r.enabled,
                        mode=r.mode,
                        updated_at=r.updated_at,
                    )
                    for r in rows
                ]
            finally:
                session.close()
        except Exception as exc:
            log.debug("Failed to fetch jobs: %s", exc)
            return []

    def update_job(
        self,
        entry: DeviceEntry,
        job_id: int,
        interval_sec: int | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Update a single job's interval and/or enabled state."""
        try:
            from sqlalchemy import select

            from splice.db.models import Job
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)
                if device is None:
                    return False

                job = session.execute(
                    select(Job).where(Job.id == job_id, Job.device_id == device.id)
                ).scalar_one_or_none()
                if job is None:
                    return False

                if interval_sec is not None:
                    job.interval_sec = interval_sec
                if enabled is not None:
                    job.enabled = enabled
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                return True
            finally:
                session.close()
        except Exception as exc:
            log.debug("Failed to update job %d: %s", job_id, exc)
            return False

    def bulk_enable(self, entry: DeviceEntry, enabled: bool) -> int:
        """Set enabled state on all jobs for a device. Returns count."""
        try:
            from sqlalchemy import update

            from splice.db.models import Job
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)
                if device is None:
                    return 0

                result = session.execute(
                    update(Job)
                    .where(Job.device_id == device.id)
                    .values(enabled=enabled)
                )
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                return result.rowcount
            finally:
                session.close()
        except Exception as exc:
            log.debug("Failed to bulk enable jobs: %s", exc)
            return 0

    def bulk_set_interval(self, entry: DeviceEntry, interval_sec: int) -> int:
        """Set interval on all enabled jobs for a device. Returns count."""
        try:
            from sqlalchemy import update

            from splice.db.models import Job
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(entry.db_url)
            try:
                device = lookup_device(session, entry)
                if device is None:
                    return 0

                result = session.execute(
                    update(Job)
                    .where(Job.device_id == device.id, Job.enabled.is_(True))
                    .values(interval_sec=interval_sec)
                )
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                return result.rowcount
            finally:
                session.close()
        except Exception as exc:
            log.debug("Failed to bulk set interval: %s", exc)
            return 0
