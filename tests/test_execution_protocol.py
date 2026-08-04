"""Coordinator side of the execution protocol (Phase 2.4).

Postgres-gated for the same reason `test_assignment.py` is: importing
`app.assignment` builds the database engine at import time, and the
transitions under test here (`ASSIGNED -> RUNNING`, `-> FAILED`) are real
row writes guarded by `FOR UPDATE` — a fake would prove nothing.

The centrepiece is `test_a_capacity_refusal_does_not_livelock_the_queue`.
That is a regression test for a defect the design review found *before* the
code existed: the original Decision #97 freed the credit on any refusal,
which meant assign → refuse → free → doorbell → assign, stranding every
queued task in `ASSIGNED` at loop speed. Decision #101 replaced it. If
anyone ever "simplifies" the saturation rule back to freeing the credit,
that test is what fails.
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
        "execution protocol tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from prometheus_client import REGISTRY  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import assignment  # noqa: E402
from app.assignment import (  # noqa: E402
    REFUSE_AT_CAPACITY,
    REFUSE_UNSUPPORTED_TYPE,
    LocalSession,
    assign_once,
    current_tasks_key,
    handle_capacity,
    handle_task_ack,
    handle_task_failed,
    handle_task_progress,
    handle_task_started,
    parse_tasks_in_flight,
    register_session,
)
from app.config import database_url  # noqa: E402
from app.task_queue import (  # noqa: E402
    ILLEGAL,
    NOOP,
    NOT_FOUND,
    NOT_OWNER,
    TRANSITIONED,
    enqueue,
    mark_status,
)
from app.task_states import CANCELLED, FAILED, RUNNING  # noqa: E402

MAX_DEQUEUE = 100
ALL_TYPES = ("count_to_n", "hash_rounds", "sleep", "opaque_payload")


@pytest.fixture(scope="module", autouse=True)
def migrated():
    from alembic import command
    from alembic.config import Config

    coordinator_dir = Path(__file__).resolve().parent.parent / "coordinator"
    prev_cwd = os.getcwd()
    os.chdir(coordinator_dir)
    try:
        command.upgrade(Config(str(coordinator_dir / "alembic.ini")), "head")
    finally:
        os.chdir(prev_cwd)


class FakeRedis:
    """Enough Redis for the current-task mirror.

    Substituted rather than used live because the real asyncio client binds
    its connection pool to the first event loop that touches it, and every
    test here runs its own loop. What is being tested is what the
    coordinator *writes*, which this records exactly.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):  # noqa: A003 — mirrors redis-py
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def publish(self, channel, message):
        return 0


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    assignment._local_sessions.clear()
    fake = FakeRedis()
    monkeypatch.setattr(assignment, "redis_client", fake)
    yield fake
    assignment._local_sessions.clear()


class FakeSocket:
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
    worker_id,
    *,
    max_concurrent: int = 4,
    types: tuple[str, ...] = ALL_TYPES,
    epoch: int = 1,
    residual_in_flight: int = 0,
) -> LocalSession:
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=epoch,
        websocket=FakeSocket(),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=types,
        residual_in_flight=residual_in_flight,
    )


def run(body):
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


async def _fill(sessionmaker, *, count: int = 1, task_type: str = "count_to_n") -> None:
    async with sessionmaker() as session:
        for _ in range(count):
            await enqueue(
                session,
                task_type=task_type,
                parameters={"n": 5} if task_type == "count_to_n" else {"rounds": 5},
                correlation_id=f"corr-{uuid.uuid4()}",
            )
        await session.commit()


async def _statuses(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as db:
        rows = (
            await db.execute(text("SELECT status, count(*) AS n FROM tasks GROUP BY status"))
        ).mappings().all()
    return {row["status"]: row["n"] for row in rows}


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --------------------------------------------------------------------------
# tasks_in_flight — worker-reported, therefore clamped (§12, Decision #101)
# --------------------------------------------------------------------------


def test_a_worker_that_declares_no_in_flight_count_starts_at_zero():
    """Every worker built before Step 2.4 existed. Zero is the truth for
    them: they never executed anything."""
    assert parse_tasks_in_flight({"agent_version": "0.1.0"}, 4) == 0


def test_an_inflated_in_flight_count_is_clamped_to_the_credit_ceiling():
    assert parse_tasks_in_flight({"tasks_in_flight": 10_000}, 4) == 4


def test_a_nonsense_in_flight_count_is_treated_as_zero():
    for claimed in (-3, "many", None, [1]):
        assert parse_tasks_in_flight({"tasks_in_flight": claimed}, 4) == 0


def test_a_reconnecting_worker_with_running_work_is_not_over_committed():
    """The whole point of the field: 3 of 4 slots are genuinely busy, so the
    coordinator may hand out exactly one more."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=10)

            session = make_session(worker_id, max_concurrent=4, residual_in_flight=3)
            register_session(session)

            assert session.free_credits == 1
            assert await assign_once() == 1
            assert await assign_once() == 0

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# The livelock the design review caught (Decision #101 supersedes #97)
# --------------------------------------------------------------------------


def test_a_capacity_refusal_saturates_the_credit_instead_of_freeing_it():
    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.pending_acks["t1"] = "corr-1"
        session.credited["t1"] = "corr-1"

        await handle_task_ack(
            session,
            {"payload": {"task_id": "t1", "accepted": False, "reason_code": REFUSE_AT_CAPACITY}},
        )

        # The credit is NOT returned, and the door is shut: this worker draws
        # no more work until it reports capacity itself.
        assert session.in_flight == 1
        assert session.saturated is True
        assert session.free_credits == 0

    asyncio.run(body())


def test_only_the_worker_reporting_capacity_reopens_a_saturated_session():
    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.credited["t1"] = "corr-1"
        session.saturated = True

        await handle_capacity(session, {"payload": {"task_id": "t1", "freed": 1}})

        assert session.saturated is False
        assert session.in_flight == 0
        assert session.free_credits == 4

    asyncio.run(body())


def test_an_unsupported_type_refusal_still_frees_one_credit():
    """The rare cause, which the eligibility filter makes nearly
    unreachable. Freeing here is safe precisely because it cannot repeat at
    loop speed."""

    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.pending_acks["t1"] = "corr-1"
        session.credited["t1"] = "corr-1"

        await handle_task_ack(
            session,
            {
                "payload": {
                    "task_id": "t1",
                    "accepted": False,
                    "reason_code": REFUSE_UNSUPPORTED_TYPE,
                }
            },
        )

        assert session.in_flight == 0
        assert session.saturated is False

    asyncio.run(body())


def test_a_refusal_without_a_reason_code_frees_the_credit():
    """Backwards compatibility: a Step 2.3 worker only ever refused an
    unsupported type, so an unlabelled refusal means exactly that."""

    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.pending_acks["t1"] = "corr-1"
        session.credited["t1"] = "corr-1"

        await handle_task_ack(session, {"payload": {"task_id": "t1", "accepted": False}})

        assert session.in_flight == 0
        assert session.saturated is False

    asyncio.run(body())


def test_a_capacity_refusal_does_not_livelock_the_queue():
    """**The regression test for the defect the review caught.**

    A worker with one credit that refuses for lack of capacity must leave
    the rest of the queue alone. Under the superseded Decision #97 the
    credit was freed, the doorbell rang, the same session was picked again,
    and the whole queue drained into `ASSIGNED` without a single task ever
    executing.
    """

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=6)

            session = make_session(worker_id, max_concurrent=1)
            register_session(session)

            assert await assign_once() == 1
            refused_task = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            await handle_task_ack(
                session,
                {
                    "payload": {
                        "task_id": refused_task,
                        "accepted": False,
                        "reason_code": REFUSE_AT_CAPACITY,
                    }
                },
            )

            # The pass that would have looped: it must deliver nothing.
            for _ in range(5):
                assert await assign_once() == 0

            statuses = await _statuses(sm)
            # Exactly one task moved — the refused one, which stays ASSIGNED
            # for Phase 3. The other five are untouched and still claimable.
            assert statuses == {"ASSIGNED": 1, "QUEUED": 5}

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Credit release is once per task, whatever the worker sends
# --------------------------------------------------------------------------


def test_a_duplicate_capacity_for_one_task_releases_one_credit():
    """§3.7 idempotency, and §12: a worker cannot mint credits by repeating
    itself."""

    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4)
        session.credited = {"t1": "corr-1", "t2": "corr-2"}

        await handle_capacity(session, {"payload": {"task_id": "t1", "freed": 1}})
        await handle_capacity(session, {"payload": {"task_id": "t1", "freed": 1}})
        await handle_capacity(session, {"payload": {"task_id": "t1", "freed": 99}})

        assert session.in_flight == 1
        assert set(session.credited) == {"t2"}

    asyncio.run(body())


def test_capacity_without_a_task_id_still_releases_reconnect_residue():
    """The reconnect case: those tasks were counted from `hello`, never
    keyed by id here, so `freed` is the only handle on them."""

    async def body():
        session = make_session(uuid.uuid4(), max_concurrent=4, residual_in_flight=3)

        await handle_capacity(session, {"payload": {"freed": 1}})
        assert session.in_flight == 2

        await handle_capacity(session, {"payload": {"freed": 99}})
        assert session.in_flight == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# mark_status — the only new write path, and every guard on it
# --------------------------------------------------------------------------


def test_a_worker_can_move_its_own_task_to_running():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            assert await assign_once() == 1
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id=task_id, worker_id=worker_id, new_status=RUNNING
                    )
                    == TRANSITIONED
                )
                await db.commit()

            assert await _statuses(sm) == {"RUNNING": 1}

        return inner(sessionmaker)

    run(_body)


def test_a_worker_cannot_move_another_workers_task():
    """§12. The same class of defect Step 2.2.1 closed on the admin surface:
    a shared authority that let any worker act for any other."""

    def _body(sessionmaker):
        async def inner(sm):
            owner, impostor = await _reset(sm, workers=2)
            await _fill(sm, count=1)
            session = make_session(owner)
            register_session(session)
            assert await assign_once() == 1
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id=task_id, worker_id=impostor, new_status=RUNNING
                    )
                    == NOT_OWNER
                )

            assert await _statuses(sm) == {"ASSIGNED": 1}

        return inner(sessionmaker)

    run(_body)


def test_an_unknown_or_malformed_task_id_is_answered_not_crashed_on():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id="not-a-uuid", worker_id=worker_id, new_status=RUNNING
                    )
                    == NOT_FOUND
                )
                assert (
                    await mark_status(
                        db,
                        task_id=str(uuid.uuid4()),
                        worker_id=worker_id,
                        new_status=RUNNING,
                    )
                    == NOT_FOUND
                )

        return inner(sessionmaker)

    run(_body)


def test_repeating_a_transition_is_a_noop_not_an_error():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id=task_id, worker_id=worker_id, new_status=RUNNING
                    )
                    == TRANSITIONED
                )
                await db.commit()
            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id=task_id, worker_id=worker_id, new_status=RUNNING
                    )
                    == NOOP
                )

        return inner(sessionmaker)

    run(_body)


def test_a_transition_out_of_a_terminal_state_is_refused():
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            async with sm() as db:
                await mark_status(
                    db, task_id=task_id, worker_id=worker_id, new_status=CANCELLED
                )
                await db.commit()
            async with sm() as db:
                assert (
                    await mark_status(
                        db, task_id=task_id, worker_id=worker_id, new_status=RUNNING
                    )
                    == ILLEGAL
                )

            assert await _statuses(sm) == {"CANCELLED": 1}

        return inner(sessionmaker)

    run(_body)


def test_the_phase_3_columns_stay_untouched_by_execution():
    """A Phase 2.1 exit criterion that holds through all of M2: nothing
    writes `lease_expires_at` or `attempt_count`, and Decision #103 adds no
    execution timeout that would need them."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            await handle_task_started(session, {"payload": {"task_id": task_id}})
            await handle_task_progress(
                session, {"payload": {"task_id": task_id, "progress": 0.5}}
            )
            await handle_task_failed(
                session, {"payload": {"task_id": task_id, "error_type": "ValueError"}}
            )

            async with sm() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT status, lease_expires_at, attempt_count, completed_at "
                            "FROM tasks"
                        )
                    )
                ).mappings().one()
            assert row["status"] == "FAILED"
            assert row["lease_expires_at"] is None
            assert row["attempt_count"] == 0
            # Completion timestamps belong to Step 2.5, including for a
            # failure: `updated_at` already records when this moved.
            assert row["completed_at"] is None

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# The three worker reports, end to end against the database
# --------------------------------------------------------------------------


def test_task_started_moves_the_row_to_running_and_shows_the_current_task(clean_state):
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            before = _metric("coordinator_tasks_started_total")
            await handle_task_started(
                session,
                {
                    "correlation_id": "corr-x",
                    "payload": {"task_id": task_id, "task_type": "count_to_n"},
                },
            )

            assert await _statuses(sm) == {"RUNNING": 1}
            assert _metric("coordinator_tasks_started_total") == before + 1
            # And it is visible to the dashboard, from Redis rather than from
            # this process's memory (§3.9).
            mirrored = json.loads(clean_state.store[current_tasks_key(str(worker_id))])
            assert [t["task_id"] for t in mirrored] == [task_id]
            assert mirrored[0]["task_type"] == "count_to_n"

        return inner(sessionmaker)

    run(_body)


def test_progress_updates_the_sample_without_touching_the_row(clean_state):
    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            async with sm() as db:
                before = (
                    await db.execute(text("SELECT updated_at FROM tasks"))
                ).scalar_one()

            for fraction in (0.25, 0.5, 0.75):
                await handle_task_progress(
                    session,
                    {"payload": {"task_id": task_id, "progress": fraction, "elapsed_seconds": 1}},
                )

            mirrored = json.loads(clean_state.store[current_tasks_key(str(worker_id))])
            assert mirrored[0]["progress"] == 0.75

            async with sm() as db:
                after = (
                    await db.execute(text("SELECT updated_at FROM tasks"))
                ).scalar_one()
            # Not one database write for three samples.
            assert after == before
            assert await _statuses(sm) == {"RUNNING": 1}

        return inner(sessionmaker)

    run(_body)


def test_progress_for_a_task_this_session_did_not_start_is_dropped():
    async def body():
        session = make_session(uuid.uuid4())
        before = _metric("coordinator_task_progress_reports_total", outcome="unknown")

        await handle_task_progress(
            session, {"payload": {"task_id": str(uuid.uuid4()), "progress": 0.9}}
        )

        assert session.current_tasks == {}
        assert (
            _metric("coordinator_task_progress_reports_total", outcome="unknown") == before + 1
        )

    asyncio.run(body())


def test_progress_is_clamped_to_a_fraction():
    async def body():
        session = make_session(uuid.uuid4())
        session.current_tasks["t1"] = {"task_id": "t1", "progress": 0.0}

        await handle_task_progress(session, {"payload": {"task_id": "t1", "progress": 12.0}})
        assert session.current_tasks["t1"]["progress"] == 1.0

        await handle_task_progress(session, {"payload": {"task_id": "t1", "progress": -4}})
        assert session.current_tasks["t1"]["progress"] == 0.0

        await handle_task_progress(session, {"payload": {"task_id": "t1", "progress": "nope"}})
        assert session.current_tasks["t1"]["progress"] == 0.0

    asyncio.run(body())


def test_a_failed_task_reaches_failed_and_frees_its_credit(caplog):
    """Decision #102's whole reason for existing: without this the task sits
    RUNNING forever and its credit is never returned."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id, max_concurrent=2)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(session, {"payload": {"task_id": task_id}})
            assert session.in_flight == 1

            with caplog.at_level(logging.WARNING, logger="coordinator"):
                await handle_task_failed(
                    session,
                    {
                        "correlation_id": "corr-f",
                        "payload": {"task_id": task_id, "error_type": "ZeroDivisionError"},
                    },
                )

            assert await _statuses(sm) == {"FAILED": 1}
            assert session.in_flight == 0
            assert session.current_tasks == {}

            failed = [r for r in caplog.records if r.getMessage() == "task_failed"]
            assert failed and failed[0].error_type == "ZeroDivisionError"
            # The type, and only the type — never a traceback (§12).
            assert not hasattr(failed[0], "traceback")

        return inner(sessionmaker)

    run(_body)


def test_a_task_that_fails_before_its_start_was_processed_still_reaches_failed():
    """`FAILED` is reachable from `ASSIGNED` as well as `RUNNING`, so a fast
    failure does not need the transitions to arrive in order."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            await handle_task_failed(
                session, {"payload": {"task_id": task_id, "error_type": "RuntimeError"}}
            )

            assert await _statuses(sm) == {"FAILED": 1}

        return inner(sessionmaker)

    run(_body)


def test_a_successful_task_frees_the_credit_and_stays_running_until_step_2_5():
    """Recorded as behaviour rather than left implicit: Step 2.4 discards
    results (Decision #98), and `RUNNING -> COMPLETED` is Step 2.5's, which
    owns the result envelope. So a finished task's row is still RUNNING —
    what changes is that the worker's slot is free again."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=2)
            session = make_session(worker_id, max_concurrent=1)
            register_session(session)

            assert await assign_once() == 1
            first = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(session, {"payload": {"task_id": first}})
            assert await assign_once() == 0  # no credit left

            await handle_capacity(session, {"payload": {"task_id": first, "freed": 1}})

            assert await assign_once() == 1  # the slot cycled
            assert await _statuses(sm) == {"RUNNING": 1, "ASSIGNED": 1}

        return inner(sessionmaker)

    run(_body)


def test_a_worker_reporting_a_task_it_does_not_own_changes_nothing(caplog):
    def _body(sessionmaker):
        async def inner(sm):
            owner, impostor = await _reset(sm, workers=2)
            await _fill(sm, count=1)
            owner_session = make_session(owner)
            register_session(owner_session)
            await assign_once()
            task_id = owner_session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            impostor_session = make_session(impostor)
            with caplog.at_level(logging.WARNING, logger="coordinator"):
                await handle_task_started(impostor_session, {"payload": {"task_id": task_id}})
                await handle_task_failed(
                    impostor_session,
                    {"payload": {"task_id": task_id, "error_type": "ValueError"}},
                )

            assert await _statuses(sm) == {"ASSIGNED": 1}
            rejected = [r for r in caplog.records if r.getMessage() == "task_started_rejected"]
            assert rejected and rejected[0].outcome == NOT_OWNER
            # And it gained nothing by trying: naming someone else's task must
            # not draw down its own credits (§12).
            assert impostor_session.in_flight == 0

        return inner(sessionmaker)

    run(_body)


def test_naming_another_workers_task_in_a_failure_report_releases_no_credit():
    """The same guard on the failure path specifically, because that one has
    to release a credit in almost every other case."""

    def _body(sessionmaker):
        async def inner(sm):
            owner, impostor = await _reset(sm, workers=2)
            await _fill(sm, count=1)
            owner_session = make_session(owner)
            register_session(owner_session)
            await assign_once()
            task_id = owner_session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            # The impostor is genuinely running two tasks of its own, declared
            # at reconnect — the credits it would otherwise be able to free.
            impostor_session = make_session(impostor, residual_in_flight=2)
            await handle_task_failed(
                impostor_session,
                {"payload": {"task_id": task_id, "error_type": "ValueError"}},
            )

            assert impostor_session.in_flight == 2  # untouched
            assert await _statuses(sm) == {"ASSIGNED": 1}

        return inner(sessionmaker)

    run(_body)


def test_the_state_machine_still_forbids_what_it_forbade():
    """A guard on the guard: `mark_status` is only safe because these two
    moves are legal and nothing here invented a third."""
    from app.task_states import ASSIGNED, check_transition

    assert check_transition(ASSIGNED, RUNNING) is True
    assert check_transition(RUNNING, FAILED) is True
    assert check_transition(ASSIGNED, FAILED) is True
