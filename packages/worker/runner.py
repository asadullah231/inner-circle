"""
Worker skeleton (M1.4).

It picks a job off the queue and advances it one step through the state
machine. It does no real work yet — no planning, no retrieval, no rendering.
Those land in M2, M3 and M4, each replacing one entry in `_STEPS` with a real
call. Proving the loop first is the point: the pipeline has to be restartable
and idempotent before there is anything expensive to lose.

Two rules the loop is built around:

  * **A parked job is not a failure.** When a job reaches a gate the worker
    acks it and walks away. Nothing polls, nothing spins. The API re-queues it
    when a human approves (`resume_after_gate`).

  * **The transaction owns the lock.** Every step runs inside one transaction
    that holds the job's FOR UPDATE lock, so two workers cannot advance the
    same job. Commit happens only if the step succeeded.
"""

from __future__ import annotations

import logging
import signal
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from packages.db import transitions
from packages.db.states import (
    AWAITING_GATE,
    GateNotApproved,
    IllegalTransition,
    JobState,
    is_terminal,
)
from packages.db.transitions import JobNotFound

from .queue import Queue

log = logging.getLogger("worker")

#: Audit event for a failure the state machine would not let us record as
#: a FAILED state (see Worker._mark_failed).
EVENT_WORKER_ERROR = "worker.error"

S = JobState

#: The next state for each state the worker drives itself.
#: States absent from this map are ones the worker never moves out of:
#: the three gate states (a human does), and the terminal ones.
_STEPS: dict[JobState, JobState] = {
    S.DRAFT: S.PLANNING,
    S.PLANNING: S.PLANNED,       # M2 replaces this with the planner
    S.RETRIEVING: S.RETRIEVED,   # M3 replaces this with media retrieval
    S.RENDERING: S.RENDERED,     # M4 replaces this with the renderer
    S.RENDERED: S.QA,
    S.QA: S.REVIEW,
}

#: Moving out of a gate state, once its approval exists.
_AFTER_GATE: dict[JobState, JobState] = {
    S.PLANNED: S.RETRIEVING,
    S.RETRIEVED: S.RENDERING,
    S.REVIEW: S.COMPLETE,
}


@dataclass
class Outcome:
    """What one pass over a job did. Returned so the caller can assert on it."""

    job_id: str
    frm: Optional[JobState] = None
    to: Optional[JobState] = None
    #: advanced | parked | terminal | gone | failed | noop
    action: str = "noop"
    detail: str = ""


@dataclass
class Stats:
    reserved: int = 0
    advanced: int = 0
    parked: int = 0
    finished: int = 0
    failed: int = 0
    recovered: int = 0
    outcomes: list[Outcome] = field(default_factory=list)


class Worker:
    """
    One worker. `connect` is a callable returning a context-managed connection
    (`Database.connection`), so the worker owns no pool of its own and the
    tests can hand it a fake.
    """

    def __init__(
        self,
        queue: Queue,
        connect: Callable[[], Any],
        *,
        name: str = "worker-1",
        poll_timeout: float = 5.0,
        steps: Optional[dict[JobState, JobState]] = None,
    ):
        self.queue = queue
        self.connect = connect
        self.name = name
        self.poll_timeout = poll_timeout
        self.steps = dict(steps or _STEPS)
        self.stats = Stats()
        self._stop = False

    # -- lifecycle -----------------------------------------------------------
    def stop(self, *_: Any) -> None:
        """Finish the job in hand, then exit. Wired to SIGTERM/SIGINT in main()."""
        log.info("%s: stop requested, finishing current job", self.name)
        self._stop = True

    def recover(self) -> list[str]:
        """Return jobs a previous worker died holding to the pending list."""
        recovered = self.queue.recover()
        self.stats.recovered += len(recovered)
        return recovered

    def run(self, *, max_jobs: Optional[int] = None, drain: bool = True) -> Stats:
        """
        Reserve and process jobs until there is nothing left to do.

        `drain=True` (the default) returns as soon as the queue is empty,
        which is what a test wants and what a one-shot `--drain` operator run
        wants. A long-running worker passes `drain=False` and blocks on the
        poll instead. `max_jobs` caps the work either way.
        """
        self.recover()
        log.info("%s: started", self.name)
        while not self._stop:
            if max_jobs is not None and self.stats.reserved >= max_jobs:
                break
            job_id = self.queue.reserve(timeout=self.poll_timeout)
            if job_id is None:
                if drain:
                    break
                continue
            self.stats.reserved += 1
            self.process(job_id)
        log.info(
            "%s: stopped (advanced=%d parked=%d finished=%d failed=%d)",
            self.name, self.stats.advanced, self.stats.parked,
            self.stats.finished, self.stats.failed,
        )
        return self.stats

    # -- one job -------------------------------------------------------------
    def process(self, job_id: str) -> Outcome:
        """
        Advance one job by one step, then ack it.

        The ack happens whatever the outcome, because the queue entry is a
        prompt to look at the job, not a claim about what state it is in —
        Postgres owns that. If the step raised before committing, the job is
        unchanged and re-queueing it would just repeat the same failure.
        """
        try:
            outcome = self._step(job_id)
        except Exception as exc:  # noqa: BLE001 - a worker must not die on one job
            log.exception("%s: job %s raised", self.name, job_id)
            outcome = Outcome(job_id, action="failed", detail=str(exc))
            self.stats.failed += 1
            self._mark_failed(job_id, exc)

        self.queue.ack(job_id)
        self.stats.outcomes.append(outcome)

        # A job that moved and is not parked has more work waiting.
        if outcome.action == "advanced":
            self.queue.push(job_id)
        return outcome

    def _step(self, job_id: str) -> Outcome:
        with self.connect() as conn:
            try:
                state = transitions.get_state(conn, job_id, for_update=True)
            except JobNotFound:
                log.warning("%s: job %s is gone, dropping", self.name, job_id)
                return Outcome(job_id, action="gone")

            if is_terminal(state):
                self.stats.finished += 1
                return Outcome(job_id, frm=state, to=state, action="terminal")

            target = self.steps.get(state)
            if target is None:
                target = self._gate_target(conn, job_id, state)
                if target is None:
                    self.stats.parked += 1
                    gate = AWAITING_GATE.get(state)
                    return Outcome(
                        job_id, frm=state, action="parked",
                        detail=f"waiting on {gate.value}" if gate else "no step defined",
                    )

            result = transitions.transition(conn, job_id, target, actor=self.name)
            if not result.changed:
                return Outcome(job_id, frm=state, to=target, action="noop")

        # Committed by the connection context manager.
        self.stats.advanced += 1
        if is_terminal(target):
            self.stats.finished += 1
            return Outcome(job_id, frm=state, to=target, action="terminal")
        return Outcome(job_id, frm=state, to=target, action="advanced")

    def _gate_target(self, conn: Any, job_id: str, state: JobState) -> Optional[JobState]:
        """Where a parked job goes once its gate is approved, else None."""
        gate = AWAITING_GATE.get(state)
        if gate is None:
            return None
        if not transitions.is_gate_approved(conn, job_id, gate):
            return None
        return _AFTER_GATE.get(state)

    def _mark_failed(self, job_id: str, exc: Exception) -> None:
        """Record the failure on the job so an operator can see and retry it.

        Best effort in its own transaction: the one that raised is already
        rolled back, and a database that cannot take this write is a bigger
        problem than one lost error string.

        Not every state can move to FAILED — `draft` cannot, because a job
        that has not started has nothing to fail. When the state machine
        refuses, the audit trail still gets the event, so the failure is never
        invisible even though the job's state is untouched.
        """
        reason = f"{type(exc).__name__}: {exc}"[:2000]
        try:
            with self.connect() as conn:
                state = transitions.get_state(conn, job_id, for_update=True)
                if is_terminal(state) or state is S.FAILED:
                    return
                try:
                    transitions.transition(
                        conn, job_id, S.FAILED, actor=self.name, error=reason
                    )
                except (IllegalTransition, GateNotApproved):
                    transitions.record_event(
                        conn,
                        job_id=job_id,
                        event_type=EVENT_WORKER_ERROR,
                        frm=state,
                        actor=self.name,
                        detail={"error": reason, "state_unchanged": state.value},
                    )
        except JobNotFound:
            pass
        except Exception:  # noqa: BLE001
            log.exception("%s: could not mark job %s failed", self.name, job_id)


def resume_after_gate(queue: Queue, job_id: str) -> None:
    """Put a job back on the queue after a human approved its gate.

    The API calls this; it is the reason a parked job needs no polling.
    """
    queue.push(str(job_id))


def main() -> int:  # pragma: no cover - process entry point
    """`python -m packages.worker.runner`"""
    import os

    from packages.api.deps import Database

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        from .queue import RedisQueue

        queue: Queue = RedisQueue(redis_url)
    else:
        log.warning("REDIS_URL is not set - using an in-process queue (dev only)")
        from .queue import MemoryQueue

        queue = MemoryQueue()

    db = Database(os.getenv("DATABASE_URL", ""))
    db.open()
    worker = Worker(queue, db.connection, name=os.getenv("WORKER_NAME", "worker-1"))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run(drain=False)
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
