"""Task state machine (Phase 2.1).

The states and legal moves are fixed here, in one place, so that every
write to `Task.status` goes through `check_transition` — the same
single-write-path discipline `Worker.status` already follows via
`_transition_status` in `app/main.py` (see `models.py`'s docstring).

    QUEUED -> ASSIGNED -> RUNNING -> COMPLETED

`FAILED` and `CANCELLED` are terminal and reachable from any live state.

**Phase 3.1 adds `ASSIGNED -> QUEUED` and `RUNNING -> QUEUED`** — the
lease reclaimer returning a task whose worker stopped answering. That is
the recovery move the whole milestone stands on, and it is a *return to
the queue*, not a new state.

`REASSIGNED` stays unreachable as a **status**, and the gate is explicit
that this is a decision rather than an oversight (§3.0.7): the queue is
`WHERE status = 'QUEUED'`, so a recovered task must genuinely be `QUEUED`
to be claimable again. A `REASSIGNED` status would be either a state
nothing can observe or a second queue predicate to maintain forever. The
constant is kept for the meaning it does have — the *attempt* that was
superseded, which Step 3.2 records in `task_attempts`.

This corrects the pre-M3 wording here and in `task_queue`'s module
docstring, both of which said returning a task to the queue *is* the
`REASSIGNED` transition. They were written before the queue predicate had
its present shape; the gate named the contradiction rather than quietly
resolving it, and this is where it is resolved.

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
#
# The two `-> QUEUED` moves are Phase 3.1's, and they are reachable *only*
# from the lease reclaimer: no worker-reported message can ask for them.
# `mark_status` takes a worker's word for `RUNNING` and `FAILED`; a worker
# asking to put its own task back in the queue would be a worker deciding
# scheduling, which §3.2 forbids outright.
_ALLOWED: dict[str, frozenset[str]] = {
    QUEUED: frozenset({ASSIGNED, CANCELLED}),
    ASSIGNED: frozenset({RUNNING, FAILED, CANCELLED, QUEUED}),
    RUNNING: frozenset({COMPLETED, FAILED, CANCELLED, QUEUED}),
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
