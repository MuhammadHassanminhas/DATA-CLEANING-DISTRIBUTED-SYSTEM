"""Durable task queue (Phase 2.2).

The queue **is** the `tasks` table — there is no second store and no
dual-write (PHASE_STATE.md Decision #79). A dequeue claims rows with
`SELECT ... FOR UPDATE SKIP LOCKED` inside the same statement that flips
`QUEUED -> ASSIGNED` and stamps the worker, so the claim and the state
transition commit together on one row. Two coordinator replicas cannot
hand out the same task because the second one's `SKIP LOCKED` steps over
the row the first has locked, and by the time that lock is released the
row no longer matches `status = 'QUEUED'`.

**Ordering guarantee** (Step 2.2 exit criterion, stated so it can be
tested rather than assumed):

  * A **single** dequeuer receives tasks in strict
    `(priority ASC, created_at ASC)` order. `priority` is
    lower-is-more-urgent (Phase 2.1), so both keys ascend and
    `ix_tasks_queue` supplies the ordering with no sort node.
  * With **N concurrent** dequeuers the order is *not* globally total,
    and that is deliberate. `SKIP LOCKED` is what buys the concurrency:
    a second dequeuer stepping over a row locked by the first may claim
    a slightly later task first. What is guaranteed under concurrency is
    that no task is handed out twice, no task is lost, and no eligible
    task is skipped once the lock that hid it is gone (no starvation).

Nothing here writes `lease_expires_at` or `attempt_count`. Those columns
stay untouched through all of M2 — a Phase 2.1 exit criterion, and the
lease engine that owns them is Phase 3.

There is no requeue primitive, for the same reason: `ASSIGNED -> QUEUED`
is not a legal move in `task_states`. Returning a claimed task to the
queue is the Phase 3 `REASSIGNED` transition and is not invented here.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.task_states import ASSIGNED, QUEUED, check_transition
from app.task_types import validate_parameters

# Recommendations, not measured values (CLAUDE.md §10). They bound how
# much work one call can ask for so a single request cannot monopolise a
# connection or a transaction. Step 2.8's harness is what produces
# defensible numbers; revise there.
DEFAULT_DEQUEUE_LIMIT = 1


class QueueLimitExceeded(ValueError):
    """Raised when a caller asks for a batch larger than the configured cap."""


# The transition this module performs, checked against the state machine
# at import rather than hardcoded twice. The dequeue below expresses
# `QUEUED -> ASSIGNED` in SQL, where `check_transition` cannot run per
# row, so this is what keeps `task_states` the single authority: if a
# later phase makes that move illegal, importing this module fails loudly
# instead of the SQL silently disagreeing with the state machine. Not an
# `assert` — those vanish under `python -O`, and this is a real check.
if not check_transition(QUEUED, ASSIGNED):
    raise RuntimeError(
        f"task_queue performs {QUEUED} -> {ASSIGNED}, which task_states no longer allows"
    )


def _validated_row(
    *,
    task_type: str,
    parameters: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    priority: int,
    correlation_id: str,
) -> dict[str, Any]:
    """Build one INSERT row, with parameters normalised by the registry.

    Raises `UnknownTaskType` / `InvalidTaskParameters` from
    `app.task_types` — validation happens here, before anything reaches
    the database, because the operator API in Step 2.6 is untrusted input
    exactly as a worker is (CLAUDE.md §12).
    """
    return {
        "id": uuid.uuid4(),
        "task_type": task_type,
        "parameters": validate_parameters(task_type, parameters),
        "payload": payload,
        "priority": priority,
        "status": QUEUED,
        "correlation_id": correlation_id,
    }


async def enqueue(
    session: AsyncSession,
    *,
    task_type: str,
    correlation_id: str,
    parameters: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
) -> uuid.UUID:
    """Enqueue one task. Returns its id. Does not commit."""
    row = _validated_row(
        task_type=task_type,
        parameters=parameters,
        payload=payload,
        priority=priority,
        correlation_id=correlation_id,
    )
    await session.execute(Task.__table__.insert().values(**row))
    return row["id"]


async def enqueue_batch(
    session: AsyncSession,
    *,
    task_type: str,
    correlation_id: str,
    count: int,
    max_batch: int,
    parameters: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
) -> list[uuid.UUID]:
    """Enqueue `count` identical tasks in one multi-row INSERT.

    Exists because the 10,000-task exit criterion has to be drivable from
    outside the cluster, and the public ingress rate-limits to a handful
    of requests per second (Step 1.5.5). One request that enqueues many
    tasks is the honest way through that, not a loosened edge limit.

    Parameters are validated once — every row is identical by
    construction, so re-validating per row would buy nothing.
    """
    if count < 1:
        raise QueueLimitExceeded("count must be at least 1")
    if count > max_batch:
        raise QueueLimitExceeded(f"count {count} exceeds the {max_batch}-task batch cap")

    rows = [
        _validated_row(
            task_type=task_type,
            parameters=parameters,
            payload=payload,
            priority=priority,
            correlation_id=correlation_id,
        )
        for _ in range(count)
    ]
    await session.execute(Task.__table__.insert(), rows)
    return [row["id"] for row in rows]


# One statement: claim, transition, stamp, return. The CTE takes the row
# locks; the UPDATE that consumes it is in the same transaction, so a
# crash between "claimed" and "assigned" is impossible — there is no
# between.
#
# `updated_at` is set explicitly because the model's `onupdate=func.now()`
# is a SQLAlchemy ORM-level hook and does not fire for raw SQL.
_DEQUEUE_SQL = text(
    """
    WITH claimed AS (
        SELECT id
        FROM tasks
        WHERE status = 'QUEUED'
        ORDER BY priority, created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE tasks AS t
    SET status = 'ASSIGNED',
        assigned_worker_id = CAST(:worker_id AS uuid),
        assigned_at = now(),
        updated_at = now()
    FROM claimed
    WHERE t.id = claimed.id
    RETURNING t.id, t.task_type, t.parameters, t.payload, t.priority,
              t.correlation_id, t.assigned_at
    """
)


async def dequeue(
    session: AsyncSession,
    *,
    worker_id: uuid.UUID | str,
    limit: int = DEFAULT_DEQUEUE_LIMIT,
    max_batch: int,
) -> list[dict[str, Any]]:
    """Claim up to `limit` queued tasks for `worker_id`. Does not commit.

    Returns the claimed rows, newest claim state included. An empty list
    means the queue held nothing claimable — either genuinely empty, or
    every candidate row was locked by a concurrent dequeuer. The caller
    cannot distinguish those two, and does not need to: both mean "no
    work for you right now".

    This is a queue primitive, not an assignment decision. The caller
    names the worker. *Choosing* which worker should get work — capability
    filtering, credit accounting, the push over the Redis channel — is the
    assignment engine in Step 2.3, which calls this.
    """
    if limit < 1:
        raise QueueLimitExceeded("limit must be at least 1")
    if limit > max_batch:
        raise QueueLimitExceeded(f"limit {limit} exceeds the {max_batch}-task dequeue cap")

    result = await session.execute(
        _DEQUEUE_SQL, {"limit": limit, "worker_id": str(worker_id)}
    )
    return [dict(row) for row in result.mappings().all()]


async def queue_depth(session: AsyncSession) -> int:
    """How many tasks are waiting.

    `status` is the leading column of `ix_tasks_queue`, so the planner
    answers this from the index. **Measured**, not assumed (PHASE_STATE.md
    Step 2.2): at 320,025 rows with 10,000 queued, 2.4 ms — and the cost
    tracks the number of *queued* rows, not the size of the table, because
    only matching index entries and their heap blocks are visited. That is
    the property that matters, since `tasks` accumulates terminal rows for
    the lifetime of the system.

    One caveat worth knowing rather than discovering: immediately after a
    bulk enqueue, before autovacuum has updated the visibility map, the
    planner falls back to a sequential scan (measured at 3.6 ms on 20,025
    rows — cheap there, but it grows with the table, not the queue). It
    recovers on its own once the table is vacuumed. A partial index
    (`... WHERE status = 'QUEUED'`) removes that fallback entirely and is
    17x smaller, but was measured as no faster in the steady state, so it
    is recorded as an option rather than taken now.
    """
    result = await session.execute(
        select(func.count()).select_from(Task).where(Task.status == QUEUED)
    )
    return int(result.scalar_one())


async def counts_by_status(session: AsyncSession) -> dict[str, int]:
    """Task counts grouped by status — the lifecycle summary Step 2.7's
    dashboard needs.

    Deliberately separate from `queue_depth`: this one groups over the
    whole table, so it is not the cheap read the exit criterion is about.
    Keeping them apart means the cheap path stays cheap.
    """
    result = await session.execute(select(Task.status, func.count()).group_by(Task.status))
    return {status: int(count) for status, count in result.all()}
