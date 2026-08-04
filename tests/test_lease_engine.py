"""Lease and timeout engine tests (Phase 3.1).

Postgres-gated for the same reason `test_task_queue.py` is, and here the
reason is sharper: the entire subject of this module is what Postgres's
own clock says about a timestamp column, and the concurrency claim — that
three replicas reclaiming at once never reclaim the same task twice — is
`FOR UPDATE SKIP LOCKED` behaviour. A fake would prove nothing at all.

**Expiry is forced by writing the past, never by sleeping.** A test that
waits out a 60-second lease is a test nobody runs. Every expiry below is
produced by setting `lease_expires_at` to a moment that has already
passed, which is precisely the state the clock would have reached anyway
— and it keeps the whole module under a second. The one thing this cannot
prove is that time passes, which is why the step's timed demo against a
real worker is recorded separately in the phase document rather than
claimed here.
"""

import asyncio
import contextlib
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "lease engine tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import assignment  # noqa: E402
from app.assignment import (  # noqa: E402
    LocalSession,
    _lease_due,
    _renew_if_due,
    assign_once,
    handle_task_failed,
    handle_task_progress,
    handle_task_started,
    reclaim_once,
    register_session,
    shorten_local_leases,
    unregister_session,
)
from app.config import database_url  # noqa: E402
from app.task_policies import (  # noqa: E402
    InvalidTaskPolicy,
    clear_policy,
    effective_policies,
    set_policy,
    validate_policy,
)
from app.task_queue import (  # noqa: E402
    QueueLimitExceeded,
    dequeue,
    enqueue,
    expired_lease_count,
    mark_status,
    reclaim_expired_leases,
    renew_lease,
    renew_worker_leases,
    shorten_worker_leases,
)
from app.task_types import UnknownTaskType  # noqa: E402

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
    """Stands in for a Starlette WebSocket. Records frames."""

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
                        "SELECT status, assigned_worker_id, assigned_at, started_at, "
                        "lease_expires_at, deadline_at, attempt_count, "
                        "lease_expires_at - now() AS remaining "
                        "FROM tasks WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(task_id)},
                )
            )
            .mappings()
            .one()
        )


async def _backoff_elapsed(sessionmaker, task_id, *, seconds_ago: int = 1) -> None:
    """Put a reclaimed task's retry backoff in the past (Phase 3.2).

    Same discipline as `_expire`: the clock would have got here anyway, and
    a test that sleeps out a backoff is a test nobody runs. Pass a value
    larger than `TASK_RETRY_EXCLUSION_SECONDS` to also age out the
    exclusion of the worker that lost the task.
    """
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET not_before = now() - make_interval(secs => :ago) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id), "ago": seconds_ago},
        )
        await session.commit()


async def _expire(sessionmaker, task_id, *, seconds_ago: int = 1) -> None:
    """Put a task's lease in the past — what the clock would have done."""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET lease_expires_at = now() - make_interval(secs => :ago) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id), "ago": seconds_ago},
        )
        await session.commit()


# --------------------------------------------------------------------------
# Leases are created, renewed, and expire correctly
# --------------------------------------------------------------------------


def test_a_claim_carries_an_acknowledgement_lease_and_an_execution_cap():
    """The first exit criterion, delivery half. A task that has been handed
    out but has not reported starting is on the *acknowledgement* clock —
    ~30s — not the execution one, and it already carries the execution cap
    its type will be held to (see `_dequeue_sql` on why the cap is stamped
    at delivery as well as at `task_started`)."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            claimed = await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        assert len(claimed) == 1
        assert claimed[0]["lease_expires_at"] is not None

        row = await _row(sessionmaker, task_id)
        assert row["deadline_at"] is not None
        # 30s default, checked as a range so a slow test machine cannot
        # fail it and a wrong unit (30 minutes, 30ms) still can.
        assert 20 <= row["remaining"].total_seconds() <= 31

    run(body)


def test_starting_a_task_replaces_the_ack_lease_with_an_execution_one():
    """`task_started` is what starts the execution clock: a full lease TTL,
    and the coordinator-set `deadline_at` that caps every later renewal."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        async with sessionmaker() as session:
            outcome = await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()
        assert outcome == "transitioned"

        row = await _row(sessionmaker, task_id)
        assert row["deadline_at"] is not None
        # 60s default lease, up from the 30s ack window.
        assert 50 <= row["remaining"].total_seconds() <= 61
        assert row["started_at"] is not None

    run(body)


def test_a_renewal_pushes_the_lease_out_but_never_past_the_deadline():
    """The cap that makes worker-driven renewal safe (§12). A worker that
    renews after its deadline has passed does not get a fresh minute — it
    gets an expiry in the past, and the reclaimer takes the task."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        # Force the deadline into the past: the task has burned its whole
        # execution cap.
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE tasks SET deadline_at = now() - interval '5 seconds' "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(task_id)},
            )
            await session.commit()

        async with sessionmaker() as session:
            assert await renew_lease(session, task_id=str(task_id), worker_id=worker_id)
            await session.commit()

        row = await _row(sessionmaker, task_id)
        # Renewal wrote the deadline, not now()+60 — the lease is already
        # expired, so the very next reclaim pass takes it.
        assert row["remaining"].total_seconds() < 0

    run(body)


def test_a_renewal_from_the_wrong_worker_writes_nothing():
    """§12. If any worker could renew any lease, a hostile one could keep
    another's task alive indefinitely and starve its recovery."""

    async def body(sessionmaker):
        holder, impostor = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=holder, max_batch=MAX_DEQUEUE)
            await session.commit()

        before = await _row(sessionmaker, task_id)
        async with sessionmaker() as session:
            assert not await renew_lease(
                session, task_id=str(task_id), worker_id=impostor
            )
            await session.commit()
        after = await _row(sessionmaker, task_id)
        assert after["lease_expires_at"] == before["lease_expires_at"]

    run(body)


def test_a_terminal_task_keeps_no_lease():
    """A finished task with a future expiry is a fact every later reader
    would have to interpret. `FAILED` clears both columns."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="FAILED"
            )
            await session.commit()

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "FAILED"
        assert row["lease_expires_at"] is None
        assert row["deadline_at"] is None

    run(body)


# --------------------------------------------------------------------------
# Expiry causes reclaim; a live task never does
# --------------------------------------------------------------------------


def test_an_expired_lease_returns_the_task_to_the_queue():
    """The core recovery move. Note what is cleared and why: a `QUEUED` row
    still claiming `started_at` would make `GET /tasks/{id}`'s timeline
    report a start belonging to an attempt that no longer exists."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()
        await _expire(sessionmaker, task_id)

        async with sessionmaker() as session:
            reclaimed = await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        assert len(reclaimed) == 1
        assert str(reclaimed[0]["previous_worker_id"]) == str(worker_id)
        assert reclaimed[0]["previous_status"] == "RUNNING"

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "QUEUED"
        assert row["assigned_worker_id"] is None
        assert row["assigned_at"] is None
        assert row["started_at"] is None
        assert row["lease_expires_at"] is None
        assert row["deadline_at"] is None
        assert row["attempt_count"] == 1

    run(body)


def test_a_reclaim_writes_one_attempt_row_naming_the_worker_that_lost_it():
    """The recovery timeline Step 3.7 reads. `attempt_number` is the attempt
    that *ended*, not the one about to start — the off-by-one this is
    guarding is exactly the one the gate warned about."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        await _expire(sessionmaker, task_id)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        async with sessionmaker() as session:
            attempt = (
                (
                    await session.execute(
                        text(
                            "SELECT attempt_number, worker_id, outcome, reason "
                            "FROM task_attempts WHERE task_id = CAST(:id AS uuid)"
                        ),
                        {"id": str(task_id)},
                    )
                )
                .mappings()
                .one()
            )

        assert attempt["attempt_number"] == 0
        assert str(attempt["worker_id"]) == str(worker_id)
        # Phase 3.2: the outcome is the branch the retry policy took. This
        # task had attempts left, so the ended attempt was superseded —
        # `REASSIGNED`. In 3.1, which had no policy, every row was
        # `EXPIRED`; the reason code is unchanged because the cause is.
        assert attempt["outcome"] == "REASSIGNED"
        assert attempt["reason"] == "lease_expired"

    run(body)


def test_a_live_lease_is_never_reclaimed():
    """"A legitimate long task renews its lease and is never reclaimed" —
    the exit criterion that stops the recovery engine from being the thing
    that breaks the system."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        # Ten reclaim passes against a task that keeps renewing.
        for _ in range(10):
            async with sessionmaker() as session:
                assert await reclaim_expired_leases(session, batch=RECLAIM_BATCH) == []
                assert await renew_lease(
                    session, task_id=str(task_id), worker_id=worker_id
                )
                await session.commit()

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "RUNNING"
        assert row["attempt_count"] == 0

    run(body)


def test_a_queued_task_has_no_lease_and_is_never_reclaimed():
    """The reclaimer's predicate is `status IN ('ASSIGNED','RUNNING')`, so a
    waiting task cannot be swept up by it however long it waits."""

    async def body(sessionmaker):
        await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        row = await _row(sessionmaker, task_id)
        assert row["lease_expires_at"] is None

        async with sessionmaker() as session:
            assert await reclaim_expired_leases(session, batch=RECLAIM_BATCH) == []
            await session.commit()
        assert (await _row(sessionmaker, task_id))["status"] == "QUEUED"

    run(body)


def test_a_reclaimed_task_is_claimable_again_once_its_backoff_has_passed():
    """`-> QUEUED` rather than `-> REASSIGNED`, proven where it matters: the
    dequeue predicate is `WHERE status = 'QUEUED'`, so recovery is only
    real if the row lands somewhere a claim can find it.

    **Phase 3.2 adds the backoff**, so "immediately" is no longer true and
    the test says so: the task is `QUEUED` at once and *claimable* once
    `not_before` has passed. Retitled rather than deleted, because the
    property it guards — a recovered task is reachable by a real dequeue —
    is the one that matters."""

    async def body(sessionmaker):
        first, second = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=first, max_batch=MAX_DEQUEUE)
            await session.commit()
        await _expire(sessionmaker, task_id)

        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()

        # Queued immediately, but held by its backoff — a second worker
        # asking right now is told there is nothing for it.
        async with sessionmaker() as session:
            assert (await dequeue(session, worker_id=second, max_batch=MAX_DEQUEUE)) == []
        assert (await _row(sessionmaker, task_id))["status"] == "QUEUED"

        await _backoff_elapsed(sessionmaker, task_id)
        async with sessionmaker() as session:
            claimed = await dequeue(session, worker_id=second, max_batch=MAX_DEQUEUE)
            await session.commit()

        assert len(claimed) == 1
        assert str(claimed[0]["id"]) == str(task_id)
        # The wire field the worker echoes back. Attempt 1 is the second
        # execution — `attempt_count` is zero-based.
        assert claimed[0]["attempt_count"] == 1

    run(body)


def test_reclaim_respects_its_batch_and_the_backlog_is_countable():
    """The batch bounds how many row locks one transaction holds. The
    backlog count is what the reclaimer-has-died alert watches."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        ids = [await _enqueue_one(sessionmaker) for _ in range(5)]
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, limit=5, max_batch=MAX_DEQUEUE)
            await session.commit()
        for task_id in ids:
            await _expire(sessionmaker, task_id)

        async with sessionmaker() as session:
            assert await expired_lease_count(session) == 5
            first = await reclaim_expired_leases(session, batch=2)
            await session.commit()
        assert len(first) == 2

        async with sessionmaker() as session:
            assert await expired_lease_count(session) == 3
            rest = await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()
        assert len(rest) == 3

        async with sessionmaker() as session:
            assert await expired_lease_count(session) == 0

        with pytest.raises(QueueLimitExceeded):
            async with sessionmaker() as session:
                await reclaim_expired_leases(session, batch=0)

    run(body)


def test_concurrent_reclaimers_never_reclaim_the_same_task_twice():
    """The multi-replica criterion, and the whole reason no leader is
    elected. Three concurrent reclaimers over 60 expired tasks: every task
    is reclaimed exactly once, by exactly one of them.

    Three *connections* rather than three processes is the honest limit of
    what a test can do here — but `SKIP LOCKED` arbitrates between
    transactions, not between processes, so this exercises the identical
    mechanism. The three-pod version is Step 2.2's measured run, which this
    inherits rather than re-proving."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        ids = [await _enqueue_one(sessionmaker) for _ in range(60)]
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, limit=60, max_batch=MAX_DEQUEUE)
            await session.commit()
        for task_id in ids:
            await _expire(sessionmaker, task_id)

        async def reclaimer():
            taken: list[str] = []
            async with sessionmaker() as session:
                for _ in range(10):
                    rows = await reclaim_expired_leases(session, batch=7)
                    await session.commit()
                    taken.extend(str(row["id"]) for row in rows)
                    if not rows:
                        break
                    await asyncio.sleep(0)
            return taken

        results = await asyncio.gather(reclaimer(), reclaimer(), reclaimer())
        claimed = [task for batch in results for task in batch]

        assert len(claimed) == len(set(claimed)), "a task was reclaimed twice"
        assert set(claimed) == {str(task_id) for task_id in ids}

        async with sessionmaker() as session:
            still_live = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM tasks "
                        "WHERE status IN ('ASSIGNED', 'RUNNING')"
                    )
                )
            ).scalar_one()
            attempts = (
                await session.execute(text("SELECT count(*) FROM task_attempts"))
            ).scalar_one()
        assert still_live == 0
        # Exactly one attempt row per task, so no reclaim was double-counted
        # in the recovery timeline either.
        assert attempts == 60

    run(body)


# --------------------------------------------------------------------------
# Disconnect and reconnect
# --------------------------------------------------------------------------


def test_a_socket_close_shortens_a_lease_but_never_extends_one():
    """A close accelerates the single recovery trigger; it is not a second
    one. And it is `LEAST`, so a task already inside the grace keeps its
    earlier expiry — a close observed twice changes nothing the second
    time."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() > 50

        async with sessionmaker() as session:
            assert await shorten_worker_leases(
                session, worker_id=worker_id, grace_seconds=30
            ) == 1
            await session.commit()
        shortened = (await _row(sessionmaker, task_id))["remaining"].total_seconds()
        assert 20 <= shortened <= 31

        # A second, longer grace must not push it back out.
        async with sessionmaker() as session:
            await shorten_worker_leases(session, worker_id=worker_id, grace_seconds=600)
            await session.commit()
        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() <= shortened

    run(body)


def test_shortening_touches_only_the_disconnected_workers_tasks():
    """The property that stops this from being a fleet-wide storm."""

    async def body(sessionmaker):
        gone, healthy = await _reset(sessionmaker, workers=2)
        mine = await _enqueue_one(sessionmaker)
        theirs = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=gone, max_batch=MAX_DEQUEUE)
            await dequeue(session, worker_id=healthy, max_batch=MAX_DEQUEUE)
            await session.commit()

        # Give both a full execution lease so the shortening is visible.
        async with sessionmaker() as session:
            for task_id, holder in ((mine, gone), (theirs, healthy)):
                await mark_status(
                    session, task_id=str(task_id), worker_id=holder, new_status="RUNNING"
                )
            await session.commit()

        untouched_before = (await _row(sessionmaker, theirs))["lease_expires_at"]
        async with sessionmaker() as session:
            await shorten_worker_leases(session, worker_id=gone, grace_seconds=5)
            await session.commit()

        assert (await _row(sessionmaker, mine))["remaining"].total_seconds() <= 6
        assert (await _row(sessionmaker, theirs))["lease_expires_at"] == untouched_before

    run(body)


def test_a_reconnect_renews_every_live_lease_the_worker_holds():
    """The `hello` path. A worker that drops and comes straight back inside
    the grace loses nothing — and the task ids come from the database, not
    from anything the worker said."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        ids = [await _enqueue_one(sessionmaker) for _ in range(3)]
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, limit=3, max_batch=MAX_DEQUEUE)
            await session.commit()

        async with sessionmaker() as session:
            await shorten_worker_leases(session, worker_id=worker_id, grace_seconds=2)
            await session.commit()
        assert (await _row(sessionmaker, ids[0]))["remaining"].total_seconds() <= 3

        async with sessionmaker() as session:
            renewed = await renew_worker_leases(session, worker_id=worker_id)
            await session.commit()
        # The ids, not a count — the reconnecting session needs them to know
        # which tasks it may renew later (see `LocalSession.recovered_tasks`).
        assert sorted(renewed) == sorted(str(task_id) for task_id in ids)

        for task_id in ids:
            assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() > 50

        async with sessionmaker() as session:
            assert await reclaim_expired_leases(session, batch=RECLAIM_BATCH) == []
            await session.commit()

    run(body)


def test_shorten_local_leases_survives_a_broken_database():
    """A disconnect must never raise out of the session teardown. Slower
    recovery — the task expires on its full TTL instead — beats a handler
    that dies mid-cleanup."""

    async def body(sessionmaker):
        @contextlib.asynccontextmanager
        async def _broken():
            raise RuntimeError("database is gone")
            yield  # pragma: no cover

        original = assignment.get_session
        assignment.get_session = _broken
        try:
            assert await shorten_local_leases(str(uuid.uuid4()), reason="test") == 0
        finally:
            assignment.get_session = original

    run(body)


# --------------------------------------------------------------------------
# Lazy renewal: the cost-control property
# --------------------------------------------------------------------------


def test_a_message_before_the_renewal_is_due_writes_nothing():
    """The whole reason a 10s progress cadence does not become 10 writes a
    second per hundred tasks. An undue message costs no round trip at all,
    which is checked here by breaking the session factory: if `_renew_if_due`
    reached the database, this would raise."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        session = make_session(worker_id)
        session.credited[str(task_id)] = "corr"

        # Unknown task -> always due, so the first message renews.
        assert _lease_due(session, str(task_id))
        await _renew_if_due(session, str(task_id))
        assert not _lease_due(session, str(task_id))

        @contextlib.asynccontextmanager
        async def _explode():
            raise AssertionError("an undue renewal reached the database")
            yield  # pragma: no cover

        original = assignment.get_session
        assignment.get_session = _explode
        try:
            await _renew_if_due(session, str(task_id))
        finally:
            assignment.get_session = original

    run(body)


def test_a_renewal_for_a_task_this_session_never_held_is_ignored():
    """§12. A worker inventing task ids must not be able to make the
    coordinator issue one statement per message."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        session = make_session(worker_id)

        @contextlib.asynccontextmanager
        async def _explode():
            raise AssertionError("an unheld task id reached the database")
            yield  # pragma: no cover

        original = assignment.get_session
        assignment.get_session = _explode
        try:
            await _renew_if_due(session, str(uuid.uuid4()))
        finally:
            assignment.get_session = original

    run(body)


def test_task_started_records_the_renewal_it_just_performed():
    """`mark_status` writes a full lease inside its own UPDATE, so the very
    next progress message must not write the same value again."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        session = make_session(worker_id)
        session.credited[str(task_id)] = "corr"
        register_session(session)
        try:
            await handle_task_started(
                session,
                {
                    "payload": {"task_id": str(task_id), "task_type": "count_to_n"},
                    "correlation_id": "corr",
                },
            )
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        assert not _lease_due(session, str(task_id))

    run(body)


# --------------------------------------------------------------------------
# The loop, end to end
# --------------------------------------------------------------------------


def test_reclaim_once_requeues_and_the_engine_hands_the_task_out_again():
    """Recovery end to end through the shipped code paths rather than the
    primitives: expire, reclaim, and watch the assignment engine deliver
    the task to a different worker."""

    async def body(sessionmaker):
        first, second = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)

        dead = make_session(first)
        register_session(dead)
        try:
            assert await assign_once() == 1
            assert str(task_id) in dead.credited
        finally:
            unregister_session(dead.worker_id, dead.session_epoch)

        await _expire(sessionmaker, task_id)
        assert await reclaim_once() == 1
        # Phase 3.2: the retry backoff holds the task until it elapses.
        # Written into the past rather than waited out (see `_expire`).
        await _backoff_elapsed(sessionmaker, task_id)

        alive = make_session(second)
        register_session(alive)
        try:
            assert await assign_once() == 1
            assert str(task_id) in alive.credited
        finally:
            unregister_session(alive.worker_id, alive.session_epoch)

        row = await _row(sessionmaker, task_id)
        assert row["status"] == "ASSIGNED"
        assert str(row["assigned_worker_id"]) == str(second)
        assert row["attempt_count"] == 1

    run(body)


# --------------------------------------------------------------------------
# Per-type policy, without a redeploy
# --------------------------------------------------------------------------


def test_a_policy_row_changes_the_lease_a_new_claim_gets():
    """The sixth exit criterion, proven at the level that matters: no
    process was restarted and no configuration was reloaded between these
    two claims — only a row was written."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        first = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert 20 <= (await _row(sessionmaker, first))["remaining"].total_seconds() <= 31

        async with sessionmaker() as session:
            await set_policy(
                session, task_type="count_to_n", values={"ack_timeout_seconds": 300}
            )
            await session.commit()

        second = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert (await _row(sessionmaker, second))["remaining"].total_seconds() > 290

    run(body)


def test_a_policy_is_per_type_even_when_one_claim_spans_several():
    """A per-type policy that only worked for a single-type worker would be
    a policy in name only — one dequeue claims across every type the worker
    supports, which is why the lookup is a correlated subquery rather than
    a bound parameter."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(
                session, task_type="sleep", values={"ack_timeout_seconds": 600}
            )
            await session.commit()

        counted = await _enqueue_one(sessionmaker, task_type="count_to_n")
        slept = await _enqueue_one(sessionmaker, task_type="sleep")
        async with sessionmaker() as session:
            claimed = await dequeue(
                session, worker_id=worker_id, limit=2, max_batch=MAX_DEQUEUE
            )
            await session.commit()
        assert len(claimed) == 2

        assert (await _row(sessionmaker, counted))["remaining"].total_seconds() <= 31
        assert (await _row(sessionmaker, slept))["remaining"].total_seconds() > 590

    run(body)


def test_a_policy_changes_the_execution_deadline_too():
    """`max_execution_seconds` is the one an operator most plausibly needs
    to change, because it is the only timeout a *legitimate* long task can
    hit."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(
                session, task_type="count_to_n", values={"max_execution_seconds": 7200}
            )
            await session.commit()

        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        async with sessionmaker() as session:
            gap = (
                await session.execute(
                    text(
                        "SELECT deadline_at - now() AS gap FROM tasks "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": str(task_id)},
                )
            ).scalar_one()
        assert 7100 <= gap.total_seconds() <= 7201

    run(body)


def test_clearing_a_policy_restores_the_code_default():
    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(
                session, task_type="count_to_n", values={"ack_timeout_seconds": 600}
            )
            await session.commit()
            assert await clear_policy(session, task_type="count_to_n")
            await session.commit()
            # A second clear is not an error — the intent is satisfied.
            assert not await clear_policy(session, task_type="count_to_n")

        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() <= 31

    run(body)


def test_effective_policies_names_where_every_number_came_from():
    """§10: a recommended value and a configured one must never be blurred.
    A bare list of numbers cannot tell an operator which is which."""

    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(
                session, task_type="sleep", values={"lease_ttl_seconds": 900}
            )
            await session.commit()
            report = await effective_policies(session)

        by_type = {entry["task_type"]: entry for entry in report}
        assert set(by_type) == {"count_to_n", "hash_rounds", "sleep", "opaque_payload"}

        slept = by_type["sleep"]
        assert slept["lease_ttl_seconds"] == 900
        assert slept["lease_ttl_seconds_source"] == "policy"
        assert slept["ack_timeout_seconds_source"] == "default"
        # The per-type code default from `task_types`, untouched.
        assert slept["max_execution_seconds"] == 3900

        counted = by_type["count_to_n"]
        assert counted["lease_ttl_seconds_source"] == "default"
        assert counted["updated_at"] is None

    run(body)


def test_a_partial_update_leaves_the_other_fields_alone():
    """Partial updates are the common operator action; a model requiring
    all four would make "lengthen one timeout" a read-modify-write."""

    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            await set_policy(
                session,
                task_type="hash_rounds",
                values={"ack_timeout_seconds": 45, "lease_ttl_seconds": 120},
            )
            await session.commit()
            await set_policy(
                session, task_type="hash_rounds", values={"lease_ttl_seconds": 240}
            )
            await session.commit()
            report = {e["task_type"]: e for e in await effective_policies(session)}

        assert report["hash_rounds"]["ack_timeout_seconds"] == 45
        assert report["hash_rounds"]["lease_ttl_seconds"] == 240

    run(body)


@pytest.mark.parametrize(
    "values",
    [
        {"lease_ttl_seconds": 0},  # would reclaim every task instantly
        {"ack_timeout_seconds": -5},
        {"max_execution_seconds": 999_999},  # a cap this long is not a cap
        {"max_attempts": 0},
        {"lease_ttl_seconds": "soon"},
        {"not_a_field": 5},
        {},  # nothing to apply
    ],
)
def test_nonsense_policy_values_are_refused(values):
    """An operator setting a zero lease is not attacking the system, they
    have made a typo — but the effect would be the recovery engine denying
    service to the system it protects."""
    with pytest.raises(InvalidTaskPolicy):
        validate_policy(values)


def test_a_policy_for_an_unregistered_type_is_refused():
    async def body(sessionmaker):
        await _reset(sessionmaker)
        async with sessionmaker() as session:
            with pytest.raises(UnknownTaskType):
                await set_policy(
                    session, task_type="not_a_type", values={"lease_ttl_seconds": 60}
                )
            await session.rollback()

    run(body)


# --------------------------------------------------------------------------
# Three defects found reviewing this step's own implementation. Each of
# these tests fails against the version of Step 3.1 that was written first.
# --------------------------------------------------------------------------


def test_a_reconnected_worker_can_renew_the_task_it_is_still_running():
    """**Regression, and it was the worst of the three.** A worker that
    reconnects mid-execution gets a new session that holds no credit key for
    the work it is still doing — those tasks were delivered to a socket that
    no longer exists, and Decision #101 has the worker declare a *count*,
    which cannot be keyed.

    The first implementation checked only `credited`, so every progress
    message about that task was skipped, nothing renewed its lease, and it
    was reclaimed one lease later **from a worker that was doing exactly
    what it should**. The recovery engine would have been the thing causing
    the failure — the outcome the gate spent §3.0.2 avoiding.

    The fix is `recovered_tasks`, seeded from what the database says the
    worker holds, never from the worker."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        # The socket dies. A new session opens with no credit for the task.
        reconnected = make_session(worker_id)
        assert str(task_id) not in reconnected.credited

        async with sessionmaker() as session:
            renewed = await renew_worker_leases(session, worker_id=worker_id)
            await session.commit()
        assert renewed == [str(task_id)]
        reconnected.recovered_tasks.update(renewed)

        # Wind the lease down as if the renewal window had elapsed, then let
        # a progress message arrive exactly as the worker would send one.
        await _expire(sessionmaker, task_id, seconds_ago=0)
        reconnected.lease_renew_due.clear()
        await handle_task_progress(
            reconnected,
            {"payload": {"task_id": str(task_id), "progress": 0.5}, "correlation_id": "c"},
        )

        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() > 50

        async with sessionmaker() as session:
            assert await reclaim_expired_leases(session, batch=RECLAIM_BATCH) == []
            await session.commit()

    run(body)


def test_progress_renews_before_the_telemetry_lookup_not_after():
    """The same defect from the other side. A reconnected worker's task has
    no `current_tasks` entry — the `task_started` that would have made one
    belongs to the dead session — so a renewal placed after that lookup is
    unreachable for exactly the case that needs it."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()

        session = make_session(worker_id)
        session.recovered_tasks.add(str(task_id))
        assert str(task_id) not in session.current_tasks

        await handle_task_progress(
            session,
            {"payload": {"task_id": str(task_id), "progress": 0.1}, "correlation_id": "c"},
        )
        # The renewal happened even though the telemetry lookup missed.
        assert not _lease_due(session, str(task_id))
        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() > 50

    run(body)


def test_a_late_result_for_a_reassigned_task_still_frees_the_slot():
    """**Regression for Decision #168, pulled forward from Step 3.4 because
    Step 3.1 is what makes the case reachable.**

    Before reassignment existed, `NOT_OWNER` could only mean a worker naming
    a task that was not its own, so refusing to release the credit was a
    §12 protection with no honest victim. A reclaimed task's row no longer
    names its original worker, so that worker's sincere late report now
    returns `NOT_OWNER` — and the shipped rule held its credit for the life
    of the session. Three reassignments and the worker silently stops
    accepting work."""

    async def body(sessionmaker):
        loser, winner = await _reset(sessionmaker, workers=2)
        task_id = await _enqueue_one(sessionmaker)

        session = make_session(loser)
        register_session(session)
        try:
            assert await assign_once() == 1
            assert session.free_credits == 3

            # The task is reclaimed and handed to someone else.
            await _expire(sessionmaker, task_id)
            assert await reclaim_once() == 1
            async with sessionmaker() as db:
                await dequeue(db, worker_id=winner, max_batch=MAX_DEQUEUE)
                await db.commit()

            # The original worker finishes and reports. The row is not its
            # own any more, so the database answers NOT_OWNER.
            await handle_task_failed(
                session,
                {
                    "payload": {"task_id": str(task_id), "error_type": "ValueError"},
                    "correlation_id": "c",
                },
            )
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        # The slot is free again: the worker delivered this task, so its
        # credit was genuinely held against it.
        assert session.free_credits == 4
        assert str(task_id) not in session.credited

    run(body)


def test_a_report_for_a_task_this_session_never_held_still_frees_nothing():
    """The other half of #168, and the reason the fix is `in credited` and
    not "release on NOT_OWNER". A worker guessing another worker's task id
    must not be able to draw down its own credits (§12)."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker)

        session = make_session(worker_id)
        register_session(session)
        try:
            assert await assign_once() == 1
            held = session.free_credits

            # A report naming a task this session never delivered.
            await handle_task_failed(
                session,
                {
                    "payload": {"task_id": str(uuid.uuid4()), "error_type": "ValueError"},
                    "correlation_id": "c",
                },
            )
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        assert session.free_credits == held
        assert str(task_id) in session.credited

    run(body)


def test_the_execution_cap_at_claim_time_is_the_types_own_default():
    """**Regression for the defect a live run found, not review.**

    The shipped worker ignores a re-delivery of a task id it is already
    executing (`task_assign_duplicate_ignored`, M2 behaviour). So after a
    reclaim hands a task back to the same worker there is no second
    `task_started` — and with `deadline_at` written only there, that task's
    lease could be renewed forever by progress messages from an execution
    that may be hung. That is exactly the failure the cap exists for.

    It also has to be **per type** at claim time, which is why the default
    is a SQL `CASE` and not one bound parameter: one dequeue claims across
    every type a worker supports, and `sleep` legitimately runs for over an
    hour while `opaque_payload` never should."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        slept = await _enqueue_one(sessionmaker, task_type="sleep")
        counted = await _enqueue_one(sessionmaker, task_type="count_to_n")

        async with sessionmaker() as session:
            claimed = await dequeue(
                session, worker_id=worker_id, limit=2, max_batch=MAX_DEQUEUE
            )
            await session.commit()
        assert len(claimed) == 2

        async def cap(task_id):
            async with sessionmaker() as session:
                gap = (
                    await session.execute(
                        text(
                            "SELECT deadline_at - now() AS gap FROM tasks "
                            "WHERE id = CAST(:id AS uuid)"
                        ),
                        {"id": str(task_id)},
                    )
                ).scalar_one()
            return gap.total_seconds()

        # Both claimed by one statement, each on its own type's cap.
        assert 3800 <= await cap(slept) <= 3901
        assert 200 <= await cap(counted) <= 301

    run(body)


def test_a_task_reclaimed_back_to_the_same_worker_still_has_a_cap():
    """The end-to-end shape of the defect above: reclaim, re-deliver, and
    confirm the second delivery is bounded even though no second
    `task_started` will ever arrive for it."""

    async def body(sessionmaker):
        (worker_id,) = await _reset(sessionmaker)
        task_id = await _enqueue_one(sessionmaker, task_type="sleep")
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await mark_status(
                session, task_id=str(task_id), worker_id=worker_id, new_status="RUNNING"
            )
            await session.commit()

        await _expire(sessionmaker, task_id)
        async with sessionmaker() as session:
            await reclaim_expired_leases(session, batch=RECLAIM_BATCH)
            await session.commit()
        # The reclaim cleared the cap along with the lease.
        assert (await _row(sessionmaker, task_id))["deadline_at"] is None

        # Phase 3.2 excludes the worker that lost the task for a bounded
        # window, so getting it back means that window has elapsed — which
        # is the case this defect lives in on a single-worker fleet, and
        # therefore still the case worth testing.
        await _backoff_elapsed(sessionmaker, task_id, seconds_ago=120)

        # The same worker is handed it back and never sends task_started.
        async with sessionmaker() as session:
            await dequeue(session, worker_id=worker_id, max_batch=MAX_DEQUEUE)
            await session.commit()
        assert (await _row(sessionmaker, task_id))["deadline_at"] is not None

        # Renewal cannot push past that cap, however long the worker keeps
        # reporting: force the deadline past and the next renewal writes an
        # expiry in the past rather than a fresh minute.
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE tasks SET deadline_at = now() - interval '1 second' "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": str(task_id)},
            )
            await renew_lease(session, task_id=str(task_id), worker_id=worker_id)
            await session.commit()
        assert (await _row(sessionmaker, task_id))["remaining"].total_seconds() < 0

    run(body)
