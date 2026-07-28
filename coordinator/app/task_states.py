"""Task state machine (Phase 2.1).

The states and legal moves are fixed here, in one place, so that every
write to `Task.status` goes through `check_transition` — the same
single-write-path discipline `Worker.status` already follows via
`_transition_status` in `app/main.py` (see `models.py`'s docstring).

    QUEUED -> ASSIGNED -> RUNNING -> COMPLETED

`FAILED` and `CANCELLED` are terminal and reachable from any live state.
`REASSIGNED` is reserved for Phase 3 and is deliberately unreachable in
V1: the constant exists so Phase 3 adds a transition rather than a state,
but no entry in `_ALLOWED` targets it, so attempting the move raises
today. There is a test asserting exactly that.

**All transitions are coordinator-authoritative.** A worker never writes
task state; it reports, and the coordinator decides what that means.

Same-state moves are a no-op rather than an error. That is what makes
duplicate result submission harmless (CLAUDE.md §3.7, idempotency
mandatory): a second `RUNNING -> COMPLETED` for an already-COMPLETED task
returns False and writes nothing, instead of either raising or
double-writing. `check_transition` returns whether the caller should
write, so the no-op is impossible to miss.
"""

from __future__ import annotations

QUEUED = "QUEUED"
ASSIGNED = "ASSIGNED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
REASSIGNED = "REASSIGNED"  # reserved for Phase 3, unreachable in V1

TASK_STATES = frozenset(
    {QUEUED, ASSIGNED, RUNNING, COMPLETED, FAILED, CANCELLED, REASSIGNED}
)

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

# Reachability, not policy. That a task *can* move to FAILED is settled
# here; what the coordinator does about a failure — retry, reassign,
# give up — is Phase 3 and is deliberately not encoded in this table.
_ALLOWED: dict[str, frozenset[str]] = {
    QUEUED: frozenset({ASSIGNED, CANCELLED}),
    ASSIGNED: frozenset({RUNNING, FAILED, CANCELLED}),
    RUNNING: frozenset({COMPLETED, FAILED, CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    REASSIGNED: frozenset(),
}


class InvalidTaskTransition(ValueError):
    """Raised when a caller attempts a move the state machine forbids."""


class UnknownTaskState(ValueError):
    """Raised when a state string is not one of `TASK_STATES`."""


def is_terminal(state: str) -> bool:
    if state not in TASK_STATES:
        raise UnknownTaskState(f"unknown task state: {state!r}")
    return state in TERMINAL_STATES


def check_transition(current: str, new: str) -> bool:
    """Validate a task state move.

    Returns True when the caller should perform the write, False when the
    move is a same-state no-op (see the module docstring on idempotency).
    Raises `UnknownTaskState` for an unrecognised state on either side,
    and `InvalidTaskTransition` for a recognised but illegal move.
    """
    if current not in TASK_STATES:
        raise UnknownTaskState(f"unknown current task state: {current!r}")
    if new not in TASK_STATES:
        raise UnknownTaskState(f"unknown target task state: {new!r}")

    if current == new:
        return False

    if new not in _ALLOWED[current]:
        raise InvalidTaskTransition(f"illegal task transition: {current} -> {new}")

    return True
