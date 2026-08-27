"""Unit tests for the pure state machine. No database, no driver."""

from __future__ import annotations

import pytest

from packages.db.states import (
    _ALLOWED,
    AWAITING_GATE,
    TERMINAL,
    Gate,
    GateNotApproved,
    IllegalTransition,
    JobState as S,
    allowed_from,
    check,
    gate_for,
    is_legal,
    is_terminal,
)


# --- graph integrity --------------------------------------------------------
def test_every_state_has_a_rule():
    assert set(_ALLOWED) == set(S), "a state is missing from the transition map"


def test_every_state_reachable_from_draft():
    seen, stack = {S.DRAFT}, [S.DRAFT]
    while stack:
        for nxt in _ALLOWED[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    assert seen == set(S), f"unreachable: {set(S) - seen}"


def test_targets_are_valid_states():
    for frm, targets in _ALLOWED.items():
        for to in targets:
            assert isinstance(to, S), f"{frm} -> {to} is not a JobState"


def test_no_self_transitions_in_the_map():
    """Self-moves are handled as no-ops in check(), not as edges."""
    for frm, targets in _ALLOWED.items():
        assert frm not in targets, f"{frm.value} lists itself as a target"


def test_state_values_match_sql_enum():
    """The enum here must mirror job_state in 001_core_schema.sql."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "migrations" / "001_core_schema.sql").read_text(
        encoding="utf-8"
    )
    for state in S:
        assert f"'{state.value}'" in sql, f"{state.value} missing from the SQL enum"


def test_gate_values_match_sql_enum():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "migrations" / "001_core_schema.sql").read_text(
        encoding="utf-8"
    )
    for gate in Gate:
        assert f"'{gate.value}'" in sql, f"{gate.value} missing from the SQL enum"


# --- happy path -------------------------------------------------------------
def test_prd_happy_path_is_walkable():
    """PRD 7.2: draft -> planning -> [G1] -> retrieving -> [G2] -> render -> qa -> [G3] -> complete."""
    path = [
        (S.DRAFT, S.PLANNING, False),
        (S.PLANNING, S.PLANNED, False),
        (S.PLANNED, S.RETRIEVING, True),      # G1
        (S.RETRIEVING, S.RETRIEVED, False),
        (S.RETRIEVED, S.RENDERING, True),     # G2
        (S.RENDERING, S.RENDERED, False),
        (S.RENDERED, S.QA, False),
        (S.QA, S.REVIEW, False),
        (S.REVIEW, S.COMPLETE, True),         # G3
    ]
    for frm, to, gated in path:
        assert check(frm, to, gate_approved=gated) is True, f"{frm.value} -> {to.value}"


# --- illegal moves ----------------------------------------------------------
def test_cannot_skip_the_pipeline():
    with pytest.raises(IllegalTransition):
        check(S.DRAFT, S.COMPLETE)


def test_cannot_jump_planning_to_rendering():
    with pytest.raises(IllegalTransition):
        check(S.PLANNING, S.RENDERING)


def test_cannot_go_backwards_arbitrarily():
    with pytest.raises(IllegalTransition):
        check(S.RENDERED, S.PLANNING)


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
def test_terminal_states_are_final(terminal):
    assert allowed_from(terminal) == frozenset()
    assert is_terminal(terminal)
    with pytest.raises(IllegalTransition, match="terminal"):
        check(terminal, S.PLANNING)


def test_illegal_transition_message_lists_legal_targets():
    with pytest.raises(IllegalTransition, match="legal from draft"):
        check(S.DRAFT, S.QA)


# --- gates ------------------------------------------------------------------
@pytest.mark.parametrize(
    "frm,to,gate",
    [
        (S.PLANNED, S.RETRIEVING, Gate.G1_SCRIPT),
        (S.RETRIEVED, S.RENDERING, Gate.G2_STORYBOARD),
        (S.REVIEW, S.COMPLETE, Gate.G3_FINAL),
    ],
)
def test_gated_transition_blocked_without_approval(frm, to, gate):
    assert gate_for(frm, to) is gate
    with pytest.raises(GateNotApproved) as e:
        check(frm, to, gate_approved=False)
    assert e.value.gate is gate
    assert check(frm, to, gate_approved=True) is True


def test_ungated_transition_ignores_approval_flag():
    assert check(S.DRAFT, S.PLANNING, gate_approved=False) is True
    assert gate_for(S.DRAFT, S.PLANNING) is None


def test_awaiting_gate_covers_every_gate():
    assert set(AWAITING_GATE.values()) == set(Gate)


def test_cancel_never_needs_a_gate():
    """An operator can always abandon a job that is not already terminal."""
    for state in S:
        if state in TERMINAL:
            continue
        assert is_legal(state, S.CANCELLED), f"{state.value} cannot be cancelled"
        assert gate_for(state, S.CANCELLED) is None


# --- idempotency and recovery ----------------------------------------------
@pytest.mark.parametrize("state", sorted(S, key=lambda s: s.value))
def test_same_state_is_a_noop_not_an_error(state):
    """A redelivered worker message must not blow up (PRD FR-11)."""
    assert check(state, state) is False


def test_failed_can_be_retried_into_the_pipeline():
    """PRD 3.3: a failure must not force a brand-new job."""
    for target in (S.PLANNING, S.RETRIEVING, S.RENDERING):
        assert check(S.FAILED, target) is True


def test_failed_cannot_jump_straight_to_complete():
    with pytest.raises(IllegalTransition):
        check(S.FAILED, S.COMPLETE)


def test_qa_can_send_a_job_back_to_render():
    assert check(S.QA, S.RENDERING) is True


def test_review_rejection_can_send_a_job_back_to_render():
    assert check(S.REVIEW, S.RENDERING) is True


def test_every_working_state_can_fail():
    working = {S.PLANNING, S.PLANNED, S.RETRIEVING, S.RETRIEVED,
               S.RENDERING, S.RENDERED, S.QA, S.REVIEW}
    for state in working:
        assert is_legal(state, S.FAILED), f"{state.value} cannot fail"
