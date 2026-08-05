#!/usr/bin/env python3
"""Load testing harness — Step 2.8. Scripted, repeatable, versioned (§4).

What this measures is the **coordinator**, end to end: enqueue, assignment,
execution reporting, result submission, completion. Every simulated worker
is a genuine registered identity with its own WebSocket session, its own
access token, its own declared credits and its own result envelopes. The
coordinator cannot tell one of these from a container or a laptop — that is
invariant §3.5, and it is what makes this a fair fleet rather than a mock.

**Honest ceiling, stated up front (§10): this is N sessions from one
process, not N machines.** The coordinator side is real; the worker side
shares one event loop, one CPU and one network stack. Read every number
below as "one coordinator under a fleet of N sessions", never as "N
machines". The `--url` pointed at a public ingress is the only thing that
makes any of it a real-network measurement.

Two more simplifications, marked rather than hidden:

* `sleep` tasks are awaited with `asyncio.sleep`, not run through
  `worker.executors.run_sleep`. A sleep workload consumes no CPU by
  definition, so the simulation is faithful, and 400 concurrent slots would
  otherwise need 400 OS threads. Every other type runs the **real**
  executor from `worker/executors.py`, so results are genuine.
* Results are submitted with a retry loop but no bounded pending buffer —
  the worker's Decision #112 machinery is worker-side state this harness
  does not need to reproduce to load the coordinator.

Scenarios
---------
`burst`       One batch of N tasks at a fleet of W workers. The drain
              measurement: throughput, latency percentiles, zero loss.
`sustained`   A fixed enqueue rate held for D seconds. Answers whether the
              pipeline keeps up or the queue grows without bound.
`mixed`       Interleaved task types and durations in one batch, so the
              short work behind a long task is visible rather than averaged
              away.
`saturation`  The same burst repeated at increasing fleet sizes. Produces
              the fleet-size/latency table that Decision #135 could not.

Usage
-----
    python scripts/loadtest.py burst \
        --url https://localhost:8443 \
        --enrollment-secret "$ENROLLMENT_SECRET" \
        --admin-secret "$ADMIN_SECRET" \
        --workers 100 --tasks 10000 --insecure

`--insecure` skips certificate verification and is for the local dev CA
only; the public ingress carries a real Let's Encrypt certificate and needs
no such flag.

Needs the `websockets` package (a WebSocket client is not in the standard
library) and `worker/executors.py` on the path — run it from a venv with
`worker/requirements.txt` installed, or inside the worker image.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import shlex
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from worker import executors  # noqa: E402  — path set above

PROTOCOL_VERSION = "1.0"
ALL_TYPES = ["count_to_n", "hash_rounds", "sleep", "opaque_payload"]

# The harness's own defaults. Every one is a *harness* knob, not a claim
# about the system: the workload is deliberately cheap so that what
# saturates is the coordinator rather than this process.
DEFAULT_WORKLOAD = {"task_type": "count_to_n", "parameters": {"n": 1000}}

# `GET /tasks` caps a page at TASK_LIST_MAX_LIMIT (default 200).
READ_PAGE = 200

# A result that has been on the wire this long with no ack is resent. The
# real worker uses a grace period plus backoff (Decisions #116/#117); this
# is the same idea with the backoff left out.
RESULT_RETRY_SECONDS = 10.0

HEARTBEAT_SECONDS = 5.0

# Phase 3.5 reconnect backoff for the simulated fleet. Full jitter, and the
# same three numbers as `worker.py`'s WS_BACKOFF_* defaults on purpose: the
# reconnect storm this harness measures has to be the storm the real fleet
# would produce, and a harness that reconnected faster than the shipped
# worker would report a herd the coordinator never actually faces.
RECONNECT_BACKOFF_BASE_SECONDS = 1.0
RECONNECT_BACKOFF_FACTOR = 2.0
RECONNECT_BACKOFF_MAX_SECONDS = 30.0


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _ctx(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(
    url: str,
    payload: dict | None,
    insecure: bool,
    headers: dict | None = None,
    *,
    method: str | None = None,
    timeout: int = 120,
    retries: int = 6,
) -> tuple[int, Any]:
    """One HTTP call, retrying a 429 rather than failing the run.

    Both rate limiters in front of this API are deliberate and neither is
    a fault: the coordinator limits the operator task API per source IP
    (Decision #125) and the public ingress limits requests per second
    (Step 1.5.5). A load harness that fell over on a 429 would be
    measuring the limiter. It backs off and continues instead, and the
    number of times it had to is reported, because a run that spent its
    time in backoff is a run whose throughput figure means something
    different.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    verb = method or ("POST" if data is not None else "GET")
    delay = 1.0
    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=verb,
        )
        try:
            with urllib.request.urlopen(request, context=_ctx(insecure), timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            body = exc.read() or b"{}"
            if exc.code == 429 and attempt < retries - 1:
                _RATE_LIMITED[0] += 1
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"detail": body.decode(errors="replace")}
    return 429, {"detail": "rate limited after retries"}


# Counted globally rather than threaded through every call: it is a
# property of the run, and every caller would otherwise have to return it.
_RATE_LIMITED = [0]


def _metrics(url: str, insecure: bool) -> dict[str, float]:
    """Scrape the coordinator's own /metrics.

    Returns `{}` rather than raising when it is unreachable. That is the
    normal case against a public ingress, which routes `/metrics` nowhere
    on purpose — the resource figures are then reported as absent instead
    of invented (§10).
    """
    try:
        request = urllib.request.Request(f"{url.rstrip('/')}/metrics")
        with urllib.request.urlopen(request, context=_ctx(insecure), timeout=15) as response:
            text = response.read().decode()
    except Exception:  # noqa: BLE001 — an unreachable scrape is not a failed run
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile.

    Nearest-rank rather than interpolated deliberately: every value
    reported is then a latency some task actually had, which is what makes
    "p99 was 4.1s" checkable against a task row rather than an artefact of
    the estimator.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return round(ordered[rank - 1], 4)


def summarise(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 4),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
    }


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The simulated worker
# --------------------------------------------------------------------------

class SimWorker:
    """One worker: register, authenticate, connect, execute, submit.

    The full Step 2.4/2.5 lifecycle, not Step 2.3's acknowledge-only
    harness — a task has to reach `COMPLETED` for "zero loss" to mean
    anything, and only a real result envelope gets it there.
    """

    def __init__(self, base: str, enrollment_secret: str, insecure: bool,
                 *, max_concurrent: int, types: list[str]) -> None:
        self.base = base.rstrip("/")
        self.enrollment_secret = enrollment_secret
        self.insecure = insecure
        self.max_concurrent = max_concurrent
        self.types = types

        self.worker_id: str | None = None
        self.credential: str | None = None
        self.ready = asyncio.Event()
        self.failed: str | None = None

        # Phase 3.5. Counted, because "workers reconnect automatically
        # without re-enrollment" is only checkable if both halves are
        # measured: `registrations` must stay at 1 across any number of
        # `reconnects`. A worker that quietly re-registers would satisfy
        # "reconnected" while failing the criterion.
        self.registrations = 0
        self.reconnects = 0
        self.sessions = 0
        self.reconnect_seconds: list[float] = []
        self.disconnected_at: float | None = None
        self.last_error: str | None = None

        self.received: list[str] = []       # task ids delivered here
        self.completed: set[str] = set()     # acked accepted
        self.refused: list[str] = []
        self.pending: dict[str, dict] = {}   # task_id -> envelope awaiting ack
        self.sent_at: dict[str, float] = {}

        self._slots: asyncio.Semaphore | None = None
        self._ws = None
        self._send_lock: asyncio.Lock | None = None
        self._epoch = 0

    # -- HTTP identity ------------------------------------------------

    def _register(self) -> None:
        status, body = _request(
            f"{self.base}/workers/register",
            {"enrollment_secret": self.enrollment_secret, "agent_version": "loadtest"},
            self.insecure,
        )
        if status != 201:
            raise RuntimeError(f"register failed: {status} {body}")
        self.worker_id = body["worker_id"]
        self.credential = body["worker_credential"]
        self.registrations += 1

    def _token(self) -> str:
        status, body = _request(
            f"{self.base}/workers/token/refresh",
            {"worker_id": self.worker_id, "worker_credential": self.credential},
            self.insecure,
        )
        if status != 200:
            raise RuntimeError(f"token refresh failed: {status} {body}")
        return body["access_token"]

    # -- wire ---------------------------------------------------------

    async def _send(self, message_type: str, payload: dict, correlation_id: str) -> bool:
        if self._ws is None or self._send_lock is None:
            return False
        frame = {
            "message_type": message_type,
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": self.worker_id,
            "correlation_id": correlation_id,
            "session_epoch": self._epoch,
            "timestamp": "",
            "payload": payload,
        }
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(frame))
            return True
        except Exception:  # noqa: BLE001 — a dead socket is a normal end of run
            return False

    async def run(self, stop: asyncio.Event) -> None:
        """Register once, then hold a session for as long as `stop` is clear.

        **Phase 3.5 turned this from one session into a reconnect loop**,
        and the shape is deliberate: registration happens exactly once,
        outside the loop, so a run in which the coordinator restarts can
        distinguish "the fleet came back" from "the fleet enrolled again".
        The token *is* refreshed per attempt, because it is short-lived by
        design and an expired one is not a re-enrollment — the long-lived
        credential from the single registration is what mints it.

        For every scenario written before 3.5 this is a no-op: they set
        `stop` before the socket ever closes, so the loop runs once.
        """
        try:
            await asyncio.to_thread(self._register)
        except Exception as exc:  # noqa: BLE001
            self.failed = f"{type(exc).__name__}: {exc}"
            self.ready.set()
            return

        self._slots = asyncio.Semaphore(self.max_concurrent)
        self._send_lock = asyncio.Lock()
        ws_url = self.base.replace("https://", "wss://", 1) + "/ws/connect"
        failures = 0

        while not stop.is_set():
            try:
                await self._session(ws_url, stop)
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                # Only the *first* failure is reported. A worker that
                # reconnected successfully afterwards did not fail the run,
                # and leaving `failed` set would make `connect_fleet`'s
                # count of broken workers wrong for every later scenario.
                if not self.ready.is_set():
                    self.failed = self.failed or f"{type(exc).__name__}: {exc}"
                    self.ready.set()
                    return
                self.last_error = f"{type(exc).__name__}: {exc}"

            if stop.is_set():
                break
            self.reconnects += 1
            # Full jitter, the same shape as `worker.py`'s reconnect
            # backoff and for the same reason: the whole fleet is
            # reconnecting at once and an unjittered retry would arrive as
            # one wavefront. This is the harness half of the thundering
            # herd the restart scenario measures.
            delay = random.random() * min(
                RECONNECT_BACKOFF_MAX_SECONDS,
                RECONNECT_BACKOFF_BASE_SECONDS * (RECONNECT_BACKOFF_FACTOR ** failures),
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _session(self, ws_url: str, stop: asyncio.Event) -> None:
        """One connection, from token refresh to socket close."""
        token = await asyncio.to_thread(self._token)
        async with websockets.connect(
            ws_url,
            extra_headers={"Authorization": f"Bearer {token}"},
            ssl=_ctx(self.insecure),
            max_size=None,
            open_timeout=60,
            # The targeted half of the fix in `shutdown_fleet`: do not
            # spend the library's default patience on a close handshake
            # the far side may never answer.
            close_timeout=5,
        ) as ws:
            self._ws = ws
            await self._send("hello", {
                "agent_version": "loadtest",
                "max_concurrent": self.max_concurrent,
                "supported_task_types": self.types,
                # Declared so the coordinator credits work this worker is
                # still executing across the restart (Decision #101).
                # Without it a reconnecting worker looks idle and is handed
                # a full set of new credits on top of what it is running.
                "tasks_in_flight": max(0, len(self.received) - len(self.completed)),
            }, str(uuid.uuid4()))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if ack.get("message_type") != "hello_ack":
                raise RuntimeError(f"handshake failed: {ack}")
            self._epoch = int(ack.get("session_epoch") or 0)
            self.sessions += 1
            if self.disconnected_at is not None:
                self.reconnect_seconds.append(time.monotonic() - self.disconnected_at)
                self.disconnected_at = None
            self.ready.set()

            helpers = [
                asyncio.create_task(self._heartbeat_loop(stop)),
                asyncio.create_task(self._result_retry_loop(stop)),
            ]
            try:
                await self._receive_loop(stop)
            finally:
                self.disconnected_at = time.monotonic()
                self._ws = None
                for helper in helpers:
                    helper.cancel()
                await asyncio.gather(*helpers, return_exceptions=True)

    async def _receive_loop(self, stop: asyncio.Event) -> None:
        executing: set[asyncio.Task] = set()
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001 — socket closed under us
                return
            message = json.loads(raw)
            kind = message.get("message_type")
            correlation_id = str(message.get("correlation_id") or uuid.uuid4())

            if kind == "ping":
                await self._send("pong", {}, correlation_id)
            elif kind == "task_assign":
                task = asyncio.create_task(self._handle_assignment(message, correlation_id))
                executing.add(task)
                task.add_done_callback(executing.discard)
            elif kind == "task_result_ack":
                payload = message.get("payload") or {}
                task_id = str(payload.get("task_id") or "")
                # Dropped on a rejection too, exactly as the real worker
                # does: a rejected envelope will not become acceptable on
                # the tenth attempt (worker.py `_handle_result_ack`).
                self.pending.pop(task_id, None)
                self.sent_at.pop(task_id, None)
                if payload.get("accepted"):
                    self.completed.add(task_id)

        for task in list(executing):
            task.cancel()
        await asyncio.gather(*executing, return_exceptions=True)

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        sequence = 0
        while not stop.is_set():
            sequence += 1
            await self._send("heartbeat", {
                "sequence": sequence,
                "uptime_seconds": round(sequence * HEARTBEAT_SECONDS, 1),
                "agent_version": "loadtest",
                "status": "ONLINE",
                "cpu_percent": None,
                "memory_percent": None,
            }, str(uuid.uuid4()))
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _result_retry_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(RESULT_RETRY_SECONDS / 2)
            now = time.monotonic()
            for task_id, envelope in list(self.pending.items()):
                if now - self.sent_at.get(task_id, now) < RESULT_RETRY_SECONDS:
                    continue
                self.sent_at[task_id] = now
                await self._send("task_result", envelope, envelope["task_id"])

    async def _handle_assignment(self, message: dict, correlation_id: str) -> None:
        payload = message.get("payload") or {}
        task_id = str(payload.get("task_id") or "")
        task_type = str(payload.get("task_type") or "")
        parameters = payload.get("parameters") or {}

        if task_type not in self.types:
            self.refused.append(task_id)
            await self._send("task_ack", {
                "task_id": task_id, "accepted": False,
                "reason_code": "unsupported_task_type",
                "reason": f"unsupported_task_type: {task_type}",
            }, correlation_id)
            return

        if self._slots.locked():
            self.refused.append(task_id)
            await self._send("task_ack", {
                "task_id": task_id, "accepted": False,
                "reason_code": "at_capacity", "reason": f"at_capacity: {task_type}",
            }, correlation_id)
            return

        await self._slots.acquire()
        self.received.append(task_id)
        try:
            await self._send("task_ack", {"task_id": task_id, "accepted": True, "reason": None},
                             correlation_id)
            await self._send("task_started", {"task_id": task_id, "task_type": task_type},
                             correlation_id)

            started = time.monotonic()
            try:
                result = await self._execute(task_type, parameters)
            except Exception as exc:  # noqa: BLE001 — reported, never crashes the loop
                await self._send("task_failed",
                                 {"task_id": task_id, "error_type": type(exc).__name__},
                                 correlation_id)
                return

            envelope = {
                "task_id": task_id,
                "status": "COMPLETED",
                "attempt_number": int(payload.get("attempt") or 0),
                "session_epoch": self._epoch,
                "idempotency_token": uuid.uuid4().hex,
                "duration_seconds": round(time.monotonic() - started, 3),
                "result": result,
                "truncated": False,
            }
            self.pending[task_id] = envelope
            self.sent_at[task_id] = time.monotonic()
            await self._send("task_result", envelope, correlation_id)
        finally:
            self._slots.release()

    async def _execute(self, task_type: str, parameters: dict) -> Any:
        # ponytail: `sleep` is awaited rather than run through the real
        # executor — a no-CPU workload simulates faithfully and 400
        # concurrent slots would otherwise need 400 OS threads. Swap in
        # `executors.run_sleep` behind a thread pool if a run ever needs
        # the executor's own timing path measured.
        if task_type == "sleep":
            seconds = float(parameters.get("seconds") or 0)
            await asyncio.sleep(seconds)
            return round(seconds, 3)
        return await asyncio.to_thread(executors.execute, task_type, parameters)


# --------------------------------------------------------------------------
# Fleet
# --------------------------------------------------------------------------

async def connect_fleet(args, count: int) -> tuple[list[SimWorker], list[asyncio.Task], asyncio.Event]:
    """Bring `count` workers up, in batches, and wait for every handshake.

    Batched because a hundred simultaneous registrations is a
    denial-of-service against the coordinator's own registration path
    rather than a load test of the pipeline — and because the registration
    limiter (Decision #87) is per source IP and would refuse most of them.
    """
    stop = asyncio.Event()
    workers = [
        SimWorker(args.url, args.enrollment_secret, args.insecure,
                  max_concurrent=args.max_concurrent, types=ALL_TYPES)
        for _ in range(count)
    ]
    runners: list[asyncio.Task] = []
    for start in range(0, count, args.connect_batch):
        batch = workers[start:start + args.connect_batch]
        runners.extend(asyncio.create_task(worker.run(stop)) for worker in batch)
        await asyncio.gather(*(worker.ready.wait() for worker in batch))
        if start + args.connect_batch < count:
            await asyncio.sleep(args.connect_pause)

    broken = [worker.failed for worker in workers if worker.failed]
    if broken:
        print(f"warning: {len(broken)} of {count} workers failed to connect; "
              f"first: {broken[0]}", file=sys.stderr)
    return workers, runners, stop


async def shutdown_fleet(runners: list[asyncio.Task], stop: asyncio.Event,
                         timeout: float = 15.0) -> None:
    """Drop every session, and refuse to wait forever for a polite close.

    **Bounded because an unbounded wait was measured hanging for over
    fifteen minutes.** Against the public ingress, three sessions held the
    process long after every task had completed, parked in the WebSocket
    closing handshake with the peer's close frame never arriving — the
    sockets were still `ESTABLISHED` while the run looked stuck. By this
    point the measurement is finished and the read-back has not started, so
    there is nothing left to lose: the sockets die with the process. It is
    reported rather than silently swallowed, because a fleet that will not
    close is worth knowing about.
    """
    # **Asked to stop, not cancelled**, and the order matters — this cost a
    # run against staging that hung for nineteen minutes after every task
    # had already completed.
    #
    # Cancelling looks like the obvious way to end a session and is a trap
    # here. A cancelled task unwinds through `async with
    # websockets.connect(...)`, whose `__aexit__` enters the closing
    # handshake; if the peer's close frame never comes back the task never
    # finishes. **Nothing downstream can rescue that**: `wait_for` waits for
    # its own cancellation to be acted on, and `asyncio.run`'s teardown
    # re-gathers every surviving task before it will return — so the run
    # hangs before it can even print the report it has already computed.
    #
    # Setting `stop` instead lets `_receive_loop` return within one recv
    # timeout and the context manager close normally, off the cancellation
    # path entirely. Cancelling is kept only as a last resort for a session
    # that ignores the flag, and `asyncio.wait` is used rather than
    # `gather` because it hands back stragglers instead of waiting on them.
    stop.set()
    if not runners:
        # `asyncio.wait` raises on an empty set where `gather` accepted one,
        # so replacing the second with the first introduced this. It is
        # reachable from the documented `--workers 0` failure demo, which
        # must reach its verdict rather than a traceback.
        return
    _, pending = await asyncio.wait(runners, timeout=timeout)
    if pending:
        for runner in pending:
            runner.cancel()
        _, still_pending = await asyncio.wait(pending, timeout=5.0)
        print(f"warning: {len(pending)} of {len(runners)} worker sessions ignored the "
              f"stop flag; {len(still_pending)} also survived cancellation and are "
              f"abandoned", file=sys.stderr)


# --------------------------------------------------------------------------
# Coordinator-side read-back — the authoritative count
# --------------------------------------------------------------------------

def enqueue(args, task_type: str, parameters: dict, count: int,
            correlation_id: str) -> tuple[float, int]:
    """One `POST /tasks`. Returns (seconds, accepted count).

    One request rather than `count` requests: the public ingress
    rate-limits per second, so a per-task loop would measure nginx.
    """
    started = time.monotonic()
    status, body = _request(
        f"{args.url.rstrip('/')}/tasks",
        {"task_type": task_type, "parameters": parameters, "count": count},
        args.insecure,
        headers={"X-Admin-Secret": args.admin_secret, "X-Correlation-ID": correlation_id},
    )
    if status != 201:
        raise RuntimeError(f"enqueue failed: {status} {body}")
    return time.monotonic() - started, int(body.get("count") or 0)


def read_back(args, correlation_id: str) -> dict[str, Any]:
    """Page every task the batch created and count what became of it.

    **This is the count that decides the run**, not the harness's own
    tally. The harness knows what it was told; the coordinator's rows are
    what happened, and every timestamp in them came from one Postgres
    clock, so latency here needs no agreement between machines.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {"correlation_id": correlation_id, "limit": READ_PAGE, "offset": offset}
        )
        status, body = _request(
            f"{args.url.rstrip('/')}/tasks?{query}", None, args.insecure,
            headers={"X-Admin-Secret": args.admin_secret},
        )
        if status != 200:
            raise RuntimeError(f"listing failed: {status} {body}")
        rows.extend(body.get("tasks") or [])
        if not body.get("has_more"):
            break
        offset += READ_PAGE

    statuses = Counter(row.get("status") for row in rows)
    end_to_end: list[float] = []
    queue_wait: list[float] = []
    for row in rows:
        created = _parse_ts(row.get("created_at"))
        assigned = _parse_ts(row.get("assigned_at"))
        completed = _parse_ts(row.get("completed_at"))
        if created is not None and completed is not None:
            end_to_end.append(completed - created)
        if created is not None and assigned is not None:
            queue_wait.append(assigned - created)

    ids = [row.get("task_id") for row in rows]
    return {
        "rows": len(rows),
        "distinct_task_ids": len(set(ids)),
        "by_status": dict(statuses),
        "completed": statuses.get("COMPLETED", 0),
        "with_result": sum(1 for row in rows if row.get("has_result")),
        "end_to_end_seconds": summarise(end_to_end),
        "queue_wait_seconds": summarise(queue_wait),
        "_completions": [
            _parse_ts(row.get("completed_at")) for row in rows
            if row.get("status") == "COMPLETED" and row.get("completed_at")
        ],
    }


def depth(args) -> dict:
    status, body = _request(
        f"{args.url.rstrip('/')}/tasks/depth", None, args.insecure,
        headers={"X-Admin-Secret": args.admin_secret},
    )
    if status != 200:
        raise RuntimeError(f"depth failed: {status} {body}")
    return body


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

class Sampler:
    """Queue depth and coordinator resource usage over the run.

    Depth over time is the difference between "the queue drained" and "the
    queue never built up in the first place", and a single before/after
    pair cannot tell those apart.
    """

    def __init__(self, args) -> None:
        self.args = args
        self.samples: list[dict] = []
        self._task: asyncio.Task | None = None
        self._started: float | None = None

    def elapsed(self) -> float:
        """Seconds since sampling began, on the samples' own clock.

        Exists so a caller can mark the end of a phase in sample time —
        `sustained` needs to judge the queue on the window it was
        *offering* load, not on the tail where it was only draining.
        """
        return 0.0 if self._started is None else time.monotonic() - self._started

    async def _loop(self) -> None:
        started = self._started = time.monotonic()
        while True:
            try:
                current = await asyncio.to_thread(depth, self.args)
                metrics = await asyncio.to_thread(_metrics, self.args.url, self.args.insecure)
                self.samples.append({
                    "t": round(time.monotonic() - started, 2),
                    "depth": current.get("depth"),
                    "cpu_seconds": metrics.get("process_cpu_seconds_total"),
                    "rss_bytes": metrics.get("process_resident_memory_bytes"),
                })
            except Exception:  # noqa: BLE001 — a lost sample must not end the run
                pass
            await asyncio.sleep(self.args.sample_interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def report(self) -> dict[str, Any]:
        depths = [s["depth"] for s in self.samples if s["depth"] is not None]
        cpu = [s["cpu_seconds"] for s in self.samples if s["cpu_seconds"] is not None]
        rss = [s["rss_bytes"] for s in self.samples if s["rss_bytes"] is not None]
        out: dict[str, Any] = {
            "samples": len(self.samples),
            "interval_seconds": self.args.sample_interval,
            "queue_depth_max": max(depths) if depths else None,
            "queue_depth_final": depths[-1] if depths else None,
            "queue_depth_series": [[s["t"], s["depth"]] for s in self.samples],
        }
        if len(cpu) >= 2 and self.samples:
            window = self.samples[-1]["t"] - self.samples[0]["t"]
            burned = cpu[-1] - cpu[0]
            out["coordinator_cpu_seconds"] = round(burned, 3)
            out["coordinator_cpu_percent_of_one_core"] = (
                round(100 * burned / window, 2) if window > 0 else None
            )
        else:
            # Not an omission: /metrics is deliberately unrouted on the
            # public ingress, so this is absent rather than invented (§10).
            out["coordinator_cpu_percent_of_one_core"] = None
        out["coordinator_rss_bytes_max"] = max(rss) if rss else None
        return out

    def depths_until(self, mark: float) -> list[int]:
        """Depth samples taken at or before `mark` seconds of sample time."""
        return [s["depth"] for s in self.samples
                if s["depth"] is not None and s["t"] <= mark]


def kept_up(depths: list[int], batch: int) -> bool:
    """Did the queue keep up over the window these samples cover?

    **The window matters more than the rule**, and getting it wrong is how
    this function was first written: judging the depth *after* the offer
    stopped reported a run whose queue climbed to 2,116 as having kept up,
    because by then it had drained back to zero. Every sample handed in
    here must come from the window load was actually being offered.

    The rule itself is deliberately generous: a pipeline that keeps up
    never carries more than about one enqueue batch of backlog, since each
    batch lands at once and is worked off before the next. Anything beyond
    that is accumulation, which is the thing worth reporting.
    """
    if len(depths) < 2:
        return True
    return depths[-1] - depths[0] <= max(batch, 1)


async def drain_wait(args, workers: list[SimWorker], expected: int, timeout: float,
                     stall_seconds: float = 30.0) -> float:
    """Wait for the batch to drain.

    The fast path watches the fleet's **own** acks rather than polling
    `GET /tasks/depth` in a tight loop, because depth reaches zero when the
    last task is *assigned*, not when it completes, and a poll tight enough
    to time a drain would itself be load on the endpoint being measured.

    **The stall path exists because the fast path assumed this fleet was
    the only fleet, and against staging that is false.** A deployed
    environment has workers of its own — staging runs a `demo-worker`
    (Decision #121) — and a task one of them completes is completed
    perfectly correctly while producing no acknowledgement here. Waiting
    for an ack per task then hangs until the timeout on a run that
    finished in seconds, which is exactly what happened: 300 of 300 tasks
    `COMPLETED` in the database while the harness sat waiting.

    So when nothing has landed here for `stall_seconds`, the queue is
    consulted; an empty queue means there is nothing left to wait for.
    `read_back` remains the authority on what actually happened either way
    (Decision #140) — this only decides when to stop waiting.
    """
    started = time.monotonic()
    last_count = -1
    last_change = time.monotonic()
    while time.monotonic() - started < timeout:
        done = sum(len(w.completed) for w in workers)
        if done >= expected:
            break
        if done != last_count:
            last_count = done
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= stall_seconds:
            try:
                if (await asyncio.to_thread(depth, args)).get("depth") == 0:
                    break
            except Exception:  # noqa: BLE001 — a failed probe just means keep waiting
                pass
            last_change = time.monotonic()
        await asyncio.sleep(0.25)
    return time.monotonic() - started


def throughput(completions: list[float | None], fallback_seconds: float,
               count: int) -> dict[str, Any]:
    """Completions per second, measured from the coordinator's own
    `completed_at` stamps where there are enough of them to span a window."""
    stamps = sorted(t for t in completions if t is not None)
    if len(stamps) >= 2 and stamps[-1] > stamps[0]:
        window = stamps[-1] - stamps[0]
        return {
            "tasks": len(stamps),
            "window_seconds": round(window, 3),
            "tasks_per_second": round(len(stamps) / window, 1),
            "source": "coordinator completed_at span",
        }
    return {
        "tasks": count,
        "window_seconds": round(fallback_seconds, 3),
        "tasks_per_second": round(count / fallback_seconds, 1) if fallback_seconds > 0 else None,
        "source": "harness wall clock",
    }


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

async def run_burst(args, *, workers_count: int | None = None,
                    tasks_count: int | None = None) -> dict[str, Any]:
    """One batch at a connected fleet — the drain measurement."""
    fleet_size = workers_count if workers_count is not None else args.workers
    task_count = tasks_count if tasks_count is not None else args.tasks

    workers, runners, stop = await connect_fleet(args, fleet_size)
    connected = sum(1 for w in workers if w.failed is None)
    baseline = depth(args)

    sampler = Sampler(args)
    sampler.start()

    correlation_id = str(uuid.uuid4())
    enqueue_seconds, accepted = await asyncio.to_thread(
        enqueue, args, args.task_type, json.loads(args.parameters), task_count, correlation_id
    )
    elapsed = await drain_wait(args, workers, accepted, args.timeout)
    await asyncio.sleep(2.0)  # let the last acks and completions land
    await sampler.stop()

    delivered = [task_id for w in workers for task_id in w.received]
    duplicates = [t for t, n in Counter(delivered).items() if n > 1]
    await shutdown_fleet(runners, stop)

    rows = await asyncio.to_thread(read_back, args, correlation_id)
    completions = rows.pop("_completions")

    per_worker = sorted((len(w.received) for w in workers), reverse=True)
    return {
        "scenario": "burst",
        "correlation_id": correlation_id,
        "fleet": {
            "requested": fleet_size,
            "connected": connected,
            "credits_each": args.max_concurrent,
            "credits_total": connected * args.max_concurrent,
            "workers_that_got_work": sum(1 for w in workers if w.received),
            "per_worker_max": per_worker[0] if per_worker else 0,
            "per_worker_min": per_worker[-1] if per_worker else 0,
        },
        "enqueue": {
            "requested": task_count,
            "accepted": accepted,
            "seconds": round(enqueue_seconds, 3),
            "tasks_per_second": round(accepted / enqueue_seconds, 1) if enqueue_seconds else None,
        },
        "drain_seconds": round(elapsed, 3),
        "throughput": throughput(completions, elapsed, rows["completed"]),
        "latency": {
            "end_to_end_seconds": rows["end_to_end_seconds"],
            "queue_wait_seconds": rows["queue_wait_seconds"],
        },
        "delivery": {
            "delivered": len(delivered),
            "distinct": len(set(delivered)),
            "duplicate_assignments": len(duplicates),
            "refusals": sum(len(w.refused) for w in workers),
        },
        "coordinator": sampler.report(),
        "read_back": {k: v for k, v in rows.items()
                      if k not in ("end_to_end_seconds", "queue_wait_seconds")},
        "queue_depth_before": baseline.get("depth"),
        "rate_limited_retries": _RATE_LIMITED[0],
        "checks": _burst_checks(accepted, task_count, rows, duplicates),
    }


def _burst_checks(accepted: int, requested: int, rows: dict, duplicates: list) -> dict[str, bool]:
    return {
        "every_task_accepted": accepted == requested,
        "every_task_read_back": rows["rows"] == accepted,
        "no_duplicate_task_rows": rows["distinct_task_ids"] == rows["rows"],
        "every_task_completed": rows["completed"] == accepted,
        "every_completion_has_a_result": rows["with_result"] == rows["completed"],
        "no_duplicate_assignments": not duplicates,
    }


async def run_sustained(args) -> dict[str, Any]:
    """A fixed enqueue rate held for a fixed time.

    The question a burst cannot answer: does the pipeline keep up, or does
    the queue grow without bound? The depth series is the answer, and it is
    reported whether or not it is flattering.
    """
    workers, runners, stop = await connect_fleet(args, args.workers)
    connected = sum(1 for w in workers if w.failed is None)

    sampler = Sampler(args)
    sampler.start()

    correlation_id = str(uuid.uuid4())
    batch = max(1, int(args.rate * args.enqueue_interval))
    deadline = time.monotonic() + args.seconds
    submitted = 0
    enqueue_times: list[float] = []
    while time.monotonic() < deadline:
        cycle = time.monotonic()
        seconds, accepted = await asyncio.to_thread(
            enqueue, args, args.task_type, json.loads(args.parameters), batch, correlation_id
        )
        submitted += accepted
        enqueue_times.append(seconds)
        remaining = args.enqueue_interval - (time.monotonic() - cycle)
        if remaining > 0:
            await asyncio.sleep(remaining)

    # Marked here, before the drain, because the verdict below belongs to
    # the window load was being offered — see `kept_up`.
    offer_end = sampler.elapsed()
    elapsed = await drain_wait(args, workers, submitted, args.timeout)
    await asyncio.sleep(2.0)
    await sampler.stop()
    await shutdown_fleet(runners, stop)

    rows = await asyncio.to_thread(read_back, args, correlation_id)
    completions = rows.pop("_completions")
    sampled = sampler.report()
    during_hold = sampler.depths_until(offer_end)

    return {
        "scenario": "sustained",
        "correlation_id": correlation_id,
        "fleet": {"requested": args.workers, "connected": connected,
                  "credits_total": connected * args.max_concurrent},
        "offered_rate_per_second": args.rate,
        "hold_seconds": args.seconds,
        "batches": len(enqueue_times),
        "submitted": submitted,
        "achieved_enqueue_rate": round(submitted / args.seconds, 1) if args.seconds else None,
        "drain_after_stop_seconds": round(elapsed, 3),
        "throughput": throughput(completions, args.seconds + elapsed, rows["completed"]),
        "latency": {
            "end_to_end_seconds": rows["end_to_end_seconds"],
            "queue_wait_seconds": rows["queue_wait_seconds"],
        },
        "queue_kept_up": kept_up(during_hold, batch),
        "queue_depth_during_hold": during_hold,
        "queue_growth_during_hold": (
            during_hold[-1] - during_hold[0] if len(during_hold) >= 2 else 0
        ),
        "coordinator": sampled,
        "read_back": {k: v for k, v in rows.items()
                      if k not in ("end_to_end_seconds", "queue_wait_seconds")},
        "rate_limited_retries": _RATE_LIMITED[0],
        "checks": {
            "every_submitted_task_read_back": rows["rows"] == submitted,
            "every_task_completed": rows["completed"] == submitted,
            "no_duplicate_task_rows": rows["distinct_task_ids"] == rows["rows"],
        },
    }


async def run_mixed(args) -> dict[str, Any]:
    """Several task types and durations in flight together.

    A uniform batch hides the thing worth knowing: whether short work
    queues behind long work. Each type is enqueued under its **own**
    correlation id so the per-type latencies can be separated, and the
    whole set is drained as one.
    """
    workers, runners, stop = await connect_fleet(args, args.workers)
    connected = sum(1 for w in workers if w.failed is None)

    per_type = max(1, args.tasks // 4)
    plan = [
        ("count_to_n", {"n": 1000}, per_type),
        ("hash_rounds", {"rounds": 20000, "algorithm": "sha256"}, per_type),
        ("sleep", {"seconds": 2}, per_type),
        ("opaque_payload", {"payload_b64": "QUJDRA=="}, per_type),
    ]

    sampler = Sampler(args)
    sampler.start()

    correlations: dict[str, str] = {}
    total = 0
    for task_type, parameters, count in plan:
        correlation_id = str(uuid.uuid4())
        correlations[task_type] = correlation_id
        _, accepted = await asyncio.to_thread(
            enqueue, args, task_type, parameters, count, correlation_id
        )
        total += accepted

    elapsed = await drain_wait(args, workers, total, args.timeout)
    await asyncio.sleep(2.0)
    await sampler.stop()

    delivered = [task_id for w in workers for task_id in w.received]
    duplicates = [t for t, n in Counter(delivered).items() if n > 1]
    await shutdown_fleet(runners, stop)

    by_type: dict[str, Any] = {}
    completed = 0
    read_rows = 0
    all_completions: list[float | None] = []
    for task_type, correlation_id in correlations.items():
        rows = await asyncio.to_thread(read_back, args, correlation_id)
        all_completions.extend(rows.pop("_completions"))
        completed += rows["completed"]
        read_rows += rows["rows"]
        by_type[task_type] = {
            "rows": rows["rows"],
            "completed": rows["completed"],
            "by_status": rows["by_status"],
            "end_to_end_seconds": rows["end_to_end_seconds"],
            "queue_wait_seconds": rows["queue_wait_seconds"],
        }

    return {
        "scenario": "mixed",
        "correlation_ids": correlations,
        "fleet": {"requested": args.workers, "connected": connected,
                  "credits_total": connected * args.max_concurrent},
        "submitted": total,
        "drain_seconds": round(elapsed, 3),
        "throughput": throughput(all_completions, elapsed, completed),
        "by_type": by_type,
        "delivery": {
            "delivered": len(delivered),
            "distinct": len(set(delivered)),
            "duplicate_assignments": len(duplicates),
        },
        "coordinator": sampler.report(),
        "rate_limited_retries": _RATE_LIMITED[0],
        "checks": {
            "every_task_read_back": read_rows == total,
            "every_task_completed": completed == total,
            "no_duplicate_assignments": not duplicates,
        },
    }


async def run_saturation(args) -> dict[str, Any]:
    """The same burst at increasing fleet sizes.

    Decision #135 measured that operator-read latency tracks **fleet
    size**, not task count, and named this step as the owner of the
    defensible number. So the variable that moves here is the fleet, and
    the batch is held constant: any difference between steps is then
    attributable to the one thing that changed.

    The saturation point is reported as the first step whose coordinator
    CPU crosses `--cpu-threshold` percent of one core, and it is reported
    as `null` when no step does — an unreached ceiling is a fact, not a
    number to interpolate.
    """
    steps: list[dict[str, Any]] = []
    for fleet_size in [int(s) for s in args.ramp.split(",")]:
        result = await run_burst(args, workers_count=fleet_size, tasks_count=args.tasks)
        steps.append({
            "workers": fleet_size,
            "connected": result["fleet"]["connected"],
            "tasks": result["enqueue"]["accepted"],
            "drain_seconds": result["drain_seconds"],
            "tasks_per_second": result["throughput"]["tasks_per_second"],
            "end_to_end_p50": result["latency"]["end_to_end_seconds"].get("p50"),
            "end_to_end_p95": result["latency"]["end_to_end_seconds"].get("p95"),
            "end_to_end_p99": result["latency"]["end_to_end_seconds"].get("p99"),
            "coordinator_cpu_percent_of_one_core":
                result["coordinator"]["coordinator_cpu_percent_of_one_core"],
            "completed": result["read_back"]["completed"],
            "checks": result["checks"],
        })
        print(json.dumps(steps[-1], indent=2), file=sys.stderr)
        await asyncio.sleep(args.step_pause)

    saturated = next(
        (s for s in steps
         if (s["coordinator_cpu_percent_of_one_core"] or 0) >= args.cpu_threshold),
        None,
    )
    return {
        "scenario": "saturation",
        "tasks_per_step": args.tasks,
        "ramp": args.ramp,
        "cpu_threshold_percent_of_one_core": args.cpu_threshold,
        "steps": steps,
        "saturation_point_workers": saturated["workers"] if saturated else None,
        "saturation_basis": (
            f"first step at or above {args.cpu_threshold}% of one core"
            if saturated else
            "no step reached the threshold — the ceiling was not found in this ramp"
        ),
        "rate_limited_retries": _RATE_LIMITED[0],
        "checks": {
            "every_step_lost_nothing": all(
                all(step["checks"].values()) for step in steps
            ),
        },
    }


async def run_restart(args) -> dict[str, Any]:
    """Step 3.5 — restart the coordinator mid-drain and measure the recovery.

    The one scenario that does not merely load the coordinator: it kills it
    while it is working and then answers, from the coordinator's own tables
    and from the fleet's own counters, whether anything was lost.

    **The restart itself is a command, not something this harness knows how
    to do.** `--restart-command` takes whatever restarts the coordinator in
    the environment under test — `docker compose -p X restart coordinator`
    locally, `kubectl -n staging rollout restart deploy/coordinator` in the
    cluster — because a harness that shelled out to Docker directly would
    only ever be able to test one of the two, and the criterion asks about
    both.

    What is measured, and why each one is here:

    * `read_back` — every task accounted for, and completed exactly once.
      This is the "loses no durable task record" criterion, taken from the
      database rather than from the fleet, because the fleet's own view
      cannot see a row it never received.
    * `registrations` vs `reconnects` — "reconnect without re-enrollment",
      which is only meaningful as both numbers together.
    * `reconnect_seconds` — how long the fleet took to come back, per
      worker, which is the reconnect-storm measurement. Reported as a
      distribution rather than a maximum: one straggler on a 30s backoff
      cap says something very different from a fleet that all took 30s.
    * `coordinator_cpu_percent_of_one_core` over the restart window — the
      other half of "does not overwhelm the coordinator". A herd that
      arrives politely still costs something to answer.
    """
    workers, runners, stop = await connect_fleet(args, args.workers)
    connected = sum(1 for w in workers if w.failed is None)
    sessions_before = sum(w.sessions for w in workers)

    sampler = Sampler(args)
    sampler.start()

    correlation_id = str(uuid.uuid4())
    enqueue_seconds, accepted = await asyncio.to_thread(
        enqueue, args, args.task_type, json.loads(args.parameters), args.tasks, correlation_id
    )

    # Let work reach the fleet before pulling the coordinator out from
    # under it. **`received - completed` and not `received`**: the
    # criterion is about tasks *in flight* at the restart, and with a
    # millisecond workload a large `received` can sit alongside nothing
    # actually running. Use a workload long enough to still be executing
    # here — the documented demo uses `sleep`.
    await asyncio.sleep(args.restart_after)
    in_flight_at_restart = sum(len(w.received) - len(w.completed) for w in workers)

    restart_started = time.monotonic()
    completed_at_restart = sum(len(w.completed) for w in workers)
    restart = await _run_restart_command(args.restart_command)
    restart["seconds"] = round(time.monotonic() - restart_started, 3)
    restart["tasks_received_at_restart"] = in_flight_at_restart
    restart["tasks_completed_at_restart"] = completed_at_restart

    # The fleet is now reconnecting. `drain_wait` tolerates the gap: it
    # watches acks, and falls back to the depth endpoint when nothing has
    # landed for a while — which is exactly the shape of a restart.
    elapsed = await drain_wait(args, workers, accepted, args.timeout)

    # **Measured separately from the drain, and after it, on purpose.**
    # The queue can drain before the last worker is back — a straggler on
    # a long backoff simply is not needed to finish the work — so folding
    # the two together would report a convergence time that is really a
    # drain time. This is the criterion's "converges cleanly, timed".
    converge_after_drain = await _await_fleet_reconnect(workers, args.reconnect_timeout)
    await asyncio.sleep(2.0)
    await sampler.stop()

    delivered = [task_id for w in workers for task_id in w.received]
    duplicates = [t for t, n in Counter(delivered).items() if n > 1]
    reconnect_times = [t for w in workers for t in w.reconnect_seconds]
    back = sum(1 for w in workers if w.sessions >= 2)
    registrations = sum(w.registrations for w in workers)
    reconnects = sum(w.reconnects for w in workers)
    late_failures = [w.last_error for w in workers if w.last_error]
    await shutdown_fleet(runners, stop)

    rows = await asyncio.to_thread(read_back, args, correlation_id)
    completions = rows.pop("_completions")

    return {
        "scenario": "restart",
        "correlation_id": correlation_id,
        "fleet": {
            "requested": args.workers,
            "connected": connected,
            "credits_each": args.max_concurrent,
            "sessions_before_restart": sessions_before,
            "sessions_total": sum(w.sessions for w in workers),
        },
        "enqueue": {
            "requested": args.tasks,
            "accepted": accepted,
            "seconds": round(enqueue_seconds, 3),
        },
        "restart": restart,
        "reconnect": {
            "registrations": registrations,
            "reconnect_attempts": reconnects,
            "workers_back": back,
            "seconds": summarise(reconnect_times),
            # Not a failed run on its own: a session that died while the
            # coordinator was down is the expected shape of a restart. It
            # is reported because a *persistent* error after the fleet came
            # back would hide behind a successful drain otherwise.
            "errors_after_first_connect": len(late_failures),
            "first_error": late_failures[0] if late_failures else None,
        },
        "drain_seconds_after_restart": round(elapsed, 3),
        # **0.0 means the fleet was already back when the queue drained**,
        # which is the good case and not a missing measurement. The
        # convergence time itself is `reconnect.seconds` — this is only the
        # tail the drain did not already cover.
        "converge_after_drain_seconds": converge_after_drain,
        "throughput": throughput(completions, elapsed, rows["completed"]),
        "latency": {
            "end_to_end_seconds": rows["end_to_end_seconds"],
            "queue_wait_seconds": rows["queue_wait_seconds"],
        },
        "delivery": {
            "delivered": len(delivered),
            "distinct": len(set(delivered)),
            # **Not a failure in this scenario, unlike `burst`.** A task
            # whose worker was mid-execution when the coordinator went away
            # may legitimately be reclaimed and delivered a second time —
            # that is at-least-once delivery working (§3.0.5), and Step 3.4
            # is what stops the loser's result from landing. Reported, and
            # checked against the *result rows* instead.
            "redeliveries": len(duplicates),
            "refusals": sum(len(w.refused) for w in workers),
        },
        "coordinator": sampler.report(),
        "read_back": {k: v for k, v in rows.items()
                      if k not in ("end_to_end_seconds", "queue_wait_seconds")},
        "rate_limited_retries": _RATE_LIMITED[0],
        "checks": {
            "restart_command_succeeded": restart["returncode"] == 0,
            "work_was_in_flight_at_restart": in_flight_at_restart > 0,
            "every_task_read_back": rows["rows"] == accepted,
            "no_task_lost": rows["completed"] == accepted,
            "no_duplicate_task_rows": rows["distinct_task_ids"] == rows["rows"],
            "every_completion_has_one_result": rows["with_result"] == rows["completed"],
            "every_worker_reconnected": back == connected,
            "no_re_enrollment": registrations == connected,
        },
    }


async def _await_fleet_reconnect(workers: list[SimWorker], timeout: float) -> float | None:
    """Seconds until every worker that was connected holds a session again.

    Returns `None` on timeout rather than the elapsed time, so a run that
    never converged cannot be read as one that converged slowly. The
    caller's `every_worker_reconnected` check is what turns that into a
    verdict — this only measures.

    "Holds a session again" is `sessions >= 2`: one before the restart and
    one after. Counting sessions rather than asking whether the socket is
    currently open is what makes it immune to a worker that reconnected and
    then briefly dropped again.
    """
    started = time.monotonic()
    expected = sum(1 for w in workers if w.failed is None)
    while time.monotonic() - started < timeout:
        if sum(1 for w in workers if w.sessions >= 2) >= expected:
            return round(time.monotonic() - started, 3)
        await asyncio.sleep(0.25)
    return None


async def _run_restart_command(command: str) -> dict[str, Any]:
    """Run the operator's restart command and report what it did.

    `shlex.split` rather than `shell=True`: the command comes from the
    operator's own argv, so this is not a trust boundary, but a shell would
    make the reported `returncode` that of the shell rather than of the
    thing that was supposed to restart the coordinator.
    """
    process = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return {
        "command": command,
        "returncode": process.returncode,
        "output": (output or b"").decode(errors="replace").strip()[-2000:],
    }


SCENARIOS = {
    "burst": run_burst,
    "sustained": run_sustained,
    "mixed": run_mixed,
    "saturation": run_saturation,
    "restart": run_restart,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--url", default=os.environ.get("COORDINATOR_URL", ""),
                        help="coordinator base URL (or COORDINATOR_URL)")
    parser.add_argument("--enrollment-secret", default=os.environ.get("ENROLLMENT_SECRET", ""))
    parser.add_argument("--admin-secret", default=os.environ.get("ADMIN_SECRET", ""))

    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--max-concurrent", type=int, default=4,
                        help="credits each simulated worker declares")
    parser.add_argument("--task-type", default=DEFAULT_WORKLOAD["task_type"])
    parser.add_argument("--parameters", default=json.dumps(DEFAULT_WORKLOAD["parameters"]),
                        help="JSON parameters for --task-type")

    parser.add_argument("--rate", type=float, default=50.0,
                        help="sustained: tasks per second offered")
    parser.add_argument("--seconds", type=int, default=60,
                        help="sustained: how long to hold the rate")
    parser.add_argument("--enqueue-interval", type=float, default=1.0,
                        help="sustained: seconds between enqueue batches")

    parser.add_argument("--ramp", default="10,25,50,100",
                        help="saturation: comma-separated fleet sizes")
    parser.add_argument("--step-pause", type=float, default=5.0)
    parser.add_argument("--cpu-threshold", type=float, default=75.0,
                        help="saturation: percent of one core that counts as saturated")

    parser.add_argument("--restart-command", default="",
                        help="restart: the command that restarts the coordinator, e.g. "
                             "'docker compose -p dcds35 restart coordinator' or "
                             "'kubectl -n staging rollout restart deploy/coordinator'")
    parser.add_argument("--restart-after", type=float, default=10.0,
                        help="restart: seconds to let work flow before restarting")
    parser.add_argument("--reconnect-timeout", type=float, default=120.0,
                        help="restart: seconds to wait for the whole fleet to come back")

    parser.add_argument("--connect-batch", type=int, default=10,
                        help="workers registered per batch")
    parser.add_argument("--connect-pause", type=float, default=0.5)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for a drain before giving up")
    parser.add_argument("--json-out", help="also write the report to this file")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (local dev CA only)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url or not args.enrollment_secret or not args.admin_secret:
        print("need --url, --enrollment-secret and --admin-secret "
              "(or COORDINATOR_URL / ENROLLMENT_SECRET / ADMIN_SECRET)", file=sys.stderr)
        return 2
    if args.scenario == "restart" and not args.restart_command:
        # Refused up front rather than run and reported as a pass: a
        # restart scenario that restarts nothing is a burst with extra
        # words, and it would produce a green report saying so.
        print("restart needs --restart-command", file=sys.stderr)
        return 2

    started = time.time()
    try:
        report = asyncio.run(SCENARIOS[args.scenario](args))
    except urllib.error.URLError as exc:
        # The coordinator became unreachable mid-run — the documented
        # failure demo (stop it mid-drain) walks straight into this. A
        # bare traceback would be the one outcome the harness must never
        # produce: no verdict at all. Report the failure and exit 1, so a
        # killed coordinator reads as FAIL and never as a green run.
        report = {
            "scenario": args.scenario,
            "aborted": "coordinator_unreachable",
            "detail": str(exc.reason),
            "checks": {"coordinator_reachable_throughout": False},
        }
    report["started_at"] = datetime.fromtimestamp(started).astimezone().isoformat()
    report["wall_seconds"] = round(time.time() - started, 2)
    report["target"] = args.url

    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text)

    checks = report.get("checks") or {}
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print("FAIL: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
