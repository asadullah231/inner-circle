"""
Integration tests for the core schema against a REAL Postgres.

Skipped unless RUN_DB_INTEGRATION=1 AND psycopg is installed AND a reachable
Postgres is configured via DATABASE_URL (or the standard libpq PG* env vars).
CI runs these in the `core-db-integration` job against postgres:16.

Follows the convention set by packages/storage/tests/test_db_integration.py.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from packages.db import migrate

pytestmark = [
    pytest.mark.db_integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION") != "1",
        reason="set RUN_DB_INTEGRATION=1 and provide a Postgres to enable",
    ),
]

psycopg = pytest.importorskip("psycopg")


@pytest.fixture()
def conn():
    dsn = os.getenv("DATABASE_URL", "")
    c = psycopg.connect(dsn) if dsn else psycopg.connect()
    try:
        # Fresh schema per test so migrations are exercised from zero.
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.commit()
        yield c
    finally:
        c.close()


def _tables(c) -> set[str]:
    with c.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        return {r[0] for r in cur.fetchall()}


def test_run_creates_every_core_table(conn):
    applied = migrate.run(conn)
    assert "001_core_schema.sql" in applied
    tables = _tables(conn)
    for t in ("projects", "jobs", "beats", "approvals", "audit_events", "schema_migrations"):
        assert t in tables, f"missing {t}"


def test_run_is_idempotent(conn):
    migrate.run(conn)
    assert migrate.run(conn) == []          # nothing pending the second time
    assert "projects" in _tables(conn)      # and the schema survived


def test_rerunning_the_sql_directly_is_safe(conn):
    """Even outside the tracking table, the SQL itself must not blow up twice."""
    migrate.run(conn)
    sql = (migrate.MIGRATIONS_DIR / "001_core_schema.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def test_job_defaults_to_draft_and_cascades(conn):
    migrate.run(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        cur.execute("INSERT INTO jobs (project_id) VALUES (%s) RETURNING id, state",
                    (project_id,))
        job_id, state = cur.fetchone()
        assert state == "draft"

        cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        cur.execute("SELECT count(*) FROM jobs WHERE id = %s", (job_id,))
        assert cur.fetchone()[0] == 0, "job should cascade with its project"
    conn.commit()


def test_illegal_state_value_is_rejected(conn):
    migrate.run(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            cur.execute("INSERT INTO jobs (project_id, state) VALUES (%s, 'banana')",
                        (project_id,))
    conn.rollback()


def test_idempotency_key_is_unique(conn):
    migrate.run(conn)
    key = f"key-{uuid4()}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        cur.execute("INSERT INTO jobs (project_id, idempotency_key) VALUES (%s, %s)",
                    (project_id, key))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO jobs (project_id, idempotency_key) VALUES (%s, %s)",
                        (project_id, key))
    conn.rollback()


def test_one_approval_row_per_gate(conn):
    migrate.run(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        cur.execute("INSERT INTO jobs (project_id) VALUES (%s) RETURNING id", (project_id,))
        job_id = cur.fetchone()[0]
        cur.execute("INSERT INTO approvals (job_id, gate) VALUES (%s, 'g1_script')", (job_id,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO approvals (job_id, gate) VALUES (%s, 'g1_script')",
                        (job_id,))
    conn.rollback()


def test_updated_at_trigger_fires(conn):
    migrate.run(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id, updated_at")
        project_id, first = cur.fetchone()
        cur.execute("UPDATE projects SET name = 'renamed' WHERE id = %s RETURNING updated_at",
                    (project_id,))
        second = cur.fetchone()[0]
        assert second > first, "updated_at should advance on UPDATE"
    conn.commit()


def test_audit_events_accept_transitions(conn):
    migrate.run(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        cur.execute("INSERT INTO jobs (project_id) VALUES (%s) RETURNING id", (project_id,))
        job_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_events (job_id, project_id, event_type, from_state, to_state, actor) "
            "VALUES (%s, %s, 'job.transition', 'draft', 'planning', 'worker-1')",
            (job_id, project_id),
        )
        cur.execute("SELECT from_state, to_state FROM audit_events WHERE job_id = %s", (job_id,))
        assert cur.fetchone() == ("draft", "planning")
    conn.commit()
