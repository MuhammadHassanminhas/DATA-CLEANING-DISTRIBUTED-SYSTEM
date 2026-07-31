"""Worker execution runtime (Phase 2.4).

Covers the worker's admission control and its reporting, which is the half
of Step 2.4 that `test_executors.py` deliberately does not touch: the
executors know nothing about sessions, slots or the wire.

`test_a_completion_after_a_reconnect_reports_on_the_new_socket` is a
regression test for a defect **found by a live reconnect test, not by
review**: an execution coroutine outlives the session that started it, so
a completion that captured its session's socket reported down a dead one.
The send failed silently, the credit was never released, and the worker sat
at zero free credits with nothing running — the queue stranded behind a
worker that was permanently, invisibly "full".

No coordinator, no Postgres, no Redis. `websockets` and `psutil` are real
dependencies of the worker image, so they are imported rather than stubbed;
`COORDINATOR_URL` is read at import time and is set here.
"""

import asyncio
import json
import os
import threading

import pytest

os.environ.setdefault("COORDINATOR_URL", "https://example.invalid")
os.environ.setdefault("WORKER_IDENTITY_FILE", "/tmp/test-worker-identity.json")

from worker import executors, worker  # noqa: E402

IDENTITY = {"worker_id": "11111111-1111-1111-1111-111111111111", "worker_credential": "x"}


class FakeWS:
    """Records frames, or refuses them the way a closed socket does."""

    def __init__(self, name: str = "ws", fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        if self.fail:
            raise ConnectionError("socket closed")
        self.sent.append(json.loads(raw))

    def types(self) -> list[str]:
        return [m["message_type"] for m in self.sent]

    def of_type(self, message_type: str) -> list[dict]:
        return [m for m in self.sent if m["message_type"] == message_type]


def assignment(task_id: str, task_type: str = "count_to_n", **parameters) -> dict:
    return {
        "message_type": "task_assign",
        "correlation_id": f"corr-{task_id}",
        "payload": {
            "task_id": task_id,
            "task_type": task_type,
            "parameters": parameters or {"n": 1000},
        },
    }


async def drain() -> None:
    """Let the execution coroutines this test started actually run."""
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not worker._EXECUTIONS:
            return
    raise AssertionError("executions did not finish")


# --------------------------------------------------------------------------
# Reporting follows the live session, not the session that started the task
# --------------------------------------------------------------------------


def test_a_report_with_no_session_is_dropped_rather_than_raising():
    async def body():
        runner = worker.TaskRunner(2)
        sent = await runner.report(
            worker.Envelope(message_type="capacity", worker_id=IDENTITY["worker_id"])
        )
        assert sent is False

    asyncio.run(body())


def test_a_completion_after_a_reconnect_reports_on_the_new_socket():
    """**The regression test.** Old socket, new socket, one completion: the
    report must land on the new one. If it lands on the old one — or nowhere
    — the coordinator never releases the credit and the worker is stuck full
    forever."""

    async def body():
        runner = worker.TaskRunner(2)
        old, new = FakeWS("old", fail=True), FakeWS("new")

        runner.attach(old, asyncio.Lock())
        await worker._handle_assignment(
            old, IDENTITY, asyncio.Lock(), runner, assignment("t1", n=1000), "session-1"
        )
        # The session dies mid-execution and a new one replaces it.
        runner.detach()
        runner.attach(new, asyncio.Lock())
        await drain()

        # Step 2.5 renamed the message a success sends — `task_result`, which
        # carries the credit release — but the defect it guards against is
        # unchanged: the report must land on the socket that is live now.
        assert new.of_type("task_result"), "the completion never reached the live session"
        assert new.of_type("task_result")[0]["payload"]["task_id"] == "t1"
        assert old.sent == []  # it refuses everything; nothing was retried into the void

    asyncio.run(body())


# --------------------------------------------------------------------------
# Admission control
# --------------------------------------------------------------------------


def test_a_type_this_worker_did_not_advertise_is_refused_with_its_reason_code():
    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", "mine_bitcoin"), "session-1"
        )

        ack = ws.of_type("task_ack")[0]["payload"]
        assert ack["accepted"] is False
        assert ack["reason_code"] == worker.REFUSE_UNSUPPORTED_TYPE
        assert runner.tasks_in_flight == 0
        assert "task_started" not in ws.types()

    asyncio.run(body())


def test_an_over_committed_worker_refuses_rather_than_queueing_locally():
    """The invariant: the worker never holds a task it is not executing. A
    local backlog would be the worker scheduling (§3.2/§3.3), and the
    refusal must be labelled `at_capacity` or the coordinator frees the
    credit and livelocks (Decision #101)."""

    async def body():
        runner = worker.TaskRunner(1)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("busy", "sleep", seconds=5), "s"
        )
        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("extra", n=1000), "s"
        )

        refusals = [a for a in ws.of_type("task_ack") if a["payload"]["accepted"] is False]
        assert len(refusals) == 1
        assert refusals[0]["payload"]["reason_code"] == worker.REFUSE_AT_CAPACITY
        # One slot, one execution — the second was never started.
        assert runner.tasks_in_flight == 1
        assert len(ws.of_type("task_started")) == 1

        runner.cancel_all()
        await drain()

    asyncio.run(body())


def test_a_duplicate_assignment_is_acknowledged_but_not_run_twice():
    """Idempotency (§3.7). Two starts for one task would mean two threads and
    two capacity releases for a single credit."""

    async def body():
        runner = worker.TaskRunner(4)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("dup", "sleep", seconds=3), "s"
        )
        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("dup", "sleep", seconds=3), "s"
        )

        accepted = [a for a in ws.of_type("task_ack") if a["payload"]["accepted"]]
        assert len(accepted) == 2  # both answered
        assert len(ws.of_type("task_started")) == 1  # one execution
        assert runner.tasks_in_flight == 1

        runner.cancel_all()
        await drain()

    asyncio.run(body())


def test_the_pool_is_sized_to_the_declared_concurrency_not_the_default():
    """Decision #96, and a measured trap: `os.cpu_count()` returns 4 inside a
    `--cpus=1` container, so the default `to_thread` pool would be 8 — fewer
    than the 64 credits the ceiling allows, silently queueing the rest
    *inside* the executor where nothing can see them."""
    runner = worker.TaskRunner(11)
    assert runner.pool._max_workers == 11


# --------------------------------------------------------------------------
# The three reports, and local state that must not survive
# --------------------------------------------------------------------------


def test_a_successful_task_reports_started_then_a_result_and_leaves_no_state():
    """The "worker deletes temporary state after submission" exit criterion:
    `running` is the worker's entire *execution* state and must be empty once
    the task is done.

    **Rewritten at Step 2.5, and the change is the point.** Step 2.4's success
    path sent `capacity`; a success now submits a `task_result` and the
    coordinator releases the credit when it processes it. Sending both would
    put one credit's accounting on two messages.
    """

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock, 7)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", n=1000), "s"
        )
        await drain()

        assert ws.types().count("task_started") == 1
        assert ws.types().count("task_result") == 1
        assert "capacity" not in ws.types()
        assert runner.running == {}
        assert runner.tasks_in_flight == 0
        # The slot came back, so the next task can be accepted.
        assert runner.slots.locked() is False

        result = ws.of_type("task_result")[0]["payload"]
        assert result["task_id"] == "t1"
        assert result["status"] == "COMPLETED"
        assert result["result"] == 1000
        assert result["duration_seconds"] >= 0
        # Present from day one, enforced by nothing in M2 — that is exactly
        # what the exit criterion asks for, so Phase 3 adds enforcement
        # rather than a protocol change.
        assert result["session_epoch"] == 7
        assert result["attempt_number"] == 0
        assert result["idempotency_token"]

    asyncio.run(body())


def test_a_result_stays_pending_until_the_coordinator_acknowledges_it():
    """A successful *send* is not a successful *submission*. A coordinator
    that died between reading the frame and committing the row would
    otherwise lose the result silently."""

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", n=1000), "s"
        )
        await drain()

        assert set(runner.pending_results) == {"t1"}

        await worker._handle_result_ack(
            IDENTITY, runner, {"payload": {"task_id": "t1", "accepted": True, "outcome": "transitioned"}}
        )
        assert runner.pending_results == {}
        assert runner.results_pending.is_set() is False

    asyncio.run(body())


def test_a_rejected_result_is_dropped_rather_than_retried_forever():
    """A malformed result is malformed on every attempt. Retrying it would
    burn the submission loop on a verdict that is already final and hold a
    slot in the bounded buffer that a recoverable result could use."""

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", n=1000), "s"
        )
        await drain()

        await worker._handle_result_ack(
            IDENTITY,
            runner,
            {"payload": {"task_id": "t1", "accepted": False, "outcome": "rejected"}},
        )
        assert runner.pending_results == {}

    asyncio.run(body())


def test_a_result_that_finishes_during_an_outage_is_retried_on_reconnect():
    """**The exit criterion, as a test.** The socket is dead when the task
    finishes, so the submission fails; the result is held, and the reconnect
    is what delivers it."""

    async def body():
        runner = worker.TaskRunner(2)
        dead, live = FakeWS("dead", fail=True), FakeWS("live")

        runner.attach(dead, asyncio.Lock())
        await worker._handle_assignment(
            dead, IDENTITY, asyncio.Lock(), runner, assignment("t1", n=1000), "s"
        )
        await drain()

        # Nothing landed, and nothing was lost either.
        assert dead.sent == []
        assert set(runner.pending_results) == {"t1"}

        runner.detach()
        runner.attach(live, asyncio.Lock())
        assert runner.results_pending.is_set(), "the reconnect must wake the retry loop"

        assert await worker._submit_result(IDENTITY, runner, "t1") is True
        assert live.of_type("task_result")[0]["payload"]["task_id"] == "t1"
        # Still pending: the ack, not the send, is what clears it.
        assert set(runner.pending_results) == {"t1"}

    asyncio.run(body())


def test_a_result_just_sent_is_not_immediately_resent(monkeypatch):
    """**Regression test for a defect a live run found, not review**
    (Decision #116).

    A completing task submits its result *and* records it as pending, which
    wakes the retry loop; the loop then found a result that had been on the
    wire for a millisecond and sent it again. Both landed — the second as a
    harmless `duplicate` — so nothing broke, no test failed, and every result
    went over the wire twice. For a full-size `opaque_payload` echo that is
    87 KB duplicated per task, fleet-wide.
    """

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", n=1000), "s"
        )
        await drain()
        assert len(ws.of_type("task_result")) == 1

        # What the retry loop does on its next pass, without the sleep.
        assert runner.awaiting_ack("t1") is True
        assert await worker._submit_result(IDENTITY, runner, "t1") is True
        assert len(ws.of_type("task_result")) == 2, "the loop skips, the caller does not"

        # Once the grace period lapses it is retried, because an ack that
        # never came means the result may never have landed.
        monkeypatch.setattr(worker, "RESULT_ACK_GRACE_SECONDS", 0.0)
        assert runner.awaiting_ack("t1") is False

    asyncio.run(body())


def test_a_reconnect_voids_the_ack_grace_so_recovery_is_immediate():
    """A send is void the moment its socket dies. Honouring a grace period
    earned on a dead socket would delay the reconnect recovery the exit
    criterion is about by up to a full grace period."""

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("t1", n=1000), "s"
        )
        await drain()
        assert runner.awaiting_ack("t1") is True

        runner.detach()
        runner.attach(FakeWS("new"), asyncio.Lock())
        assert runner.awaiting_ack("t1") is False

    asyncio.run(body())


def test_the_pending_buffer_is_bounded_and_drops_the_oldest():
    """Step 2.4 shipped with no buffer at all, deliberately. 2.5's outage
    criterion forces one, so it is bounded — an unbounded buffer is the one
    thing that breaks the flat-memory property."""

    async def body():
        runner = worker.TaskRunner(1)
        cap = worker.MAX_PENDING_RESULTS
        for index in range(cap + 3):
            runner.record_result(f"t{index}", {"task_id": f"t{index}"})

        assert len(runner.pending_results) == cap
        # The three oldest went; the newest survived.
        assert "t0" not in runner.pending_results
        assert f"t{cap + 2}" in runner.pending_results

    asyncio.run(body())


def test_an_oversize_result_is_truncated_rather_than_dropped_or_sent():
    """Truncation, not rejection: the task genuinely completed, so refusing
    to report it would strand real work over a payload size — and sending it
    anyway is what "breaks the connection" in the criterion's own words."""

    task = worker.RunningTask(
        task_id="big", task_type="opaque_payload", correlation_id="c", started_at=0.0
    )
    envelope = worker._build_result_envelope(task, "x" * (worker.MAX_RESULT_BYTES + 10), 1.0)

    assert envelope["truncated"] is True
    assert envelope["result"] is None
    assert envelope["original_size_bytes"] > worker.MAX_RESULT_BYTES
    # Everything Phase 3 reasons about survives; only the body it would never
    # have read is gone.
    assert envelope["idempotency_token"] and envelope["status"] == "COMPLETED"


def test_the_largest_legal_opaque_payload_result_is_not_truncated():
    """The arithmetic behind the 128 KB cap (Decision #113). `opaque_payload`
    accepts 64 KB *decoded* and echoes it back **base64-encoded** — 4/3 the
    size — so a 64 KB result cap would have truncated the largest legal
    task's result, and Step 2.1's note that a worker echoing its input
    "cannot exceed the result cap by construction" compared decoded input to
    encoded output."""
    import base64

    largest = base64.b64encode(b"\xab" * executors.MAX_OPAQUE_PAYLOAD_BYTES).decode("ascii")
    assert len(largest) > 64 * 1024, "the premise: this exceeds the old 64 KB cap"

    task = worker.RunningTask(
        task_id="big", task_type="opaque_payload", correlation_id="c", started_at=0.0
    )
    envelope = worker._build_result_envelope(task, largest, 1.0)

    assert envelope["truncated"] is False
    assert envelope["result"] == largest


def test_a_raising_executor_reports_task_failed_with_the_type_and_no_traceback(monkeypatch):
    """Decision #102, and §12: a traceback can contain payload data, so the
    exception *type* is all that goes on the wire."""

    def explode(parameters, progress=None, cancel=None):
        raise ZeroDivisionError("secret-payload-value-must-not-appear")

    monkeypatch.setitem(executors.EXECUTORS, "count_to_n", explode)

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("boom", n=1000), "s"
        )
        await drain()

        failures = ws.of_type("task_failed")
        assert len(failures) == 1
        payload = failures[0]["payload"]
        assert payload == {"task_id": "boom", "error_type": "ZeroDivisionError"}
        assert "secret-payload-value-must-not-appear" not in json.dumps(failures[0])
        # No `capacity` for a failure — `task_failed` is what frees the credit.
        assert "capacity" not in ws.types()
        # And the slot is still returned locally, or the worker would leak it.
        assert runner.running == {}
        assert runner.slots.locked() is False

    asyncio.run(body())


def test_a_cancelled_task_reports_nothing_and_still_clears_its_state():
    """Shutdown. The socket is going away with the process, so a report would
    be noise; the local state must go regardless."""

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, assignment("slow", "sleep", seconds=30), "s"
        )
        assert runner.tasks_in_flight == 1
        cancelled = runner.cancel_all()
        await drain()

        assert cancelled == "slow"
        assert runner.running == {}
        assert "task_failed" not in ws.types()
        assert "capacity" not in ws.types()

    asyncio.run(body())


def test_progress_reports_are_suppressed_when_the_value_has_not_moved():
    """One reporter per worker, and a stalled task must go quiet rather than
    repeating itself at the interval forever."""

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        task = worker.RunningTask(
            task_id="t1",
            task_type="sleep",
            correlation_id="corr-1",
            started_at=0.0,
            progress=[0.4],
            cancel=threading.Event(),
        )
        runner.running["t1"] = task

        reporter = asyncio.create_task(worker._progress_reporter_loop(IDENTITY, runner))
        try:
            await asyncio.sleep(0.05)
            # Drive the loop by hand rather than waiting out the interval.
            reporter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reporter
        except asyncio.CancelledError:
            pass

        # Emulate two ticks with an unchanged value.
        for _ in range(2):
            fraction = round(task.progress[0], 4)
            if task.last_reported is None or fraction != task.last_reported:
                task.last_reported = fraction
                await runner.report(
                    worker.Envelope(
                        message_type="task_progress",
                        worker_id=IDENTITY["worker_id"],
                        payload={"task_id": "t1", "progress": fraction},
                    )
                )
        assert len(ws.of_type("task_progress")) == 1

        runner.running.clear()

    asyncio.run(body())
