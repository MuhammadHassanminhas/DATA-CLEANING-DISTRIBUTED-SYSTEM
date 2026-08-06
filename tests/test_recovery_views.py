"""The reads behind the recovery console (Phase 3.7), against real Postgres.

Step 3.7 is a GUI step, and the two things in it that can be tested without
a browser are the ones the browser cannot compensate for: **a fleet-wide
feed of abnormal endings that is ordered and filtered correctly**, and **a
worker-reported failure that finally carries a reason**.

The page itself is verified by the demo in `docs/phase-3-fault-tolerance.md`
§3.7 — a table that renders is not something a Python test can honestly
assert. What is asserted here is everything the page reads, plus the one
behaviour change under it: `handle_task_failed` writing an attempt row.

Postgres-gated like every other module that touches the queue: these are
row writes and an ordered scan, and a fake would prove nothing about
either.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "recovery view tests require Postgres/Redis (set POSTGRES_HOST)",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402 — after the skip guard
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import database_url, redis_url  # noqa: E402
from app.task_queue import (  # noqa: E402
    KNOWN_ATTEMPT_OUTCOMES,
    recent_attempts,
    record_execution_failure,
)

ADMIN = os.environ["ADMIN_SECRET"]
AUTH = {"X-Admin-Secret": ADMIN}
COORDINATOR_DIR = Path(__file__).resolve().parent.parent / "coordinator"


@pytest.fixture(scope="module", autouse=True)
def migrated():
    """Schema first, like `test_execution_protocol.py`.

    The query-level tests here touch the database without going through the
    app, so they cannot rely on the coordinator's lifespan having migrated
    it — and a module that only passes when some earlier module happened to
    run first is not a test, it is a coincidence.
    """
    from alembic import command
    from alembic.config import Config

    prev_cwd = os.getcwd()
    os.chdir(COORDINATOR_DIR)
    try:
        command.upgrade(Config(str(COORDINATOR_DIR / "alembic.ini")), "head")
    finally:
        os.chdir(prev_cwd)


@pytest.fixture(scope="module")
def client():
    from app import assignment
    from app.main import app

    # Same reason `test_operator_api.py` does it: a module-level
    # `asyncio.Event` binds to the first loop that awaits it, and earlier
    # modules have already used theirs under their own `asyncio.run`.
    assignment._work_available = asyncio.Event()

    prev_cwd = os.getcwd()
    os.chdir(COORDINATOR_DIR)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(prev_cwd)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    import redis

    sync = redis.Redis.from_url(redis_url())
    for key in sync.scan_iter(match="*:ratelimit:*"):
        sync.delete(key)
    sync.close()


def db(body):
    """Run `body(sessionmaker)` on its own engine and event loop."""

    async def _main():
        engine = create_async_engine(database_url())
        try:
            return await body(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_main())


def reset():
    """Empty the queue and the fleet.

    `TRUNCATE tasks CASCADE` takes `task_attempts` with it (the foreign key
    is `ON DELETE CASCADE`), which is what makes each test's feed exactly
    the rows it wrote.
    """

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(text("TRUNCATE tasks CASCADE"))
            await session.execute(text("DELETE FROM workers"))
            await session.commit()

    db(body)


def seed_worker() -> str:
    worker_id = str(uuid.uuid4())

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO workers (id, status, revoked, created_at, updated_at) "
                    "VALUES (CAST(:id AS uuid), 'ONLINE', false, now(), now())"
                ),
                {"id": worker_id},
            )
            await session.commit()

    db(body)
    return worker_id


def seed_task(*, worker_id: str | None = None, status: str = "RUNNING") -> str:
    task_id = str(uuid.uuid4())

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO tasks (id, task_type, parameters, priority, status, "
                    "assigned_worker_id, attempt_count, correlation_id, created_at, updated_at) "
                    "VALUES (CAST(:id AS uuid), 'count_to_n', '{\"n\": 5}', 0, :status, "
                    "CAST(:worker AS uuid), 0, :corr, now(), now())"
                ),
                {
                    "id": task_id,
                    "status": status,
                    "worker": worker_id,
                    "corr": f"corr-{task_id}",
                },
            )
            await session.commit()

    db(body)
    return task_id


def seed_attempt(*, task_id: str, worker_id: str | None, outcome: str, reason: str, ago: int = 0):
    """One attempt row, `ago` seconds in the past.

    The offset is what makes the ordering assertion mean something: rows
    written in one statement share `recorded_at` to the microsecond.
    """

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO task_attempts "
                    "(id, task_id, attempt_number, worker_id, outcome, reason, "
                    " correlation_id, recorded_at) "
                    "VALUES (gen_random_uuid(), CAST(:task AS uuid), 0, CAST(:worker AS uuid), "
                    ":outcome, :reason, :corr, now() - make_interval(secs => :ago))"
                ),
                {
                    "task": task_id,
                    "worker": worker_id,
                    "outcome": outcome,
                    "reason": reason,
                    "corr": f"corr-{task_id}",
                    "ago": ago,
                },
            )
            await session.commit()

    db(body)


# --------------------------------------------------------------------------
# The feed itself — "reassignment events visible in real time"
# --------------------------------------------------------------------------


def test_the_feed_is_newest_first_across_every_task():
    """The property the per-task read cannot provide.

    `task_attempts(task_id=...)` answers oldest-first for one task, which is
    a timeline. A console watching a fleet needs the opposite ordering over
    every task at once, because the newest event is the one that has not
    been read yet.
    """
    reset()
    worker = seed_worker()
    old_task, new_task = seed_task(worker_id=worker), seed_task(worker_id=worker)
    seed_attempt(task_id=old_task, worker_id=worker, outcome="REASSIGNED", reason="lease_expired", ago=120)
    seed_attempt(task_id=new_task, worker_id=worker, outcome="FENCED", reason="stale_attempt", ago=1)

    async def body(sessionmaker):
        async with sessionmaker() as session:
            return await recent_attempts(session, limit=50)

    rows = db(body)
    assert [str(row["task_id"]) for row in rows] == [new_task, old_task]


def test_the_feed_filters_by_outcome_and_by_worker():
    reset()
    noisy, quiet = seed_worker(), seed_worker()
    task = seed_task(worker_id=noisy)
    other = seed_task(worker_id=quiet)
    seed_attempt(task_id=task, worker_id=noisy, outcome="REASSIGNED", reason="lease_expired", ago=3)
    seed_attempt(task_id=task, worker_id=noisy, outcome="FENCED", reason="task_reassigned", ago=2)
    seed_attempt(task_id=other, worker_id=quiet, outcome="REASSIGNED", reason="lease_expired", ago=1)

    async def body(sessionmaker):
        async with sessionmaker() as session:
            return (
                await recent_attempts(session, outcomes=["FENCED"]),
                await recent_attempts(session, worker_id=quiet),
                await recent_attempts(session, outcomes=["REASSIGNED"], worker_id=noisy),
            )

    fenced, by_worker, both = db(body)
    assert [row["outcome"] for row in fenced] == ["FENCED"]
    assert [str(row["worker_id"]) for row in by_worker] == [quiet]
    assert len(both) == 1 and str(both[0]["task_id"]) == task


def test_a_worker_id_that_is_not_a_uuid_returns_nothing_rather_than_raising():
    """The query layer refuses to build SQL around a value it cannot parse.

    The endpoint answers 400 before reaching here; this is the second line,
    for any future caller that does not.
    """
    reset()

    async def body(sessionmaker):
        async with sessionmaker() as session:
            return await recent_attempts(session, worker_id="not-a-uuid")

    assert db(body) == []


# --------------------------------------------------------------------------
# `GET /tasks/attempts`
# --------------------------------------------------------------------------


def test_the_feed_requires_the_operator_credential(client):
    assert client.get("/tasks/attempts").status_code == 401
    assert client.get("/tasks/attempts", headers={"X-Admin-Secret": "wrong"}).status_code == 401


def test_an_unknown_outcome_is_a_400_not_an_empty_feed(client):
    """A typo must not read as "nothing is going wrong".

    The same call `GET /tasks` makes for an unknown status, and it matters
    more here: this console is opened precisely when something is suspected,
    so an empty answer is the most misleading possible response.
    """
    response = client.get("/tasks/attempts", params={"outcome": "REASIGNED"}, headers=AUTH)
    assert response.status_code == 400
    assert "REASIGNED" in response.json()["detail"]
    for known in KNOWN_ATTEMPT_OUTCOMES:
        assert known in response.json()["detail"]


def test_a_malformed_worker_filter_is_a_400(client):
    response = client.get("/tasks/attempts", params={"worker_id": "nope"}, headers=AUTH)
    assert response.status_code == 400


def test_the_feed_is_capped_by_the_list_limit_rather_than_a_second_tunable(client):
    """`TASK_LIST_MAX_LIMIT`, not a new setting that can disagree with it."""
    from app.config import task_list_max_limit

    response = client.get("/tasks/attempts", params={"limit": 100000}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["limit"] == task_list_max_limit()


def test_the_endpoint_renders_every_field_the_console_reads(client):
    reset()
    worker = seed_worker()
    task = seed_task(worker_id=worker)
    seed_attempt(task_id=task, worker_id=worker, outcome="REASSIGNED", reason="lease_expired")

    body = client.get("/tasks/attempts", headers=AUTH).json()
    assert body["count"] == 1
    event = body["attempts"][0]
    assert event["task_id"] == task
    assert event["worker_id"] == worker
    assert event["outcome"] == "REASSIGNED"
    assert event["reason"] == "lease_expired"
    assert event["attempt_number"] == 0
    assert event["correlation_id"] == f"corr-{task}"
    # Rendered as a string the browser can pass to `new Date()`, like every
    # other timestamp this API returns.
    assert event["recorded_at"].startswith("20")
    assert uuid.UUID(event["attempt_id"])


# --------------------------------------------------------------------------
# The reason a worker-reported failure never had
# --------------------------------------------------------------------------


def test_an_executor_failure_is_recorded_with_its_exception_type():
    """"Failed tasks are inspectable with a reason" — for *both* failures.

    Before this step a task that exhausted its attempts carried a full
    attempt row and a task whose executor raised carried nothing at all, so
    the GUI could show the second kind only as the word "failed".
    """
    reset()
    worker = seed_worker()
    task = seed_task(worker_id=worker, status="FAILED")

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await record_execution_failure(
                session,
                task_id=task,
                worker_id=worker,
                error_type="ValueError",
                correlation_id=f"corr-{task}",
            )
            await session.commit()
        async with sessionmaker() as session:
            return await recent_attempts(session)

    rows = db(body)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FAILED"
    assert rows[0]["reason"] == "executor_error:ValueError"
    # Read from the task row, never from the worker's message (§12).
    assert rows[0]["attempt_number"] == 0


def test_recording_the_same_execution_failure_twice_writes_one_row():
    """Idempotency is mandatory (§3.7), including on the recovery record."""
    reset()
    worker = seed_worker()
    task = seed_task(worker_id=worker, status="FAILED")

    async def body(sessionmaker):
        for _ in range(3):
            async with sessionmaker() as session:
                await record_execution_failure(
                    session, task_id=task, worker_id=worker, error_type="ValueError"
                )
                await session.commit()
        async with sessionmaker() as session:
            return await recent_attempts(session)

    assert len(db(body)) == 1


def test_a_reason_is_bounded_and_carries_no_message():
    """§12: never a traceback, never payload data, and never unbounded.

    `handle_task_failed` already caps the exception type at 100 characters;
    this caps what reaches the row regardless of who calls it, because the
    column is read straight into a GUI.
    """
    reset()
    worker = seed_worker()
    task = seed_task(worker_id=worker, status="FAILED")

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await record_execution_failure(
                session, task_id=task, worker_id=worker, error_type="E" * 500
            )
            await session.commit()
        async with sessionmaker() as session:
            return await recent_attempts(session)

    reason = db(body)[0]["reason"]
    assert len(reason) == 120
    assert reason.startswith("executor_error:")


def test_a_failure_for_a_task_that_does_not_exist_writes_nothing():
    """The row is built from the task, so no task means no row — rather than
    an attempt row pointing at nothing, which the foreign key would refuse
    anyway and which would surface as a 500 on a worker's message."""
    reset()
    worker = seed_worker()

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await record_execution_failure(
                session, task_id=str(uuid.uuid4()), worker_id=worker, error_type="ValueError"
            )
            await session.commit()
        async with sessionmaker() as session:
            return await recent_attempts(session)

    assert db(body) == []


def test_the_failure_reason_reaches_the_task_detail_the_drawer_reads(client):
    """The console opens `GET /tasks/{id}`; the reason has to be there too."""
    reset()
    worker = seed_worker()
    task = seed_task(worker_id=worker, status="FAILED")

    async def body(sessionmaker):
        async with sessionmaker() as session:
            await record_execution_failure(
                session, task_id=task, worker_id=worker, error_type="ZeroDivisionError"
            )
            await session.commit()

    db(body)
    detail = client.get(f"/tasks/{task}", headers=AUTH).json()
    assert [a["reason"] for a in detail["attempts"]] == ["executor_error:ZeroDivisionError"]
