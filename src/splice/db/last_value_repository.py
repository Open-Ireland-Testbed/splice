# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from splice.db.models import LastValue
from splice.pathing import is_path_prefix, normalize_path


def upsert_last_values(session: Session, device_id: int, updates: Iterable[Dict[str, object]]) -> int:
    count = 0
    for u in updates:
        path = u.get("path")
        if not path:
            continue
        stmt = select(LastValue).where(
            LastValue.device_id == device_id,
            LastValue.canonical_path == path,
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = LastValue(
                device_id=device_id,
                canonical_path=str(path),
                value_json=u.get("value"),
                timestamp_ns=int(u.get("timestamp_ns", 0) or 0),
                meta=u.get("meta"),
            )
            session.add(row)
            count += 1
        else:
            row.value_json = u.get("value")
            row.timestamp_ns = int(u.get("timestamp_ns", 0) or 0)
            row.meta = u.get("meta")
            count += 1
    session.commit()
    return count


def fetch_last_values(session: Session, device_id: int, prefix: str = "") -> Dict[str, Dict[str, object]]:
    stmt = select(LastValue).where(LastValue.device_id == device_id)
    rows = session.execute(stmt).scalars().all()
    out: Dict[str, Dict[str, object]] = {}
    for r in rows:
        if prefix and not is_path_prefix(r.canonical_path, prefix):
            continue
        out[r.canonical_path] = {
            "path": r.canonical_path,
            "value": r.value_json,
            "timestamp_ns": r.timestamp_ns,
            "meta": r.meta,
        }
    return out


def delete_last_values(
    session: Session,
    device_id: int,
    paths: Iterable[str],
    include_descendants: bool = True,
) -> int:
    targets = [normalize_path(p) for p in paths if p]
    if not targets:
        return 0

    rows = session.execute(
        select(LastValue).where(LastValue.device_id == device_id)
    ).scalars().all()

    to_delete = []
    for row in rows:
        row_norm = normalize_path(row.canonical_path)
        for target in targets:
            if row_norm == target or (
                include_descendants and row_norm.startswith(target + "/")
            ):
                to_delete.append(row)
                break

    for row in to_delete:
        session.delete(row)
    if to_delete:
        session.commit()
    return len(to_delete)
