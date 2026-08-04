"""Task state machine tests (Phase 2.1).

Covers the exit criterion "invalid state transitions are rejected in code
— verified by test". Pure, no database, so these run anywhere pytest does
and in CI without a Postgres service.
"""

import pytest

from app.task_states import (
    ASSIGNED,
    CANCELLED,
    COMPLETED,
    FAILED,
    QUEUED,
    REASSIGNED,
    RUNNING,
    TERMINAL_STATES,
    InvalidTaskTransition,
    UnknownTaskState,
    check_transition,
    is_terminal,
)

HAPPY_PATH = [(QUEUED, ASSIGNED), (ASSIGNED, RUNNING), (RUNNING, COMPLETED)]


@pytest.mark.parametrize(("current", "new"), HAPPY_PATH)
def test_happy_path_transitions_are_allowed(current, new):
    assert check_transition(current, new) is True


@pytest.mark.parametrize("live", [ASSIGNED, RUNNING])
def test_failed_is_reachable_from_every_live_state(live):
    assert check_transition(live, FAILED) is True


@pytest.mark.parametrize("live", [QUEUED, ASSIGNED, RUNNING])
def test_cancelled_is_reachable_from_every_live_state(live):
    assert check_transition(live, CANCELLED) is True


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (QUEUED, RUNNING),  # cannot skip assignment
        (QUEUED, COMPLETED),  # cannot skip the whole pipeline
        (ASSIGNED, COMPLETED),  # cannot complete without running
        (RUNNING, ASSIGNED),  # no going backwards
    ],
)
def test_illegal_moves_are_rejected(current, new):
    with pytest.raises(InvalidTaskTransition):
        check_transition(current, new)


@pytest.mark.parametrize("live", [ASSIGNED, RUNNING])
def test_a_live_task_can_be_returned_to_the_queue(live):
    """Phase 3.1's recovery move, and the reason it is `-> QUEUED`.

    Until M3 both of these raised, under the heading "no going backwards".
    The lease reclaimer is exactly a task going backwards: its worker
    stopped answering, so the work has not happened and must be offered
    again.

    It returns to `QUEUED` rather than to the reserved `REASSIGNED` state
    because the queue is `WHERE status = 'QUEUED'` — the test below still
    holds `REASSIGNED` unreachable, so a recovered task that landed there
    would be invisible to every dequeue forever.
    """
    assert check_transition(live, QUEUED) is True


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("target", [QUEUED, ASSIGNED, RUNNING])
def test_terminal_states_never_move_on(terminal, target):
    with pytest.raises(InvalidTaskTransition):
        check_transition(terminal, target)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_terminal_states_are_terminal(terminal):
    assert is_terminal(terminal) is True


@pytest.mark.parametrize("live", [QUEUED, ASSIGNED, RUNNING])
def test_live_states_are_not_terminal(live):
    assert is_terminal(live) is False


@pytest.mark.parametrize("state", [QUEUED, ASSIGNED, RUNNING, COMPLETED, FAILED, CANCELLED])
def test_same_state_is_a_no_op_not_an_error(state):
    """Idempotency (CLAUDE.md §3.7): a duplicate submission must be
    harmless. `False` means "do not write" — not "rejected"."""
    assert check_transition(state, state) is False


@pytest.mark.parametrize("current", [QUEUED, ASSIGNED, RUNNING])
def test_reassigned_is_reserved_and_unreachable_in_v1(current):
    """Phase 3 will add this transition. Until then the state exists as a
    constant but nothing may move into it — so the schema and the enum
    are already Phase-3 shaped while the behaviour is not."""
    with pytest.raises(InvalidTaskTransition):
        check_transition(current, REASSIGNED)


def test_unknown_states_are_rejected_on_both_sides():
    with pytest.raises(UnknownTaskState):
        check_transition("NONSENSE", QUEUED)
    with pytest.raises(UnknownTaskState):
        check_transition(QUEUED, "NONSENSE")
    with pytest.raises(UnknownTaskState):
        is_terminal("NONSENSE")


def test_worker_reported_status_cannot_be_smuggled_in():
    """Worker lifecycle states (`ONLINE`, `OFFLINE`, ...) are a different
    machine. Sharing a free-text column type must not mean sharing a
    vocabulary."""
    for worker_state in ("ONLINE", "OFFLINE", "SUSPECT", "REGISTERED", "QUARANTINED"):
        with pytest.raises(UnknownTaskState):
            check_transition(QUEUED, worker_state)
