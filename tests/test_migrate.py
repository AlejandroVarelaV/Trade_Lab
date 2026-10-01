"""database/migrate.py: applies, skips, detects drift, rolls back failures."""
from pathlib import Path

import psycopg2
import pytest

from database.migrate import MIGRATIONS_DIR, MigrationError, run_migrations

pytestmark = pytest.mark.db


def _ledger(url):
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY filename")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _table_exists(url, table):
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_applies_all_real_migrations(empty_db):
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert run_migrations(empty_db) == {"applied": len(files), "skipped": 0}
    assert _ledger(empty_db) == files


def test_second_run_skips_everything(empty_db):
    run_migrations(empty_db)
    n = len(list(MIGRATIONS_DIR.glob("*.sql")))
    assert run_migrations(empty_db) == {"applied": 0, "skipped": n}


def test_drift_is_detected(empty_db, tmp_path: Path):
    mig = tmp_path / "001_a.sql"
    mig.write_text("CREATE TABLE a (x int);")
    run_migrations(empty_db, migrations_dir=tmp_path)
    mig.write_text("CREATE TABLE a (x bigint);")
    with pytest.raises(MigrationError, match="drift"):
        run_migrations(empty_db, migrations_dir=tmp_path)


def test_failed_migration_is_rolled_back_and_not_recorded(empty_db, tmp_path: Path):
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok_t (x int);")
    # First statement succeeds, second fails: the whole file must roll back.
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half_t (x int); SELECT * FROM nope;")
    with pytest.raises(MigrationError, match="002_bad.sql"):
        run_migrations(empty_db, migrations_dir=tmp_path)
    assert _ledger(empty_db) == ["001_ok.sql"]
    assert _table_exists(empty_db, "ok_t")
    assert not _table_exists(empty_db, "half_t")
