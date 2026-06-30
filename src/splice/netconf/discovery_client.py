# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from splice.discovery import (
    parse_netconf_monitoring,
    parse_yang_library,
)
from splice.db.repository import upsert_modules


def build_yang_library_filter() -> str:
    return (
        "<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"
        "<yang-library xmlns=\"urn:ietf:params:xml:ns:yang:ietf-yang-library\"/>"
        "</filter>"
    )


def build_modules_state_filter() -> str:
    return (
        "<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"
        "<modules-state xmlns=\"urn:ietf:params:xml:ns:yang:ietf-yang-library\"/>"
        "</filter>"
    )


def build_netconf_monitoring_filter() -> str:
    return (
        "<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"
        "<netconf-state xmlns=\"urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring\">"
        "<schemas/>"
        "</netconf-state>"
        "</filter>"
    )


def _get_data_xml(reply) -> str:
    return getattr(reply, "data_xml", "") or ""


def fetch_supported_modules(manager) -> Tuple[Optional[str], List[Dict[str, object]]]:
    # Try RFC 8525
    try:
        reply = manager.get(build_yang_library_filter())
        parsed = parse_yang_library(_get_data_xml(reply))
        if parsed is not None:
            return parsed["source"], parsed["modules"]  # type: ignore[index]
    except Exception:
        # If the yang-library RPC itself fails, fall back directly to monitoring.
        try:
            reply = manager.get(build_netconf_monitoring_filter())
            parsed = parse_netconf_monitoring(_get_data_xml(reply))
            if parsed is not None:
                return parsed["source"], parsed["modules"]  # type: ignore[index]
        except Exception:
            return None, []

    # Try RFC 7895
    try:
        reply = manager.get(build_modules_state_filter())
        parsed = parse_yang_library(_get_data_xml(reply))
        if parsed is not None:
            return parsed["source"], parsed["modules"]  # type: ignore[index]
    except Exception:
        pass

    # Fallback to netconf-monitoring
    try:
        reply = manager.get(build_netconf_monitoring_filter())
        parsed = parse_netconf_monitoring(_get_data_xml(reply))
        if parsed is not None:
            return parsed["source"], parsed["modules"]  # type: ignore[index]
    except Exception:
        pass

    return None, []


def discover_and_store(manager, session, device_id: int) -> Tuple[Optional[str], int]:
    source, modules = fetch_supported_modules(manager)
    if not modules:
        return source, 0
    count = upsert_modules(session, device_id=device_id, modules=modules, source=source)
    return source, count
