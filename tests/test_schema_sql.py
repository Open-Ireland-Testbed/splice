from splice.db.migrator import MIGRATIONS_DIR


def test_migration_contains_expected_tables():
    sql_path = MIGRATIONS_DIR / "001_init.sql"
    assert sql_path.exists()
    sql = sql_path.read_text()
    for table in [
        "devices",
        "modules",
        "yang_files",
        "catalog_paths",
        "path_aliases",
        "jobs",
        "last_value",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
