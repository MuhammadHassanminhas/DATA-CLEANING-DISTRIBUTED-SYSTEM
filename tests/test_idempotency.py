"""Idempotency and duplicate suppression (Phase 3.3), against a real Postgres.

Postgres-gated for the reason `test_result_submission.py` is, and one more
that belongs to this step alone: **the suppression mechanism IS the row
lock**. `complete_task` locks the task row `FOR UPDATE`, reads its status,
and answers without writing. A fake would be testing a Python transcription
of a rule whose whole content is "two transactions serialise on one row" —
the one property a fake cannot have.

What each part of the file proves:

* **Duplicate vs superseded.** A retry carries the *same* idempotency token,
  because the worker mints it once per task execution and reuses it for
  every retry. A second result with a *different* token is not a retry — it
  is another attempt's work arriving late — so it is answered `superseded`,
  and the difference is the whole of "the token is enforced" (Step 3.3).
* **The race.** A's result in flight, the lease expires, the task goes to B,
  both results arrive. All three orderings are reproduced deliberately here,
  including the one where A beats the reclaimer and *wins*.
* **No dedup store.** Asserted, not assumed: the schema is enumerated after a
  storm of duplicates and the coordinator's Redis is compared key for key.

Clocks are written, never waited on — the same discipline `test_retry_policy`
uses, and for the same reason.
"""

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "idempotency tests require Postgres (set POSTGRES_HOST)",
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
    handle_task_failed,
    handle_task_result,
    handle_task_started,
    register_session,
    unregister_session,
)
from app.config import database_url  # noqa: E402
from app.task_queue import (  # noqa: E402
    DUPLICATE,
    NOT_OWNER,
    SUPERSEDED,
    TRANSITIONED,
    complete_task,
    enqueue,
    purge_expired_results,
    reclaim_expired_leases,
)

ALL_TYPES = ("count_to_n", "hash_rounds", "sleep", "opaque_payload")
RECLAIM_BATCH = 100

# Every table the coordinator's migrations create, at head. The point of
# spelling it out is Step 3.3's fourth exit criterion: **no dedup store
# exists to grow unbounded**, and a list that has to be edited before a new
# store can pass CI is what turns that from a promise into a check.
EXPECTED_TABLES = frozenset(
    {
        "alembic_version",
        "workers",
        "tasks",
        "task_results",
        "task_attempts",
        "task_policies",
    }
)


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
    """Records every key written, so "nothing new is stored" is checkable."""

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

    def outcomes(self) -> list[str]:
        return [m["payload"]["outcome"] for m in self.acks()]


def make_session(worker_id, *, max_concurrent: int = 4, residual: int = 0) -> LocalSession:
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=1,
        websocket=FakeSocket(),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=ALL_TYPES,
        residual_in_flight=residual,
    )


def run(body):
    async def _main():
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
    assignment._local_sessions.clear()
    ids = [uuid.uuid4() for _ in range(workers)]
    async with sessionmaker() as session:
        await session.execute(text("TRUNCATE tasks CASCADE"))
        await session.execute(text("TRUNCATE task_results CASCADE"))
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


async def _fill(sessionmaker, *, count: int = 1) -> None:
    async with sessionmaker() as session:
        for _ in range(count):
            await enqueue(
                session,
                task_type="count_to_n",
                parameters={"n": 5},
                correlation_id=f"corr-{uuid.uuid4()}",
            )
        await session.commit()


async def _statuses(sessionmaker) -> dict[str, int]:
    async with sessionmaker() as db:
        rows = (
            await db.execute(text("SELECT status, count(*) AS n FROM tasks GROUP BY status"))
        ).mappings().all()
    return {row["status"]: row["n"] for row in rows}


async def _result_rows(sessionmaker) -> list[dict]:
    async with sessionmaker() as db:
        return [
            dict(row)
            for row in (
                await db.execute(text("SELECT * FROM task_results ORDER BY submitted_at"))
            ).mappings().all()
        ]


async def _task_row(sessionmaker, task_id) -> dict:
    async with sessionmaker() as db:
        return dict(
            (
                await db.execute(
                    text("SELECT * FROM tasks WHERE id = CAST(:id AS uuid)"),
                    {"id": str(task_id)},
                )
            ).mappings().one()
        )


async def _expire(sessionmaker, task_id) -> None:
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id)},
        )
        await session.commit()


async def _reassign_to(sessionmaker, task_id, loser: LocalSession, winner: LocalSession) -> None:
    """Take the task away from `loser` and put it on `winner`.

    **The loser's socket is dropped from the engine first, and that is not
    tidiness.** Ageing `not_before` elapses the retry backoff *and* the
    bounded exclusion window with it (Step 3.2 — one column, two clocks), so
    a still-registered loser with free credits is a perfectly legal
    destination for the task it just lost, and the first run of this test got
    it back. Dropping the socket models the reason the lease expired in the
    first place: that worker is not reachable.
    """
    await _expire(sessionmaker, task_id)
    unregister_session(loser.worker_id, loser.session_epoch)
    async with sessionmaker() as db:
        assert len(await reclaim_expired_leases(db, batch=RECLAIM_BATCH)) == 1
        await db.commit()
    await _backoff_elapsed(sessionmaker, task_id)

    register_session(winner)
    assert await assign_once() == 1
    delivered = winner.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
    assert delivered == str(task_id)
    await handle_task_started(winner, {"payload": {"task_id": str(task_id)}})


async def _backoff_elapsed(sessionmaker, task_id) -> None:
    """Age both clocks `not_before` carries — the retry backoff and the
    bounded exclusion of the worker that lost the task."""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE tasks SET not_before = now() - interval '1 hour' "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(task_id)},
        )
        await session.commit()


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


def result_message(task_id: str, *, token: str | None = None, **overrides) -> dict:
    """One `task_result` message.

    `token` defaults to a fresh one per call, so a test that means *a retry*
    has to say so — which is the distinction this whole step turns on.
    """
    payload = {
        "task_id": str(task_id),
        "status": "COMPLETED",
        "attempt_number": 0,
        "session_epoch": 1,
        "idempotency_token": token or uuid.uuid4().hex,
        "duration_seconds": 1.5,
        "result": 5,
    }
    payload.update(overrides)
    return {"correlation_id": "corr-result", "payload": payload}


async def _assigned_and_running(sessionmaker, *, max_concurrent: int = 4):
    worker_id = (await _reset(sessionmaker))[0]
    await _fill(sessionmaker, count=1)
    session = make_session(worker_id, max_concurrent=max_concurrent)
    register_session(session)
    assert await assign_once() == 1
    task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
    await handle_task_started(session, {"payload": {"task_id": task_id}})
    return session, task_id


# --------------------------------------------------------------------------
# The token is enforced: a retry is a duplicate, another attempt is not
# --------------------------------------------------------------------------


def test_a_retry_of_the_same_submission_completes_the_task_exactly_once():
    """The first exit criterion, checked in the database rather than in the
    ack: one result row, one `completed_at`, one `result_id`."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            token = uuid.uuid4().hex
            retry = result_message(task_id, token=token)

            for _ in range(5):
                await handle_task_result(session, retry)

            rows = await _result_rows(sm)
            assert len(rows) == 1
            assert rows[0]["payload"]["idempotency_token"] == token

            task = await _task_row(sm, task_id)
            assert task["status"] == "COMPLETED"
            assert str(task["result_id"]) == str(rows[0]["id"])
            assert session.websocket.outcomes() == [TRANSITIONED] + [DUPLICATE] * 4  # type: ignore[attr-defined]

        return inner(sessionmaker)

    run(_body)


def test_a_duplicate_is_answered_as_success_so_the_worker_stops_retrying():
    """The third exit criterion. A duplicate is not an error: the worker's
    own result is the stored one, so `accepted` is true and the pending
    submission is dropped."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            retry = result_message(task_id, token=uuid.uuid4().hex)

            await handle_task_result(session, retry)
            await handle_task_result(session, retry)

            acks = [a["payload"] for a in session.websocket.acks()]  # type: ignore[attr-defined]
            assert [a["outcome"] for a in acks] == [TRANSITIONED, DUPLICATE]
            assert all(a["accepted"] for a in acks)

        return inner(sessionmaker)

    run(_body)


def test_a_different_result_for_a_completed_task_is_superseded_not_duplicate():
    """**This is what "enforce the idempotency token" buys.** Before 3.3 both
    of these were `duplicate`, so an ack could say a result had landed when
    another submission's had. A different token is a different submission and
    is told so — definitively, and without writing anything."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            winner = uuid.uuid4().hex

            await handle_task_result(session, result_message(task_id, token=winner))
            first = await _task_row(sm, task_id)

            await handle_task_result(session, result_message(task_id, result="different"))

            assert session.websocket.outcomes() == [TRANSITIONED, SUPERSEDED]  # type: ignore[attr-defined]
            ack = session.websocket.acks()[-1]["payload"]  # type: ignore[attr-defined]
            assert ack["accepted"] is False

            rows = await _result_rows(sm)
            assert len(rows) == 1
            assert rows[0]["payload"]["idempotency_token"] == winner
            assert rows[0]["payload"]["result"] == 5  # the loser's body was never stored

            after = await _task_row(sm, task_id)
            assert after["completed_at"] == first["completed_at"]
            assert after["result_id"] == first["result_id"]

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# The in-flight-versus-reassignment race, reproduced in all three orders
# --------------------------------------------------------------------------


def test_the_slow_workers_result_loses_to_the_reassignment_that_finished_first():
    """The race the step names. A is delivered the task and is still
    computing; the lease expires; the reclaimer requeues it; B takes it and
    completes it; **then** A's result arrives.

    Winner rule: B, because it completed the task while holding it. A is told
    `superseded`, nothing is written, and the ledger has one row."""

    def _body(sessionmaker):
        async def inner(sm):
            a_id, b_id = await _reset(sm, workers=2)
            await _fill(sm, count=1)

            a = make_session(a_id)
            register_session(a)
            assert await assign_once() == 1
            task_id = a.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(a, {"payload": {"task_id": task_id}})

            # A stops answering: its lease runs out, the reclaimer requeues
            # the task, and B picks it up and finishes it.
            b = make_session(b_id)
            await _reassign_to(sm, task_id, a, b)
            winner = uuid.uuid4().hex
            await handle_task_result(b, result_message(task_id, token=winner))

            # A finally finishes the work it never stopped doing.
            await handle_task_result(a, result_message(task_id))

            assert b.websocket.outcomes() == [TRANSITIONED]  # type: ignore[attr-defined]
            assert a.websocket.outcomes() == [SUPERSEDED]  # type: ignore[attr-defined]

            rows = await _result_rows(sm)
            assert len(rows) == 1
            assert rows[0]["payload"]["idempotency_token"] == winner
            assert await _statuses(sm) == {"COMPLETED": 1}

            # And the slot A was holding comes back — losing a race must not
            # cost a worker capacity for the life of its session.
            assert a.in_flight == 0

        return inner(sessionmaker)

    run(_body)


def test_a_result_that_beats_the_reclaimer_wins_and_the_reclaim_matches_nothing():
    """The other order, and the one that shows the rule needs no extra
    machinery: A's result lands *before* the sweep runs. The task is terminal,
    so the reclaimer's `WHERE status IN ('ASSIGNED','RUNNING')` no longer
    matches it — no reassignment, no second attempt, no extra row."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await _expire(sm, task_id)  # the lease is already past, unswept

            await handle_task_result(session, result_message(task_id))

            async with sm() as db:
                reclaimed = await reclaim_expired_leases(db, batch=RECLAIM_BATCH)
                await db.commit()

            assert reclaimed == []
            task = await _task_row(sm, task_id)
            assert task["status"] == "COMPLETED"
            assert task["attempt_count"] == 0
            assert task["lease_expires_at"] is None
            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


def test_a_result_for_a_task_reassigned_but_not_yet_finished_writes_nothing():
    """The third order: A's result arrives while B is still running the
    reassigned task. The task is not terminal, so the ownership guard is what
    refuses it — `not_owner`, nothing written, and B can still complete it."""

    def _body(sessionmaker):
        async def inner(sm):
            a_id, b_id = await _reset(sm, workers=2)
            await _fill(sm, count=1)

            a = make_session(a_id)
            register_session(a)
            assert await assign_once() == 1
            task_id = a.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(a, {"payload": {"task_id": task_id}})

            b = make_session(b_id)
            await _reassign_to(sm, task_id, a, b)

            await handle_task_result(a, result_message(task_id))

            assert a.websocket.outcomes() == [NOT_OWNER]  # type: ignore[attr-defined]
            assert await _result_rows(sm) == []
            assert await _statuses(sm) == {"RUNNING": 1}

            # B's own result still lands — refusing A must not poison the task.
            await handle_task_result(b, result_message(task_id))
            assert b.websocket.outcomes() == [TRANSITIONED]  # type: ignore[attr-defined]
            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


def test_a_result_for_a_task_that_exhausted_its_attempts_is_superseded():
    """Step 3.2 made this reachable: the attempts run out, the task is
    terminally `FAILED`, and the worker that was still computing finishes
    afterwards. Nothing is written — a `FAILED` task must not be resurrected
    into `COMPLETED` — and the worker is told definitively rather than being
    left to retry a result that will never be accepted."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            await handle_task_failed(
                session, {"payload": {"task_id": task_id, "error_type": "ValueError"}}
            )
            assert await _statuses(sm) == {"FAILED": 1}

            await handle_task_result(session, result_message(task_id))

            assert session.websocket.outcomes() == [SUPERSEDED]  # type: ignore[attr-defined]
            assert await _result_rows(sm) == []
            assert await _statuses(sm) == {"FAILED": 1}

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Across replicas, and with no store anywhere
# --------------------------------------------------------------------------


def test_two_replicas_racing_the_same_submission_complete_it_once():
    """The fifth exit criterion. Two coordinator sessions — separate database
    connections, exactly what two replicas are — submit the same retried
    result at the same time. They serialise on the row lock: one writes, the
    other reads a completed task and answers `duplicate`."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=1)
            session = make_session(worker_id)
            register_session(session)
            assert await assign_once() == 1
            task_id = session.websocket.assignments()[0]["payload"]["task_id"]  # type: ignore[attr-defined]
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            envelope = result_message(task_id, token=uuid.uuid4().hex)["payload"]
            envelope["size_bytes"] = 100

            async def replica():
                async with sm() as db:
                    outcome = await complete_task(db, envelope=envelope, worker_id=worker_id)
                    await db.commit()
                    return outcome

            outcomes = await asyncio.gather(replica(), replica())

            assert sorted(outcomes) == sorted([TRANSITIONED, DUPLICATE])
            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


def test_duplicate_suppression_adds_no_store_that_can_grow():
    """The fourth exit criterion, **asserted rather than assumed**. After a
    storm of duplicates and superseded submissions: the schema is exactly the
    tables the migrations create — no seen-token table — and the coordinator's
    Redis holds not one key more than it did before the storm."""

    def _body(sessionmaker):
        async def inner(sm):
            fake = assignment.redis_client
            session, task_id = await _assigned_and_running(sm)
            token = uuid.uuid4().hex

            await handle_task_result(session, result_message(task_id, token=token))
            keys_after_completion = dict(fake.store)  # type: ignore[attr-defined]

            for _ in range(50):
                await handle_task_result(session, result_message(task_id, token=token))
                await handle_task_result(session, result_message(task_id))  # fresh token

            assert dict(fake.store) == keys_after_completion  # type: ignore[attr-defined]
            assert len(await _result_rows(sm)) == 1

            async with sm() as db:
                tables = {
                    row[0]
                    for row in (
                        await db.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = 'public'"
                            )
                        )
                    ).all()
                }
            assert tables == EXPECTED_TABLES

        return inner(sessionmaker)

    run(_body)


def test_the_result_ledger_matches_the_completion_count_exactly():
    """The sixth exit criterion, at unit scale — the same count is taken
    against a real fleet in the phase document's demo. Every task is
    submitted three times and half of them are also submitted by a second,
    losing envelope; the two counts still match."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            await _fill(sm, count=40)
            session = make_session(worker_id, max_concurrent=40)
            register_session(session)
            assert await assign_once() == 40

            task_ids = [
                m["payload"]["task_id"]
                for m in session.websocket.assignments()  # type: ignore[attr-defined]
            ]
            assert len(set(task_ids)) == 40

            for index, task_id in enumerate(task_ids):
                await handle_task_started(session, {"payload": {"task_id": task_id}})
                retry = result_message(task_id, token=uuid.uuid4().hex)
                for _ in range(3):
                    await handle_task_result(session, retry)
                if index % 2 == 0:
                    await handle_task_result(session, result_message(task_id))

            async with sm() as db:
                completed = (
                    await db.execute(
                        text("SELECT count(*) FROM tasks WHERE status = 'COMPLETED'")
                    )
                ).scalar_one()
                stored = (
                    await db.execute(text("SELECT count(*) FROM task_results"))
                ).scalar_one()
                pointed = (
                    await db.execute(
                        text("SELECT count(*) FROM tasks WHERE result_id IS NOT NULL")
                    )
                ).scalar_one()

            assert completed == 40
            assert stored == 40
            assert pointed == 40

        return inner(sessionmaker)

    run(_body)


def test_a_duplicate_whose_body_retention_has_purged_is_superseded_not_accepted():
    """The one honest limit of comparing tokens, named in `complete_task` and
    checked here rather than left to be discovered: the token lives in the
    result body, so retention takes it with the body. A duplicate arriving
    after the retention window can no longer be recognised as one and is
    answered `superseded`.

    Both answers mean the same thing to the worker — stop retrying, nothing is
    owed — and no submission survives seven days in a buffer that lives in
    memory."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            token = uuid.uuid4().hex
            await handle_task_result(session, result_message(task_id, token=token))

            async with sm() as db:
                await db.execute(
                    text("UPDATE task_results SET submitted_at = now() - interval '30 days'")
                )
                await db.commit()
            async with sm() as db:
                assert await purge_expired_results(db, retention_days=7) == 1
                await db.commit()

            await handle_task_result(session, result_message(task_id, token=token))

            assert session.websocket.outcomes() == [TRANSITIONED, SUPERSEDED]  # type: ignore[attr-defined]
            task = await _task_row(sm, task_id)
            assert task["status"] == "COMPLETED"  # the audit trail survives
            assert task["result_id"] is None
            assert await _result_rows(sm) == []

        return inner(sessionmaker)

    run(_body)


def test_a_superseded_result_gives_the_slot_back():
    """§12 cuts both ways here. A worker that loses a race must get its credit
    back — otherwise losing three races quietly retires it — while a worker
    that never held the task must not be able to mint capacity by naming a
    completed one."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm, max_concurrent=2)
            session.credited["another-task"] = "corr-other"
            assert session.in_flight == 2

            await handle_task_result(session, result_message(task_id))
            assert session.in_flight == 1  # the completed task's slot, once

            for _ in range(5):
                await handle_task_result(session, result_message(task_id))

            assert session.in_flight == 1  # and no more, however often it repeats
            assert set(session.credited) == {"another-task"}

        return inner(sessionmaker)

    run(_body)


def test_the_outcome_of_every_suppressed_submission_is_counted():
    """Suppression that is invisible is suppression nobody can audit. Both
    answers land on the shipped result counter, so `duplicate` (a fleet
    retrying itself) and `superseded` (a fleet being reassigned out from under
    itself) can be read side by side without opening a log."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id = await _assigned_and_running(sm)
            before_dup = _metric("coordinator_task_results_total", outcome=DUPLICATE)
            before_sup = _metric("coordinator_task_results_total", outcome=SUPERSEDED)

            token = uuid.uuid4().hex
            await handle_task_result(session, result_message(task_id, token=token))
            await handle_task_result(session, result_message(task_id, token=token))
            await handle_task_result(session, result_message(task_id))

            assert _metric("coordinator_task_results_total", outcome=DUPLICATE) == before_dup + 1
            assert (
                _metric("coordinator_task_results_total", outcome=SUPERSEDED) == before_sup + 1
            )

        return inner(sessionmaker)

    run(_body)
