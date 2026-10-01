"""Fixtures for tests that need PostgreSQL.

Set TEST_DATABASE_URL to a server where the user may CREATE DATABASE, e.g.
    postgresql://postgres:test@localhost:55432/postgres
Each test gets a throwaway database that is dropped afterwards. Without the
variable, tests marked `db` are skipped.
"""
from __future__ import annotations

import os
import uuid

import psycopg2
import pytest
from psycopg2.extensions import make_dsn, parse_dsn

from database.migrate import run_migrations

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if ADMIN_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def _admin_exec(sql: str) -> None:
    conn = psycopg2.connect(ADMIN_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


@pytest.fixture()
def empty_db():
    """URL of a fresh, empty database; always dropped afterwards."""
    name = f"tl_test_{uuid.uuid4().hex[:10]}"
    _admin_exec(f'CREATE DATABASE "{name}"')
    try:
        yield make_dsn(**{**parse_dsn(ADMIN_URL), "dbname": name})
    finally:
        _admin_exec(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def migrated_db(empty_db):
    """Connection (autocommit) to a database with all real migrations applied."""
    run_migrations(empty_db)
    conn = psycopg2.connect(empty_db)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()
