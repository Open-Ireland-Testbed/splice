# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Base class for simulator device profiles."""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol


class DeviceProfile(Protocol):
    """Protocol that all simulator device profiles must implement."""

    @property
    def name(self) -> str:
        """Short identifier for this profile (e.g. 'openroadm')."""
        ...

    @property
    def capabilities(self) -> List[str]:
        """NETCONF server capabilities URIs."""
        ...

    @property
    def modules(self) -> List[Dict[str, object]]:
        """List of YANG module dicts with 'name', 'revision', 'namespace' keys."""
        ...

    def get_response(self, filter_xml: str) -> str:
        """Return XML response for a NETCONF <get> with the given filter.

        The returned XML must be a complete ``<rpc-reply><data>...</data></rpc-reply>``
        document parseable by ``parse_xml_to_kv()``.
        """
        ...

    def get_config_response(self, source: str, filter_xml: str) -> str:
        """Return XML response for a NETCONF <get-config>."""
        ...

    def get_schema_response(self, identifier: str, version: Optional[str]) -> str:
        """Return XML response for a <get-schema> RPC.

        Should return a minimal but valid YANG module text so that
        ``build_catalog_in_memory()`` can build a catalog.
        """
        ...

    def edit_config_response(self, target: str, config: str) -> str:
        """Return XML response for a NETCONF <edit-config>.

        Profiles may also update their internal state to reflect the edit.
        """
        ...

    def netconf_monitoring_xml(self) -> str:
        """Return ietf-netconf-monitoring <schemas> XML wrapped in <rpc-reply><data>."""
        ...
