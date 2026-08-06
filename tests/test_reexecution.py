"""Partial completion and re-execution (Phase 3.6).

Step 3.6 decides what happens to work that was interrupted. The policy is
**discard and re-execute in full** (Decision #200): a task that lost its
lease is redone from scratch by whoever gets it next, and nothing of the
abandoned attempt is kept, resumed or credited.

That policy is only safe if re-execution is safe, and "the dummy workloads
are pure" is an assertion until something checks it. This file is the
check. It is deliberately separate from `test_executors.py`, which
verifies that an executor gets the *right* answer: what is verified here is
that it gets the *same* answer, every time, no matter what happened to the
attempt before it.

Four properties, each with the failure it would catch:

* **Value determinism** — three of the four types are a pure function of
  their parameters. If one grew a clock, a random seed or a machine
  identifier, re-execution after a reassignment would produce a different
  answer from the attempt that was abandoned, and the system would be
  quietly non-deterministic.
* **No residue from a cancelled attempt** — a chunked loop that kept its
  accumulator anywhere but a local would make the second execution start
  from where the first stopped. `count_to_n` would return more than `n`.
* **Purity in the strict sense** — an executor that wrote a file or opened
  a socket would make re-execution a side effect rather than a repeat, and
  every guarantee in §3.6 would be wrong.
* **No shared state between concurrent executions** — one worker runs
  several tasks in one process, in threads (Decision #93).

`sleep` is the honest exception and it has its own test. Its result is a
**measurement of elapsed time**, not a value derived from its parameters,
so it is identical only to a tolerance. That is stated rather than
smoothed over, and it is not a defect: the coordinator stores the result
body and compares nothing, so a duration that differs between attempts
costs nothing (§3.6.2).

No Postgres, no Redis, no coordinator.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import pathlib
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

os.environ.setdefault("COORDINATOR_URL", "https://example.invalid")
os.environ.setdefault("WORKER_IDENTITY_FILE", "/tmp/test-worker-identity.json")

from worker import executors, worker  # noqa: E402

IDENTITY = {"worker_id": "33333333-3333-3333-3333-333333333333", "worker_credential": "x"}

# The three types whose result is a function of their parameters alone.
# `sleep` is excluded on purpose — see the module docstring and
# `test_sleep_is_a_measurement_so_it_repeats_only_to_a_tolerance`.
VALUE_DETERMINISTIC = [
    ("count_to_n", {"n": 250_000}),
    ("hash_rounds", {"rounds": 500, "algorithm": "sha256"}),
    ("hash_rounds", {"rounds": 500, "algorithm": "sha512"}),
    ("opaque_payload", {"payload_b64": base64.b64encode(b"partial completion").decode()}),
]

# Every module `worker/executors.py` is allowed to import. The point is not
# the list, it is that adding to it has to be a deliberate act: `os`,
# `socket`, `random`, `pathlib`, `requests` or `sqlite3` appearing here
# would each break re-execution safety in a different way.
PERMITTED_IMPORTS = {
    "__future__",
    "base64",
    "binascii",
    "hashlib",
    "json",
    "threading",
    "time",
    "typing",
}


class CancelAfter(threading.Event):
    """A cancel flag that trips after a fixed number of chunk boundaries.

    A real `threading.Event` with `is_set` overridden, rather than a
    duck-typed stand-in, so it is the same type the executors are annotated
    against. A timer-based cancel would make "how far did it get" a race;
    here the partial execution is exact and repeatable: N chunks ran, then
    it stopped.
    """

    def __init__(self, chunks: int) -> None:
        super().__init__()
        self.remaining = chunks
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False


# --------------------------------------------------------------------------
# Repeated execution produces an identical result
# --------------------------------------------------------------------------


@pytest.mark.parametrize("task_type,parameters", VALUE_DETERMINISTIC)
def test_repeated_execution_returns_an_identical_value(task_type, parameters):
    """The exit criterion, at the level the criterion is actually about.

    Three runs in one process. The fingerprint is compared as well as the
    value because the fingerprint is what a live run logs (Decision #106),
    so a determinism claim that held for the value but not for its
    canonical JSON would be unverifiable from a log.
    """
    results = [executors.execute(task_type, parameters) for _ in range(3)]
    assert len(set(map(repr, results))) == 1, results
    assert len({executors.fingerprint(r) for r in results}) == 1


def test_sleep_is_a_measurement_so_it_repeats_only_to_a_tolerance():
    """**Stated, not glossed** (§10): `sleep` returns measured elapsed time.

    Two executions of `sleep(0.3)` are not required to return the same
    float, and asserting that they do would be asserting something false.
    What is required — and is what the policy depends on — is that a
    re-execution runs the workload *again in full*: it sleeps for the
    requested duration, not for what was left of it.
    """
    first = executors.execute("sleep", {"seconds": 0.3})
    second = executors.execute("sleep", {"seconds": 0.3})

    assert first >= 0.3 and second >= 0.3, (first, second)
    assert abs(first - second) < 0.5, (first, second)


# --------------------------------------------------------------------------
# A cancelled attempt is redone in full, not resumed
#
# **The result value cannot evidence this, and finding that out is the point
# of §3.6.4.** These workloads are pure, so a resumed execution and a
# restarted one return the *same* answer — a mutant that saved the partial
# digest and continued from it passed a known-answer assertion cleanly.
# What distinguishes them is the amount of work done, so that is what is
# measured: a full re-execution reports every chunk of progress from the
# first, and a resumed one would start part-way up.
# --------------------------------------------------------------------------


class ProgressLog(list):
    """A progress slot that remembers every fraction written to it.

    `_report` does `progress[0] = ...`, so overriding `__setitem__` records
    the chunk boundaries an execution actually crossed.
    """

    def __init__(self) -> None:
        super().__init__([0.0])
        self.writes: list[float] = []

    def __setitem__(self, index, value):  # noqa: D105
        self.writes.append(value)
        super().__setitem__(index, value)


def test_a_cancelled_count_is_redone_in_full_rather_than_resumed(monkeypatch):
    """100,000 in chunks of 1,000 is 100 progress reports. The cancelled
    attempt gets 3 of them; the re-execution must get all 100 and start at
    the first, not at the fourth."""
    monkeypatch.setattr(executors, "COUNT_CHUNK", 1_000)

    partial = ProgressLog()
    with pytest.raises(executors.ExecutionCancelled):
        executors.execute("count_to_n", {"n": 100_000}, partial, CancelAfter(3))
    assert partial.writes == [0.01, 0.02, 0.03]

    redone = ProgressLog()
    assert executors.execute("count_to_n", {"n": 100_000}, redone) == 100_000
    assert len(redone.writes) == 100
    assert redone.writes[0] == pytest.approx(0.01)
    assert redone.writes[-1] == 1.0


def test_a_cancelled_hash_is_redone_in_full_and_reaches_its_known_answer(monkeypatch):
    """Both halves matter and they check different things: the work count
    says the chained digest was rebuilt from the zero seed, and the known
    answer — computed independently, `test_executors.SHA256_1000_ROUNDS` —
    says rebuilding it produced the right value."""
    expected = "36c1cb4f826ae42ceba848227e0c5f786178ca9dceca6772e5d728d09c30a2f6"
    monkeypatch.setattr(executors, "HASH_CHUNK_ROUNDS", 10)

    partial = ProgressLog()
    with pytest.raises(executors.ExecutionCancelled):
        executors.execute("hash_rounds", {"rounds": 1000}, partial, CancelAfter(5))
    assert len(partial.writes) == 5

    redone = ProgressLog()
    assert executors.execute("hash_rounds", {"rounds": 1000}, redone) == expected
    assert len(redone.writes) == 100
    assert redone.writes[0] == pytest.approx(0.01)


def test_a_re_execution_starts_its_progress_at_zero(monkeypatch):
    """Progress is per-execution state too. A dashboard showing a
    re-executed task resuming at 3% would be describing work that is not
    being reused."""
    monkeypatch.setattr(executors, "COUNT_CHUNK", 1_000)
    partial = ProgressLog()
    with pytest.raises(executors.ExecutionCancelled):
        executors.execute("count_to_n", {"n": 100_000}, partial, CancelAfter(3))
    assert 0.0 < partial[0] < 1.0

    fresh = [0.0]
    executors.execute("count_to_n", {"n": 100_000}, fresh)
    assert fresh[0] == 1.0


# --------------------------------------------------------------------------
# Purity, enforced rather than asserted
# --------------------------------------------------------------------------


def test_execution_opens_no_file_and_no_socket(monkeypatch):
    """Re-execution safety in its strict form: a workload with a side effect
    is not repeatable, it is repeatable-with-consequences.

    `open` and `socket.socket` are made to raise for the duration, so a
    future executor that logged to a file or phoned home would fail here
    rather than in a chaos run.
    """

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an executor touched the outside world")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    for task_type, parameters in VALUE_DETERMINISTIC + [("sleep", {"seconds": 0.01})]:
        executors.execute(task_type, parameters)


def test_the_executor_module_imports_nothing_that_can_reach_the_outside_world():
    """A static counterpart to the runtime check above, and it catches the
    case the runtime one cannot: an import that is present but unused today
    and reached tomorrow."""
    source = pathlib.Path(worker.__file__).with_name("executors.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= PERMITTED_IMPORTS, sorted(imported - PERMITTED_IMPORTS)


def test_concurrent_executions_agree_with_serial_ones():
    """One worker runs several tasks in one process (Decision #93). Module
    state shared between them would show up as a concurrent answer that
    differs from the serial one."""
    serial = [executors.execute(t, p) for t, p in VALUE_DETERMINISTIC]

    with ThreadPoolExecutor(max_workers=len(VALUE_DETERMINISTIC)) as pool:
        concurrent = list(
            pool.map(lambda job: executors.execute(job[0], job[1]), VALUE_DETERMINISTIC * 3)
        )

    assert concurrent == serial * 3


# --------------------------------------------------------------------------
# The same policy, one level up: the worker redoes the whole task
# --------------------------------------------------------------------------


class FakeWS:
    """Same shape as `test_worker_runtime.FakeWS` — records frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def of_type(self, message_type: str) -> list[dict]:
        return [m for m in self.sent if m["message_type"] == message_type]


def _assignment(task_id: str, task_type: str, parameters: dict, attempt: int) -> dict:
    return {
        "message_type": "task_assign",
        "correlation_id": f"corr-{task_id}",
        "payload": {
            "task_id": task_id,
            "task_type": task_type,
            "parameters": parameters,
            "attempt": attempt,
        },
    }


async def _drain() -> None:
    for _ in range(400):
        await asyncio.sleep(0.01)
        if not worker._EXECUTIONS:
            return
    raise AssertionError("executions did not finish")


def test_a_cancelled_task_re_delivered_later_is_executed_in_full_again():
    """**The policy, demonstrated on the real worker path.**

    A task is cancelled part-way — the Phase 3.2 message a reclaim sends —
    and the same task id is delivered again afterwards, which is what the
    original worker sees if it wins the task back after its exclusion
    lapses. The second execution must run the whole workload: `sleep(1.0)`
    interrupted at ~0.3s and then redone takes a full second again, not the
    0.7s that would remain if anything had been resumed.

    It also pins the two reports the policy depends on: the cancelled
    attempt sends `capacity` and **no result**, and the completed one sends
    exactly one `task_result`.
    """

    async def body():
        runner = worker.TaskRunner(2)
        ws, lock = FakeWS(), asyncio.Lock()
        runner.attach(ws, lock)

        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, _assignment("t-36", "sleep", {"seconds": 1.0}, 0), "s1"
        )
        await asyncio.sleep(0.3)
        worker._handle_cancel(
            IDENTITY,
            runner,
            {"message_type": "task_cancel", "payload": {"task_id": "t-36", "reason": "lease_expired"}},
        )
        await _drain()

        assert ws.of_type("task_result") == [], "a cancelled attempt must report no result"
        assert [m["payload"]["task_id"] for m in ws.of_type("capacity")] == ["t-36"]

        # The task comes back on a later attempt.
        await worker._handle_assignment(
            ws, IDENTITY, lock, runner, _assignment("t-36", "sleep", {"seconds": 1.0}, 1), "s1"
        )
        await _drain()

        results = ws.of_type("task_result")
        assert len(results) == 1
        payload = results[0]["payload"]
        assert payload["attempt_number"] == 1
        # Re-executed in full: the second attempt slept its own whole second.
        assert payload["result"] >= 1.0, payload
        assert payload["duration_seconds"] >= 1.0, payload
        # Nothing of either attempt is left behind (§3.6.3).
        assert runner.running == {}

    asyncio.run(body())
