"""
Integration tests for the state machine against a REAL Postgres.

Skipped unless RUN_DB_INTEGRATION=1 AND psycopg is installed AND a reachable
Postgres is configured via DATABASE_URL. CI runs these in `core-db-integration`
against postgres:16.

The concurrency test is the reason this suite exists: SELECT ... FOR UPDATE
cannot be proven against a fake connection.
"""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest

from packages.db import migrate
from packages.db import transitions as T
from packages.db.states import Gate, GateNotApproved, IllegalTransition, JobState as S

pytestmark = [
    pytest.mark.db_integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION") != "1",
        reason="set RUN_DB_INTEGRATION=1 and provide a Postgres to enable",
    ),
]

psycopg = pytest.importorskip("psycopg")

DSN = os.getenv("DATABASE_URL", "")


def _connect():
    return psycopg.connect(DSN) if DSN else psycopg.connect()


@pytest.fixture()
def conn():
    c = _connect()
    try:
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.commit()
        migrate.run(c)
        yield c
    finally:
        c.close()


@pytest.fixture()
def job(conn):
    """A fresh job in `draft`, committed so other connections can see it."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO projects (name) VALUES ('demo') RETURNING id")
        project_id = cur.fetchone()[0]
        cur.execute("INSERT INTO jobs (project_id) VALUES (%s) RETURNING id", (project_id,))
        job_id = cur.fetchone()[0]
    conn.commit()
    return job_id


def _state(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM jobs WHERE id = %s", (str(job_id),))
        return cur.fetchone()[0]


# --- basic durability -------------------------------------------------------
def test_transition_persists(conn, job):
    T.transition(conn, job, S.PLANNING, actor="worker-1")
    conn.commit()
    assert _state(conn, job) == "planning"


def test_transition_writes_audit_row(conn, job):
    T.transition(conn, job, S.PLANNING, actor="worker-1", detail={"why": "start"})
    conn.commit()
    events = T.history(conn, job)
    assert len(events) == 1
    e = events[0]
    assert (e["event_type"], e["from_state"], e["to_state"], e["actor"]) == (
        T.EVENT_TRANSITION, "draft", "planning", "worker-1",
    )
    assert e["detail"] == {"why": "start"}


def test_rollback_undoes_state_and_audit(conn, job):
    """The caller owns the transaction, so a rollback must undo both writes."""
    T.transition(conn, job, S.PLANNING)
    conn.rollback()
    assert _state(conn, job) == "draft"
    assert T.history(conn, job) == []


def test_started_and_finished_timestamps(conn, job):
    T.transition(conn, job, S.PLANNING)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT started_at, finished_at FROM jobs WHERE id = %s", (str(job),))
        started, finished = cur.fetchone()
    assert started is not None and finished is None

    T.transition(conn, job, S.FAILED, error="planner died")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT started_at, finished_at, error FROM jobs WHERE id = %s", (str(job),))
        started2, finished2, err = cur.fetchone()
    assert started2 == started, "started_at must not be overwritten"
    assert finished2 is not None
    assert err == "planner died"


# --- rules hold against the real database -----------------------------------
def test_illegal_transition_leaves_state_untouched(conn, job):
    with pytest.raises(IllegalTransition):
        T.transition(conn, job, S.COMPLETE)
    conn.rollback()
    assert _state(conn, job) == "draft"


def test_gate_blocks_until_approved(conn, job):
    T.transition(conn, job, S.PLANNING)
    T.transition(conn, job, S.PLANNED)
    conn.commit()

    with pytest.raises(GateNotApproved):
        T.transition(conn, job, S.RETRIEVING)
    conn.rollback()
    assert _state(conn, job) == "planned"

    T.decide_gate(conn, job, Gate.G1_SCRIPT, approved=True, actor="asad")
    conn.commit()
    T.transition(conn, job, S.RETRIEVING)
    conn.commit()
    assert _state(conn, job) == "retrieving"


def test_rejected_gate_does_not_open_the_door(conn, job):
    T.transition(conn, job, S.PLANNING)
    T.transition(conn, job, S.PLANNED)
    T.decide_gate(conn, job, Gate.G1_SCRIPT, approved=False, actor="asad", note="facts wrong")
    conn.commit()
    with pytest.raises(GateNotApproved):
        T.transition(conn, job, S.RETRIEVING)
    conn.rollback()


def test_gate_decision_can_be_reversed(conn, job):
    """One row per gate (unique constraint), so a re-decision upserts."""
    T.transition(conn, job, S.PLANNING)
    T.transition(conn, job, S.PLANNED)
    T.decide_gate(conn, job, Gate.G1_SCRIPT, approved=False, actor="asad", note="fix line 3")
    T.decide_gate(conn, job, Gate.G1_SCRIPT, approved=True, actor="asad")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(decision) FROM approvals WHERE job_id = %s", (str(job),))
        count, decision = cur.fetchone()
    assert (count, decision) == (1, "approved")
    T.transition(conn, job, S.RETRIEVING)
    conn.commit()


def test_full_happy_path_end_to_end(conn, job):
    """PRD 7.2 walked against a real database, all three gates."""
    T.transition(conn, job, S.PLANNING, actor="worker")
    T.transition(conn, job, S.PLANNED, actor="worker")
    T.decide_gate(conn, job, Gate.G1_SCRIPT, approved=True, actor="asad")
    T.transition(conn, job, S.RETRIEVING, actor="worker")
    T.transition(conn, job, S.RETRIEVED, actor="worker")
    T.decide_gate(conn, job, Gate.G2_STORYBOARD, approved=True, actor="asad")
    T.transition(conn, job, S.RENDERING, actor="worker")
    T.transition(conn, job, S.RENDERED, actor="worker")
    T.transition(conn, job, S.QA, actor="worker")
    T.transition(conn, job, S.REVIEW, actor="worker")
    T.decide_gate(conn, job, Gate.G3_FINAL, approved=True, actor="asad")
    T.transition(conn, job, S.COMPLETE, actor="asad")
    conn.commit()

    assert _state(conn, job) == "complete"
    events = T.history(conn, job)
    transitions = [e for e in events if e["event_type"] == T.EVENT_TRANSITION]
    gates = [e for e in events if e["event_type"] == T.EVENT_GATE]
    # draft -> planning -> planned -> retrieving -> retrieved -> rendering
    # -> rendered -> qa -> review -> complete is 9 moves.
    assert len(transitions) == 9
    assert len(gates) == 3
    assert [e["id"] for e in events] == sorted(e["id"] for e in events), "audit must be ordered"


def test_complete_is_final(conn, job):
    T.transition(conn, job, S.CANCELLED, actor="asad")
    conn.commit()
    with pytest.raises(IllegalTransition, match="terminal"):
        T.transition(conn, job, S.PLANNING)
    conn.rollback()


def test_failed_job_can_be_retried(conn, job):
    T.transition(conn, job, S.PLANNING)
    T.transition(conn, job, S.FAILED, error="provider timeout")
    conn.commit()
    T.transition(conn, job, S.PLANNING, actor="operator")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT state, error FROM jobs WHERE id = %s", (str(job),))
        state, err = cur.fetchone()
    assert state == "planning"
    assert err is None, "retry should clear the stale error"


def test_missing_job_raises(conn):
    with pytest.raises(T.JobNotFound):
        T.transition(conn, uuid4(), S.PLANNING)


# --- the reason this suite exists -------------------------------------------
def test_idempotent_redelivery_writes_one_audit_row(conn, job):
    """A worker redelivering the same message must not double-log."""
    T.transition(conn, job, S.PLANNING, actor="worker-1")
    conn.commit()
    r = T.transition(conn, job, S.PLANNING, actor="worker-1")
    conn.commit()
    assert r.changed is False
    assert len(T.history(conn, job)) == 1


def test_concurrent_workers_cannot_both_advance(conn, job):
    """
    Two connections race the same job. FOR UPDATE must serialise them, so
    exactly one writes the transition and the other sees the new state and
    no-ops. Without the row lock, both would write an audit row.
    """
    results: list[object] = []
    barrier = threading.Barrier(2)

    def worker():
        c = _connect()
        try:
            barrier.wait(timeout=10)
            r = T.transition(c, job, S.PLANNING, actor="racer")
            c.commit()
            results.append(r.changed)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            results.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"a racing worker errored: {errors}"
    assert sorted(results, key=str) == [False, True], f"expected one winner, got {results}"
    assert len(T.history(conn, job)) == 1, "the transition was audited twice"
    assert _state(conn, job) == "planning"
