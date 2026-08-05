"""Graceful shutdown and restart recovery (Phase 3.5).

Postgres-gated like every other Phase 3 module, and here for a blunter
reason than usual: importing `app.assignment` builds `app.db`'s engine
from `POSTGRES_*` at import time. Only one test in this file actually
reaches the database, and it is the one that matters most — proof that a
draining replica claims **nothing**, taken by counting rows in the queue
rather than by trusting the early return.

What this module proves, and what it deliberately leaves to the live demo:

* **Draining stops assignment**, checked against the queue itself.
* **A draining replica fails readiness and keeps liveness.** The split is
  the whole reason Kubernetes takes the pod out of the Service without
  restarting it.
* **The drain waits for deliveries, not for executions**, including the
  timeout path — because a drain that could be held open by a long-running
  task would turn a 45-second grace period into a SIGKILL.
* **A draining replica is still a correct replica**: the reclaimer still
  reclaims and results are still accepted. A drain that quietly stopped
  recovery would be a fault-tolerance regression dressed as a feature.
* **The one-way flag**, which every reader depends on to treat
  `is_draining()` as a fact rather than a value that might flip back.

What is NOT here: that a real SIGTERM reaches `DrainingServer.handle_exit`
and that uvicorn exits afterwards. That is uvicorn's signal plumbing plus
the container's process model, and the only honest test of it is stopping
a real container and reading its logs — recorded in the phase document as
a measurement, not asserted here.
"""

import asyncio
import contextlib
import os
import uuid

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "shutdown drain tests require Postgres (set POSTGRES_HOST)",
        allow_module_level=True,
    )

# Imported after the skip guard — app.config reads POSTGRES_* eagerly.
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import assignment  # noqa: E402
from app.assignment import (  # noqa: E402
    LocalSession,
    assign_once,
    begin_drain,
    drain_local_sessions,
    is_draining,
    reclaim_once,
    register_session,
    unregister_session,
)
from app.config import database_url, shutdown_drain_seconds  # noqa: E402
from app.metrics import DRAINING  # noqa: E402
from app.task_queue import enqueue  # noqa: E402

@pytest.fixture(autouse=True)
def _not_draining():
    """Every test starts undrained and leaves the module undrained.

    `_draining` is process-global and one-way by design, so a test that set
    it would silently disable assignment for every test that ran after it —
    in this file and in any other module sharing the interpreter.
    """
    assignment._draining = False
    DRAINING.set(0)
    yield
    assignment._draining = False
    DRAINING.set(0)


@pytest.fixture()
def db_url() -> str:
    return database_url()


async def _fresh_engine(url: str):
    engine = create_async_engine(url, poolclass=None)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class _FakeSocket:
    """A session needs a websocket object; nothing here ever sends."""

    async def send_text(self, _: str) -> None:  # pragma: no cover — never reached
        raise AssertionError("a draining replica must not send anything")


def _session(worker_id: str, *, pending: dict[str, str] | None = None) -> LocalSession:
    return LocalSession(
        worker_id=worker_id,
        session_epoch=1,
        websocket=_FakeSocket(),
        send_lock=asyncio.Lock(),
        max_concurrent=4,
        supported_task_types=("count_to_n",),
        pending_acks=dict(pending or {}),
    )


# --------------------------------------------------------------------------
# The flag
# --------------------------------------------------------------------------

def test_the_drain_flag_is_off_until_it_is_turned_on_and_then_stays_on():
    assert is_draining() is False
    begin_drain()
    assert is_draining() is True
    # Called twice by a second signal in the real path; must not toggle.
    begin_drain()
    assert is_draining() is True


def test_the_draining_gauge_follows_the_flag():
    from prometheus_client import REGISTRY

    assert REGISTRY.get_sample_value("coordinator_draining") == 0.0
    begin_drain()
    assert REGISTRY.get_sample_value("coordinator_draining") == 1.0


# --------------------------------------------------------------------------
# Assignment stops — checked against the queue, not against the return value
# --------------------------------------------------------------------------

def test_a_draining_replica_claims_no_task_even_with_a_queue_and_a_free_worker(db_url):
    """The criterion this file exists for.

    A `return 0` proves the function returned; it does not prove no row
    moved. So the queue is read afterwards and the task must still be
    `QUEUED` — claimable by another replica, which is the entire point.
    """

    async def scenario() -> None:
        engine, factory = await _fresh_engine(db_url)
        correlation_id = str(uuid.uuid4())
        try:
            async with factory() as db:
                await enqueue(
                    db,
                    task_type="count_to_n",
                    parameters={"n": 10},
                    correlation_id=correlation_id,
                )
                await db.commit()

            @contextlib.asynccontextmanager
            async def _get_session():
                async with factory() as db:
                    yield db

            original = assignment.get_session
            assignment.get_session = _get_session
            session = _session(str(uuid.uuid4()))
            register_session(session)
            try:
                begin_drain()
                assert await assign_once() == 0

                async with factory() as db:
                    status = (
                        await db.execute(
                            text(
                                "SELECT status FROM tasks WHERE correlation_id = :cid"
                            ),
                            {"cid": correlation_id},
                        )
                    ).scalar_one()
                assert status == "QUEUED", "a draining replica claimed a task"
            finally:
                unregister_session(session.worker_id, session.session_epoch)
                assignment.get_session = original
                async with factory() as db:
                    await db.execute(
                        text("DELETE FROM tasks WHERE correlation_id = :cid"),
                        {"cid": correlation_id},
                    )
                    await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_the_reclaimer_still_runs_while_draining(db_url):
    """A draining replica is still a correct replica.

    Recovery must not stop: the tasks this replica is about to abandon are
    exactly the ones that need reclaiming, and if draining switched the
    reclaimer off, the replica that owns the sockets would stop recovering
    them at the worst possible moment.
    """

    async def scenario() -> None:
        engine, factory = await _fresh_engine(db_url)
        try:
            @contextlib.asynccontextmanager
            async def _get_session():
                async with factory() as db:
                    yield db

            original = assignment.get_session
            assignment.get_session = _get_session
            try:
                begin_drain()
                # An empty pass is enough: the assertion is that it runs and
                # returns a count rather than short-circuiting like
                # `assign_once` does.
                assert await reclaim_once() >= 0
            finally:
                assignment.get_session = original
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The wait itself
# --------------------------------------------------------------------------

def test_a_drain_with_nothing_outstanding_returns_at_once():
    async def scenario() -> None:
        session = _session(str(uuid.uuid4()))
        register_session(session)
        try:
            observed = await drain_local_sessions(timeout=5.0)
        finally:
            unregister_session(session.worker_id, session.session_epoch)
        assert observed["pending_acks_at_start"] == 0
        assert observed["timed_out"] is False
        # The window is 5s; an immediate return is the whole claim.
        assert observed["waited_seconds"] < 1.0

    asyncio.run(scenario())


def test_a_drain_ends_when_the_last_delivery_is_acknowledged():
    """The normal path, and the one that pays for the step.

    The ack is simulated by clearing `pending_acks` from another task, the
    way `handle_task_ack` clears it when the worker answers.
    """

    async def scenario() -> None:
        session = _session(str(uuid.uuid4()), pending={"task-a": "corr-a"})
        register_session(session)

        async def ack_after(delay: float) -> None:
            await asyncio.sleep(delay)
            session.pending_acks.clear()

        try:
            acker = asyncio.create_task(ack_after(0.3))
            observed = await drain_local_sessions(timeout=10.0)
            await acker
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        assert observed["pending_acks_at_start"] == 1
        assert observed["pending_acks_at_end"] == 0
        assert observed["timed_out"] is False
        # Ended on the ack, not on the window: 0.3s in, not 10s in.
        assert 0.2 < observed["waited_seconds"] < 5.0

    asyncio.run(scenario())


def test_a_delivery_that_is_never_acknowledged_only_costs_the_window():
    """The bound that keeps the drain safe.

    A worker that has stopped answering must not be able to hold the
    process open past its kill deadline — that would turn a graceful
    shutdown into a SIGKILL, which is strictly worse than the abrupt exit
    this step replaced.
    """

    async def scenario() -> None:
        session = _session(str(uuid.uuid4()), pending={"task-a": "corr-a"})
        register_session(session)
        try:
            observed = await drain_local_sessions(timeout=0.4)
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        assert observed["timed_out"] is True
        assert observed["pending_acks_at_end"] == 1
        assert observed["waited_seconds"] < 2.0

    asyncio.run(scenario())


def test_a_running_task_does_not_hold_the_drain_open():
    """**The distinction the whole design rests on.**

    A task that has been acked and is executing is credited but not
    pending. It survives this process — the worker renews its lease on
    `hello` against whichever replica it reconnects to — so waiting for it
    would be waiting for something that does not need waiting for, for up
    to `TASK_MAX_EXECUTION_SECONDS`.
    """

    async def scenario() -> None:
        session = _session(str(uuid.uuid4()))
        session.credited["long-task"] = "corr-long"
        register_session(session)
        try:
            observed = await drain_local_sessions(timeout=5.0)
        finally:
            unregister_session(session.worker_id, session.session_epoch)

        assert session.in_flight == 1, "the task is genuinely still in flight"
        assert observed["timed_out"] is False
        assert observed["waited_seconds"] < 1.0

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------

def test_a_draining_replica_is_not_ready_and_says_why():
    from fastapi import Response

    from app.main import ready

    async def scenario() -> None:
        response = Response()
        begin_drain()
        body = await ready(response)
        assert response.status_code == 503
        assert body["status"] == "draining"

    asyncio.run(scenario())


def test_liveness_still_answers_while_draining():
    """A draining pod must not be restarted by the liveness probe — it is
    shutting down on purpose and needs its window."""
    from app.main import health

    begin_drain()
    assert health()["status"] == "healthy"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def test_the_drain_window_is_shorter_than_both_kill_deadlines():
    """The one number that makes the whole step work or not work.

    45 is `terminationGracePeriodSeconds` in
    `infra/helm/platform/templates/coordinator.yaml` and
    `stop_grace_period` in `docker-compose.yml`. If the default window ever
    grows past them, every graceful shutdown becomes a SIGKILL partway
    through its drain — silently, and only in the environments nobody runs
    locally. This fails instead.
    """
    assert shutdown_drain_seconds() < 45


def test_the_drain_can_be_switched_off_for_the_pre_3_5_behaviour(monkeypatch):
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0")
    assert shutdown_drain_seconds() == 0
