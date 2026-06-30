# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Example PM adapter.

Copy this file and adjust the filter XML for your vendor.
"""

from __future__ import annotations

from typing import Dict


def discover_pm_filters(cfg, session, dev) -> Dict[str, str]:
    # Return mapping of root_path -> filter_xml.
    # root_path is the synthetic PM root for your collector (e.g., "/pm/vendor").
    # filter_xml is a NETCONF <filter> subtree to fetch pm-current-data.
    #
    # NOTE: keep this minimal and vendor-specific. This example returns nothing.
    _ = (cfg, session, dev)
    return {}
