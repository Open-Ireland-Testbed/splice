# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from splice.db.models import CatalogPath, Job, ListInstance, PathAlias
from splice.expander import strip_predicates


def _chunked(values, size: int = 1000):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def prune_catalog_paths(
    session: Session,
    device_id: int,
    keep_paths: Iterable[str],
) -> Dict[str, int]:
    """Delete catalog rows (and their dependent aliases/jobs/list instances)
    whose canonical paths are absent from keep_paths.

    There are no FKs between these tables — cross-references are
    (device_id, path-string) — so dependents are deleted explicitly. Jobs are
    matched on their predicate-stripped path: keyed jobs created by set-job /
    trigger onboarding must survive when their base path survives, and jobs
    outside the catalog (e.g. /pm paths) are never touched.
    """
    keep = set(keep_paths)
    existing = set(
        session.execute(
            select(CatalogPath.canonical_path).where(CatalogPath.device_id == device_id)
        ).scalars()
    )
    to_delete = existing - keep
    counts = {"catalog_paths": 0, "path_aliases": 0, "jobs": 0, "list_instances": 0}
    if not to_delete:
        return counts

    job_paths = session.execute(
        select(Job.canonical_path).where(Job.device_id == device_id)
    ).scalars()
    jobs_to_delete = [p for p in job_paths if strip_predicates(p) in to_delete]

    for chunk in _chunked(to_delete):
        counts["catalog_paths"] += session.execute(
            delete(CatalogPath).where(
                CatalogPath.device_id == device_id,
                CatalogPath.canonical_path.in_(chunk),
            )
        ).rowcount
        counts["path_aliases"] += session.execute(
            delete(PathAlias).where(
                PathAlias.device_id == device_id,
                PathAlias.canonical_path.in_(chunk),
            )
        ).rowcount
        counts["list_instances"] += session.execute(
            delete(ListInstance).where(
                ListInstance.device_id == device_id,
                ListInstance.list_path.in_(chunk),
            )
        ).rowcount
    for chunk in _chunked(jobs_to_delete):
        counts["jobs"] += session.execute(
            delete(Job).where(
                Job.device_id == device_id,
                Job.canonical_path.in_(chunk),
            )
        ).rowcount
    session.commit()
    return counts


def upsert_catalog_paths(
    session: Session,
    device_id: int,
    module_id: int | None,
    entries: Iterable[Dict[str, object]],
) -> int:
    count = 0
    for e in entries:
        canonical_path = e.get("path")
        if not canonical_path:
            continue
        stmt = select(CatalogPath).where(
            CatalogPath.device_id == device_id,
            CatalogPath.canonical_path == canonical_path,
        )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            row = CatalogPath(
                device_id=device_id,
                module_id=module_id,
                canonical_path=str(canonical_path),
                filter_xml=str(e.get("filter_xml")),
                namespace_map=e.get("namespace_map"),
                list_keys=e.get("list_keys"),
                keyword=str(e.get("keyword")),
                config=bool(e.get("config")),
                data_type=e.get("data_type"),
                description=e.get("description"),
            )
            session.add(row)
            count += 1
        else:
            row.filter_xml = str(e.get("filter_xml"))
            row.namespace_map = e.get("namespace_map")
            row.list_keys = e.get("list_keys")
            row.keyword = str(e.get("keyword"))
            row.config = bool(e.get("config"))
            row.data_type = e.get("data_type")
            row.description = e.get("description")
            count += 1
    session.commit()
    return count
