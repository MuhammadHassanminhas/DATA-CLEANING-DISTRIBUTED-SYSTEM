"""Worker agent.

Phase 1.3: registers with the coordinator using a bootstrap enrollment
credential, persists the coordinator-assigned identity to a local file
(on a volume, so it survives container recreation), and reuses it on
every later startup instead of re-enrolling. Heartbeat protocol proper
is Phase 1.6 — this still only touches the local heartbeat file so
Docker's HEALTHCHECK keeps working.

Duplicate-use handling:
- Same machine, launched twice: an exclusive OS file lock on a lock
  file next to the identity file makes the second process fail fast
  rather than silently share the identity.
- Identity copied to a second machine: every run registers a short-TTL
  "claim" for its worker ID on the coordinator (see
  `coordinator/app/main.py` and `config.worker_claim_ttl_seconds`'s
  docstring for exactly what this does and does not guarantee). A
  concurrent claim attempt while the previous one is still live is
  rejected (409) and logged as a duplicate. On graceful shutdown
  (SIGTERM) the claim is released so a normal restart never trips this.

Phase 1.4: after identity is established, exchanges the long-lived
`worker_credential` for a short-lived access token via
`/workers/token/refresh`.

Phase 1.5: the access token authenticates a persistent WebSocket
connection to `/ws/connect` (Decisions Log #1). The token is exchanged
once per connection attempt, at handshake time — the coordinator never
re-checks it for the life of the socket, so there is nothing to keep
"live" while connected. On disconnect (coordinator drain, network drop,
coordinator restart) the worker reconnects using the Phase 1.7 backoff
policy below.

Phase 1.6: alongside the transport-level ping/pong keepalive, the
worker now sends an application-level `heartbeat` envelope over the
same socket every `WORKER_HEARTBEAT_INTERVAL_SECONDS`, carrying a
sequence number, uptime, agent version, and CPU/memory read via
`psutil` (cross-platform — Linux/Windows/macOS, so native non-Docker
workers report real figures instead of blank; Step 1.5.8). These read
host-level figures, not a container cgroup limit, which is deliberate:
the Step 1.6 exit criterion asks for accuracy "against the host". The
coordinator times
SUSPECT/OFFLINE off its own receipt clock, not this envelope's
`timestamp` field, so a skewed worker clock cannot affect the state
machine (Decisions Log #6).

Phase 1.7: the fixed 3-second reconnect delay is replaced with full-
jitter exponential backoff (`_backoff_delay_seconds`) — also the
thundering-herd mitigation for a mass reconnect after a coordinator
restart, since the jitter alone spreads simultaneous workers across a
window instead of all retrying at the same instant. The backoff resets
to its fastest tier the moment a session is genuinely established
(`hello_ack` received), not merely attempted, so one clean disconnect
never leaves the next retry slower than it needs to be. The coordinator
can also unilaterally end a session via a `session_evicted` message
(Step 1.7's session-conflict resolution: if this worker ID opens a
second connection — e.g. a fast forced-restart racing the old
session's own cleanup — the coordinator always keeps exactly one
winner and terminates the other).

Phase 2.4: the worker executes. Tasks run in a `ThreadPoolExecutor` sized
to `WORKER_MAX_CONCURRENT` (Decision #96), never on the event loop —
measured at the design sub-gate, a CPU-bound task run inline makes the
heartbeat gap the *entire task duration*, so a 24.58s task breached the
coordinator's 12s SUSPECT threshold and a perfectly healthy worker would
have been declared dead. With threads the gap held at 5.0–5.7s from 1 to
8 concurrent tasks. Threads buy heartbeat survival, **not** throughput
(4 CPU tasks on one core measured 1.03x versus serial — nothing);
throughput in V1 comes from more workers, not more threads.

The invariant the whole design turns on: **the worker never holds a task
it is not executing.** There is no local queue. A worker with no free slot
*refuses* the assignment (Decision #101), because a local backlog would
be the worker making a scheduling decision, which §3.2/§3.3 reserve for
the coordinator.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil

try:  # POSIX (Docker/Linux/macOS worker)
    import fcntl

    msvcrt = None
except ImportError:  # native Windows worker (no Docker) — Step 1.5.8 installer
    fcntl = None
    import msvcrt

import websockets
from websockets.exceptions import ConnectionClosed

from protocol.envelope import Envelope
from worker import executors

HEARTBEAT_FILE = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat"))
IDENTITY_FILE = Path(
    os.environ.get("WORKER_IDENTITY_FILE", "/var/lib/worker-identity/identity.json")
)
LOCK_FILE = IDENTITY_FILE.with_suffix(".lock")
INTERVAL_SECONDS = int(os.environ.get("WORKER_LOOP_INTERVAL_SECONDS", "5"))
COORDINATOR_URL = os.environ["COORDINATOR_URL"]
# Private dev CA for local Docker (self-signed coordinator cert). Set
# WORKER_CA_FILE="" for a worker reaching the PUBLIC ingress (Step 1.5.5):
# the coordinator there presents a Let's Encrypt cert chaining to a public
# root, so the OS trust store validates it — no dev CA needed.
CA_FILE = os.environ.get("WORKER_CA_FILE", "/certs/dev-ca.crt")
AGENT_VERSION = os.environ.get("WORKER_AGENT_VERSION", "0.1.0")


def _ssl_context() -> ssl.SSLContext:
    # Trust the private dev CA only if the file is actually present (local
    # Docker mounts it at /certs). On every machine without it — non-Docker
    # workers (Step 1.5.8), and anything reaching the public Let's Encrypt
    # endpoint — fall back to the OS trust store. "Missing file" means "use
    # system roots", never "fail": a self-signed coordinator cert won't
    # validate against system roots, so this cannot silently downgrade trust.
    # (Also dodges Windows PowerShell not propagating an empty env var to the
    # child, which would otherwise leave CA_FILE at its dev-CA default.)
    if CA_FILE and os.path.exists(CA_FILE):
        return ssl.create_default_context(cafile=CA_FILE)
    return ssl.create_default_context()
WS_HELLO_ACK_TIMEOUT_SECONDS = 10
# Full-jitter exponential backoff. Recommendations, not measured values.
WS_BACKOFF_BASE_SECONDS = float(os.environ.get("WORKER_WS_BACKOFF_BASE_SECONDS", "1"))
WS_BACKOFF_FACTOR = float(os.environ.get("WORKER_WS_BACKOFF_FACTOR", "2"))
WS_BACKOFF_MAX_SECONDS = float(os.environ.get("WORKER_WS_BACKOFF_MAX_SECONDS", "30"))
# Recommendation, not a measured value. Must stay comfortably below the
# coordinator's HEARTBEAT_SUSPECT_THRESHOLD_SECONDS (default 12s).
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))

# Phase 2.3 capabilities, declared once per session in `hello`.
#
# MAX_CONCURRENT is how many tasks this worker will hold at a time. The
# coordinator clamps it to its own ceiling — this is a request, not an
# entitlement (see `coordinator/app/config.worker_max_concurrent_ceiling`).
MAX_CONCURRENT = int(os.environ.get("WORKER_MAX_CONCURRENT", "4"))
# Which dummy task types this worker will accept. Empty/unset means "all
# of them", which is what every worker built before Step 2.3 implicitly
# claimed. A comma-separated list narrows it — that is also how the
# "queued task with no eligible worker" case is produced deliberately.
SUPPORTED_TASK_TYPES = [
    t.strip()
    for t in os.environ.get(
        "WORKER_SUPPORTED_TASK_TYPES", "count_to_n,hash_rounds,sleep,opaque_payload"
    ).split(",")
    if t.strip()
]

# Phase 2.4 progress cadence. A **recommendation, not a measured value**
# (CLAUDE.md §10): 10s is frequent enough for a human watching the dashboard
# and slow enough that a fleet of 100 workers running 4 tasks each produces
# 40 messages a second, not 4,000. One reporter task per worker emits one
# `task_progress` per running task per interval, and suppresses a value that
# has not moved since the last one.
PROGRESS_INTERVAL_SECONDS = float(os.environ.get("WORKER_PROGRESS_INTERVAL_SECONDS", "10"))

# Refusal reason codes. The coordinator's credit accounting branches on
# these, so they are a wire contract, not log text (Decision #101):
# `at_capacity` saturates the worker's credit until it reports capacity
# again, `unsupported_task_type` frees a single credit.
REFUSE_AT_CAPACITY = "at_capacity"
REFUSE_UNSUPPORTED_TYPE = "unsupported_task_type"

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger("worker")

_lock_handle = None  # kept open for the process lifetime; see _acquire_single_instance_lock
_process_start = time.monotonic()
# Strong references to in-flight execution coroutines. See `_handle_assignment`.
_EXECUTIONS: set[asyncio.Task] = set()


def _read_cpu_percent() -> float | None:
    """Host CPU usage since the previous call, via `psutil` (Linux/
    Windows/macOS). Non-blocking (`interval=None`): the first call
    returns 0.0, later calls the delta. `None` only if psutil errors."""
    try:
        return round(psutil.cpu_percent(interval=None), 1)
    except Exception:
        return None


def _read_memory_percent() -> float | None:
    """Host memory usage via `psutil` — real host figures, not a
    container cgroup limit, which is what the Step 1.6 exit criterion
    ("accurate against the host") asks for."""
    try:
        return round(psutil.virtual_memory().percent, 1)
    except Exception:
        return None


def _log(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, "service": "worker", **fields}, default=str))


def _acquire_single_instance_lock() -> None:
    global _lock_handle
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # "a" not "w": never truncate a file a first instance may have locked.
    handle = open(LOCK_FILE, "a")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # Windows: same "second instance fails fast" guarantee via msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _log("duplicate_local_instance_detected", lock_file=str(LOCK_FILE))
        sys.exit(1)
    _lock_handle = handle  # holding the reference keeps the lock held


def _post(path: str, payload: dict, correlation_id: str | None = None) -> tuple[int, dict]:
    ctx = _ssl_context()
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    # Carries the session correlation id onto the HTTP leg (token refresh)
    # so it shares one id with the WebSocket leg — the coordinator's
    # CorrelationIDMiddleware reuses X-Correlation-ID when present (§11).
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    req = urllib.request.Request(
        f"{COORDINATOR_URL}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _load_identity() -> dict | None:
    if not IDENTITY_FILE.exists():
        return None
    return json.loads(IDENTITY_FILE.read_text())


def _save_identity(identity: dict) -> None:
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_FILE.write_text(json.dumps(identity))
    os.chmod(IDENTITY_FILE, 0o600)


def _register_or_reaffirm() -> dict:
    identity = _load_identity()

    if identity is None:
        status, body = _post(
            "/workers/register",
            {"enrollment_secret": os.environ["ENROLLMENT_SECRET"], "agent_version": AGENT_VERSION},
        )
        if status != 201:
            _log("registration_rejected", status=status, detail=body.get("detail"))
            sys.exit(1)
        identity = {"worker_id": body["worker_id"], "worker_credential": body["worker_credential"]}
        _save_identity(identity)
        _log("registered", worker_id=identity["worker_id"])
        return identity

    status, body = _post(
        "/workers/register",
        {
            "worker_id": identity["worker_id"],
            "worker_credential": identity["worker_credential"],
            "agent_version": AGENT_VERSION,
        },
    )
    if status == 409:
        _log("duplicate_identity_detected", worker_id=identity["worker_id"], detail=body.get("detail"))
        sys.exit(1)
    if status == 429:
        _log("registration_rate_limited", worker_id=identity.get("worker_id"))
        sys.exit(1)
    if status != 200:
        _log("reaffirm_rejected", status=status, detail=body.get("detail"))
        sys.exit(1)
    _log("reaffirmed", worker_id=identity["worker_id"])
    return identity


def _release_claim(worker_id: str, worker_credential: str) -> None:
    try:
        _post("/workers/release", {"worker_id": worker_id, "worker_credential": worker_credential})
    except Exception:  # noqa: BLE001 — best-effort on shutdown, never block exit
        pass


def _refresh_access_token(
    worker_id: str, worker_credential: str, correlation_id: str | None = None
) -> tuple[str, int] | None:
    status, body = _post(
        "/workers/token/refresh",
        {"worker_id": worker_id, "worker_credential": worker_credential},
        correlation_id=correlation_id,
    )
    if status != 200:
        _log("access_token_refresh_rejected", worker_id=worker_id, status=status, detail=body.get("detail"))
        return None
    _log(
        "access_token_refreshed",
        worker_id=worker_id,
        expires_in=body["expires_in"],
        correlation_id=correlation_id,
    )
    return body["access_token"], body["expires_in"]


def _ws_url() -> str:
    return COORDINATOR_URL.replace("https://", "wss://", 1) + "/ws/connect"


async def _heartbeat_ws_loop(
    ws, identity: dict, send_lock: asyncio.Lock, correlation_id: str
) -> None:
    """Sends an application-level `heartbeat` envelope every
    `HEARTBEAT_INTERVAL_SECONDS`, separate from the transport-level
    ping/pong keepalive already handled inline in `_hold_connection`.
    Cancelled from that function's `finally` block when the connection
    ends, so this never outlives the socket it's sending on."""
    sequence = 0
    while True:
        sequence += 1
        envelope = Envelope(
            message_type="heartbeat",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={
                "sequence": sequence,
                "uptime_seconds": round(time.monotonic() - _process_start, 1),
                "agent_version": AGENT_VERSION,
                "status": "ONLINE",
                "cpu_percent": _read_cpu_percent(),
                "memory_percent": _read_memory_percent(),
            },
        )
        async with send_lock:
            await ws.send(json.dumps(envelope.to_dict()))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@dataclass
class RunningTask:
    """One task this worker is executing right now.

    This *is* the worker's local task state, and its whole lifetime is the
    execution: created when a slot is taken, deleted in the `finally` of
    the coroutine that ran it (the "worker deletes temporary state after
    submission" exit criterion). Nothing is written to disk and nothing
    survives the process — §3.6, workers are stateless between tasks.

    `progress` is the one-slot list the executor thread writes and the
    event loop reads; `cancel` is the cooperative stop flag. See
    `executors.py` for why both are shaped this way.
    """

    task_id: str
    task_type: str
    correlation_id: str
    started_at: float
    progress: list[float] = field(default_factory=lambda: [0.0])
    cancel: threading.Event = field(default_factory=threading.Event)
    last_reported: float | None = None


class TaskRunner:
    """Owns the thread pool, the concurrency slots, and what is running.

    **Process-level, not session-level.** It is created once at startup and
    outlives every WebSocket session, which is what makes in-flight work
    survive a reconnect (Decision #97): the socket dies, the threads keep
    computing, and the new `hello` declares how many tasks are still
    running so the coordinator's credit accounting starts from the truth
    rather than from zero (Decision #101).

    The pool is sized explicitly to `MAX_CONCURRENT` rather than left to
    `asyncio.to_thread`'s default, and that is not a style preference. A
    measured trap: `os.cpu_count()` returns 4 inside a `--cpus=1`
    container, so the default pool would be `min(32, 4+4)` = 8 — smaller
    than the 64 credits `WORKER_MAX_CONCURRENT_CEILING` permits. A worker
    accepting 64 tasks would silently queue 56 *inside the executor*:
    acknowledged, apparently running, not started. Sizing it here makes
    the pool incapable of hiding a backlog.
    """

    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max_concurrent
        self.pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="task")
        self.running: dict[str, RunningTask] = {}
        # The enforcement, distinct from the admission check below. Even a
        # bug in the refusal path cannot start more than `max_concurrent`
        # executions, because it cannot acquire a slot that does not exist.
        self.slots = asyncio.Semaphore(max_concurrent)
        # The socket to report on *right now*, rebound on every reconnect.
        self._ws = None
        self._send_lock: asyncio.Lock | None = None

    @property
    def tasks_in_flight(self) -> int:
        return len(self.running)

    def attach(self, ws, send_lock: asyncio.Lock) -> None:
        """Point completion reports at the session that is live now.

        This exists because of a defect a live reconnect test found, and it
        is worth stating plainly since the shape of the bug is not obvious:
        an execution coroutine outlives the session that started it, so if it
        captured that session's socket it would report a finished task down a
        socket the coordinator has already thrown away. The send fails
        silently, the credit is never released, and the worker looks
        permanently full to the new session — a worker stuck at zero free
        credits with nothing actually running, which is the exact failure
        Decision #101 exists to prevent, arriving by a different route.
        """
        self._ws = ws
        self._send_lock = send_lock

    def detach(self) -> None:
        self._ws = None
        self._send_lock = None

    async def report(self, envelope: Envelope) -> bool:
        """Send a report on the current session, or drop it.

        Dropping is safe, and specifically it is safe for `capacity`: a task
        that finished while the worker was disconnected is already out of
        `running`, so the next `hello` declares a `tasks_in_flight` that
        excludes it, and the credit is released by that count rather than by
        this message. The reconnect handshake *is* the fallback path — there
        is no need for a report buffer, and a buffer would be worker-side
        state with nothing to release it if the process died.
        """
        if self._ws is None or self._send_lock is None:
            _log("task_report_dropped_no_session", message_type=envelope.message_type)
            return False
        return await _send(self._ws, self._send_lock, envelope)

    def cancel_all(self) -> str:
        """Ask every running task to stop, cooperatively (Decision #94).

        A Python thread cannot be killed, so this sets the flags and the
        executors notice between chunks — measured at 0.04–0.11s. Returns
        the ids it signalled, for the log line.
        """
        for task in self.running.values():
            task.cancel.set()
        return ",".join(self.running)

    def shutdown(self) -> None:
        self.cancel_all()
        self.pool.shutdown(wait=False, cancel_futures=True)


async def _send(ws, send_lock: asyncio.Lock, envelope: Envelope) -> bool:
    """Send one envelope, tolerating a socket that has already died.

    A completing task must never take the process down because its
    `capacity` message could not be delivered. If the socket is gone the
    coordinator has already released every credit this session held (they
    die with the session), so the lost message costs nothing.
    """
    try:
        async with send_lock:
            await ws.send(json.dumps(envelope.to_dict()))
        return True
    except Exception as exc:  # noqa: BLE001 — a dead socket is a normal outcome here
        _log("task_message_send_failed", message_type=envelope.message_type, detail=str(exc))
        return False


async def _refuse_task(
    ws,
    identity: dict,
    send_lock: asyncio.Lock,
    task_id: str,
    task_type: str,
    correlation_id: str,
    reason_code: str,
) -> None:
    """Refuse an assignment, with a machine-readable cause.

    The cause matters more than the refusal: the coordinator saturates this
    worker's credit for `at_capacity` and frees one credit for
    `unsupported_task_type` (Decision #101). Getting that backwards
    livelocks the queue — assign, refuse, credit freed, doorbell, assign —
    at loop speed, which is exactly the defect the design review caught.
    """
    await _send(
        ws,
        send_lock,
        Envelope(
            message_type="task_ack",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={
                "task_id": task_id,
                "accepted": False,
                "reason_code": reason_code,
                "reason": f"{reason_code}: {task_type}",
            },
        ),
    )
    _log(
        "task_refused",
        worker_id=identity["worker_id"],
        task_id=task_id,
        task_type=task_type,
        reason_code=reason_code,
        correlation_id=correlation_id,
    )


async def _execute_task(
    identity: dict,
    runner: TaskRunner,
    task: RunningTask,
    parameters: dict,
) -> None:
    """Run one task in the pool and report what happened.

    The result is computed and **discarded** (Decision #98) — the result
    envelope, its persistence and its retry are Step 2.5's, and a buffer
    built here would be half of 2.5 with none of its design, as well as the
    one thing that breaks the flat-memory criterion. What survives is a
    12-hex fingerprint in the log (Decision #106): deterministic, so a live
    run can be checked against a known-answer vector, and disclosing
    nothing about the bytes, because `opaque_payload` echoes caller-supplied
    data and payload data must never reach a log (§12).

    Three outcomes, three different reports:

    * success  → `capacity`, releasing the credit. The task row stays
      `RUNNING` until Step 2.5 writes a result and completes it.
    * raised   → `task_failed` carrying the **exception type only**, never
      a message and never a traceback (Decision #102, §12).
    * cancelled → nothing. The worker is shutting down and the socket is
      going with it; the row stays where Phase 3 can see it.
    """
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            runner.pool,
            functools.partial(
                executors.execute, task.task_type, parameters, task.progress, task.cancel
            ),
        )
    except executors.ExecutionCancelled:
        _log(
            "task_execution_cancelled",
            worker_id=identity["worker_id"],
            task_id=task.task_id,
            correlation_id=task.correlation_id,
            elapsed_seconds=round(time.monotonic() - task.started_at, 3),
        )
    except Exception as exc:  # noqa: BLE001 — every failure is reported, none crashes the loop
        error_type = type(exc).__name__
        _log(
            "task_execution_failed",
            worker_id=identity["worker_id"],
            task_id=task.task_id,
            task_type=task.task_type,
            error_type=error_type,
            correlation_id=task.correlation_id,
            elapsed_seconds=round(time.monotonic() - task.started_at, 3),
        )
        await runner.report(
            Envelope(
                message_type="task_failed",
                worker_id=identity["worker_id"],
                correlation_id=task.correlation_id,
                payload={"task_id": task.task_id, "error_type": error_type},
            )
        )
    else:
        _log(
            "task_execution_completed",
            worker_id=identity["worker_id"],
            task_id=task.task_id,
            task_type=task.task_type,
            elapsed_seconds=round(time.monotonic() - task.started_at, 3),
            result_fingerprint=executors.fingerprint(result),
            correlation_id=task.correlation_id,
        )
        await runner.report(
            Envelope(
                message_type="capacity",
                worker_id=identity["worker_id"],
                correlation_id=task.correlation_id,
                payload={"task_id": task.task_id, "freed": 1},
            )
        )
    finally:
        # Local task state is deleted here and nowhere else, on every path
        # including cancellation — the "temporary state deleted after
        # submission" exit criterion. The slot is released with it.
        runner.running.pop(task.task_id, None)
        runner.slots.release()


async def _handle_assignment(
    ws,
    identity: dict,
    send_lock: asyncio.Lock,
    runner: TaskRunner,
    message: dict,
    session_correlation_id: str,
) -> None:
    """Admission control for one `task_assign`, then start executing.

    Four outcomes, in this order:

    1. **A type this worker did not advertise** → refused as
       `unsupported_task_type`. The coordinator's eligibility filter makes
       this nearly unreachable, so a refusal here means the two sides
       disagree, which is worth surfacing rather than letting the task sit
       ASSIGNED and unanswered.
    2. **A task already running** → acknowledged, not started twice.
       Duplicate delivery is a no-op (§3.7).
    3. **No free slot** → refused as `at_capacity`. Never queued locally:
       the worker holding a task it is not executing would be the worker
       scheduling, which §3.2/§3.3 forbid.
    4. Otherwise the slot is taken, the ack goes out, `task_started` moves
       the task to `RUNNING`, and execution begins.

    Every message carries the **task's** correlation id, not the session's,
    so one id spans enqueue → assign → ack → started → progress →
    completion across both services (§11).
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")
    task_type = str(payload.get("task_type") or "")
    parameters = payload.get("parameters") or {}
    correlation_id = str(message.get("correlation_id") or session_correlation_id)

    if task_type not in SUPPORTED_TASK_TYPES or task_type not in executors.EXECUTORS:
        await _refuse_task(
            ws, identity, send_lock, task_id, task_type, correlation_id, REFUSE_UNSUPPORTED_TYPE
        )
        return

    if task_id in runner.running:
        _log(
            "task_assign_duplicate_ignored",
            worker_id=identity["worker_id"],
            task_id=task_id,
            correlation_id=correlation_id,
        )
        await _send(
            ws,
            send_lock,
            Envelope(
                message_type="task_ack",
                worker_id=identity["worker_id"],
                correlation_id=correlation_id,
                payload={"task_id": task_id, "accepted": True, "reason": None},
            ),
        )
        return

    # `locked()` is true exactly when the semaphore's value is 0. Nothing
    # can slip in between this check and the acquire below: there is no
    # await between them, and the event loop is single-threaded.
    if runner.slots.locked():
        await _refuse_task(
            ws, identity, send_lock, task_id, task_type, correlation_id, REFUSE_AT_CAPACITY
        )
        return
    await runner.slots.acquire()

    task = RunningTask(
        task_id=task_id,
        task_type=task_type,
        correlation_id=correlation_id,
        started_at=time.monotonic(),
    )
    runner.running[task_id] = task

    await _send(
        ws,
        send_lock,
        Envelope(
            message_type="task_ack",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={"task_id": task_id, "accepted": True, "reason": None},
        ),
    )
    # An explicit `task_started` drives ASSIGNED -> RUNNING (Decision #95)
    # rather than the coordinator inferring it from the first progress
    # report. A state transition and a telemetry sample have to stay
    # separable: Phase 3 will want to throttle or drop progress under load,
    # and must never drop a state transition.
    await _send(
        ws,
        send_lock,
        Envelope(
            message_type="task_started",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={"task_id": task_id, "task_type": task_type},
        ),
    )
    _log(
        "task_execution_started",
        worker_id=identity["worker_id"],
        task_id=task_id,
        task_type=task_type,
        tasks_in_flight=runner.tasks_in_flight,
        correlation_id=correlation_id,
    )

    execution = asyncio.create_task(_execute_task(identity, runner, task, parameters))
    # A reference has to be held or the loop may garbage-collect a running
    # task, and the callback is not decoration: asyncio *silently discards*
    # an unawaited task's exception, which is the exact failure mode that
    # cost real debugging time in Phase 1.7 (see `_run_ws_forever`). These
    # coroutines are deliberately not awaited — that is what lets execution
    # outlive the session — so this is the only place a bug in them could
    # ever surface.
    _EXECUTIONS.add(execution)
    execution.add_done_callback(_execution_finished)


def _execution_finished(execution: asyncio.Task) -> None:
    _EXECUTIONS.discard(execution)
    if execution.cancelled():
        return
    exc = execution.exception()
    if exc is not None:
        _log("task_execution_supervisor_error", error_type=type(exc).__name__)


async def _progress_reporter_loop(identity: dict, runner: TaskRunner) -> None:
    """One reporter for the whole worker, not one per task.

    Per-task timers would put the message rate under the control of how
    many tasks are running *and* how long each takes; one loop makes it
    exactly `tasks_in_flight / interval` messages a second, which is a
    number the coordinator side can be reasoned about at fleet scale.

    Unchanged fractions are suppressed, so a `sleep` task that has not
    advanced and a stuck task both go quiet rather than filling the log
    with the same value. The coordinator keeps only the latest sample per
    task, in Redis, and never writes one to Postgres.
    """
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
        for task in list(runner.running.values()):
            fraction = round(task.progress[0], 4)
            if task.last_reported is not None and fraction == task.last_reported:
                continue
            task.last_reported = fraction
            await runner.report(
                Envelope(
                    message_type="task_progress",
                    worker_id=identity["worker_id"],
                    correlation_id=task.correlation_id,
                    payload={
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "progress": fraction,
                        "elapsed_seconds": round(time.monotonic() - task.started_at, 1),
                    },
                )
            )


async def _hold_connection(
    identity: dict,
    access_token: str,
    established: dict,
    correlation_id: str,
    runner: TaskRunner,
) -> None:
    """Open one WebSocket session and block until it closes, handling
    the ping/pong keepalive, the Phase 1.6 heartbeat loop, and any
    coordinator-pushed messages inline. Returns normally on a clean
    coordinator-initiated `shutdown` or `session_evicted`; raises on an
    unexpected drop so the caller knows to reconnect. Sets
    `established["value"] = True` the moment a real session exists
    (`hello_ack`) — the caller uses this, not how the function exits, to
    decide whether the Phase 1.7 backoff should reset."""
    ctx = _ssl_context()
    headers = {"Authorization": f"Bearer {access_token}"}
    async with websockets.connect(_ws_url(), extra_headers=headers, ssl=ctx) as ws:
        send_lock = asyncio.Lock()
        hello = Envelope(
            message_type="hello",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={
                "agent_version": AGENT_VERSION,
                # Phase 2.3 (Decision #80): the credit count and the type
                # filter the coordinator's assignment engine schedules
                # against. Declared per session, so a restart with new
                # settings takes effect on reconnect with no coordinator
                # state to clean up (§3.6, workers are stateless between
                # tasks).
                "max_concurrent": MAX_CONCURRENT,
                "supported_task_types": SUPPORTED_TASK_TYPES,
                # Phase 2.4 (Decision #101): how many tasks are executing
                # *right now*. Normally 0; non-zero only when this is a
                # reconnect and the previous socket died under running work.
                # Without it the coordinator starts a reconnected worker at
                # zero credits used and immediately over-commits it, and
                # every over-commit is a refusal — which is how the original
                # #97 design livelocked the whole queue.
                "tasks_in_flight": runner.tasks_in_flight,
            },
        )
        async with send_lock:
            await ws.send(json.dumps(hello.to_dict()))

        ack_raw = await asyncio.wait_for(ws.recv(), timeout=WS_HELLO_ACK_TIMEOUT_SECONDS)
        ack = json.loads(ack_raw)
        if ack.get("message_type") == "error":
            _log("ws_connection_rejected", reason=ack.get("payload", {}).get("reason"))
            return
        if ack.get("message_type") != "hello_ack":
            _log("ws_handshake_unexpected", received=ack.get("message_type"))
            return
        established["value"] = True
        _log(
            "ws_connected",
            worker_id=identity["worker_id"],
            session_epoch=ack.get("session_epoch"),
            correlation_id=correlation_id,
        )

        # Redirect completion reports to this session. Anything a task
        # finished while there was no session is already excluded from the
        # `hello` above, so nothing is owed here — see `TaskRunner.report`.
        runner.attach(ws, send_lock)

        heartbeat_task = asyncio.create_task(
            _heartbeat_ws_loop(ws, identity, send_lock, correlation_id)
        )
        progress_task = asyncio.create_task(_progress_reporter_loop(identity, runner))
        try:
            async for raw in ws:
                message = json.loads(raw)
                message_type = message.get("message_type")
                if message_type == "ping":
                    pong = Envelope(
                        message_type="pong",
                        worker_id=identity["worker_id"],
                        correlation_id=correlation_id,
                    )
                    async with send_lock:
                        await ws.send(json.dumps(pong.to_dict()))
                    continue
                if message_type == "shutdown":
                    _log("ws_shutdown_received", worker_id=identity["worker_id"])
                    return
                if message_type == "task_assign":
                    await _handle_assignment(
                        ws, identity, send_lock, runner, message, correlation_id
                    )
                    continue
                if message_type == "session_evicted":
                    _log(
                        "ws_session_evicted",
                        worker_id=identity["worker_id"],
                        detail=message.get("payload", {}).get("reason"),
                    )
                    return
                _log("ws_message_received", worker_id=identity["worker_id"], message_type=message_type)
        finally:
            runner.detach()
            # Both loops send on this socket, so neither may outlive it.
            # Note what is *not* cancelled here: the execution coroutines.
            # They are process-level on purpose (Decision #97) — a task
            # keeps computing across a reconnect, and the new session's
            # `hello` declares it.
            for session_task in (heartbeat_task, progress_task):
                session_task.cancel()
                try:
                    await session_task
                except asyncio.CancelledError:
                    pass


def _backoff_delay_seconds(consecutive_failures: int) -> float:
    """Full-jitter exponential backoff (AWS's "full jitter" algorithm):
    the delay is a random draw from `[0, cap)`, not the cap itself — this
    is also the thundering-herd mitigation for a mass reconnect after a
    coordinator restart, since a fleet of workers all failing at once get
    spread across that window instead of all retrying in lockstep.
    `consecutive_failures=0` (the tier right after a successful session)
    still jitters within `[0, BASE)` rather than reconnecting instantly,
    so even the "just disconnected cleanly" case doesn't thunder."""
    cap = min(WS_BACKOFF_MAX_SECONDS, WS_BACKOFF_BASE_SECONDS * (WS_BACKOFF_FACTOR**consecutive_failures))
    return random.uniform(0, cap)


async def _run_ws_forever(identity: dict, stop_event: asyncio.Event, runner: TaskRunner) -> None:
    """Runs until `stop_event` is set. Everything inside one iteration —
    token refresh included — is inside the same try/except: a token
    refresh can fail exactly the way a live connection can (the
    coordinator is mid-restart, DNS hiccups, a network blip), and must
    retry the same way rather than killing this whole background task
    with an unhandled exception (asyncio silently drops an unawaited
    task's exception — that failure mode cost real debugging time here:
    the worker container stayed "healthy" via its unrelated heartbeat
    loop while the connection loop was silently dead).

    `consecutive_failures` drives the Phase 1.7 backoff and resets to 0
    the moment a session is genuinely established, regardless of *how*
    this iteration ends (return or raise) — a clean coordinator-
    initiated close after a real session is not a failure."""
    consecutive_failures = 0
    while not stop_event.is_set():
        established = {"value": False}
        # One correlation id per session (= one connection lifetime). Shared
        # by the token-refresh HTTP call and the WS hello/heartbeat/pong, so
        # a session is traceable end to end by a single id — and across
        # coordinator replicas, since the HTTP leg and the WS leg can land on
        # different replicas behind the Service (§11, Step 1.5.6 C2).
        session_correlation_id = str(uuid.uuid4())
        try:
            refreshed = await asyncio.to_thread(
                _refresh_access_token,
                identity["worker_id"],
                identity["worker_credential"],
                session_correlation_id,
            )
            if refreshed is None:
                raise RuntimeError("token refresh rejected")
            access_token, _ = refreshed
            await _hold_connection(
                identity, access_token, established, session_correlation_id, runner
            )
        except (ConnectionClosed, OSError, urllib.error.URLError) as exc:
            _log("ws_connection_lost", worker_id=identity["worker_id"], detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — keep the retry loop alive
            _log("ws_connection_error", worker_id=identity["worker_id"], detail=str(exc))

        consecutive_failures = 0 if established["value"] else consecutive_failures + 1

        if not stop_event.is_set():
            delay = _backoff_delay_seconds(consecutive_failures)
            _log(
                "ws_reconnect_backoff",
                worker_id=identity["worker_id"],
                consecutive_failures=consecutive_failures,
                delay_seconds=round(delay, 2),
            )
            await asyncio.sleep(delay)


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        HEARTBEAT_FILE.touch()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _async_main(identity: dict) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_sigterm() -> None:
        _log("shutting_down", worker_id=identity["worker_id"])
        _release_claim(identity["worker_id"], identity["worker_credential"])
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except NotImplementedError:
        # Windows' Proactor loop has no add_signal_handler. The worker still
        # runs; graceful claim-release on shutdown is skipped, and the
        # short-TTL claim (Decision #11) expires on its own instead.
        _log("signal_handler_unavailable", worker_id=identity["worker_id"])

    # Created here, once, so it outlives every session (Decision #97). The
    # semaphore inside it must be constructed on a running loop, which is
    # why this is not a module-level global.
    runner = TaskRunner(MAX_CONCURRENT)
    _log(
        "task_runner_started",
        worker_id=identity["worker_id"],
        max_concurrent=MAX_CONCURRENT,
        supported_task_types=SUPPORTED_TASK_TYPES,
        progress_interval_seconds=PROGRESS_INTERVAL_SECONDS,
    )

    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    ws_task = asyncio.create_task(_run_ws_forever(identity, stop_event, runner))

    await stop_event.wait()
    # Cooperative cancel first, then stop waiting for the pool: a thread
    # cannot be killed, so the flags are the only thing that actually stops
    # the work (measured 0.04–0.11s to notice).
    if runner.running:
        _log(
            "task_execution_cancelling_on_shutdown",
            worker_id=identity["worker_id"],
            tasks_in_flight=runner.tasks_in_flight,
        )
    runner.shutdown()
    heartbeat_task.cancel()
    ws_task.cancel()
    for task in (heartbeat_task, ws_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    _acquire_single_instance_lock()
    identity = _register_or_reaffirm()
    asyncio.run(_async_main(identity))


if __name__ == "__main__":
    main()
