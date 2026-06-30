# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterable

from sqlalchemy.orm import Session

from splice.db.models import TelemetryHistory


def insert_history(session: Session, device_id: int, updates: Iterable[Dict[str, object]]) -> int:
    count = 0
    rows = []
    for u in updates:
        path = u.get("path")
        if not path:
            continue
        rows.append(
            TelemetryHistory(
                device_id=device_id,
                canonical_path=str(path),
                value_json=u.get("value"),
                timestamp_ns=int(u.get("timestamp_ns", 0) or 0),
                meta=u.get("meta"),
            )
        )
        count += 1
    if rows:
        session.add_all(rows)
        session.commit()
    return count
