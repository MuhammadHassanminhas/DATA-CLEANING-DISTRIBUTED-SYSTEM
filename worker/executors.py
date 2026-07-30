"""Task executors (Phase 2.4).

The four dummy workloads CLAUDE.md §2 allows in V1 — count to N, hash
rounds, fixed sleep, opaque byte payload. Nothing here knows about
WebSockets, the coordinator, or task state: an executor takes validated
parameters and returns a result, or raises. That separation is what makes
the correctness of "all four task types return correct results" testable
against known-answer vectors rather than against a running fleet, which
matters because Step 2.4 **discards** every result it computes
(Decision #98 — the result envelope is Step 2.5's).

**Every executor is a chunked loop** (Decision #94), not a single blocking
call, for two reasons that are properties of Python rather than choices:

1. A thread cannot be killed. Cancellation is cooperative or it does not
   exist, so the loop checks a flag between chunks. Measured at the design
   sub-gate: a running task stops in 0.04–0.11s.
2. Progress has to be observable from outside the thread. The loop writes
   a fraction into a one-slot list, which is atomic under the GIL — no
   lock, no queue, no `threading` machinery at all.

The chunk sizes below are **recommendations, not measured optima**
(CLAUDE.md §10). What *was* measured is that they do not matter for
throughput: sizes from 1,000 to 500,000 all landed inside run-to-run noise
(−5.5% to +3.2%). They are picked so that a cancel check and a progress
update happen a few times a second on the slowest declared parameters, and
that is the only property being bought here.

These run inside a `ThreadPoolExecutor` owned by `worker.py`, one thread
per running task (Decision #93). They must therefore never touch the event
loop, and they do not: no asyncio import, no shared mutable state beyond
the two slots the caller hands in.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
from typing import Any, Callable

# Cancel/progress granularity. See the module docstring: recommendations,
# and measured to be throughput-neutral.
HASH_CHUNK_ROUNDS = 100_000
COUNT_CHUNK = 1_000_000
SLEEP_SLICE_SECONDS = 0.5

# Belt and braces. The coordinator validates this ceiling before a task is
# ever queued (`coordinator/app/task_types.MAX_OPAQUE_PAYLOAD_BYTES`), so a
# payload over it should be unreachable here. Checked anyway because the
# worker is the thing that has to allocate the bytes, and "the other side
# validated it" is an assumption, not a guarantee.
MAX_OPAQUE_PAYLOAD_BYTES = 64 * 1024

# How much of the result fingerprint is logged (Decision #106). Long enough
# that a match is meaningful for a known-answer check, short enough that the
# log line stays readable.
FINGERPRINT_HEX_CHARS = 12


class UnsupportedTaskType(ValueError):
    """The worker was handed a type it has no executor for.

    Distinct from a failure: nothing went wrong during execution, the task
    simply cannot run here. It drives the refusal path, and the coordinator
    frees the credit rather than saturating it (Decision #101).
    """


class ExecutionCancelled(Exception):
    """Raised out of a chunk loop when the cooperative cancel flag is set.

    Not a task failure — the worker is shutting down, so the task is not
    reported as `FAILED`; the socket is going away with it and the row stays
    where Phase 3 can see it.
    """


class InvalidExecutionParameters(ValueError):
    """Parameters that this executor cannot run.

    The coordinator has already validated them against the registry in
    `coordinator/app/task_types.py`, so reaching this means the two sides
    disagree — worth a clean failure rather than a `KeyError` deep in a
    loop.
    """


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ExecutionCancelled()


def _report(progress: list[float] | None, fraction: float) -> None:
    if progress is not None:
        # One slot, written by this thread, read by the event loop. A single
        # list-item store is atomic under the GIL, which is the whole reason
        # this needs no synchronisation (Decision #94).
        progress[0] = min(1.0, fraction)


def run_count_to_n(
    parameters: dict[str, Any],
    progress: list[float] | None = None,
    cancel: threading.Event | None = None,
) -> int:
    """Count to N. The result is N — trivially checkable, which is the point
    of having a workload whose correct answer is not in doubt."""
    n = _require_int(parameters, "n")
    total = 0
    done = 0
    while done < n:
        _check(cancel)
        step = min(COUNT_CHUNK, n - done)
        for _ in range(step):
            total += 1
        done += step
        _report(progress, done / n)
    return total


def run_hash_rounds(
    parameters: dict[str, Any],
    progress: list[float] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Iterated hashing from a fixed 32-zero-byte seed. CPU-bound, and the
    knob for loading a worker.

    The seed is fixed rather than random precisely so the answer is
    reproducible: `rounds` and `algorithm` determine the digest exactly, so
    a live run can be checked against a vector computed independently.
    """
    rounds = _require_int(parameters, "rounds")
    algorithm = parameters.get("algorithm", "sha256")
    if algorithm not in ("sha256", "sha512"):
        raise InvalidExecutionParameters(f"unsupported algorithm: {algorithm!r}")

    digest = b"\x00" * 32
    done = 0
    while done < rounds:
        _check(cancel)
        step = min(HASH_CHUNK_ROUNDS, rounds - done)
        for _ in range(step):
            digest = hashlib.new(algorithm, digest).digest()
        done += step
        _report(progress, done / rounds)
    return digest.hex()


def run_sleep(
    parameters: dict[str, Any],
    progress: list[float] | None = None,
    cancel: threading.Event | None = None,
) -> float:
    """Fixed sleep — occupies a slot without consuming CPU.

    Sliced rather than one long `time.sleep` so cancellation stays
    sub-second and progress advances while it waits. It still holds a pool
    thread for its whole duration, which is correct: it is holding a
    concurrency slot, and that is what this workload is for.

    Returns the measured elapsed time, not the requested one, so the result
    reflects what happened rather than what was asked.
    """
    seconds = _require_number(parameters, "seconds")
    started = time.monotonic()
    while True:
        _check(cancel)
        elapsed = time.monotonic() - started
        remaining = seconds - elapsed
        if remaining <= 0:
            break
        time.sleep(min(SLEEP_SLICE_SECONDS, remaining))
        _report(progress, (time.monotonic() - started) / seconds)
    _report(progress, 1.0)
    return round(time.monotonic() - started, 3)


def run_opaque_payload(
    parameters: dict[str, Any],
    progress: list[float] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Decode the opaque bytes and echo them back.

    The worker never interprets the contents — that is what "opaque" means
    here — but it does decode them, so the payload path is genuinely
    exercised end to end rather than the string being passed through
    untouched.
    """
    raw_b64 = parameters.get("payload_b64")
    if not isinstance(raw_b64, str):
        raise InvalidExecutionParameters("payload_b64 must be a string")
    _check(cancel)
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidExecutionParameters("payload_b64 is not valid base64") from exc
    if len(raw) > MAX_OPAQUE_PAYLOAD_BYTES:
        raise InvalidExecutionParameters(
            f"decoded payload is {len(raw)} bytes, over the {MAX_OPAQUE_PAYLOAD_BYTES}-byte limit"
        )
    _report(progress, 1.0)
    # Re-encoded from the decoded bytes, not the input string echoed back:
    # that is what makes the round trip real.
    return base64.b64encode(raw).decode("ascii")


def _require_int(parameters: dict[str, Any], key: str) -> int:
    value = parameters.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidExecutionParameters(f"{key} must be an integer >= 1, got {value!r}")
    return value


def _require_number(parameters: dict[str, Any], key: str) -> float:
    value = parameters.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise InvalidExecutionParameters(f"{key} must be a number > 0, got {value!r}")
    return float(value)


Executor = Callable[[dict[str, Any], list[float] | None, threading.Event | None], Any]

# The worker-side counterpart of the coordinator's `TASK_TYPES` registry.
# Deliberately a separate map rather than an import: the worker does not
# depend on the coordinator package (it ships in its own image with its own
# requirements), and the two are joined by the wire protocol, not by a
# shared module. Adding a fifth type means one entry here and one there —
# still no envelope change and no migration (Decision #82).
EXECUTORS: dict[str, Executor] = {
    "count_to_n": run_count_to_n,
    "hash_rounds": run_hash_rounds,
    "sleep": run_sleep,
    "opaque_payload": run_opaque_payload,
}


def execute(
    task_type: str,
    parameters: dict[str, Any] | None,
    progress: list[float] | None = None,
    cancel: threading.Event | None = None,
) -> Any:
    """Run one task to completion in the calling thread.

    Raises `UnsupportedTaskType` for a type with no executor,
    `InvalidExecutionParameters` for parameters it cannot run, and
    `ExecutionCancelled` if the cooperative flag is set mid-run. Anything
    else an executor raises propagates untouched — `worker.py` reports it
    as `task_failed` (Decision #102).
    """
    executor = EXECUTORS.get(task_type)
    if executor is None:
        raise UnsupportedTaskType(f"no executor for task type: {task_type!r}")
    return executor(parameters or {}, progress, cancel)


def fingerprint(result: Any) -> str:
    """A short, deterministic fingerprint of a result (Decision #106).

    Step 2.4 discards results (Decision #98), which leaves a real question:
    how is "returns correct results" evidenced from a live run rather than
    only from unit tests? Logging the result itself is not an option —
    `opaque_payload` echoes caller-supplied bytes, and payload data must
    never reach a log (CLAUDE.md §12).

    A hash is the way out. It is deterministic, so a live log line can be
    checked against a vector computed independently, and it discloses
    nothing about the bytes that produced it. Canonical JSON keeps that
    determinism stable across processes and Python versions, which a
    `repr()` would not.
    """
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:FINGERPRINT_HEX_CHARS]
