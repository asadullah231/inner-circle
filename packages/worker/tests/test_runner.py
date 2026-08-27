"""
Worker loop tests.

Reuses the in-memory database from packages/api/tests/fakedb.py, so the real
transitions SQL runs against a dict store: the worker's behaviour is tested,
not a mock of it. test_recovery_integration.py repeats the crash scenarios
against a real Postgres.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from packages.api.tests.fakedb import FakeDatabase, Store
from packages.db.states import Gate, JobState as S
from packages.worker.queue import MemoryQueue
from packages.worker.runner import Worker, resume_after_gate


@pytest.fixture()
def store() -> Store:
    return Store()


@pytest.fixture()
def db(store) -> FakeDatabase:
    return FakeDatabase(store)


@pytest.fixture()
def queue() -> MemoryQueue:
    return MemoryQueue()


@pytest.fixture()
def worker(queue, db) -> Worker:
    return Worker(queue, db.connection, poll_timeout=0.05)


def _job(store: Store, state: str = "draft") -> str:
    project = store.add_project()
    return store.add_job(project["id"], state=state)["id"]


# --- one step ---------------------------------------------------------------
def test_a_draft_job_moves_to_planning(worker, store):
    job_id = _job(store)
    outcome = worker.process(job_id)
    assert outcome.action == "advanced"
    assert (outcome.frm, outcome.to) == (S.DRAFT, S.PLANNING)
    assert store.jobs[job_id]["state"] == "planning"


def test_advancing_a_job_writes_an_audit_row(worker, store):
    job_id = _job(store)
    worker.process(job_id)
    assert store.audit[-1]["event_type"] == "job.transition"
    assert store.audit[-1]["actor"] == "worker-1"


def test_a_step_locks_the_job_row(worker, store):
    """Two workers must not advance the same job.

    The lock is taken twice in the one transaction: once by the worker to
    decide what the next step is, once inside transition() which re-reads
    under the same lock. Re-locking a row you already hold is free in
    Postgres, and having transition() take it unconditionally is what keeps
    it safe for callers that are not this worker.
    """
    job_id = _job(store)
    worker.process(job_id)
    assert set(store.locked) == {job_id}


def test_an_advanced_job_is_pushed_back_for_its_next_step(worker, queue, store):
    job_id = _job(store)
    worker.process(job_id)
    assert queue.depth() == (1, 0)


def test_a_processed_job_is_always_acked(worker, queue, store):
    job_id = _job(store)
    queue.push(job_id)
    queue.reserve(timeout=0.1)
    worker.process(job_id)
    assert queue.depth()[1] == 0


def test_a_missing_job_is_dropped_not_retried(worker, queue):
    outcome = worker.process(str(uuid4()))
    assert outcome.action == "gone"
    assert queue.depth() == (0, 0)


def test_a_terminal_job_is_left_alone(worker, store):
    job_id = _job(store, "complete")
    outcome = worker.process(job_id)
    assert outcome.action == "terminal"
    assert store.audit == []


# --- gates ------------------------------------------------------------------
def test_the_worker_parks_at_g1_instead_of_spinning(worker, queue, store):
    job_id = _job(store, "planned")
    outcome = worker.process(job_id)
    assert outcome.action == "parked"
    assert "g1_script" in outcome.detail
    assert queue.depth() == (0, 0)   # nothing re-queued: no busy-wait
    assert store.jobs[job_id]["state"] == "planned"


def test_the_worker_parks_at_g2(worker, store):
    assert worker.process(_job(store, "retrieved")).detail.endswith("g2_storyboard")


def test_the_worker_parks_at_g3(worker, store):
    assert worker.process(_job(store, "review")).detail.endswith("g3_final")


def test_a_rejected_gate_still_parks_the_job(worker, store):
    job_id = _job(store, "planned")
    store.approve(job_id, "g1_script", decision="rejected")
    assert worker.process(job_id).action == "parked"
    assert store.jobs[job_id]["state"] == "planned"


def test_an_approved_gate_lets_the_job_through(worker, store):
    job_id = _job(store, "planned")
    store.approve(job_id, "g1_script")
    outcome = worker.process(job_id)
    assert outcome.action == "advanced"
    assert store.jobs[job_id]["state"] == "retrieving"


def test_resume_after_gate_puts_the_job_back_on_the_queue(queue):
    resume_after_gate(queue, "job-1")
    assert queue.reserve(timeout=0.1) == "job-1"


# --- the full pipeline ------------------------------------------------------
def _approve_all(store: Store, job_id: str) -> None:
    for gate in Gate:
        store.approve(job_id, gate.value)


def test_a_job_walks_the_whole_pipeline_to_complete(worker, queue, store):
    job_id = _job(store)
    _approve_all(store, job_id)
    queue.push(job_id)

    stats = worker.run(max_jobs=20)

    assert store.jobs[job_id]["state"] == "complete"
    assert stats.finished >= 1
    assert store.jobs[job_id]["finished_at"] is not None


def test_the_full_pipeline_is_nine_transitions(worker, queue, store):
    """draft->planning->planned->retrieving->retrieved->rendering->rendered
    ->qa->review->complete. Nine moves, one audit row each."""
    job_id = _job(store)
    _approve_all(store, job_id)
    queue.push(job_id)
    worker.run(max_jobs=20)

    moves = [
        (e["from_state"], e["to_state"])
        for e in store.audit
        if e["event_type"] == "job.transition"
    ]
    assert moves == [
        ("draft", "planning"), ("planning", "planned"),
        ("planned", "retrieving"), ("retrieving", "retrieved"),
        ("retrieved", "rendering"), ("rendering", "rendered"),
        ("rendered", "qa"), ("qa", "review"), ("review", "complete"),
    ]


def test_a_pipeline_with_no_approvals_stops_at_the_first_gate(worker, queue, store):
    job_id = _job(store)
    queue.push(job_id)
    worker.run(max_jobs=20)
    assert store.jobs[job_id]["state"] == "planned"
    assert worker.stats.parked == 1


def test_approval_arriving_later_resumes_the_job(worker, queue, store):
    job_id = _job(store)
    queue.push(job_id)
    worker.run(max_jobs=10)
    assert store.jobs[job_id]["state"] == "planned"

    store.approve(job_id, "g1_script")
    resume_after_gate(queue, job_id)
    worker.run(max_jobs=10)
    assert store.jobs[job_id]["state"] == "retrieved"   # parks again at G2


# --- failure handling -------------------------------------------------------
class ExplodingDatabase(FakeDatabase):
    """Raises on the first connection, then behaves. Simulates a provider or
    database blip mid-step."""

    def __init__(self, store, fail_times: int = 1):
        super().__init__(store)
        self.remaining = fail_times

    def connection(self):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("connection reset by peer")
        return super().connection()


def test_a_raising_step_does_not_kill_the_worker(store, queue):
    job_id = _job(store)
    worker = Worker(queue, ExplodingDatabase(store, fail_times=1).connection)
    outcome = worker.process(job_id)
    assert outcome.action == "failed"
    assert worker.stats.failed == 1


def test_a_raising_step_marks_the_job_failed_with_the_reason(store, queue):
    job_id = _job(store, "planning")
    worker = Worker(queue, ExplodingDatabase(store, fail_times=1).connection)
    worker.process(job_id)
    assert store.jobs[job_id]["state"] == "failed"
    assert "connection reset by peer" in store.jobs[job_id]["error"]


def test_a_failure_the_state_machine_refuses_is_still_audited(store, queue):
    """`draft` cannot move to `failed`. The failure must not vanish."""
    job_id = _job(store, "draft")
    worker = Worker(queue, ExplodingDatabase(store, fail_times=1).connection)
    worker.process(job_id)

    assert store.jobs[job_id]["state"] == "draft"   # state machine held
    event = store.audit[-1]
    assert event["event_type"] == "worker.error"
    assert "connection reset by peer" in event["detail"]["error"]


def test_a_failed_job_is_not_re_queued_into_the_same_failure(store, queue):
    job_id = _job(store, "planning")
    worker = Worker(queue, ExplodingDatabase(store, fail_times=1).connection)
    worker.process(job_id)
    assert queue.depth() == (0, 0)


def test_a_job_that_fails_twice_is_not_marked_failed_twice(store, queue):
    """The second failure is the mark-failed write itself; the job must not
    end up with a bogus state because the recovery path also broke."""
    job_id = _job(store)
    worker = Worker(queue, ExplodingDatabase(store, fail_times=2).connection)
    worker.process(job_id)
    assert store.jobs[job_id]["state"] == "draft"   # unchanged, nothing invented


def test_an_operator_can_retry_a_failed_job(worker, queue, store):
    job_id = _job(store, "failed")
    store.jobs[job_id]["error"] = "provider timed out"
    _approve_all(store, job_id)

    from packages.db import transitions

    with FakeDatabase(store).connection() as conn:
        transitions.transition(conn, job_id, S.PLANNING, actor="operator")

    queue.push(job_id)
    worker.run(max_jobs=20)
    assert store.jobs[job_id]["state"] == "complete"


# --- restart recovery (M1.5) ------------------------------------------------
def test_a_job_the_worker_died_holding_is_recovered(store, queue):
    """The crash: reserved, never acked. A restart must pick it up again."""
    job_id = _job(store)
    queue.push(job_id)
    queue.reserve(timeout=0.1)          # worker took it...
    assert queue.depth() == (0, 1)      # ...and died here

    fresh = Worker(queue, FakeDatabase(store).connection, name="worker-2",
                   poll_timeout=0.05)
    fresh.run(max_jobs=1)

    assert fresh.stats.recovered == 1
    assert store.jobs[job_id]["state"] == "planning"


def test_state_survives_the_restart_unchanged(store, queue):
    """A restart must not rewind a job or replay a step."""
    job_id = _job(store)
    first = Worker(queue, FakeDatabase(store).connection, poll_timeout=0.05)
    queue.push(job_id)
    first.run(max_jobs=2)
    state_before = store.jobs[job_id]["state"]
    audit_before = len(store.audit)

    second = Worker(queue, FakeDatabase(store).connection, name="worker-2",
                    poll_timeout=0.05)
    second.recover()

    assert store.jobs[job_id]["state"] == state_before
    assert len(store.audit) == audit_before


def test_a_redelivered_job_is_a_no_op_not_a_double_step(store, queue):
    """Recovery can hand the same job to the worker twice. The second pass
    must not advance it a second time."""
    job_id = _job(store, "planned")     # parked, so the step is deterministic
    worker = Worker(queue, FakeDatabase(store).connection, poll_timeout=0.05)
    worker.process(job_id)
    worker.process(job_id)
    assert store.jobs[job_id]["state"] == "planned"
    assert store.audit == []


def test_recovery_is_reported_in_stats(store, queue):
    for _ in range(3):
        job_id = _job(store)
        queue.push(job_id)
        queue.reserve(timeout=0.1)

    worker = Worker(queue, FakeDatabase(store).connection, poll_timeout=0.05)
    assert len(worker.recover()) == 3
    assert worker.stats.recovered == 3


# --- shutdown ---------------------------------------------------------------
def test_stop_ends_the_loop(worker, queue, store):
    queue.push(_job(store))
    worker.stop()
    stats = worker.run(max_jobs=10)
    assert stats.reserved == 0


def test_a_worker_with_nothing_to_do_exits_on_the_poll_timeout(worker):
    assert worker.run(max_jobs=1).reserved == 0


def test_stop_from_another_thread_is_honoured(queue, db):
    worker = Worker(queue, db.connection, poll_timeout=0.05)
    thread = threading.Thread(target=worker.run)
    thread.start()
    worker.stop()
    thread.join(timeout=3)
    assert not thread.is_alive()
