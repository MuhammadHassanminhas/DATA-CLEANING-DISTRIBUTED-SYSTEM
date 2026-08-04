"""Operator task API (Phase 2.6), against a real Postgres + Redis.

Runs in CI where GitHub Actions `services:` provide both; skipped locally
unless POSTGRES_HOST is set. These are HTTP tests on purpose — the step's
exit criteria are about an API an operator drives, so the thing under test
is the endpoint, not the queue function behind it.

Two things are exercised through the database rather than the API,
deliberately: moving a task to RUNNING and completing it. Both are worker
messages over a WebSocket in production (Steps 2.4 and 2.5, already
covered by their own suites), and standing that up here would test the
protocol again instead of testing the lifecycle read.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "operator API tests require Postgres/Redis (set POSTGRES_HOST)",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402 — after the skip guard
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import database_url, redis_url  # noqa: E402
from app.task_queue import complete_task, mark_status  # noqa: E402

ADMIN = os.environ["ADMIN_SECRET"]
AUTH = {"X-Admin-Secret": ADMIN}
COORDINATOR_DIR = Path(__file__).resolve().parent.parent / "coordinator"


@pytest.fixture(scope="module")
def client():
    from app import assignment
    from app.main import app

    # `assignment._work_available` is a module-level asyncio.Event, and an
    # Event binds itself to the first event loop that awaits it. Earlier test
    # modules await it inside their own `asyncio.run`, so by the time this
    # module starts the app the object is bound to a loop that is long gone —
    # and `assignment_loop` dies on its first wait, surfacing as an error at
    # lifespan shutdown. A fresh Event is bound by whichever loop reaches it
    # first, which here is TestClient's.
    #
    # This is a property of running one process across many event loops, which
    # only the suite does: a deployed coordinator has one loop for the life of
    # the process, so the production path is unaffected and is not being
    # worked around here.
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
    """Empty every rate-limit bucket before each test.

    TestClient presents no routable address, so every request in this module
    shares one bucket per scope (`taskapi:ratelimit:testclient`,
    `register:ratelimit:testclient`). Without this the module's own request
    volume would trip limits no test asked for — registration defaults to
    five per minute and several tests enroll a worker — and the one test
    that *does* exercise the limiter could never be sure the counter started
    at zero.
    """
    import redis

    sync = redis.Redis.from_url(redis_url())
    for key in sync.scan_iter(match="*:ratelimit:*"):
        sync.delete(key)
    sync.close()


def db(body):
    """Run `body(sessionmaker)` on its own engine and event loop.

    Separate from `app.db.engine` for the same reason `test_task_queue.py`
    is: that engine binds its pool to the first loop that touches it, which
    here is TestClient's.
    """

    async def _main():
        engine = create_async_engine(database_url())
        try:
            return await body(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_main())


def reset_tasks():
    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(text("TRUNCATE tasks CASCADE"))
            await session.commit()

    db(body)


def register_worker(client) -> str:
    r = client.post(
        "/workers/register",
        json={"enrollment_secret": os.environ["ENROLLMENT_SECRET"], "agent_version": "2.6-test"},
    )
    assert r.status_code == 201, r.text
    return r.json()["worker_id"]


def enqueue(client, task_type="count_to_n", parameters=None, count=1, priority=0) -> dict:
    r = client.post(
        "/tasks",
        headers=AUTH,
        json={
            "task_type": task_type,
            "parameters": parameters if parameters is not None else {"n": 5},
            "count": count,
            "priority": priority,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def one_task_id(client, **kwargs) -> str:
    body = enqueue(client, **kwargs)
    assert body["task_ids"] and len(body["task_ids"]) == 1
    return body["task_ids"][0]


def dequeue_for(client, worker_id: str, limit: int = 1) -> list[dict]:
    r = client.post(
        "/tasks/dequeue",
        json={"admin_secret": ADMIN, "worker_id": worker_id, "limit": limit},
    )
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def finish_task(task_id: str, worker_id: str, *, via_running: bool = True) -> None:
    """Drive a task to COMPLETED the way a worker would, minus the socket."""

    async def body(sessionmaker):
        async with sessionmaker() as session:
            if via_running:
                assert await mark_status(
                    session, task_id=task_id, worker_id=worker_id, new_status="RUNNING"
                ) == "transitioned"
                await session.commit()
        async with sessionmaker() as session:
            envelope = {
                "task_id": task_id,
                "status": "COMPLETED",
                "attempt_number": 0,
                "session_epoch": 1,
                "idempotency_token": str(uuid.uuid4()),
                "duration_seconds": 0.5,
                "result": 5,
                "truncated": False,
                "size_bytes": 120,
            }
            assert await complete_task(session, envelope=envelope, worker_id=worker_id) == "transitioned"
            await session.commit()

    db(body)


# --------------------------------------------------------------------------
# Authentication and rate limiting — "authenticated and rate limited"
# --------------------------------------------------------------------------


def test_every_operator_endpoint_rejects_a_missing_credential(client):
    task_id = str(uuid.uuid4())
    assert client.get("/tasks").status_code == 401
    assert client.get("/tasks/depth").status_code == 401
    assert client.get("/tasks/throughput").status_code == 401
    assert client.get(f"/tasks/{task_id}").status_code == 401
    assert client.post(f"/tasks/{task_id}/cancel").status_code == 401
    assert client.post("/tasks", json={"task_type": "count_to_n"}).status_code == 401


def test_a_near_miss_credential_is_rejected_like_a_wild_one(client):
    assert client.get("/tasks", headers={"X-Admin-Secret": ADMIN[:-1]}).status_code == 401
    assert client.get("/tasks", headers={"X-Admin-Secret": "totally-wrong"}).status_code == 401


def test_enqueue_accepts_the_header_and_still_accepts_the_body(client):
    """The header is the documented path; the body field predates it and is
    what every existing script and the CD smoke test sends."""
    reset_tasks()
    assert client.post(
        "/tasks", headers=AUTH, json={"task_type": "count_to_n", "parameters": {"n": 5}}
    ).status_code == 201
    assert client.post(
        "/tasks", json={"admin_secret": ADMIN, "task_type": "count_to_n", "parameters": {"n": 5}}
    ).status_code == 201


def test_requests_past_the_rate_limit_are_rejected(client, monkeypatch):
    monkeypatch.setenv("TASK_API_RATE_LIMIT_PER_MINUTE", "3")
    assert client.get("/tasks/depth", headers=AUTH).status_code == 200
    assert client.get("/tasks/depth", headers=AUTH).status_code == 200
    assert client.get("/tasks/depth", headers=AUTH).status_code == 200
    limited = client.get("/tasks/depth", headers=AUTH)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate limited"


def test_the_limit_applies_before_authentication(client, monkeypatch):
    """An unauthenticated flood is what the limiter is for, so it must not
    be reachable only by callers who already hold the credential."""
    monkeypatch.setenv("TASK_API_RATE_LIMIT_PER_MINUTE", "1")
    assert client.get("/tasks", headers=AUTH).status_code == 200
    assert client.get("/tasks", headers={"X-Admin-Secret": "wrong"}).status_code == 429


def test_the_dequeue_primitive_is_exempt_from_the_operator_limit(client, monkeypatch):
    """`scripts/queue_harness.py` drives ~1,000 claim calls as fast as it
    can to prove three replicas never double-assign. Limiting that endpoint
    would break a versioned verification for no security gain."""
    worker_id = register_worker(client)
    monkeypatch.setenv("TASK_API_RATE_LIMIT_PER_MINUTE", "1")
    assert client.get("/tasks", headers=AUTH).status_code == 200  # consumes the budget
    r = client.post("/tasks/dequeue", json={"admin_secret": ADMIN, "worker_id": worker_id})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# Listing with filters
# --------------------------------------------------------------------------


def test_listing_returns_newest_first_and_pages_without_repeating(client):
    reset_tasks()
    first = one_task_id(client)
    second = one_task_id(client)
    third = one_task_id(client)

    page = client.get("/tasks", headers=AUTH, params={"limit": 2}).json()
    assert [t["task_id"] for t in page["tasks"]] == [third, second]
    assert page["count"] == 2 and page["has_more"] is True

    rest = client.get("/tasks", headers=AUTH, params={"limit": 2, "offset": 2}).json()
    assert [t["task_id"] for t in rest["tasks"]] == [first]
    assert rest["has_more"] is False


def test_listing_filters_by_status_type_worker_and_correlation_id(client):
    reset_tasks()
    worker_id = register_worker(client)
    sleeper = one_task_id(client, task_type="sleep", parameters={"seconds": 1})
    batch = enqueue(client, count=2)
    claimed = dequeue_for(client, worker_id, limit=5)
    claimed_ids = {t["task_id"] for t in claimed}

    by_status = client.get("/tasks", headers=AUTH, params={"status": "ASSIGNED"}).json()
    assert {t["task_id"] for t in by_status["tasks"]} == claimed_ids

    by_type = client.get("/tasks", headers=AUTH, params={"task_type": "sleep"}).json()
    assert [t["task_id"] for t in by_type["tasks"]] == [sleeper]

    by_worker = client.get("/tasks", headers=AUTH, params={"worker_id": worker_id}).json()
    assert {t["task_id"] for t in by_worker["tasks"]} == claimed_ids

    by_correlation = client.get(
        "/tasks", headers=AUTH, params={"correlation_id": batch["correlation_id"]}
    ).json()
    assert by_correlation["count"] == 2

    # Repeatable, and combining with AND.
    two_states = client.get(
        "/tasks", headers=AUTH, params={"status": ["QUEUED", "ASSIGNED"], "task_type": "count_to_n"}
    ).json()
    assert two_states["count"] == 2
    assert two_states["filters"]["status"] == ["QUEUED", "ASSIGNED"]


def test_an_unknown_filter_value_is_a_400_not_an_empty_list(client):
    """An operator who typos `RUNING` must be told, not shown "no tasks"
    and left to conclude the fleet is idle."""
    reset_tasks()
    one_task_id(client)
    bad_status = client.get("/tasks", headers=AUTH, params={"status": "RUNING"})
    assert bad_status.status_code == 400
    assert "RUNING" in bad_status.json()["detail"]

    bad_type = client.get("/tasks", headers=AUTH, params={"task_type": "count_to_m"})
    assert bad_type.status_code == 400

    bad_worker = client.get("/tasks", headers=AUTH, params={"worker_id": "not-a-uuid"})
    assert bad_worker.status_code == 400


def test_a_page_larger_than_the_cap_is_refused(client):
    over = client.get("/tasks", headers=AUTH, params={"limit": 100_000})
    assert over.status_code == 400
    assert "cap" in over.json()["detail"]


def test_a_listing_never_carries_result_bodies(client):
    reset_tasks()
    worker_id = register_worker(client)
    task_id = one_task_id(client)
    dequeue_for(client, worker_id)
    finish_task(task_id, worker_id)

    row = client.get("/tasks", headers=AUTH).json()["tasks"][0]
    assert row["has_result"] is True
    assert "result" not in row
    assert client.get(f"/tasks/{task_id}", headers=AUTH).json()["result"]["result"] == 5


# --------------------------------------------------------------------------
# Batch submission — the 1,000-task exit criterion
# --------------------------------------------------------------------------


def test_a_thousand_task_batch_succeeds_and_is_findable_by_correlation_id(client):
    reset_tasks()
    body = enqueue(client, count=1000)
    assert body["count"] == 1000
    # No ids are echoed for a bulk create — the correlation id is how a
    # batch is found again, which is what migration 0004 indexes.
    assert body["task_ids"] is None

    seen: set[str] = set()
    offset, pages = 0, 0
    while True:
        page = client.get(
            "/tasks",
            headers=AUTH,
            params={"correlation_id": body["correlation_id"], "limit": 200, "offset": offset},
        ).json()
        seen.update(t["task_id"] for t in page["tasks"])
        pages += 1
        if not page["has_more"]:
            break
        offset += 200

    assert len(seen) == 1000, "paging must not repeat or skip rows"
    assert pages == 5
    assert client.get("/tasks/depth", headers=AUTH).json()["depth"] == 1000


# --------------------------------------------------------------------------
# Inspection — full lifecycle with timestamps
# --------------------------------------------------------------------------


def test_inspection_returns_the_full_lifecycle_with_timestamps(client):
    reset_tasks()
    worker_id = register_worker(client)
    task_id = one_task_id(client)
    dequeue_for(client, worker_id)
    finish_task(task_id, worker_id)

    task = client.get(f"/tasks/{task_id}", headers=AUTH).json()
    assert task["status"] == "COMPLETED"
    assert [entry["state"] for entry in task["timeline"]] == [
        "QUEUED",
        "ASSIGNED",
        "RUNNING",
        "COMPLETED",
    ]
    stamps = [entry["at"] for entry in task["timeline"]]
    assert stamps == sorted(stamps), "a lifecycle that goes backwards is not a lifecycle"
    assert all(entry["source"] for entry in task["timeline"])
    assert task["started_at"] is not None
    assert task["observed_duration_seconds"] is not None
    # Worker-reported and coordinator-observed durations are both present
    # and are not the same measurement (§10).
    assert task["result"]["duration_seconds"] == 0.5


def test_a_result_that_arrives_without_a_started_report_still_dates_the_run(client):
    """Decision #114's walk through RUNNING. `started_at` is COALESCEd, so
    the timeline gains a RUNNING entry rather than a hole."""
    reset_tasks()
    worker_id = register_worker(client)
    task_id = one_task_id(client)
    dequeue_for(client, worker_id)
    finish_task(task_id, worker_id, via_running=False)

    task = client.get(f"/tasks/{task_id}", headers=AUTH).json()
    assert [entry["state"] for entry in task["timeline"]] == [
        "QUEUED",
        "ASSIGNED",
        "RUNNING",
        "COMPLETED",
    ]


def test_a_queued_task_has_a_one_entry_lifecycle(client):
    reset_tasks()
    task_id = one_task_id(client)
    task = client.get(f"/tasks/{task_id}", headers=AUTH).json()
    assert [entry["state"] for entry in task["timeline"]] == ["QUEUED"]
    assert task["assigned_at"] is None and task["started_at"] is None
    assert task["has_result"] is False and task["result"] is None


def test_inspecting_an_unknown_task_is_a_404(client):
    assert client.get(f"/tasks/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert client.get("/tasks/not-a-uuid", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


def test_cancelling_a_queued_task_removes_it_from_the_queue(client):
    reset_tasks()
    worker_id = register_worker(client)
    task_id = one_task_id(client)
    assert client.get("/tasks/depth", headers=AUTH).json()["depth"] == 1

    r = client.post(f"/tasks/{task_id}/cancel", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"task_id": task_id, "status": "CANCELLED", "previous_status": "QUEUED"}

    assert client.get("/tasks/depth", headers=AUTH).json()["depth"] == 0
    assert dequeue_for(client, worker_id) == [], "a cancelled task must not be claimable"
    assert client.get(f"/tasks/{task_id}", headers=AUTH).json()["status"] == "CANCELLED"

    timeline = client.get(f"/tasks/{task_id}", headers=AUTH).json()["timeline"]
    assert [entry["state"] for entry in timeline] == ["QUEUED", "CANCELLED"]


def test_cancelling_an_assigned_task_is_refused_with_its_status(client):
    """M2 cancels queued work only. A coordinator-side write would not stop
    the worker, and the result it later submits would be refused as illegal
    — losing real work. So the refusal is the honest answer."""
    reset_tasks()
    worker_id = register_worker(client)
    task_id = one_task_id(client)
    dequeue_for(client, worker_id)

    r = client.post(f"/tasks/{task_id}/cancel", headers=AUTH)
    assert r.status_code == 409
    assert r.json()["status"] == "ASSIGNED"
    assert client.get(f"/tasks/{task_id}", headers=AUTH).json()["status"] == "ASSIGNED"


def test_cancelling_twice_reports_the_conflict_rather_than_cancelling_again(client):
    reset_tasks()
    task_id = one_task_id(client)
    assert client.post(f"/tasks/{task_id}/cancel", headers=AUTH).status_code == 200
    second = client.post(f"/tasks/{task_id}/cancel", headers=AUTH)
    assert second.status_code == 409
    assert second.json()["status"] == "CANCELLED"


def test_cancelling_an_unknown_task_is_a_404(client):
    assert client.post(f"/tasks/{uuid.uuid4()}/cancel", headers=AUTH).status_code == 404
    assert client.post("/tasks/not-a-uuid/cancel", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------
# The 422 that leaked a live credential in session 13
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Throughput (Phase 2.7) — the read behind the dashboard's chart
# --------------------------------------------------------------------------


def test_throughput_counts_the_same_completions_the_listing_returns(client):
    """The exit criterion is "the throughput chart matches measured
    reality", so the check is not that the endpoint returns a plausible
    number — it is that its number equals what `GET /tasks?status=COMPLETED`
    lists over the same window. Two independent reads of the same rows."""
    reset_tasks()
    worker_id = register_worker(client)
    enqueue(client, count=3)
    for task in dequeue_for(client, worker_id, limit=3):
        finish_task(task["task_id"], worker_id)

    chart = client.get("/tasks/throughput", headers=AUTH, params={"minutes": 30}).json()
    listed = client.get(
        "/tasks", headers=AUTH, params={"status": "COMPLETED", "limit": 200}
    ).json()

    assert chart["completed_in_window"] == len(listed["tasks"]) == 3
    assert sum(point["completed"] for point in chart["series"]) == 3


def test_throughput_returns_every_minute_in_the_window_including_empty_ones(client):
    """A chart that omits the quiet minutes draws a busy fleet and an idle
    one the same way. The series length is the window, not the number of
    minutes that happened to have completions."""
    reset_tasks()
    body = client.get("/tasks/throughput", headers=AUTH, params={"minutes": 10}).json()
    assert body["window_minutes"] == 10
    assert len(body["series"]) == 10
    assert body["completed_in_window"] == 0
    assert all(point["completed"] == 0 for point in body["series"])
    minutes = [point["minute"] for point in body["series"]]
    assert minutes == sorted(minutes), "oldest first, so a chart reads left to right"


def test_a_failed_task_is_not_counted_as_throughput(client):
    """`completed_at` means "produced a result", not "stopped moving" — so a
    failure must not appear in a completions-per-minute series at all."""
    reset_tasks()
    worker_id = register_worker(client)
    enqueue(client, count=1)
    claimed = dequeue_for(client, worker_id, limit=1)

    async def fail(sessionmaker):
        async with sessionmaker() as session:
            assert await mark_status(
                session, task_id=claimed[0]["task_id"], worker_id=worker_id, new_status="FAILED"
            ) == "transitioned"
            await session.commit()

    db(fail)

    body = client.get("/tasks/throughput", headers=AUTH, params={"minutes": 30}).json()
    assert body["completed_in_window"] == 0


def test_a_window_past_the_cap_is_refused(client):
    """An uncapped window is a full-table aggregate wearing a filter."""
    r = client.get("/tasks/throughput", headers=AUTH, params={"minutes": 100000})
    assert r.status_code == 400
    assert "exceeds" in r.json()["detail"]
    assert client.get("/tasks/throughput", headers=AUTH, params={"minutes": 0}).status_code == 422


def test_a_validation_error_does_not_quote_the_request_back(client):
    """The exact shape of the session 13 leak: a required field is missing,
    and pydantic's `missing` error carries the **whole body** as its input —
    including the credential that was in it. Decision #119 rotated the
    secret; this is what stops it happening again."""
    r = client.post("/tasks", json={"admin_secret": ADMIN, "parameters": {"n": 5}})
    assert r.status_code == 422
    assert ADMIN not in r.text
    assert "input" not in r.text
    error = r.json()["detail"][0]
    assert error["type"] == "missing" and "task_type" in error["loc"]


def test_the_validation_handler_keeps_the_error_useful(client):
    """Stripping the echo must not turn a 422 into a riddle — the caller
    still has to be able to fix its own request."""
    r = client.post("/tasks", headers=AUTH, json={"task_type": "count_to_n", "count": "many"})
    assert r.status_code == 422
    body = json.dumps(r.json())
    assert "count" in body and "int" in body


# --------------------------------------------------------------------------
# Per-task-type timeout policy (Phase 3.1)
# --------------------------------------------------------------------------


def clear_policies():
    async def body(sessionmaker):
        async with sessionmaker() as session:
            await session.execute(text("DELETE FROM task_policies"))
            await session.commit()

    db(body)


def test_the_policy_listing_covers_every_type_and_names_its_source(client):
    """A surface that listed only what had been changed could not answer the
    question an operator actually has, which is what is in force now."""
    clear_policies()
    r = client.get("/tasks/policies", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 4
    by_type = {entry["task_type"]: entry for entry in body["policies"]}
    assert set(by_type) == {"count_to_n", "hash_rounds", "sleep", "opaque_payload"}
    assert by_type["sleep"]["max_execution_seconds"] == 3900
    assert by_type["sleep"]["max_execution_seconds_source"] == "default"
    # No attempt cap exists in 3.1 — reporting a number would invent one.
    assert by_type["sleep"]["max_attempts"] is None


def test_a_policy_can_be_set_and_cleared_without_a_restart(client):
    """Step 3.1's sixth exit criterion, driven the way an operator would.
    Nothing is restarted or reloaded between these calls."""
    clear_policies()
    r = client.put(
        "/tasks/policies/sleep", headers=AUTH, json={"max_execution_seconds": 120}
    )
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["max_execution_seconds"] == 120
    assert r.json()["effective"]["max_execution_seconds_source"] == "policy"

    # Read back through the listing, not the write's own response.
    listed = {
        e["task_type"]: e
        for e in client.get("/tasks/policies", headers=AUTH).json()["policies"]
    }
    assert listed["sleep"]["max_execution_seconds"] == 120
    assert listed["sleep"]["updated_at"] is not None
    # Untouched fields stay on their code defaults.
    assert listed["sleep"]["lease_ttl_seconds_source"] == "default"

    r = client.delete("/tasks/policies/sleep", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert r.json()["effective"]["max_execution_seconds"] == 3900
    # A second delete is 200, not 404: the intent is satisfied either way.
    assert client.delete("/tasks/policies/sleep", headers=AUTH).json()["removed"] is False


def test_a_nonsense_policy_is_a_400_naming_the_field(client):
    """A zero lease TTL would reclaim every task the instant it was
    assigned. That is a typo, not an attack, and the operator has to be
    told which field was wrong."""
    clear_policies()
    r = client.put("/tasks/policies/sleep", headers=AUTH, json={"lease_ttl_seconds": 0})
    assert r.status_code == 400
    assert "lease_ttl_seconds" in r.json()["detail"]

    r = client.put("/tasks/policies/not_a_type", headers=AUTH, json={"lease_ttl_seconds": 60})
    assert r.status_code == 400
    assert "not_a_type" in r.json()["detail"]

    r = client.put("/tasks/policies/sleep", headers=AUTH, json={})
    assert r.status_code == 400


def test_every_policy_endpoint_needs_the_operator_credential(client):
    """The same guard the rest of the task API carries. A worker holds
    ENROLLMENT_SECRET, and a worker that could rewrite the lease policy
    could give itself an unbounded execution cap (§12)."""
    clear_policies()
    for method, path in (
        ("get", "/tasks/policies"),
        ("put", "/tasks/policies/sleep"),
        ("delete", "/tasks/policies/sleep"),
    ):
        call = getattr(client, method)
        kwargs = {"json": {"lease_ttl_seconds": 60}} if method == "put" else {}
        assert call(path, **kwargs).status_code == 401
        assert call(path, headers={"X-Admin-Secret": "wrong"}, **kwargs).status_code == 401


def test_a_task_reports_its_lease_and_deadline(client):
    """§6: the lease has to be visible in the browser, or a reassignment is
    something that happens off-screen. `lease_seconds_remaining` is computed
    against the coordinator's clock, not the viewer's."""
    reset_tasks()
    task_id = one_task_id(client)

    listed = client.get("/tasks", headers=AUTH).json()["tasks"][0]
    # A queued task holds no lease, and that is the honest answer.
    assert listed["lease_expires_at"] is None
    assert listed["lease_seconds_remaining"] is None

    worker_id = register_worker(client)
    r = client.post(
        "/tasks/dequeue",
        json={"admin_secret": ADMIN, "worker_id": worker_id, "limit": 1},
    )
    assert r.status_code == 200, r.text
    # The raw queue primitive reports the deadline it just imposed — a claim
    # that did not say so would be a trap for anything driving it directly.
    assert r.json()["tasks"][0]["lease_expires_at"] is not None

    detail = client.get(f"/tasks/{task_id}", headers=AUTH).json()
    assert detail["lease_expires_at"] is not None
    # The execution cap is stamped at delivery too — `task_started`
    # re-stamps it from the real start.
    assert detail["deadline_at"] is not None
    assert 0 < detail["lease_seconds_remaining"] <= 30
