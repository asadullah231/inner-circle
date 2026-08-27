"""
Project and job persistence for the API.

Same convention as packages/db/transitions.py and packages/storage/db.py:
the caller supplies the connection and owns commit/rollback, psycopg is only
imported for typing, and every query is parameterised.

State is never written here — jobs move only through
packages.db.transitions.transition(), so the state machine cannot be bypassed
by an endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from packages.db.states import JobState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

PROJECT_COLS = (
    "id", "name", "brief", "language", "format_w", "format_h", "fps",
    "created_by", "created_at", "updated_at",
)
JOB_COLS = (
    "id", "project_id", "state", "video_spec", "idempotency_key", "attempt",
    "error", "started_at", "finished_at", "created_at", "updated_at",
)

_P = ", ".join(PROJECT_COLS)
_J = ", ".join(JOB_COLS)


class ProjectNotFound(LookupError):
    def __init__(self, project_id: UUID | str):
        super().__init__(f"project not found: {project_id}")


def _row(cols: tuple[str, ...], row: tuple[Any, ...] | None) -> Optional[dict[str, Any]]:
    return dict(zip(cols, row)) if row is not None else None


# --- projects ---------------------------------------------------------------
def create_project(
    conn: "Connection",
    *,
    name: str,
    brief: Optional[str] = None,
    language: str = "en",
    format_w: int = 1080,
    format_h: int = 1920,
    fps: int = 30,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (name, brief, language, format_w, format_h, fps, created_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_P}",
            (name, brief, language, format_w, format_h, fps, created_by),
        )
        return _row(PROJECT_COLS, cur.fetchone())  # type: ignore[return-value]


def get_project(conn: "Connection", project_id: UUID | str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_P} FROM projects WHERE id = %s", (str(project_id),))
        row = _row(PROJECT_COLS, cur.fetchone())
    if row is None:
        raise ProjectNotFound(project_id)
    return row


def list_projects(conn: "Connection", *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_P} FROM projects ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return [dict(zip(PROJECT_COLS, r)) for r in cur.fetchall()]


def update_project(
    conn: "Connection", project_id: UUID | str, **fields: Any
) -> dict[str, Any]:
    """Partial update. Unknown or None fields are ignored; no field is a no-op read."""
    allowed = {"name", "brief", "language", "format_w", "format_h", "fps"}
    sets, params = [], []
    for key in sorted(allowed & fields.keys()):
        if fields[key] is None:
            continue
        sets.append(f"{key} = %s")
        params.append(fields[key])
    if not sets:
        return get_project(conn, project_id)
    params.append(str(project_id))
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = %s RETURNING {_P}", params
        )
        row = _row(PROJECT_COLS, cur.fetchone())
    if row is None:
        raise ProjectNotFound(project_id)
    return row


def delete_project(conn: "Connection", project_id: UUID | str) -> bool:
    """Cascades to jobs, beats, approvals and audit events (see 001_core_schema.sql)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE id = %s", (str(project_id),))
        return cur.rowcount > 0


# --- jobs -------------------------------------------------------------------
def create_job(
    conn: "Connection",
    *,
    project_id: UUID | str,
    idempotency_key: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """
    Create a job in `draft`.

    Returns (job, created). When an idempotency key is replayed the existing
    job is returned with created=False, so a retried POST /jobs cannot start a
    second production run of the same request.
    """
    if idempotency_key:
        existing = get_job_by_key(conn, idempotency_key)
        if existing is not None:
            return existing, False

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM projects WHERE id = %s", (str(project_id),))
        if cur.fetchone() is None:
            raise ProjectNotFound(project_id)
        cur.execute(
            "INSERT INTO jobs (project_id, state, idempotency_key) "
            f"VALUES (%s, 'draft', %s) RETURNING {_J}",
            (str(project_id), idempotency_key),
        )
        return _row(JOB_COLS, cur.fetchone()), True  # type: ignore[return-value]


def get_job(conn: "Connection", job_id: UUID | str) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_J} FROM jobs WHERE id = %s", (str(job_id),))
        return _row(JOB_COLS, cur.fetchone())


def get_job_by_key(conn: "Connection", idempotency_key: str) -> Optional[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_J} FROM jobs WHERE idempotency_key = %s", (idempotency_key,))
        return _row(JOB_COLS, cur.fetchone())


def list_jobs(
    conn: "Connection",
    *,
    project_id: Optional[UUID | str] = None,
    state: Optional[JobState] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = [], []
    if project_id is not None:
        where.append("project_id = %s")
        params.append(str(project_id))
    if state is not None:
        where.append("state = %s")
        params.append(state.value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_J} FROM jobs{clause} ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            params,
        )
        return [dict(zip(JOB_COLS, r)) for r in cur.fetchall()]


def get_approvals(conn: "Connection", job_id: UUID | str) -> list[dict[str, Any]]:
    cols = ("gate", "decision", "decided_by", "decided_at", "note")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM approvals WHERE job_id = %s ORDER BY gate",
            (str(job_id),),
        )
        return [dict(zip(cols, r)) for r in cur.fetchall()]
