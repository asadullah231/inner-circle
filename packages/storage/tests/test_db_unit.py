"""Unit tests for the assets DB adapter — NO database, NO psycopg required.

These run in the ordinary `python-tests` CI job (which does not install
psycopg), which is what proves db.py needs no driver at import or call time. A
tiny fake connection records the SQL + params each function would send.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.storage import db


class FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every execute() and returns a canned result. Never touches a DB."""

    def __init__(self, row=None, rows=None):
        self.calls: list[tuple] = []  # (sql, params)
        self._row = row
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return FakeCursor(row=self._row, rows=self._rows)

    @property
    def last_sql(self):
        return self.calls[-1][0]

    @property
    def last_params(self):
        return self.calls[-1][1]


def _staged():
    return {
        "storage_key": "assets/9f/a_9f2c4e1b7d0a5c33.mp4",
        "file_hash": "9f" + "0" * 62,
        "local_uri": "s3://avg-media/assets/9f/a_9f2c4e1b7d0a5c33.mp4",
        "media_type": "video",
        "width": 1920,
        "height": 1080,
        "duration_s": 4.25,
        "size_bytes": 1024,
        "downloaded_at": "2026-08-26T10:00:00+00:00",
        # extra key from staging that is NOT a column — must be dropped:
        "deduplicated": "SENTINEL_SHOULD_NOT_APPEAR",
    }


def _provider():
    return {
        "provider": "pexels",
        "provider_asset_id": "12345",
        "source_url": "https://pexels.com/12345",
        "license": "pexels",
        "attribution": "Jane Doe",
        "allowed_use": "commercial",
        "bogus": "ALSO_SHOULD_NOT_APPEAR",  # extra, must be dropped
    }


def _fake_row(**overrides):
    base = {c: None for c in db._ROW_COLUMNS}
    base.update(
        {
            "id": uuid4(),
            "storage_key": "assets/9f/a_9f2c4e1b7d0a5c33.mp4",
            "file_hash": "9f" + "0" * 62,
            "media_type": "video",
            "created_at": datetime.now(timezone.utc),
        }
    )
    base.update(overrides)
    return tuple(base[c] for c in db._ROW_COLUMNS)


# --- insert -----------------------------------------------------------------


def test_insert_builds_upsert_sql():
    conn = FakeConn(row=(uuid4(), True))
    db.insert_or_get_asset(conn, _staged(), _provider())
    sql = conn.last_sql
    assert "INSERT INTO assets" in sql
    assert "ON CONFLICT (file_hash) DO UPDATE SET file_hash = EXCLUDED.file_hash" in sql
    assert "RETURNING id, (xmax = 0) AS is_new" in sql
    # exactly one placeholder per insert column, no more
    assert sql.count("%s") == len(db._INSERT_COLUMNS)


def test_insert_params_are_whitelisted_and_ordered():
    conn = FakeConn(row=(uuid4(), True))
    db.insert_or_get_asset(conn, _staged(), _provider())
    params = list(conn.last_params)
    assert len(params) == len(db._INSERT_COLUMNS)
    # unknown fields never leak into the parameter list
    assert "SENTINEL_SHOULD_NOT_APPEAR" not in params
    assert "ALSO_SHOULD_NOT_APPEAR" not in params
    # values land at the right positions
    assert params[db._INSERT_COLUMNS.index("storage_key")] == _staged()["storage_key"]
    assert params[db._INSERT_COLUMNS.index("provider")] == "pexels"
    assert params[db._INSERT_COLUMNS.index("size_bytes")] == 1024
    assert params[db._INSERT_COLUMNS.index("allowed_use")] == "commercial"


def test_all_staged_and_provider_values_present():
    conn = FakeConn(row=(uuid4(), True))
    db.insert_or_get_asset(conn, _staged(), _provider())
    params = list(conn.last_params)
    for col in db._STAGED_COLUMNS:
        if col == "downloaded_at":
            continue  # converted; checked separately
        assert _staged()[col] in params
    for col in db._PROVIDER_COLUMNS:
        assert _provider()[col] in params


def test_downloaded_at_string_is_converted_to_datetime():
    conn = FakeConn(row=(uuid4(), True))
    db.insert_or_get_asset(conn, _staged(), _provider())
    dt = list(conn.last_params)[db._INSERT_COLUMNS.index("downloaded_at")]
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None  # tz-aware, as the column requires


def test_missing_or_blank_provider_raises():
    conn = FakeConn(row=(uuid4(), True))
    for bad in ({}, {"provider": ""}, {"provider": "   "}, {"provider": None}):
        with pytest.raises(ValueError):
            db.insert_or_get_asset(conn, _staged(), bad)


def test_is_new_reflects_returned_xmax_flag():
    new_id = uuid4()
    aid, is_new = db.insert_or_get_asset(FakeConn(row=(new_id, True)), _staged(), _provider())
    assert aid == new_id and is_new is True

    aid2, is_new2 = db.insert_or_get_asset(FakeConn(row=(new_id, False)), _staged(), _provider())
    assert aid2 == new_id and is_new2 is False


# --- reads ------------------------------------------------------------------


def test_get_by_hash_selects_and_coerces_numeric_to_float():
    row = _fake_row(duration_s=Decimal("4.250"), quality_score=Decimal("0.80"))
    conn = FakeConn(row=row)
    rec = db.get_asset_by_hash(conn, "9fabc")
    assert "WHERE file_hash = %s" in conn.last_sql
    assert conn.last_params == ("9fabc",)
    assert isinstance(rec["duration_s"], float) and rec["duration_s"] == 4.25
    assert isinstance(rec["quality_score"], float)
    assert rec["storage_key"] == "assets/9f/a_9f2c4e1b7d0a5c33.mp4"


def test_get_by_id_uses_id_predicate_and_returns_none_when_absent():
    conn = FakeConn(row=None)
    assert db.get_asset_by_id(conn, uuid4()) is None
    assert "WHERE id = %s" in conn.last_sql


def test_list_in_use_returns_keys_and_joins_beats():
    conn = FakeConn(rows=[("assets/9f/a_x.mp4",), ("assets/ab/a_y.mp4",)])
    keys = db.list_in_use_asset_keys(conn)
    assert keys == ["assets/9f/a_x.mp4", "assets/ab/a_y.mp4"]
    assert "JOIN beats" in conn.last_sql and "DISTINCT" in conn.last_sql


def test_touch_last_used_executes_update():
    conn = FakeConn()
    aid = uuid4()
    db.touch_asset_last_used(conn, aid)
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "UPDATE assets SET last_used_at = now()" in sql
    assert params == (aid,)


# --- recently used ----------------------------------------------------------


def test_list_recently_used_returns_keys_and_uses_days_param():
    conn = FakeConn(rows=[("assets/aa/a_1.mp4",), ("assets/bb/a_2.mp4",)])
    keys = db.list_recently_used_asset_keys(conn, days=30)
    assert keys == ["assets/aa/a_1.mp4", "assets/bb/a_2.mp4"]
    assert "last_used_at" in conn.last_sql
    assert conn.last_params == (30,)


def test_list_recently_used_default_days_is_30():
    conn = FakeConn(rows=[])
    db.list_recently_used_asset_keys(conn)
    assert conn.last_params == (30,)


# --- insert render ----------------------------------------------------------


def test_insert_render_builds_correct_sql():
    conn = FakeConn(row=("r_20260830T120000Z",))
    is_new = db.insert_render(
        conn,
        render_id="r_20260830T120000Z",
        project_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        mp4_key="projects/p1/renders/r_20260830T120000Z/final.mp4",
        manifest_key="projects/p1/manifests/r_20260830T120000Z/manifest.json",
        thumbnail_key="projects/p1/thumbs/r_20260830T120000Z/thumb.jpg",
        captions_key=None,
    )
    assert is_new is True
    sql = conn.last_sql
    assert "INSERT INTO renders" in sql
    assert "ON CONFLICT (render_id) DO NOTHING" in sql
    assert "RETURNING render_id" in sql
    params = conn.last_params
    assert params[0] == "r_20260830T120000Z"  # render_id
    assert params[1] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # project_id
    assert params[2] == "projects/p1/renders/r_20260830T120000Z/final.mp4"  # mp4_key
    assert params[3] == "projects/p1/thumbs/r_20260830T120000Z/thumb.jpg"  # thumbnail_key
    assert params[4] is None  # captions_key
    assert params[5] == "projects/p1/manifests/r_20260830T120000Z/manifest.json"  # manifest_key


def test_insert_render_returns_false_on_conflict():
    # ON CONFLICT DO NOTHING returns no row -> fetchone() returns None -> is_new=False
    conn = FakeConn(row=None)
    is_new = db.insert_render(
        conn,
        render_id="r_dup",
        project_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        mp4_key="k.mp4",
        manifest_key="k.json",
    )
    assert is_new is False
