# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from splice.db.models import Device


def get_or_create_device(
    session: Session,
    host: str,
    port: int,
    username: str,
    password_ref: str | None = None,
    name: str | None = None,
) -> Device:
    stmt = select(Device).where(Device.host == host, Device.port == port)
    dev = session.execute(stmt).scalar_one_or_none()
    if dev is None:
        dev = Device(
            host=host,
            port=port,
            username=username,
            password_ref=password_ref,
            name=name,
        )
        session.add(dev)
        session.commit()
    return dev


def find_device(session: Session, host: str, port: int) -> Device | None:
    """Read-only lookup — for diagnostic commands that must not create rows."""
    stmt = select(Device).where(Device.host == host, Device.port == port)
    return session.execute(stmt).scalar_one_or_none()
