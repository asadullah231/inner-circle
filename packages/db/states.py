"""
Job state machine — the workflow engine's rulebook.

PRD 7.2 flow:
    Draft -> Planning -> [G1 script] -> Sourcing -> [G2 assets]
          -> Audio+compose -> Rendering -> QA -> [G3 final] -> Complete

This module is pure: no database, no I/O, no imports beyond the stdlib. It
answers one question — "is this transition legal?" — so both the API and the
worker can enforce the same rules, and so the rules are testable without a
Postgres.

Persistence lives in transitions.py, which calls into here.

Design rules (PRD FR-11, NFR Reliability):
  * Only transitions listed in _ALLOWED may happen. Anything else raises.
  * A transition to the state a job is already in is a no-op, not an error —
    this is what makes a retried worker message idempotent.
  * Every terminal state is final: nothing leaves COMPLETE or CANCELLED.
    FAILED is the one exception — an operator may retry it back into the
    pipeline (PRD 3.3: a failed job must not force a fresh job).
"""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    """Mirrors the `job_state` enum in migrations/001_core_schema.sql."""

    DRAFT = "draft"
    PLANNING = "planning"
    PLANNED = "planned"           # waiting on G1 (script approval)
    RETRIEVING = "retrieving"
    RETRIEVED = "retrieved"       # waiting on G2 (asset/contact-sheet approval)
    RENDERING = "rendering"
    RENDERED = "rendered"
    QA = "qa"
    REVIEW = "review"             # waiting on G3 (final approval)
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Gate(str, Enum):
    """Mirrors the `approval_gate` enum. The three human checkpoints."""

    G1_SCRIPT = "g1_script"
    G2_STORYBOARD = "g2_storyboard"
    G3_FINAL = "g3_final"


class IllegalTransition(ValueError):
    """Raised when a transition is not in the state machine."""

    def __init__(self, frm: "JobState", to: "JobState", reason: str = ""):
        self.frm, self.to = frm, to
        detail = f": {reason}" if reason else ""
        super().__init__(f"illegal transition {frm.value} -> {to.value}{detail}")


class GateNotApproved(PermissionError):
    """Raised when a gated transition is attempted without its approval."""

    def __init__(self, gate: "Gate", frm: "JobState", to: "JobState"):
        self.gate, self.frm, self.to = gate, frm, to
        super().__init__(
            f"{frm.value} -> {to.value} requires {gate.value} approval"
        )


S = JobState

#: The only legal moves. Everything not listed here is rejected.
_ALLOWED: dict[JobState, frozenset[JobState]] = {
    S.DRAFT:      frozenset({S.PLANNING, S.CANCELLED}),
    S.PLANNING:   frozenset({S.PLANNED, S.FAILED, S.CANCELLED}),
    S.PLANNED:    frozenset({S.RETRIEVING, S.PLANNING, S.FAILED, S.CANCELLED}),
    S.RETRIEVING: frozenset({S.RETRIEVED, S.FAILED, S.CANCELLED}),
    S.RETRIEVED:  frozenset({S.RENDERING, S.RETRIEVING, S.FAILED, S.CANCELLED}),
    S.RENDERING:  frozenset({S.RENDERED, S.FAILED, S.CANCELLED}),
    S.RENDERED:   frozenset({S.QA, S.FAILED, S.CANCELLED}),
    S.QA:         frozenset({S.REVIEW, S.RENDERING, S.FAILED, S.CANCELLED}),
    S.REVIEW:     frozenset({S.COMPLETE, S.RENDERING, S.FAILED, S.CANCELLED}),
    S.COMPLETE:   frozenset(),
    S.CANCELLED:  frozenset(),
    # An operator may put a failed job back at the step that broke.
    S.FAILED:     frozenset({S.PLANNING, S.RETRIEVING, S.RENDERING, S.CANCELLED}),
}

#: Transitions that may only happen once a human has approved a gate.
#: PRD FR-3: nothing proceeds past a gate without explicit approval.
_GATED: dict[tuple[JobState, JobState], Gate] = {
    (S.PLANNED, S.RETRIEVING): Gate.G1_SCRIPT,
    (S.RETRIEVED, S.RENDERING): Gate.G2_STORYBOARD,
    (S.REVIEW, S.COMPLETE): Gate.G3_FINAL,
}

#: States a job can never leave.
TERMINAL: frozenset[JobState] = frozenset({S.COMPLETE, S.CANCELLED})

#: States where the pipeline is parked waiting on a person, mapped to the gate.
AWAITING_GATE: dict[JobState, Gate] = {
    S.PLANNED: Gate.G1_SCRIPT,
    S.RETRIEVED: Gate.G2_STORYBOARD,
    S.REVIEW: Gate.G3_FINAL,
}


def allowed_from(state: JobState) -> frozenset[JobState]:
    """Every state reachable in one step from `state`."""
    return _ALLOWED[state]


def is_terminal(state: JobState) -> bool:
    return state in TERMINAL


def gate_for(frm: JobState, to: JobState) -> Gate | None:
    """The approval this transition needs, or None if it needs no approval."""
    return _GATED.get((frm, to))


def is_legal(frm: JobState, to: JobState) -> bool:
    """True if the move exists in the machine. Ignores gate approval."""
    return to in _ALLOWED[frm]


def check(frm: JobState, to: JobState, *, gate_approved: bool = False) -> bool:
    """
    Validate a transition.

    Returns True if the caller should write the change, False if it is a no-op
    (same state in, same state out — an already-delivered message being
    redelivered). Raises on anything illegal.

    `gate_approved` must be supplied by the caller after checking the approvals
    table; this module never touches the database.
    """
    if frm == to:
        return False  # idempotent redelivery, nothing to write

    if is_terminal(frm):
        raise IllegalTransition(frm, to, f"{frm.value} is terminal")

    if not is_legal(frm, to):
        legal = ", ".join(sorted(s.value for s in _ALLOWED[frm])) or "nothing"
        raise IllegalTransition(frm, to, f"legal from {frm.value}: {legal}")

    gate = gate_for(frm, to)
    if gate is not None and not gate_approved:
        raise GateNotApproved(gate, frm, to)

    return True
