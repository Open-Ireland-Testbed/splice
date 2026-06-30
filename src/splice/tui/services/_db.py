# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared database helpers for TUI services."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceEntry

log = logging.getLogger(__name__)

_factory_cache: dict[str, object] = {}
_factory_lock = threading.Lock()


def get_session(db_url: str):
    """Return a new session, reusing a cached engine/factory per db_url."""
    with _factory_lock:
        factory = _factory_cache.get(db_url)
        if factory is None:
            from splice.db.session import make_session_factory

            factory = make_session_factory(db_url)
            _factory_cache[db_url] = factory
    return factory()


def lookup_device(session, entry: DeviceEntry):
    """Look up a Device row by host + netconf_port. Returns None if not found."""
    from sqlalchemy import select

    from splice.db.models import Device

    return session.execute(
        select(Device).where(
            Device.host == entry.host,
            Device.port == entry.netconf_port,
        )
    ).scalar_one_or_none()
