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
from typing import Callable, Dict, List, Optional

from ncclient.transport.errors import AuthenticationError

from splice.alias import ensure_unique_aliases
from splice.catalog import build_catalog
from splice.config import AppConfig
from splice.db.alias_repository import upsert_aliases
from splice.db.catalog_repository import upsert_catalog_paths
from splice.db.device_repository import get_or_create_device
from splice.db.job_repository import upsert_jobs
from splice.db.repository import fetch_modules
from splice.netconf.client import _force_close_session
from splice.netconf.discovery_client import discover_and_store
from splice.netconf.yang_fetcher import fetch_and_store_schema


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, AuthenticationError):
        return True
    msg = str(exc).lower()
    return "authentication" in msg and "failed" in msg


def _safe_close_manager(mgr) -> None:
    if mgr is None:
        return
    try:
        mgr.close_session()
    except Exception:
        _force_close_session(mgr)


def run_discovery(cfg: AppConfig, manager, session) -> int:
    dev = get_or_create_device(
        session,
        host=cfg.device.host,
        port=cfg.device.port,
        username=cfg.device.username,
        password_ref=None,
        name=cfg.device.name,
    )
    _source, count = discover_and_store(manager, session, dev.id)
    return count


def run_fetch_yang(cfg: AppConfig, manager, session,
                   throttle_sec: float = 0.3,
                   reconnect_fn: Optional[Callable] = None,
                   max_consecutive_failures: int = 10) -> int:
    """Fetch YANG schemas with skip-existing, throttle, and reconnect.

    Fails fast on authentication failures to prevent device lockouts.
    Caps consecutive failures to avoid reconnect storms.
    """
    log = logging.getLogger(__name__)
    dev = get_or_create_device(
        session,
        host=cfg.device.host,
        port=cfg.device.port,
        username=cfg.device.username,
        password_ref=None,
        name=cfg.device.name,
    )
    modules = fetch_modules(session, dev.id)
    wrote = 0
    skipped = 0
    active_mgr = manager
    consecutive_failures = 0

    try:
        for m in modules:
            if m.format and m.format != "yang":
                continue

            # Skip if YANG file already exists on disk
            fname = f"{m.name}@{m.revision}.yang" if m.revision else f"{m.name}.yang"
            path = os.path.join(cfg.yang_dir, fname)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                skipped += 1
                continue

            # Fail fast if too many consecutive failures (avoid reconnect storm)
            if consecutive_failures >= max_consecutive_failures:
                log.error(
                    "fetch-yang: stopping after %d consecutive failures (reconnect storm protection)",
                    consecutive_failures,
                )
                break

            # Throttle between RPCs
            if wrote > 0:
                time.sleep(throttle_sec)

            # Attempt fetch with reconnect on failure
            try:
                result = fetch_and_store_schema(
                    active_mgr, session, module_id=m.id,
                    module_name=m.name, revision=m.revision, out_dir=cfg.yang_dir,
                )
                if result:
                    wrote += 1
                    consecutive_failures = 0  # Reset on success
            except Exception as exc:
                if _is_auth_error(exc):
                    log.error(
                        "fetch-yang: authentication failed for %s — stopping to prevent device lockout: %s",
                        m.name, exc,
                    )
                    raise
                log.warning("fetch-yang failed for %s: %s", m.name, exc)
                consecutive_failures += 1
                if reconnect_fn is not None:
                    time.sleep(2.0)  # let device recover
                    prev_mgr = active_mgr
                    try:
                        next_mgr = reconnect_fn()
                        if next_mgr is not prev_mgr:
                            _safe_close_manager(prev_mgr)
                        active_mgr = next_mgr
                        result = fetch_and_store_schema(
                            active_mgr, session, module_id=m.id,
                            module_name=m.name, revision=m.revision, out_dir=cfg.yang_dir,
                        )
                        if result:
                            wrote += 1
                            consecutive_failures = 0  # Reset on success
                    except Exception as retry_exc:
                        if _is_auth_error(retry_exc):
                            log.error(
                                "fetch-yang: authentication failed on reconnect for %s — stopping to prevent lockout: %s",
                                m.name, retry_exc,
                            )
                            raise
                        log.warning("fetch-yang retry failed for %s: %s", m.name, retry_exc)
                        consecutive_failures += 1
                # else: skip and continue
    finally:
        if reconnect_fn is not None and active_mgr is not manager:
            _safe_close_manager(active_mgr)

    if skipped:
        log.info("fetch-yang: skipped %d already-existing files", skipped)
    return wrote


def run_build_catalog_with_paths(
    cfg: AppConfig, session, include_config: bool = True
) -> tuple[int, set]:
    """Build + upsert the catalog and also return the set of built canonical
    paths. The upsert count mixes inserts and updates, so it cannot be used
    to reason about what the build actually produced (e.g. for pruning)."""
    dev = get_or_create_device(
        session,
        host=cfg.device.host,
        port=cfg.device.port,
        username=cfg.device.username,
        password_ref=None,
        name=cfg.device.name,
    )
    catalog = build_catalog(cfg.yang_dir, include_config=include_config)
    count = upsert_catalog_paths(session, dev.id, None, catalog)
    built_paths = {str(e["path"]) for e in catalog if e.get("path")}
    return count, built_paths


def run_build_catalog(cfg: AppConfig, session, include_config: bool = True) -> int:
    count, _ = run_build_catalog_with_paths(cfg, session, include_config=include_config)
    return count


def run_build_aliases(session, device_id: int, canonical_paths: List[str]) -> int:
    alias_map = ensure_unique_aliases(canonical_paths)
    return upsert_aliases(session, device_id, alias_map)


def run_init_jobs(session, device_id: int, canonical_paths: List[str], interval_sec: int) -> int:
    jobs = {p: interval_sec for p in canonical_paths}
    # Bulk re-init must not resurrect jobs an operator manually disabled.
    return upsert_jobs(session, device_id, jobs, force_enable=False)
