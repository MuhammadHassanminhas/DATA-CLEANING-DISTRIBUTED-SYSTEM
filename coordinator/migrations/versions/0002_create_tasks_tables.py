"""create tasks and task_results tables

Phase 2.1. Schema shape and the reasoning behind it are in
`app/models.py`; the design decisions behind it are PHASE_STATE.md
Decisions #79 (Postgres is the queue) and #81 (result bodies live in
their own table).

`task_results` is created first because `tasks.result_id` references it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="QUEUED"),
        sa.Column(
            "assigned_worker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        # Phase 3 columns — present now so the lease/retry engine needs no
        # schema change. Nothing in M2 writes them.
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_results.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # The dequeue scan, mandated by Decision #79. Column order matches
    # `WHERE status = 'QUEUED' ORDER BY priority, created_at`, and both
    # sorts ascend (priority is lower-is-more-urgent) so one index
    # direction serves the whole clause.
    op.create_index("ix_tasks_queue", "tasks", ["status", "priority", "created_at"])

    # "What is this worker running" — the dashboard's current-task column
    # (CLAUDE.md §6).
    op.create_index("ix_tasks_assigned_worker_id", "tasks", ["assigned_worker_id"])

    # End-to-end traceability by a single correlation id (§11).
    op.create_index("ix_tasks_correlation_id", "tasks", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_correlation_id", table_name="tasks")
    op.drop_index("ix_tasks_assigned_worker_id", table_name="tasks")
    op.drop_index("ix_tasks_queue", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("task_results")
