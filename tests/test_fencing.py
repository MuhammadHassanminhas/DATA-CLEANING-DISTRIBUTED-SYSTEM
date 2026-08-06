"""Stale result fencing (Phase 3.4), against a real Postgres.

Postgres-gated for the reason `test_idempotency.py` is, plus one that
belongs to this step: **the fencing comparison and the row it writes happen
inside the same `FOR UPDATE` lock**, and the `NOT EXISTS` that bounds the
attempt table is only race-free because of it. A fake would be testing a
Python transcription of a rule whose content is "one statement, one lock,
one row".

What each part of the file proves, against Step 3.4's six exit criteria:

* **A superseded attempt's result is rejected**, reproduced deliberately in
  both shapes it has — the same worker holding the task again under a newer
  attempt number, and a worker the task has been reassigned away from while
  it is still live.
* **A result submitted under a NEWER session epoch than it was executed
  under is ACCEPTED.** Step 2.5 measured that path; the amended criterion
  (Decision #169) asks for it to be asserted rather than assumed, because
  the original wording would have broken it.
* **Rejected results leave task state untouched** — checked column by
  column, not by looking at `status` alone.
* **The worker handles the rejection gracefully**, checked against the
  shipped worker code rather than a description of it.
* **Rejections are visible on the dashboard**, which here means the two
  reads the task console actually performs: `GET /tasks/{id}`'s attempt
  list and `GET /tasks/depth`'s fenced count.
* **No protocol change**, asserted by sending nothing the Phase 2.5
  envelope did not already carry and by checking the ack's shape.

Two properties are proved beyond the criteria because §12 makes them
load-bearing: an impostor still gets `not_owner` and writes nothing, and a
worker cannot make the coordinator grow `task_attempts` without bound by
resubmitting.

Clocks are written, never waited on — the same discipline `test_retry_policy`
and `test_idempotency` use.
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
        "fencing tests require Postgres (set POSTGRES_HOST)",
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
    handle_task_result,
    handle_task_started,
    register_session,
    unregister_session,
)
from app.config import database_url  # noqa: E402
from app.task_queue import (  # noqa: E402
    DUPLICATE,
    FENCED,
    FENCED_OUTCOME,
    NOT_OWNER,
    REASON_STALE_ATTEMPT,
    REASON_TASK_REASSIGNED,
    SUPERSEDED,
    TRANSITIONED,
    complete_task,
    enqueue,
    fenced_result_count,
    reclaim_expired_leases,
    task_attempts,
)

ALL_TYPES = ("count_to_n", "hash_rounds", "sleep", "opaque_payload")
RECLAIM_BATCH = 100


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


def make_session(worker_id, *, max_concurrent: int = 4, session_epoch: int = 1) -> LocalSession:
    return LocalSession(
        worker_id=str(worker_id),
        session_epoch=session_epoch,
        websocket=FakeSocket(),  # type: ignore[arg-type]
        send_lock=asyncio.Lock(),
        max_concurrent=max_concurrent,
        supported_task_types=ALL_TYPES,
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
        await session.execute(text("TRUNCATE task_attempts CASCADE"))
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


async def _result_rows(sessionmaker) -> list[dict]:
    async with sessionmaker() as db:
        return [
            dict(row)
            for row in (
                await db.execute(text("SELECT * FROM task_results"))
            ).mappings().all()
        ]


async def _attempts(sessionmaker, task_id) -> list[dict]:
    async with sessionmaker() as db:
        return await task_attempts(db, task_id=str(task_id))


async def _fences(sessionmaker, task_id) -> list[dict]:
    return [a for a in await _attempts(sessionmaker, task_id) if a["outcome"] == FENCED_OUTCOME]


async def _fenced_total(sessionmaker) -> int:
    async with sessionmaker() as db:
        return await fenced_result_count(db)


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


async def _reclaim(sessionmaker, task_id) -> None:
    """Expire the lease and let the reclaimer requeue the task."""
    await _expire(sessionmaker, task_id)
    async with sessionmaker() as db:
        assert len(await reclaim_expired_leases(db, batch=RECLAIM_BATCH)) == 1
        await db.commit()
    await _backoff_elapsed(sessionmaker, task_id)


def result_message(task_id: str, *, attempt: int = 0, epoch: int = 1, **overrides) -> dict:
    """One `task_result` message, carrying **only** Phase 2.5's fields.

    `attempt` is named rather than defaulted invisibly: this whole step turns
    on whether it matches the task's `attempt_count`, so a test that means
    "the current attempt" has to say which one that is.
    """
    payload = {
        "task_id": str(task_id),
        "status": "COMPLETED",
        "attempt_number": attempt,
        "session_epoch": epoch,
        "idempotency_token": uuid.uuid4().hex,
        "duration_seconds": 1.5,
        "result": 5,
    }
    payload.update(overrides)
    return {"correlation_id": "corr-result", "payload": payload}


async def _assigned_and_running(sessionmaker, *, worker_id=None):
    """One task delivered to one worker and reported started. Returns the
    session, the task id, and the attempt number the coordinator delivered."""
    if worker_id is None:
        worker_id = (await _reset(sessionmaker))[0]
    await _fill(sessionmaker, count=1)
    session = make_session(worker_id)
    register_session(session)
    assert await assign_once() == 1
    delivered = session.websocket.assignments()[0]["payload"]  # type: ignore[attr-defined]
    await handle_task_started(session, {"payload": {"task_id": delivered["task_id"]}})
    return session, delivered["task_id"], delivered["attempt"]


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


# --------------------------------------------------------------------------
# Criterion 1 — a result from a superseded attempt is rejected
# --------------------------------------------------------------------------


def test_a_stale_attempt_is_fenced_when_the_same_worker_holds_the_task_again():
    """**The case Step 3.3 explicitly left accepted**, and the reason 3.4
    exists at all.

    One worker, one task. The lease expires, the reclaimer requeues it, and
    because the exclusion window is bounded (Step 3.2 — a permanent exclusion
    starves a task to death on a one-worker fleet) the *same* worker takes it
    back as attempt 1. Its original attempt-0 execution then finishes.

    Ownership cannot catch this: the row names exactly the worker that is
    submitting. Only the attempt number can."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, first_attempt = await _assigned_and_running(sm)
            assert first_attempt == 0

            # The lease runs out and the task comes back to the same worker.
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            second = session.websocket.assignments()[1]["payload"]  # type: ignore[attr-defined]
            assert second["task_id"] == task_id
            assert second["attempt"] == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            # The attempt-0 execution, which never stopped, now finishes.
            await handle_task_result(session, result_message(task_id, attempt=0))

            assert session.websocket.outcomes() == [FENCED]  # type: ignore[attr-defined]
            assert await _result_rows(sm) == []
            assert (await _task_row(sm, task_id))["status"] == "RUNNING"

            fences = await _fences(sm, task_id)
            assert len(fences) == 1
            assert fences[0]["attempt_number"] == 0
            assert fences[0]["reason"] == REASON_STALE_ATTEMPT
            assert str(fences[0]["worker_id"]) == session.worker_id

            # And attempt 1 — the one the worker is genuinely running — still
            # completes. Fencing must refuse a submission, not poison a task.
            await handle_task_result(session, result_message(task_id, attempt=1))
            assert session.websocket.outcomes() == [FENCED, TRANSITIONED]  # type: ignore[attr-defined]
            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


def test_a_result_from_a_worker_the_task_was_reassigned_away_from_is_fenced():
    """The other shape, and the one that changes a shipped answer.

    A is delivered the task and is still computing; the lease expires; B
    takes it and is still running it when A's result arrives. Before 3.4 this
    was `not_owner` — the answer §12 reserves for an impostor — with no
    durable record that a real result had been thrown away. It is now
    `fenced`, with one attempt row saying so."""

    def _body(sessionmaker):
        async def inner(sm):
            a_id, b_id = await _reset(sm, workers=2)
            a, task_id, _ = await _assigned_and_running(sm, worker_id=a_id)

            # A stops answering. Its socket goes, so the reclaimed task is not
            # simply handed straight back to it.
            await _reclaim(sm, task_id)
            unregister_session(a.worker_id, a.session_epoch)
            b = make_session(b_id)
            register_session(b)
            assert await assign_once() == 1
            delivered = b.websocket.assignments()[0]["payload"]  # type: ignore[attr-defined]
            assert delivered["task_id"] == task_id
            assert delivered["attempt"] == 1
            await handle_task_started(b, {"payload": {"task_id": task_id}})

            # A finishes the work it never stopped doing.
            await handle_task_result(a, result_message(task_id, attempt=0))

            assert a.websocket.outcomes() == [FENCED]  # type: ignore[attr-defined]
            assert await _result_rows(sm) == []

            fences = await _fences(sm, task_id)
            assert len(fences) == 1
            assert fences[0]["reason"] == REASON_TASK_REASSIGNED
            assert str(fences[0]["worker_id"]) == str(a_id)

            # B's own result still lands.
            await handle_task_result(b, result_message(task_id, attempt=1))
            assert b.websocket.outcomes() == [TRANSITIONED]  # type: ignore[attr-defined]
            assert len(await _result_rows(sm)) == 1

        return inner(sessionmaker)

    run(_body)


def test_a_late_result_for_a_task_another_attempt_completed_stays_superseded():
    """The boundary between 3.3 and 3.4, asserted so it cannot drift.

    Fencing applies to a task that is still **live**. Once some attempt has
    completed the task, the terminal answer is the honest one and it is
    reached first — a late result there is `superseded`, exactly as Step 3.3
    measured, and no fence row is written for it. If this ever flipped to
    `fenced`, the fenced count would start including work that had already
    been accounted for."""

    def _body(sessionmaker):
        async def inner(sm):
            a_id, b_id = await _reset(sm, workers=2)
            a, task_id, _ = await _assigned_and_running(sm, worker_id=a_id)

            await _reclaim(sm, task_id)
            unregister_session(a.worker_id, a.session_epoch)
            b = make_session(b_id)
            register_session(b)
            assert await assign_once() == 1
            await handle_task_started(b, {"payload": {"task_id": task_id}})
            await handle_task_result(b, result_message(task_id, attempt=1))

            await handle_task_result(a, result_message(task_id, attempt=0))

            assert a.websocket.outcomes() == [SUPERSEDED]  # type: ignore[attr-defined]
            assert await _fences(sm, task_id) == []
            assert await _fenced_total(sm) == 0

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Criterion 2 — a newer session epoch is ACCEPTED, not fenced
# --------------------------------------------------------------------------


def test_a_result_submitted_under_a_newer_session_epoch_than_it_ran_under_is_accepted():
    """**The amended criterion (Decision #169), and the reason it was
    amended.**

    Step 2.5 measured a legitimate result executed under session epoch 4 and
    submitted on 5, after a reconnect — a worker whose socket died mid-task
    finishes the work and reports it on the new connection. The phase plan's
    original wording ("a result from an old session epoch is rejected")
    would have made that measured, correct behaviour a rejection.

    So `session_epoch` is deliberately not a fencing input, and this asserts
    it against a submission that disagrees with its session in both
    directions at once: an epoch older than the session's *and* a session
    reconnected under a new epoch."""

    def _body(sessionmaker):
        async def inner(sm):
            worker_id = (await _reset(sm))[0]
            first, task_id, attempt = await _assigned_and_running(sm, worker_id=worker_id)

            # The socket dies and the worker comes back under a new epoch,
            # still executing the task the coordinator still says is its own.
            unregister_session(first.worker_id, first.session_epoch)
            reconnected = make_session(worker_id, session_epoch=5)
            register_session(reconnected)

            # The envelope reports the epoch the work RAN under, which is the
            # old one. Nothing about that makes the result invalid.
            await handle_task_result(
                reconnected, result_message(task_id, attempt=attempt, epoch=4)
            )

            assert reconnected.websocket.outcomes() == [TRANSITIONED]  # type: ignore[attr-defined]
            rows = await _result_rows(sm)
            assert len(rows) == 1
            assert rows[0]["payload"]["session_epoch"] == 4
            assert (await _task_row(sm, task_id))["status"] == "COMPLETED"
            assert await _fenced_total(sm) == 0

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Criterion 3 — a rejected result leaves task state untouched
# --------------------------------------------------------------------------


def test_a_fenced_result_changes_no_column_of_the_task_at_all():
    """Checked column by column rather than by reading `status`.

    "Leaves task state untouched" has to mean the assignee, the lease, the
    deadline, the attempt count, the result pointer and the completion stamp
    — a fence that quietly cleared a lease would pass a status assertion and
    hand the task to the reclaimer a lease early. `updated_at` is in the
    list on purpose: a refusal is not an update, and a row whose
    `updated_at` moves every time a worker retries a doomed submission tells
    an operator the task is progressing when it is not."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            before = await _task_row(sm, task_id)
            await handle_task_result(session, result_message(task_id, attempt=0))
            after = await _task_row(sm, task_id)

            watched = (
                "status",
                "assigned_worker_id",
                "assigned_at",
                "started_at",
                "completed_at",
                "result_id",
                "attempt_count",
                "lease_expires_at",
                "deadline_at",
                "not_before",
                "excluded_worker_id",
                "updated_at",
            )
            assert {k: after[k] for k in watched} == {k: before[k] for k in watched}
            assert await _result_rows(sm) == []

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Criterion 4 — the worker handles the rejection without crashing
# --------------------------------------------------------------------------


def test_the_ack_is_definitive_and_the_slot_comes_back():
    """What the worker is actually told, and what it costs it.

    `accepted: false` is what makes the worker drop the pending result
    instead of retrying a verdict that will never change, and the credit
    coming back is what stops losing a race from costing the worker a slot
    for the life of its session — the defect Decision #168 fixed on the
    neighbouring path, which `fenced` would otherwise reintroduce."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})
            assert session.in_flight == 1

            await handle_task_result(session, result_message(task_id, attempt=0))

            ack = session.websocket.acks()[-1]  # type: ignore[attr-defined]
            assert ack["message_type"] == "task_result_ack"
            assert ack["payload"]["outcome"] == FENCED
            assert ack["payload"]["accepted"] is False
            assert ack["payload"]["task_id"] == task_id
            # The shape Phase 2.5 defined, unchanged: no field was added to
            # the ack to carry the fence (criterion 6).
            assert set(ack["payload"]) == {"task_id", "accepted", "outcome"}

            assert session.in_flight == 0
            assert task_id not in session.credited

        return inner(sessionmaker)

    run(_body)


def test_the_shipped_worker_drops_a_fenced_result_without_crashing():
    """Criterion 4 against the worker's own code, not a description of it.

    The worker's ack handler was written in Phase 2.5 to drop a pending
    result on **any** ack, accepted or not, precisely so that a new refusal
    outcome would need no worker change. This is the assertion that the
    claim "no worker change was required" is true rather than assumed."""
    os.environ.setdefault("COORDINATOR_URL", "https://example.invalid")
    os.environ.setdefault("WORKER_IDENTITY_FILE", "/tmp/test-worker-identity.json")
    from worker import worker as worker_mod

    runner = worker_mod.TaskRunner(max_concurrent=2)
    task_id = str(uuid.uuid4())
    runner.pending_results[task_id] = {
        "message_type": "task_result",
        "payload": {"task_id": task_id},
    }

    asyncio.run(
        worker_mod._handle_result_ack(
            {"worker_id": "w"},
            runner,
            {
                "message_type": "task_result_ack",
                "correlation_id": "c",
                "payload": {"task_id": task_id, "accepted": False, "outcome": FENCED},
            },
        )
    )

    assert runner.pending_results == {}


# --------------------------------------------------------------------------
# Criterion 5 — rejections are visible on the dashboard, not buried in logs
# --------------------------------------------------------------------------


def test_a_fence_is_on_both_reads_the_task_console_performs():
    """The two API responses the console renders, checked as the console
    reads them: the per-task attempt list in the detail drawer, and the
    fenced count on the summary tiles.

    A fence leaves no trace in any lifecycle state — that is the whole point
    of "nothing is written" — so without these two it would be discoverable
    only by grepping one replica's logs, which is what the criterion
    forbids."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            assert await _fenced_total(sm) == 0

            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})
            await handle_task_result(session, result_message(task_id, attempt=0))

            # The drawer's list. `REASSIGNED` from the reclaim and `FENCED`
            # from the refused result are both there — two different facts
            # about the same attempt, and neither hides the other.
            rows = await _attempts(sm, task_id)
            assert [r["outcome"] for r in rows] == ["REASSIGNED", FENCED_OUTCOME]
            assert rows[1]["reason"] == REASON_STALE_ATTEMPT
            # §11: the fence is traceable in the task's own thread.
            assert rows[1]["correlation_id"] == (await _task_row(sm, task_id))["correlation_id"]

            # The tile.
            assert await _fenced_total(sm) == 1

        return inner(sessionmaker)

    run(_body)


def test_both_fenced_metrics_move_and_the_reason_label_separates_the_two_causes():
    """Two series, and the gate asked for both (§3.0.9).

    `coordinator_task_results_total{outcome="fenced"}` comes free from the
    counter Phase 2.5 created and answers "what happened to submissions",
    read beside `duplicate` and `superseded`.
    `coordinator_results_fenced_total{reason}` answers "why work is being
    thrown away", and only it carries the reason — which matters because
    `stale_attempt` (a worker racing itself, so the lease TTL is short for
    the work) and `task_reassigned` (workers dropping off) have different
    causes and different fixes.

    Both reasons are produced here in one run, so the labels are proved to
    separate rather than both landing on whichever branch ran last."""

    def _body(sessionmaker):
        async def inner(sm):
            before_outcome = _metric("coordinator_task_results_total", outcome=FENCED)
            before_stale = _metric(
                "coordinator_results_fenced_total", reason=REASON_STALE_ATTEMPT
            )
            before_moved = _metric(
                "coordinator_results_fenced_total", reason=REASON_TASK_REASSIGNED
            )

            # (1) stale attempt — the same worker gets the task back.
            session, task_id, _ = await _assigned_and_running(sm)
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})
            await handle_task_result(session, result_message(task_id, attempt=0))

            # (2) reassigned — a second worker takes it instead.
            a_id, b_id = await _reset(sm, workers=2)
            a, other_id, _ = await _assigned_and_running(sm, worker_id=a_id)
            await _reclaim(sm, other_id)
            unregister_session(a.worker_id, a.session_epoch)
            b = make_session(b_id)
            register_session(b)
            assert await assign_once() == 1
            await handle_task_started(b, {"payload": {"task_id": other_id}})
            await handle_task_result(a, result_message(other_id, attempt=0))

            assert (
                _metric("coordinator_task_results_total", outcome=FENCED)
                == before_outcome + 2
            )
            assert (
                _metric("coordinator_results_fenced_total", reason=REASON_STALE_ATTEMPT)
                == before_stale + 1
            )
            assert (
                _metric(
                    "coordinator_results_fenced_total", reason=REASON_TASK_REASSIGNED
                )
                == before_moved + 1
            )

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# §12 — fencing must not become a lever for an untrusted worker
# --------------------------------------------------------------------------


def test_an_impostor_naming_another_workers_live_task_still_gets_not_owner():
    """`NOT_OWNER` is narrowed by 3.4, not retired, and this is the meaning
    it keeps.

    B names A's task. B has never held it, so there is no attempt row naming
    B and the coordinator says so: `not_owner`, no fence row, no credit
    released, and A's task still `RUNNING` with its lease intact. If this
    answered `fenced` instead, an untrusted worker could make the
    coordinator write rows about tasks it was never given."""

    def _body(sessionmaker):
        async def inner(sm):
            a_id, b_id = await _reset(sm, workers=2)
            a, task_id, attempt = await _assigned_and_running(sm, worker_id=a_id)

            b = make_session(b_id)
            register_session(b)
            before = await _task_row(sm, task_id)
            await handle_task_result(b, result_message(task_id, attempt=attempt))

            assert b.websocket.outcomes() == [NOT_OWNER]  # type: ignore[attr-defined]
            assert await _attempts(sm, task_id) == []
            assert await _fenced_total(sm) == 0
            assert await _result_rows(sm) == []
            assert await _task_row(sm, task_id) == before

            # A's own result is unaffected.
            await handle_task_result(a, result_message(task_id, attempt=attempt))
            assert a.websocket.outcomes() == [TRANSITIONED]  # type: ignore[attr-defined]

        return inner(sessionmaker)

    run(_body)


def test_resubmitting_the_same_fenced_result_writes_exactly_one_attempt_row():
    """The bound, and it is not tidiness.

    A worker that legitimately holds one task can submit its stale result as
    many times as it likes — the pending-result loop retries until it is
    acked, and a hostile one need not stop at all. One row per (task, worker,
    attempt) is what keeps `task_attempts` a function of the work rather than
    of how loudly a worker talks. The `NOT EXISTS` that enforces it is inside
    the caller's row lock, so concurrent submissions cannot both pass it."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            for _ in range(20):
                await handle_task_result(session, result_message(task_id, attempt=0))

            assert session.websocket.outcomes() == [FENCED] * 20  # type: ignore[attr-defined]
            assert len(await _fences(sm, task_id)) == 1
            assert await _fenced_total(sm) == 1

        return inner(sessionmaker)

    run(_body)


def test_an_attempt_number_the_task_has_never_reached_is_fenced_without_a_row():
    """A submission claiming attempt 900 of a task on attempt 0 is refused
    like any other — but nothing durable records an attempt that never
    happened.

    Two reasons, and the second is the load-bearing one: a `task_attempts`
    row is a history, and a coordinator that writes worker-invented history
    is worse than one that writes none; and `attempt_number` is unbounded
    worker input, so a row per value is a table a worker can grow at will
    (§12)."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)

            await handle_task_result(session, result_message(task_id, attempt=900))

            assert session.websocket.outcomes() == [FENCED]  # type: ignore[attr-defined]
            assert await _attempts(sm, task_id) == []
            assert await _fenced_total(sm) == 0
            assert await _result_rows(sm) == []
            assert (await _task_row(sm, task_id))["status"] == "RUNNING"

        return inner(sessionmaker)

    run(_body)


# --------------------------------------------------------------------------
# Criterion 6 — no protocol change, and no new store
# --------------------------------------------------------------------------


def test_fencing_reads_only_fields_phase_2_5_already_put_on_the_wire():
    """The whole rule is `assigned_worker_id` (Phase 2.1) versus the sender,
    and `attempt_count` (Phase 2.1, written from Phase 3.2) versus
    `attempt_number` (Phase 2.5). Called directly rather than through a
    session so that what the decision depends on is the envelope alone."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            envelope = result_message(task_id, attempt=0)["payload"]
            envelope["size_bytes"] = 100

            async with sm() as db:
                assert await complete_task(
                    db, envelope=envelope, worker_id=session.worker_id
                ) == TRANSITIONED
                await db.commit()

            # Same envelope again: terminal now, same token — 3.3's answer,
            # unchanged by 3.4.
            async with sm() as db:
                assert await complete_task(
                    db, envelope=envelope, worker_id=session.worker_id
                ) == DUPLICATE

        return inner(sessionmaker)

    run(_body)


def test_a_storm_of_fenced_submissions_creates_no_redis_key_and_no_table():
    """Step 3.3 proved there is no dedup store; 3.4 must not quietly add one.

    The fence's memory is the `task_attempts` row, which is a table that
    already existed and is bounded above. Nothing is cached, nothing expires,
    and there is no key with a retention window to tune."""

    def _body(sessionmaker):
        async def inner(sm):
            session, task_id, _ = await _assigned_and_running(sm)
            await _reclaim(sm, task_id)
            assert await assign_once() == 1
            await handle_task_started(session, {"payload": {"task_id": task_id}})

            keys_before = set(assignment.redis_client.store)
            for _ in range(20):
                await handle_task_result(session, result_message(task_id, attempt=0))

            # **No key is ADDED**, which is the claim. The set is allowed to
            # shrink and does: `worker:{id}:current_tasks` is the shipped
            # Phase 2.3 telemetry mirror, and the first fence releases the
            # credit, so this session correctly stops claiming to be running
            # anything. Asserting equality here would be asserting that the
            # credit was *not* released, which is the opposite of what the
            # step wants.
            assert set(assignment.redis_client.store) - keys_before == set()

            async with sm() as db:
                tables = {
                    row[0]
                    for row in (
                        await db.execute(
                            text(
                                "SELECT tablename FROM pg_tables "
                                "WHERE schemaname = 'public'"
                            )
                        )
                    ).all()
                }
            assert tables == {
                "alembic_version",
                "workers",
                "tasks",
                "task_results",
                "task_attempts",
                "task_policies",
            }

        return inner(sessionmaker)

    run(_body)
