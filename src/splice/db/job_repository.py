# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from splice.db.models import Job


def fetch_job_intervals(session: Session, device_id: int) -> Dict[str, int]:
    rows = session.execute(
        select(Job).where(Job.device_id == device_id, Job.enabled.is_(True))
    ).scalars().all()
    return {r.canonical_path: int(r.interval_sec) for r in rows}


def upsert_jobs(
    session: Session,
    device_id: int,
    jobs: Dict[str, int],
    force_enable: bool = True,
) -> int:
    """Insert or update polling jobs.

    force_enable=True re-enables existing rows — right for explicit intent
    (set-job, metrics curation, trigger onboarding). Bulk re-initialization
    passes False so a job an operator manually disabled stays disabled across
    `setup`/`init-jobs` re-runs. New rows are always created enabled.
    """
    count = 0
    for path, interval in jobs.items():
        stmt = select(Job).where(
            Job.device_id == device_id,
            Job.canonical_path == path,
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = Job(
                device_id=device_id,
                canonical_path=path,
                interval_sec=int(interval),
                enabled=True,
                mode="sample",
            )
            session.add(row)
            count += 1
        else:
            row.interval_sec = int(interval)
            if force_enable:
                row.enabled = True
            count += 1
    session.commit()
    return count


def disable_jobs_except(
    session: Session, device_id: int, keep_paths: Iterable[str]
) -> int:
    """Disable (never delete) enabled jobs whose path is not curated. Keyed
    jobs survive when their predicate-stripped base path is curated."""
    from splice.expander import strip_predicates

    keep = set(keep_paths)
    rows = session.execute(
        select(Job).where(Job.device_id == device_id, Job.enabled.is_(True))
    ).scalars().all()
    count = 0
    for row in rows:
        path = row.canonical_path
        if path in keep or strip_predicates(path) in keep:
            continue
        row.enabled = False
        count += 1
    session.commit()
    return count
