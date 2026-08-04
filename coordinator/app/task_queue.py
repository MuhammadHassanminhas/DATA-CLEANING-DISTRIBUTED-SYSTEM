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

**Phase 3.1 adds the lease engine to this module**, and it is deliberately
here rather than in a module of its own: a lease is a column on the queue
row, written by the same statements that already move the row, so putting
it elsewhere would mean two modules writing `tasks` and a second place to
look when a task is in the wrong state.

    dequeue          -> lease_expires_at = now() + ack timeout
    mark_status(RUNNING)
                     -> deadline_at = now() + execution cap
                        lease_expires_at = LEAST(now() + ttl, deadline_at)
    renew_lease      -> lease_expires_at = LEAST(now() + ttl, deadline_at)
    shorten_worker_leases (socket close)
                     -> lease_expires_at = LEAST(it, now() + grace)
    renew_worker_leases (hello)
                     -> every non-terminal row this worker holds
    terminal state   -> lease_expires_at = NULL, deadline_at = NULL
    reclaim_expired_leases
                     -> ASSIGNED/RUNNING -> QUEUED, attempt_count + 1,
                        excluded_worker_id + not_before (Phase 3.2 retry),
                        or -> FAILED once the attempts are exhausted

**One recovery trigger: an expired lease in Postgres.** No other signal
reclaims a task — not a closed socket, not `OFFLINE`, not a missed
heartbeat, not a stalled progress figure. A close is *coordinator-
observed*, so it shortens the lease, which accelerates the one mechanism
instead of adding a second one that could disagree with it. The reasoning
is in the Phase 3.0 gate (§3.0.2) and it is worth restating in one line:
`_sweep_heartbeats` declares a worker `OFFLINE` when its Redis key is
missing, so an `OFFLINE`-driven reclaim would reassign the entire
in-flight fleet on a Redis blip — the recovery system causing the outage.

**The requeue transition is real, and it is `-> QUEUED`, not
`-> REASSIGNED`.** This module's pre-M3 docstring said otherwise, and the
gate named the contradiction rather than resolving it quietly: the queue
is `WHERE status = 'QUEUED'`, so a recovered task must actually be
`QUEUED` to be claimable. `REASSIGNED` is an *attempt* outcome. See
`app.task_states`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    task_ack_timeout_seconds,
    task_lease_ttl_seconds,
    task_max_attempts,
    task_max_execution_seconds_default,
    task_retry_backoff_base_seconds,
    task_retry_backoff_factor,
    task_retry_backoff_max_seconds,
    task_retry_exclusion_seconds,
)
from app.models import Task
from app.task_policies import max_attempts, max_execution_seconds, policy_seconds
from app.task_states import (
    ASSIGNED,
    CANCELLED,
    COMPLETED,
    FAILED,
    QUEUED,
    REASSIGNED,
    RUNNING,
    TASK_STATES,
    InvalidTaskTransition,
    UnknownTaskState,
    check_transition,
    is_terminal,
)
from app.task_types import TASK_TYPES, UnknownTaskType, validate_parameters

# Recommendations, not measured values (CLAUDE.md §10). They bound how
# much work one call can ask for so a single request cannot monopolise a
# connection or a transaction. Step 2.8's harness is what produces
# defensible numbers; revise there.
DEFAULT_DEQUEUE_LIMIT = 1
# Phase 2.6. Page size when an operator does not ask for one. Small enough
# to be a readable answer in a terminal, large enough that the common
# "what happened to my last batch" question is one call. The hard ceiling
# is configuration (`task_list_max_limit`), not this.
DEFAULT_LIST_LIMIT = 50


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
# Phase 3.1's reclaim, checked the same way and for the same reason. The
# reclaimer expresses both moves in one SQL statement, where
# `check_transition` cannot run per row.
for _reclaim_from in (ASSIGNED, RUNNING):
    for _reclaim_to in (QUEUED, FAILED):
        # `-> FAILED` is Phase 3.2's exhaustion branch: the same statement
        # that requeues a retryable task ends a task that has no attempt
        # left, so both targets are checked here.
        if not check_transition(_reclaim_from, _reclaim_to):
            raise RuntimeError(
                f"the lease reclaimer performs {_reclaim_from} -> {_reclaim_to}, "
                "which task_states no longer allows"
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
#
# **Phase 3.1 adds the acknowledgement lease to this same statement.** A
# task is durably `ASSIGNED` *with an expiry* before a byte reaches the
# worker, which is what closes the gap Step 2.3's commit-before-send
# deliberately left open: a task recorded as ASSIGNED whose delivery never
# arrived was visible and unrecoverable, and now it simply expires.
#
# The timeout is resolved per row against `task_policies` rather than being
# a single bound parameter, because one dequeue claims several task types
# at once — a per-type policy that only worked when a worker supported one
# type would be a policy in name only. See `app.task_policies` for why the
# lookup is in SQL and not in a process-side cache.
#
# **`deadline_at` is set here as well as at `task_started`, and that is a
# Step 3.1 decision the gate's lifecycle table did not call for.** It was
# found by a live run, not by review. The shipped worker ignores a
# re-delivery of a task id it is already executing
# (`task_assign_duplicate_ignored`, an M2 behaviour), so after a reclaim
# hands a task back to the same worker there is no second `task_started` —
# and with the deadline set only there, that task's lease could be renewed
# forever by progress messages from an execution that may be hung. The
# uncapped renewal is precisely what `deadline_at` exists to prevent, so
# the cap now starts at delivery and `task_started` re-stamps it from the
# real start. Setting it twice costs nothing: both statements were already
# writing this row.
#
# The cost of the earlier stamp is that the cap includes the delivery and
# acknowledgement round trip, which is bounded by the ack timeout — so the
# effective execution budget is the type's cap minus at most ~30s, and it
# is re-stamped in full the moment the task genuinely starts.
#
# **Phase 3.2 adds the two retry-eligibility conditions.** A task that has
# just been reclaimed is `QUEUED` but not yet claimable: `not_before`
# holds it for its backoff, and `excluded_worker_id` keeps it away from
# the worker that just lost it. Both live in the same predicate as the
# status test so they are applied inside the one ordered index walk — the
# same cost note as the 2.3 type filter, and for the same reason.
#
# The exclusion **expires**, and that is the gate's decision rather than an
# oversight (§3.0.6): a permanent one starves the task to death on a
# single-worker fleet, which is exactly the shape of a laptop demo and of
# one Internet worker on a hotspot. Retrying eventually on the same worker
# beats never retrying at all.
_ELIGIBLE = """
          AND (not_before IS NULL OR not_before <= now())
          AND (excluded_worker_id IS NULL
               OR excluded_worker_id <> CAST(:worker_id AS uuid)
               OR not_before + make_interval(secs => :exclusion_window) <= now())"""


def _dequeue_sql(type_filter: str) -> str:
    return f"""
    WITH claimed AS (
        SELECT id
        FROM tasks
        WHERE status = 'QUEUED'{type_filter}{_ELIGIBLE}
        ORDER BY priority, created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE tasks AS t
    SET status = 'ASSIGNED',
        assigned_worker_id = CAST(:worker_id AS uuid),
        assigned_at = now(),
        lease_expires_at = now() + make_interval(
            secs => {policy_seconds("ack_timeout_seconds", "ack_timeout")}),
        deadline_at = now() + make_interval(secs => {max_execution_seconds()}),
        updated_at = now()
    FROM claimed
    WHERE t.id = claimed.id
    RETURNING t.id, t.task_type, t.parameters, t.payload, t.priority,
              t.correlation_id, t.assigned_at, t.attempt_count, t.lease_expires_at
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

    params: dict[str, Any] = {
        "limit": limit,
        "worker_id": str(worker_id),
        "ack_timeout": task_ack_timeout_seconds(),
        "max_execution_fallback": task_max_execution_seconds_default(),
        # Phase 3.2. How long the worker that lost a task stays ineligible
        # for it, measured from `not_before`.
        "exclusion_window": task_retry_exclusion_seconds(),
    }
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
# Phase 2.6. The task exists but is past the point where a coordinator-side
# write alone can cancel it — see `cancel_queued_task`. Distinct from
# ILLEGAL because nothing was attempted: this is a refusal, not a rejected
# transition, and the caller is told the status that caused it.
NOT_CANCELLABLE = "not_cancellable"


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

    Writes `status` and `updated_at`, plus `started_at` on the move to
    `RUNNING` (Phase 2.6 — inside this same statement, so the hot path gains
    no extra round trip). It does not stamp `completed_at`, even on
    `FAILED`: that column belongs to Step 2.5's completion path, and
    `updated_at` already records when the transition happened.

    **Phase 3.1 folds the lease into the same statement**, in both
    directions and with no extra round trip either way:

    * `-> RUNNING` sets `deadline_at` — the coordinator-set execution cap
      no worker renewal can push past — and the first execution lease,
      `LEAST(now() + ttl, deadline_at)`. The ack lease written at dequeue
      is replaced here rather than extended, because the two measure
      different things: one bounds delivery, the other bounds execution.
    * a **terminal** state clears both columns. Strictly this is belt and
      braces — the reclaimer's `WHERE status IN ('ASSIGNED','RUNNING')`
      already cannot match a terminal row — but leaving a stale expiry on
      a finished task means every later reader has to know that, and it
      keeps the partial index off rows that will never be reclaimed.
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

    params: dict[str, Any] = {"status": new_status, "id": str(parsed)}
    if new_status == RUNNING:
        # Phase 2.6: the RUNNING transition also stamps when it happened. Set
        # unconditionally rather than with COALESCE — a task can only reach
        # this branch from ASSIGNED (a same-state report is the NOOP above),
        # and now that Phase 3 makes a second attempt possible, the start of
        # *this* attempt is the useful answer.
        deadline = (
            "now() + make_interval(secs => "
            f"{max_execution_seconds(table_alias='tasks')})"
        )
        ttl = (
            "now() + make_interval(secs => "
            f"{policy_seconds('lease_ttl_seconds', 'lease_ttl', table_alias='tasks')})"
        )
        # Re-stamped from the real start, replacing the conservative
        # delivery-time stamp the claim wrote (see `_dequeue_sql`).
        extra = (
            f", started_at = now(), deadline_at = {deadline}, "
            f"lease_expires_at = LEAST({ttl}, {deadline})"
        )
        params["lease_ttl"] = task_lease_ttl_seconds()
        params["max_execution_fallback"] = task_max_execution_seconds_default()
    elif is_terminal(new_status):
        # Phase 3.2 clears the retry columns here too, for the reason the
        # lease columns are cleared: a finished task that still names an
        # excluded worker and a retry instant is a row every later reader
        # has to know to ignore — and the task console would render "retry
        # after ..." on a task that is never going to be retried.
        extra = (
            ", lease_expires_at = NULL, deadline_at = NULL, "
            "not_before = NULL, excluded_worker_id = NULL"
        )
    else:
        extra = ""

    await session.execute(
        text(
            f"UPDATE tasks SET status = :status, updated_at = now(){extra} "
            "WHERE id = CAST(:id AS uuid)"
        ),
        params,
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
    #
    # `started_at` is COALESCEd rather than set (Phase 2.6): when the path
    # above walked ASSIGNED -> RUNNING, the `task_started` report was lost and
    # nothing stamped it, so now is the closest true answer available. When
    # the report did arrive, the real start time is already there and must not
    # be overwritten with the completion time.
    #
    # Phase 3.1 clears the lease and the deadline here, in the statement
    # that was already completing the task. A completed task cannot be
    # reclaimed regardless — the reclaimer only looks at `ASSIGNED` and
    # `RUNNING` — so this is about leaving the row honest rather than about
    # safety: a finished task with a future expiry on it is a fact nobody
    # should have to interpret.
    await session.execute(
        text(
            "UPDATE tasks SET status = :status, result_id = CAST(:result_id AS uuid), "
            "completed_at = now(), updated_at = now(), "
            # Phase 3.1 cleared the lease pair here; Phase 3.2 clears the
            # retry pair with it, so a completed task carries no state about
            # an attempt that will never happen.
            "lease_expires_at = NULL, deadline_at = NULL, "
            "not_before = NULL, excluded_worker_id = NULL, "
            "started_at = COALESCE(started_at, now()) WHERE id = CAST(:id AS uuid)"
        ),
        {"status": COMPLETED, "result_id": str(result_id), "id": str(parsed)},
    )
    return TRANSITIONED


# ---------------------------------------------------------------------------
# Lease engine (Phase 3.1)
# ---------------------------------------------------------------------------

# Every statement below caps its renewal at `deadline_at`, expressed as
# `LEAST(now() + ttl, COALESCE(deadline_at, 'infinity'))`. The COALESCE is
# what makes a task that has been delivered but has not yet reported
# `task_started` renewable — it has no deadline yet, because the execution
# clock has not begun.
_RENEWAL_TARGET = (
    "LEAST(now() + make_interval(secs => :lease_ttl), "
    "COALESCE(deadline_at, 'infinity'::timestamptz))"
)

_LIVE = "status IN ('ASSIGNED', 'RUNNING')"


async def renew_lease(
    session: AsyncSession, *, task_id: str, worker_id: uuid.UUID | str
) -> bool:
    """Push one task's lease out by a TTL. Returns whether a row was written.

    Called when the coordinator observes **any** message naming the task —
    an ack, a start, a progress sample, a result attempt. Note what the
    input is and is not: the *arrival* of a message renews the lease, and
    its *contents* decide nothing. A worker reporting 99% progress forever
    renews exactly as much as one reporting 1%, and no more (gate §3.0.6).

    Three guards, and each is load-bearing (§12 — a worker is untrusted
    input):

    * `assigned_worker_id` must be the sender, so a worker cannot extend
      another worker's lease and keep its task alive;
    * the task must still be live, so a message about a finished task
      cannot resurrect an expiry;
    * the new value is capped at `deadline_at`, so renewal can postpone a
      reclaim but can never prevent one. That cap is what makes it safe
      for the renewal trigger to be worker-driven at all.

    **When to call it is decided by the caller, not here.** The replica
    holding the socket tracks when each of its tasks is next due and skips
    the call entirely otherwise, so an undue renewal costs no database
    round trip — not merely no write. See `assignment._lease_due`.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return False

    result = await session.execute(
        text(
            f"UPDATE tasks SET lease_expires_at = {_RENEWAL_TARGET}, updated_at = now() "
            f"WHERE id = CAST(:id AS uuid) AND assigned_worker_id = CAST(:worker_id AS uuid) "
            f"AND {_LIVE}"
        ),
        {
            "id": str(parsed),
            "worker_id": str(worker_id),
            "lease_ttl": task_lease_ttl_seconds(),
        },
    )
    return bool(result.rowcount)


async def renew_worker_leases(
    session: AsyncSession, *, worker_id: uuid.UUID | str
) -> list[str]:
    """Renew every live lease held by one worker. Returns the task ids.

    This is the reconnect path (gate §3.0.3 row 10). A worker whose socket
    died and came back is still executing the work it had; renewing on
    `hello` is what makes a clean reconnect inside the disconnect grace
    lose nothing at all.

    **The task ids come from the database, never from the worker.** The
    `hello` message says "I am worker X" and nothing more is believed: the
    coordinator asks its own tables which tasks X holds. A worker that
    declared a list of task ids here could keep another worker's task
    alive, or claim work it was never given.

    They are **returned** rather than merely counted because the caller
    needs them: the reconnecting session was not the one these tasks were
    delivered to, so it holds no credit key for any of them, and without
    this list every subsequent progress message about them would be
    discarded as "not this session's" — leaving a healthy worker's running
    task to be reclaimed one lease later. This is the only place those ids
    can come from that is not the worker's own word for it.
    """
    result = await session.execute(
        text(
            f"UPDATE tasks SET lease_expires_at = {_RENEWAL_TARGET}, updated_at = now() "
            f"WHERE assigned_worker_id = CAST(:worker_id AS uuid) AND {_LIVE} "
            "RETURNING id"
        ),
        {"worker_id": str(worker_id), "lease_ttl": task_lease_ttl_seconds()},
    )
    return [str(row[0]) for row in result.all()]


async def shorten_worker_leases(
    session: AsyncSession, *, worker_id: uuid.UUID | str, grace_seconds: int
) -> int:
    """Cut every live lease this worker holds to at most `grace_seconds`.

    Called by the replica that **personally observed** the socket close.
    That distinction is the whole reason this is safe: it can only affect
    tasks belonging to a worker whose connection this process was holding,
    so it cannot storm the way an `OFFLINE`-driven reclaim could.

    It **shortens and never extends** — `LEAST` against the current value —
    so a task already inside the grace window keeps its earlier expiry. A
    close observed twice therefore changes nothing the second time.

    This is an accelerator, not a second recovery trigger: the reclaimer
    still does the reclaiming, and a close that is never observed (a power
    cut, a severed link) simply falls back to the full lease TTL.
    """
    result = await session.execute(
        text(
            "UPDATE tasks SET lease_expires_at = LEAST("
            "  COALESCE(lease_expires_at, 'infinity'::timestamptz), "
            "  now() + make_interval(secs => :grace)), updated_at = now() "
            f"WHERE assigned_worker_id = CAST(:worker_id AS uuid) AND {_LIVE}"
        ),
        {"worker_id": str(worker_id), "grace": grace_seconds},
    )
    return int(result.rowcount or 0)


# The reclaimer. One loop per replica, no leader election — the same shape
# as the shipped retention sweep and the assignment engine, for the same
# §3.9 reason.
#
# **Double-reclaim is impossible for exactly the reason double-assignment
# is**, and this is deliberately the same primitive rather than a new one
# to be proven from scratch: a second replica's `SKIP LOCKED` steps over
# the row the first has locked, and by the time that lock lifts the row is
# `QUEUED` and no longer matches. Step 2.2 measured that at 10,000 claims
# with 0 duplicates across three AKS pods.
#
# `previous_worker_id` and `expired_at` come out of the CTE rather than the
# UPDATE's `RETURNING`, because `RETURNING` reports the **new** row: by
# then the worker is NULL and the lease is cleared. Reading them from the
# subquery is what lets the log line name who lost the task and by how
# much the lease was overdue.
#
# **Phase 3.2 adds the retry policy to this same statement**, deliberately
# rather than as a second pass over the rows it just wrote. Whether a task
# is retried or given up on is a function of one number the statement is
# already reading (`attempt_count`) and one row-level lookup it can do in
# the same walk (the type's `max_attempts`), so splitting it would mean two
# statements, a second lock acquisition and a window in which a task is
# requeued but not yet backed off — visible to a dequeue.
#
# `t.attempt_count` inside `SET` is the **old** value; the incremented one
# is what `RETURNING` reports. So the exhaustion test is written
# `t.attempt_count + 1 >= max_attempts`, and `attempt_count` is zero-based
# (a first delivery is `attempt: 0`), which makes `max_attempts = 3` mean
# executions 0, 1, 2.
#
# The backoff is **full jitter** — `random() * min(max, base * factor^n)` —
# computed here rather than in Python for the same reason the policy lookup
# is: one statement, one clock, and every replica agreeing without
# coordination. Postgres evaluates `random()` per row, so a hundred tasks
# reclaimed together get a hundred different delays, which is the whole
# point of jitter.
_RETRY_BACKOFF = (
    "random() * LEAST(:backoff_max, "
    ":backoff_base * power(:backoff_factor, t.attempt_count))"
)


def _reclaim_sql() -> str:
    exhausted = f"t.attempt_count + 1 >= {max_attempts()}"
    return f"""
    WITH expired AS (
        SELECT id, assigned_worker_id AS previous_worker_id,
               lease_expires_at AS expired_at, attempt_count AS previous_attempt,
               status AS previous_status,
               -- Which clock ran out. A lease that was capped by the
               -- execution deadline is the gate's taxonomy row 7 (a worker
               -- slower than its type's cap), not row 1 (a worker that
               -- stopped answering), and the attempt row has to be able to
               -- say which. Evaluated against Postgres's clock, in the same
               -- statement that reads the column, so no coordinator's clock
               -- skew can relabel it.
               (deadline_at IS NOT NULL AND deadline_at <= now()) AS deadline_exceeded
        FROM tasks
        WHERE status IN ('ASSIGNED', 'RUNNING')
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at < now()
        ORDER BY lease_expires_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    UPDATE tasks AS t
    SET status = CASE WHEN {exhausted} THEN 'FAILED' ELSE 'QUEUED' END,
        assigned_worker_id = NULL,
        assigned_at = NULL,
        started_at = NULL,
        lease_expires_at = NULL,
        deadline_at = NULL,
        attempt_count = t.attempt_count + 1,
        -- Both retry columns are cleared on the terminal branch: a FAILED
        -- task is never claimed again, so a backoff or an exclusion left on
        -- it would be a fact about a task that no longer has a future.
        excluded_worker_id = CASE
            WHEN {exhausted} THEN NULL ELSE expired.previous_worker_id END,
        not_before = CASE
            WHEN {exhausted} THEN NULL
            ELSE now() + make_interval(secs => {_RETRY_BACKOFF}) END,
        updated_at = now()
    FROM expired
    WHERE t.id = expired.id
    RETURNING t.id, t.task_type, t.correlation_id, t.attempt_count, t.status,
              t.not_before, expired.previous_worker_id, expired.previous_status,
              expired.expired_at, expired.deadline_exceeded
    """


_RECORD_EXPIRY_SQL = text(
    """
    INSERT INTO task_attempts
        (id, task_id, attempt_number, worker_id, outcome, reason, correlation_id)
    VALUES (gen_random_uuid(), CAST(:task_id AS uuid), :attempt_number,
            CAST(:worker_id AS uuid), :outcome, :reason, :correlation_id)
    """
)

# What an attempt row's `outcome` says, and what its `reason` says, kept
# apart on purpose: **`outcome` is what happened to the task, `reason` is
# why this attempt ended.** A task given up on after its execution cap was
# exceeded is `FAILED` / `execution_deadline_exceeded`, and nothing has to
# encode both facts in one string. "Exhausted" is not a reason code either
# — it is `attempt_count = max_attempts` on the task, which is derivable
# and does not go stale if the policy changes.
#
# Both outcomes come from `task_states` rather than being spelled here:
# `REASSIGNED` is the constant that module reserved for exactly this — an
# attempt that was superseded, never a task status — and `FAILED` is the
# state the task actually reached.
REASSIGNED_OUTCOME = REASSIGNED
EXHAUSTED_OUTCOME = FAILED
REASON_LEASE_EXPIRED = "lease_expired"
REASON_DEADLINE_EXCEEDED = "execution_deadline_exceeded"


async def reclaim_expired_leases(
    session: AsyncSession, *, batch: int
) -> list[dict[str, Any]]:
    """Return every task whose lease has run out to the queue. Does not commit.

    Returns one entry per reclaimed task, so the caller can log and count
    each individually — "42 tasks were reclaimed" is not a recovery
    timeline, and §11 asks for every task to be traceable by its own
    correlation id.

    **Phase 3.2 makes this the retry engine as well.** Each expired task
    takes one of two branches, decided by its type's `max_attempts`:

    * **retry** — `QUEUED`, `attempt_count + 1`, the worker that lost it in
      `excluded_worker_id`, and `not_before` set to a full-jitter backoff.
      The task is visible in the queue immediately and claimable once the
      backoff has passed.
    * **exhausted** — terminal `FAILED`, both retry columns cleared. Never
      retried again and never silently dropped (gate §3.0.4 item 2): the
      row stays, its result-less history stays, and the attempt rows that
      explain it stay.

    Either way one `task_attempts` row is written recording who lost the
    task, which attempt it was, and which clock ran out. `status` on the
    returned row is the branch that was taken, so a caller can log,
    count and cancel without asking again.

    `assigned_at` and `started_at` are cleared with the lease. Leaving them
    would put a `QUEUED` row in the database claiming to have started
    running at a particular time, which is not a small untidiness: it is
    what `GET /tasks/{id}`'s timeline is reconstructed from, so the task
    would report a start that belongs to an attempt that no longer exists.
    The superseded attempt is preserved in `task_attempts` instead.
    """
    if batch < 1:
        raise QueueLimitExceeded("batch must be at least 1")

    reclaimed = [
        dict(row)
        for row in (
            await session.execute(
                text(_reclaim_sql()),
                {
                    "batch": batch,
                    "max_attempts": task_max_attempts(),
                    "backoff_base": task_retry_backoff_base_seconds(),
                    "backoff_factor": task_retry_backoff_factor(),
                    "backoff_max": task_retry_backoff_max_seconds(),
                },
            )
        )
        .mappings()
        .all()
    ]
    for row in reclaimed:
        await session.execute(
            _RECORD_EXPIRY_SQL,
            {
                "task_id": str(row["id"]),
                # The attempt that ended, not the one about to start:
                # `attempt_count` in the RETURNING is already incremented.
                "attempt_number": int(row["attempt_count"]) - 1,
                "worker_id": (
                    str(row["previous_worker_id"]) if row["previous_worker_id"] else None
                ),
                "outcome": (
                    EXHAUSTED_OUTCOME if row["status"] == FAILED else REASSIGNED_OUTCOME
                ),
                "reason": (
                    REASON_DEADLINE_EXCEEDED
                    if row["deadline_exceeded"]
                    else REASON_LEASE_EXPIRED
                ),
                "correlation_id": row["correlation_id"],
            },
        )
    return reclaimed


async def expired_lease_count(session: AsyncSession) -> int:
    """How many tasks are overdue for reclaim right now.

    The backlog figure the gate's third alert watches — a reclaimer that
    has died shows up as this number climbing and never draining, which is
    the failure the other two alerts cannot see. Answered from the partial
    index `ix_tasks_lease_expiry`, so it costs the number of *overdue*
    rows rather than the size of the table.
    """
    result = await session.execute(
        text(
            "SELECT count(*) FROM tasks "
            f"WHERE {_LIVE} AND lease_expires_at IS NOT NULL AND lease_expires_at < now()"
        )
    )
    return int(result.scalar_one())


async def awaiting_retry_count(session: AsyncSession) -> int:
    """How many queued tasks are waiting out a retry backoff right now.

    Separate from `queue_depth` on purpose: those tasks *are* queued, so
    they are in the depth figure, but none of them is claimable yet. A
    queue that reads 40 deep with nothing being assigned looks like a stuck
    scheduler until this number says otherwise, which is precisely the
    question an operator asks during a recovery.
    """
    result = await session.execute(
        text(
            "SELECT count(*) FROM tasks "
            "WHERE status = 'QUEUED' AND not_before IS NOT NULL AND not_before > now()"
        )
    )
    return int(result.scalar_one())


async def task_attempts(
    session: AsyncSession, *, task_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The abnormal endings recorded for one task, oldest first (Phase 3.2).

    This is what makes "failed tasks are inspectable, never silently
    dropped" checkable rather than asserted: a task that reached `FAILED`
    by exhausting its attempts has one row here per attempt, each naming
    the worker that held it and which clock ran out.

    Bounded, because `attempt_number` is bounded by policy but the table is
    not: `max_attempts` can be raised, and a caller must not be able to ask
    for an unbounded list by naming a task. Read from
    `ix_task_attempts_task`, the index this table was created with.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return []

    result = await session.execute(
        text(
            "SELECT attempt_number, worker_id, outcome, reason, correlation_id, recorded_at "
            "FROM task_attempts WHERE task_id = CAST(:id AS uuid) "
            "ORDER BY recorded_at, attempt_number LIMIT :limit"
        ),
        {"id": str(parsed), "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def worker_failure_counts(session: AsyncSession) -> list[dict[str, Any]]:
    """Abnormal attempt endings per worker, by outcome (Phase 3.2).

    The per-worker failure counters the gate promised, and the reason they
    are **counted from `task_attempts` rather than accumulated in a
    column**: a counter on `workers` would be a second write on the
    recovery path and, worse, an in-memory-style number that no longer
    agrees with the rows once anything is deleted. This is derived, so it
    cannot drift from the history it describes.

    Deliberately **not** merged into `GET /workers`, which every open
    dashboard polls every 2s (§6): this is a grouped scan over a table that
    grows with every abnormal ending, and putting it on the live fleet read
    would make the common case pay for the rare one.

    Rows whose worker has since been deleted are excluded — their
    `worker_id` is `NULL` by `ON DELETE SET NULL`, and "failures belonging
    to nobody" is not a per-worker counter.
    """
    result = await session.execute(
        text(
            "SELECT worker_id, outcome, count(*) AS failures, max(recorded_at) AS last_failure_at "
            "FROM task_attempts WHERE worker_id IS NOT NULL "
            "GROUP BY worker_id, outcome ORDER BY failures DESC"
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def get_task(session: AsyncSession, task_id: str) -> dict[str, Any] | None:
    """One task with its result envelope, or None (Phase 2.5).

    The minimum read that makes "execution duration recorded and visible"
    verifiable rather than asserted, and since Phase 2.6 the row the
    operator API's lifecycle timeline is built from — which is why
    `started_at` is selected here.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return None

    row = (
        await session.execute(
            text(
                "SELECT t.id, t.task_type, t.status, t.priority, t.assigned_worker_id, "
                "t.assigned_at, t.started_at, t.completed_at, t.created_at, t.updated_at, "
                "t.attempt_count, t.correlation_id, t.result_id, "
                # Phase 3.1: the lease is what an operator watching a
                # recovery actually needs to see — how long this attempt
                # has left before the coordinator takes the task back.
                "t.lease_expires_at, t.deadline_at, "
                # Phase 3.2: why a queued task is not being picked up.
                "t.not_before, t.excluded_worker_id, "
                "r.payload AS result_payload, r.size_bytes AS result_size_bytes, "
                "r.submitted_at AS result_submitted_at "
                "FROM tasks t LEFT JOIN task_results r ON r.id = t.result_id "
                "WHERE t.id = CAST(:id AS uuid)"
            ),
            {"id": str(parsed)},
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def list_tasks(
    session: AsyncSession,
    *,
    statuses: list[str] | None = None,
    task_type: str | None = None,
    worker_id: uuid.UUID | str | None = None,
    correlation_id: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    max_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """List tasks newest-first, filtered (Phase 2.6).

    Returns `(rows, has_more)`.

    **No total count is returned, and that is a decision rather than an
    omission.** A filtered `COUNT(*)` over `tasks` costs more than the page
    it describes, and `tasks` grows for the lifetime of the system by design
    (Decision #79) — so a "3 of 412,905" header would make every listing pay
    for a number nobody acts on. `has_more` answers the only question a
    paging caller actually has, and it costs one extra row: the query asks
    for `limit + 1` and reports whether it got it.

    **The result body is deliberately not joined in.** A page of 200 results
    at the 128 KB cap is 25 MB of response for a listing. `has_result` says
    whether one exists, and `GET /tasks/{id}` fetches it. That also keeps
    this query off `task_results` entirely.

    **Order is fixed at `created_at DESC, id DESC`**, not caller-selectable.
    `id` breaks ties so a page boundary cannot repeat or skip a row when
    several tasks share a creation timestamp — which a bulk enqueue
    guarantees, since one multi-row INSERT stamps them all identically.

    Filters are validated against the same authorities the rest of the
    system uses — `task_states.TASK_STATES` and the task-type registry — so
    an unknown value is refused rather than silently matching nothing. An
    operator who typos a status must be told, not shown an empty list.
    """
    if limit < 1:
        raise QueueLimitExceeded("limit must be at least 1")
    if limit > max_limit:
        raise QueueLimitExceeded(f"limit {limit} exceeds the {max_limit}-task listing cap")
    if offset < 0:
        raise QueueLimitExceeded("offset must not be negative")

    query = select(
        Task.id,
        Task.task_type,
        Task.status,
        Task.priority,
        Task.assigned_worker_id,
        Task.assigned_at,
        Task.started_at,
        Task.completed_at,
        Task.created_at,
        Task.updated_at,
        Task.attempt_count,
        Task.correlation_id,
        Task.result_id,
        # Phase 3.1. Two more columns off the same row the listing already
        # reads — no join, no second query, and it is what makes a
        # reassignment watchable in the task console (§6).
        Task.lease_expires_at,
        Task.deadline_at,
        # Phase 3.2. Same row again, no join: a queued task that is waiting
        # out a retry backoff is otherwise indistinguishable on a listing
        # from one nobody is eligible for.
        Task.not_before,
        Task.excluded_worker_id,
    )

    if statuses:
        for state in statuses:
            if state not in TASK_STATES:
                raise UnknownTaskState(f"unknown task state: {state!r}")
        query = query.where(Task.status.in_(statuses))
    if task_type is not None:
        if task_type not in TASK_TYPES:
            raise UnknownTaskType(f"unknown task type: {task_type!r}")
        query = query.where(Task.task_type == task_type)
    if worker_id is not None:
        query = query.where(Task.assigned_worker_id == worker_id)
    if correlation_id is not None:
        query = query.where(Task.correlation_id == correlation_id)

    query = query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1).offset(offset)

    rows = [dict(row) for row in (await session.execute(query)).mappings().all()]
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def cancel_queued_task(session: AsyncSession, *, task_id: str) -> tuple[str, str | None]:
    """Cancel a task that is still waiting in the queue (Phase 2.6).

    Returns `(outcome, current_status)`. Does not commit.

    **Only `QUEUED` is cancellable in M2**, and the restriction is
    deliberate. `task_states` also permits `ASSIGNED -> CANCELLED` and
    `RUNNING -> CANCELLED`, but a coordinator-side write is not a
    cancellation: the worker holding that task keeps executing, keeps its
    credit, and eventually submits a result for a task the database calls
    terminal — which `complete_task` would then refuse as `ILLEGAL`, losing
    real work and stranding the credit. Cancelling in-flight work needs a
    wire message and a worker-side cancel path (the executors already carry
    the cooperative flag, Decision #94). That is not in Step 2.6's scope, so
    an in-flight task is refused with its current status rather than
    half-cancelled.

    **The race against the assignment engine is settled by the row lock, not
    by timing.** This locks the row `FOR UPDATE` before reading its status;
    `dequeue` claims with `FOR UPDATE SKIP LOCKED`. Whichever gets there
    first wins cleanly — a dequeue in flight steps over a row being
    cancelled, and a cancel arriving mid-dequeue blocks, then sees
    `ASSIGNED` and refuses. There is no window in which a task is both
    cancelled and handed out.

    `completed_at` is not stamped: it means "this task produced a result"
    (Step 2.5), which a cancelled task did not. `updated_at` records when
    the cancellation happened, and nothing writes the row afterwards.
    """
    try:
        parsed = uuid.UUID(str(task_id))
    except (ValueError, AttributeError):
        return NOT_FOUND, None

    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"),
            {"id": str(parsed)},
        )
    ).mappings().one_or_none()
    if row is None:
        return NOT_FOUND, None

    current = row["status"]
    if current != QUEUED:
        return NOT_CANCELLABLE, current

    check_transition(QUEUED, CANCELLED)
    await session.execute(
        text(
            "UPDATE tasks SET status = :status, updated_at = now() "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"status": CANCELLED, "id": str(parsed)},
    )
    return TRANSITIONED, current


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


async def completions_per_minute(
    session: AsyncSession, *, minutes: int, max_minutes: int
) -> list[dict[str, Any]]:
    """Completed tasks per minute over the last `minutes` (Phase 2.7).

    Returns one entry per minute in the window, oldest first, **including
    the minutes in which nothing completed** — a chart with the empty
    buckets missing draws a busy fleet and an idle one identically.

    The count comes from `tasks.completed_at`, which `complete_task` stamps
    and nothing else does, so a bucket here is the same set of rows
    `GET /tasks?status=COMPLETED` would list over that minute. That is what
    makes the chart checkable against the API rather than merely plausible,
    and it is how Step 2.7's "throughput chart matches measured reality"
    criterion is verified.

    **A `FAILED` task is not in here.** `completed_at` means "produced a
    result", not "stopped moving" (see `mark_status`), so failures never
    enter the series at all rather than entering it as zero-value
    completions. Failure counts belong to the lifecycle totals
    (`counts_by_status`), which is where the dashboard reads them.

    The window is generated by Postgres rather than by Python so the bucket
    boundaries come from the same clock the rows were stamped with — a
    coordinator and a browser in different time zones, or with a drifting
    clock, must not produce different charts.
    """
    if minutes < 1:
        raise QueueLimitExceeded("minutes must be at least 1")
    if minutes > max_minutes:
        raise QueueLimitExceeded(f"window of {minutes} minutes exceeds the {max_minutes} cap")

    result = await session.execute(
        text(
            """
            SELECT bucket, COALESCE(hits.n, 0) AS completed
            FROM generate_series(
                     date_trunc('minute', now()) - make_interval(mins => :minutes - 1),
                     date_trunc('minute', now()),
                     interval '1 minute'
                 ) AS bucket
            LEFT JOIN (
                SELECT date_trunc('minute', completed_at) AS minute, count(*) AS n
                FROM tasks
                WHERE completed_at >= date_trunc('minute', now())
                                      - make_interval(mins => :minutes - 1)
                GROUP BY 1
            ) AS hits ON hits.minute = bucket
            ORDER BY bucket
            """
        ),
        {"minutes": minutes},
    )
    return [
        {"minute": row["bucket"].isoformat(), "completed": int(row["completed"])}
        for row in result.mappings().all()
    ]
