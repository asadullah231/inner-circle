"""Assets-table adapter.

Persists the metadata the storage layer already returns (contract §9) and
becomes the source of truth for dedup and for the retention cleanup job's
in-use set.

Design rules (Task 2 brief, Part 4):
  * Never opens a connection or manages a pool. Every function takes a
    caller-supplied psycopg connection; the API service will own the pool and
    hand a connection in. (A pooled connection is still just a Connection.)
  * Never commits or rolls back — transaction boundaries belong to the caller.
    This is what makes the concurrent-insert behaviour testable.
  * Parameterised queries only (%s placeholders). SQL is never built from
    caller data; the only interpolation is of the *static* column-name
    constants below, which are not user input.
  * Does NOT import psycopg at runtime, so the unit tests — and the whole
    storage suite — run with no database driver installed. The connection is
    duck-typed; psycopg is imported only under TYPE_CHECKING.

Dedup authority: once this adapter is wired in, `is_new` from the DB is the
source of truth for deduplication. The storage layer's `deduplicated` flag
(from a MinIO exists() check) is only an upload-skip optimisation and can lag
the DB — do not build dedup logic on it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from psycopg import Connection

log = logging.getLogger("storage.db")


# --- column whitelists ------------------------------------------------------
# Anything not named here (e.g. staging's `deduplicated` flag) is dropped rather
# than smuggled into SQL. Order defines the INSERT / VALUES / RETURNING mapping.

_STAGED_COLUMNS = (
    "storage_key",
    "file_hash",
    "local_uri",
    "media_type",
    "width",
    "height",
    "duration_s",
    "size_bytes",
    "downloaded_at",
)
_PROVIDER_COLUMNS = (
    "provider",
    "provider_asset_id",
    "source_url",
    "license",
    "attribution",
    "allowed_use",
)
_INSERT_COLUMNS = _STAGED_COLUMNS + _PROVIDER_COLUMNS

# Full row shape returned by get_asset_by_* (explicit SELECT, so table column
# order does not matter).
_ROW_COLUMNS = (
    "id",
    *_INSERT_COLUMNS,
    "embedding_uri",
    "quality_score",
    "created_at",
)

# NUMERIC comes back from psycopg as Decimal; the AssetRecord contract types
# these as float, so coerce on read for parity.
_FLOAT_COLUMNS = frozenset({"duration_s", "quality_score"})


# --- SQL (static; identifiers are constants, values are always %s) ----------

_INSERT_SQL = (
    "INSERT INTO assets (" + ", ".join(_INSERT_COLUMNS) + ") "
    "VALUES (" + ", ".join(["%s"] * len(_INSERT_COLUMNS)) + ") "
    # DO UPDATE (not DO NOTHING) with a no-op SET makes RETURNING return the row
    # in BOTH the insert and the conflict case, so the caller always gets an id
    # even on a dedup hit. DO NOTHING would return nothing on conflict and a
    # follow-up SELECT can miss an as-yet-uncommitted concurrent insert.
    "ON CONFLICT (file_hash) DO UPDATE SET file_hash = EXCLUDED.file_hash "
    # (xmax = 0) is the standard trick for telling insert from conflict apart.
    "RETURNING id, (xmax = 0) AS is_new"
)

_SELECT_BY_HASH_SQL = (
    "SELECT " + ", ".join(_ROW_COLUMNS) + " FROM assets WHERE file_hash = %s"
)
_SELECT_BY_ID_SQL = "SELECT " + ", ".join(_ROW_COLUMNS) + " FROM assets WHERE id = %s"

# An asset is 'in use' if any beat points at it. DISTINCT so an asset referenced
# by several beats appears once. A NULL storage_key can't be an object key.
_IN_USE_SQL = (
    "SELECT DISTINCT a.storage_key "
    "FROM assets a JOIN beats b ON b.asset_id = a.id "
    "WHERE a.storage_key IS NOT NULL"
)


# --- helpers ----------------------------------------------------------------


def _coerce_downloaded_at(value: Any) -> Optional[datetime]:
    """staging.stage_asset() returns downloaded_at as an ISO-8601 string (it is
    also an HTTP/JSON field, where a string is correct). The column is
    TIMESTAMPTZ, so normalise to a tz-aware datetime here rather than lean on an
    implicit text->timestamptz cast."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(
        f"downloaded_at must be a datetime or ISO string, got {type(value).__name__}"
    )


def _insert_params(staged: dict, provider_fields: dict) -> list[Any]:
    """Pick exactly the insertable columns, in _INSERT_COLUMNS order. Unknown
    keys (e.g. `deduplicated`) are ignored."""
    provider = provider_fields.get("provider")
    if not provider or not str(provider).strip():
        # provider is NOT NULL and must be real. Never silently insert a
        # placeholder — that would hide a bug in the caller.
        raise ValueError("provider is required and must be a non-empty string")

    values: list[Any] = []
    for col in _STAGED_COLUMNS:
        value = staged.get(col)
        if col == "downloaded_at":
            value = _coerce_downloaded_at(value)
        values.append(value)
    for col in _PROVIDER_COLUMNS:
        values.append(provider_fields.get(col))
    return values


def _row_to_dict(row: Optional[tuple]) -> Optional[dict]:
    if row is None:
        return None
    record = dict(zip(_ROW_COLUMNS, row))
    for col in _FLOAT_COLUMNS:
        if record.get(col) is not None:
            record[col] = float(record[col])
    return record


# --- public interface -------------------------------------------------------


def insert_or_get_asset(
    conn: "Connection", staged: dict, provider_fields: dict
) -> tuple[UUID, bool]:
    """Insert an asset; if the file_hash already exists, return the existing id.

    Returns (asset_id, is_new). is_new is True when this call created the row,
    False when it was a dedup hit — this is the dedup source of truth.

    staged: the dict staging.stage_asset() returns (storage_key, file_hash,
      local_uri, media_type, width, height, duration_s, size_bytes,
      downloaded_at). Extra keys such as `deduplicated` are ignored.
    provider_fields: what the retrieval worker knows — provider (required),
      provider_asset_id, source_url, license, attribution, allowed_use.

    On a dedup hit the provider/licence fields are NOT refreshed (first writer
    wins) — see the PR description note for the rights owner.

    Does not commit; the caller owns the transaction.
    """
    params = _insert_params(staged, provider_fields)
    row = conn.execute(_INSERT_SQL, params).fetchone()
    asset_id, is_new = row[0], bool(row[1])
    log.info(
        "asset upsert hash=%s is_new=%s id=%s",
        staged.get("file_hash"),
        is_new,
        asset_id,
    )
    return asset_id, is_new


def get_asset_by_hash(conn: "Connection", file_hash: str) -> Optional[dict]:
    """Return the full asset row for a hash, or None."""
    row = conn.execute(_SELECT_BY_HASH_SQL, (file_hash,)).fetchone()
    return _row_to_dict(row)


def get_asset_by_id(conn: "Connection", asset_id: UUID) -> Optional[dict]:
    """Return the full asset row for an id, or None."""
    row = conn.execute(_SELECT_BY_ID_SQL, (asset_id,)).fetchone()
    return _row_to_dict(row)


def list_in_use_asset_keys(conn: "Connection") -> list[str]:
    """Return the storage_key of every asset referenced by a beat.

    This is the real in-use set for retention.run_cleanup — an asset in this
    list is never deleted regardless of age. It plugs straight into
    run_cleanup(in_use_keys=...), because storage_key is exactly the MinIO
    object key retention compares against.
    """
    return [r[0] for r in conn.execute(_IN_USE_SQL).fetchall()]


def touch_asset_last_used(conn: "Connection", asset_id: UUID) -> None:
    """Bump a last_used_at timestamp for the '30 days after last use' rule.

    TODO(mubashir): the assets table has no last_used_at column yet, and it is
    not ours to add. Two options are on the table (Task 2 brief, Part 5.1): add
    `assets.last_used_at TIMESTAMPTZ` and bump it from the beat-attach code, or
    derive MAX(beats.updated_at). Until that decision lands this is a no-op, so
    call sites can be wired now and start working when the column exists.
    list_in_use_asset_keys already protects in-use assets regardless of age, so
    nothing is deleted wrongly in the meantime.
    """
    # Intentionally does not touch the database yet.
    log.debug("touch_asset_last_used stub (no last_used_at column yet) id=%s", asset_id)
    return None
