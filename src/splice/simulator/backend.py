# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Simulated NETCONF client that returns pre-canned responses.

``SimulatedNetconfClient`` is a drop-in replacement for
``NetconfClient`` — it exposes the same public methods so it can be
wrapped by ``NetconfClientPool`` and consumed by the ``Device`` class
without any special handling.
"""
from __future__ import annotations

import logging
import re
import threading
from types import SimpleNamespace
from typing import Optional

from splice.simulator.profiles.base import DeviceProfile

log = logging.getLogger(__name__)


class _SimulatedReply:
    """Minimal stand-in for an ncclient RPC reply."""

    def __init__(self, xml: str) -> None:
        self.xml = xml
        self.data_xml = xml


class _SimulatedManager:
    """Fake ncclient manager exposed via ``client.manager()``.

    The discovery pipeline calls ``manager.get()`` and reads
    ``manager.server_capabilities``, so we need to support both.
    """

    def __init__(self, profile: DeviceProfile) -> None:
        self._profile = profile
        self.server_capabilities = list(profile.capabilities)
        self.connected = True

    def get(self, filter_xml) -> _SimulatedReply:
        # filter_xml may be a raw string or a tuple — normalise
        fxml = filter_xml if isinstance(filter_xml, str) else str(filter_xml)
        return _SimulatedReply(self._profile.get_response(fxml))

    def get_config(self, source: str = "running", filter: str = "") -> _SimulatedReply:
        fxml = filter if isinstance(filter, str) else str(filter)
        return _SimulatedReply(self._profile.get_config_response(source, fxml))

    def dispatch(self, rpc_elem) -> _SimulatedReply:
        """Handle <get-schema> and similar dispatch RPCs."""
        raw = ""
        if isinstance(rpc_elem, str):
            raw = rpc_elem
        elif hasattr(rpc_elem, "tag"):
            # Handle both lxml and stdlib Elements
            try:
                from lxml.etree import tostring as _lxml_tostring
                raw = _lxml_tostring(rpc_elem, encoding="unicode")
            except Exception:
                try:
                    from xml.etree.ElementTree import tostring as _std_tostring
                    raw = _std_tostring(rpc_elem, encoding="unicode")
                except Exception:
                    pass
        # Try to extract identifier/version from get-schema
        # Handle both plain and namespace-prefixed tags (e.g. <ns0:identifier>)
        id_match = re.search(r"<(?:\w+:)?identifier>(.*?)</(?:\w+:)?identifier>", raw)
        ver_match = re.search(r"<(?:\w+:)?version>(.*?)</(?:\w+:)?version>", raw)
        identifier = id_match.group(1) if id_match else ""
        version = ver_match.group(1) if ver_match else None
        if identifier:
            return _SimulatedReply(
                self._profile.get_schema_response(identifier, version)
            )
        # Fallback — empty reply
        return _SimulatedReply(
            '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><data/></rpc-reply>'
        )

    def close_session(self) -> None:
        self.connected = False


class SimulatedNetconfClient:
    """Drop-in replacement for ``NetconfClient`` backed by a device profile.

    All methods are thread-safe (guarded by an RLock, same as the real
    client) and return immediately — no network I/O is performed.
    """

    def __init__(self, profile: DeviceProfile) -> None:
        self._profile = profile
        self._mgr: Optional[_SimulatedManager] = None
        self._lock = threading.RLock()
        # Mirror NetconfClient attributes so NetconfClientPool works
        self.host = "sim://localhost"
        self.port = 0

    def connect(self) -> None:
        with self._lock:
            if self._mgr is not None and self._mgr.connected:
                return
            self._mgr = _SimulatedManager(self._profile)
            log.info("SimulatedNetconfClient connected (profile=%s)", self._profile.name)

    def close(self, graceful: bool = True) -> None:
        with self._lock:
            if self._mgr is not None:
                self._mgr.connected = False
            self._mgr = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def get(self, filter_xml: str) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return self._mgr.get(filter_xml)  # type: ignore[union-attr]

    def get_config(self, source: str, filter_xml: str) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return self._mgr.get_config(source=source, filter=filter_xml)  # type: ignore[union-attr]

    def dispatch(self, rpc_elem) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return self._mgr.dispatch(rpc_elem)  # type: ignore[union-attr]

    def edit_config(self, target: str, config: str) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return _SimulatedReply(
                self._profile.edit_config_response(target, config)
            )

    def commit(self) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return _SimulatedReply(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
            )

    def discard_changes(self) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return _SimulatedReply(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
            )

    def lock(self, target: str) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return _SimulatedReply(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
            )

    def unlock(self, target: str) -> _SimulatedReply:
        with self._lock:
            self.connect()
            return _SimulatedReply(
                '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
            )

    def create_subscription(self, *args, **kwargs):
        pass

    def take_notification(self, timeout: int = 1):
        return None

    def manager(self) -> _SimulatedManager:
        with self._lock:
            self.connect()
            return self._mgr  # type: ignore[return-value]
