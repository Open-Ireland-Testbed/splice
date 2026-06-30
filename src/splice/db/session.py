# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_engine(db_url: str):
    # pool_pre_ping: validate a pooled connection before handing it out, so a
    # connection the server (or a firewall) closed after idle is transparently
    # replaced instead of raising "server closed the connection unexpectedly"
    # on the first query — the classic failure of a process running for days.
    # pool_recycle caps connection age below typical idle timeouts.
    return create_engine(db_url, pool_pre_ping=True, pool_recycle=1800)


def make_session_factory(db_url: str):
    engine = make_engine(db_url)
    return sessionmaker(bind=engine)
