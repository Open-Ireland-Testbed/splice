# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Database migration tracking and application.

Tracks applied migrations in a ``schema_migrations`` table and applies
pending SQL files from the ``migrations/`` directory in order.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
import time
from typing import List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url


# migrations/ ships inside the package (src/splice/migrations) so it is present
# in both editable and wheel installs.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_ADVISORY_LOCK_KEY = 0x474E4D4942524944  # "GNMIBRID"

DEFAULT_MIGRATION_LOCK_WAIT_SEC = float(
    os.getenv("SPLICE_MIGRATION_LOCK_WAIT_SEC", "30")
)
DEFAULT_MIGRATION_LOCK_TIMEOUT_MS = int(
    os.getenv("SPLICE_MIGRATION_LOCK_TIMEOUT_MS", "5000")
)
DEFAULT_MIGRATION_STATEMENT_TIMEOUT_MS = int(
    os.getenv("SPLICE_MIGRATION_STATEMENT_TIMEOUT_MS", "120000")
)
DEFAULT_MIGRATION_IDLE_TX_TIMEOUT_MS = int(
    os.getenv("SPLICE_MIGRATION_IDLE_TX_TIMEOUT_MS", "120000")
)


class MigrationLockError(RuntimeError):
    """Raised when the migration advisory lock cannot be acquired in time."""


def ensure_database(db_url: str) -> bool:
    """Create the PostgreSQL database if it doesn't exist. Returns True if created."""
    url = make_url(db_url)
    db_name = url.database
    admin_url = url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).fetchone()
            if row is None:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                return True
        return False
    finally:
        admin_engine.dispose()


def ensure_migration_table(engine) -> None:
    """Create the schema_migrations tracking table if it doesn't exist."""
    with engine.begin() as conn:
        _ensure_migration_table_on_conn(conn)


def get_applied_versions(engine) -> set:
    """Return the set of already-applied migration version strings."""
    with engine.begin() as conn:
        return _get_applied_versions_on_conn(conn)


def get_all_migrations() -> List[Tuple[str, Path]]:
    """Return all migration files sorted by filename (version order)."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(
            f"Migrations directory not found: {MIGRATIONS_DIR}. "
            "This usually means the package was installed without its data files; "
            "reinstall splice (the migrations ship under splice/migrations)."
        )
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [(f.stem, f) for f in files]


def get_pending_migrations(engine) -> List[Tuple[str, Path]]:
    """Return migrations that have not yet been applied."""
    ensure_migration_table(engine)
    applied = get_applied_versions(engine)
    return [(ver, path) for ver, path in get_all_migrations() if ver not in applied]


def apply_migration(
    engine,
    version: str,
    path: Path,
    *,
    lock_timeout_ms: int = DEFAULT_MIGRATION_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = DEFAULT_MIGRATION_STATEMENT_TIMEOUT_MS,
    idle_in_tx_timeout_ms: int = DEFAULT_MIGRATION_IDLE_TX_TIMEOUT_MS,
) -> None:
    """Apply a single migration file and record it."""
    sql = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        _set_local_migration_timeouts(
            conn,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
            idle_in_tx_timeout_ms=idle_in_tx_timeout_ms,
        )
        # Execute migration SQL (may contain multiple statements)
        for statement in _split_statements(sql):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(
            text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :t)"),
            {"v": version, "t": datetime.datetime.now(datetime.timezone.utc)},
        )


def run_migrations(
    engine,
    dry_run: bool = False,
    *,
    lock_wait_sec: float = DEFAULT_MIGRATION_LOCK_WAIT_SEC,
    lock_timeout_ms: int = DEFAULT_MIGRATION_LOCK_TIMEOUT_MS,
    statement_timeout_ms: int = DEFAULT_MIGRATION_STATEMENT_TIMEOUT_MS,
    idle_in_tx_timeout_ms: int = DEFAULT_MIGRATION_IDLE_TX_TIMEOUT_MS,
) -> int:
    """Apply all pending migrations. Returns count of applied migrations."""
    lock_conn = engine.connect()
    lock_acquired = False
    try:
        _acquire_migration_lock(lock_conn, wait_sec=lock_wait_sec)
        lock_acquired = True
        pending = _get_pending_migrations_on_conn(lock_conn)
        if lock_conn.in_transaction():
            # Ensure schema_migrations DDL is visible to other connections used below.
            lock_conn.commit()
        if dry_run:
            return len(pending)
        for version, path in pending:
            apply_migration(
                engine,
                version,
                path,
                lock_timeout_ms=lock_timeout_ms,
                statement_timeout_ms=statement_timeout_ms,
                idle_in_tx_timeout_ms=idle_in_tx_timeout_ms,
            )
        return len(pending)
    finally:
        try:
            if lock_acquired:
                _release_migration_lock(lock_conn)
        except Exception:
            # Preserve the original migration error if unlock fails.
            pass
        finally:
            lock_conn.close()


def _split_statements(sql: str) -> List[str]:
    """Split SQL text on semicolons, respecting basic quoting."""
    statements = []
    current: list[str] = []
    in_quote = False
    quote_char = ""
    for char in sql:
        if in_quote:
            current.append(char)
            if char == quote_char:
                in_quote = False
        elif char in ("'", '"'):
            in_quote = True
            quote_char = char
            current.append(char)
        elif char == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(char)
    # Remainder after last semicolon
    stmt = "".join(current).strip()
    if stmt:
        statements.append(stmt)
    return statements


def _ensure_migration_table_on_conn(conn) -> None:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version VARCHAR(255) PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        )
    )


def _get_applied_versions_on_conn(conn) -> set:
    rows = conn.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {row[0] for row in rows}


def _get_pending_migrations_on_conn(conn) -> List[Tuple[str, Path]]:
    _ensure_migration_table_on_conn(conn)
    applied = _get_applied_versions_on_conn(conn)
    return [(ver, path) for ver, path in get_all_migrations() if ver not in applied]


def _set_local_migration_timeouts(
    conn,
    *,
    lock_timeout_ms: int,
    statement_timeout_ms: int,
    idle_in_tx_timeout_ms: int,
) -> None:
    # Fail fast on lock contention and long-running statements instead of hanging setup.
    conn.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": f"{max(int(lock_timeout_ms), 1)}ms"},
    )
    conn.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": f"{max(int(statement_timeout_ms), 1)}ms"},
    )
    conn.execute(
        text("SELECT set_config('idle_in_transaction_session_timeout', :value, true)"),
        {"value": f"{max(int(idle_in_tx_timeout_ms), 1)}ms"},
    )


def _acquire_migration_lock(conn, *, wait_sec: float) -> None:
    deadline = time.monotonic() + max(float(wait_sec), 0.0)
    while True:
        locked = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": MIGRATION_ADVISORY_LOCK_KEY},
            ).scalar()
        )
        if locked:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockError(
                "Timed out waiting for migration lock; another setup/migrate may be running"
            )
        time.sleep(0.25)


def _release_migration_lock(conn) -> None:
    conn.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": MIGRATION_ADVISORY_LOCK_KEY},
    )
