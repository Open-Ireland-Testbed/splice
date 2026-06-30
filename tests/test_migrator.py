"""Tests for splice.db.migrator — unit and PG integration."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from splice.db.migrator import (
    MigrationLockError,
    MIGRATION_ADVISORY_LOCK_KEY,
    apply_migration,
    ensure_database,
    ensure_migration_table,
    get_all_migrations,
    run_migrations,
    _split_statements,
)


# ---------------------------------------------------------------------------
# Unit tests (no PG needed)
# ---------------------------------------------------------------------------


def test_ensure_migration_table():
    """ensure_migration_table executes CREATE TABLE IF NOT EXISTS."""
    mock_conn = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx

    ensure_migration_table(mock_engine)

    mock_conn.execute.assert_called_once()
    sql_arg = str(mock_conn.execute.call_args[0][0])
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sql_arg


def test_get_all_migrations():
    """get_all_migrations finds the 7 migration SQL files in sorted order."""
    migrations = get_all_migrations()
    assert len(migrations) == 7
    versions = [ver for ver, _ in migrations]
    assert versions == sorted(versions)
    assert versions[0].startswith("001_")
    assert versions[-1].startswith("007_")


def test_split_statements():
    """_split_statements splits on semicolons, respecting quotes."""
    sql = "CREATE TABLE t (name TEXT); INSERT INTO t VALUES ('a;b');"
    result = _split_statements(sql)
    assert len(result) == 2
    assert result[0] == "CREATE TABLE t (name TEXT)"
    assert result[1] == "INSERT INTO t VALUES ('a;b')"


def test_split_statements_no_trailing_semicolon():
    """_split_statements handles SQL without trailing semicolon."""
    sql = "SELECT 1; SELECT 2"
    result = _split_statements(sql)
    assert result == ["SELECT 1", "SELECT 2"]


def test_ensure_database_already_exists():
    """When the database already exists, ensure_database returns False."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = (1,)
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn_ctx

    with patch("splice.db.migrator.create_engine", return_value=mock_engine):
        result = ensure_database("postgresql+psycopg://user:pass@localhost:5432/mydb")

    assert result is False
    # Should only have the SELECT query, no CREATE DATABASE
    assert mock_conn.execute.call_count == 1


def test_ensure_database_creates():
    """When the database does not exist, ensure_database creates it and returns True."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchone.return_value = None
    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn_ctx

    with patch("splice.db.migrator.create_engine", return_value=mock_engine):
        result = ensure_database("postgresql+psycopg://user:pass@localhost:5432/mydb")

    assert result is True
    # SELECT check + CREATE DATABASE
    assert mock_conn.execute.call_count == 2
    create_call = str(mock_conn.execute.call_args_list[1][0][0])
    assert 'CREATE DATABASE "mydb"' in create_call


def test_run_migrations_lock_timeout_raises():
    """run_migrations should fail fast if advisory migration lock is unavailable."""
    mock_conn = MagicMock()
    lock_row = MagicMock()
    lock_row.scalar.return_value = False
    mock_conn.execute.side_effect = [lock_row]
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn

    with pytest.raises(MigrationLockError):
        run_migrations(mock_engine, lock_wait_sec=0)


def test_apply_migration_sets_local_timeouts(tmp_path):
    """apply_migration should set lock/statement/idle timeouts before SQL."""
    migration = tmp_path / "999_test.sql"
    migration.write_text("SELECT 1;", encoding="utf-8")

    mock_conn = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx

    apply_migration(
        mock_engine,
        "999_test",
        migration,
        lock_timeout_ms=7,
        statement_timeout_ms=11,
        idle_in_tx_timeout_ms=13,
    )

    sql_calls = [str(c.args[0]) for c in mock_conn.execute.call_args_list]
    assert "SELECT set_config('lock_timeout', :value, true)" in sql_calls[0]
    assert "SELECT set_config('statement_timeout', :value, true)" in sql_calls[1]
    assert "SELECT set_config('idle_in_transaction_session_timeout', :value, true)" in sql_calls[2]
    # 3 timeout statements + migration SQL + schema_migrations insert
    assert len(sql_calls) == 5


# ---------------------------------------------------------------------------
# PG integration tests (require TEST_DATABASE_URL)
# ---------------------------------------------------------------------------

def _db_url():
    return os.environ.get("TEST_DATABASE_URL")


def _skip_no_pg():
    if not _db_url():
        pytest.skip("TEST_DATABASE_URL not set")


def test_ensure_database_creates_and_is_idempotent_pg():
    """Create a temp DB via ensure_database, verify idempotency, then clean up."""
    _skip_no_pg()
    base_url = make_url(_db_url())
    temp_name = f"splice_test_ensure_{uuid.uuid4().hex[:8]}"
    temp_url = str(base_url.set(database=temp_name))

    try:
        # First call should create
        assert ensure_database(temp_url) is True
        # Second call should be idempotent
        assert ensure_database(temp_url) is False
    finally:
        # Clean up: drop the temp database
        admin_engine = create_engine(
            base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_name}"'))
        finally:
            admin_engine.dispose()


def test_run_migrations_pg():
    """Create a temp DB, run migrations, verify schema_migrations has all 7 entries."""
    _skip_no_pg()
    base_url = make_url(_db_url())
    temp_name = f"splice_test_mig_{uuid.uuid4().hex[:8]}"
    temp_url = str(base_url.set(database=temp_name))

    # Create the temp database
    ensure_database(temp_url)
    engine = create_engine(temp_url)
    try:
        count = run_migrations(engine)
        assert count == 7

        # Running again should apply 0
        count2 = run_migrations(engine)
        assert count2 == 0

        # Verify schema_migrations has all 7 entries
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        versions = [r[0] for r in rows]
        assert len(versions) == 7
        assert versions[0].startswith("001_")
        assert versions[-1].startswith("007_")
    finally:
        engine.dispose()
        admin_engine = create_engine(
            base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_name}"'))
        finally:
            admin_engine.dispose()


def test_run_migrations_respects_advisory_lock_pg():
    """If another session holds the migration lock, run_migrations should fail fast."""
    _skip_no_pg()
    base_url = make_url(_db_url())
    temp_name = f"splice_test_mig_lock_{uuid.uuid4().hex[:8]}"
    temp_url = str(base_url.set(database=temp_name))

    ensure_database(temp_url)
    lock_engine = create_engine(temp_url)
    test_engine = create_engine(temp_url)
    lock_conn = lock_engine.connect()
    try:
        lock_conn.execute(
            text("SELECT pg_advisory_lock(:k)"),
            {"k": MIGRATION_ADVISORY_LOCK_KEY},
        )
        with pytest.raises(MigrationLockError):
            run_migrations(test_engine, lock_wait_sec=0)
    finally:
        try:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": MIGRATION_ADVISORY_LOCK_KEY},
            )
        finally:
            lock_conn.close()
            lock_engine.dispose()
            test_engine.dispose()
        admin_engine = create_engine(
            base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_name}"'))
        finally:
            admin_engine.dispose()
