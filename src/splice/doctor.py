# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pre-flight validation checks for splice.

Each check returns a CheckResult with pass/fail status and an
actionable fix hint on failure.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from sqlalchemy import text


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    fix: Optional[str] = None


def check_config_valid(cfg) -> CheckResult:
    """Verify the loaded config has required fields."""
    problems = []
    if not cfg.device.host or cfg.device.host == "None":
        problems.append("device.host is missing")
    if not cfg.db.url or cfg.db.url == "None":
        problems.append("db.url is missing")
    if problems:
        return CheckResult(
            name="config",
            passed=False,
            message=f"Configuration invalid: {'; '.join(problems)}",
            fix="Edit config.yaml and set the missing fields.",
        )
    return CheckResult(name="config", passed=True, message="Configuration is valid")


def check_db_reachable(engine) -> CheckResult:
    """Verify the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult(name="database", passed=True, message="Database is reachable")
    except Exception as exc:
        return CheckResult(
            name="database",
            passed=False,
            message=f"Database connection failed: {exc}",
            fix="Check db.url in config. Verify PostgreSQL is running and accessible.",
        )


def check_migrations_applied(engine) -> CheckResult:
    """Check whether all migrations have been applied."""
    from splice.db.migrator import get_pending_migrations

    try:
        pending = get_pending_migrations(engine)
    except Exception as exc:
        return CheckResult(
            name="migrations",
            passed=False,
            message=f"Migration check failed: {exc}",
            fix="Run: splice --config <config> migrate",
        )
    if pending:
        names = [v for v, _ in pending]
        return CheckResult(
            name="migrations",
            passed=False,
            message=f"{len(pending)} pending migrations: {', '.join(names)}",
            fix="Run: splice --config <config> migrate",
        )
    return CheckResult(name="migrations", passed=True, message="All migrations applied")


def check_db_lock_blockers(
    engine,
    *,
    min_age_sec: int = 30,
    limit: int = 5,
) -> CheckResult:
    """Check for long-lived idle transactions that commonly block migrations."""
    query = text(
        """
        SELECT
          pid,
          usename,
          state,
          wait_event_type,
          wait_event,
          EXTRACT(EPOCH FROM (now() - xact_start))::BIGINT AS age_sec,
          LEFT(REGEXP_REPLACE(COALESCE(query, ''), '\\s+', ' ', 'g'), 140) AS query_short
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND state = 'idle in transaction'
          AND xact_start IS NOT NULL
          AND EXTRACT(EPOCH FROM (now() - xact_start)) >= :min_age_sec
        ORDER BY xact_start ASC
        LIMIT :limit
        """
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                query,
                {"min_age_sec": int(min_age_sec), "limit": int(limit)},
            ).fetchall()
    except Exception as exc:
        text_lower = str(exc).lower()
        if "permission denied" in text_lower and "pg_stat_activity" in text_lower:
            return CheckResult(
                name="db_lock_blockers",
                passed=True,
                message="Lock blocker check skipped (no pg_stat_activity permission)",
            )
        return CheckResult(
            name="db_lock_blockers",
            passed=False,
            message=f"Lock blocker check failed: {exc}",
            fix="Verify DB user privileges and run doctor again.",
        )

    if not rows:
        return CheckResult(
            name="db_lock_blockers",
            passed=True,
            message="No long-lived idle transactions detected",
        )

    blockers = ", ".join(
        f"pid={r.pid} age={int(r.age_sec)}s user={r.usename}" for r in rows
    )
    return CheckResult(
        name="db_lock_blockers",
        passed=False,
        message=f"Detected {len(rows)} potential blockers: {blockers}",
        fix=(
            "Inspect/clear stale transactions in pg_stat_activity, then rerun migrate/setup."
        ),
    )


def check_device_port_open(host: str, port: int, timeout: float = 5.0) -> CheckResult:
    """Check if the NETCONF port is reachable via TCP."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return CheckResult(
            name="device_port",
            passed=True,
            message=f"Port {host}:{port} is open",
        )
    except Exception as exc:
        return CheckResult(
            name="device_port",
            passed=False,
            message=f"Port {host}:{port} unreachable: {exc}",
            fix=f"Check that the device at {host}:{port} is powered on and network-reachable.",
        )


def check_netconf_auth(cfg) -> CheckResult:
    """Verify NETCONF login succeeds."""
    try:
        from ncclient import manager

        with manager.connect(
            host=cfg.device.host,
            port=cfg.device.port,
            username=cfg.device.username,
            password=cfg.device.password or "",
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=15,
        ):
            pass
        return CheckResult(
            name="netconf_auth",
            passed=True,
            message="NETCONF authentication successful",
        )
    except Exception as exc:
        return CheckResult(
            name="netconf_auth",
            passed=False,
            message=f"NETCONF login failed: {type(exc).__name__}: {exc}",
            fix="Check username/password. Verify device allows NETCONF on this port.",
        )


def check_yang_dir(yang_dir: str) -> CheckResult:
    """Check that YANG directory exists and has .yang files."""
    p = Path(yang_dir)
    if not p.is_dir():
        return CheckResult(
            name="yang_dir",
            passed=False,
            message=f"YANG directory not found: {yang_dir}",
            fix="Run: splice --config <config> setup  (or fetch-yang)",
        )
    yang_files = list(p.glob("*.yang"))
    if not yang_files:
        return CheckResult(
            name="yang_dir",
            passed=False,
            message=f"YANG directory is empty: {yang_dir}",
            fix="Run: splice --config <config> setup  (or fetch-yang)",
        )
    return CheckResult(
        name="yang_dir",
        passed=True,
        message=f"YANG directory OK ({len(yang_files)} .yang files)",
    )


def check_catalog_populated(session, device_id: int) -> CheckResult:
    """Verify catalog has entries for this device."""
    try:
        row = session.execute(
            text("SELECT COUNT(*) FROM catalog_paths WHERE device_id = :did"),
            {"did": device_id},
        ).scalar()
        if row and row > 0:
            return CheckResult(
                name="catalog",
                passed=True,
                message=f"Catalog has {row} paths",
            )
        return CheckResult(
            name="catalog",
            passed=False,
            message="Catalog is empty",
            fix="Run: splice --config <config> setup  (or build-catalog)",
        )
    except Exception as exc:
        return CheckResult(
            name="catalog",
            passed=False,
            message=f"Catalog check failed: {exc}",
            fix="Run migrations first, then setup.",
        )


def check_jobs_exist(session, device_id: int) -> CheckResult:
    """Verify polling jobs exist for this device."""
    try:
        row = session.execute(
            text("SELECT COUNT(*) FROM jobs WHERE device_id = :did"),
            {"did": device_id},
        ).scalar()
        if row and row > 0:
            return CheckResult(
                name="jobs",
                passed=True,
                message=f"{row} polling jobs configured",
            )
        return CheckResult(
            name="jobs",
            passed=False,
            message="No polling jobs configured",
            fix="Run: splice --config <config> setup  (or init-jobs)",
        )
    except Exception as exc:
        return CheckResult(
            name="jobs",
            passed=False,
            message=f"Jobs check failed: {exc}",
            fix="Run migrations first, then setup.",
        )


def _normalized_instance(entry) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in (entry or {}).items()))


def check_pm_filters_fresh(cfg, session, device_id: int) -> CheckResult:
    """Verify instance-keyed PM filters still reference list instances that
    exist on the device. Devices answer filters for non-existent instances
    with empty data (no error), so a re-provisioned port silently kills PM
    until the filters are rebuilt — this check makes that visible.
    """
    from splice.db.catalog_query import fetch_list_nodes
    from splice.db.pm_filter_repository import fetch_pm_filters
    from splice.netconf.list_instance_resolver import discover_list_instances
    from splice.pm import match_selector, parse_selector

    pm_filters = fetch_pm_filters(session, device_id)
    if not pm_filters:
        return CheckResult(
            name="pm_filters",
            passed=True,
            message="No PM filters stored (PM discovery has not run)",
        )
    keyed = [r for r in pm_filters if r.list_path and r.key_values]
    if not keyed:
        return CheckResult(
            name="pm_filters",
            passed=True,
            message=f"{len(pm_filters)} PM filters, none instance-keyed",
        )
    fanout_paths = sorted({r.list_path for r in keyed})
    nodes = [
        n for n in fetch_list_nodes(session, device_id)
        if n["canonical_path"] in set(fanout_paths)
    ]
    if not nodes:
        return CheckResult(
            name="pm_filters",
            passed=False,
            message=f"PM fanout lists missing from catalog: {fanout_paths}",
            fix="Re-run `setup` (or `build-catalog`) to rebuild the catalog.",
        )
    try:
        from ncclient import manager

        with manager.connect(
            host=cfg.device.host,
            port=cfg.device.port,
            username=cfg.device.username,
            password=cfg.device.password or "",
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=30,
        ) as m:
            resolved = discover_list_instances(
                fetch=lambda fx: m.get(filter=fx).data_xml,
                list_nodes=nodes,
            )
    except Exception as exc:
        return CheckResult(
            name="pm_filters",
            passed=False,
            message=f"Could not query device for list instances: {exc}",
            fix="Check connectivity/credentials, then rerun doctor.",
        )

    problems = []
    for list_path in fanout_paths:
        current = resolved.get(list_path) or []
        current_norm = {_normalized_instance(i) for i in current}
        ghosts = [
            r.key_values
            for r in keyed
            if r.list_path == list_path
            and _normalized_instance(r.key_values) not in current_norm
        ]
        if ghosts:
            sample = ", ".join(str(g) for g in ghosts[:3])
            problems.append(
                f"{len(ghosts)} PM filter(s) reference instances no longer on "
                f"the device under {list_path} (e.g. {sample})"
            )
        if cfg.discovery.pm_selector and current:
            try:
                clauses, ops = parse_selector(cfg.discovery.pm_selector)
            except ValueError:
                clauses, ops = [], []
            if clauses and not [
                i for i in current if match_selector(i, clauses, ops)
            ]:
                problems.append(
                    f"pm_selector {cfg.discovery.pm_selector!r} matches 0 of "
                    f"{len(current)} current instances of {list_path}"
                )
    if problems:
        return CheckResult(
            name="pm_filters",
            passed=False,
            message="; ".join(problems),
            fix=(
                "Device was likely re-provisioned. Re-run `setup` to rebuild "
                "PM filters (or start `run` with discovery.pm_refresh_on_run "
                "enabled, which rebuilds them automatically)."
            ),
        )
    return CheckResult(
        name="pm_filters",
        passed=True,
        message=(
            f"{len(keyed)} instance-keyed PM filters match live device instances"
        ),
    )


def run_all_checks(cfg, engine, session, device_id: int) -> List[CheckResult]:
    """Run all pre-flight checks and return results."""
    results = []
    results.append(check_config_valid(cfg))
    results.append(check_db_reachable(engine))
    results.append(check_db_lock_blockers(engine))
    results.append(check_migrations_applied(engine))
    results.append(check_yang_dir(cfg.yang_dir))
    results.append(check_device_port_open(cfg.device.host, cfg.device.port))
    results.append(check_netconf_auth(cfg))
    results.append(check_catalog_populated(session, device_id))
    results.append(check_jobs_exist(session, device_id))
    if getattr(cfg.discovery, "pm_discovery", False):
        results.append(check_pm_filters_fresh(cfg, session, device_id))
    return results
