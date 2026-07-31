"""index task_results.submitted_at for the retention sweep

Phase 2.5. The only schema change the step needs, and it is an index
rather than a shape change: Phase 2.1 built `task_results` so that writing
results needed no migration, and it did — the envelope lives in the
existing JSONB `payload` column and its length in the existing
`size_bytes`.

What 2.1 could not foresee is the read pattern retention introduces.
`purge_expired_results` runs `DELETE ... WHERE submitted_at < now() -
interval`, on a schedule, forever. Without this index that is a sequential
scan of every result ever stored, every sweep, growing with the table
rather than with the number of expired rows — the same property the Step
2.2 `queue_depth` note is careful about for the queue.

**Recommendation, not a measurement (§10):** no sweep has been run against
a large `task_results` table, so the index is here because the access
pattern is known, not because a slow sweep was observed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_task_results_submitted_at", "task_results", ["submitted_at"])


def downgrade() -> None:
    op.drop_index("ix_task_results_submitted_at", table_name="task_results")
