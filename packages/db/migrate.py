"""
Minimal forward-only migration runner.

Why not Alembic: the schema is split across two lanes (this repo owns the
workflow tables, the storage lane owns `assets`), and Alembic's single-head
model fights that. Plain numbered SQL files applied in order, tracked in a
`schema_migrations` table, is enough for M1 and stays readable for whoever
picks this up next.

Rules:
  * Files are `NNN_name.sql`, applied in filename order, never edited once
    applied (add a new file instead).
  * Each file runs inside one transaction; a failure rolls that file back.
  * `applied` is keyed on the filename, so re-running is a no-op.

The caller supplies the connection — this module never opens one, matching the
convention set by packages/storage/db.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

log = logging.getLogger("db.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


def discover(migrations_dir: Path | None = None) -> list[Path]:
    """Return migration files in apply order."""
    d = migrations_dir or MIGRATIONS_DIR
    if not d.is_dir():
        raise MigrationError(f"migrations directory not found: {d}")
    files = sorted(p for p in d.glob("*.sql") if p.is_file())
    seen_prefixes: set[str] = set()
    for f in files:
        prefix = f.name.split("_", 1)[0]
        if not prefix.isdigit():
            raise MigrationError(f"migration filename must start with digits: {f.name}")
        if prefix in seen_prefixes:
            raise MigrationError(f"duplicate migration number: {prefix}")
        seen_prefixes.add(prefix)
    return files


def applied_migrations(conn: "Connection") -> set[str]:
    """Filenames already applied. Creates the tracking table if missing."""
    with conn.cursor() as cur:
        cur.execute(_TRACKING_TABLE)
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def pending(conn: "Connection", migrations_dir: Path | None = None) -> list[Path]:
    done = applied_migrations(conn)
    return [f for f in discover(migrations_dir) if f.name not in done]


def apply_migration(conn: "Connection", path: Path) -> None:
    """Apply one file and record it. Caller owns commit."""
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s) "
            "ON CONFLICT (filename) DO NOTHING",
            (path.name,),
        )
    log.info("applied migration %s", path.name)


def run(conn: "Connection", migrations_dir: Path | None = None) -> list[str]:
    """
    Apply every pending migration in order. Returns the filenames applied.

    Commits after each file so a later failure does not undo earlier ones.
    """
    applied: list[str] = []
    for path in pending(conn, migrations_dir):
        try:
            apply_migration(conn, path)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise MigrationError(f"migration {path.name} failed: {exc}") from exc
        applied.append(path.name)
    return applied
