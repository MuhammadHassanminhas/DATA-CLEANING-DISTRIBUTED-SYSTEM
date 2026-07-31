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

**What Step 2.4 added here.** The worker-reported half of the lifecycle:
`task_started` moves `ASSIGNED -> RUNNING`, `task_progress` records
telemetry (Redis only, never Postgres), `task_failed` moves a task whose
executor raised to `FAILED`, and `capacity` releases the credit when a task
finishes. An acknowledgement still means "received", never "started" — the
two are separate messages so Phase 3 can shed telemetry under load without
ever losing a state transition.

**What Step 2.5 added here.** `handle_task_result` — the message that
finally completes a task. A success now submits a result envelope instead
of a bare `capacity`; the coordinator validates it, persists the body, moves
the task to `COMPLETED`, releases the credit and acknowledges. `capacity` is
kept and still handled, because a pre-2.5 worker sends nothing else and the
coordinator must not be able to tell one worker generation from another
(§3.5).

**What still does not happen here.** Nothing writes `lease_expires_at` or
`attempt_count`; those stay untouched through all of M2 (a Phase 2.1 exit
criterion) — 2.5 *reads* `attempt_count` to put it on the wire, which is a
different thing. Nothing times a task out (Decision #103) — duration is
already bounded by Step 2.1's parameter validation. Nothing enforces the
`idempotency_token` or the `session_epoch` a result carries: they are
recorded from day one so Phase 3 needs no protocol change, and duplicate
suppression in M2 comes from the task's own terminal state instead.
Recovering a task whose worker vanished is Phase 3; this module only
*detects* and logs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from app.config import (
    assignment_poll_interval_seconds,
    heartbeat_offline_threshold_seconds,
    task_dequeue_max_batch,
    task_result_max_bytes,
    worker_default_max_concurrent,
    worker_max_concurrent_ceiling,
)
from app.db import get_session
from app.metrics import (
    ASSIGNMENT_PASSES,
    ASSIGNMENT_QUERIES,
    ASSIGNMENTS_IN_FLIGHT,
    RESULT_SIZE_BYTES,
    TASK_ACKS,
    TASK_PROGRESS_REPORTS,
    TASK_RESULTS,
    TASKS_ASSIGNED,
    TASKS_COMPLETED,
    TASKS_FAILED,
    TASKS_STARTED,
)
from app.redis_client import redis_client
from app.results import MalformedResult
from app.results import validate as validate_result
from app.task_queue import (
    DUPLICATE,
    NOT_FOUND,
    NOT_OWNER,
    TRANSITIONED,
    complete_task,
    dequeue,
    mark_status,
    queue_depth,
)
from app.task_states import FAILED, RUNNING
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

# Refusal reason codes (Decision #101). A **wire contract**, not log text:
# the credit accounting branches on them, and getting the branch wrong is
# what livelocks the queue. Kept in step with `worker/worker.py`'s
# constants of the same names — they are the two ends of one protocol, and
# the worker deliberately does not import from the coordinator package.
REFUSE_AT_CAPACITY = "at_capacity"
REFUSE_UNSUPPORTED_TYPE = "unsupported_task_type"

# Progress samples live as long as a worker's metrics snapshot does, for the
# same reason: it is the dashboard's read, and a stale entry must expire on
# its own if the replica holding the socket dies.
CURRENT_TASKS_TTL_MULTIPLIER = 3


@dataclass
class LocalSession:
    """One worker socket held by *this* coordinator process.

    `in_flight` is the credit accounting from Decision #80: how many tasks
    this worker has been handed that it has not finished with. Step 2.4
    added the other half — the worker executes, and releases the credit with
    a `capacity` message when it is done — so a worker now cycles rather
    than filling up once and stopping.

    `saturated` is Decision #101, and it is the fix for a livelock the
    design review caught before any of this was built. A worker that refuses
    an assignment **because it has no free slot** must not have its credit
    freed: freeing it rings the doorbell, the next pass picks the same
    session (any session with `free_credits > 0`), it refuses again, and
    every task in the queue is stranded in `ASSIGNED` at loop speed. So a
    capacity refusal closes the door entirely, and only the worker's own
    `capacity` message reopens it. An unsupported-type refusal is different
    — it is rare, and the type filter makes it nearly unreachable — so that
    one does free a single credit.
    """

    worker_id: str
    session_epoch: int
    websocket: WebSocket
    send_lock: asyncio.Lock
    max_concurrent: int
    supported_task_types: tuple[str, ...]
    # task_id -> that task's correlation id, so a disconnect can name every
    # task it stranded *with the id that traces it* (§11), not just a count.
    pending_acks: dict[str, str] = field(default_factory=dict)
    # task_id -> correlation id, held from delivery until the credit is
    # released. **Credits are keyed by task, not counted**, which is what
    # makes a double release structurally impossible rather than merely
    # clamped: a `capacity` naming a task this session is not holding cannot
    # release anything (§3.7, and §12 — the worker sending it is untrusted).
    credited: dict[str, str] = field(default_factory=dict)
    # Work that was already running when this session opened, declared in
    # `hello` as `tasks_in_flight` (Decision #101). It cannot be keyed —
    # those tasks were delivered to a socket that no longer exists — so it is
    # the one pool a `capacity` message may draw down by count. Bounding the
    # draw to what the worker declared is what stops a duplicate release
    # from over-crediting.
    residual_in_flight: int = 0
    # Latest progress sample per running task, mirrored to Redis for the
    # dashboard. Telemetry only — nothing schedules on it.
    current_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    saturated: bool = False

    @property
    def in_flight(self) -> int:
        """Credits consumed: work declared at reconnect, plus work delivered
        to this session and not yet released."""
        return self.residual_in_flight + len(self.credited)

    @property
    def free_credits(self) -> int:
        if self.saturated:
            return 0
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


def parse_tasks_in_flight(payload: dict[str, Any] | None, max_concurrent: int) -> int:
    """Read `tasks_in_flight` out of a `hello` (Decision #101).

    A reconnecting worker whose previous socket died under running work is
    still executing it — Step 2.4's runtime is process-level, so the threads
    survive the session. Starting its credit count at zero would immediately
    over-commit it, and every over-commit is a refusal; that is the loop the
    review found.

    Worker-reported, so clamped like everything else (§12) — to
    `[0, max_concurrent]`. A worker inflating it only starves itself, which
    is why clamping is sufficient here and no coordinator-side proof is
    needed: the lie is self-limiting. A worker *understating* it gets
    over-committed and refuses, which the saturation rule then absorbs
    without stranding the queue.
    """
    payload = payload or {}
    raw = payload.get("tasks_in_flight")
    if raw is None:
        return 0
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, max_concurrent))


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
            # Non-zero only on a reconnect under running work (Decision #101).
            "tasks_in_flight": session.in_flight,
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

    Does **not** change task state. `ASSIGNED -> RUNNING` is driven by the
    separate `task_started` message (Decision #95); an ack here means the
    task arrived and was accepted, nothing more. Keeping the two apart is
    what stops "delivered" from being silently reported as "started", and it
    is what lets Phase 3 throttle telemetry without ever dropping a state
    transition.

    **A refusal's cause decides what happens to the credit** (Decision
    #101). `at_capacity` saturates the session — the worker has no slot, and
    freeing the credit here is precisely what would livelock the queue.
    `unsupported_task_type` (or a refusal from a pre-2.4 worker, which could
    only ever mean that) frees one credit and rings the doorbell, because
    that cause is rare and the eligibility filter makes it nearly
    unreachable.

    Either way the task is left `ASSIGNED` for Phase 3, for the same reason
    `log_unacknowledged` leaves its own.
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
        _refresh_in_flight_gauge()
        return

    reason_code = str(payload.get("reason_code") or REFUSE_UNSUPPORTED_TYPE)
    if reason_code == REFUSE_AT_CAPACITY:
        # Saturate, do not free, and do not ring the doorbell. The credit
        # stays consumed and this session draws nothing until it reports
        # capacity — which is the whole of the anti-livelock rule.
        session.saturated = True
        TASK_ACKS.labels("refused_capacity").inc()
        logger.warning(
            "task_refused_at_capacity",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "in_flight": session.in_flight,
                "max_concurrent": session.max_concurrent,
            },
        )
    else:
        _release_credit(session, task_id)
        TASK_ACKS.labels("refused").inc()
        logger.warning(
            "task_refused",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "reason_code": reason_code,
                "detail": payload.get("reason"),
            },
        )
        notify_local()

    _refresh_in_flight_gauge()


def _release_credit(session: LocalSession, task_id: str, freed: int = 1) -> bool:
    """Give credit back. Returns whether anything was released.

    **A named task is exactly-once.** If the id is one this session
    delivered, the key is deleted and a second release for the same id does
    nothing at all — not clamped, structurally impossible. If the id is
    unknown but the session declared reconnect residue, one unit of residue
    is released: that is the same task, reported by the worker that has been
    running it since before this socket existed. If it is unknown and there
    is no residue, nothing happens — that is a duplicate, or a worker
    inventing ids (§12).

    **An unnamed release is bounded best-effort**, residue first and then
    arbitrary held credits. It cannot release more than the session holds,
    so the total can never go negative. It *can* free the slot of a task
    that is genuinely still running, in which case the worker gets
    over-committed and refuses `at_capacity`, which the saturation rule
    absorbs. That is the right way round: refusing to honour an unnamed
    release would strand the worker's capacity permanently, which is worse
    than one refusal.
    """
    if task_id:
        if task_id in session.credited:
            del session.credited[task_id]
            return True
        if session.residual_in_flight > 0:
            session.residual_in_flight -= 1
            return True
        return False

    released = min(max(1, freed), session.residual_in_flight)
    session.residual_in_flight -= released
    while released < freed and session.credited:
        session.credited.pop(next(iter(session.credited)))
        released += 1
    return released > 0


async def handle_capacity(session: LocalSession, message: dict[str, Any]) -> None:
    """Worker reports freed slots (Decision #80's `capacity` message).

    Step 2.4 is what finally emits it: a task finishes, the worker releases
    the slot, and the next pass hands it more work. It is also the **only**
    thing that clears saturation (Decision #101) — a session that refused
    for lack of capacity stays closed until the worker itself says otherwise.

    Two shapes are accepted, and both are needed:

    * with a `task_id` this session is holding — the normal case, released
      exactly once no matter how many times the message arrives;
    * without one, or naming a task this session never delivered — the
      reconnect case. A worker that reconnected mid-execution declared those
      tasks as `tasks_in_flight`, so they were counted without ever being
      keyed by id here; `freed` decrements that residue.
    """
    payload = message.get("payload") or {}
    try:
        freed = int(payload.get("freed", 1))
    except (TypeError, ValueError):
        freed = 1
    if freed < 1:
        return

    task_id = str(payload.get("task_id") or "")
    _release_credit(session, task_id, freed)
    session.current_tasks.pop(task_id, None)
    session.saturated = False

    await _publish_current_tasks(session)
    _refresh_in_flight_gauge()
    logger.info(
        "worker_capacity_released",
        extra={
            "worker_id": session.worker_id,
            "task_id": task_id or None,
            "freed": freed,
            "in_flight": session.in_flight,
            # The **task's** id, not the session's. Without this the line
            # inherits the session correlation id from the context var and the
            # task's trace stops one message short of its own completion
            # (§11) — which is exactly what the first live run showed.
            "correlation_id": message.get("correlation_id"),
        },
    )
    notify_local()


async def handle_task_started(session: LocalSession, message: dict[str, Any]) -> None:
    """`ASSIGNED -> RUNNING`, on the worker's explicit report (Decision #95).

    The transition is coordinator-authoritative: the worker reports that it
    began, and this decides what that means. `mark_status` enforces that the
    task is real, is this worker's, and that the move is legal; anything else
    is logged and ignored rather than closing the socket, because a worker is
    untrusted input, not a trusted peer (§12).
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")
    correlation_id = str(message.get("correlation_id") or "")

    async with get_session() as db:
        outcome = await mark_status(
            db, task_id=task_id, worker_id=session.worker_id, new_status=RUNNING
        )
        if outcome == TRANSITIONED:
            await db.commit()

    if outcome == TRANSITIONED:
        TASKS_STARTED.inc()
        session.current_tasks[task_id] = {
            "task_id": task_id,
            "task_type": payload.get("task_type"),
            "progress": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "correlation_id": correlation_id,
        }
        await _publish_current_tasks(session)
        logger.info(
            "task_started",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "task_type": payload.get("task_type"),
                "correlation_id": correlation_id,
            },
        )
        return

    logger.warning(
        "task_started_rejected",
        extra={
            "task_id": task_id,
            "worker_id": session.worker_id,
            "correlation_id": correlation_id,
            "outcome": outcome,
        },
    )


async def handle_task_progress(session: LocalSession, message: dict[str, Any]) -> None:
    """Record the latest progress sample for a running task.

    **Redis only, never Postgres.** A DB write per task per interval would
    put avoidable write load on the `tasks` table, and under Decision #79
    that table *is* the queue's hot path — the one place in the system where
    extra writes cost throughput. Progress is also the most droppable data
    in the protocol: losing a sample loses nothing, which is exactly why it
    must not share a path with state transitions.

    Only tasks this session actually started are recorded, so a worker
    cannot inject a progress line for someone else's task.
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")
    entry = session.current_tasks.get(task_id)
    if entry is None:
        TASK_PROGRESS_REPORTS.labels("unknown").inc()
        return

    try:
        progress = float(payload.get("progress", 0.0))
    except (TypeError, ValueError):
        progress = 0.0
    entry["progress"] = max(0.0, min(1.0, progress))
    entry["elapsed_seconds"] = payload.get("elapsed_seconds")
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    TASK_PROGRESS_REPORTS.labels("recorded").inc()
    await _publish_current_tasks(session)
    logger.info(
        "task_progress",
        extra={
            "task_id": task_id,
            "worker_id": session.worker_id,
            "progress": entry["progress"],
            "elapsed_seconds": entry["elapsed_seconds"],
            "correlation_id": entry.get("correlation_id"),
        },
    )


async def handle_task_failed(session: LocalSession, message: dict[str, Any]) -> None:
    """A task whose executor raised → `FAILED`, credit freed (Decision #102).

    This closes a gap the design gate never addressed. Without it a task
    whose executor blew up would sit `RUNNING` forever in M2, holding a
    worker credit that nothing ever releases, with no Phase 3 lease engine
    to reclaim either.

    `FAILED` is reachable from both `ASSIGNED` and `RUNNING`, so a task that
    failed before its `task_started` was processed still lands correctly.

    The payload carries the **exception type only** — never a message and
    never a traceback. A traceback can contain payload data, and payload
    data must never reach a log (§12). What to *do* about a failure —
    retry, reassign, give up — is Phase 3's, and is deliberately not decided
    here.
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")
    error_type = str(payload.get("error_type") or "unknown")[:100]
    correlation_id = str(message.get("correlation_id") or "")

    async with get_session() as db:
        outcome = await mark_status(
            db, task_id=task_id, worker_id=session.worker_id, new_status=FAILED
        )
        if outcome == TRANSITIONED:
            await db.commit()

    # Release the credit unless the database says this task was never this
    # worker's. `ILLEGAL` (already terminal) and `NOOP` (already FAILED) both
    # still release: the worker *was* running it, so the slot genuinely is
    # free, and holding the credit would cost capacity for the life of the
    # session. `NOT_OWNER` and `NOT_FOUND` release nothing — otherwise naming
    # another worker's task id would be a way to draw down your own credits
    # (§12).
    if outcome not in (NOT_OWNER, NOT_FOUND):
        _release_credit(session, task_id)
    session.current_tasks.pop(task_id, None)
    session.saturated = False
    await _publish_current_tasks(session)
    _refresh_in_flight_gauge()

    if outcome == TRANSITIONED:
        TASKS_FAILED.inc()
    logger.warning(
        "task_failed",
        extra={
            "task_id": task_id,
            "worker_id": session.worker_id,
            "error_type": error_type,
            "correlation_id": correlation_id,
            "outcome": outcome,
        },
    )
    notify_local()


async def _send_to_session(session: LocalSession, envelope: Envelope) -> bool:
    """Write one envelope to this session's socket, tolerating a dead one.

    A failed send is logged and swallowed for the same reason `_deliver`
    swallows its own: one worker whose socket died between its message and
    this reply must not raise out of the read loop and take the session
    down with it. For an ack specifically the loss is harmless — the worker
    retries and the second submission is a `DUPLICATE`.
    """
    try:
        async with session.send_lock:
            await session.websocket.send_text(json.dumps(envelope.to_dict()))
        return True
    except Exception as exc:  # noqa: BLE001 — a dead socket is a normal outcome here
        logger.warning(
            "task_result_ack_send_failed",
            extra={"worker_id": session.worker_id, "detail": str(exc)},
        )
        return False


async def handle_task_result(session: LocalSession, message: dict[str, Any]) -> None:
    """`RUNNING -> COMPLETED`, with the result persisted (Phase 2.5).

    This is what finally completes a task. Step 2.4 computed results and
    threw them away (Decision #98), so every successful task in M2 stopped
    at `RUNNING`; from here a success submits an envelope, the coordinator
    validates and stores it, and the task reaches its terminal state.

    **Three things happen here and their order is deliberate.**

    1. **Validate before touching the database.** `results.validate` is
       pure, so a malformed submission is refused having written nothing at
       all — which is the whole of the "malformed results are rejected
       without corrupting task state" criterion. Oversize is *not*
       malformed and is truncated rather than refused; see `app.results`.
    2. **Persist and complete atomically.** `task_queue.complete_task` does
       both inside one `FOR UPDATE` lock, so a completed task always has its
       result and a stored result is always pointed at.
    3. **Release the credit, then acknowledge.** The credit goes back on
       every outcome except `NOT_OWNER` and `NOT_FOUND` — the same rule
       `handle_task_failed` uses and for the same §12 reason: naming another
       worker's task must never be a way to draw down your own credits.
       Otherwise the slot genuinely is free, and holding the credit over a
       *rejected* result would cost the worker capacity for the life of the
       session.

    **The ack is always definitive**, whether or not the result landed. A
    worker retries a pending result until it is acknowledged (the "finishes
    during a brief coordinator outage" criterion), so an ack that only ever
    meant success would leave a malformed result retrying forever — the
    worker punishing itself for an answer that will never change. `accepted`
    tells it whether the result was stored; either way it stops.

    A task left `RUNNING` by a rejection stays visible for Phase 3, exactly
    like a task stranded `ASSIGNED` by a refusal.
    """
    payload = message.get("payload") or {}
    correlation_id = str(message.get("correlation_id") or "")
    raw_task_id = str(payload.get("task_id") or "")

    try:
        envelope = validate_result(payload, max_bytes=task_result_max_bytes())
    except MalformedResult as exc:
        TASK_RESULTS.labels("rejected").inc()
        # Rejected, but the slot is still free — see the docstring.
        _release_credit(session, raw_task_id)
        session.current_tasks.pop(raw_task_id, None)
        session.saturated = False
        await _publish_current_tasks(session)
        _refresh_in_flight_gauge()
        logger.warning(
            "task_result_rejected",
            extra={
                "task_id": raw_task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "reason_code": exc.reason_code,
                # The reason, never the body: a result payload is caller
                # data and must not reach a log (§12).
                "detail": exc.detail,
            },
        )
        await _send_to_session(
            session,
            Envelope(
                message_type="task_result_ack",
                worker_id=session.worker_id,
                session_epoch=session.session_epoch,
                correlation_id=correlation_id,
                payload={
                    "task_id": raw_task_id,
                    "accepted": False,
                    "outcome": "rejected",
                    "reason_code": exc.reason_code,
                },
            ),
        )
        notify_local()
        return

    task_id = envelope["task_id"]
    async with get_session() as db:
        outcome = await complete_task(db, envelope=envelope, worker_id=session.worker_id)
        if outcome == TRANSITIONED:
            await db.commit()

    if outcome not in (NOT_OWNER, NOT_FOUND):
        _release_credit(session, task_id)
        session.current_tasks.pop(task_id, None)
        session.saturated = False
        await _publish_current_tasks(session)
        _refresh_in_flight_gauge()

    TASK_RESULTS.labels(outcome).inc()
    if outcome == TRANSITIONED:
        TASKS_COMPLETED.inc()
        RESULT_SIZE_BYTES.observe(envelope["size_bytes"])
        logger.info(
            "task_completed",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "duration_seconds": envelope["duration_seconds"],
                "result_size_bytes": envelope["size_bytes"],
                "result_truncated": envelope["truncated"],
                # Recorded, enforced by nothing in M2 — Phase 3's handles.
                "attempt_number": envelope["attempt_number"],
                "session_epoch": envelope["session_epoch"],
                "idempotency_token": envelope["idempotency_token"],
            },
        )
    else:
        logger.warning(
            "task_result_not_applied",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "outcome": outcome,
            },
        )

    await _send_to_session(
        session,
        Envelope(
            message_type="task_result_ack",
            worker_id=session.worker_id,
            session_epoch=session.session_epoch,
            correlation_id=correlation_id,
            payload={
                "task_id": task_id,
                # A duplicate is a success from the worker's side: the task
                # is completed and there is nothing left to retry.
                "accepted": outcome in (TRANSITIONED, DUPLICATE),
                "outcome": outcome,
            },
        ),
    )
    notify_local()


def current_tasks_key(worker_id: str) -> str:
    return f"worker:{worker_id}:current_tasks"


async def _publish_current_tasks(session: LocalSession) -> None:
    """Mirror this session's running tasks to Redis for the dashboard.

    Written from the in-memory view in one `SET` rather than read-modify-
    written, so a sample can never be lost to an interleaving. It is
    ephemeral, coordinator-observed, high-frequency state — the same
    reasoning that put worker metrics in Redis in Step 1.6 — and it carries
    a TTL so a replica that dies does not leave a worker looking busy
    forever.

    A publish failure is swallowed. This is telemetry: it must never be able
    to fail a state transition or drop a socket.
    """
    try:
        if session.current_tasks:
            await redis_client.set(
                current_tasks_key(session.worker_id),
                json.dumps(list(session.current_tasks.values())),
                ex=heartbeat_offline_threshold_seconds() * CURRENT_TASKS_TTL_MULTIPLIER,
            )
        else:
            await redis_client.delete(current_tasks_key(session.worker_id))
    except Exception as exc:  # noqa: BLE001 — telemetry, never load-bearing
        logger.warning(
            "current_tasks_publish_failed",
            extra={"worker_id": session.worker_id, "detail": str(exc)},
        )


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
            # Phase 2.5: which attempt this is, so the worker can echo it
            # back in the result envelope. **Always 0 through all of M2** —
            # `attempt_count` is a Phase 3 column and nothing writes it — but
            # it is on the wire from day one, so Phase 3's retry engine
            # changes a number rather than a protocol.
            "attempt": task.get("attempt_count", 0),
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

    session.pending_acks[task_id] = correlation_id
    # The credit *is* this entry — one key, one credit, released exactly once
    # on a `capacity`, a `task_failed`, or an unsupported-type refusal.
    session.credited[task_id] = correlation_id
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
