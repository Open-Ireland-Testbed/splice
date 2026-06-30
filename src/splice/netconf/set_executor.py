# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import time
from typing import Callable, Mapping, Sequence

import splice.gnmi_pb2 as gnmi_pb2
from splice.netconf.client import NetconfClientPool
from splice.netconf.set_request import validate_set_request
from splice.netconf.setter import SetMutation, build_config_xml, parse_canonical_path

_log = logging.getLogger(__name__)


class SetPreconditionError(RuntimeError):
    """Target device/datastore preconditions are not met for Set execution."""


def _has_capability(capabilities: Sequence[str], token: str) -> bool:
    return any(token in cap for cap in capabilities)


def _path_from_canonical(path: str) -> gnmi_pb2.Path:
    elems = []
    for seg in parse_canonical_path(path):
        elems.append(gnmi_pb2.PathElem(name=seg.tag, key=dict(seg.key_values)))
    return gnmi_pb2.Path(elem=elems)


def _result_op(operation: str) -> int:
    op = operation.lower()
    if op == "delete":
        return gnmi_pb2.UpdateResult.DELETE
    if op == "replace":
        return gnmi_pb2.UpdateResult.REPLACE
    if op == "merge":
        return gnmi_pb2.UpdateResult.UPDATE
    raise ValueError(f"unsupported operation for SetResponse: {operation}")


class NetconfSetExecutor:
    def __init__(
        self,
        pool: NetconfClientPool,
        catalog_map: Mapping[str, dict],
        alias_map: Mapping[str, str],
        list_keys: Mapping[str, list[str]] | None = None,
        datastore_preference: str = "auto",
        use_lock: bool = False,
        post_apply_refresh: Callable[[Sequence[SetMutation]], None] | None = None,
    ) -> None:
        self._pool = pool
        self._catalog_map = dict(catalog_map)
        self._alias_map = dict(alias_map)
        self._list_keys = dict(list_keys or {})
        self._datastore_preference = datastore_preference
        self._use_lock = bool(use_lock)
        self._post_apply_refresh = post_apply_refresh
        self._caps_cache: list[str] | None = None

    def _capabilities(self) -> list[str]:
        if self._caps_cache is None:
            self._caps_cache = self._pool.get_server_capabilities()
        return list(self._caps_cache)

    def _select_datastore(self) -> str:
        pref = (self._datastore_preference or "auto").strip().lower()
        caps = self._capabilities()
        has_candidate = _has_capability(caps, ":candidate")
        has_writable_running = _has_capability(caps, ":writable-running")

        if pref == "auto":
            if has_candidate:
                return "candidate"
            if has_writable_running:
                return "running"
            raise SetPreconditionError(
                "device does not advertise :candidate or :writable-running capability"
            )
        if pref == "candidate":
            if not has_candidate:
                raise SetPreconditionError("set_datastore=candidate requires :candidate capability")
            return "candidate"
        if pref == "running":
            if not has_writable_running:
                raise SetPreconditionError("set_datastore=running requires :writable-running capability")
            return "running"
        raise SetPreconditionError(f"invalid set_datastore value: {self._datastore_preference!r}")

    def _apply_edit_config(self, datastore: str, config_xml: str) -> None:
        locked = False
        if self._use_lock:
            self._pool.lock(datastore)
            locked = True
        try:
            try:
                self._pool.edit_config(datastore, config_xml)
            except Exception:
                if datastore == "candidate":
                    try:
                        self._pool.discard_changes()
                    except Exception:
                        _log.exception("failed to discard candidate after edit-config failure")
                raise
            if datastore == "candidate":
                try:
                    self._pool.commit()
                except Exception:
                    try:
                        self._pool.discard_changes()
                    except Exception:
                        _log.exception("failed to discard candidate after commit failure")
                    raise
        finally:
            if locked:
                try:
                    self._pool.unlock(datastore)
                except Exception:
                    _log.exception("failed to unlock NETCONF datastore %s", datastore)

    def execute(self, request: gnmi_pb2.SetRequest) -> gnmi_pb2.SetResponse:
        mutations = validate_set_request(
            request=request,
            catalog_map=self._catalog_map,
            alias_map=self._alias_map,
            list_keys=self._list_keys,
        )
        if not mutations:
            raise SetPreconditionError("set request has no operations")

        config_xml = build_config_xml(mutations)
        datastore = self._select_datastore()
        self._apply_edit_config(datastore, config_xml)
        if self._post_apply_refresh is not None:
            try:
                self._post_apply_refresh(mutations)
            except Exception:
                # Best-effort refresh only; do not fail successful Set transaction.
                _log.exception("post-set refresh callback failed")

        results = []
        for m in mutations:
            results.append(
                gnmi_pb2.UpdateResult(
                    path=_path_from_canonical(m.canonical_path),
                    op=_result_op(m.operation),
                )
            )
        return gnmi_pb2.SetResponse(
            response=results,
            timestamp=time.time_ns(),
        )
