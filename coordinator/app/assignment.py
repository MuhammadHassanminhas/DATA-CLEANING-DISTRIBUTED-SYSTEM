"""Assignment engine (Phase 2.3).

Decides *which* connected worker gets a queued task, delivers it over
that worker's live socket, and records the acknowledgement. The queue
primitive it stands on is Step 2.2's `task_queue.dequeue`; this module
adds eligibility, credit accounting, delivery and detection.

**Where the engine runs.** One loop per coordinator replica, and a
replica assigns **only to worker sockets it is holding itself**. There is
no leader election and no global scheduler, because neither is needed:
whichever replica holds a worker's connection is by construction the one
that can write to it. Two replicas cannot hand out the same task — not
because they coordinate, but because the claim is
`FOR UPDATE SKIP LOCKED` on one row in one transaction (Decision #79).
Atomicity is a property of the queue, not of the scheduler, which is why
the scheduler is allowed to be this simple.

That leaves §3.9 intact. The only in-memory state here is
`_local_sessions`: the sockets *this process* currently owns, plus how
many tasks each has outstanding. Kill the process and nothing
authoritative is lost — the sockets die with it, the workers reconnect
somewhere else, and every task's real state is the row in Postgres. It is
a routing table for live file descriptors, not a source of truth.

**Delivery is push, not pull** (Decision #80). Note the assignment path
deliberately does *not* go through the `worker:{id}:push` Redis channel
that `POST /workers/{id}/push` uses. That channel exists so a replica can
reach a socket it does *not* hold; here the assigning replica always
holds the socket, so publishing to Redis only to read it back on the same
process would add a round trip and a failure mode for nothing. Same
decision, shorter path — recorded because Decision #80's wording named
the pub/sub path.

**Commit before send, always.** A task is durably `ASSIGNED` in Postgres
before a single byte goes to the worker. The reverse order would let a
worker hold a task the database never recorded, which is the one
inconsistency Phase 3 could not clean up. The cost of this order is the
opposite and far milder failure: a task recorded as ASSIGNED that never
reached its worker. That is visible (`task_assign_delivery_failed`, and
an entry in the unacknowledged log on disconnect) and is exactly what
Phase 3's lease reclaim is for.

**Idle policy.** The loop is event-driven: `enqueue` publishes to the
`tasks:available` channel and every replica wakes at once. When nothing
is queued the loop is parked on an `asyncio.Event`, and a pass that finds
`queue_depth() == 0` costs **one** query no matter how many workers are
connected — the depth check happens before any per-worker work. That
single property is what the "100 idle workers produce negligible load"
exit criterion rests on.

**Waiting policy.** A task nobody is eligible for simply stays `QUEUED`.
There is no dead-letter path, no drop, and no error: the pass assigns
nothing, returns 0, and the loop parks again. The task remains visible in
`GET /tasks/depth` and in `coordinator_tasks_queued`.

**What this step does not do.** It does not move a task to `RUNNING` —
that is Step 2.4, when a worker actually begins executing. An
acknowledgement here means "received", not "started". Nothing writes
`lease_expires_at` or `attempt_count`; those stay untouched through all
of M2 (a Phase 2.1 exit criterion). Recovering a task whose worker
vanished is Phase 3 — this module only *detects* and logs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from app.config import (
    assignment_poll_interval_seconds,
    task_dequeue_max_batch,
    worker_default_max_concurrent,
    worker_max_concurrent_ceiling,
)
from app.db import get_session
from app.metrics import (
    ASSIGNMENT_PASSES,
    ASSIGNMENT_QUERIES,
    ASSIGNMENTS_IN_FLIGHT,
    TASK_ACKS,
    TASKS_ASSIGNED,
)
from app.redis_client import redis_client
from app.task_queue import dequeue, queue_depth
from app.task_types import TASK_TYPES
from protocol import Envelope

logger = logging.getLogger("coordinator")

# Fan-out channel for "there is work". Carries no task data on purpose —
# it is a doorbell, not a delivery mechanism. Every replica reacts by
# asking Postgres, which is the only thing that can answer authoritatively
# and the only thing that can hand out a claim.
WORK_AVAILABLE_CHANNEL = "tasks:available"

# Backoff for the notification subscriber if Redis drops. Short, because
# the safety-net poll already bounds the damage of being unsubscribed.
_LISTENER_RETRY_SECONDS = 2.0


@dataclass
class LocalSession:
    """One worker socket held by *this* coordinator process.

    `in_flight` is the credit accounting from Decision #80: how many tasks
    this worker has been handed that it has not finished with. In Step 2.3
    nothing finishes a task — the worker acknowledges receipt and holds
    the slot — so a worker fills to `max_concurrent` and then stops
    drawing work. That is the correct behaviour for this step, not a
    limitation of it: execution and slot release arrive in Step 2.4 via
    the `capacity` message this module already handles.
    """

    worker_id: str
    session_epoch: int
    websocket: WebSocket
    send_lock: asyncio.Lock
    max_concurrent: int
    supported_task_types: tuple[str, ...]
    in_flight: int = 0
    # task_id -> that task's correlation id, so a disconnect can name every
    # task it stranded *with the id that traces it* (§11), not just a count.
    pending_acks: dict[str, str] = field(default_factory=dict)

    @property
    def free_credits(self) -> int:
        return max(0, self.max_concurrent - self.in_flight)


# Sockets this process owns. Not authoritative state — see module docstring.
_local_sessions: dict[str, LocalSession] = {}

# Set when there may be work. Cleared at the top of each pass, so a
# notification arriving *during* a pass re-arms it rather than being lost.
_work_available = asyncio.Event()


def _refresh_in_flight_gauge() -> None:
    ASSIGNMENTS_IN_FLIGHT.set(sum(s.in_flight for s in _local_sessions.values()))


def parse_capabilities(payload: dict[str, Any] | None) -> tuple[int, tuple[str, ...]]:
    """Read `max_concurrent` and `supported_task_types` out of a `hello`.

    Both are **worker-reported** and therefore untrusted (§12). They are
    sanitised here rather than believed:

    * `max_concurrent` is clamped to `[1, WORKER_MAX_CONCURRENT_CEILING]`.
      A worker claiming a million credits gets the ceiling, not the queue.
    * Unrecognised task types are dropped. A worker that names *only*
      unrecognised types ends up eligible for nothing, which is the
      honest answer — it is not quietly widened to "everything".
    * A worker that declares neither field is treated as supporting every
      registered type at the default credit count. That is a deliberate
      compatibility choice for workers built before this step existed:
      such a worker is a dumb executor by design (§3.2), so "all types" is
      what it always implicitly claimed.

    A worker lying about which types it can run is not prevented here, and
    cannot be: the claim is only falsified by trying. In Step 2.4 that
    surfaces as a refusal or a failure, and Phase 3 owns the recovery.
    """
    payload = payload or {}

    raw_max = payload.get("max_concurrent")
    try:
        max_concurrent = int(raw_max)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        max_concurrent = worker_default_max_concurrent()
    max_concurrent = max(1, min(max_concurrent, worker_max_concurrent_ceiling()))

    raw_types = payload.get("supported_task_types")
    if raw_types is None:
        supported = tuple(TASK_TYPES)
    elif isinstance(raw_types, list):
        supported = tuple(t for t in raw_types if isinstance(t, str) and t in TASK_TYPES)
    else:
        supported = ()

    return max_concurrent, supported


def register_session(session: LocalSession) -> None:
    """Take ownership of a worker socket and immediately look for work.

    An unconditional overwrite is correct. If an older session for this
    worker id is still in the map, Step 1.7 has already published a newer
    epoch and that session is being torn down; the newest handshake is
    always the winner ("one winner, always").
    """
    _local_sessions[session.worker_id] = session
    _refresh_in_flight_gauge()
    logger.info(
        "worker_capabilities_registered",
        extra={
            "worker_id": session.worker_id,
            "session_epoch": session.session_epoch,
            "max_concurrent": session.max_concurrent,
            "supported_task_types": list(session.supported_task_types),
        },
    )
    # A worker that connects to a non-empty queue should not wait for the
    # next notification or poll to be given work.
    notify_local()


def unregister_session(worker_id: str, session_epoch: int) -> None:
    """Release a socket, but only if the map still holds *this* session.

    The epoch guard matters: a superseded session's cleanup runs after the
    winner has already registered, and without this it would evict the
    live one. Same reasoning as the Redis registry-key check in `main.py`.
    """
    current = _local_sessions.get(worker_id)
    if current is not None and current.session_epoch == session_epoch:
        del _local_sessions[worker_id]
    _refresh_in_flight_gauge()


def log_unacknowledged(session: LocalSession, reason: str) -> None:
    """Exit criterion: a worker that disconnects between assignment and
    acknowledgement is **logged**.

    Detection only. Recovery — putting the task back in play — is Phase 3,
    and deliberately not attempted here: `ASSIGNED -> QUEUED` is not a
    legal move in `task_states`, and inventing it would pre-empt the
    `REASSIGNED` transition Phase 3 owns. The tasks stay `ASSIGNED` and
    are named here so nothing is lost silently.
    """
    for task_id, correlation_id in session.pending_acks.items():
        logger.warning(
            "task_unacknowledged_at_disconnect",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "session_epoch": session.session_epoch,
                "correlation_id": correlation_id,
                "reason": reason,
            },
        )


async def handle_task_ack(session: LocalSession, message: dict[str, Any]) -> None:
    """Record a worker's acknowledgement of an assignment.

    Does **not** change task state. `ASSIGNED -> RUNNING` belongs to Step
    2.4, when the worker actually starts executing; an ack here means the
    task arrived and was accepted, nothing more. Keeping the two apart is
    what stops "delivered" from being silently reported as "started".

    A refusal frees the credit — the worker is not going to run it, so
    holding the slot would strand capacity — but leaves the task
    `ASSIGNED` for Phase 3, for the same reason `log_unacknowledged` does.
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")
    accepted = bool(payload.get("accepted", True))

    correlation_id = session.pending_acks.pop(task_id, None)
    if correlation_id is None:
        # A duplicate ack, or an ack for a task this session never held.
        # Harmless and idempotent (§3.7) — recorded, not acted on.
        TASK_ACKS.labels("unknown").inc()
        logger.warning(
            "task_ack_unknown",
            extra={"task_id": task_id, "worker_id": session.worker_id},
        )
        return

    if accepted:
        TASK_ACKS.labels("accepted").inc()
        logger.info(
            "task_acknowledged",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
            },
        )
    else:
        session.in_flight = max(0, session.in_flight - 1)
        TASK_ACKS.labels("refused").inc()
        logger.warning(
            "task_refused",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "detail": payload.get("reason"),
            },
        )
        notify_local()

    _refresh_in_flight_gauge()


async def handle_capacity(session: LocalSession, message: dict[str, Any]) -> None:
    """Worker reports freed slots (Decision #80's `capacity` message).

    Handled now so Step 2.4 has nothing to add on the coordinator side —
    it only has to start sending this once a task finishes. Nothing emits
    it in Step 2.3, because nothing executes a task yet; the path is
    covered by unit tests rather than by the live demo, and that
    distinction is recorded rather than blurred (§10).
    """
    payload = message.get("payload") or {}
    try:
        freed = int(payload.get("freed", 1))
    except (TypeError, ValueError):
        freed = 1
    if freed < 1:
        return
    session.in_flight = max(0, session.in_flight - freed)
    _refresh_in_flight_gauge()
    logger.info(
        "worker_capacity_released",
        extra={
            "worker_id": session.worker_id,
            "freed": freed,
            "in_flight": session.in_flight,
        },
    )
    notify_local()


async def _deliver(session: LocalSession, task: dict[str, Any]) -> bool:
    """Send one already-claimed task to its worker. Returns delivery success.

    The envelope carries the **task's** correlation id, not a fresh one, so
    `tasks_enqueued`, `task_assigned`, the worker's own log line and the
    ack all share a single id end to end (§11).
    """
    task_id = str(task["id"])
    correlation_id = task["correlation_id"]
    envelope = Envelope(
        message_type="task_assign",
        worker_id=session.worker_id,
        session_epoch=session.session_epoch,
        correlation_id=correlation_id,
        payload={
            "task_id": task_id,
            "task_type": task["task_type"],
            "parameters": task["parameters"],
            "payload": task["payload"],
            "priority": task["priority"],
            "assigned_at": task["assigned_at"].isoformat(),
        },
    )

    try:
        async with session.send_lock:
            await session.websocket.send_text(json.dumps(envelope.to_dict()))
    except Exception as exc:  # noqa: BLE001 — one dead socket must not stop the pass
        logger.warning(
            "task_assign_delivery_failed",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "detail": str(exc),
            },
        )
        return False

    session.in_flight += 1
    session.pending_acks[task_id] = correlation_id
    TASKS_ASSIGNED.inc()
    logger.info(
        "task_assigned",
        extra={
            "task_id": task_id,
            "worker_id": session.worker_id,
            "task_type": task["task_type"],
            "priority": task["priority"],
            "correlation_id": correlation_id,
            "session_epoch": session.session_epoch,
        },
    )
    return True


async def assign_once() -> int:
    """Run one assignment pass over this replica's sockets. Returns the
    number of tasks delivered.

    The single depth check before the per-worker loop is the whole idle
    story: with an empty queue this costs one query regardless of whether
    the replica holds one socket or a hundred.
    """
    ASSIGNMENT_PASSES.inc()

    candidates = [
        s for s in list(_local_sessions.values()) if s.free_credits > 0 and s.supported_task_types
    ]
    if not candidates:
        return 0

    delivered = 0
    async with get_session() as db:
        if await queue_depth(db) == 0:
            return 0

        for session in candidates:
            limit = min(session.free_credits, task_dequeue_max_batch())
            ASSIGNMENT_QUERIES.inc()
            claimed = await dequeue(
                db,
                worker_id=session.worker_id,
                limit=limit,
                max_batch=task_dequeue_max_batch(),
                task_types=list(session.supported_task_types),
            )
            if not claimed:
                continue
            # Commit per worker, not once at the end of the pass: it keeps
            # the row locks short, and it is what makes "durably ASSIGNED
            # before delivery" true for these rows specifically.
            await db.commit()
            for task in claimed:
                if await _deliver(session, task):
                    delivered += 1

    if delivered:
        _refresh_in_flight_gauge()
    return delivered


def notify_local() -> None:
    """Wake this replica's loop. In-process only, no Redis hop."""
    _work_available.set()


async def notify_work_available() -> None:
    """Ring the doorbell on every replica (including this one).

    Fire-and-forget by nature — Redis pub/sub has no delivery guarantee,
    which is precisely why `assignment_poll_interval_seconds` exists. A
    failure to publish is logged and swallowed: it must never turn a
    successful enqueue into a failed request, since the tasks are already
    committed and the safety-net poll will find them.
    """
    try:
        await redis_client.publish(WORK_AVAILABLE_CHANNEL, "1")
    except Exception as exc:  # noqa: BLE001
        logger.warning("work_notification_publish_failed", extra={"detail": str(exc)})


async def notification_listener() -> None:
    """Subscribe to the doorbell for the life of the process."""
    while True:
        pubsub = redis_client.pubsub()
        try:
            await pubsub.subscribe(WORK_AVAILABLE_CHANNEL)
            async for message in pubsub.listen():
                if message["type"] == "message":
                    notify_local()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — resubscribe; the poll covers the gap
            logger.warning("work_notification_listener_error", extra={"detail": str(exc)})
            await asyncio.sleep(_LISTENER_RETRY_SECONDS)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


async def assignment_loop() -> None:
    """The engine. One per replica, started from the FastAPI lifespan."""
    interval = assignment_poll_interval_seconds()
    logger.info("assignment_engine_started", extra={"poll_interval_seconds": interval})
    while True:
        try:
            await asyncio.wait_for(_work_available.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass  # safety-net tick
        _work_available.clear()

        try:
            # Drain: a bulk enqueue should not need one wakeup per task.
            while await assign_once() > 0:
                await asyncio.sleep(0)  # yield, so delivery never starves the loop
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad pass must not kill the engine
            logger.warning("assignment_pass_error", extra={"detail": str(exc)})
            await asyncio.sleep(1)
