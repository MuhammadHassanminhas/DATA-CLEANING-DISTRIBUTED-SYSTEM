"""Worker, Task, and TaskResult tables.

Columns match the Phase 1.2 spec exactly: identity, hashed credentials,
registration metadata, agent version, lifecycle timestamps, status, and
a revocation flag. Task schema arrives in Phase 2.1, below.

`status` is a free-text column, not a DB-level enum/CHECK constraint —
the state machine (REGISTERED -> CONNECTING -> ONLINE -> SUSPECT ->
OFFLINE, plus QUARANTINED) is enforced application-side starting Phase
1.6: every write to this column goes through a single transition
helper in `app/main.py` that logs `worker_state_transition` with the
trigger, rather than being set ad hoc at multiple call sites. Actual
issuance of `credential_hash` happens in Phase 1.3 (registration) and
1.4 (auth) — the column exists now so the schema doesn't change under
those phases.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credential_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    registration_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="REGISTERED")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TaskResult(Base):
    """Result bodies, kept out of the `tasks` row on purpose.

    Under Decision #79 the `tasks` table *is* the queue, so every dequeue
    scans it; a fat JSONB result column would bloat those pages and slow
    the hot path. Decision #81 therefore puts bodies here and leaves
    `Task.result_id` as the "result reference" the Phase 2.1 spec calls
    for.

    The link is one-directional — `Task.result_id` points here and this
    table does not point back. That avoids a circular foreign key, and it
    makes "at most one result per task" structural rather than a
    constraint that has to be enforced and tested (CLAUDE.md §3.7).

    Retention (recommended 7 days for bodies, Decision #81) deletes rows
    here while the task row survives as the audit trail; the reference is
    `ON DELETE SET NULL` so that stays consistent.

    Writing to this table is Step 2.5. It exists now so 2.5 needs no
    migration.
    """

    __tablename__ = "task_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Recorded rather than derived so the recommended 64 KB cap can be
    # enforced and audited without re-serialising the body.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Task(Base):
    """Task table (Phase 2.1) — also the queue itself (Decision #79).

    `status` is free-text for the same reason `Worker.status` is: the
    state machine is enforced application-side, in `app/task_states.py`,
    where every transition goes through one checked write path. A DB-level
    enum would have to be migrated for the Phase 3 `REASSIGNED`
    transition, which is exactly what this phase is meant to avoid.

    `priority` is **lower-is-more-urgent** (like Unix nice), default 0.
    That is deliberate: it lets the dequeue scan read
    `ORDER BY priority, created_at` in one index direction, so the
    composite index Decision #79 requires is used directly instead of
    needing a mixed-direction index.

    `payload` and `parameters` are both present because the Phase 2.1
    spec lists both. They are not redundant: `parameters` holds the
    validated, normalised output of `task_types.validate_parameters` and
    is what the coordinator reasons about, while `payload` is the opaque
    body handed through to the worker uninterpreted.

    `lease_expires_at` and `attempt_count` were **written by nothing in
    M2** — a Phase 2.1 exit criterion, and the reason they were created
    early enough that Phase 3 needed no schema change for them. **Phase
    3.1 is what finally writes both.**
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="QUEUED")

    assigned_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 2.6. Stamped on the `ASSIGNED -> RUNNING` transition, inside the
    # UPDATE that already performs it. It exists because the transition was
    # otherwise unrecoverable once the task finished: it wrote `updated_at`,
    # and completion overwrote that. NULL for tasks that predate migration
    # 0004, and for tasks that never started.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 3.1. **The single recovery trigger for the whole milestone.**
    # A timestamp in Postgres, compared against Postgres's own clock by a
    # query that touches no Redis at all — which is why a Redis outage
    # degrades the fleet display and changes nothing about task recovery
    # (gate §3.0.2). Written on assignment, renewed on any observed message
    # about the task, shortened when the worker's socket closes, and
    # cleared when the task reaches a terminal state.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 3.1. The cap that makes worker-driven renewal safe (§12). Set
    # by the coordinator when the task starts, never by a worker and never
    # extended by one: a worker that renews forever still loses the task at
    # its type's execution cap, because every renewal is
    # `LEAST(now() + ttl, deadline_at)`.
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 3.1: incremented by the reclaimer, so `attempt` on the wire
    # finally means something. Zero-based — a first delivery is attempt 0
    # (`assignment._deliver`), so `max_attempts = 3` means executions
    # numbered 0, 1, 2.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Phase 3.2 columns, created by migration 0006 with the rest of the
    # milestone's schema (one migration for M3, gate §3.0.7) and **written
    # by nothing in 3.1**. Named here so 3.2 adds behaviour rather than a
    # second migration: `excluded_worker_id` is the worker that just lost
    # the task, `not_before` is one column with one meaning — this row is
    # not eligible yet — serving both the retry backoff and the bounded
    # exclusion window.
    excluded_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_results.id", ondelete="SET NULL"),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Not nullable: every task is traceable end to end by one id from the
    # moment it is created (CLAUDE.md §11, and a 2.1 exit criterion).
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TaskAttempt(Base):
    """One attempt that did **not** end normally (Phase 3, migration 0006).

    The recovery timeline: who held the task, which attempt it was, and
    why it ended. Step 3.1 writes exactly one kind of row — `EXPIRED`, from
    the lease reclaimer — and Step 3.2 adds the rest (`REASSIGNED`,
    `FAILED` on attempt exhaustion) plus Step 3.4's `FENCED`.

    **Only abnormal endings are recorded, and that is a decision** (gate
    §3.0.7). A row per attempt always would add a second write to the hot
    assignment path — on a pipeline measured at 92–112% of one core (#141)
    — to record that nothing went wrong, when a healthy task's timeline is
    already fully derivable from `created_at / assigned_at / started_at /
    completed_at`. The happy path keeps its single write.

    `worker_id` is `ON DELETE SET NULL` rather than cascading: the record
    that an attempt expired must survive the worker row being deleted, or
    the history quietly rewrites itself.
    """

    __tablename__ = "task_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # Which attempt this row is about — the value `tasks.attempt_count`
    # held while the attempt was running, so it lines up with the `attempt`
    # field the worker was handed on the wire.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    # `EXPIRED` | `REASSIGNED` | `FENCED` | `FAILED` — free text for the
    # same reason `Task.status` is: the vocabulary grows across M3's steps
    # and a DB enum would need a migration for each addition.
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    # A short machine-readable code (`lease_expired`, `stranded_pre_m3`,
    # `execution_deadline_exceeded`). **Never a traceback and never payload
    # data** — the same rule `handle_task_failed` already follows (§12).
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaskPolicy(Base):
    """Per-task-type timeout overrides (Phase 3.1, migration 0006).

    This table is what makes "timeouts configurable per task type without
    a redeploy" true rather than aspirational. **A missing row is the
    normal case**: the defaults live in code
    (`config.task_ack_timeout_seconds`, `config.task_lease_ttl_seconds`,
    `task_types.DEFAULT_MAX_EXECUTION_SECONDS`) and a row here overrides
    them per column, so an operator who wants only a longer execution cap
    sets one field and inherits the rest.

    **Postgres rather than env vars, Redis or a ConfigMap** (gate
    §3.0.10). Env vars need a restart, so the criterion would be false by
    construction. Redis is ephemeral by contract (§4) and a flush would
    silently revert operator intent. A ConfigMap needs a reload path that
    does not exist and would not cover the Docker Compose environment.

    The lease-writing statements resolve a policy with a correlated
    subquery against this table rather than caching it in the process, so
    a change takes effect on the next task rather than after a cache TTL,
    and every replica agrees without coordination. The table holds at most
    one row per task type — four today — so the lookup is a primary-key
    probe on a table that fits in a single page.
    """

    __tablename__ = "task_policies"

    task_type: Mapped[str] = mapped_column(String, primary_key=True)
    ack_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_execution_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Written by nothing in 3.1 — the attempt cap is Step 3.2's policy. The
    # column ships now because M3 gets one migration.
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
