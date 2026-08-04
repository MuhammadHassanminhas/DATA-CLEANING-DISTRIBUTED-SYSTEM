"""lease, recovery and per-type policy schema for Milestone 3

**One migration for the whole milestone** (Phase 3.0 gate §3.0.7). Steps
3.2, 3.3 and 3.4 add behaviour on top of this schema and ship no migration
of their own, so the production rollout that carries M3 takes exactly one
schema change rather than four.

Five things, one concern each.

**`tasks.deadline_at`.** The cap that makes worker-driven lease renewal
safe. A renewal is always `LEAST(now() + ttl, deadline_at)`, so a worker
that renews forever still loses the task at its type's execution cap
(§12: nothing the scheduler trusts heavily may be worker-set). NULL until
the task starts, and NULL again once it is terminal.

**`tasks.excluded_worker_id` and `tasks.not_before`.** Step 3.2's retry
exclusion and backoff. **Written by nothing in Step 3.1** — they are here
because of the one-migration rule, not because 3.1 uses them.

**`ix_tasks_lease_expiry`, partial on `lease_expires_at`.** The
reclaimer's only scan. It is partial (`WHERE status IN ('ASSIGNED',
'RUNNING')`) for two reasons that both matter: it keeps the index to the
handful of rows that are actually in flight rather than every task the
system has ever run — `tasks` grows for the lifetime of the deployment by
design (Decision #79) — and it keeps the reclaimer off `ix_tasks_queue`,
which is the dequeue's hot path.

A partial index on a mutable predicate costs an index entry inserted when
a task is claimed and deleted when it completes, which is the same write
the row was already making. That is the honest cost and it is not a
measurement; the reclaimer's own scan is measured in the Step 3.1 record.

**`task_attempts`.** The recovery timeline: abnormal endings only. See
`app.models.TaskAttempt` for why not one row per attempt.

**`task_policies`.** Per-type timeout overrides, so "configurable per task
type without a redeploy" is a property of the running system rather than a
sentence in a design note. Ships **empty**: a missing row means the code
default applies, and that is the normal state.

**Backfill: pre-M3 stranded rows are closed as `FAILED`, not resurrected.**

Staging carries ~20,636 tasks stranded `ASSIGNED` since session 20, which
Decision #91 recorded as the designed outcome of commit-before-send with
no reclaimer yet in existence. They have `lease_expires_at IS NULL`,
because nothing had ever written that column.

Treating a NULL lease as "expired" would have been elegant and would have
dumped twenty thousand months-old tasks into the queue on the first
production rollout, at a coordinator whose measured ceiling is ~110–124
tasks/second (#141) — a self-inflicted thundering herd caused by the
recovery system on the day it shipped. Instead each row is marked terminal
with an attempt row explaining why, which is honest, inspectable, and
finally closes #91's carried item.

The backfill is scoped to `lease_expires_at IS NULL` precisely so it can
only ever touch rows that predate the lease engine. A task assigned by a
coordinator running this code always has a lease, so a rolling upgrade
cannot catch a live task in this net.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "excluded_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tasks_lease_expiry",
        "tasks",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('ASSIGNED', 'RUNNING')"),
    )

    op.create_table(
        "task_attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The read this table exists for: "show me everything that happened to
    # this task, oldest first". Step 3.7's timeline is exactly that query.
    op.create_index("ix_task_attempts_task", "task_attempts", ["task_id", "recorded_at"])
    # The per-worker failure counter the gate promises Step 3.7. Partial on
    # a non-NULL worker so the index does not carry the rows whose worker
    # has since been deleted.
    op.create_index(
        "ix_task_attempts_worker",
        "task_attempts",
        ["worker_id", "recorded_at"],
        postgresql_where=sa.text("worker_id IS NOT NULL"),
    )

    op.create_table(
        "task_policies",
        sa.Column("task_type", sa.String(), primary_key=True, nullable=False),
        sa.Column("ack_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("lease_ttl_seconds", sa.Integer(), nullable=True),
        sa.Column("max_execution_seconds", sa.Integer(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Backfill. Attempt rows first, so a row is never marked terminal
    # without the record of why — if this migration is interrupted between
    # the two statements the transaction rolls both back, but writing them
    # in this order means the failure mode of a future edit is a harmless
    # orphan explanation rather than an unexplained mass failure.
    op.execute(
        sa.text(
            """
            INSERT INTO task_attempts
                (id, task_id, attempt_number, worker_id, outcome, reason, correlation_id)
            SELECT gen_random_uuid(), id, attempt_count, assigned_worker_id,
                   'EXPIRED', 'stranded_pre_m3', correlation_id
            FROM tasks
            WHERE status IN ('ASSIGNED', 'RUNNING')
              AND lease_expires_at IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET status = 'FAILED', updated_at = now()
            WHERE status IN ('ASSIGNED', 'RUNNING')
              AND lease_expires_at IS NULL
            """
        )
    )


def downgrade() -> None:
    # The backfill is **not** reversed. Which rows were stranded before the
    # migration is recoverable from `task_attempts`, but that table is
    # dropped below, and reviving twenty thousand tasks into the queue is
    # exactly the outcome the forward migration exists to avoid — doing it
    # on a downgrade would be worse, not better, because a downgrade is
    # already an incident.
    op.drop_table("task_policies")
    op.drop_index("ix_task_attempts_worker", table_name="task_attempts")
    op.drop_index("ix_task_attempts_task", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_index("ix_tasks_lease_expiry", table_name="tasks")
    op.drop_column("tasks", "excluded_worker_id")
    op.drop_column("tasks", "not_before")
    op.drop_column("tasks", "deadline_at")
