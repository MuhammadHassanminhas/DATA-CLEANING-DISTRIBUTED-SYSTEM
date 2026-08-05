"""Coordinator side of result submission and completion (Phase 2.5).

Postgres-gated for the same reason `test_execution_protocol.py` is: what is
under test here is a **transaction** — the result row and the task's move to
`COMPLETED` landing together under one `FOR UPDATE` lock — and a fake would
prove nothing about that.

The two tests worth reading first:

* `test_a_duplicate_submission_writes_nothing_the_second_time` is §3.7 made
  concrete. Duplicate result submission is a no-op *structurally*: the task's
  own terminal state is what suppresses it, so it holds for a retry, for two
  replicas racing, and for a worker that simply repeats itself.
* `test_a_malformed_result_leaves_the_task_exactly_as_it_was` is the exit
  criterion that a bad message cannot corrupt state — checked field by field
  rather than by status alone, because "not corrupted" is a claim about the
  whole row.
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
        "result submission tests require Postgres (set POSTGRES_HOST)",
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
    handle_task_failed,
    handle_task_result,
    handle_task_started,
    register_session,
)
from app.config import database_url, task_result_max_bytes  # noqa: E402
from app.task_queue import (  # noqa: E402
    DUPLICATE,
    NOT_OWNER,
    TRANSITIONED,
    complete_task,
    enqueue,
    get_task,
    purge_expired_results,
)

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
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def assignments(self) -> list[dict]:
        return [m for m in self.sent if m["message_type"] == "task_assign"]

    def acks(self) -> list[dict]:
        return [m for m in self.sent if m["message_type"] == "task_result_ack"]


def make_session(worker_id, *, max_concurrent: int = 4, epoch: int = 1, residual: int = 0):
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=epoch,
        websocket=FakeSocket(),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=ALL_TYPES,
        residual_in_flight=residual,
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
    # Sockets from an earlier phase of the same test are dropped here, not
    # just between tests. `register_session` keys on worker id and each reset
    # mints new ids, so without this a stale session keeps its credits and
    # `assign_once` hands it the task — the delivery lands on a socket the
    # test is no longer looking at.
    assignment._local_sessions.clear()
    ids = [uuid.uuid4() for _ in range(workers)]
    async with sessionmaker() as session:
        await session.execute(text("TRUNCATE tasks CASCADE"))
        await session.execute(text("TRUNCATE task_results CASCADE"))
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


async def _row(sessionmaker) -> dict:
    async with sessionmaker() as db:
        return dict((await db.execute(text("SELECT * FROM tasks"))).mappings().one())


async def _result_rows(sessionmaker) -> list[dict]:
    async with sessionmaker() as db:
        return [
            dict(row)
            for row in (await db.execute(text("SELECT * FROM task_results"))).mappings().all()
        ]


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


def result_message(task_id: str, **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "status": "COMPLETED",
        "attempt_number": 0,
        "session_epoch": 1,
        "idempotency_token": uuid.uuid4().hex,
        "duration_seconds": 2.5,
        "result": 5,
    }
    payload.update(overrides)
    return {"correlation_id": "corr-result", "payload": payload}


async def _assigned_and_running(sessionmaker, *, max_concurrent: int = 4):
    """One task delivered to one worker and moved to RUNNING. Returns the
    session and the task id."""
    worker_id = (await _reset(sessionmaker))[0]
    await _fill(sessionmaker, count=1)
    session = make_session(worker_id, max_concurrent=max_concurrent)
    register_session(session)
    assert await assign_once() == 1
    task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
    await handle_task_started(session, {"payload": {"task_id": task_id}})
    return session, task_id


# --------------------------------------------------------------------------
# The criterion: results persist and tasks reach COMPLETED
# --------------------------------------------------------------------------


def test_a_result_persists_and_the_task_reaches_completed():
    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            before = _metric("coordinator_tasks_completed_total")

            await handle_task_result(session, result_message(task_id))

            assert await _statuses(sm) == {"COMPLETED": 1}
            row = await _row(sm)
            assert row["completed_at"] is not None
            assert row["result_id"] is not None

            results = await _result_rows(sm)
            assert len(results) == 1
            assert str(results[0]["id"]) == str(row["result_id"])
            assert results[0]["payload"]["result"] == 5
            assert results[0]["size_bytes"] == results[0]["payload"]["size_bytes"]
            assert _metric("coordinator_tasks_completed_total") == before + 1

        return inner(sessionmaker)

    run(_body)


def test_the_credit_comes_back_so_the_worker_cycles():
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

            await handle_task_result(session, result_message(first))

            assert session.in_flight == 0
            assert await assign_once() == 1  # the slot cycled
            assert await _statuses(sm) == {"COMPLETED": 1, "ASSIGNED": 1}

        return inner(sessionmaker)

    run(_body)


def test_execution_duration_is_recorded_and_readable_alongside_the_observed_one():
    """The "execution duration recorded and visible" criterion. Two durations
    come back and they are different measurements: the worker-reported
    execution time, and the coordinator-observed assigned-to-completed
    interval, which is the one that cannot be lied about (§12)."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await handle_task_result(session, result_message(task_id, duration_seconds=12.75))

            async with sm() as db:
                task = await get_task(db, task_id)

            assert task["status"] == "COMPLETED"
            assert task["result_payload"]["duration_seconds"] == 12.75
            assert task["assigned_at"] is not None and task["completed_at"] is not None
            assert task["completed_at"] >= task["assigned_at"]

        return inner(sessionmaker)

    run(_body)


def test_a_task_that_never_reported_running_still_completes_through_running():
    """A `task_started` can be lost to a socket that died between it and the
    result. Rather than making `ASSIGNED -> COMPLETED` legal everywhere
    forever to serve one lossy edge, the task is walked through `RUNNING`
    inside the same transaction — two legal transitions, no new edge."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            assert await _statuses(sm) == {"ASSIGNED": 1}  # no task_started was sent

            await handle_task_result(session, result_message(task_id))

            assert await _statuses(sm) == {"COMPLETED": 1}

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Idempotency (§3.7) — structural, not clamped
# --------------------------------------------------------------------------


def test_a_duplicate_submission_writes_nothing_the_second_time():
    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            message = result_message(task_id)

            await handle_task_result(session, message)
            first = await _row(sm)

            # Same envelope again, then a *different* one for the same task —
            # a worker is untrusted, so a retry and a lie must both be no-ops.
            await handle_task_result(session, message)
            await handle_task_result(session, result_message(task_id, result="different"))

            assert len(await _result_rows(sm)) == 1
            after = await _row(sm)
            assert after["result_id"] == first["result_id"]
            assert after["completed_at"] == first["completed_at"]
            assert after["status"] == "COMPLETED"

        return inner(sessionmaker)

    run(_body)


def test_a_duplicate_is_acknowledged_as_accepted_so_the_worker_stops_retrying():
    """From the worker's side a duplicate *is* success — the task is
    completed and there is nothing left to submit.

    **The same message object twice, and Phase 3.3 is why it has to be.** A
    real retry re-sends the envelope it already built, idempotency token and
    all; the worker mints that token once per task execution. Two calls to
    `result_message` would be two *different* submissions for one task, which
    3.3 answers `superseded` — see `tests/test_idempotency.py`.
    """

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            retry = result_message(task_id)
            await handle_task_result(session, retry)
            await handle_task_result(session, retry)

            acks = session.websocket.acks()  # type: ignore[attr-defined]
            assert [a["payload"]["outcome"] for a in acks] == [TRANSITIONED, DUPLICATE]
            assert all(a["payload"]["accepted"] for a in acks)

        return inner(sessionmaker)

    run(_body)


def test_a_duplicate_releases_no_extra_credit():
    """§12: a worker must not be able to mint credits by repeating itself."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm, max_concurrent=4)
            session.credited["other-task"] = "corr-other"
            assert session.in_flight == 2

            for _ in range(5):
                await handle_task_result(session, result_message(task_id))

            assert session.in_flight == 1
            assert set(session.credited) == {"other-task"}

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Malformed: rejected without corrupting task state
# --------------------------------------------------------------------------


def test_a_malformed_result_leaves_the_task_exactly_as_it_was(caplog):
    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            before = await _row(sm)

            with caplog.at_level(logging.WARNING, logger="coordinator"):
                await handle_task_result(
                    session, result_message(task_id, idempotency_token=None)
                )

            after = await _row(sm)
            assert after == before  # every column, not just the status
            assert await _result_rows(sm) == []

            rejected = [r for r in caplog.records if r.getMessage() == "task_result_rejected"]
            assert rejected and rejected[0].reason_code == "missing_idempotency_token"

        return inner(sessionmaker)

    run(_body)


def test_a_malformed_result_is_refused_definitively_and_frees_the_slot():
    """Two things at once, and they pull in opposite directions if you get
    them wrong. The ack must be *definitive* (`accepted: false`) or the worker
    retries a verdict that will never change; the credit must still come back
    or a rejected result costs the worker a slot for the life of the
    session."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            assert session.in_flight == 1

            await handle_task_result(session, result_message(task_id, status="RUNNING"))

            ack = session.websocket.acks()[0]["payload"]  # type: ignore[attr-defined]
            assert ack["accepted"] is False
            assert ack["outcome"] == "rejected"
            assert ack["reason_code"] == "bad_status"
            assert session.in_flight == 0

        return inner(sessionmaker)

    run(_body)


def test_a_malformed_result_that_names_no_task_releases_no_credit():
    """A rejected message cannot identify a slot, so it must not free one.

    `_release_credit` cannot tell a missing id from a real one: an empty
    string takes its unnamed best-effort branch and pops an arbitrary held
    credit, and an unrecognised string draws down the reconnect residue.
    Either would free the slot of a task that is genuinely still running, on
    the say-so of a message the coordinator has just rejected as not being a
    result at all (§12).
    """

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            session.residual_in_flight = 2  # two tasks running since before this session
            assert session.in_flight == 3

            # No task id at all, then a garbage one, then someone else's.
            await handle_task_result(session, {"payload": {"status": "COMPLETED"}})
            await handle_task_result(
                session, {"payload": {"task_id": "not-a-uuid", "status": "COMPLETED"}}
            )
            await handle_task_result(
                session, result_message(str(uuid.uuid4()), idempotency_token=None)
            )

            assert session.in_flight == 3  # nothing was freed
            assert session.residual_in_flight == 2
            assert await _statuses(sm) == {"RUNNING": 1}

            # The named case still releases: the slot really is free there.
            await handle_task_result(session, result_message(task_id, status="RUNNING"))
            assert session.in_flight == 2

        return inner(sessionmaker)

    run(_body)


def test_a_rejection_never_logs_the_result_body(caplog):
    """§12. A result payload is caller data — the reason may be logged, the
    body may not."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)

            with caplog.at_level(logging.WARNING, logger="coordinator"):
                await handle_task_result(
                    session,
                    result_message(
                        task_id, status="NONSENSE", result="secret-payload-must-not-appear"
                    ),
                )

            rendered = " ".join(
                f"{r.getMessage()} {getattr(r, 'detail', '')}" for r in caplog.records
            )
            assert "secret-payload-must-not-appear" not in rendered

        return inner(sessionmaker)

    run(_body)


def test_a_result_for_another_workers_task_changes_nothing_and_releases_nothing():
    """The same §12 guard the failure path has, on the path that writes the
    most: naming someone else's task must not complete it *or* draw down your
    own credits."""

    def _body(sessionmaker):
        async def inner(sm):
            owner, impostor = await _reset(sm, workers=2)
            await _fill(sm, count=1)
            owner_session = make_session(owner)
            register_session(owner_session)
            await assign_once()
            task_id = owner_session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            impostor_session = make_session(impostor, residual=2)
            await handle_task_result(impostor_session, result_message(task_id))

            assert await _statuses(sm) == {"ASSIGNED": 1}
            assert await _result_rows(sm) == []
            assert impostor_session.in_flight == 2  # untouched
            ack = impostor_session.websocket.acks()[0]["payload"]  # type: ignore[attr-defined]
            assert ack["accepted"] is False and ack["outcome"] == NOT_OWNER

        return inner(sessionmaker)

    run(_body)


def test_a_result_for_an_already_failed_task_is_refused_not_overwritten():
    """`FAILED` is terminal. A late result must not resurrect it into
    `COMPLETED` — the state machine says so, and this is the path that would
    otherwise quietly disagree with it."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await handle_task_failed(
                session, {"payload": {"task_id": task_id, "error_type": "ValueError"}}
            )
            assert await _statuses(sm) == {"FAILED": 1}

            await handle_task_result(session, result_message(task_id))

            assert await _statuses(sm) == {"FAILED": 1}
            assert await _result_rows(sm) == []

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Large payloads
# --------------------------------------------------------------------------


def test_a_large_result_is_stored_whole_and_an_oversize_one_is_truncated():
    def _body(sessionmaker):
        async def inner(sm):
            cap = task_result_max_bytes()

            session, task_id = await _assigned_and_running(sm)
            # Comfortably large, comfortably legal — this is the shape of a
            # full-size `opaque_payload` echo.
            await handle_task_result(session, result_message(task_id, result="y" * 80_000))
            stored = (await _result_rows(sm))[0]
            assert stored["payload"]["truncated"] is False
            assert len(stored["payload"]["result"]) == 80_000
            assert stored["size_bytes"] <= cap

            session, task_id = await _assigned_and_running(sm)
            await handle_task_result(session, result_message(task_id, result="z" * (cap + 5_000)))
            stored = (await _result_rows(sm))[0]
            assert stored["payload"]["truncated"] is True
            assert stored["payload"]["result"] is None
            assert stored["size_bytes"] <= cap
            # Truncated, but still completed — an oversize body is a fact about
            # the payload, not a protocol error.
            assert await _statuses(sm) == {"COMPLETED": 1}

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Backwards compatibility and retention
# --------------------------------------------------------------------------


def test_a_pre_2_5_worker_still_releases_its_credit_with_capacity():
    """§3.5: the coordinator cannot tell one worker generation from another,
    and must not need to. A worker built before this step sends `capacity` on
    success and nothing else — its tasks stay RUNNING, exactly as they did in
    Step 2.4, and its slot still cycles."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm, max_concurrent=1)
            await handle_capacity(session, {"payload": {"task_id": task_id, "freed": 1}})

            assert session.in_flight == 0
            assert await _statuses(sm) == {"RUNNING": 1}
            assert await _result_rows(sm) == []

        return inner(sessionmaker)

    run(_body)


def test_retention_deletes_bodies_and_keeps_the_task_row():
    """The documented period, enforced. The `tasks` row is the permanent
    audit trail; only the body expires, and `result_id` goes NULL through the
    existing `ON DELETE SET NULL` rather than through any application write."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await handle_task_result(session, result_message(task_id))
            assert len(await _result_rows(sm)) == 1

            async with sm() as db:
                # Age the row past the window rather than waiting seven days.
                await db.execute(
                    text("UPDATE task_results SET submitted_at = now() - interval '30 days'")
                )
                await db.commit()

            async with sm() as db:
                purged = await purge_expired_results(db, retention_days=7)
                await db.commit()

            assert purged == 1
            assert await _result_rows(sm) == []
            row = await _row(sm)
            assert row["status"] == "COMPLETED"  # the task survives
            assert row["result_id"] is None  # ON DELETE SET NULL did its job
            assert row["completed_at"] is not None

        return inner(sessionmaker)

    run(_body)


def test_retention_leaves_bodies_inside_the_window_alone():
    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await handle_task_result(session, result_message(task_id))

            async with sm() as db:
                assert await purge_expired_results(db, retention_days=7) == 0
                # Zero disables the sweep entirely — a configuration change,
                # not a code path to remember.
                assert await purge_expired_results(db, retention_days=0) == 0
                await db.commit()

            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# The Phase 3 columns, still untouched
# --------------------------------------------------------------------------


def test_completion_still_writes_neither_lease_nor_attempt_count():
    """A Phase 2.1 exit criterion that has to survive the step that writes
    the most. Step 2.5 *reads* `attempt_count` to put it on the wire, which
    is a different thing from writing it."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            assignment_payload = session.websocket.assignments()[0]["payload"]  # type: ignore[attr-defined]
            assert assignment_payload["attempt"] == 0  # on the wire, and zero

            await handle_task_result(session, result_message(task_id, attempt_number=0))

            row = await _row(sm)
            assert row["lease_expires_at"] is None
            assert row["attempt_count"] == 0

        return inner(sessionmaker)

    run(_body)


def test_complete_task_refuses_a_task_that_is_not_the_reporting_workers():
    """`complete_task` guarded directly, not only through its handler — it is
    the write path, and the ownership check is the thing standing between an
    untrusted worker and someone else's task row."""

    def _body(sessionmaker):
        async def inner(sm):
            owner, impostor = await _reset(sm, workers=2)
            await _fill(sm, count=1)
            session = make_session(owner)
            register_session(session)
            await assign_once()
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]

            envelope = {"task_id": task_id, "size_bytes": 10, "status": "COMPLETED"}
            async with sm() as db:
                assert await complete_task(db, envelope=envelope, worker_id=impostor) == NOT_OWNER
                assert (
                    await complete_task(
                        db, envelope={**envelope, "task_id": str(uuid.uuid4())}, worker_id=owner
                    )
                    == "not_found"
                )

            assert await _result_rows(sm) == []

        return inner(sessionmaker)

    run(_body)
