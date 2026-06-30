# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterable, List

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from splice.db.models import ListInstance


def fetch_list_instances(session: Session, device_id: int) -> Dict[str, List[Dict[str, str]]]:
    rows = session.execute(
        select(ListInstance).where(ListInstance.device_id == device_id)
    ).scalars().all()
    out: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        out.setdefault(r.list_path, []).append(dict(r.key_values))
    return out


def upsert_list_instances(
    session: Session,
    device_id: int,
    instances: Dict[str, Iterable[Dict[str, str]]],
) -> int:
    count = 0
    for list_path, entries in instances.items():
        for key_values in entries:
            stmt = select(ListInstance).where(
                ListInstance.device_id == device_id,
                ListInstance.list_path == list_path,
                ListInstance.key_values == key_values,
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                session.add(
                    ListInstance(
                        device_id=device_id,
                        list_path=list_path,
                        key_values=key_values,
                    )
                )
                count += 1
    if count:
        session.commit()
    return count


def replace_list_instances(
    session: Session,
    device_id: int,
    instances: Dict[str, Iterable[Dict[str, str]]],
    list_paths: Iterable[str] | None = None,
) -> int:
    paths = list(list_paths) if list_paths is not None else list(instances.keys())
    if paths:
        session.execute(
            delete(ListInstance).where(
                ListInstance.device_id == device_id,
                ListInstance.list_path.in_(paths),
            )
        )
    count = 0
    seen: set[tuple[str, str]] = set()
    for list_path, entries in instances.items():
        for key_values in entries:
            # Deduplicate: convert key_values to a hashable tuple for tracking
            key_tuple = (list_path, tuple(sorted(key_values.items())))
            if key_tuple in seen:
                continue
            seen.add(key_tuple)
            session.add(
                ListInstance(
                    device_id=device_id,
                    list_path=list_path,
                    key_values=key_values,
                )
            )
            count += 1
    session.commit()
    return count
