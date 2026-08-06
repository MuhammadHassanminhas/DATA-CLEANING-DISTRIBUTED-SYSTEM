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

**What Step 3.1 added here.** Lease renewal and the reclaimer loop.
Every worker message this module already handled is now also a renewal
opportunity: an ack, a start, a progress sample and a result attempt each
push the task's lease out, because the *arrival* of a message about a
task is the coordinator's only objective evidence that the worker is
still working on it. What the message *says* remains an input to nothing
— a worker reporting 99% progress forever renews no more than one
reporting 1%, and neither can extend past the coordinator-set
`deadline_at` (gate §3.0.6).

**Renewal is lazy, and the laziness lives in this process.** Each session
tracks when each of its tasks is next due for a renewal, on this
replica's own monotonic clock. A message that arrives before its task is
due costs **no database round trip at all** — not a no-op UPDATE, no
statement. At the shipped defaults that turns a 10s progress cadence into
roughly one write per running task per 30s, which is what keeps the lease
engine off the throughput ceiling measured in #141.

**What Step 3.2 added here.** The retry policy the reclaimer had none of.
An expired task is now either requeued with a backoff and an exclusion, or
— once its type's `max_attempts` is used up — moved to terminal `FAILED`,
and the two branches are counted and logged as the different events they
are. The worker that lost the task is also sent a best-effort
`task_cancel` over the shipped push channel, because the replica that
reclaimed it usually does not hold that worker's socket.

**What still does not happen here.** Nothing enforces the
`idempotency_token` or the `session_epoch` a result carries — the gate
settled that `session_epoch` will never be a fencing input (#169), because
Step 2.5 *measured* a legitimate result executed under epoch 4 and
submitted on 5. Fencing itself is Step 3.4, so a late result from a
superseded attempt is still accepted if the row happens to name that
worker again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from app.config import (
    assignment_poll_interval_seconds,
    heartbeat_offline_threshold_seconds,
    lease_disconnect_grace_seconds,
    lease_reclaim_batch,
    lease_reclaim_interval_seconds,
    task_dequeue_max_batch,
    task_lease_renew_fraction,
    task_lease_ttl_seconds,
    task_result_max_bytes,
    worker_default_max_concurrent,
    worker_max_concurrent_ceiling,
)
from app.db import get_session
from app.metrics import (
    ASSIGNMENT_PASSES,
    ASSIGNMENT_QUERIES,
    ASSIGNMENTS_IN_FLIGHT,
    DRAINING,
    LEASE_RECLAIM_SECONDS,
    LEASE_RENEWALS,
    LEASES_EXPIRED,
    RESULT_SIZE_BYTES,
    TASK_ACKS,
    TASK_PROGRESS_REPORTS,
    TASK_RESULTS,
    TASKS_ASSIGNED,
    TASKS_COMPLETED,
    TASKS_EXHAUSTED,
    TASKS_FAILED,
    TASKS_REASSIGNED,
    TASKS_STARTED,
)
from app.redis_client import redis_client
from app.results import MalformedResult
from app.results import validate as validate_result
from app.task_queue import (
    DUPLICATE,
    FENCED,
    NOT_FOUND,
    NOT_OWNER,
    TRANSITIONED,
    complete_task,
    dequeue,
    mark_status,
    queue_depth,
    reclaim_expired_leases,
    record_execution_failure,
    renew_lease,
    shorten_worker_leases,
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

# Phase 3.5. Set once, never cleared: a replica that has begun draining is
# on its way out of the process table and has no route back to serving.
# Making it one-way is what lets every reader treat it as a fact rather
# than as a value that might change under them.
_draining = False

# How often `drain_local_sessions` re-checks. Short relative to any drain
# window worth configuring, so the wait ends on the work finishing rather
# than on the poll's own granularity.
_DRAIN_POLL_SECONDS = 0.1


def is_draining() -> bool:
    """True once this replica has begun a graceful shutdown.

    Read by `/ready` (which must start failing before the sockets close, so
    Kubernetes takes this pod out of the Service while it can still finish
    what it holds) and by `assign_once` (which must stop handing out work
    it cannot see through).
    """
    return _draining


def begin_drain() -> None:
    """Stop taking new work. Idempotent, and deliberately one-way.

    **This does not close anything and does not cancel anything.** Tasks
    already running on this replica's workers keep running, results already
    in flight are still accepted, and the reclaimer keeps reclaiming — a
    draining replica is still a correct replica, it has simply stopped
    adding to what it will have to abandon.
    """
    global _draining
    _draining = True
    DRAINING.set(1)


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
    # Phase 3.1. task_id -> the monotonic instant at which this task's lease
    # is next worth writing. **This is what makes lazy renewal free rather
    # than merely cheap**: a message about a task that is not yet due is
    # dropped here, before any session is opened, so it costs no round trip
    # at all. Losing this map on a reconnect is harmless — the first
    # message on the new session finds no entry and renews immediately.
    lease_renew_due: dict[str, float] = field(default_factory=dict)
    # Phase 3.1. Tasks this worker was already holding when the session
    # opened, **read from the database on `hello`, never from the worker**.
    #
    # It exists because `credited` cannot cover them: those tasks were
    # delivered to a socket that no longer exists, so the reconnecting
    # session has no key for them (Decision #101 — the worker declares a
    # *count*, and a count cannot be keyed). Without this set, a worker
    # that reconnected mid-execution would keep sending perfectly good
    # progress for its running task, every one of those messages would be
    # skipped as "not this session's", and the task would be reclaimed one
    # lease later from a worker that was doing exactly what it should.
    #
    # Coordinator-observed, so it satisfies §12 on its own terms: the ids
    # come from `assigned_worker_id`, not from anything the worker said.
    recovered_tasks: set[str] = field(default_factory=set)
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


def _lease_due(session: LocalSession, task_id: str) -> bool:
    """Whether this task's lease is worth a write yet.

    The renewal cadence is `TASK_LEASE_TTL_SECONDS * TASK_LEASE_RENEW_FRACTION`
    — at the defaults, 30s of a 60s lease — and the clock is
    `time.monotonic()` rather than the wall clock deliberately: a replica
    whose system time is adjusted by NTP mid-session must not suddenly
    believe every lease is due, or stop renewing for an hour.

    An unknown task is always due. That covers the first message of a
    task's life and the first message after a reconnect, both of which
    should renew immediately rather than wait out a window measured from a
    session that no longer exists.
    """
    due = session.lease_renew_due.get(task_id)
    return due is None or time.monotonic() >= due


def _mark_renewed(session: LocalSession, task_id: str) -> None:
    session.lease_renew_due[task_id] = time.monotonic() + (
        task_lease_ttl_seconds() * task_lease_renew_fraction()
    )


async def _renew_if_due(session: LocalSession, task_id: str) -> None:
    """Renew one task's lease, if it is due and the task is this session's.

    **Every renewal path funnels through here**, so the laziness rule and
    the "only tasks this session is entitled to renew" rule are each
    written once. A task id this session neither holds nor inherited is
    ignored outright rather than sent to the database to be refused: a
    worker inventing ids (§12) must not be able to make the coordinator
    issue a statement per message.

    "Entitled" is two sets, not one, and the second is why: `credited` is
    what this session was handed, and `recovered_tasks` is what the
    *database* said this worker was already running when it reconnected.
    Checking only the first would leave a worker that reconnected
    mid-execution unable to renew the task it is visibly still working on.

    A failure is logged and swallowed. A renewal that does not happen costs
    the task its lease, and losing a lease is a *recoverable* outcome the
    reclaimer handles by design — whereas letting the exception out would
    take down the read loop and every other task on the socket with it.
    """
    if task_id not in session.credited and task_id not in session.recovered_tasks:
        return
    if not _lease_due(session, task_id):
        return
    try:
        async with get_session() as db:
            if await renew_lease(db, task_id=task_id, worker_id=session.worker_id):
                await db.commit()
                LEASE_RENEWALS.labels("renewed").inc()
            else:
                # The row rejected the renewal — it is terminal, or it is no
                # longer this worker's because the reclaimer took it. Under
                # Step 3.2 that second case is how a worker learns its work
                # was reassigned; in 3.1 it is recorded and nothing more.
                LEASE_RENEWALS.labels("not_applied").inc()
    except Exception as exc:  # noqa: BLE001 — a lost renewal is recoverable; a raise is not
        LEASE_RENEWALS.labels("error").inc()
        logger.warning(
            "task_lease_renew_failed",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "detail": str(exc),
            },
        )
        return
    _mark_renewed(session, task_id)


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

    Step 2.3 could only detect this. **Phase 3.1 recovers it**, and not
    from here: the task already carries an acknowledgement lease from the
    moment it was claimed, `shorten_local_leases` cuts that lease to the
    disconnect grace, and the reclaimer requeues it. This function stays
    exactly as it was — naming each stranded task with its own correlation
    id — because the log line is still the thing that ties a disconnect to
    the recovery that follows it (§11).
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
        # Phase 3.1: the ack is the first coordinator-observed evidence that
        # the task arrived, so it converts the short delivery lease into a
        # full one. A worker that acks and then goes quiet still loses the
        # task — one TTL later instead of one ack timeout later.
        await _renew_if_due(session, task_id)
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
    session.lease_renew_due.pop(task_id, None)
    session.recovered_tasks.discard(task_id)
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
        # `mark_status` has just written a full execution lease and the
        # deadline inside its own UPDATE, so the next renewal is a TTL
        # fraction away — recording that here is what stops the very next
        # progress message from writing the same value again.
        _mark_renewed(session, task_id)
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

    **Phase 3.1 makes this message renew the task's lease**, and the
    distinction that keeps Decision #94 intact is worth stating exactly:
    what gets written is a lease timestamp, never the progress figure. A
    progress sample's *arrival* is coordinator-observed evidence that the
    worker is still there; its *contents* remain untrusted telemetry that
    decides nothing. This is what lets a legitimate ten-minute task hold
    its lease for ten minutes — and, because renewal is capped at
    `deadline_at`, what still cannot keep a task past its type's execution
    cap.
    """
    payload = message.get("payload") or {}
    task_id = str(payload.get("task_id") or "")

    # **Renew before the telemetry check, not after.** A worker that
    # reconnected mid-execution has no `current_tasks` entry for the task it
    # is still running — the `task_started` that would have created one
    # belongs to the session that died — so the check below returns early
    # for exactly the case that most needs its lease renewed. `_renew_if_due`
    # does its own entitlement check against `credited` and
    # `recovered_tasks`, so moving it up widens nothing (§12).
    await _renew_if_due(session, task_id)

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
            # Phase 3.7. The attempt row that gives this failure a reason.
            # In the same transaction as the transition, so a task is never
            # terminal without the record of why — the ordering migration
            # 0006's backfill chose, for the same reason.
            await record_execution_failure(
                db,
                task_id=task_id,
                worker_id=session.worker_id,
                error_type=error_type,
                correlation_id=correlation_id,
            )
            await db.commit()

    # Release the credit unless the database says this task was never this
    # worker's. `ILLEGAL` (already terminal) and `NOOP` (already FAILED) both
    # still release: the worker *was* running it, so the slot genuinely is
    # free, and holding the credit would cost capacity for the life of the
    # session.
    #
    # **Phase 3.1 adds the second half of that condition, and it is
    # Decision #168 pulled forward from Step 3.4 because 3.1 is what makes
    # the case reachable.** Before reassignment existed, the only way to
    # reach `NOT_OWNER` was a worker naming a task that was not its own, so
    # refusing to release was a §12 protection with no honest victim. A
    # reclaimed task's row no longer names its original worker, so that
    # worker's perfectly sincere late report now returns `NOT_OWNER` — and
    # the shipped rule would hold its credit consumed for the rest of the
    # session. A worker that lost three tasks to reassignment would quietly
    # stop being able to accept work, and it would present as "the fleet
    # mysteriously slows down during recovery".
    #
    # The fix needs no new concept: release when *this session actually
    # delivered that task id*. A guessed id is not in `credited`, so the
    # §12 protection is untouched; a genuine one is, so the slot is freed.
    if outcome not in (NOT_OWNER, NOT_FOUND) or task_id in session.credited:
        _release_credit(session, task_id)
    session.current_tasks.pop(task_id, None)
    session.lease_renew_due.pop(task_id, None)
    session.recovered_tasks.discard(task_id)
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

    **Phase 3.3 adds one outcome to answer for, `SUPERSEDED`** — this
    worker's result lost to another attempt, or arrived for a task that had
    already been failed or cancelled. It is acknowledged definitively like
    every other outcome, `accepted: false` because nothing was stored, and
    **it releases the credit**: unlike `NOT_OWNER` it is not a worker
    naming something that was never its own, it is a worker whose slot
    genuinely is free. Holding the credit here would make losing a race
    cost the worker capacity for the life of the session, which is the
    defect Decision #168 fixed on the neighbouring path.

    **Phase 3.4 adds a second, `FENCED`** — a result for a task that is
    still live but whose current attempt is not this one. It behaves like
    `SUPERSEDED` in every way the worker can see (definitive ack,
    `accepted: false`, credit released), and differs in one way the
    coordinator can: it commits, because the `task_attempts` row recording
    the fence is what makes the rejection inspectable afterwards. The rule
    itself lives in `task_queue.complete_task` and nowhere else.
    """
    payload = message.get("payload") or {}
    correlation_id = str(message.get("correlation_id") or "")
    raw_task_id = str(payload.get("task_id") or "")

    try:
        envelope = validate_result(payload, max_bytes=task_result_max_bytes())
    except MalformedResult as exc:
        TASK_RESULTS.labels("rejected").inc()
        # The slot is free, but **only release it if the message names a task
        # this session actually delivered.** A malformed result may carry no
        # task id at all, or a garbage one, and `_release_credit` cannot tell
        # those apart from a legitimate id: an empty string takes its unnamed
        # best-effort branch and pops an arbitrary held credit, and an
        # unrecognised string draws down the reconnect residue. Either would
        # free the slot of a task that is genuinely still running, on the say-so
        # of a message the coordinator has just rejected as not being a result
        # (§12). Same discipline as `NOT_OWNER`/`NOT_FOUND` on the failure path:
        # if the report cannot identify a slot, it releases none.
        # Phase 3.1: renew before the credit is released, while the id is
        # still one this session holds. A rejected result leaves the task
        # `RUNNING`, so the worker has just proved it is alive and working
        # on it — and one more lease period is what gives a worker whose
        # *next* attempt is well-formed somewhere to land. The task still
        # expires and retries if nothing better ever arrives.
        await _renew_if_due(session, raw_task_id)
        if raw_task_id and raw_task_id in session.credited:
            _release_credit(session, raw_task_id)
        session.current_tasks.pop(raw_task_id, None)
        session.lease_renew_due.pop(raw_task_id, None)
        session.recovered_tasks.discard(raw_task_id)
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
        # `FENCED` commits too, and it is the one outcome that writes
        # without transitioning: the result is refused, but the
        # `task_attempts` row recording that it was refused is the durable
        # half of "rejections are visible, not buried in logs". Rolling it
        # back with the rest of the no-op would leave the fence provable
        # only from a log line on one replica.
        if outcome in (TRANSITIONED, FENCED):
            await db.commit()

    # The same Decision #168 condition as `handle_task_failed`, and for the
    # same reason: after Step 3.1 a `NOT_OWNER` can mean "your task was
    # reassigned while you were finishing it", which is a sincere report
    # from a worker whose slot really is free.
    #
    # **Phase 3.4 puts `FENCED` in the same guarded set, not outside it.**
    # A fenced worker's slot genuinely is free, so the credit must come
    # back — but the outcome is reachable by naming a task id, and
    # releasing unconditionally would hand any worker a way to free a slot
    # of a task that is still running by naming an id (§12). Keyed on
    # `credited`, a genuine id releases and a guessed one does not, which is
    # exactly the discipline the neighbouring paths already apply.
    if outcome not in (NOT_OWNER, NOT_FOUND, FENCED) or task_id in session.credited:
        _release_credit(session, task_id)
        session.current_tasks.pop(task_id, None)
        session.lease_renew_due.pop(task_id, None)
        session.recovered_tasks.discard(task_id)
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
            # Phase 3.4 gives the fence its own event name rather than
            # folding it into the generic one. "A result was fenced" is the
            # thing an operator greps for during a recovery, and it should
            # not require knowing that `outcome` is a field on a line whose
            # name says only that something was not applied.
            "task_result_fenced" if outcome == FENCED else "task_result_not_applied",
            extra={
                "task_id": task_id,
                "worker_id": session.worker_id,
                "correlation_id": correlation_id,
                "outcome": outcome,
                # Which attempt the worker believed it was submitting. On a
                # fence this is the whole diagnosis, and it is worker-
                # reported — recorded, never trusted (§12).
                "attempt_number": envelope["attempt_number"],
                "session_epoch": envelope["session_epoch"],
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
    # Phase 3.5, and it is checked before the pass is even counted: a
    # draining replica must not claim a row. `dequeue` moves a task to
    # ASSIGNED durably, so claiming here and dying a second later strands
    # that task for a full `TASK_ACK_TIMEOUT_SECONDS` before the reclaimer
    # can have it back — work thrown away by the shutdown itself, on a
    # queue another live replica was ready to serve.
    if _draining:
        return 0

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


async def drain_local_sessions(timeout: float) -> dict[str, Any]:
    """Wait for work this replica has handed out to be acknowledged.

    **"In-flight work" here means deliveries, not executions**, and the
    distinction is the whole design. Waiting for the *tasks* to finish is
    not an option a shutdown can offer: a `sleep` task may legitimately run
    for the better part of an hour (`TASK_MAX_EXECUTION_SECONDS`), and no
    termination grace period is going to cover that. Those tasks do not
    need waiting for — they survive this process, because a lease is a row
    and not a socket, and the worker renews them on `hello` against
    whichever replica it lands on next (§3.0.13).

    What genuinely dies with this process is the frame in the socket buffer
    that has not been answered yet. A task delivered and not acked is
    durably `ASSIGNED` with nobody visibly executing it, and recovering it
    costs a full ack timeout — 30s by default, spent on a task a worker
    would have picked up in milliseconds. That is the loss a drain can
    actually prevent, and it usually takes a fraction of a second.

    Returns what it observed rather than a verdict, so the caller can log
    the honest number including the case where the window ran out.
    """
    started = time.monotonic()

    def outstanding() -> int:
        return sum(len(s.pending_acks) for s in _local_sessions.values())

    at_start = outstanding()
    while outstanding() and time.monotonic() - started < timeout:
        await asyncio.sleep(_DRAIN_POLL_SECONDS)

    return {
        "sessions": len(_local_sessions),
        "pending_acks_at_start": at_start,
        "pending_acks_at_end": outstanding(),
        "waited_seconds": round(time.monotonic() - started, 3),
        "timed_out": bool(outstanding()),
    }


async def shorten_local_leases(worker_id: str, reason: str) -> int:
    """A socket this replica held has closed — accelerate its tasks' leases.

    **This is not a second recovery trigger and it must not become one.**
    Nothing is reclaimed here: the leases are cut to
    `LEASE_DISCONNECT_GRACE_SECONDS`, and the reclaimer does what it always
    does, one tick later. The gate's reasoning (§3.0.2) is that a system
    with two recovery paths has to keep them agreeing forever, whereas a
    close that merely shortens a clock cannot disagree with anything.

    It is safe to run from a replica because of *what it is scoped to*: a
    socket this process personally observed closing, and only that
    worker's rows. Contrast the rejected design, reclaiming on `OFFLINE` —
    that status comes from a missing Redis key, so a Redis flush would
    have declared the whole fleet dead and reassigned every task in
    flight at once.

    Returns how many leases were shortened. A failure is logged and
    swallowed: without it the tasks still expire on the full TTL, which is
    slower recovery, not lost recovery.
    """
    grace = lease_disconnect_grace_seconds()
    try:
        async with get_session() as db:
            shortened = await shorten_worker_leases(
                db, worker_id=worker_id, grace_seconds=grace
            )
            if shortened:
                await db.commit()
    except Exception as exc:  # noqa: BLE001 — slower recovery beats a raised disconnect path
        logger.warning(
            "task_lease_shorten_failed",
            extra={"worker_id": worker_id, "reason": reason, "detail": str(exc)},
        )
        return 0

    if shortened:
        logger.info(
            "task_leases_shortened_on_disconnect",
            extra={
                "worker_id": worker_id,
                "tasks": shortened,
                "grace_seconds": grace,
                "reason": reason,
            },
        )
    return shortened


async def cancel_on_worker(worker_id: str, task_id: str, correlation_id: str) -> None:
    """Ask the worker that lost a task to stop burning CPU on it (Phase 3.2).

    **Best-effort by design, and nothing depends on it arriving** (gate
    §3.0.6). The task was reclaimed precisely because that worker stopped
    answering, so the most likely outcome is that this message reaches
    nobody. When it does land it saves the CPU of an execution whose result
    can no longer be accepted, and it does not save anything else — the
    reassignment already happened, in the database, before this was sent.

    It goes over the shipped `worker:{id}:push` Redis channel rather than
    over a socket, because **the reclaimer usually runs on a replica that
    does not hold that worker's socket** — a lease is a row, not a
    connection, so any replica's tick may reclaim any worker's task. The
    channel `POST /workers/{id}/push` and `_push_listener` already
    implement is exactly the "reach a socket someone else is holding"
    mechanism, so this adds no fan-out of its own.

    No protocol version bump: the worker's dispatch falls through unknown
    message types to a log line, so a worker built before this step ignores
    it and keeps computing, which is the same outcome as the message being
    lost.
    """
    envelope = Envelope(
        message_type="task_cancel",
        worker_id=worker_id,
        correlation_id=correlation_id,
        payload={"task_id": task_id, "reason": "lease_expired"},
    )
    try:
        await redis_client.publish(
            f"worker:{worker_id}:push", json.dumps(envelope.to_dict())
        )
    except Exception as exc:  # noqa: BLE001 — a cancel that is not delivered costs CPU, nothing else
        logger.warning(
            "task_cancel_publish_failed",
            extra={"task_id": task_id, "worker_id": worker_id, "detail": str(exc)},
        )


async def reclaim_once() -> int:
    """One reclaim pass. Returns how many tasks were returned to the queue.

    Every replica runs this and **no leader is elected**, which is the same
    call the retention sweep and the assignment engine already make (§3.9).
    Two replicas reclaiming at once is safe for precisely the reason two
    replicas dequeuing at once is safe: `FOR UPDATE SKIP LOCKED` means the
    second one steps over the locked row, and by the time the lock lifts
    that row is `QUEUED` and no longer matches the predicate. This inherits
    Step 2.2's measured proof — 10,000 claims, 0 duplicates, three pods —
    rather than introducing a new mechanism that would need its own.

    The doorbell is rung only when something was actually reclaimed, so an
    idle fleet's reclaim loop costs one indexed count query per tick and
    wakes nothing.
    """
    start = time.perf_counter()
    async with get_session() as db:
        reclaimed = await reclaim_expired_leases(db, batch=lease_reclaim_batch())
        if reclaimed:
            await db.commit()
    LEASE_RECLAIM_SECONDS.observe(time.perf_counter() - start)

    for row in reclaimed:
        LEASES_EXPIRED.inc()
        exhausted = row["status"] == FAILED
        if exhausted:
            TASKS_EXHAUSTED.inc()
        else:
            TASKS_REASSIGNED.inc()
        logger.warning(
            # Phase 3.2 splits the event name by branch rather than adding a
            # field to one line: "this task will be tried again" and "this
            # task is over" are different operational facts, and a log query
            # for the second must not have to filter the first out.
            "task_attempts_exhausted" if exhausted else "task_lease_expired",
            extra={
                "task_id": str(row["id"]),
                "task_type": row["task_type"],
                # The worker that lost the task, read from the row before it
                # was cleared — without it the recovery timeline cannot say
                # who stopped answering.
                "worker_id": (
                    str(row["previous_worker_id"]) if row["previous_worker_id"] else None
                ),
                "previous_status": row["previous_status"],
                # How overdue the lease was when this pass caught it. This is
                # the coordinator-observed detection lag, and it is what the
                # step's ≤35s / ≤65s bounds are verified against — measured,
                # not asserted.
                "overdue_seconds": round(
                    (
                        datetime.now(timezone.utc) - row["expired_at"]
                    ).total_seconds(),
                    3,
                ),
                "attempt": row["attempt_count"],
                # Phase 3.2: when this task becomes claimable again, and by
                # whom it will not be claimed. NULL on the exhausted branch,
                # which is how a reader tells the two apart without parsing
                # the event name.
                "not_before": (
                    row["not_before"].isoformat() if row["not_before"] else None
                ),
                "excluded_worker_id": (
                    None
                    if exhausted or not row["previous_worker_id"]
                    else str(row["previous_worker_id"])
                ),
                "reason": (
                    "execution_deadline_exceeded"
                    if row["deadline_exceeded"]
                    else "lease_expired"
                ),
                "correlation_id": row["correlation_id"],
            },
        )

        # Best-effort, after the requeue is committed: the worker that lost
        # the task may still be executing it, and a cancel it never receives
        # changes nothing that has already been decided.
        if row["previous_worker_id"]:
            await cancel_on_worker(
                str(row["previous_worker_id"]), str(row["id"]), row["correlation_id"]
            )

    if reclaimed:
        # Local first so this replica reacts without a Redis round trip,
        # then the fan-out so replicas holding other sockets do too.
        notify_local()
        await notify_work_available()
        # **Phase 3.2: ring it again when the backoff actually elapses.**
        # Found reviewing this step rather than by a test: the doorbell above
        # now wakes an engine that will find nothing, because every task it
        # just requeued is held by `not_before`. Without a second wake the
        # retry waits for the safety-net poll — up to
        # `ASSIGNMENT_POLL_INTERVAL_SECONDS` (30s by default) *on top of* a
        # backoff measured in seconds, which is a recovery that looks stalled
        # to anyone watching the console.
        _schedule_retry_wake(reclaimed)
    return len(reclaimed)


# Sleeping wake-ups this replica has scheduled, kept only so they are not
# garbage-collected mid-sleep (asyncio holds no strong reference to a task).
_retry_wakes: set[asyncio.Task] = set()


def _schedule_retry_wake(reclaimed: list[dict[str, Any]]) -> None:
    """Ring the doorbell once, when the earliest requeued task is eligible.

    **One wake-up per reclaim pass, not per task**, and only for the
    earliest `not_before` in it: a later task's own pass, or the safety-net
    poll, covers the rest. It costs no query and no timer thread — an
    `asyncio.sleep` and one Redis publish.

    Deliberately not a scheduler. The database remains the authority on
    when a task may be claimed (`not_before` is in the dequeue predicate),
    so a wake that never happens — this replica dying mid-sleep, the
    publish failing — costs latency and nothing else. That is why it is
    fire-and-forget and why nothing waits on it.
    """
    moments = [row["not_before"] for row in reclaimed if row["not_before"] is not None]
    if not moments:
        return
    delay = (min(moments) - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        return  # already eligible; the doorbell just rung is enough

    async def _wake() -> None:
        try:
            await asyncio.sleep(delay)
            notify_local()
            await notify_work_available()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a missed wake costs latency, nothing more
            logger.warning("retry_wake_failed", extra={"detail": str(exc)})

    task = asyncio.create_task(_wake())
    _retry_wakes.add(task)
    task.add_done_callback(_retry_wakes.discard)


async def lease_reclaim_loop() -> None:
    """The recovery engine. One per replica, started from the lifespan.

    A fixed tick rather than an event: there is no event to subscribe to.
    A lease does not *fire*, it simply stops being in the future, and the
    only way to notice is to look. That is why the detection bound this
    step publishes is a lease window **plus** one reclaim interval.

    A pass that raises is logged and the loop continues, matching the
    retention sweep: an unreachable database must not kill recovery for the
    life of the process — and note that a database outage takes the
    reclaimer out on its own, which is the designed behaviour in gate row
    13. The reclaimer needs the same database it has lost, so a Postgres
    outage cannot produce a reassignment storm.
    """
    interval = lease_reclaim_interval_seconds()
    logger.info(
        "lease_reclaimer_started",
        extra={"interval_seconds": interval, "batch": lease_reclaim_batch()},
    )
    while True:
        await asyncio.sleep(interval)
        try:
            # Drain: after a large outage there may be more expired leases
            # than one batch holds, and waiting a full interval per batch
            # would turn a 100-task backlog into a five-second stagger.
            while await reclaim_once() >= lease_reclaim_batch():
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad pass must not kill recovery
            logger.warning("lease_reclaim_pass_error", extra={"detail": str(exc)})


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
