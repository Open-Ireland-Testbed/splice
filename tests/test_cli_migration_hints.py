from __future__ import annotations

from splice.cli import _migration_fix_hints
from splice.db.migrator import MigrationLockError


def test_migration_fix_hints_for_lock_error():
    hints = _migration_fix_hints(MigrationLockError("lock wait exceeded"))
    combined = " ".join(hints).lower()
    assert "another setup/migrate" in combined
    assert "doctor" in combined
    assert "pg_stat_activity" in combined


def test_migration_fix_hints_for_lock_timeout_message():
    hints = _migration_fix_hints(RuntimeError("canceling statement due to lock timeout"))
    combined = " ".join(hints).lower()
    assert "waited on a db lock" in combined
    assert "pg_stat_activity" in combined


def test_migration_fix_hints_generic_error():
    hints = _migration_fix_hints(RuntimeError("unexpected error"))
    combined = " ".join(hints).lower()
    assert "doctor" in combined
    assert "migrate" in combined
