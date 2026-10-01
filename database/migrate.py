#!/usr/bin/env python3
"""database/migrate.py — schema migration runner (same pattern as STAIR).

Usage:
    python database/migrate.py [--db-url URL] [--migrations-dir DIR] [--dry-run]

URL resolution order:
    1. --db-url flag
    2. DATABASE_URL environment variable
    Neither set → exit with an error. There is no default, to avoid running
    against the wrong database.

Design:
    - Files in database/migrations/*.sql are applied in lexicographic order.
    - A schema_migrations ledger records (filename, sha256, applied_at).
      Already-applied files are skipped; a checksum mismatch raises
      MigrationError (drift guard). Applied files are never edited: changes go
      in a new file.
    - Difference from STAIR: each file runs in ONE transaction together with
      its ledger INSERT, so a crash can never leave a migration applied but
      unrecorded. Migration files must therefore NOT contain BEGIN/COMMIT.
    - A failed migration is rolled back and not recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   TEXT        PRIMARY KEY,
    sha256     TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(Exception):
    """Raised when a migration fails to apply or drift is detected."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_migrations(
    db_url: str,
    migrations_dir: str | Path = MIGRATIONS_DIR,
    dry_run: bool = False,
) -> dict:
    """Apply pending SQL migrations in lexicographic order.

    Returns a summary dict with keys 'applied' and 'skipped'.
    Raises MigrationError on drift or SQL failure (failed migration NOT recorded).
    """
    migrations_dir = Path(migrations_dir)
    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        raise MigrationError(f"no .sql files found in {migrations_dir}")

    try:
        conn = psycopg2.connect(db_url)
    except Exception as exc:
        raise MigrationError(f"cannot connect to database: {exc}") from exc

    try:
        with conn, conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
            cur.execute("SELECT filename, sha256 FROM public.schema_migrations;")
            applied: dict[str, str] = dict(cur.fetchall())

        n_applied = 0
        n_skipped = 0

        for mig_path in migration_files:
            filename = mig_path.name
            checksum = sha256_file(mig_path)

            if filename in applied:
                if applied[filename] != checksum:
                    raise MigrationError(
                        f"drift: {filename} has changed since it was applied.\n"
                        f"  stored : {applied[filename][:16]}…\n"
                        f"  current: {checksum[:16]}…\n"
                        "Restore the file; put schema changes in a new migration."
                    )
                print(f"  skip  {filename}")
                n_skipped += 1
                continue

            if dry_run:
                print(f"  would apply  {filename}")
                continue

            sql = mig_path.read_text(encoding="utf-8")
            try:
                # `with conn` commits on success and rolls back on any error,
                # so the SQL and its ledger row land together or not at all.
                with conn, conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO public.schema_migrations (filename, sha256)"
                        " VALUES (%s, %s);",
                        (filename, checksum),
                    )
            except Exception as exc:
                raise MigrationError(f"applying {filename}: {exc}") from exc

            print(f"  apply {filename}")
            n_applied += 1

        return {"applied": n_applied, "skipped": n_skipped}

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply SQL migrations in lexicographic order with a ledger."
    )
    parser.add_argument("--db-url", dest="db_url", default=None,
                        help="PostgreSQL connection URL (overrides DATABASE_URL)")
    parser.add_argument("--migrations-dir", dest="migrations_dir", default=None,
                        help="Directory with .sql migration files (default: database/migrations/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print which migrations would be applied without executing them.")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print(
            "ERROR: no database URL provided.\n"
            "Use --db-url <URL> or set the DATABASE_URL environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    kwargs: dict = {"db_url": db_url, "dry_run": args.dry_run}
    if args.migrations_dir:
        kwargs["migrations_dir"] = args.migrations_dir

    try:
        summary = run_migrations(**kwargs)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"\nDry-run: {summary['applied']} would be applied,"
              f" {summary['skipped']} already recorded.")
    else:
        print(f"\nDone: {summary['applied']} applied, {summary['skipped']} skipped.")


if __name__ == "__main__":
    main()
