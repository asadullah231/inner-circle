"""Unit tests for the migration runner. No database driver required."""

from __future__ import annotations

import pytest

from packages.db import migrate


# --- fake connection --------------------------------------------------------
class FakeCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.store["executed"].append((sql.strip(), params))
        if self.store.get("fail_on") and self.store["fail_on"] in sql:
            raise RuntimeError("boom")

    def fetchall(self):
        return [(name,) for name in self.store["applied"]]


class FakeConn:
    def __init__(self, applied=(), fail_on=None):
        self.store = {"applied": list(applied), "executed": [], "fail_on": fail_on}
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# --- discover ---------------------------------------------------------------
def test_discover_finds_shipped_migration():
    files = migrate.discover()
    assert files, "no migrations found"
    assert files[0].name == "001_core_schema.sql"


def test_discover_orders_numerically(tmp_path):
    for name in ("003_c.sql", "001_a.sql", "002_b.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    assert [f.name for f in migrate.discover(tmp_path)] == [
        "001_a.sql", "002_b.sql", "003_c.sql",
    ]


def test_discover_rejects_unnumbered_file(tmp_path):
    (tmp_path / "core.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="must start with digits"):
        migrate.discover(tmp_path)


def test_discover_rejects_duplicate_number(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="duplicate migration number"):
        migrate.discover(tmp_path)


def test_discover_missing_dir(tmp_path):
    with pytest.raises(migrate.MigrationError, match="not found"):
        migrate.discover(tmp_path / "nope")


# --- pending / run ----------------------------------------------------------
def test_pending_excludes_applied(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = FakeConn(applied=["001_a.sql"])
    assert [f.name for f in migrate.pending(conn, tmp_path)] == ["002_b.sql"]


def test_run_applies_in_order_and_commits(tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a();", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b();", encoding="utf-8")
    conn = FakeConn()
    assert migrate.run(conn, tmp_path) == ["001_a.sql", "002_b.sql"]
    assert conn.commits == 2
    assert conn.rollbacks == 0


def test_run_is_noop_when_all_applied(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = FakeConn(applied=["001_a.sql"])
    assert migrate.run(conn, tmp_path) == []
    assert conn.commits == 0


def test_run_records_filename(tmp_path):
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a();", encoding="utf-8")
    conn = FakeConn()
    migrate.run(conn, tmp_path)
    inserts = [e for e in conn.store["executed"] if "schema_migrations" in e[0] and "INSERT" in e[0]]
    assert inserts and inserts[0][1] == ("001_a.sql",)


def test_run_rolls_back_and_raises_on_failure(tmp_path):
    (tmp_path / "001_bad.sql").write_text("CREATE TABLE boom();", encoding="utf-8")
    conn = FakeConn(fail_on="boom")
    with pytest.raises(migrate.MigrationError, match="001_bad.sql failed"):
        migrate.run(conn, tmp_path)
    assert conn.rollbacks == 1
    assert conn.commits == 0


# --- shipped SQL sanity (text-level, no DB) ---------------------------------
def test_core_schema_creates_expected_tables():
    sql = (migrate.MIGRATIONS_DIR / "001_core_schema.sql").read_text(encoding="utf-8")
    for table in ("projects", "jobs", "beats", "approvals", "audit_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table


def test_core_schema_does_not_create_assets():
    """`assets` belongs to the storage lane — creating it here would collide."""
    sql = (migrate.MIGRATIONS_DIR / "001_core_schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS assets" not in sql
    assert "CREATE TABLE assets" not in sql


def test_core_schema_is_idempotent_by_construction():
    sql = (migrate.MIGRATIONS_DIR / "001_core_schema.sql").read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE ") == sql.count("CREATE TABLE IF NOT EXISTS ")
    assert sql.count("CREATE INDEX ") == sql.count("CREATE INDEX IF NOT EXISTS ")
