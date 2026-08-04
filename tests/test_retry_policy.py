"""Reassignment and retry (Phase 3.2), against a real Postgres.

Postgres-gated for the same reasons `test_lease_engine.py` is, and one
more that is specific to this step: **the retry policy is a `CASE` inside
the reclaim statement**, the backoff is `random()` evaluated by Postgres
per row, and the eligibility rules are two predicates inside the dequeue's
index walk. Every one of those is SQL behaviour. A fake would be testing a
Python transcription of the thing rather than the thing.

**Clocks are written, never waited on.** A lease is expired by putting
`lease_expires_at` in the past, and a backoff is elapsed by putting
`not_before` in the past — exactly the states the clock would have reached.
That keeps the module under a second and it is why the timed proof of
those same bounds lives in the phase document's demo, measured against a
real worker, rather than being claimed here.
"""

import asyncio
import contextlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "retry policy tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import assignment  # noqa: E402
from app.assignment import (  # noqa: E402
    LocalSession,
    assign_once,
    reclaim_once,
    register_session,
    unregister_session,
)
from app.config import database_url, task_max_attempts  # noqa: E402
from app.task_policies import set_policy  # noqa: E402
from app.task_queue import (  # noqa: E402
    awaiting_retry_count,
    complete_task,
    dequeue,
    enqueue,
    mark_status,
    reclaim_expired_leases,
    task_attempts,
    worker_failure_counts,
)

MAX_DEQUEUE = 100
RECLAIM_BATCH = 100


@pytest.fixture(scope="module", autouse=True)
def migrated():
    """Bring the schema to head once for this module (see test_task_queue)."""
    from alembic import command
    from alembic.config import Config

    coordinator_dir = Path(__file__).resolve().parent.parent / "coordinator"
    prev_cwd = os.getcwd()
    os.chdir(coordinator_dir)
    try:
        command.upgrade(Config(str(coordinator_dir / "alembic.ini")), "head")
    finally:
        os.chdir(prev_cwd)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


def make_session(worker_id, *, max_concurrent: int = 4) -> LocalSession:
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=1,
        websocket=FakeSocket(),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=("count_to_n", "hash_rounds", "sleep", "opaque_payload"),
    )


def run(body):
    """Run `body(sessionmaker)` on a fresh engine and loop, with the
    assignment engine's session factory pointed at it."""

    async def _main():
        # The coordinator's Redis client is built once at import and its
        # pool caches connections bound to whichever loop created them
        # (`tests/conftest.py` has the full story). Each test here gets its
        # own loop, and one of them subscribes for real, so the pool is
        # emptied of the previous loop's connections rather than being
        # allowed to hand one of them to this one. `reset()` drops the
        # references without touching the dead transports, which is the
        # only safe order once that loop is closed.
        with contextlib.suppress(Exception):
            from app.redis_client import redis_client

            redis_client.connection_pool.reset()

        engine = create_async_engine(database_url())
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        @contextlib.asynccontextmanager
        async def _get_session():
            async with sessionmaker() as session:
                yield session

        original = assignment.get_session
        assignment.get_session = _get_session
        try:
            return await body(sessionmaker)
        finally:
            assignment.get_session = original
            await engine.dispose()

    return asyncio.run(_main())


async def _reset(sessionmaker, workers: int = 1) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(workers)]
    async with sessionmaker() as session:
        await session.execute(text("TRUNCATE tasks CASCADE"))
        await session.execute(text("DELETE FROM task_policies"))
        for worker_id in ids:
            await session.execute(
                text(
                    "INSERT INTO workers (id, status, revoked, created_at, updated_at) "
                    "VALUES (CAST(:id AS uuid), 'ONLINE', false, now(), now())"
                ),
                {"id": str(worker_id)},
            )
        await session.commit()
    return ids


async def _enqueue_one(sessionmaker, *, task_type: str = "count_to_n") -> uuid.UUID:
    async with sessionmaker() as session:
        task_id = await enqueue(
            session,
            task_type=task_type,
            parameters={"n": 5} if task_type == "count_to_n" else {"seconds": 1},
            correlation_id=f"corr-{uuid.uuid4()}",
        )
        await session.commit()
    return task_id


async def _row(sessionmaker, task_id) -> dict:
    async with sessionmaker() as session:
        return dict(
            (
                await session.execute(
                    text(
                        "SELECT status, assigned_worker_id, attempt_count, not_before, "
                        "excluded_worker_id, lease_expires_at, deadline_at, "
                        "not_before - now() AS backoff_left "
                        "FROM tasks WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(task_id)},
                )
            )
            .mappings()
            .one()
        )


async def _expire(sessionmaker, task_id, *, seconds_ago: int = 1) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET lease_expires_at = now() - make_interval(secs => :ago) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id), "ago": seconds_ago},
        )
        await session.commit()


async def _backoff_elapsed(sessionmaker, task_id, *, seconds_ago: int = 1) -> None:
    """Age the retry backoff. A value above `TASK_RETRY_EXCLUSION_SECONDS`
    also ages out the exclusion of the worker that lost the task."""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET not_before = now() - make_interval(secs => :ago) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id), "ago": seconds_ago},
        )
        await session.commit()


async def _park(sessionmaker, task_id) -> None:
    """Push a task's backoff an hour out, so it cannot be claimed while
    another task is the subject of the test. Deterministic where waiting on
    the real jitter would not be: a `random() * 5` backoff can land at 0.01s.
    """
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET not_before = now() + interval '1 hour' "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id)},
        )
        await session.commit()


async def _claim_and_expire(sessionmaker, task_id, worker_id) -> None:
    """One full failed attempt: claimed by `worker_id`, lease run out.

    Asserts the claim landed on the task the caller named — a dequeue takes
    the oldest eligible row, so a test with more than one claimable task
    would otherwise silently exercise a different one.
    """
    async with sessionmaker() as session:
        claimed = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
        await session.commit()
    assert [str(row["id"]) for row in claimed] == [str(task_id)]
    await _expire(sessionmaker, task_id)


# --------------------------------------------------------------------------
# Reassignment: the failed worker is excluded, and the exclusion expires
# --------------------------------------------------------------------------


def test_a_reclaim_records_the_backoff_and_the_worker_that_lost_the_task():
    """The two columns migration 0006 shipped for this step and Step 3.1
    left unwritten. `not_before` is in the future, `excluded_worker_id` is
    the worker that stopped answering."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, worker_id)

        async with sessionmaker() as session:
            reclaimed = await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        assert len(reclaimed) == 1
        assert reclaimed[0]["status"] == "QUEUED"

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "QUEUED"
        assert row["attempt_count"] == 1
        assert str(row["excluded_worker_id"]) == str(worker_id)
        # Full jitter on the first attempt: `random() * base`, so the only
        # thing assertable is the **ceiling**. Deliberately not a lower
        # bound — `random()` can return 0.0001, and a test that assumed the
        # delay was still in the future would be a flake that fires once a
        # month in CI and is blamed on the database.
        assert row["not_before"] is not None
        assert row["backoff_left"].total_seconds() <= 5.0

    run(body)


def test_the_worker_that_lost_the_task_does_not_get_it_straight_back():
    """The exit criterion, at the level that decides it: the dequeue
    predicate. A single-worker fleet asking for work is told there is none
    while the exclusion holds — and a *different* worker is given it."""

    async def body(sessionmaker):
        first, second = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, first)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()
        await _backoff_elapsed(sessionmaker, task_id)

        # The backoff has passed, but the exclusion has not.
        async with sessionmaker() as session:
            assert await dequeue(session, worker_id=first, max_batch=MAX_DEQUEUE) == []

        async with sessionmaker() as session:
            claimed = await dequeue(session, worker_id=second, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert len(claimed) == 1
        assert str(claimed[0]["id"]) == str(task_id)
        assert claimed[0]["attempt_count"] == 1

    run(body)


def test_the_exclusion_expires_so_a_one_worker_fleet_is_not_starved():
    """**Deliberate, and the gate says so** (§3.0.6): a permanent exclusion
    would starve a task to death on a single-worker fleet — a laptop demo,
    or one Internet worker on a hotspot. Retrying eventually on the same
    worker is better than never retrying."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, worker_id)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        # Inside the window: nothing for this worker.
        await _backoff_elapsed(sessionmaker, task_id)
        async with sessionmaker() as session:
            assert await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE) == []

        # Past it: the same worker may have it again.
        await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)
        async with sessionmaker() as session:
            claimed = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert len(claimed) == 1

    run(body)


def test_a_task_waiting_out_its_backoff_is_queued_but_not_claimable():
    """A recovered task is `QUEUED` immediately — the queue is
    `WHERE status = 'QUEUED'` and recovery has to land there — but it is
    not claimable until its backoff passes. Both halves matter: the first
    is what makes it visible, the second is what makes the retry a retry
    rather than an instant loop."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, worker_id)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        assert (await _row(sessionmaker, task_id))["status"] == "QUEUED"
        # Parked before the gauge is read, because the real backoff is
        # `random() * 5` and can elapse between the reclaim and this line.
        # The subject here is the gauge, not the size of the jitter.
        await _park(sessionmaker, task_id)
        async with sessionmaker() as session:
            # The gauge behind `coordinator_tasks_awaiting_retry`: this task
            # is inside the queue depth and none of it is claimable.
            assert await awaiting_retry_count(session) == 1

        await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)
        async with sessionmaker() as session:
            assert await awaiting_retry_count(session) == 0

    run(body)


def test_the_backoff_grows_with_the_attempt_number():
    """Exponential, and bounded on both ends. Full jitter means only the
    ceiling is assertable per attempt — so the test asserts the ceiling
    each time, and that the later attempt's ceiling is the larger one."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)

        ceilings = []
        for expected_ceiling in (5.0, 10.0):
            await _claim_and_expire(sessionmaker, task_id, worker_id)
            async with sessionmaker() as session:
                await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
                await session.commit()
            row = await _row(sessionmaker, task_id)
            assert row["backoff_left"].total_seconds() <= expected_ceiling
            ceilings.append(expected_ceiling)
            # Let the same worker take it again for the next round.
            await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)

        assert ceilings == [5.0, 10.0]

    run(body)


# --------------------------------------------------------------------------
# Exhaustion: a terminal FAILED that is kept, not dropped
# --------------------------------------------------------------------------


def test_a_task_that_keeps_losing_its_worker_ends_in_terminal_failed():
    """The poison-task criterion. `max_attempts = 3` means executions 0, 1
    and 2 — the third expiry is the one that ends it, and the task is
    `FAILED` with both retry columns cleared."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)

        for attempt in range(task_max_attempts()):
            await _claim_and_expire(sessionmaker, task_id, worker_id)
            async with sessionmaker() as session:
                await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
                await session.commit()
            row = await _row(sessionmaker, task_id)
            assert row["attempt_count"] == attempt + 1
            if row["status"] == "QUEUED":
                # Only a task with a future has a backoff to age out; doing
                # this on the terminal pass would write retry state back
                # onto a FAILED row and hide the assertion below.
                await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "FAILED"
        assert row["attempt_count"] == task_max_attempts()
        # A task with no future carries no retry state.
        assert row["not_before"] is None
        assert row["excluded_worker_id"] is None
        assert row["lease_expires_at"] is None
        assert row["deadline_at"] is None

        # And it is never handed out again.
        async with sessionmaker() as session:
            assert await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE) == []

    run(body)


def test_the_exhausted_task_keeps_one_inspectable_row_per_attempt():
    """Gate §3.0.4 item 2: terminal, kept visible and inspectable, never
    silently dropped. Three attempt rows, the last one `FAILED` and the
    earlier ones `REASSIGNED`, each naming the worker that held it."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)

        for _ in range(task_max_attempts()):
            await _claim_and_expire(sessionmaker, task_id, worker_id)
            async with sessionmaker() as session:
                await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
                await session.commit()
            await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)

        async with sessionmaker() as session:
            attempts = await task_attempts(session, task_id=str(task_id))

        assert [a["attempt_number"] for a in attempts] == [0, 1, 2]
        assert [a["outcome"] for a in attempts] == ["REASSIGNED", "REASSIGNED", "FAILED"]
        assert {str(a["worker_id"]) for a in attempts} == {str(worker_id)}
        assert {a["reason"] for a in attempts} == {"lease_expired"}

    run(body)


def test_a_per_type_attempt_cap_overrides_the_default_with_no_restart():
    """The cap is per task type and lives in `task_policies`, so a type
    that is expensive to re-run can be given one attempt. Nothing was
    restarted between the row being written and it taking effect."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(session, task_type="count_to_n", values={"max_attempts": 1})
            await session.commit()

        await _claim_and_expire(sessionmaker, task_id, worker_id)
        async with sessionmaker() as session:
            reclaimed = await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        # One attempt allowed, so the very first expiry is terminal.
        assert reclaimed[0]["status"] == "FAILED"
        assert (await _row(sessionmaker, task_id))["status"] == "FAILED"

    run(body)


def test_an_execution_deadline_is_recorded_as_a_different_reason():
    """Taxonomy row 7 is not row 1. A task killed by its type's execution
    cap and a task whose worker stopped answering are both lease expiries,
    and the attempt row has to say which — the first is a task that is too
    slow for its policy, the second is a machine that died."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker, task_type="sleep")
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        # The deadline is what ran out, and the lease with it — which is
        # exactly what `LEAST(now() + ttl, deadline_at)` produces once the
        # cap has passed.
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE tasks SET deadline_at = now() - interval '2 seconds', "
                    "lease_expires_at = now() - interval '1 second' "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(task_id)},
            )
            await session.commit()

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()
            attempts = await task_attempts(session, task_id=str(task_id))

        assert attempts[0]["reason"] == "execution_deadline_exceeded"
        assert attempts[0]["outcome"] == "REASSIGNED"

    run(body)


# --------------------------------------------------------------------------
# Per-worker failure counters
# --------------------------------------------------------------------------


def test_failure_counters_accumulate_per_worker_and_are_queryable():
    """The exit criterion, and the Phase 4 input. Derived from the attempt
    rows rather than accumulated in a column, so it cannot drift from the
    history it describes."""

    async def body(sessionmaker):
        busy, unlucky = await _reset(sessionmaker, workers=2)
        first = await _enqueue_one(sessionmaker)
        second = await _enqueue_one(sessionmaker)

        # `busy` loses two tasks, `unlucky` loses one. Each reclaimed task
        # is parked afterwards so exactly one row is claimable at a time —
        # otherwise the next dequeue could take the previous task back and
        # the test would count something it did not set up.
        await _park(sessionmaker, second)
        for task_id in (first, second):
            if task_id is second:
                await _backoff_elapsed(sessionmaker, second)
            await _claim_and_expire(sessionmaker, task_id, busy)
            async with sessionmaker() as session:
                await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
                await session.commit()
            await _park(sessionmaker, task_id)

        await _backoff_elapsed(sessionmaker, first, seconds_ago=120)
        await _claim_and_expire(sessionmaker, first, unlucky)
        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        async with sessionmaker() as session:
            counts = await worker_failure_counts(session)

        by_worker = {str(row["worker_id"]): int(row["failures"]) for row in counts}
        assert by_worker[str(busy)] == 2
        assert by_worker[str(unlucky)] == 1
        assert all(row["last_failure_at"] is not None for row in counts)

    run(body)


# --------------------------------------------------------------------------
# End to end, through the shipped code paths
# --------------------------------------------------------------------------


def test_a_lost_task_is_reassigned_to_a_different_worker_end_to_end():
    """**The first exit criterion, through the engine rather than the
    primitives**: worker A is given the task, stops answering, the
    reclaimer takes it back, and the next assignment pass hands it to
    worker B with the attempt number incremented on the wire."""

    async def body(sessionmaker):
        first, second = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)

        dead = make_session(first)
        register_session(dead)
        try:
            assert await assign_once() == 1
        finally:
            unregister_session(dead.worker_id, dead.session_epoch)

        await _expire(sessionmaker, task_id)
        assert await reclaim_once() == 1
        await _backoff_elapsed(sessionmaker, task_id)

        alive = make_session(second)
        register_session(alive)
        try:
            assert await assign_once() == 1
        finally:
            unregister_session(alive.worker_id, alive.session_epoch)

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "ASSIGNED"
        assert str(row["assigned_worker_id"]) == str(second)
        assert row["attempt_count"] == 1

        # The attempt number the second worker was handed, which it echoes
        # back in its result envelope.
        import json

        delivered = [json.loads(frame) for frame in alive.websocket.sent]  # type: ignore[attr-defined]
        assign_frames = [f for f in delivered if f["message_type"] == "task_assign"]
        assert assign_frames[0]["payload"]["attempt"] == 1

    run(body)


def test_the_reclaimer_asks_the_old_worker_to_stop_via_the_push_channel():
    """Best-effort `task_cancel`, published to `worker:{id}:push` rather
    than written to a socket — the replica that reclaims a task usually is
    not the one holding that worker's connection.

    Subscribed here with the real client, so what is asserted is a message
    that genuinely went through Redis, not a mocked call."""

    async def body(sessionmaker):
        import json

        from app.redis_client import redis_client

        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, worker_id)

        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"worker:{worker_id}:push")
        try:
            assert await reclaim_once() == 1
            envelope = None
            for _ in range(50):
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.2
                )
                if message and message["type"] == "message":
                    envelope = json.loads(message["data"])
                    break
            assert envelope is not None, "no task_cancel was published"
            assert envelope["message_type"] == "task_cancel"
            assert envelope["payload"]["task_id"] == str(task_id)
        finally:
            await pubsub.unsubscribe(f"worker:{worker_id}:push")
            await pubsub.aclose()

    run(body)


def test_completing_a_reassigned_task_clears_its_retry_state():
    """A task that was reclaimed and then completed by someone else must
    not keep the backoff and the exclusion of the attempt that failed.

    Found reviewing this step, not by a demo: the row is terminal, so
    nothing would ever read those columns again to *act* on — but the task
    console renders them, and a completed task showing "retry after ..."
    and an excluded worker is a row every later reader has to know to
    ignore. Same reasoning that made Step 3.1 clear the lease pair."""

    async def body(sessionmaker):
        first, second = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)
        await _claim_and_expire(sessionmaker, task_id, first)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()
        await _backoff_elapsed(sessionmaker, task_id)

        async with sessionmaker() as session:
            await dequeue(session, worker_id=second, max_batch=MAX_DEQUEUE)
            await session.commit()
        async with sessionmaker() as session:
            outcome = await complete_task(
                session,
                envelope={
                    "task_id": str(task_id),
                    "status": "COMPLETED",
                    "attempt_number": 1,
                    "session_epoch": 1,
                    "idempotency_token": str(uuid.uuid4()),
                    "duration_seconds": 0.1,
                    "result": 5,
                    "truncated": False,
                    "size_bytes": 100,
                },
                worker_id=second,
            )
            await session.commit()
        assert outcome == "transitioned"

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "COMPLETED"
        assert row["not_before"] is None
        assert row["excluded_worker_id"] is None
        assert row["lease_expires_at"] is None

    run(body)


def test_the_reclaimer_wakes_the_engine_when_the_backoff_elapses():
    """**Found by reviewing this step, and it is a latency defect rather
    than a correctness one.** The doorbell rung at reclaim time wakes an
    engine that finds nothing — the task it just requeued is held by
    `not_before`. Without a second wake the retry waits for the safety-net
    poll, up to 30s on top of a backoff measured in seconds, and a recovery
    that is working looks stalled.

    The database stays the authority: this only rings the bell. What is
    asserted is that the bell rings, near the moment the task becomes
    eligible, without anything else happening in between."""

    async def body(sessionmaker):
        assignment._work_available = asyncio.Event()
        moment = datetime.now(timezone.utc) + timedelta(seconds=0.2)
        assignment._schedule_retry_wake([{"not_before": moment}])

        assert not assignment._work_available.is_set()
        await asyncio.wait_for(assignment._work_available.wait(), timeout=3)

        # A backoff that has already elapsed schedules nothing — the
        # doorbell rung by the reclaim itself is enough.
        assignment._work_available = asyncio.Event()
        assignment._schedule_retry_wake(
            [{"not_before": datetime.now(timezone.utc) - timedelta(seconds=5)}]
        )
        await asyncio.sleep(0.05)
        assert not assignment._work_available.is_set()

    run(body)
