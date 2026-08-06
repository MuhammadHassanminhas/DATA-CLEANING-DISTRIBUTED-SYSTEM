"""Durable task queue tests (Phase 2.2), against a real Postgres.

Runs in CI where GitHub Actions `services:` provides one; skipped locally
unless POSTGRES_HOST is set. There is deliberately no in-memory or SQLite
stand-in: the entire mechanism under test is `FOR UPDATE SKIP LOCKED`
semantics across concurrent transactions, which only Postgres has. A test
that faked it would prove nothing.

Each test builds its own engine and disposes it, rather than reusing
`app.db.engine` — that engine is created at import and binds its pool to
the first event loop that touches it, and every test here runs its own
loop via `asyncio.run`.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "task queue tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import database_url  # noqa: E402
from app.task_queue import (  # noqa: E402
    QueueLimitExceeded,
    counts_by_status,
    dequeue,
    enqueue,
    enqueue_batch,
    queue_depth,
)
from app.task_types import InvalidTaskParameters, UnknownTaskType  # noqa: E402

MAX_DEQUEUE = 100
MAX_ENQUEUE = 10_000


@pytest.fixture(scope="module", autouse=True)
def migrated():
    """Bring the schema to head once for this module.

    alembic.ini's `script_location = migrations` resolves against the cwd,
    and the app always runs with cwd=coordinator/, so match that.
    """
    from alembic import command
    from alembic.config import Config

    coordinator_dir = Path(__file__).resolve().parent.parent / "coordinator"
    prev_cwd = os.getcwd()
    os.chdir(coordinator_dir)
    try:
        command.upgrade(Config(str(coordinator_dir / "alembic.ini")), "head")
    finally:
        os.chdir(prev_cwd)


def run(body):
    """Run `body(sessionmaker)` on a fresh engine and event loop."""

    async def _main():
        engine = create_async_engine(database_url())
        try:
            return await body(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_main())


async def _reset(sessionmaker) -> uuid.UUID:
    """Empty the queue and return a worker id claims can point at.

    `tasks.assigned_worker_id` is a real foreign key, so a dequeue needs a
    real worker row. Truncating `tasks` between tests keeps counts exact
    without depending on what other test modules left behind in the shared
    CI database.
    """
    async with sessionmaker() as session:
        await session.execute(text("TRUNCATE tasks CASCADE"))
        worker_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO workers (id, status, revoked, created_at, updated_at) "
                "VALUES (CAST(:id AS uuid), 'REGISTERED', false, now(), now())"
            ),
            {"id": str(worker_id)},
        )
        await session.commit()
    return worker_id


# --------------------------------------------------------------------------
# Enqueue: validation and durability
# --------------------------------------------------------------------------


def test_enqueue_validates_parameters_before_touching_the_database():
    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            with pytest.raises(UnknownTaskType):
                await enqueue(session, task_type="not_a_type", correlation_id="c")
            with pytest.raises(InvalidTaskParameters):
                await enqueue(
                    session,
                    task_type="count_to_n",
                    parameters={"n": -1},
                    correlation_id="c",
                )
            await session.rollback()

        # Nothing reached the table.
        async with sessionmaker() as session:
            assert await queue_depth(session) == 0

    run(body)


def test_enqueued_task_lands_queued_with_its_correlation_id():
    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            task_id = await enqueue(
                session,
                task_type="hash_rounds",
                parameters={"rounds": 5},
                correlation_id="corr-abc",
                priority=3,
            )
            await session.commit()

        async with sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, priority, correlation_id, parameters, "
                        "assigned_worker_id, assigned_at, lease_expires_at, attempt_count "
                        "FROM tasks WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(task_id)},
                )
            ).mappings().one()

        assert row["status"] == "QUEUED"
        assert row["priority"] == 3
        assert row["correlation_id"] == "corr-abc"
        # The stored form is the *normalised* one — the registry's default
        # for `algorithm` is filled in, not left absent.
        assert row["parameters"] == {"rounds": 5, "algorithm": "sha256"}
        assert row["assigned_worker_id"] is None
        assert row["assigned_at"] is None

    run(body)


def test_enqueue_batch_respects_its_cap():
    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            with pytest.raises(QueueLimitExceeded):
                await enqueue_batch(
                    session,
                    task_type="sleep",
                    parameters={"seconds": 1},
                    correlation_id="c",
                    count=MAX_ENQUEUE + 1,
                    max_batch=MAX_ENQUEUE,
                )
            await session.rollback()

    run(body)


# --------------------------------------------------------------------------
# Dequeue: the claim, and what it must not touch
# --------------------------------------------------------------------------


def test_dequeue_claims_the_task_and_stamps_an_acknowledgement_lease():
    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await enqueue(
                session, task_type="count_to_n", parameters={"n": 10}, correlation_id="c1"
            )
            await session.commit()

        async with sessionmaker() as session:
            claimed = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        assert len(claimed) == 1
        assert claimed[0]["task_type"] == "count_to_n"
        assert claimed[0]["correlation_id"] == "c1"

        async with sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status, assigned_worker_id, assigned_at, "
                        "lease_expires_at, deadline_at, attempt_count FROM tasks"
                    )
                )
            ).mappings().one()

        assert row["status"] == "ASSIGNED"
        assert row["assigned_worker_id"] == worker_id
        assert row["assigned_at"] is not None
        # Phase 3.1 reverses the M2 assertion that used to stand here
        # ("nothing writes `lease_expires_at`"). The claim now carries an
        # acknowledgement lease, written in the same statement, so a task
        # delivered to a worker that never answers is recoverable rather
        # than merely visible.
        assert row["lease_expires_at"] is not None
        # The execution cap is stamped at delivery too, not only at
        # `task_started`. A live run found the reason: the shipped worker
        # ignores a re-delivery of a task id it is already running, so a
        # reclaimed task handed back to the same worker never sends a
        # second `task_started` — and a deadline set only there would
        # leave that task's lease renewable forever.
        assert row["deadline_at"] is not None
        # `attempt_count` still starts at zero and is written by the
        # reclaimer alone — a first delivery is attempt 0 on the wire.
        assert row["attempt_count"] == 0

    run(body)


def test_dequeue_on_an_empty_queue_returns_nothing():
    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            assert await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE) == []
            await session.commit()

    run(body)


def test_an_assigned_task_is_never_handed_out_again():
    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await enqueue(session, task_type="sleep", parameters={"seconds": 1}, correlation_id="c")
            await session.commit()

        async with sessionmaker() as session:
            first = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        async with sessionmaker() as session:
            second = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        assert len(first) == 1
        assert second == []

    run(body)


def test_dequeue_rejects_a_batch_over_the_cap():
    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            with pytest.raises(QueueLimitExceeded):
                await dequeue(
                    session, worker_id=worker_id, limit=MAX_DEQUEUE + 1, max_batch=MAX_DEQUEUE
                )
            await session.rollback()

    run(body)


# --------------------------------------------------------------------------
# The ordering guarantee, exactly as stated in `task_queue`'s docstring
# --------------------------------------------------------------------------


def test_a_single_dequeuer_gets_strict_priority_then_fifo_order():
    """The stated guarantee: one dequeuer sees `(priority ASC, created_at
    ASC)`, and priority is lower-is-more-urgent.

    Each task is committed separately on purpose. `created_at` defaults to
    `now()`, which in Postgres is the *transaction* timestamp — tasks
    enqueued in a single transaction therefore share one `created_at` and
    have no defined order among themselves. That is the honest limit of
    the FIFO half of the guarantee, and committing separately here is what
    makes the test actually about ordering rather than about ties.
    """

    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)

        # (priority, label) in insertion order. Expected drain order is
        # priority ascending, then insertion order within a priority.
        spec = [(5, "e"), (1, "a"), (5, "f"), (0, "z"), (1, "b")]
        for priority, label in spec:
            async with sessionmaker() as session:
                await enqueue(
                    session,
                    task_type="count_to_n",
                    parameters={"n": 1},
                    correlation_id=label,
                    priority=priority,
                )
                await session.commit()

        drained = []
        while True:
            async with sessionmaker() as session:
                claimed = await dequeue(
                    session, worker_id=worker_id, limit=1, max_batch=MAX_DEQUEUE
                )
                await session.commit()
            if not claimed:
                break
            drained.append(claimed[0]["correlation_id"])

        assert drained == ["z", "a", "b", "e", "f"]

    run(body)


# --------------------------------------------------------------------------
# Concurrency: the property the whole design exists for
# --------------------------------------------------------------------------


def test_concurrent_dequeuers_never_double_assign_and_never_lose_a_task():
    """Exit criterion 2, at test scale.

    Each dequeuer holds its own session, so each runs in its own
    transaction on its own connection — which is what makes this a real
    test of `SKIP LOCKED` rather than of asyncio interleaving. The
    three-real-replicas version of this runs against AKS staging; see the
    Step 2.2 record in PHASE_STATE.md.
    """
    total = 600
    dequeuers = 8

    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await enqueue_batch(
                session,
                task_type="count_to_n",
                parameters={"n": 1},
                correlation_id="load",
                count=total,
                max_batch=MAX_ENQUEUE,
            )
            await session.commit()

        async def drain() -> list[str]:
            mine: list[str] = []
            while True:
                async with sessionmaker() as session:
                    claimed = await dequeue(
                        session, worker_id=worker_id, limit=7, max_batch=MAX_DEQUEUE
                    )
                    await session.commit()
                if not claimed:
                    return mine
                mine.extend(str(task["id"]) for task in claimed)

        per_dequeuer = await asyncio.gather(*(drain() for _ in range(dequeuers)))
        all_claimed = [task_id for batch in per_dequeuer for task_id in batch]

        # No task handed out twice.
        assert len(all_claimed) == len(set(all_claimed))
        # No task lost.
        assert len(all_claimed) == total

        async with sessionmaker() as session:
            assert await queue_depth(session) == 0
            assert await counts_by_status(session) == {"ASSIGNED": total}

        # The work genuinely spread — if one dequeuer took everything the
        # test would still pass the assertions above while proving nothing
        # about concurrency.
        assert sum(1 for batch in per_dequeuer if batch) > 1

    run(body)


# --------------------------------------------------------------------------
# Depth, and survival across a restart
# --------------------------------------------------------------------------


def test_queue_depth_counts_only_queued_tasks():
    async def body(sessionmaker):
        worker_id = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await enqueue_batch(
                session,
                task_type="count_to_n",
                parameters={"n": 1},
                correlation_id="c",
                count=20,
                max_batch=MAX_ENQUEUE,
            )
            await session.commit()

        async with sessionmaker() as session:
            assert await queue_depth(session) == 20
            await session.commit()

        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, limit=6, max_batch=MAX_DEQUEUE)
            await session.commit()

        async with sessionmaker() as session:
            assert await queue_depth(session) == 14
            assert await counts_by_status(session) == {"QUEUED": 14, "ASSIGNED": 6}

    run(body)


def test_queued_tasks_survive_every_connection_being_torn_down():
    """Exit criterion 4, at test scale.

    `run` builds an engine and disposes it, so the second half of this
    test reaches the queue over connections that did not exist when the
    tasks were written — the same thing a coordinator restart does to the
    database, minus the pod. The full three-replica restart is verified on
    AKS; see the Step 2.2 record in PHASE_STATE.md.
    """

    async def fill(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            await enqueue_batch(
                session,
                task_type="opaque_payload",
                parameters={"payload_b64": "aGVsbG8="},
                correlation_id="survive",
                count=25,
                max_batch=MAX_ENQUEUE,
            )
            await session.commit()

    async def check(sessionmaker):
        async with sessionmaker() as session:
            return await queue_depth(session)

    run(fill)
    assert run(check) == 25
