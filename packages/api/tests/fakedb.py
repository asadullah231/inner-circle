"""
An in-memory stand-in for the psycopg pool the API depends on.

Rather than mock every repository function, this implements just enough of the
cursor protocol that the real SQL in repository.py and transitions.py runs
against a dict store. That keeps the tests honest about column order and about
which statements a handler actually issues, while needing no Postgres.

The real database is exercised in test_api_integration.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from packages.api.repository import JOB_COLS, PROJECT_COLS


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    """The tables, as dicts keyed by id."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.approvals: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.locked: list[str] = []   # jobs locked with FOR UPDATE
        self.fail_ping = False

    # -- helpers used by tests --
    def add_project(self, **over: Any) -> dict[str, Any]:
        row = {
            "id": str(uuid4()), "name": "Test project", "brief": None,
            "language": "en", "format_w": 1080, "format_h": 1920, "fps": 30,
            "created_by": None, "created_at": _now(), "updated_at": _now(),
        }
        row.update(over)
        self.projects[row["id"]] = row
        return row

    def add_job(self, project_id: str, **over: Any) -> dict[str, Any]:
        row = {
            "id": str(uuid4()), "project_id": project_id, "state": "draft",
            "video_spec": None, "idempotency_key": None, "attempt": 0,
            "error": None, "started_at": None, "finished_at": None,
            "created_at": _now(), "updated_at": _now(),
        }
        row.update(over)
        self.jobs[row["id"]] = row
        return row

    def approve(self, job_id: str, gate: str, decision: str = "approved") -> None:
        self.approvals[(job_id, gate)] = {
            "job_id": job_id, "gate": gate, "decision": decision,
            "decided_by": "test", "decided_at": _now(), "note": None,
        }


class FakeCursor:
    def __init__(self, store: Store):
        self.store = store
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    # -- dispatch ------------------------------------------------------------
    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        p = list(params or [])
        self.store.executed.append((s, params))
        self._rows = []
        self.rowcount = 0

        for match, handler in _ROUTES:
            if match(s):
                handler(self, s, p)
                return
        raise AssertionError(f"FakeCursor has no route for: {s}")

    def fetchone(self) -> Optional[tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    # -- table helpers -------------------------------------------------------
    def _emit(self, rows: list[dict[str, Any]], cols: tuple[str, ...]) -> None:
        self._rows = [tuple(r.get(c) for c in cols) for r in rows]


# --- handlers ---------------------------------------------------------------
def _h_select_1(cur: FakeCursor, s: str, p: list) -> None:
    if cur.store.fail_ping:
        raise RuntimeError("database is down")
    cur._rows = [(1,)]


def _h_project_exists(cur: FakeCursor, s: str, p: list) -> None:
    cur._rows = [(1,)] if p[0] in cur.store.projects else []


def _h_insert_project(cur: FakeCursor, s: str, p: list) -> None:
    row = {
        "id": str(uuid4()), "name": p[0], "brief": p[1], "language": p[2],
        "format_w": p[3], "format_h": p[4], "fps": p[5], "created_by": p[6],
        "created_at": _now(), "updated_at": _now(),
    }
    cur.store.projects[row["id"]] = row
    cur._emit([row], PROJECT_COLS)


def _h_select_project(cur: FakeCursor, s: str, p: list) -> None:
    row = cur.store.projects.get(p[0])
    cur._emit([row] if row else [], PROJECT_COLS)


def _h_list_projects(cur: FakeCursor, s: str, p: list) -> None:
    limit, offset = p[-2], p[-1]
    rows = sorted(
        cur.store.projects.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True
    )
    cur._emit(rows[offset : offset + limit], PROJECT_COLS)


def _h_update_project(cur: FakeCursor, s: str, p: list) -> None:
    pid = p[-1]
    row = cur.store.projects.get(pid)
    if row is None:
        cur._rows = []
        return
    fields = re.findall(r"(\w+) = %s", s)
    for name, value in zip(fields, p[:-1]):
        row[name] = value
    row["updated_at"] = _now()
    cur._emit([row], PROJECT_COLS)


def _h_delete_project(cur: FakeCursor, s: str, p: list) -> None:
    pid = p[0]
    if pid in cur.store.projects:
        del cur.store.projects[pid]
        for jid in [j for j, r in cur.store.jobs.items() if r["project_id"] == pid]:
            del cur.store.jobs[jid]   # ON DELETE CASCADE
        cur.rowcount = 1


def _h_insert_job(cur: FakeCursor, s: str, p: list) -> None:
    row = {
        "id": str(uuid4()), "project_id": p[0], "state": "draft",
        "video_spec": None, "idempotency_key": p[1], "attempt": 0, "error": None,
        "started_at": None, "finished_at": None,
        "created_at": _now(), "updated_at": _now(),
    }
    cur.store.jobs[row["id"]] = row
    cur._emit([row], JOB_COLS)


def _h_select_job(cur: FakeCursor, s: str, p: list) -> None:
    row = cur.store.jobs.get(p[0])
    cur._emit([row] if row else [], JOB_COLS)


def _h_select_job_by_key(cur: FakeCursor, s: str, p: list) -> None:
    rows = [r for r in cur.store.jobs.values() if r["idempotency_key"] == p[0]]
    cur._emit(rows[:1], JOB_COLS)


def _h_list_jobs(cur: FakeCursor, s: str, p: list) -> None:
    limit, offset = p[-2], p[-1]
    filters = p[:-2]
    rows = list(cur.store.jobs.values())
    # Read the filters off the WHERE clause only: the SELECT list also
    # mentions project_id and state, so matching on the whole statement
    # would invent a filter that was never applied.
    where = re.search(r"\bWHERE (.+?) ORDER BY", s)
    for column in re.findall(r"(\w+) = %s", where.group(1)) if where else []:
        wanted = filters.pop(0)
        rows = [r for r in rows if r[column] == wanted]
    rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    cur._emit(rows[offset : offset + limit], JOB_COLS)


def _h_job_state(cur: FakeCursor, s: str, p: list) -> None:
    row = cur.store.jobs.get(p[0])
    if "FOR UPDATE" in s and row is not None:
        cur.store.locked.append(p[0])
    cur._rows = [(row["state"],)] if row else []


def _h_update_job(cur: FakeCursor, s: str, p: list) -> None:
    jid = p[-1]
    row = cur.store.jobs.get(jid)
    if row is None:
        return
    values = list(p[:-1])
    for assignment in re.findall(r"(\w+) = ([^,]+)", s.split("SET ", 1)[1].split(" WHERE")[0]):
        name, expr = assignment
        if "%s" in expr:
            row[name] = values.pop(0)
        elif "now()" in expr and "COALESCE" in expr:
            row[name] = row.get(name) or _now()
        elif "now()" in expr:
            row[name] = _now()
        elif expr.strip() == "NULL":
            row[name] = None
    row["updated_at"] = _now()
    cur.rowcount = 1


def _h_select_approval_decision(cur: FakeCursor, s: str, p: list) -> None:
    row = cur.store.approvals.get((p[0], p[1]))
    cur._rows = [(row["decision"],)] if row else []


def _h_list_approvals(cur: FakeCursor, s: str, p: list) -> None:
    rows = sorted(
        (r for k, r in cur.store.approvals.items() if k[0] == p[0]),
        key=lambda r: r["gate"],
    )
    cur._emit(rows, ("gate", "decision", "decided_by", "decided_at", "note"))


def _h_upsert_approval(cur: FakeCursor, s: str, p: list) -> None:
    cur.store.approvals[(p[0], p[1])] = {
        "job_id": p[0], "gate": p[1], "decision": p[2],
        "decided_by": p[3], "decided_at": _now(), "note": p[4],
    }
    cur.rowcount = 1


def _h_insert_audit(cur: FakeCursor, s: str, p: list) -> None:
    job = cur.store.jobs.get(p[0])
    if job is None:
        cur._rows = []
        return
    row = {
        "id": len(cur.store.audit) + 1, "job_id": p[0],
        "project_id": job["project_id"], "event_type": p[1],
        "from_state": p[2], "to_state": p[3], "actor": p[4],
        "detail": json.loads(p[5]) if p[5] else None, "created_at": _now(),
    }
    cur.store.audit.append(row)
    cur._rows = [(row["id"],)]


def _h_select_audit(cur: FakeCursor, s: str, p: list) -> None:
    rows = [r for r in cur.store.audit if r["job_id"] == p[0]][: p[1]]
    cur._emit(
        rows,
        ("id", "event_type", "from_state", "to_state", "actor", "detail", "created_at"),
    )


#: Ordered most-specific first — the first match wins.
_ROUTES = [
    (lambda s: s.startswith("SELECT 1 FROM projects"), _h_project_exists),
    (lambda s: s.startswith("SELECT 1"), _h_select_1),
    (lambda s: s.startswith("INSERT INTO projects"), _h_insert_project),
    (lambda s: s.startswith("INSERT INTO jobs"), _h_insert_job),
    (lambda s: s.startswith("INSERT INTO approvals"), _h_upsert_approval),
    (lambda s: s.startswith("INSERT INTO audit_events"), _h_insert_audit),
    (lambda s: s.startswith("UPDATE projects"), _h_update_project),
    (lambda s: s.startswith("UPDATE jobs"), _h_update_job),
    (lambda s: s.startswith("DELETE FROM projects"), _h_delete_project),
    (lambda s: s.startswith("SELECT state FROM jobs"), _h_job_state),
    (lambda s: s.startswith("SELECT decision FROM approvals"), _h_select_approval_decision),
    (lambda s: "FROM approvals WHERE job_id" in s, _h_list_approvals),
    (lambda s: "FROM audit_events" in s, _h_select_audit),
    (lambda s: "FROM jobs WHERE idempotency_key" in s, _h_select_job_by_key),
    (lambda s: "FROM jobs WHERE id" in s, _h_select_job),
    (lambda s: "FROM jobs" in s and "LIMIT" in s, _h_list_jobs),
    (lambda s: "FROM projects WHERE id" in s, _h_select_project),
    (lambda s: "FROM projects" in s and "LIMIT" in s, _h_list_projects),
]


class FakeConnection:
    def __init__(self, store: Store):
        self.store = store

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.store.commits += 1

    def rollback(self) -> None:
        self.store.rollbacks += 1

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type: Any, *rest: Any) -> bool:
        # Mirrors psycopg: commit on clean exit, roll back on an exception.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class FakeDatabase:
    """Drop-in for packages.api.deps.Database."""

    def __init__(self, store: Optional[Store] = None):
        self.store = store or Store()
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def connection(self) -> FakeConnection:
        return FakeConnection(self.store)

    def ping(self) -> bool:
        return not self.store.fail_ping
