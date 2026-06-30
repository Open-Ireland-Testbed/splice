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

from splice.db.models import Module


def upsert_modules(
    session: Session,
    device_id: int,
    modules: Iterable[Dict[str, object]],
    source: str | None = None,
) -> int:
    """Insert or update module rows for a device.

    Returns the number of rows inserted or updated.
    """
    count = 0
    for m in modules:
        name = m.get("name")
        if not name:
            continue
        revision = m.get("revision")
        stmt = select(Module).where(
            Module.device_id == device_id,
            Module.name == name,
            Module.revision == revision,
        )
        existing = session.execute(stmt).scalar_one_or_none()
        if existing is None:
            row = Module(
                device_id=device_id,
                name=str(name),
                revision=str(revision) if revision else None,
                namespace=m.get("namespace"),
                format=m.get("format"),
                source=source,
                conformance=m.get("conformance"),
                features=m.get("features"),
                deviations=m.get("deviations"),
            )
            session.add(row)
            count += 1
        else:
            existing.namespace = m.get("namespace")
            existing.format = m.get("format")
            existing.source = source
            existing.conformance = m.get("conformance")
            existing.features = m.get("features")
            existing.deviations = m.get("deviations")
            count += 1
    session.commit()
    return count


def fetch_modules(session: Session, device_id: int):
    return session.execute(
        select(Module).where(Module.device_id == device_id)
    ).scalars().all()


def create_yang_file(
    session: Session,
    module_id: int,
    path: str,
    checksum: str | None = None,
) -> int:
    yf = session.execute(
        select(Module).where(Module.id == module_id)
    ).scalar_one_or_none()
    if yf is None:
        raise ValueError(f"module_id not found: {module_id}")

    from splice.db.models import YangFile

    y = YangFile(module_id=module_id, path=path, checksum=checksum)
    session.add(y)
    session.commit()
    return y.id
