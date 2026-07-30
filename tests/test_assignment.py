"""Assignment engine tests (Phase 2.3).

Postgres-gated for the same reason `test_task_queue.py` is: the half of
this module that matters — claiming, eligibility filtering, and never
handing one task to two workers — is `FOR UPDATE SKIP LOCKED` behaviour,
and a fake would prove nothing. The capability and credit-accounting
tests do not need a database, but they live behind the same guard because
importing `app.assignment` pulls in `app.db`, which builds its engine at
import time from `POSTGRES_*`.

The engine's own `get_session` is patched per test onto a test-local
sessionmaker. `app.db.engine` binds its pool to the first event loop that
touches it, and every test here runs its own loop via `asyncio.run`, so
sharing it across tests would fail on the second one.
"""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "assignment tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from prometheus_client import REGISTRY  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import assignment  # noqa: E402
from app.assignment import (  # noqa: E402
    LocalSession,
    assign_once,
    handle_capacity,
    handle_task_ack,
    log_unacknowledged,
    parse_capabilities,
    register_session,
    unregister_session,
)
from app.config import database_url  # noqa: E402
from app.task_queue import QueueLimitExceeded, dequeue, enqueue, queue_depth  # noqa: E402

MAX_DEQUEUE = 100
ALL_TYPES = ("count_to_n", "hash_rounds", "sleep", "opaque_payload")


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


@pytest.fixture(autouse=True)
def clean_registry():
    """The session registry is module-global; never leak one test's sockets
    into the next."""
    assignment._local_sessions.clear()
    yield
    assignment._local_sessions.clear()


class FakeSocket:
    """Stands in for a Starlette WebSocket. Records frames, or refuses."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send_text(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(data))

    def assignments(self) -> list[dict]:
        return [m for m in self.sent if m["message_type"] == "task_assign"]


def make_session(
    worker_id: uuid.UUID | str,
    *,
    max_concurrent: int = 4,
    types: tuple[str, ...] = ALL_TYPES,
    fail: bool = False,
    epoch: int = 1,
) -> LocalSession:
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=epoch,
        websocket=FakeSocket(fail=fail),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=types,
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
    """Empty the queue and create `workers` real worker rows.

    `tasks.assigned_worker_id` is a real foreign key, so a claim needs a
    real row to point at.
    """
    ids = [uuid.uuid4() for _ in range(workers)]
    async with sessionmaker() as session:
        await session.execute(text("TRUNCATE tasks"))
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


async def _fill(sessionmaker, *, task_type: str = "count_to_n", count: int = 1) -> None:
    async with sessionmaker() as session:
        for _ in range(count):
            await enqueue(
                session,
                task_type=task_type,
                parameters={"n": 5} if task_type == "count_to_n" else {"rounds": 5},
                correlation_id=f"corr-{uuid.uuid4()}",
            )
        await session.commit()


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --------------------------------------------------------------------------
# Declared capabilities — worker-reported, therefore sanitised (§12)
# --------------------------------------------------------------------------


def test_a_worker_that_declares_nothing_supports_every_registered_type():
    """Compatibility with every worker built before Step 2.3 existed."""
    max_concurrent, types = parse_capabilities({"agent_version": "0.1.0"})
    assert set(types) == set(ALL_TYPES)
    assert max_concurrent >= 1


def test_an_inflated_max_concurrent_is_clamped_to_the_ceiling():
    """A worker claiming a billion credits must not drain the queue."""
    _, _ = parse_capabilities({})
    max_concurrent, _ = parse_capabilities({"max_concurrent": 1_000_000_000})
    assert max_concurrent == int(os.environ.get("WORKER_MAX_CONCURRENT_CEILING", "64"))


def test_a_nonsense_max_concurrent_floors_at_one():
    for claimed in (0, -5):
        max_concurrent, _ = parse_capabilities({"max_concurrent": claimed})
        assert max_concurrent == 1


def test_a_garbage_max_concurrent_falls_back_to_the_default():
    max_concurrent, _ = parse_capabilities({"max_concurrent": "lots"})
    assert max_concurrent == int(os.environ.get("WORKER_DEFAULT_MAX_CONCURRENT", "4"))


def test_unknown_task_types_are_dropped_not_trusted():
    _, types = parse_capabilities(
        {"supported_task_types": ["count_to_n", "mine_bitcoin", "drop_tables"]}
    )
    assert types == ("count_to_n",)


def test_a_worker_naming_only_unknown_types_is_eligible_for_nothing():
    """The honest answer — deliberately not widened to "everything"."""
    _, types = parse_capabilities({"supported_task_types": ["nonsense"]})
    assert types == ()


def test_a_non_list_type_declaration_is_eligible_for_nothing():
    _, types = parse_capabilities({"supported_task_types": "count_to_n"})
    assert types == ()


# --------------------------------------------------------------------------
# Session registry
# --------------------------------------------------------------------------


def test_a_superseded_session_cleanup_does_not_evict_the_winner():
    """Step 1.7 ordering: the loser's `finally` runs after the winner has
    registered. Without the epoch guard it would unregister the live one."""
    worker_id = uuid.uuid4()
    winner = make_session(worker_id, epoch=2)
    register_session(winner)

    unregister_session(str(worker_id), session_epoch=1)  # the loser cleaning up

    assert assignment._local_sessions[str(worker_id)] is winner

    unregister_session(str(worker_id), session_epoch=2)
    assert str(worker_id) not in assignment._local_sessions


# --------------------------------------------------------------------------
# Acknowledgement and credit accounting
# --------------------------------------------------------------------------


def test_an_accepted_ack_clears_the_pending_entry_but_holds_the_slot():
    """Receipt is not completion. The credit stays consumed until the
    worker releases it, which is Step 2.4's `capacity` message."""

    async def body():
        session = make_session(uuid.uuid4())
        session.in_flight = 1
        session.pending_acks["t1"] = "corr-1"

        await handle_task_ack(session, {"payload": {"task_id": "t1", "accepted": True}})

        assert session.pending_acks == {}
        assert session.in_flight == 1

    asyncio.run(body())


def test_a_refusal_frees_the_credit():
    async def body():
        session = make_session(uuid.uuid4())
        session.in_flight = 1
        session.pending_acks["t1"] = "corr-1"

        await handle_task_ack(
            session,
            {"payload": {"task_id": "t1", "accepted": False, "reason": "unsupported"}},
        )

        assert session.pending_acks == {}
        assert session.in_flight == 0

    asyncio.run(body())


def test_a_duplicate_ack_is_a_harmless_noop():
    """Idempotency (§3.7): a second ack must not double-credit the worker."""

    async def body():
        session = make_session(uuid.uuid4())
        session.in_flight = 1
        session.pending_acks["t1"] = "corr-1"

        await handle_task_ack(session, {"payload": {"task_id": "t1", "accepted": False}})
        await handle_task_ack(session, {"payload": {"task_id": "t1", "accepted": False}})

        assert session.in_flight == 0  # not -1

    asyncio.run(body())


def test_capacity_releases_slots_without_going_negative():
    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.in_flight = 2

        await handle_capacity(session, {"payload": {"freed": 1}})
        assert session.in_flight == 1

        await handle_capacity(session, {"payload": {"freed": 99}})
        assert session.in_flight == 0
        assert session.free_credits == 4

    asyncio.run(body())


def test_tasks_unacknowledged_at_disconnect_are_logged_with_their_correlation_id(caplog):
    """Exit criterion: a worker vanishing between assignment and ack is
    detected. Recovery is Phase 3; the log line is what exists now."""
    session = make_session(uuid.uuid4())
    session.pending_acks = {"task-a": "corr-a", "task-b": "corr-b"}

    with caplog.at_level(logging.WARNING, logger="coordinator"):
        log_unacknowledged(session, reason="worker_disconnected")

    events = [r for r in caplog.records if r.getMessage() == "task_unacknowledged_at_disconnect"]
    assert {r.task_id for r in events} == {"task-a", "task-b"}
    assert {r.correlation_id for r in events} == {"corr-a", "corr-b"}


# --------------------------------------------------------------------------
# Eligibility filtering, in the queue primitive
# --------------------------------------------------------------------------


def test_the_eligibility_filter_claims_only_declared_types():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, task_type="count_to_n", count=3)
            await _fill(sm, task_type="hash_rounds", count=3)

            async with sm() as session:
                claimed = await dequeue(
                    session,
                    worker_id=worker_id,
                    limit=10,
                    max_batch=MAX_DEQUEUE,
                    task_types=["hash_rounds"],
                )
                await session.commit()

            assert len(claimed) == 3
            assert {c["task_type"] for c in claimed} == {"hash_rounds"}

            # The ineligible ones are untouched, not consumed.
            async with sm() as session:
                assert await queue_depth(session) == 3

        return inner(sessionmaker)

    run(_body)


def test_an_empty_type_list_is_rejected_rather_than_widened():
    """"Eligible for nothing" must never quietly become "eligible for
    everything" — that would hand a lying worker the whole queue."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            async with sm() as session:
                with pytest.raises(QueueLimitExceeded):
                    await dequeue(
                        session,
                        worker_id=worker_id,
                        limit=1,
                        max_batch=MAX_DEQUEUE,
                        task_types=[],
                    )

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# The engine itself
# --------------------------------------------------------------------------


def test_a_task_reaches_an_eligible_worker_and_is_recorded_assigned():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)

            session = make_session(worker_id, max_concurrent=4)
            register_session(session)

            assert await assign_once() == 1

            frames = session.websocket.assignments()  # type: ignore[attr-defined]
            assert len(frames) == 1
            frame = frames[0]
            assert frame["payload"]["task_type"] == "count_to_n"
            # The task's own correlation id travels with it (§11).
            assert frame["correlation_id"].startswith("corr-")

            async with sm() as session_db:
                row = (
                    await session_db.execute(
                        text(
                            "SELECT status, assigned_worker_id, assigned_at, "
                            "lease_expires_at, attempt_count FROM tasks"
                        )
                    )
                ).mappings().one()
            assert row["status"] == "ASSIGNED"
            assert str(row["assigned_worker_id"]) == str(worker_id)
            assert row["assigned_at"] is not None
            # Phase 2.1 reservation still holds — M2 writes neither.
            assert row["lease_expires_at"] is None
            assert row["attempt_count"] == 0

        return inner(sessionmaker)

    run(_body)


def test_a_task_with_no_eligible_worker_stays_queued_and_visible():
    """Exit criterion: not silently dropped, not dead-lettered."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, task_type="hash_rounds", count=2)

            # Connected, with credits — but eligible for a different type.
            register_session(make_session(worker_id, types=("sleep",)))

            assert await assign_once() == 0

            async with sm() as session:
                assert await queue_depth(session) == 2
                statuses = (
                    await session.execute(text("SELECT DISTINCT status FROM tasks"))
                ).scalars().all()
            assert statuses == ["QUEUED"]

        return inner(sessionmaker)

    run(_body)


def test_credits_bound_how_much_work_one_worker_is_handed():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=10)

            session = make_session(worker_id, max_concurrent=3)
            register_session(session)

            assert await assign_once() == 3
            assert session.in_flight == 3
            assert session.free_credits == 0
            # A second pass adds nothing: no credits left.
            assert await assign_once() == 0

            async with sm() as db:
                assert await queue_depth(db) == 7

            # Releasing a slot lets exactly one more through.
            await handle_capacity(session, {"payload": {"freed": 1}})
            assert await assign_once() == 1

        return inner(sessionmaker)

    run(_body)


def test_no_task_is_delivered_to_two_workers():
    def _body(sessionmaker):
        async def inner(sm):
            worker_ids = await _reset(sm, workers=5)
            await _fill(sm, count=50)

            sessions = [make_session(w, max_concurrent=10) for w in worker_ids]
            for session in sessions:
                register_session(session)

            delivered = 0
            while (n := await assign_once()) > 0:
                delivered += n

            assert delivered == 50

            task_ids = [
                frame["payload"]["task_id"]
                for session in sessions
                for frame in session.websocket.assignments()  # type: ignore[attr-defined]
            ]
            assert len(task_ids) == 50
            assert len(set(task_ids)) == 50  # no id delivered twice

            # And the database agrees: every row claimed exactly once.
            async with sm() as db:
                rows = (
                    await db.execute(
                        text(
                            "SELECT count(*) AS total, "
                            "count(DISTINCT id) AS distinct_ids, "
                            "count(assigned_worker_id) AS assigned "
                            "FROM tasks WHERE status = 'ASSIGNED'"
                        )
                    )
                ).mappings().one()
            assert rows["total"] == 50
            assert rows["distinct_ids"] == 50
            assert rows["assigned"] == 50

        return inner(sessionmaker)

    run(_body)


def test_an_idle_pass_costs_one_query_no_matter_how_many_workers_are_connected():
    """The measurable core of the "100 idle workers, negligible load" exit
    criterion: with an empty queue the per-worker loop is never entered, so
    the cost of a pass is flat in fleet size rather than linear."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_ids = await _reset(sm, workers=3)
            for worker_id in worker_ids:
                register_session(make_session(worker_id, max_concurrent=8))

            before = _metric("coordinator_assignment_dequeue_queries_total")
            assert await assign_once() == 0
            after = _metric("coordinator_assignment_dequeue_queries_total")

            # Zero dequeue statements — the depth check short-circuited the
            # whole pass, with three idle workers registered.
            assert after == before

        return inner(sessionmaker)

    run(_body)


def test_a_dead_socket_leaves_the_task_assigned_and_is_logged(caplog):
    """Commit-before-send means a failed delivery cannot lose the task; it
    strands it as ASSIGNED, which is visible and is Phase 3's to reclaim."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)

            session = make_session(worker_id, fail=True)
            register_session(session)

            with caplog.at_level(logging.WARNING, logger="coordinator"):
                assert await assign_once() == 0  # nothing delivered

            assert any(
                r.getMessage() == "task_assign_delivery_failed" for r in caplog.records
            )
            assert session.in_flight == 0
            assert session.pending_acks == {}

            async with sm() as db:
                status = (
                    await db.execute(text("SELECT status FROM tasks"))
                ).scalar_one()
            assert status == "ASSIGNED"  # claimed, undelivered — not lost

        return inner(sessionmaker)

    run(_body)
