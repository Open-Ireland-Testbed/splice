# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from splice.api._catalog import InMemoryCatalog
from splice.api._errors import DiscoveryError

log = logging.getLogger(__name__)


def _sanitize_module_part(value: str, label: str) -> str:
    """Strip path separators and reject traversal sequences in YANG identifiers."""
    if '..' in value:
        raise DiscoveryError(f"invalid {label}: {value!r}")
    return value.replace('/', '').replace('\\', '')


def discover_modules(pool) -> Tuple[Optional[str], List[Dict[str, object]]]:
    """Discover YANG modules via NETCONF (no DB).

    Uses the raw ncclient manager through the pool's client.
    """
    from splice.netconf.discovery_client import fetch_supported_modules

    mgr = pool.client.manager()
    source, modules = fetch_supported_modules(mgr)
    if not modules:
        raise DiscoveryError("no YANG modules discovered from device")
    return source, modules


def fetch_yang_schemas(
    pool,
    modules: List[Dict[str, object]],
    yang_dir: str,
    throttle_sec: float = 0.3,
) -> int:
    """Fetch YANG schemas to disk (no DB storage).

    Reuses ``build_get_schema_rpc`` / ``extract_schema_text`` from
    ``yang_fetcher`` but writes only to the filesystem.
    """
    from defusedxml import ElementTree as ET

    from splice.netconf.yang_fetcher import (
        build_get_schema_rpc,
        extract_schema_text,
    )

    os.makedirs(yang_dir, exist_ok=True)
    written = 0

    for mod in modules:
        name = str(mod.get("name", ""))
        revision = mod.get("revision") or None
        if not name:
            continue

        name = _sanitize_module_part(name, "module name")
        if revision:
            revision = _sanitize_module_part(str(revision), "revision")

        fname = f"{name}@{revision}.yang" if revision else f"{name}.yang"
        dest = os.path.join(yang_dir, fname)
        if os.path.exists(dest):
            written += 1
            continue

        rpc_xml = build_get_schema_rpc(name, revision, fmt="yang")
        mgr = pool.client.manager()
        try:
            try:
                from ncclient.xml_ import to_ele  # type: ignore
                reply = mgr.dispatch(to_ele(rpc_xml))
            except Exception:
                reply = mgr.dispatch(ET.fromstring(rpc_xml))
        except Exception as exc:
            log.warning("failed to fetch schema %s: %s", name, exc)
            continue

        raw = getattr(reply, "xml", "")
        if not isinstance(raw, str):
            raw = ET.tostring(raw, encoding="unicode")

        yang_text = extract_schema_text(raw)
        if not yang_text:
            log.debug("empty schema text for %s", name)
            continue

        with open(dest, "w") as f:
            f.write(yang_text)
        written += 1

        if throttle_sec > 0:
            time.sleep(throttle_sec)

    return written


def build_catalog_in_memory(
    yang_dir: str,
    include_config: bool = True,
) -> InMemoryCatalog:
    """Build a complete in-memory catalog from YANG files on disk."""
    from splice.alias import ensure_unique_aliases
    from splice.catalog import build_catalog

    if not os.path.isdir(yang_dir) or not any(
        f.endswith(".yang") for f in os.listdir(yang_dir)
    ):
        raise DiscoveryError(f"no YANG files found in {yang_dir}")

    raw_catalog = build_catalog(yang_dir, include_config=include_config)
    if not raw_catalog:
        raise DiscoveryError("catalog build produced no paths")

    catalog_map: Dict[str, Dict] = {}
    for entry in raw_catalog:
        catalog_map[entry["path"]] = entry

    canonical_paths = list(catalog_map.keys())

    # ensure_unique_aliases returns canonical->alias; we need alias->canonical
    canonical_to_alias = ensure_unique_aliases(canonical_paths)
    alias_map: Dict[str, str] = {v: k for k, v in canonical_to_alias.items()}

    type_map: Dict[str, str] = {}
    list_keys: Dict[str, List[str]] = {}
    description_map: Dict[str, str] = {}
    for path, entry in catalog_map.items():
        if entry.get("data_type"):
            type_map[path] = str(entry["data_type"])
        if entry.get("list_keys"):
            list_keys[path] = list(entry["list_keys"])
        if entry.get("description"):
            description_map[path] = str(entry["description"])

    cat = InMemoryCatalog(
        catalog_map=catalog_map,
        alias_map=alias_map,
        type_map=type_map,
        list_keys=list_keys,
        description_map=description_map,
    )
    return cat
