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

    `lease_expires_at` and `attempt_count` are **written by nothing in
    M2**. They exist now so Phase 3's lease and retry engine needs no
    schema change — a Phase 2.1 exit criterion.
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

    # Phase 3 columns. Present, indexed nowhere, written by nothing in M2.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
