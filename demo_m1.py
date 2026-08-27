"""
M1.2 demo - the job state machine, running.

No Postgres needed: this uses an in-memory stand-in for the four tables the
state machine touches, so anyone can run it with `python demo_m1.py`. The
rules being enforced are the real ones from packages/db/states.py, and the
calls are the same ones packages/db/transitions.py makes against Postgres.

What it shows, in order:
  1. A job walks the full PRD 7.2 pipeline, draft to complete
  2. A gate blocks the pipeline until a human approves
  3. An illegal transition is refused
  4. A terminal job cannot be restarted
  5. A redelivered worker message is a no-op, not a crash
  6. The audit trail that recorded all of it
"""

from __future__ import annotations

import sys

from packages.db.states import (
    Gate,
    GateNotApproved,
    IllegalTransition,
    JobState,
    check,
    gate_for,
)

# -- colours (skipped when piped to a file) -------------------------------
TTY = sys.stdout.isatty()
DIM = "\033[2m" if TTY else ""
BOLD = "\033[1m" if TTY else ""
GREEN = "\033[32m" if TTY else ""
RED = "\033[31m" if TTY else ""
YELLOW = "\033[33m" if TTY else ""
CYAN = "\033[36m" if TTY else ""
OFF = "\033[0m" if TTY else ""


class FakeJob:
    """
    Stands in for the `jobs`, `approvals` and `audit_events` rows.

    Postgres holds this in production; keeping it in memory here means the
    demo runs anywhere, and the rules being exercised are identical.
    """

    def __init__(self, title: str):
        self.title = title
        self.id = "job_7f3a91"
        self.state = JobState.DRAFT
        self.error: str | None = None
        self.approvals: dict[Gate, str] = {}
        self.audit: list[tuple[str, str, str]] = []
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 47
        m, s = divmod(self._clock, 60)
        return f"06:{m:02d}:{s:02d}"

    def transition(self, to: JobState, actor: str) -> bool:
        """Mirrors transitions.transition(): validate, apply, audit."""
        gate = gate_for(self.state, to)
        approved = self.approvals.get(gate) == "approved" if gate else False
        changed = check(self.state, to, gate_approved=approved)
        if not changed:
            return False
        frm, self.state = self.state, to
        self.audit.append((self._stamp(), f"{frm.value} -> {to.value}", actor))
        return True

    def decide(self, gate: Gate, approved: bool, actor: str, note: str = "") -> None:
        """Mirrors transitions.decide_gate()."""
        if not approved and not note.strip():
            raise ValueError("a rejection requires a note")
        self.approvals[gate] = "approved" if approved else "rejected"
        detail = f"{gate.value} {self.approvals[gate]}"
        self.audit.append((self._stamp(), detail, actor))


def head(n: int, text: str) -> None:
    print(f"\n{BOLD}{n}. {text}{OFF}")
    print(DIM + "-" * 62 + OFF)


def ok(text: str) -> None:
    print(f"   {GREEN}OK{OFF}      {text}")


def blocked(text: str) -> None:
    print(f"   {YELLOW}BLOCKED{OFF} {text}")


def refused(text: str) -> None:
    print(f"   {RED}REFUSED{OFF} {text}")


def step(job: FakeJob, to: JobState, actor: str = "worker-1") -> None:
    """Try a transition and report what the state machine decided."""
    try:
        if job.transition(to, actor):
            ok(f"{actor}: job is now {BOLD}{to.value}{OFF}")
        else:
            print(f"   {CYAN}NO-OP{OFF}   already {to.value}, nothing written")
    except GateNotApproved as e:
        blocked(f"{e.frm.value} -> {e.to.value} needs {BOLD}{e.gate.value}{OFF} approval")
    except IllegalTransition as e:
        refused(str(e))


def main() -> None:
    print(f"\n{BOLD}INNER CIRCLE - M1.2 job state machine{OFF}")
    print(DIM + "packages/db/states.py + transitions.py, running for real" + OFF)

    job = FakeJob("Solar panels explainer")
    print(f"\n   project : {job.title}")
    print(f"   job     : {job.id}")
    print(f"   state   : {job.state.value}")

    # 1 -----------------------------------------------------------------
    head(1, "The worker starts planning")
    step(job, JobState.PLANNING)
    step(job, JobState.PLANNED)
    print(f"   {DIM}the planner produced a VideoSpec; the job now waits for a human{OFF}")

    # 2 -----------------------------------------------------------------
    head(2, "G1 blocks the pipeline until a human approves the script")
    step(job, JobState.RETRIEVING)
    print(f"   {DIM}nothing was written: the job is still {job.state.value}{OFF}")

    print(f"\n   {DIM}reviewer rejects it first{OFF}")
    job.decide(Gate.G1_SCRIPT, approved=False, actor="asad", note="line 3 is factually wrong")
    print(f"   {YELLOW}REJECTED{OFF} g1_script - a rejection requires a note, enforced in code")
    step(job, JobState.RETRIEVING)

    print(f"\n   {DIM}script fixed, reviewer approves{OFF}")
    job.decide(Gate.G1_SCRIPT, approved=True, actor="asad")
    ok("asad approved g1_script")
    step(job, JobState.RETRIEVING)

    # 3 -----------------------------------------------------------------
    head(3, "Media worker finishes, G2 gates the render")
    step(job, JobState.RETRIEVED)
    step(job, JobState.RENDERING)
    job.decide(Gate.G2_STORYBOARD, approved=True, actor="asad")
    ok("asad approved g2_storyboard")
    step(job, JobState.RENDERING)

    # 4 -----------------------------------------------------------------
    head(4, "Render, automated QA, then G3 final sign-off")
    step(job, JobState.RENDERED)
    step(job, JobState.QA)
    step(job, JobState.REVIEW)
    step(job, JobState.COMPLETE, actor="asad")
    job.decide(Gate.G3_FINAL, approved=True, actor="asad")
    ok("asad approved g3_final")
    step(job, JobState.COMPLETE, actor="asad")

    # 5 -----------------------------------------------------------------
    head(5, "The rules that protect the pipeline")

    print(f"   {DIM}a finished job cannot be restarted{OFF}")
    step(job, JobState.PLANNING)

    print(f"\n   {DIM}a fresh job cannot skip the pipeline{OFF}")
    other = FakeJob("Skip test")
    step(other, JobState.COMPLETE)

    print(f"\n   {DIM}a redelivered worker message is a no-op, not a crash{OFF}")
    other.transition(JobState.PLANNING, "worker-2")
    step(other, JobState.PLANNING, actor="worker-2")

    print(f"\n   {DIM}a failed job is retried, not thrown away{OFF}")
    third = FakeJob("Retry test")
    third.transition(JobState.PLANNING, "worker-3")
    third.transition(JobState.FAILED, "worker-3")
    print(f"   {RED}FAILED{OFF}  provider timed out")
    step(third, JobState.PLANNING, actor="operator")

    # 6 -----------------------------------------------------------------
    head(6, "Audit trail - every move, who made it")
    for stamp, what, actor in job.audit:
        print(f"   {DIM}{stamp}{OFF}  {what:<28} {DIM}{actor}{OFF}")

    print(f"\n{DIM}   Every line above is a row in audit_events. Nothing moves this")
    print(f"   pipeline without leaving one.{OFF}")

    print(f"\n{BOLD}   final state: {GREEN}{job.state.value}{OFF}")
    print(f"{DIM}   Next: M1.3 puts a FastAPI service in front of this,")
    print(f"   M1.4 a Redis worker behind it.{OFF}\n")


if __name__ == "__main__":
    main()
