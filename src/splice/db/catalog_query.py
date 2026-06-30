# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, List

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from splice.db.models import CatalogPath


def fetch_catalog_map(session: Session, device_id: int) -> Dict[str, Dict[str, object]]:
    rows = session.execute(
        select(CatalogPath).where(CatalogPath.device_id == device_id)
    ).scalars().all()
    out: Dict[str, Dict[str, object]] = {}
    for r in rows:
        out[r.canonical_path] = {
            "filter_xml": r.filter_xml,
            "keyword": r.keyword,
            "config": r.config,
            "namespace_map": r.namespace_map,
            "data_type": r.data_type,
            "list_keys": r.list_keys,
            "description": r.description,
        }
    return out


def fetch_catalog_map_with_module(session: Session, device_id: int) -> Dict[str, Dict[str, object]]:
    rows = session.execute(
        select(CatalogPath, CatalogPath.module_id).where(CatalogPath.device_id == device_id)
    ).all()
    module_names = {}
    if rows:
        mod_ids = {r[1] for r in rows if r[1] is not None}
        if mod_ids:
            from splice.db.models import Module
            mods = session.execute(select(Module).where(Module.id.in_(mod_ids))).scalars().all()
            module_names = {m.id: m.name for m in mods}
    out: Dict[str, Dict[str, object]] = {}
    for r, mid in rows:
        out[r.canonical_path] = {
            "filter_xml": r.filter_xml,
            "keyword": r.keyword,
            "config": r.config,
            "namespace_map": r.namespace_map,
            "data_type": r.data_type,
            "list_keys": r.list_keys,
            "description": r.description,
            "module": module_names.get(mid, ""),
        }
    return out


def fetch_type_map(session: Session, device_id: int) -> Dict[str, str]:
    rows = session.execute(
        select(CatalogPath.canonical_path, CatalogPath.data_type).where(
            CatalogPath.device_id == device_id, CatalogPath.data_type.isnot(None)
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def fetch_list_keys(session: Session, device_id: int) -> Dict[str, list]:
    rows = session.execute(
        select(CatalogPath.canonical_path, CatalogPath.list_keys).where(
            CatalogPath.device_id == device_id,
            CatalogPath.list_keys.isnot(None),
            text("list_keys::text <> 'null'"),
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def fetch_list_nodes(session: Session, device_id: int) -> List[Dict[str, object]]:
    rows = session.execute(
        select(
            CatalogPath.canonical_path,
            CatalogPath.list_keys,
            CatalogPath.namespace_map,
        ).where(
            CatalogPath.device_id == device_id,
            CatalogPath.list_keys.isnot(None),
            text("list_keys::text <> 'null'"),
        )
    ).all()
    return [
        {
            "canonical_path": r[0],
            "list_keys": r[1],
            "namespace_map": r[2],
        }
        for r in rows
    ]


def fetch_catalog_paths(session: Session, device_id: int) -> List[str]:
    rows = session.execute(
        select(CatalogPath.canonical_path).where(CatalogPath.device_id == device_id)
    ).all()
    return [r[0] for r in rows]
