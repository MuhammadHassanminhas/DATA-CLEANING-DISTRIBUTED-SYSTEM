"""index tasks.completed_at for the throughput chart

Phase 2.7. One index, one reader: `task_queue.completions_per_minute`,
which backs `GET /tasks/throughput` and the dashboard's throughput chart.

That query is a bounded range scan — `WHERE completed_at >= now() -
interval` — grouped into per-minute buckets. Without an index the range
predicate cannot be answered from an index at all, so every chart refresh
reads the whole table. The window it displays is minutes wide; the table
it would scan grows for the lifetime of the system by design (Decision
#79). Those two facts diverging is the reason this index exists, and it is
the same reasoning that put `ix_task_results_submitted_at` in 0003 for the
retention sweep.

**Plain btree on `(completed_at)`, not partial and not composite.**

* A partial index (`WHERE completed_at IS NOT NULL`) was considered and
  rejected: `completed_at` is stamped by `complete_task` and nowhere else,
  so the NULL rows are exactly the in-flight ones plus anything that ended
  FAILED or CANCELLED. In a system that completes most of what it starts
  that is a small minority, so the index would be barely smaller while
  adding a predicate every planner decision has to match.
* No second column, because the query aggregates rather than sorts: it
  reads a contiguous range and counts. There is no tie group to break, so
  0004's `(created_at, id)` reasoning does not carry over here.

**Not measured (§10).** 0004's index shape was chosen from `EXPLAIN
ANALYZE` on 60,000 rows; this one is not. No throughput query has been run
against a large `tasks` table, so it is here because the access pattern is
known — a range scan over a monotonically growing table — not because a
slow chart was observed. If Step 2.8's harness produces a table large
enough to measure, that is where the number should come from.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_tasks_completed_at", "tasks", ["completed_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_completed_at", table_name="tasks")
