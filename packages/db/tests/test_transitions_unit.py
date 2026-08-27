"""
Unit tests for the transitions layer against a fake connection.

No psycopg, no Postgres. These prove the SQL shape and the decision logic;
test_transitions_integration.py proves it against a real database.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from packages.db import transitions as T
from packages.db.states import Gate, GateNotApproved, IllegalTransition, JobState as S

JOB = str(uuid4())


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.store["executed"].append((s, params))
        if s.startswith("SELECT state FROM jobs"):
            self._result = None if self.store["state"] is None else (self.store["state"],)
        elif "FROM approvals" in s:
            d = self.store["approvals"].get(params[1])
            self._result = (d,) if d else None
        elif "INTO audit_events" in s:
            self.store["audit"].append(params)
            self._result = (len(self.store["audit"]),)
        elif "FROM audit_events" in s:
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, state="draft", approvals=None):
        self.store = {
            "state": state,
            "approvals": approvals or {},
            "executed": [],
            "audit": [],
        }
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1

    # helpers
    def sql(self):
        return [e[0] for e in self.store["executed"]]

    def updates(self):
        return [e for e in self.store["executed"] if e[0].startswith("UPDATE jobs")]


# --- reads ------------------------------------------------------------------
def test_get_state_returns_enum():
    assert T.get_state(FakeConn(state="planning"), JOB) is S.PLANNING


def test_get_state_raises_when_missing():
    with pytest.raises(T.JobNotFound):
        T.get_state(FakeConn(state=None), JOB)


def test_transition_locks_the_row():
    """Two workers must not both read 'draft' and both decide to advance."""
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING)
    assert any("FOR UPDATE" in s for s in c.sql()), "job row was not locked"


def test_gate_approval_only_counts_when_approved():
    assert T.is_gate_approved(FakeConn(approvals={"g1_script": "approved"}), JOB, Gate.G1_SCRIPT)
    assert not T.is_gate_approved(FakeConn(approvals={"g1_script": "rejected"}), JOB, Gate.G1_SCRIPT)
    assert not T.is_gate_approved(FakeConn(approvals={"g1_script": "pending"}), JOB, Gate.G1_SCRIPT)
    assert not T.is_gate_approved(FakeConn(), JOB, Gate.G1_SCRIPT)


# --- transition -------------------------------------------------------------
def test_legal_transition_updates_and_audits():
    c = FakeConn(state="draft")
    r = T.transition(c, JOB, S.PLANNING, actor="worker-1")
    assert r.changed and r.frm is S.DRAFT and r.to is S.PLANNING
    assert len(c.updates()) == 1
    assert len(c.store["audit"]) == 1
    assert c.store["audit"][0][1] == T.EVENT_TRANSITION


def test_transition_does_not_commit():
    """Transaction boundaries belong to the caller (storage/db.py convention)."""
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING)
    assert c.commits == 0


def test_idempotent_redelivery_writes_nothing():
    c = FakeConn(state="planning")
    r = T.transition(c, JOB, S.PLANNING)
    assert r.changed is False
    assert c.updates() == []
    assert c.store["audit"] == []


def test_illegal_transition_writes_nothing():
    c = FakeConn(state="draft")
    with pytest.raises(IllegalTransition):
        T.transition(c, JOB, S.COMPLETE)
    assert c.updates() == []
    assert c.store["audit"] == []


def test_gated_transition_without_approval_writes_nothing():
    c = FakeConn(state="planned")
    with pytest.raises(GateNotApproved):
        T.transition(c, JOB, S.RETRIEVING)
    assert c.updates() == []
    assert c.store["audit"] == []


def test_gated_transition_passes_once_approved():
    c = FakeConn(state="planned", approvals={"g1_script": "approved"})
    assert T.transition(c, JOB, S.RETRIEVING).changed is True


def test_start_stamps_started_at_once():
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING)
    assert "COALESCE(started_at, now())" in c.updates()[0][0]


def test_terminal_transition_stamps_finished_at():
    c = FakeConn(state="rendering")
    T.transition(c, JOB, S.FAILED, error="render blew up")
    sql, params = c.updates()[0]
    assert "finished_at = now()" in sql
    assert "render blew up" in params


def test_successful_move_clears_a_stale_error():
    c = FakeConn(state="failed")
    T.transition(c, JOB, S.RENDERING)
    assert "error = NULL" in c.updates()[0][0]


def test_failing_does_not_null_its_own_error():
    c = FakeConn(state="qa")
    T.transition(c, JOB, S.FAILED, error="qa rejected")
    assert "error = NULL" not in c.updates()[0][0]


def test_audit_detail_is_json_encoded():
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING, detail={"reason": "manual start"})
    detail = c.store["audit"][0][5]
    assert json.loads(detail) == {"reason": "manual start"}


def test_audit_records_both_ends_of_the_move():
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING, actor="asad")
    _, _, frm, to, actor, _, _ = c.store["audit"][0]  # (job, type, from, to, actor, detail, job)
    assert (frm, to, actor) == ("draft", "planning", "asad")


def test_parameterised_sql_only():
    """No caller value may be interpolated into SQL text."""
    c = FakeConn(state="draft")
    T.transition(c, JOB, S.PLANNING, actor="'; DROP TABLE jobs; --")
    for sql, _ in c.store["executed"]:
        assert "DROP TABLE" not in sql


# --- gate decisions ---------------------------------------------------------
def test_decide_gate_upserts_and_audits():
    c = FakeConn(state="planned")
    T.decide_gate(c, JOB, Gate.G1_SCRIPT, approved=True, actor="asad")
    sql = " ".join(c.sql())
    assert "INTO approvals" in sql and "ON CONFLICT (job_id, gate) DO UPDATE" in sql
    assert c.store["audit"][0][1] == T.EVENT_GATE


def test_decide_gate_does_not_move_the_job():
    """Approving is a decision, not a transition — the worker advances the job."""
    c = FakeConn(state="planned")
    T.decide_gate(c, JOB, Gate.G1_SCRIPT, approved=True, actor="asad")
    assert c.updates() == []


def test_rejection_requires_a_note():
    c = FakeConn(state="planned")
    with pytest.raises(ValueError, match="requires a note"):
        T.decide_gate(c, JOB, Gate.G1_SCRIPT, approved=False, actor="asad")
    with pytest.raises(ValueError):
        T.decide_gate(c, JOB, Gate.G1_SCRIPT, approved=False, actor="asad", note="   ")
    T.decide_gate(c, JOB, Gate.G1_SCRIPT, approved=False, actor="asad", note="facts wrong")


def test_awaiting_gate_maps_parked_states():
    assert T.awaiting_gate(S.PLANNED) is Gate.G1_SCRIPT
    assert T.awaiting_gate(S.RETRIEVED) is Gate.G2_STORYBOARD
    assert T.awaiting_gate(S.REVIEW) is Gate.G3_FINAL
    assert T.awaiting_gate(S.RENDERING) is None
