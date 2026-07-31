"""record when a task started running, and index the operator listing

Phase 2.6. Two changes, one concern each.

**`tasks.started_at`.** The step's exit criterion is "task inspection
returns the full lifecycle with timestamps", and until now the moment a
task moved `ASSIGNED -> RUNNING` was recoverable only while it stayed
`RUNNING`: the transition wrote `updated_at`, and completion overwrote it.
So a finished task could say when it was created, assigned and completed,
but never when it began. Progress samples deliberately write nothing to
Postgres (Decision #94), so there was no second source to reconstruct it
from. One nullable column, written inside the UPDATE that already performs
the transition — no extra statement on the hot path.

**Backfill is deliberately not attempted.** Rows that predate this
migration keep `started_at` NULL and their timeline omits the RUNNING
entry rather than inventing one from `updated_at`, which for a completed
task is the completion time and would be a fabricated measurement (§10).

**`ix_tasks_created_at`, on `(created_at, id)`.** `GET /tasks` lists
newest-first and breaks ties on `id`, so the index carries both columns:
one multi-row INSERT stamps every row in a bulk enqueue with an identical
`created_at`, which makes a 10,000-row tie group the *normal* case rather
than a corner. Postgres walks an ascending index backwards, so no DESC
index is needed.

Measured on 60,000 rows, `EXPLAIN (ANALYZE)` on the endpoint's own query
(`ORDER BY created_at DESC, id DESC LIMIT 50`):

| index | plan | execution |
|---|---|---|
| none | Parallel Seq Scan + top-N sort | **20.172 ms** |
| `(created_at)` | Index Scan Backward + Incremental Sort over the tie group (10,001 rows read to return 50) | **4.516 ms** |
| `(created_at, id)` | Index Scan Backward, no sort node (50 rows read) | **0.096 ms** |

The single-column index still pays the tie group; the composite removes the
sort entirely. And the seq-scan cost grows with the table, which `tasks`
does forever by design (Decision #79) — that, rather than the millisecond
difference at this size, is the reason the index is here.

**No index for `correlation_id` or `assigned_worker_id`**, though the
operator API filters on both — migration 0002 already created
`ix_tasks_correlation_id` and `ix_tasks_assigned_worker_id`. That was found
by the first live run of this migration failing on a duplicate relation,
not by reading 0002 first, and it is recorded because the reverse mistake
(shipping a second index under a new name) is the one that would have gone
unnoticed.

**What it costs, measured rather than assumed (§10):** one more index is
one more entry written per inserted row, and the 10,000-task bulk enqueue
is the path that pays. Seven runs each after a discarded warm-up, same
process, same table state — enqueue of 10,000 tasks:

| index | median | min | max |
|---|---|---|---|
| none | 0.540s | 0.505 | 0.587 |
| `(created_at)` | 0.628s | 0.570 | 0.699 |
| `(created_at, id)` | 0.593s | 0.550 | 0.622 |

So roughly **+0.05s on a 10,000-task bulk enqueue**, and the gap between
the two index shapes is inside the run-to-run spread — the composite is not
being claimed as cheaper to write, only as not measurably dearer. Measured
on this laptop's Docker against Postgres 16; treat the absolute figures as
a property of that machine.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_created_at", "tasks", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_column("tasks", "started_at")
