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


def fetch_alias_map(session: Session, device_id: int) -> Dict[str, str]:
    rows = session.execute(
        select(PathAlias).where(PathAlias.device_id == device_id)
    ).scalars().all()
    return {r.alias_path: r.canonical_path for r in rows}
