# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import List, Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from splice.db.models import Module


def fetch_models(session: Session, device_id: int) -> List[Dict[str, str]]:
    rows = session.execute(
        select(Module).where(Module.device_id == device_id)
    ).scalars().all()
    out = []
    for r in rows:
        out.append(
            {
                "name": r.name,
                "organization": "NETCONF",
                "version": r.revision or "",
            }
        )
    return out
