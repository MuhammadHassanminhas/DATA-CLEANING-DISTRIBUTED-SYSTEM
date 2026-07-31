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

import json
import uuid
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.task_states import (
    ASSIGNED,
    COMPLETED,
    QUEUED,
    RUNNING,
    InvalidTaskTransition,
    check_transition,
)
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
# `attempt_count` is **read** in the RETURNING list from Phase 2.5 so the
# assignment can tell the worker which attempt it is running, and the
# worker can echo it back in the result envelope (a 2.5 exit criterion:
# the field is on the wire from day one so Phase 3 adds no protocol
# change). Reading it is not writing it — it stays 0 through all of M2,
# which is the Phase 2.1 criterion, and there is a test asserting exactly
# that.
#
# `updated_at` is set explicitly because the model's `onupdate=func.now()`
# is a SQLAlchemy ORM-level hook and does not fire for raw SQL.
def _dequeue_sql(type_filter: str) -> str:
    return f"""
    WITH claimed AS (
        SELECT id
        FROM tasks
        WHERE status = 'QUEUED'{type_filter}
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
              t.correlation_id, t.assigned_at, t.attempt_count
    """


_DEQUEUE_SQL = text(_dequeue_sql(""))

# Phase 2.3 eligibility filter: claim only types this worker said it can
# run. A separate statement rather than a `(:all OR ...)` predicate in the
# one above, so the unfiltered path Step 2.2 measured keeps exactly the
# plan it was measured with.
#
# `expanding=True` renders a real `IN (:t1, :t2, ...)` list at execution
# time. That is deliberate over `= ANY(:types)`: passing a Python list as
# a single array parameter through `text()` leaves the parameter's type
# ambiguous to asyncpg, and expanding sidesteps the question entirely.
_DEQUEUE_SQL_TYPED = text(_dequeue_sql(" AND task_type IN :task_types")).bindparams(
    bindparam("task_types", expanding=True)
)


async def dequeue(
    session: AsyncSession,
    *,
    worker_id: uuid.UUID | str,
    limit: int = DEFAULT_DEQUEUE_LIMIT,
    max_batch: int,
    task_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Claim up to `limit` queued tasks for `worker_id`. Does not commit.

    Returns the claimed rows, newest claim state included. An empty list
    means the queue held nothing claimable — either genuinely empty, every
    candidate row was locked by a concurrent dequeuer, or (with
    `task_types` set) nothing queued matches. The caller cannot
    distinguish those, and does not need to: all mean "no work for you
    right now".

    `task_types` is the Phase 2.3 eligibility filter — claim only types
    the worker declared it can run. `None` means no filter, which is the
    Step 2.2 primitive's behaviour and what `POST /tasks/dequeue` still
    does. An **empty list is not the same as `None`**: it means "eligible
    for nothing" and is rejected rather than silently widened to
    everything, because a worker that declared only unrecognised types
    must not quietly become eligible for all of them.

    This is a queue primitive, not an assignment decision. The caller
    names the worker. *Choosing* which worker should get work — capability
    filtering, credit accounting, the push over the socket — is the
    assignment engine in Step 2.3, which calls this.

    **Cost note, honest (§10):** the filter is applied inside the index
    scan's ordered walk, not by a dedicated index. `ix_tasks_queue` is
    `(status, priority, created_at)`, so a worker eligible for a rare type
    behind a long run of ineligible ones pays for the rows it steps over.
    Measured in Step 2.3 rather than assumed; see the PHASE_STATE row. A
    `(status, task_type, priority, created_at)` index is the fix if a real
    workload needs it, and is deliberately not added on speculation.
    """
    if limit < 1:
        raise QueueLimitExceeded("limit must be at least 1")
    if limit > max_batch:
        raise QueueLimitExceeded(f"limit {limit} exceeds the {max_batch}-task dequeue cap")

    params: dict[str, Any] = {"limit": limit, "worker_id": str(worker_id)}
    if task_types is None:
        statement = _DEQUEUE_SQL
    else:
        if not task_types:
            raise QueueLimitExceeded("task_types is empty: the worker is eligible for nothing")
        statement = _DEQUEUE_SQL_TYPED
        params["task_types"] = list(task_types)

    result = await session.execute(statement, params)
    return [dict(row) for row in result.mappings().all()]


# Outcomes of a worker-reported state move. Returned rather than raised
# because none of them is exceptional: a worker is untrusted (§12), so
# "that task is not yours" and "that task is already finished" are ordinary
# inputs the coordinator must answer without dropping the connection.
TRANSITIONED = "transitioned"
NOOP = "noop"
NOT_FOUND = "not_found"
NOT_OWNER = "not_owner"
ILLEGAL = "illegal"
# Phase 2.5. Distinct from NOOP because the caller must treat them
# differently: a duplicate result submission is a **success** from the
# worker's point of view — the task is completed, so the worker should stop
# retrying — whereas NOOP is a state report that changed nothing.
DUPLICATE = "duplicate"


async def mark_status(
    session: AsyncSession,
    *,
    task_id: str,
    worker_id: uuid.UUID | str,
    new_status: str,
) -> str:
    """Move one task to `new_status` on behalf of the worker holding it.

    Phase 2.4's only new write path. Every guard here exists because the
    request comes from a worker, and **every worker is untrusted** (§12):

    * The id must parse as a UUID. A worker can send anything.
    * The row is locked `FOR UPDATE` before it is read, so two messages
      about the same task cannot interleave.
    * `assigned_worker_id` must be the worker reporting. Without this check
      any worker could move any other worker's task — the same class of
      defect Step 2.2.1 fixed on the admin surface.
    * The move goes through `task_states.check_transition`, so the state
      machine stays the single authority. A same-state report is a no-op
      rather than an error, which is what makes duplicate reports harmless
      (§3.7).

    Writes `status` and `updated_at`, and nothing else. In particular it
    does not touch `lease_expires_at` or `attempt_count` — a Phase 2.1 exit
    criterion holds those untouched through all of M2 — and it does not
    stamp `completed_at`, even on `FAILED`: that column belongs to Step
    2.5's completion path, and `updated_at` already records when the
    transition happened.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return NOT_FOUND

    row = (
        await session.execute(
            text(
                "SELECT status, assigned_worker_id FROM tasks "
                "WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": str(parsed)},
        )
    ).mappings().one_or_none()
    if row is None:
        return NOT_FOUND
    if str(row["assigned_worker_id"]) != str(worker_id):
        return NOT_OWNER

    try:
        should_write = check_transition(row["status"], new_status)
    except InvalidTaskTransition:
        return ILLEGAL
    if not should_write:
        return NOOP

    await session.execute(
        text(
            "UPDATE tasks SET status = :status, updated_at = now() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"status": new_status, "id": str(parsed)},
    )
    return TRANSITIONED


async def complete_task(
    session: AsyncSession,
    *,
    envelope: dict[str, Any],
    worker_id: uuid.UUID | str,
) -> str:
    """Persist a result and complete its task, in one transaction (Phase 2.5).

    Does not commit — the caller owns the transaction boundary, exactly as
    `dequeue` and `mark_status` do.

    **Both writes or neither.** The result row and the task's move to
    `COMPLETED` happen inside the same `FOR UPDATE` lock, so there is no
    window in which a task is completed with no result, or a result exists
    that nothing points at. That is the whole reason this is one function
    and not `mark_status` plus a separate insert.

    **Idempotency is structural, not clamped** (§3.7). The row is locked
    before its status is read; a task already `COMPLETED` returns
    `DUPLICATE` having written nothing at all — not a second result row, not
    a re-stamped `completed_at`. Two coordinator replicas processing the
    same retried submission serialise on that lock, so the second one sees
    `COMPLETED` and stops. The `idempotency_token` in the envelope is
    therefore **recorded, not enforced** in M2: the task's own terminal
    state is what makes a duplicate a no-op, and the token is what lets
    Phase 3 tell a retry apart from a genuinely second attempt.

    **A missing `RUNNING` is walked through, not widened.** A result can
    arrive for a task still `ASSIGNED` — the `task_started` report is a
    separate message and can be lost to a socket that died between the two.
    Rather than adding `ASSIGNED -> COMPLETED` to the state machine (which
    would make "completed without ever running" legal everywhere, forever,
    to serve one lossy edge), the task is moved through `RUNNING` first.
    Two legal transitions, no new edge, and the audit trail still says the
    task ran.
    """
    task_id = str(envelope["task_id"])
    try:
        parsed = uuid.UUID(task_id)
    except (ValueError, AttributeError):
        return NOT_FOUND

    row = (
        await session.execute(
            text(
                "SELECT status, assigned_worker_id FROM tasks "
                "WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": str(parsed)},
        )
    ).mappings().one_or_none()
    if row is None:
        return NOT_FOUND
    if str(row["assigned_worker_id"]) != str(worker_id):
        return NOT_OWNER

    current = row["status"]
    if current == COMPLETED:
        return DUPLICATE

    # ASSIGNED needs the RUNNING hop; RUNNING goes straight through. Every
    # step is checked, so an already-terminal task (FAILED, CANCELLED) is
    # refused here rather than silently overwritten.
    path = [RUNNING, COMPLETED] if current == ASSIGNED else [COMPLETED]
    state = current
    try:
        for target in path:
            check_transition(state, target)
            state = target
    except InvalidTaskTransition:
        return ILLEGAL

    result_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO task_results (id, payload, size_bytes, submitted_at) "
            "VALUES (CAST(:id AS uuid), CAST(:payload AS jsonb), :size_bytes, now())"
        ),
        {
            "id": str(result_id),
            "payload": json.dumps(envelope),
            "size_bytes": int(envelope["size_bytes"]),
        },
    )
    # `completed_at` is stamped here and nowhere else — Step 2.4 deliberately
    # left it alone, including on failure, because it means "this task
    # produced a result", not "this task stopped moving".
    await session.execute(
        text(
            "UPDATE tasks SET status = :status, result_id = CAST(:result_id AS uuid), "
            "completed_at = now(), updated_at = now() WHERE id = CAST(:id AS uuid)"
        ),
        {"status": COMPLETED, "result_id": str(result_id), "id": str(parsed)},
    )
    return TRANSITIONED


async def get_task(session: AsyncSession, task_id: str) -> dict[str, Any] | None:
    """One task with its result envelope, or None (Phase 2.5).

    The minimum read that makes "execution duration recorded and visible"
    verifiable rather than asserted. It is a **primitive**, in the same
    sense `POST /tasks/dequeue` was kept as one in Step 2.2: Step 2.6 owns
    the operator task API with filtering, batch views and full lifecycle
    history, and will build on this rather than around it.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return None

    row = (
        await session.execute(
            text(
                "SELECT t.id, t.task_type, t.status, t.priority, t.assigned_worker_id, "
                "t.assigned_at, t.completed_at, t.created_at, t.updated_at, "
                "t.attempt_count, t.correlation_id, t.result_id, "
                "r.payload AS result_payload, r.size_bytes AS result_size_bytes, "
                "r.submitted_at AS result_submitted_at "
                "FROM tasks t LEFT JOIN task_results r ON r.id = t.result_id "
                "WHERE t.id = CAST(:id AS uuid)"
            ),
            {"id": str(parsed)},
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def purge_expired_results(session: AsyncSession, *, retention_days: int) -> int:
    """Delete result **bodies** past the retention period. Does not commit.

    Returns how many rows went. Only `task_results` is touched: the `tasks`
    row survives as the permanent audit trail and its `result_id` becomes
    NULL through the existing `ON DELETE SET NULL`, which is why retention
    needs no second write and no application-side bookkeeping.

    `retention_days <= 0` disables the sweep and deletes nothing, so
    switching retention off is a configuration change rather than a code
    path that has to be remembered.

    Every replica runs this. The DELETE is idempotent and row-scoped, so
    concurrent sweeps race harmlessly — no leader election, consistent with
    §3.9 and with how the assignment engine already runs per replica.
    """
    if retention_days <= 0:
        return 0
    result = await session.execute(
        text(
            "DELETE FROM task_results "
            "WHERE submitted_at < now() - make_interval(days => :days)"
        ),
        {"days": retention_days},
    )
    return int(result.rowcount or 0)


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
