"""
Durable job transitions: apply a state change, write the audit trail.

The rules live in states.py (pure, no I/O). This module is the only place that
turns a rule into a row, and it enforces PRD FR-11: every transition is
idempotent and audit-logged.

Convention, matching packages/storage/db.py:
  * The caller supplies the psycopg connection and owns commit/rollback.
  * psycopg is never imported at runtime, so unit tests need no driver.
  * Parameterised queries only.

Concurrency: transition() locks the job row (SELECT ... FOR UPDATE) before
reading its state, so two workers racing the same job cannot both decide the
move is legal. The caller's transaction boundary is what holds that lock, which
is exactly why this module refuses to commit on your behalf.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from .states import (
    AWAITING_GATE,
    Gate,
    JobState,
    check,
    gate_for,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

log = logging.getLogger("db.transitions")

EVENT_TRANSITION = "job.transition"
EVENT_GATE = "approval.decision"


class JobNotFound(LookupError):
    def __init__(self, job_id: UUID | str):
        super().__init__(f"job not found: {job_id}")


@dataclass(frozen=True)
class TransitionResult:
    """What actually happened, so the caller can log or return it."""

    job_id: str
    frm: JobState
    to: JobState
    changed: bool          # False = idempotent no-op (already in `to`)
    audit_event_id: Optional[int] = None


# --- reads ------------------------------------------------------------------
def get_state(conn: "Connection", job_id: UUID | str, *, for_update: bool = False) -> JobState:
    """Current state of a job. `for_update` locks the row for the transaction."""
    sql = "SELECT state FROM jobs WHERE id = %s"
    if for_update:
        sql += " FOR UPDATE"
    with conn.cursor() as cur:
        cur.execute(sql, (str(job_id),))
        row = cur.fetchone()
    if row is None:
        raise JobNotFound(job_id)
    return JobState(row[0])


def is_gate_approved(conn: "Connection", job_id: UUID | str, gate: Gate) -> bool:
    """True only if an approvals row for this gate is explicitly 'approved'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT decision FROM approvals WHERE job_id = %s AND gate = %s",
            (str(job_id), gate.value),
        )
        row = cur.fetchone()
    return bool(row) and row[0] == "approved"


# --- writes -----------------------------------------------------------------
def record_event(
    conn: "Connection",
    *,
    job_id: UUID | str,
    event_type: str,
    frm: Optional[JobState] = None,
    to: Optional[JobState] = None,
    actor: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> int:
    """Append one audit_events row. Append-only: never updated, never deleted."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_events "
            "(job_id, project_id, event_type, from_state, to_state, actor, detail) "
            "SELECT %s, j.project_id, %s, %s, %s, %s, %s FROM jobs j WHERE j.id = %s "
            "RETURNING id",
            (
                str(job_id),
                event_type,
                frm.value if frm else None,
                to.value if to else None,
                actor,
                json.dumps(detail) if detail is not None else None,
                str(job_id),
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise JobNotFound(job_id)
    return row[0]


def transition(
    conn: "Connection",
    job_id: UUID | str,
    to: JobState,
    *,
    actor: Optional[str] = None,
    error: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> TransitionResult:
    """
    Move a job to `to`, if the state machine allows it.

    Idempotent: if the job is already in `to`, nothing is written and
    `changed` is False. Callers can safely retry a delivered message.

    Raises IllegalTransition / GateNotApproved / JobNotFound. On success the
    change and its audit row are in the caller's open transaction — commit is
    the caller's job.
    """
    frm = get_state(conn, job_id, for_update=True)

    gate = gate_for(frm, to)
    approved = is_gate_approved(conn, job_id, gate) if gate else False

    if not check(frm, to, gate_approved=approved):
        log.debug("job %s already in %s, no-op", job_id, to.value)
        return TransitionResult(str(job_id), frm, to, changed=False)

    sets = ["state = %s"]
    params: list[Any] = [to.value]
    if to is JobState.PLANNING and frm is JobState.DRAFT:
        sets.append("started_at = COALESCE(started_at, now())")
    if to in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED):
        sets.append("finished_at = now()")
    if error is not None:
        sets.append("error = %s")
        params.append(error)
    elif to is not JobState.FAILED:
        sets.append("error = NULL")   # clear a stale error on a successful move
    params.append(str(job_id))

    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET " + ", ".join(sets) + " WHERE id = %s", params)

    event_id = record_event(
        conn,
        job_id=job_id,
        event_type=EVENT_TRANSITION,
        frm=frm,
        to=to,
        actor=actor,
        detail=detail,
    )
    log.info("job %s %s -> %s", job_id, frm.value, to.value)
    return TransitionResult(str(job_id), frm, to, changed=True, audit_event_id=event_id)


def decide_gate(
    conn: "Connection",
    job_id: UUID | str,
    gate: Gate,
    *,
    approved: bool,
    actor: str,
    note: Optional[str] = None,
) -> int:
    """
    Record a human decision at a gate (PRD FR-3).

    Upserts the approvals row and appends an audit event. This does NOT advance
    the job — the caller (or the worker that picks the job up) calls
    transition() next, which will now see the approval.

    A reject requires a note; PRD 10 makes that a UI rule, enforced here too so
    the API cannot bypass it.
    """
    if not approved and not (note or "").strip():
        raise ValueError("a rejection requires a note")

    decision = "approved" if approved else "rejected"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO approvals (job_id, gate, decision, decided_by, decided_at, note) "
            "VALUES (%s, %s, %s, %s, now(), %s) "
            "ON CONFLICT (job_id, gate) DO UPDATE SET "
            "decision = EXCLUDED.decision, decided_by = EXCLUDED.decided_by, "
            "decided_at = EXCLUDED.decided_at, note = EXCLUDED.note",
            (str(job_id), gate.value, decision, actor, note),
        )
    return record_event(
        conn,
        job_id=job_id,
        event_type=EVENT_GATE,
        actor=actor,
        detail={"gate": gate.value, "decision": decision, "note": note},
    )


def awaiting_gate(state: JobState) -> Optional[Gate]:
    """Which gate a parked job is waiting on, if any."""
    return AWAITING_GATE.get(state)


def history(conn: "Connection", job_id: UUID | str, limit: int = 100) -> list[dict[str, Any]]:
    """Audit trail for a job, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, event_type, from_state, to_state, actor, detail, created_at "
            "FROM audit_events WHERE job_id = %s ORDER BY id LIMIT %s",
            (str(job_id), limit),
        )
        rows = cur.fetchall()
    cols = ("id", "event_type", "from_state", "to_state", "actor", "detail", "created_at")
    return [dict(zip(cols, r)) for r in rows]
