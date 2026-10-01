"""Thin PostgreSQL helpers."""
from __future__ import annotations

import psycopg2


def connect(database_url: str):
    return psycopg2.connect(database_url)


def migration_count(database_url: str) -> int:
    """Number of rows in schema_migrations (raises if the DB is unreachable)."""
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.schema_migrations;")
            return cur.fetchone()[0]
    finally:
        conn.close()
