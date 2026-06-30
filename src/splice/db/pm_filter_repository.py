# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

from sqlalchemy import delete
from sqlalchemy.orm import Session

from splice.db.models import PMFilter


def fetch_pm_filters(session: Session, device_id: int) -> List[PMFilter]:
    rows = session.query(PMFilter).filter(PMFilter.device_id == device_id).all()
    return rows


def replace_pm_filters(
    session: Session,
    device_id: int,
    filters: Sequence[Mapping[str, object]] | Dict[str, str],
) -> int:
    session.execute(delete(PMFilter).where(PMFilter.device_id == device_id))
    if isinstance(filters, dict):
        items = [{"root_path": k, "filter_xml": v} for k, v in filters.items()]
    else:
        items = list(filters)
    for item in items:
        session.add(
            PMFilter(
                device_id=device_id,
                root_path=str(item.get("root_path")),
                filter_xml=str(item.get("filter_xml")),
                list_path=item.get("list_path"),
                key_values=item.get("key_values"),
            )
        )
    session.commit()
    return len(items)
