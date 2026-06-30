# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from splice.db.models import PathAlias


def upsert_aliases(session: Session, device_id: int, alias_map: Dict[str, str]) -> int:
    count = 0
    for canonical, alias in alias_map.items():
        stmt = select(PathAlias).where(
            PathAlias.device_id == device_id,
            PathAlias.canonical_path == canonical,
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = PathAlias(
                device_id=device_id,
                canonical_path=canonical,
                alias_path=alias,
                source="generated",
            )
            session.add(row)
            count += 1
        else:
            row.alias_path = alias
            count += 1
    session.commit()
    return count
