"""Integration tests for the assets DB adapter against a REAL Postgres.

Skipped unless RUN_STORAGE_DB_INTEGRATION=1 AND psycopg is installed AND a
reachable Postgres is configured via DATABASE_URL (or the standard libpq PG*
env vars). CI runs these in the `storage-db-integration` job against a
postgres:16 service container; see .github/workflows/ci.yml.

NOTE: Docker does not run on the author's machine, so as of this PR these tests
were NOT executed locally — they are verified in CI only. The concurrent-insert
test in particular needs a real Postgres and is the reason this suite exists;
it is not skipped.

Schema is bootstrapped from tests/fixtures/assets_schema.sql (which also creates
a minimal stand-in `beats` table — see that file's header).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from packages.storage import db

pytestmark = [
    pytest.mark.db_integration,
    pytest.mark.skipif(
        os.getenv("RUN_STORAGE_DB_INTEGRATION") != "1",
        reason="set RUN_STORAGE_DB_INTEGRATION=1 and provide a Postgres to enable",
    ),
]

# Imported here (not at top) so the ordinary, driver-free test job can collect
# this module and cleanly skip it instead of erroring.
psycopg = pytest.importorskip("psycopg")

_SCHEMA_SQL = (Path(__file__).parent / "fixtures" / "assets_schema.sql").read_text()


def _connect():
    dsn = os.environ.get("DATABASE_URL")
    return psycopg.connect(dsn) if dsn else psycopg.connect()


def _apply_schema(conn) -> None:
    """Run the migration file if the schema is not already present. Splitting on
    ';' is safe here — the fixture DDL has no embedded semicolons."""
    exists = conn.execute("SELECT to_regclass('public.assets')").fetchone()[0]
    if exists is not None:
        return
    for statement in _SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()


@pytest.fixture(scope="module")
def _schema():
    conn = _connect()
    try:
        _apply_schema(conn)
    finally:
        conn.close()


@pytest.fixture
def conn(_schema):
    connection = _connect()
    connection.execute("TRUNCATE beats, assets RESTART IDENTITY CASCADE")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


# --- helpers ----------------------------------------------------------------


def _hash(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def _staged(hash_hex: str, **overrides) -> dict:
    base = {
        "storage_key": f"assets/{hash_hex[:2]}/a_{hash_hex[:16]}.mp4",
        "file_hash": hash_hex,
        "local_uri": f"s3://avg-media/assets/{hash_hex[:2]}/a_{hash_hex[:16]}.mp4",
        "media_type": "video",
        "width": 1920,
        "height": 1080,
        "duration_s": 4.25,
        "size_bytes": 2048,
        "downloaded_at": "2026-08-26T10:00:00+00:00",
        "deduplicated": False,  # extra key; must be ignored by the adapter
    }
    base.update(overrides)
    return base


def _provider(**overrides) -> dict:
    base = {
        "provider": "pexels",
        "provider_asset_id": "12345",
        "source_url": "https://pexels.com/12345",
        "license": "pexels",
        "attribution": "Jane Doe",
        "allowed_use": "commercial",
    }
    base.update(overrides)
    return base


def _count_assets(conn) -> int:
    return conn.execute("SELECT count(*) FROM assets").fetchone()[0]


def _add_beat(conn, asset_id: UUID) -> None:
    conn.execute("INSERT INTO beats (id, asset_id) VALUES (%s, %s)", (uuid4(), asset_id))


# --- tests ------------------------------------------------------------------


def test_new_hash_inserts_new_row(conn):
    asset_id, is_new = db.insert_or_get_asset(conn, _staged(_hash("new")), _provider())
    conn.commit()
    assert is_new is True
    assert isinstance(asset_id, UUID)
    assert _count_assets(conn) == 1


def test_same_hash_returns_same_id_and_no_duplicate(conn):
    h = _hash("dup")
    id1, new1 = db.insert_or_get_asset(conn, _staged(h), _provider())
    conn.commit()
    # second attempt with different provider + storage_key
    id2, new2 = db.insert_or_get_asset(
        conn,
        _staged(h, storage_key="assets/zz/a_other.mp4"),
        _provider(provider="pixabay"),
    )
    conn.commit()

    assert new1 is True and new2 is False
    assert id1 == id2
    assert _count_assets(conn) == 1
    # first writer wins: the conflict path does NOT overwrite provider/storage_key
    row = db.get_asset_by_id(conn, id1)
    assert row["provider"] == "pexels"
    assert row["storage_key"] == _staged(h)["storage_key"]


def test_get_by_hash_and_id_round_trip(conn):
    h = _hash("round")
    asset_id, _ = db.insert_or_get_asset(conn, _staged(h), _provider())
    conn.commit()

    by_hash = db.get_asset_by_hash(conn, h)
    by_id = db.get_asset_by_id(conn, asset_id)
    assert by_hash["id"] == asset_id == by_id["id"]
    assert by_hash["storage_key"] == _staged(h)["storage_key"]
    assert by_hash["media_type"] == "video"
    assert by_hash["size_bytes"] == 2048
    # NUMERIC coerced to float (contract parity), TIMESTAMPTZ back as tz-aware
    assert isinstance(by_hash["duration_s"], float) and by_hash["duration_s"] == 4.25
    assert by_hash["downloaded_at"].tzinfo is not None


def test_missing_hash_and_id_return_none(conn):
    assert db.get_asset_by_hash(conn, _hash("absent")) is None
    assert db.get_asset_by_id(conn, uuid4()) is None


def test_null_dimensions_and_unknown_media_type_round_trip(conn):
    # audio / corrupt download: no width/height, media_type 'unknown'
    h = _hash("audio")
    staged = _staged(
        h,
        media_type="unknown",
        width=None,
        height=None,
        duration_s=12.0,
        storage_key=f"assets/{h[:2]}/a_{h[:16]}.wav",
    )
    asset_id, _ = db.insert_or_get_asset(conn, staged, _provider())
    conn.commit()

    row = db.get_asset_by_id(conn, asset_id)
    assert row["media_type"] == "unknown"
    assert row["width"] is None and row["height"] is None
    assert row["duration_s"] == 12.0


def test_list_in_use_returns_only_referenced_keys(conn):
    used = _hash("used")
    unused = _hash("unused")
    used_id, _ = db.insert_or_get_asset(conn, _staged(used), _provider())
    db.insert_or_get_asset(
        conn, _staged(unused, storage_key="assets/uu/a_unused.mp4"), _provider()
    )
    _add_beat(conn, used_id)
    conn.commit()

    assert db.list_in_use_asset_keys(conn) == [_staged(used)["storage_key"]]


def test_asset_referenced_by_two_beats_appears_once(conn):
    h = _hash("twobeats")
    asset_id, _ = db.insert_or_get_asset(conn, _staged(h), _provider())
    _add_beat(conn, asset_id)
    _add_beat(conn, asset_id)
    conn.commit()

    assert db.list_in_use_asset_keys(conn) == [_staged(h)["storage_key"]]


def test_concurrent_inserts_same_hash_return_same_id(conn):
    """The race ON CONFLICT protects against: two connections insert the same
    hash at once. Both must succeed, return the SAME id, leave exactly one row,
    and report exactly one is_new=True. Tested for real, not assumed."""
    h = _hash("race")
    barrier = threading.Barrier(2)
    results: list[tuple[UUID, bool]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        c = _connect()
        try:
            barrier.wait(timeout=10)  # line both inserts up
            asset_id, is_new = db.insert_or_get_asset(c, _staged(h), _provider())
            c.commit()
            with lock:
                results.append((asset_id, is_new))
        except Exception as exc:  # noqa: BLE001 - surfaced through `errors`
            with lock:
                errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, f"a concurrent insert raised: {errors}"
    assert len(results) == 2
    assert len({asset_id for asset_id, _ in results}) == 1, "different ids returned"
    assert sum(1 for _, is_new in results if is_new) == 1, "exactly one should be new"
    assert _count_assets(conn) == 1
