"""Coordinator service.

Phase 1.2: real persistence (Postgres via versioned migrations, run
automatically and idempotently on startup), Redis wired and verified
live, structured JSON logs with correlation IDs, and separate liveness
(/health) vs readiness (/ready) endpoints. No authoritative state is
held in process memory — every check below reaches an external store,
which is what lets any coordinator replica serve any request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Header, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.assignment import (
    LocalSession,
    assignment_loop,
    current_tasks_key,
    lease_reclaim_loop,
    handle_capacity,
    handle_task_ack,
    handle_task_failed,
    handle_task_progress,
    handle_task_result,
    handle_task_started,
    is_draining,
    log_unacknowledged,
    notification_listener,
    notify_work_available,
    parse_capabilities,
    parse_tasks_in_flight,
    register_session,
    shorten_local_leases,
    unregister_session,
)
from app.config import (
    access_token_ttl_seconds,
    admin_secret_is_separate,
    heartbeat_offline_threshold_seconds,
    heartbeat_suspect_threshold_seconds,
    heartbeat_sweep_interval_seconds,
    register_rate_limit_per_minute,
    result_retention_days,
    result_retention_sweep_interval_seconds,
    run_migrations_on_startup,
    task_api_rate_limit_per_minute,
    task_dequeue_max_batch,
    task_enqueue_max_batch,
    task_list_max_limit,
    task_throughput_max_minutes,
    worker_claim_ttl_seconds,
    ws_ping_interval_seconds,
    ws_pong_timeout_seconds,
    ws_registry_ttl_seconds,
)
from app.db import engine, get_session
from app.logging_config import configure_logging
from app.metrics import (
    RESULTS_PURGED,
    TASK_API_RATE_LIMITED,
    TASKS_CANCELLED,
    MetricsMiddleware,
    metrics_endpoint,
)
from app.middleware import (
    CorrelationIDMiddleware,
    _caller_ip,
    client_ip_var,
    correlation_id_var,
)
from app.models import Worker
from app.redis_client import redis_client
from app.schemas import (
    DequeueTaskRequest,
    EnqueueTaskRequest,
    TaskPolicyRequest,
    PushRequest,
    RegisterRequest,
    ReleaseRequest,
    RevokeRequest,
    TokenRefreshRequest,
)
from app.security import (
    generate_access_token,
    generate_worker_credential,
    hash_credential,
    verify_admin_secret,
    verify_credential,
    verify_enrollment_secret,
)
from app.task_queue import (
    DEFAULT_LIST_LIMIT,
    NOT_CANCELLABLE,
    NOT_FOUND,
    TRANSITIONED,
    QueueLimitExceeded,
    cancel_queued_task,
    completions_per_minute,
    counts_by_status,
    dequeue,
    enqueue_batch,
    fenced_result_count,
    get_task,
    list_tasks,
    purge_expired_results,
    queue_depth,
    renew_worker_leases,
    task_attempts,
    worker_failure_counts,
)
from app.task_policies import (
    InvalidTaskPolicy,
    clear_policy,
    effective_policies,
    set_policy,
    validate_policy,
)
from app.task_states import UnknownTaskState
from app.task_types import InvalidTaskParameters, UnknownTaskType
from protocol import PROTOCOL_VERSION, Envelope

logger = logging.getLogger("coordinator")

RATE_LIMIT_WINDOW_SECONDS = 60
# Redis TTL on a token record outlives its nominal lifetime so an expired
# (but not yet garbage-collected) token can be reported as "expired"
# rather than indistinguishable from one that never existed ("malformed").
TOKEN_RECORD_TTL_MULTIPLIER = 3
WS_HELLO_TIMEOUT_SECONDS = 10

# Identifies which coordinator instance is holding a given worker's live
# socket (written into the Redis connection registry). Hostname is
# already unique per container/pod with zero extra config.
COORDINATOR_INSTANCE_ID = socket.gethostname()


def _admin_log(**fields: object) -> dict[str, object]:
    """Log extras for an admin-endpoint event, stamped with the caller's
    apparent address.

    `ADMIN_SECRET` is a single shared operator credential (Step 2.2.1), so
    the coordinator can say *that* an operator acted but not *which human*.
    The address is the closest thing to attribution that exists today —
    a hint, deliberately not an identity, and never an authentication
    input. See `middleware._client_ip` for why it can be spoofed. It is
    stamped on the rejection paths too, which is where it earns its keep:
    the Step 1.5.6 auth-spike alert is built on `*_rejected` log events,
    and "where from" is what makes such an alert actionable.
    """
    return {"client_ip": client_ip_var.get(), **fields}


def _run_migrations() -> None:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(cfg, "head")


async def _result_retention_loop() -> None:
    """Delete result bodies past `RESULT_RETENTION_DAYS` (Phase 2.5).

    Retention is a **documented period the system actually enforces**, not
    a paragraph in a design note — a retention policy nothing implements is
    a hypothesis, and this project has been careful not to record those as
    results (§10).

    Every replica runs its own sweep. That is deliberate and consistent with
    the assignment engine (Decision #89): the DELETE is idempotent and
    row-scoped, so concurrent sweeps race harmlessly, and adding leader
    election here would buy nothing but a failure mode. A pass that raises is
    logged and retried on the next tick — an unreachable database must not
    kill the loop for the life of the process.
    """
    interval = result_retention_sweep_interval_seconds()
    days = result_retention_days()
    logger.info(
        "result_retention_started",
        extra={"retention_days": days, "sweep_interval_seconds": interval},
    )
    while True:
        await asyncio.sleep(interval)
        # Bound before the try: if opening the session raises, an unbound
        # `purged` would raise NameError inside the handler below and that is
        # what would get logged — masking the real reason the sweep failed.
        purged = 0
        try:
            async with get_session() as session:
                purged = await purge_expired_results(session, retention_days=result_retention_days())
                if purged:
                    await session.commit()
            if purged:
                RESULTS_PURGED.inc(purged)
                logger.info(
                    "result_retention_purged",
                    extra={"rows": purged, "retention_days": result_retention_days()},
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad sweep must not kill the loop
            logger.warning("result_retention_sweep_failed", extra={"detail": str(exc)})


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("coordinator starting")
    # Step 2.2.1. Announced at startup rather than left to be discovered:
    # while the fallback is active, every worker holds a credential that
    # opens the admin endpoints (see `config.admin_secret`). Never logs
    # either secret — only whether they are the same one (§12).
    if admin_secret_is_separate():
        logger.info("admin_credential_separate")
    else:
        logger.warning(
            "admin_secret_fallback_in_use",
            extra={
                "detail": "ADMIN_SECRET is unset or equal to ENROLLMENT_SECRET; "
                "every worker can call the admin endpoints"
            },
        )
    if run_migrations_on_startup():
        await asyncio.to_thread(_run_migrations)
        # Alembic's fileConfig (invoked inside _run_migrations via env.py)
        # reconfigures the root logger's level and handler as a side effect —
        # reassert ours or every INFO log after this point is silently dropped.
        configure_logging()
        logger.info("migrations applied")
    else:
        logger.info("startup migrations skipped (RUN_MIGRATIONS_ON_STARTUP=false)")
    sweep_task = asyncio.create_task(_heartbeat_sweep_loop())
    # Step 2.3. One assignment engine per replica, assigning only to the
    # sockets this replica holds, plus the subscriber that wakes it when
    # any replica enqueues. Both are pure background workers: they hold no
    # authoritative state and a replica without them still serves every
    # request, it just stops handing out work (§3.9).
    assignment_task = asyncio.create_task(assignment_loop())
    notification_task = asyncio.create_task(notification_listener())
    # Step 2.5. Same shape as the two above and for the same reason: it holds
    # no state, and a replica without it still serves every request.
    retention_task = asyncio.create_task(_result_retention_loop())
    # Step 3.1. The recovery engine, and the same shape again: every replica
    # runs one, no leader is elected, and safety comes from
    # `FOR UPDATE SKIP LOCKED` rather than from coordination (§3.9). A
    # replica whose reclaimer is missing serves every request and simply
    # contributes nothing to recovery; the others still reclaim its
    # workers' tasks, because a lease is a row and not a socket.
    reclaim_task = asyncio.create_task(lease_reclaim_loop())
    yield
    # No app-level connection drain here: uvicorn's own `Server.shutdown()`
    # calls `connection.shutdown()` on every live WebSocket (a proper
    # close, code 1012 "service restart") and waits for each connection's
    # handler task to finish *before* this lifespan shutdown code runs —
    # verified live (Decisions Log #21). By this point every `ws_connect`
    # handler has already run its own `finally` cleanup below, so there is
    # nothing left here to drain.
    for background in (
        sweep_task,
        assignment_task,
        notification_task,
        retention_task,
        reclaim_task,
    ):
        background.cancel()
        try:
            await background
        except asyncio.CancelledError:
            pass
    await engine.dispose()
    await redis_client.aclose()
    logger.info("coordinator stopped")


app = FastAPI(title="Coordinator", version="0.2.0", lifespan=lifespan)
app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(MetricsMiddleware)

@app.exception_handler(RequestValidationError)
async def _validation_error_without_echo(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422 responses that name the problem without repeating the input.

    FastAPI's default handler echoes the offending value back in each
    error's `input` field. That is convenient and, on a credential-bearing
    endpoint, dangerous: during session 13 a demo helper sent `ADMIN_SECRET`
    under the wrong field name, and the validation error handed the live
    secret straight back in the response body — where it was then captured
    in a transcript and had to be rotated (Decision #119).

    CLAUDE.md §12 says credentials are never logged and never rendered. A
    response body is a rendering. So `input` and `ctx` are dropped and only
    the location, the message and the error type survive — which is all a
    caller needs to fix its own request, since it already knows what it
    sent.

    This is a whole-app handler rather than a per-endpoint one on purpose: a
    future endpoint that takes a secret should not have to remember.
    """
    safe = [
        {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]
    logger.warning("request_validation_failed", extra={"errors": safe})
    return JSONResponse(status_code=422, content={"detail": safe})


# Prometheus scrape target (Step 1.5.6). Unauthenticated — exposes only
# aggregate operational counters, never worker credentials or tokens, and
# is reachable in-cluster by Prometheus (not via the public ingress route).
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "coordinator", "status": "ok"}


# Deployed image SHA, injected by the Helm chart (GIT_SHA env = image.tag).
# Lets CD assert the running version matches the commit it deployed
# (Step 1.5.4, exit criterion 4). "unknown" outside k8s / local runs.
GIT_SHA = os.getenv("GIT_SHA", "unknown")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only — the process is up. Never checks dependencies."""
    return {"status": "healthy", "version": GIT_SHA}


@app.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness — the coordinator can actually serve requests.

    Checks every external dependency directly rather than trusting
    cached state, since nothing authoritative is held in memory.
    """
    # Phase 3.5. A draining replica is *healthy* and not *ready*, and the
    # split is the point: `/health` keeps answering, so the liveness probe
    # never restarts a pod that is deliberately shutting down, while
    # `/ready` fails immediately so the Service and the ingress stop
    # sending it new workers before its sockets close.
    #
    # Answered before the dependency checks rather than after, because the
    # answer cannot change: no result from Postgres or Redis makes a
    # draining replica ready again, and a shutdown should not be spending
    # its window on queries whose outcome it will ignore.
    if is_draining():
        response.status_code = 503
        return {"status": "draining", "checks": {}}

    checks: dict[str, str] = {}
    healthy = True

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — reported to caller, not swallowed
        checks["database"] = f"error: {exc}"
        healthy = False

    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
        healthy = False

    response.status_code = 200 if healthy else 503
    logger.info("readiness check", extra={"ready": healthy, "checks": checks})
    return {"status": "ready" if healthy else "not_ready", "checks": checks}


HEARTBEAT_METRICS_TTL_MULTIPLIER = 3


async def _merge_metrics(worker_id: str, **fields: object) -> None:
    """Read-modify-write the Redis-cached metrics snapshot Step 1.6's own
    exit criteria require ("latest metrics cached in Redis for the
    dashboard to read cheaply") rather than a Postgres write per
    heartbeat/pong — this is ephemeral, high-frequency, coordinator-
    observed state, matching the existing `worker:{id}:*` convention."""
    key = f"worker:{worker_id}:metrics"
    existing = await redis_client.get(key)
    metrics: dict[str, object] = json.loads(existing) if existing else {}
    metrics.update(fields)
    ttl = heartbeat_offline_threshold_seconds() * HEARTBEAT_METRICS_TTL_MULTIPLIER
    await redis_client.set(key, json.dumps(metrics), ex=ttl)


async def _transition_status(worker_uuid: uuid.UUID, new_status: str, trigger: str, **extra: object) -> None:
    """The single write path for `Worker.status` (see `models.py`'s
    docstring) — every transition is logged with its trigger, satisfying
    the Step 1.6 exit criterion, and a no-op if the worker is already in
    the target state (avoids redundant DB writes and log spam when e.g.
    a heartbeat arrives while status is already ONLINE).

    QUARANTINED is sticky: nothing here can transition a worker back out
    of it. Without this guard, a socket that was already open *before*
    `/workers/{id}/revoke` was called keeps sending heartbeats — the
    live connection isn't dropped until it naturally ends (Decisions Log
    #18, revocation doesn't re-check a socket mid-connection) — and
    those heartbeats would otherwise silently flip the DB status back to
    ONLINE, contradicting the revocation an operator just issued. Un-
    quarantine isn't a feature that exists yet; when it is, it gets its
    own explicit call site, not a side effect of this guard being
    loosened.
    """
    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)
        if worker is None or worker.status == new_status:
            return
        if worker.status == "QUARANTINED":
            return
        old_status = worker.status
        worker.status = new_status
        await session.commit()
    logger.info(
        "worker_state_transition",
        extra={"worker_id": str(worker_uuid), "from": old_status, "to": new_status, "trigger": trigger, **extra},
    )


async def _sweep_heartbeats() -> None:
    """Background liveness check, independent of any single WebSocket's
    own transport-level timeout (`ws_pong_timeout_seconds`) — this is
    what catches an abrupt `docker kill` (no close frame, no exception
    raised anywhere) within the documented SUSPECT/OFFLINE thresholds
    rather than waiting on the much longer transport timeout. Reads
    `last_heartbeat_at` off the coordinator's own Redis-cached clock
    (Decisions Log #6), never the worker-supplied envelope timestamp."""
    suspect_threshold = heartbeat_suspect_threshold_seconds()
    offline_threshold = heartbeat_offline_threshold_seconds()
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        result = await session.execute(
            select(Worker.id, Worker.status).where(Worker.status.in_(["CONNECTING", "ONLINE", "SUSPECT"]))
        )
        candidates = result.all()

    for worker_uuid, current_status in candidates:
        metrics_json = await redis_client.get(f"worker:{worker_uuid}:metrics")
        elapsed = None
        if metrics_json is not None:
            last_raw = json.loads(metrics_json).get("last_heartbeat_at")
            if last_raw is not None:
                elapsed = (now - datetime.fromisoformat(last_raw)).total_seconds()

        if elapsed is None or elapsed > offline_threshold:
            if current_status != "OFFLINE":
                await _transition_status(
                    worker_uuid, "OFFLINE", "heartbeat_missed_offline_threshold", elapsed_seconds=elapsed
                )
        elif elapsed > suspect_threshold and current_status != "SUSPECT":
            await _transition_status(
                worker_uuid, "SUSPECT", "heartbeat_missed_suspect_threshold", elapsed_seconds=elapsed
            )


async def _heartbeat_sweep_loop() -> None:
    interval = heartbeat_sweep_interval_seconds()
    while True:
        try:
            await _sweep_heartbeats()
        except Exception as exc:  # noqa: BLE001 — keep the sweep alive across a transient DB/Redis blip
            logger.warning("heartbeat_sweep_error", extra={"detail": str(exc)})
        await asyncio.sleep(interval)


async def _fixed_window_limited(scope: str, source_ip: str, limit: int) -> bool:
    """Fixed-window counter per source IP, per named scope.

    One counter family, two callers: worker registration (Phase 1.3) and the
    operator task API (Phase 2.6). The scopes are separate keys on purpose —
    a registration flood must not consume an operator's budget, and vice
    versa, because they are different callers defending against different
    things.

    Both limits are recommendations, not measured values; see
    `config.register_rate_limit_per_minute` and
    `config.task_api_rate_limit_per_minute`.
    """
    key = f"{scope}:ratelimit:{source_ip}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    return count > limit


async def _rate_limited(source_ip: str) -> bool:
    return await _fixed_window_limited("register", source_ip, register_rate_limit_per_minute())


async def _operator_guard(
    request: Request, response: Response, *, secret: str, event: str
) -> dict[str, object] | None:
    """Rate-limit then authenticate an operator task API call (Phase 2.6).

    Returns the error body to send, or None to proceed.

    **Order matters and is deliberate: limit first, authenticate second.**
    An unauthenticated flood is exactly what the limiter is for, so it must
    not be reachable only by callers who already hold the credential. It is
    the same order `register_worker` uses, for the same reason.

    The rejection log keeps each endpoint's own event name (`enqueue_...`,
    `task_list_...`) rather than one shared string: the Step 1.5.6 auth-spike
    alert is built on `*_rejected` events, and collapsing them would tell an
    operator that *something* was refused without saying what.
    """
    if await _fixed_window_limited(
        "taskapi", _caller_ip(request), task_api_rate_limit_per_minute()
    ):
        response.status_code = 429
        TASK_API_RATE_LIMITED.inc()
        logger.warning("task_api_rate_limited", extra=_admin_log(endpoint=event))
        return {"detail": "rate limited"}
    if not verify_admin_secret(secret):
        response.status_code = 401
        logger.warning(f"{event}_rejected_invalid_admin_secret", extra=_admin_log())
        return {"detail": "invalid admin credential"}
    return None


@app.post("/workers/register", status_code=201)
async def register_worker(request: Request, body: RegisterRequest, response: Response) -> dict[str, object]:
    """First-contact enrollment, and reaffirmation on every subsequent
    worker startup (the worker CLI reuses its persisted identity rather
    than re-enrolling). Both paths share one endpoint; which one runs is
    decided by whether the caller presents a fresh enrollment secret or
    an already-issued worker_id/worker_credential pair.
    """
    # The real caller, not the ingress pod. Keying the rate limiter on the
    # socket peer put every external worker in one shared bucket, turning
    # a per-client limit into a fleet-wide one — see `middleware._caller_ip`
    # for the measurement that showed the forwarded value is trustworthy
    # behind this ingress.
    source_ip = _caller_ip(request)

    if await _rate_limited(source_ip):
        response.status_code = 429
        logger.warning("registration_rate_limited", extra={"source_ip": source_ip})
        return {"detail": "rate limited"}

    if body.worker_id and body.worker_credential:
        try:
            worker_uuid = uuid.UUID(body.worker_id)
        except ValueError:
            response.status_code = 401
            logger.warning("reaffirm_rejected_malformed_id", extra={"source_ip": source_ip})
            return {"detail": "invalid identity"}

        async with get_session() as session:
            worker = await session.get(Worker, worker_uuid)
            if (
                worker is None
                or worker.revoked
                or not worker.credential_hash
                or not verify_credential(body.worker_credential, worker.credential_hash)
            ):
                response.status_code = 401
                logger.warning(
                    "reaffirm_rejected", extra={"worker_id": body.worker_id, "source_ip": source_ip}
                )
                return {"detail": "invalid identity"}

            claim_key = f"worker:{worker.id}:claim"
            claim_set = await redis_client.set(
                claim_key, str(uuid.uuid4()), nx=True, ex=worker_claim_ttl_seconds()
            )
            if not claim_set:
                response.status_code = 409
                logger.warning(
                    "duplicate_identity_detected",
                    extra={"worker_id": str(worker.id), "source_ip": source_ip},
                )
                return {"detail": "identity already active elsewhere"}

            worker.agent_version = body.agent_version
            await session.commit()

        response.status_code = 200
        logger.info("worker_reaffirmed", extra={"worker_id": str(worker.id), "source_ip": source_ip})
        return {"worker_id": str(worker.id), "status": "reaffirmed"}

    if not body.enrollment_secret or not verify_enrollment_secret(body.enrollment_secret):
        response.status_code = 401
        logger.warning("registration_rejected_invalid_credential", extra={"source_ip": source_ip})
        return {"detail": "invalid enrollment credential"}

    raw_credential = generate_worker_credential()
    worker = Worker(
        credential_hash=hash_credential(raw_credential),
        agent_version=body.agent_version,
        registration_metadata={"source_ip": source_ip},
    )
    async with get_session() as session:
        session.add(worker)
        await session.commit()
        await session.refresh(worker)

    await redis_client.set(f"worker:{worker.id}:claim", str(uuid.uuid4()), ex=worker_claim_ttl_seconds())

    logger.info("worker_registered", extra={"worker_id": str(worker.id), "source_ip": source_ip})
    return {"worker_id": str(worker.id), "worker_credential": raw_credential, "status": "registered"}


@app.post("/workers/release")
async def release_worker(body: ReleaseRequest) -> dict[str, str]:
    """Best-effort claim release on graceful worker shutdown, so a normal
    restart never trips the duplicate-use check in `register_worker`.
    Always returns 200 — this clears a claim tied to a verified
    credential and does nothing destructive, so there's no information
    worth withholding from a caller that fails verification."""
    try:
        worker_uuid = uuid.UUID(body.worker_id)
    except ValueError:
        return {"status": "ignored"}

    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)

    if worker is None or not worker.credential_hash or not verify_credential(
        body.worker_credential, worker.credential_hash
    ):
        return {"status": "ignored"}

    await redis_client.delete(f"worker:{worker_uuid}:claim")
    logger.info("worker_claim_released", extra={"worker_id": str(worker_uuid)})
    return {"status": "released"}


_WORKER_METRICS_FIELDS = (
    "sequence",
    "uptime_seconds",
    "cpu_percent",
    "memory_percent",
    "latency_ms",
    "last_heartbeat_at",
)


@app.get("/workers")
async def list_workers(response: Response, x_admin_secret: str = Header(default="")) -> dict[str, object]:
    """Phase 1.8: the dashboard's one read path. Merges the durable
    Postgres row (identity, status, agent_version — survives restart)
    with the ephemeral Redis-cached metrics and connection registry
    (Phase 1.6/1.5) for a live view, reflecting CLAUDE.md §3.9: no
    authoritative state lives in coordinator memory, so this reaches
    both stores fresh on every call rather than caching anything here.
    Same operator credential as `/revoke`, `/push` and the task endpoints
    — since Step 2.2.1 that is `ADMIN_SECRET`, a different secret from the
    one workers enroll with (see `config.admin_secret`)."""
    if not verify_admin_secret(x_admin_secret):
        response.status_code = 401
        logger.warning("list_workers_rejected_invalid_admin_secret", extra=_admin_log())
        return {"detail": "invalid admin credential"}

    async with get_session() as session:
        result = await session.execute(select(Worker))
        workers = result.scalars().all()

    items: list[dict[str, object]] = []
    for worker in workers:
        metrics_json = await redis_client.get(f"worker:{worker.id}:metrics")
        metrics = json.loads(metrics_json) if metrics_json else {}
        registry_json = await redis_client.get(f"worker:{worker.id}:connection")
        registry = json.loads(registry_json) if registry_json else None
        # Step 2.4: what this worker is executing right now, latest progress
        # sample included. Read from Redis like everything else ephemeral, so
        # any replica answers identically (§3.9) — including a replica that
        # does not hold this worker's socket.
        tasks_json = await redis_client.get(current_tasks_key(str(worker.id)))
        current_tasks = json.loads(tasks_json) if tasks_json else []

        item: dict[str, object] = {
            "worker_id": str(worker.id),
            "status": worker.status,
            "revoked": worker.revoked,
            "agent_version": worker.agent_version,
            "created_at": worker.created_at.isoformat(),
            "connected": registry is not None,
            "session_epoch": registry.get("session_epoch") if registry else None,
            # Step 2.3 declared capabilities. Read from the Redis registry
            # rather than from any process's memory, so every replica
            # answers identically (§3.9). None when not connected.
            "max_concurrent": registry.get("max_concurrent") if registry else None,
            "supported_task_types": registry.get("supported_task_types") if registry else None,
            "current_tasks": current_tasks,
            "running_task_count": len(current_tasks),
        }
        for field in _WORKER_METRICS_FIELDS:
            item[field] = metrics.get(field)
        items.append(item)

    return {"workers": items, "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/workers/failures")
async def worker_failures(
    request: Request, response: Response, x_admin_secret: str = Header(default="")
) -> dict[str, object]:
    """Per-worker failure counters (Phase 3.2 exit criterion).

    One row per worker per outcome, counted from `task_attempts` — the same
    rows `GET /tasks/{id}` shows on an individual task, aggregated the other
    way. `REASSIGNED` means a worker lost a task that was tried again;
    `FAILED` means it held the attempt that used the last one up.

    **Derived, never accumulated.** A counter column on `workers` would be
    a second write on the recovery path and a number that could disagree
    with the history it summarises; this cannot drift, because it *is* the
    history.

    **A separate endpoint from `GET /workers` on purpose.** That one is the
    dashboard's 2s poll (§6); this is a grouped scan over a table that
    grows with every abnormal ending. The rare read must not be charged to
    the frequent one — the same reasoning that keeps `counts_by_status` out
    of `queue_depth`.

    This is also the read Phase 4 consumes: "which workers fail most" is a
    scheduling input, and it is **coordinator-observed** (§12) — every row
    here was written by the reclaimer, never reported by a worker.

    Declared before `/workers/{worker_id}/...` routes: FastAPI matches in
    declaration order, and `failures` must not be read as a worker id.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="worker_failures"
    )
    if rejection is not None:
        return rejection

    async with get_session() as session:
        rows = await worker_failure_counts(session)

    return {
        "workers": [
            {
                "worker_id": str(row["worker_id"]),
                "outcome": row["outcome"],
                "failures": int(row["failures"]),
                "last_failure_at": row["last_failure_at"].isoformat(),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@app.post("/workers/token/refresh")
async def refresh_token(body: TokenRefreshRequest, response: Response) -> dict[str, object]:
    """Mint a new short-lived access token against the long-lived
    `worker_credential` issued at registration (Phase 1.3). Every refresh
    bumps `worker:{id}:token_gen` in Redis and stamps the new token with
    that generation — any token minted before it is superseded
    immediately, which is what makes replay of an old token detectable
    in `verify_token` rather than just eventually-expiring.
    """
    try:
        worker_uuid = uuid.UUID(body.worker_id)
    except ValueError:
        response.status_code = 401
        logger.warning("token_refresh_rejected_malformed_id")
        return {"detail": "invalid identity"}

    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)

    if (
        worker is None
        or worker.revoked
        or not worker.credential_hash
        or not verify_credential(body.worker_credential, worker.credential_hash)
    ):
        response.status_code = 401
        logger.warning("token_refresh_rejected", extra={"worker_id": body.worker_id})
        return {"detail": "invalid credential"}

    generation = await redis_client.incr(f"worker:{worker_uuid}:token_gen")
    raw_token = generate_access_token()
    ttl = access_token_ttl_seconds()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    record = {"worker_id": str(worker_uuid), "generation": generation, "expires_at": expires_at.isoformat()}
    await redis_client.set(
        f"access_token:{hash_credential(raw_token)}",
        json.dumps(record),
        ex=ttl * TOKEN_RECORD_TTL_MULTIPLIER,
    )

    logger.info("access_token_issued", extra={"worker_id": str(worker_uuid), "generation": generation})
    return {"access_token": raw_token, "expires_in": ttl}


@app.get("/workers/token/verify")
async def verify_token(response: Response, authorization: str = Header(default="")) -> dict[str, object]:
    """Validate a Bearer access token, rejecting malformed, expired, and
    replayed (superseded-by-a-newer-refresh) tokens distinctly and
    logging each distinctly, per the Phase 1.4 exit criterion."""
    if not authorization.startswith("Bearer "):
        response.status_code = 401
        logger.warning("token_verify_rejected_malformed")
        return {"detail": "malformed token"}

    raw_token = authorization.removeprefix("Bearer ")
    record_json = await redis_client.get(f"access_token:{hash_credential(raw_token)}")
    if record_json is None:
        response.status_code = 401
        logger.warning("token_verify_rejected_malformed")
        return {"detail": "malformed token"}

    record = json.loads(record_json)
    worker_uuid = uuid.UUID(record["worker_id"])

    if datetime.now(timezone.utc) >= datetime.fromisoformat(record["expires_at"]):
        response.status_code = 401
        logger.warning("token_verify_rejected_expired", extra={"worker_id": record["worker_id"]})
        return {"detail": "expired token"}

    current_generation = await redis_client.get(f"worker:{worker_uuid}:token_gen")
    if current_generation is not None and int(current_generation) != record["generation"]:
        response.status_code = 401
        logger.warning("token_verify_rejected_replayed", extra={"worker_id": record["worker_id"]})
        return {"detail": "superseded token"}

    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)
    if worker is None or worker.revoked:
        response.status_code = 401
        logger.warning("token_verify_rejected_revoked", extra={"worker_id": record["worker_id"]})
        return {"detail": "revoked"}

    return {"worker_id": record["worker_id"], "status": "valid"}


@app.post("/workers/{worker_id}/revoke")
async def revoke_worker(worker_id: str, body: RevokeRequest, response: Response) -> dict[str, str]:
    """Revoke a worker ID. Guarded by the operator credential, which since
    Step 2.2.1 is `ADMIN_SECRET` and is deliberately NOT the secret workers
    enroll with — otherwise any worker could revoke its peers (see
    `config.admin_secret`).

    Effect is immediate on every enforcement point that exists today:
    register/reaffirm and token refresh both check `revoked` and reject
    at once. There's no live connection yet to actively drop (Phase
    1.5) — an already-issued access token dies at its own short TTL,
    which is the documented "bounded time to effect" until then.
    """
    if not verify_admin_secret(body.admin_secret):
        response.status_code = 401
        logger.warning("revoke_rejected_invalid_admin_secret", extra=_admin_log(worker_id=worker_id))
        return {"detail": "invalid admin credential"}

    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError:
        response.status_code = 404
        return {"detail": "not found"}

    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)
        if worker is None:
            response.status_code = 404
            return {"detail": "not found"}
        worker.revoked = True
        old_status = worker.status
        worker.status = "QUARANTINED"
        await session.commit()

    logger.info("worker_revoked", extra=_admin_log(worker_id=worker_id))
    logger.info(
        "worker_state_transition",
        extra={"worker_id": worker_id, "from": old_status, "to": "QUARANTINED", "trigger": "worker_revoked"},
    )
    return {"worker_id": worker_id, "status": "revoked"}


async def _authenticate_ws_token(raw_token: str) -> uuid.UUID | None:
    """WS-handshake-only auth check. Deliberately separate from
    `verify_token`'s REST endpoint above rather than refactored to share
    it — that endpoint's distinct-reason logging (malformed/expired/
    replayed/revoked) is an already-verified Phase 1.4 exit criterion,
    and this path only needs a yes/no answer plus the worker ID."""
    record_json = await redis_client.get(f"access_token:{hash_credential(raw_token)}")
    if record_json is None:
        return None
    record = json.loads(record_json)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(record["expires_at"]):
        return None
    worker_uuid = uuid.UUID(record["worker_id"])
    current_generation = await redis_client.get(f"worker:{worker_uuid}:token_gen")
    if current_generation is not None and int(current_generation) != record["generation"]:
        return None
    async with get_session() as session:
        worker = await session.get(Worker, worker_uuid)
    if worker is None or worker.revoked:
        return None
    return worker_uuid


async def _ws_reject(websocket: WebSocket, event: str, close_code: int, **log_extra: object) -> None:
    logger.warning(event, extra=log_extra)
    envelope = Envelope(message_type="error", payload={"reason": event})
    try:
        await websocket.send_text(json.dumps(envelope.to_dict()))
    except Exception:  # noqa: BLE001 — best-effort, we're closing anyway
        pass
    await websocket.close(code=close_code)


async def _ping_loop(worker_id: str, websocket: WebSocket, send_lock: asyncio.Lock, ping_state: dict) -> None:
    """`ping_state["sent_at"]` is a monotonic clock reading shared with the
    main receive loop below, which stamps the round-trip latency into
    Redis when the matching `pong` arrives — coordinator-measured, per
    Step 1.6's "latency measured coordinator-side" requirement."""
    interval = ws_ping_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        envelope = Envelope(message_type="ping", worker_id=worker_id)
        ping_state["sent_at"] = time.monotonic()
        async with send_lock:
            await websocket.send_text(json.dumps(envelope.to_dict()))


async def _push_listener(worker_id: str, websocket: WebSocket, send_lock: asyncio.Lock) -> None:
    """Delivers on-demand pushes (`POST /workers/{id}/push`) regardless
    of which coordinator instance published them — this instance
    subscribes to the worker's channel for as long as it holds the
    connection and forwards whatever arrives."""
    channel = f"worker:{worker_id}:push"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            async with send_lock:
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


async def _control_listener(
    worker_id: str, my_session_epoch: int, websocket: WebSocket, send_lock: asyncio.Lock
) -> None:
    """Step 1.7 session-conflict resolution: every accepted handshake
    (below) publishes its own new epoch to this per-worker control
    channel *before* it becomes authoritative. Whichever coordinator
    instance is holding an older live session for the same worker ID —
    which may not be this instance, since any replica can hold the
    connection (CLAUDE.md §3.9) — is listening here for exactly that,
    and force-closes its own socket the moment it sees a newer epoch.
    "One winner, always": the newer handshake never waits on this to
    complete, and the older session never lingers past this message.
    """
    channel = f"worker:{worker_id}:control"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            data = json.loads(message["data"])
            new_epoch = data.get("new_session_epoch")
            if new_epoch is None or new_epoch <= my_session_epoch:
                continue
            logger.info(
                "session_superseded",
                extra={"worker_id": worker_id, "old_session_epoch": my_session_epoch, "new_session_epoch": new_epoch},
            )
            envelope = Envelope(
                message_type="session_evicted",
                worker_id=worker_id,
                session_epoch=my_session_epoch,
                payload={"reason": "superseded_by_newer_session", "new_session_epoch": new_epoch},
            )
            try:
                async with send_lock:
                    await websocket.send_text(json.dumps(envelope.to_dict()))
            except Exception:  # noqa: BLE001 — best-effort, closing regardless
                pass
            await websocket.close(code=4409)
            return
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@app.websocket("/ws/connect")
async def ws_connect(websocket: WebSocket) -> None:
    """Persistent bidirectional channel (Decisions Log #1). Handshake:
    accept -> verify the Phase 1.4 access token from the Authorization
    header -> require a `hello` envelope naming the same worker ID and a
    matching protocol version -> ack with the new session epoch. After
    that: periodic ping/pong keeps the connection (and its Redis
    registry TTL) alive, and a per-worker pub/sub subscription lets any
    coordinator instance push to this worker on demand.
    """
    await websocket.accept()

    auth_header = websocket.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        await _ws_reject(websocket, "ws_auth_rejected_malformed", close_code=4401)
        return

    worker_uuid = await _authenticate_ws_token(auth_header.removeprefix("Bearer "))
    if worker_uuid is None:
        await _ws_reject(websocket, "ws_auth_rejected", close_code=4401)
        return

    await _transition_status(worker_uuid, "CONNECTING", "ws_authenticated")

    try:
        hello_raw = await asyncio.wait_for(websocket.receive_text(), timeout=WS_HELLO_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        logger.warning("ws_hello_not_received", extra={"worker_id": str(worker_uuid)})
        return

    hello = json.loads(hello_raw)
    if hello.get("message_type") != "hello":
        await _ws_reject(
            websocket, "ws_protocol_error_expected_hello", close_code=4002, worker_id=str(worker_uuid)
        )
        return
    if hello.get("worker_id") != str(worker_uuid):
        await _ws_reject(
            websocket, "ws_auth_rejected_worker_id_mismatch", close_code=4401, worker_id=str(worker_uuid)
        )
        return
    if hello.get("protocol_version") != PROTOCOL_VERSION:
        await _ws_reject(
            websocket,
            "ws_protocol_version_mismatch",
            close_code=4001,
            worker_id=str(worker_uuid),
            expected=PROTOCOL_VERSION,
            received=hello.get("protocol_version"),
        )
        return

    # Bind the worker's session correlation id (from its `hello` envelope)
    # to this coroutine's context so every subsequent log line for this
    # session — worker_connected, heartbeat_received, ping/pong, disconnect,
    # and the inherited ping/push/control tasks — carries it. The HTTP
    # middleware only covers request scope; WebSocket sessions were logging
    # correlation_id="-" without this (§11: one id, end to end, across replicas).
    session_correlation_id = hello.get("correlation_id")
    if session_correlation_id:
        correlation_id_var.set(session_correlation_id)

    # Step 2.3 capabilities, declared in `hello` and sanitised before use —
    # both fields are worker-reported and therefore untrusted (§12). See
    # `assignment.parse_capabilities` for exactly what is clamped and why.
    hello_payload = hello.get("payload") or {}
    max_concurrent, supported_task_types = parse_capabilities(hello_payload)
    # Step 2.4 (Decision #101): a worker reconnecting mid-execution is still
    # running that work, so its credits start where reality is, not at zero.
    tasks_in_flight = parse_tasks_in_flight(hello_payload, max_concurrent)

    worker_id = str(worker_uuid)
    session_epoch = await redis_client.incr(f"worker:{worker_id}:session_epoch")
    # Announce the new epoch before this session becomes authoritative —
    # whichever coordinator instance (any replica, per CLAUDE.md §3.9)
    # still holds a prior live session for this worker ID is listening
    # on this channel and evicts itself immediately (Step 1.7).
    await redis_client.publish(f"worker:{worker_id}:control", json.dumps({"new_session_epoch": session_epoch}))
    registry_key = f"worker:{worker_id}:connection"
    await redis_client.set(
        registry_key,
        json.dumps(
            {
                "session_epoch": session_epoch,
                "coordinator_instance": COORDINATOR_INSTANCE_ID,
                "connected_at": datetime.now(timezone.utc).isoformat(),
                # Step 2.3: published here so the declared capabilities are
                # readable by any replica and by the dashboard, not only by
                # the one process holding the socket.
                "max_concurrent": max_concurrent,
                "supported_task_types": list(supported_task_types),
            }
        ),
        ex=ws_registry_ttl_seconds(),
    )

    send_lock = asyncio.Lock()
    ack = Envelope(message_type="hello_ack", worker_id=worker_id, session_epoch=session_epoch)
    async with send_lock:
        await websocket.send_text(json.dumps(ack.to_dict()))

    await _transition_status(worker_uuid, "ONLINE", "ws_session_established")
    # Seeds a baseline so the Step 1.6 liveness sweep has something to
    # measure freshness against even before the worker's first real
    # heartbeat arrives.
    await _merge_metrics(worker_id, sequence=0, last_heartbeat_at=datetime.now(timezone.utc).isoformat())

    logger.info(
        "worker_connected",
        extra={
            "worker_id": worker_id,
            "session_epoch": session_epoch,
            "coordinator_instance": COORDINATOR_INSTANCE_ID,
        },
    )

    ping_state: dict[str, float | None] = {"sent_at": None}
    ping_task = asyncio.create_task(_ping_loop(worker_id, websocket, send_lock, ping_state))
    push_task = asyncio.create_task(_push_listener(worker_id, websocket, send_lock))
    control_task = asyncio.create_task(_control_listener(worker_id, session_epoch, websocket, send_lock))

    # Step 2.3: hand this socket to the assignment engine. Registered only
    # after the session is fully established and ONLINE, so a task is never
    # delivered down a half-open handshake. Sharing `send_lock` is what
    # keeps a task frame from interleaving with a ping or an eviction.
    local_session = LocalSession(
        worker_id=worker_id,
        session_epoch=session_epoch,
        websocket=websocket,
        send_lock=send_lock,
        max_concurrent=max_concurrent,
        supported_task_types=supported_task_types,
        residual_in_flight=tasks_in_flight,
    )
    register_session(local_session)

    # Step 3.1: a reconnecting worker is still executing whatever it had, so
    # renew the leases on its live tasks before the reclaimer's next tick.
    # This is the whole of "coordinator restart and recovery" for the worker
    # side of the problem, and it is why the gate could say Step 3.5 needs no
    # startup scan (§3.0.13): a task's lease is renewed by the worker coming
    # back, not by a coordinator sweeping its own tables on boot.
    #
    # **The task ids come from the database, never from the `hello`.** The
    # worker is trusted for its identity — the access token proved that —
    # and for nothing else (§12). It is asked which tasks it holds by
    # querying `assigned_worker_id`, not by reading a list it supplied.
    #
    # Failure is logged and swallowed: without the renewal those tasks
    # expire on their own clock and are reassigned, which is a worse outcome
    # than a clean reconnect but is not a broken one. Refusing the
    # connection would be.
    try:
        async with get_session() as db:
            renewed = await renew_worker_leases(db, worker_id=worker_id)
            if renewed:
                await db.commit()
        if renewed:
            # Hand the session the ids the *database* just named. Without
            # this the reconnecting session has no key for work it did not
            # deliver, so every later progress message about that work would
            # be skipped and the task reclaimed from a worker that was busy
            # running it. See `LocalSession.recovered_tasks`.
            local_session.recovered_tasks.update(renewed)
            logger.info(
                "task_leases_renewed_on_hello",
                extra={
                    "worker_id": worker_id,
                    "tasks": len(renewed),
                    "session_epoch": session_epoch,
                },
            )
    except Exception as exc:  # noqa: BLE001 — a failed renewal costs a reassignment, not a session
        logger.warning(
            "task_lease_renew_on_hello_failed",
            extra={"worker_id": worker_id, "detail": str(exc)},
        )

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=ws_pong_timeout_seconds())
            except asyncio.TimeoutError:
                logger.warning("worker_connection_timeout", extra={"worker_id": worker_id})
                break
            message = json.loads(raw)
            message_type = message.get("message_type")

            if message_type == "pong":
                await redis_client.expire(registry_key, ws_registry_ttl_seconds())
                if ping_state["sent_at"] is not None:
                    latency_ms = round((time.monotonic() - ping_state["sent_at"]) * 1000, 1)
                    await _merge_metrics(worker_id, latency_ms=latency_ms)
                continue

            if message_type == "heartbeat":
                payload = message.get("payload") or {}
                await _merge_metrics(
                    worker_id,
                    sequence=payload.get("sequence"),
                    uptime_seconds=payload.get("uptime_seconds"),
                    agent_version=payload.get("agent_version"),
                    reported_status=payload.get("status"),
                    cpu_percent=payload.get("cpu_percent"),
                    memory_percent=payload.get("memory_percent"),
                    last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                )
                await _transition_status(worker_uuid, "ONLINE", "heartbeat_received")
                logger.info(
                    "heartbeat_received",
                    extra={"worker_id": worker_id, "sequence": payload.get("sequence")},
                )
                continue

            # Step 2.3/2.4 task protocol. New `message_type` values only —
            # the envelope is untouched and PROTOCOL_VERSION stays "1.0",
            # which is the extension route Decision #6 specified.
            if message_type == "task_ack":
                await handle_task_ack(local_session, message)
                continue

            if message_type == "capacity":
                await handle_capacity(local_session, message)
                continue

            # Step 2.4. `task_started` is a state transition and
            # `task_progress` is telemetry; they are separate messages on
            # purpose (Decision #95), so Phase 3 can shed the second under
            # load without ever losing the first.
            if message_type == "task_started":
                await handle_task_started(local_session, message)
                continue

            if message_type == "task_progress":
                await handle_task_progress(local_session, message)
                continue

            if message_type == "task_failed":
                await handle_task_failed(local_session, message)
                continue

            # Step 2.5. The message that completes a task: validate the
            # result envelope, persist the body, move RUNNING -> COMPLETED,
            # release the credit, acknowledge. A pre-2.5 worker sends
            # `capacity` instead and is handled above — the coordinator
            # cannot tell one worker generation from another (§3.5), and
            # must not need to.
            if message_type == "task_result":
                await handle_task_result(local_session, message)
                continue

            logger.info("worker_message_received", extra={"worker_id": worker_id, "message_type": message_type})
    except WebSocketDisconnect:
        logger.info("worker_disconnected", extra={"worker_id": worker_id})
    finally:
        ping_task.cancel()
        push_task.cancel()
        control_task.cancel()
        # Step 2.3 exit criterion: name every task this session was handed
        # but never acknowledged. Step 3.1 is what recovers them.
        log_unacknowledged(local_session, reason="worker_disconnected")
        unregister_session(worker_id, session_epoch)
        current = await redis_client.get(registry_key)
        if current is not None and json.loads(current)["session_epoch"] == session_epoch:
            await redis_client.delete(registry_key)
            # Step 2.4: a disconnected worker has no *observable* current
            # task, whatever its threads are still doing. Clearing it beats
            # waiting out the TTL, which would leave the dashboard showing
            # progress that can no longer advance.
            await redis_client.delete(current_tasks_key(worker_id))
            await _transition_status(worker_uuid, "OFFLINE", "ws_disconnected")
            # Step 3.1: this replica personally observed the socket close, so
            # it cuts that worker's live leases to the disconnect grace.
            # Nothing is reclaimed here — the reclaimer still does that, one
            # tick later. The grace is longer than the worker's reconnect
            # backoff on purpose, so a worker that drops and comes straight
            # back renews its own leases on `hello` and loses no work.
            #
            # **Inside the epoch guard, and that placement is the whole
            # correctness argument.** A superseded session's teardown runs
            # *after* the winning session has registered and renewed
            # (Step 1.7), so shortening from it would cut a lease that a
            # live, working worker had just had extended — and with the
            # shipped defaults the grace (30s) and the renewal interval
            # (0.5 x 60s) are the same length, so there would be no margin
            # to absorb it. The guard makes the superseded case skip
            # entirely, exactly as it already does for the registry key.
            #
            # A residual interleaving remains and is recorded rather than
            # claimed away: if the new session writes the registry between
            # this read and the UPDATE, the shorten lands on the new
            # session's lease. The cost is bounded at one unnecessary
            # reassignment of a task that will be executed again — the
            # at-least-once guarantee (§3.0.5) already covers it — and the
            # window is the few milliseconds between two adjacent
            # statements.
            await shorten_local_leases(worker_id, reason="worker_disconnected")


@app.post("/workers/{worker_id}/push")
async def push_to_worker(worker_id: str, body: PushRequest, response: Response) -> dict[str, object]:
    """Push a message to a specific worker on demand, regardless of which
    coordinator instance holds its live connection (fanned out via the
    Redis `worker:{id}:push` channel). Fire-and-forward: this confirms
    the message was published, not that a worker received it — there is
    no connected-worker check and no delivery queue. Durable task
    delivery is M2's job, not this phase's.
    """
    if not verify_admin_secret(body.admin_secret):
        response.status_code = 401
        logger.warning("push_rejected_invalid_admin_secret", extra=_admin_log(worker_id=worker_id))
        return {"detail": "invalid admin credential"}

    try:
        uuid.UUID(worker_id)
    except ValueError:
        response.status_code = 404
        return {"detail": "not found"}

    envelope = Envelope(message_type=body.message_type, worker_id=worker_id, payload=body.payload or {})
    await redis_client.publish(f"worker:{worker_id}:push", json.dumps(envelope.to_dict()))
    logger.info("push_published", extra=_admin_log(worker_id=worker_id, message_type=body.message_type))
    return {"status": "published", "correlation_id": envelope.correlation_id}


# --------------------------------------------------------------------------
# Operator task API (Phase 2.2, completed in Phase 2.6)
#
# Step 2.2 built the minimum its own criteria needed — enqueue, depth, and
# the dequeue primitive. Step 2.6 completes the surface so that **no
# operator ever needs to touch the database directly**: listing with
# filters, inspection with the full lifecycle, and cancellation.
#
# Every endpoint here is guarded by `_operator_guard` — per-source-IP rate
# limit, then the operator credential. Since Step 2.2.1 that credential is
# `ADMIN_SECRET` rather than the shared enrollment secret; these endpoints
# are exactly why that split had to happen, since otherwise any worker
# could drain the queue (see `config.admin_secret`).
#
# One endpoint is deliberately outside the guard: `POST /tasks/dequeue`.
# See `config.task_api_rate_limit_per_minute` for why.
#
# **Credential placement.** The read endpoints take `X-Admin-Secret`; the
# two POSTs that predate this step also accept `admin_secret` in the body
# and keep doing so, because scripts, harnesses and the CD smoke test send
# it that way. The header is the documented path for new callers — a body
# field can end up echoed in a validation error, which is precisely how a
# live secret leaked in session 13 (see `_validation_error_without_echo`).
# --------------------------------------------------------------------------


@app.post("/tasks", status_code=201)
async def enqueue_tasks(
    request: Request,
    body: EnqueueTaskRequest,
    response: Response,
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """Create one or more queued tasks.

    Every task carries the request's correlation ID from creation (§11 and
    a Phase 2.1 exit criterion), so a task is traceable back to the call
    that made it, and a bulk enqueue shares one ID across the batch.

    A bulk create returns the correlation ID and **not** 10,000 task ids —
    the response would be larger than the request that caused it. Since
    Step 2.6 that is a complete answer rather than a dead end:
    `GET /tasks?correlation_id=...` lists exactly what the batch created,
    which is why migration 0004 indexes that column.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret or body.admin_secret, event="enqueue"
    )
    if rejection is not None:
        return rejection

    correlation_id = correlation_id_var.get()

    try:
        async with get_session() as session:
            task_ids = await enqueue_batch(
                session,
                task_type=body.task_type,
                correlation_id=correlation_id,
                count=body.count,
                max_batch=task_enqueue_max_batch(),
                parameters=body.parameters,
                payload=body.payload,
                priority=body.priority,
            )
            await session.commit()
    except (UnknownTaskType, InvalidTaskParameters, QueueLimitExceeded) as exc:
        response.status_code = 400
        logger.warning(
            "enqueue_rejected_invalid_task",
            extra=_admin_log(task_type=body.task_type, detail=str(exc)),
        )
        return {"detail": str(exc)}

    logger.info(
        "tasks_enqueued",
        extra=_admin_log(task_type=body.task_type, count=len(task_ids), priority=body.priority),
    )
    # Step 2.3: wake every replica's assignment engine. Published *after*
    # the commit, so a woken replica cannot look before the rows are
    # visible. Best-effort by design — see `assignment.notify_work_available`.
    await notify_work_available()
    # Ids are returned only for a single-task create. Echoing 10,000 UUIDs
    # back would make the response bigger than the request that caused it,
    # for no caller that needs them — a bulk enqueue is a load operation.
    return {
        "status": "queued",
        "count": len(task_ids),
        "task_ids": [str(task_id) for task_id in task_ids] if len(task_ids) == 1 else None,
        "correlation_id": correlation_id,
    }


def _task_summary(row: dict[str, Any]) -> dict[str, object]:
    """One task as a listing row (Phase 2.6).

    Metadata only. `has_result` replaces the result body — see
    `task_queue.list_tasks` for why a listing never joins `task_results`.
    """
    assigned_at = row["assigned_at"]
    completed_at = row["completed_at"]
    return {
        "task_id": str(row["id"]),
        "task_type": row["task_type"],
        "status": row["status"],
        "priority": row["priority"],
        "assigned_worker_id": (
            str(row["assigned_worker_id"]) if row["assigned_worker_id"] else None
        ),
        "created_at": row["created_at"].isoformat(),
        "assigned_at": assigned_at.isoformat() if assigned_at else None,
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "updated_at": row["updated_at"].isoformat(),
        "attempt_count": row["attempt_count"],
        "correlation_id": row["correlation_id"],
        "observed_duration_seconds": _observed_duration(assigned_at, completed_at),
        "has_result": row["result_id"] is not None,
        # Phase 3.1. `lease_expires_at` is when the coordinator will take
        # this task back if it hears nothing more; `deadline_at` is the cap
        # no amount of worker renewal can push past. Both are NULL for a
        # task that is queued or finished, which is the honest answer — a
        # task nobody holds has no lease.
        "lease_expires_at": (
            row["lease_expires_at"].isoformat() if row["lease_expires_at"] else None
        ),
        "deadline_at": row["deadline_at"].isoformat() if row["deadline_at"] else None,
        # Seconds of lease left, computed here rather than in the browser so
        # the countdown is against the **coordinator's** clock. A dashboard
        # subtracting from the viewer's clock would show a different number
        # on every laptop in the room, which is exactly the failure mode
        # `completions_per_minute` already avoids by bucketing in Postgres.
        "lease_seconds_remaining": _seconds_until(row["lease_expires_at"]),
        # Phase 3.2. Why a QUEUED task is sitting there: `not_before` is its
        # retry backoff, `excluded_worker_id` is the worker that lost the
        # previous attempt and may not immediately have it back. Both NULL
        # for a task that has never been reclaimed, which is the common case.
        "not_before": row["not_before"].isoformat() if row["not_before"] else None,
        "retry_in_seconds": _seconds_until(row["not_before"]),
        "excluded_worker_id": (
            str(row["excluded_worker_id"]) if row["excluded_worker_id"] else None
        ),
    }


def _seconds_until(moment: datetime | None) -> float | None:
    """Seconds from now until `moment`, negative once it has passed.

    Negative rather than clamped to zero on purpose: a lease that is 8
    seconds overdue and still sitting there is the visible signature of a
    reclaimer that has stopped, and clamping would hide it behind a row of
    zeroes.
    """
    if moment is None:
        return None
    return round((moment - datetime.now(timezone.utc)).total_seconds(), 1)


def _observed_duration(assigned_at: datetime | None, completed_at: datetime | None) -> float | None:
    """Coordinator-observed wall time from assignment to completion.

    The measurement that cannot be lied about, as distinct from the
    worker-reported `duration_seconds` inside the result envelope — see
    `inspect_task` for why both are reported and never blurred (§10).
    """
    if assigned_at is None or completed_at is None:
        return None
    return round((completed_at - assigned_at).total_seconds(), 3)


@app.get("/tasks")
async def list_tasks_endpoint(
    request: Request,
    response: Response,
    status: list[str] | None = Query(default=None),
    task_type: str | None = None,
    worker_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """List tasks, newest first, with filters (Phase 2.6).

    Filters combine with AND: `status` (repeatable — `?status=QUEUED&
    status=RUNNING`), `task_type`, `worker_id`, `correlation_id`. An
    unrecognised status or task type is a **400, not an empty list**: an
    operator who typos `RUNING` must be told, not shown "no tasks" and left
    to conclude the fleet is idle.

    Paging is `limit`/`offset`, capped by `TASK_LIST_MAX_LIMIT`, with
    `has_more` instead of a total — the reasoning, and the cost that drove
    it, is in `task_queue.list_tasks`.

    The filters that were actually applied are echoed back. That is not
    decoration: with paging and a shared credential it is the one thing that
    makes a pasted response self-describing when it turns up later in an
    incident channel.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_list"
    )
    if rejection is not None:
        return rejection

    parsed_worker: uuid.UUID | None = None
    if worker_id is not None:
        try:
            parsed_worker = uuid.UUID(worker_id)
        except ValueError:
            response.status_code = 400
            return {"detail": "worker_id is not a valid UUID"}

    try:
        async with get_session() as session:
            rows, has_more = await list_tasks(
                session,
                statuses=status,
                task_type=task_type,
                worker_id=parsed_worker,
                correlation_id=correlation_id,
                limit=limit,
                offset=offset,
                max_limit=task_list_max_limit(),
            )
    except (UnknownTaskState, UnknownTaskType, QueueLimitExceeded) as exc:
        response.status_code = 400
        logger.warning("task_list_rejected_invalid_filter", extra=_admin_log(detail=str(exc)))
        return {"detail": str(exc)}

    return {
        "tasks": [_task_summary(row) for row in rows],
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "filters": {
            "status": status,
            "task_type": task_type,
            "worker_id": worker_id,
            "correlation_id": correlation_id,
        },
    }


@app.get("/tasks/depth")
async def task_queue_depth(
    request: Request, response: Response, x_admin_secret: str = Header(default="")
) -> dict[str, object]:
    """Queue depth — the cheap read (Step 2.2 exit criterion) that backs
    the dashboard's queue-size figure (§6).

    `counts` is the fuller lifecycle breakdown for Step 2.7 and is a
    grouped scan, not the cheap path; `depth` is the index range count.
    Both are recomputed from Postgres per call — nothing is cached here,
    so every replica answers identically (§3.9).

    **Phase 3.4 adds `fenced_results`**, and it belongs here rather than on
    an endpoint of its own for the reason `counts` does: this is the read
    the task console already polls, so a fenced result becomes visible
    without a second request and without an operator having to know which
    task to open. It is strictly the cheaper of the two scans this handler
    already performs — `task_attempts` holds one row per abnormal ending
    where `tasks` holds every task there has ever been.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_depth"
    )
    if rejection is not None:
        return rejection

    async with get_session() as session:
        depth = await queue_depth(session)
        counts = await counts_by_status(session)
        fenced = await fenced_result_count(session)

    return {"depth": depth, "counts": counts, "fenced_results": fenced}


@app.get("/tasks/throughput")
async def task_throughput(
    request: Request,
    response: Response,
    minutes: int = Query(default=30, ge=1),
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """Completed tasks per minute over a recent window (Phase 2.7).

    Declared before `/tasks/{task_id}` for the same reason `/tasks/depth`
    is — FastAPI matches in declaration order.

    This is the read behind the dashboard's throughput chart, and it is a
    query over `tasks.completed_at` rather than a counter the dashboard
    accumulates while a browser tab happens to be open. Three consequences,
    all of them the reason for the choice:

    * The chart survives a page reload, and two operators watching see the
      same numbers.
    * A bucket is exactly the rows `GET /tasks?status=COMPLETED` would list
      for that minute, so "the chart matches measured reality" — this
      step's exit criterion — is something an operator can check against
      another endpoint rather than take on trust.
    * The buckets are cut by Postgres's clock, the same one that stamped
      the rows, so a browser in another time zone cannot shift them.

    Empty minutes come back as zero rather than being omitted; see
    `task_queue.completions_per_minute`.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_throughput"
    )
    if rejection is not None:
        return rejection

    try:
        async with get_session() as session:
            series = await completions_per_minute(
                session, minutes=minutes, max_minutes=task_throughput_max_minutes()
            )
    except QueueLimitExceeded as exc:
        response.status_code = 400
        return {"detail": str(exc)}

    return {
        "window_minutes": minutes,
        "series": series,
        "completed_in_window": sum(int(point["completed"]) for point in series),
    }


@app.get("/tasks/policies")
async def list_task_policies(
    request: Request, response: Response, x_admin_secret: str = Header(default="")
) -> dict[str, object]:
    """The timeouts the coordinator will actually apply, per task type.

    Phase 3.1, and the read half of "configurable per task type **without
    redeploy**". Every registered type appears whether or not it has an
    override, because a policy surface that only lists what has been
    changed cannot answer the question an operator actually has, which is
    "what is in force right now".

    Each value carries a `_source` of `default` or `policy`. That is not
    decoration: §10 forbids blurring a recommended value with a configured
    one, and a bare list of numbers does exactly that — 300 looks the same
    whether it came from the code or from someone's change last Tuesday.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_policy_list"
    )
    if rejection is not None:
        return rejection

    async with get_session() as session:
        policies = await effective_policies(session)
    return {"policies": policies, "count": len(policies)}


@app.put("/tasks/policies/{task_type}")
async def upsert_task_policy(
    request: Request,
    task_type: str,
    body: TaskPolicyRequest,
    response: Response,
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """Set one task type's timeout overrides (Phase 3.1).

    **This is the endpoint the sixth exit criterion is about.** The change
    is a row in Postgres, so it applies to the next task claimed by any
    replica — no restart, no rollout, no ConfigMap reload, and no
    invalidation message between replicas, because nothing cached it.

    A partial body updates only the fields it names. To go back to the code
    defaults, `DELETE` the policy rather than guessing at the numbers.

    Bounds are enforced (`app.task_policies`) and a violation is a **400
    naming the field**. An operator setting a lease TTL of 0 is not
    attacking the system, they have made a typo — but the effect would be
    every task reclaimed the instant it was assigned, which is the recovery
    engine denying service to the system it protects.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_policy_set"
    )
    if rejection is not None:
        return rejection

    try:
        values = validate_policy(body.model_dump(exclude_unset=True))
    except InvalidTaskPolicy as exc:
        response.status_code = 400
        logger.warning("task_policy_rejected", extra=_admin_log(detail=str(exc)))
        return {"detail": str(exc)}

    try:
        async with get_session() as session:
            await set_policy(session, task_type=task_type, values=values)
            await session.commit()
            policies = await effective_policies(session)
    except UnknownTaskType as exc:
        response.status_code = 400
        logger.warning("task_policy_rejected", extra=_admin_log(detail=str(exc)))
        return {"detail": str(exc)}

    logger.info(
        "task_policy_updated",
        extra=_admin_log(task_type=task_type, fields=sorted(values)),
    )
    effective = next(entry for entry in policies if entry["task_type"] == task_type)
    return {"task_type": task_type, "applied": values, "effective": effective}


@app.delete("/tasks/policies/{task_type}")
async def delete_task_policy(
    request: Request,
    task_type: str,
    response: Response,
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """Drop a type's overrides so the code defaults apply again (Phase 3.1).

    A delete of a type that has no override is **200, not 404**. The
    operator's intent — "this type should be on the defaults" — is
    satisfied either way, and this is the opposite case from
    `POST /tasks/{id}/cancel`, where the second call is a question whose
    honest answer is "no". Here it is an instruction, and it has been
    carried out.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_policy_clear"
    )
    if rejection is not None:
        return rejection

    try:
        async with get_session() as session:
            removed = await clear_policy(session, task_type=task_type)
            if removed:
                await session.commit()
            policies = await effective_policies(session)
    except UnknownTaskType as exc:
        response.status_code = 400
        logger.warning("task_policy_rejected", extra=_admin_log(detail=str(exc)))
        return {"detail": str(exc)}

    logger.info("task_policy_cleared", extra=_admin_log(task_type=task_type, removed=removed))
    effective = next(entry for entry in policies if entry["task_type"] == task_type)
    return {"task_type": task_type, "removed": removed, "effective": effective}


@app.post("/tasks/dequeue")
async def dequeue_tasks(body: DequeueTaskRequest, response: Response) -> dict[str, object]:
    """Atomically claim queued tasks for a named worker.

    A queue primitive, exposed so the "three coordinator replicas
    dequeuing concurrently never double-assign" criterion can be proven
    against real replicas rather than only against three local processes.
    Step 2.3's assignment engine calls `task_queue.dequeue` directly and
    is what will decide *which* worker to claim for; this endpoint stays
    as the operator/harness entry point.

    An empty `tasks` list is a normal answer, not an error — see
    `task_queue.dequeue`.
    """
    if not verify_admin_secret(body.admin_secret):
        response.status_code = 401
        logger.warning("dequeue_rejected_invalid_admin_secret", extra=_admin_log())
        return {"detail": "invalid admin credential"}

    try:
        worker_uuid = uuid.UUID(body.worker_id)
    except ValueError:
        response.status_code = 400
        return {"detail": "worker_id is not a valid UUID"}

    try:
        async with get_session() as session:
            claimed = await dequeue(
                session,
                worker_id=worker_uuid,
                limit=body.limit,
                max_batch=task_dequeue_max_batch(),
            )
            await session.commit()
    except QueueLimitExceeded as exc:
        response.status_code = 400
        return {"detail": str(exc)}

    for task in claimed:
        logger.info(
            "task_dequeued",
            extra=_admin_log(
                task_id=str(task["id"]),
                worker_id=body.worker_id,
                task_type=task["task_type"],
                correlation_id=task["correlation_id"],
                coordinator_instance=COORDINATOR_INSTANCE_ID,
            ),
        )

    return {
        "count": len(claimed),
        "tasks": [
            {
                "task_id": str(task["id"]),
                "task_type": task["task_type"],
                "parameters": task["parameters"],
                "payload": task["payload"],
                "priority": task["priority"],
                "correlation_id": task["correlation_id"],
                "assigned_at": task["assigned_at"].isoformat(),
                # Phase 3.1: the acknowledgement lease this claim carries.
                # Reported because a caller of the raw queue primitive — the
                # versioned `scripts/queue_harness.py`, and anything else
                # driving it directly — now has a deadline it must beat, and
                # a claim that does not say so would be a trap.
                "lease_expires_at": task["lease_expires_at"].isoformat(),
            }
            for task in claimed
        ],
        "coordinator_instance": COORDINATOR_INSTANCE_ID,
    }


def _lifecycle(task: dict[str, Any]) -> list[dict[str, object]]:
    """The task's state history, oldest first (Phase 2.6).

    Built from the row's own timestamp columns, and every entry names the
    column it came from. That `source` field is the honest part: the
    lifecycle is **reconstructed**, not replayed from an event log, and the
    reconstruction is exact for some states and inferred for others.

    * `QUEUED`, `ASSIGNED`, `RUNNING`, `COMPLETED` each have a dedicated
      column, so those entries are what the coordinator actually recorded.
    * `FAILED` and `CANCELLED` have none. They use `updated_at`, which is
      correct because a terminal state is the last write the row ever
      receives — nothing in M2 updates a task afterwards, and the retention
      sweep touches `task_results`, not `tasks`. Stated rather than assumed,
      because it stops being true the moment something updates a terminal
      task.
    * A task that predates migration 0004 has no `started_at`, so its
      RUNNING entry is **absent rather than fabricated** (§10).

    A per-transition event table would remove the inference, and was
    rejected for M2: it adds a write to the assignment hot path to serve a
    read that four columns already answer. Phase 3, which introduces retries
    and therefore *repeated* transitions per task, is where a single column
    per state stops being enough.
    """
    timeline: list[dict[str, object]] = [
        {"state": "QUEUED", "at": task["created_at"].isoformat(), "source": "created_at"}
    ]
    if task["assigned_at"] is not None:
        timeline.append(
            {"state": "ASSIGNED", "at": task["assigned_at"].isoformat(), "source": "assigned_at"}
        )
    if task["started_at"] is not None:
        timeline.append(
            {"state": "RUNNING", "at": task["started_at"].isoformat(), "source": "started_at"}
        )
    if task["status"] == "COMPLETED" and task["completed_at"] is not None:
        timeline.append(
            {"state": "COMPLETED", "at": task["completed_at"].isoformat(), "source": "completed_at"}
        )
    elif task["status"] in ("FAILED", "CANCELLED"):
        timeline.append(
            {
                "state": task["status"],
                "at": task["updated_at"].isoformat(),
                "source": "updated_at (no dedicated column; terminal states are the last write)",
            }
        )
    return timeline


@app.get("/tasks/{task_id}")
async def inspect_task(
    request: Request,
    task_id: str,
    response: Response,
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """One task: full lifecycle, timestamps, and its persisted result.

    Declared **after** `/tasks/depth` and `/tasks` on purpose: FastAPI
    matches routes in declaration order, so a path parameter registered
    first would swallow the literal paths.

    Built in Step 2.5 as the minimum read that made "execution duration
    recorded and visible" verifiable rather than asserted; Step 2.6 adds the
    `timeline` and `started_at` that make it the operator's inspection
    endpoint — "returns the full lifecycle with timestamps" is this step's
    own exit criterion.

    Two durations come back and they are not the same measurement, which is
    why both are reported rather than one being presented as "the" duration
    (§10 and §12):

    * `duration_seconds` inside `result` is **worker-reported**. It is the
      execution time proper, and it is untrusted.
    * `observed_duration_seconds` is **coordinator-observed**, from
      `assigned_at` to `completed_at`. It includes delivery, queueing inside
      the worker's admission path and the result round trip, so it is always
      the larger of the two, and it is the one that cannot be lied about.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_inspect"
    )
    if rejection is not None:
        return rejection

    async with get_session() as session:
        task = await get_task(session, task_id)
        # Phase 3.2. Read in the same session, and only for a single task —
        # this is the inspection endpoint, not the listing (§3.0.7's reason
        # for not putting attempt rows on the hot read path).
        attempts = await task_attempts(session, task_id=task_id) if task else []

    if task is None:
        response.status_code = 404
        return {"detail": "task not found"}

    return {
        **_task_summary(task),
        "timeline": _lifecycle(task),
        # **What makes a FAILED task inspectable rather than merely
        # visible** (Step 3.2 exit criterion, and gate §3.0.4 item 2): one
        # row per attempt that ended abnormally, naming the worker that
        # held it and which clock ran out. A task that has never been
        # reclaimed has an empty list here, not a missing field.
        "attempts": [
            {
                "attempt_number": attempt["attempt_number"],
                "worker_id": str(attempt["worker_id"]) if attempt["worker_id"] else None,
                "outcome": attempt["outcome"],
                "reason": attempt["reason"],
                "recorded_at": attempt["recorded_at"].isoformat(),
                "correlation_id": attempt["correlation_id"],
            }
            for attempt in attempts
        ],
        # None once the retention sweep has removed the body. The task row
        # survives as the audit trail — that is the documented behaviour of
        # `RESULT_RETENTION_DAYS`, not a lost result.
        "result": task["result_payload"],
        "result_size_bytes": task["result_size_bytes"],
        "result_submitted_at": (
            task["result_submitted_at"].isoformat() if task["result_submitted_at"] else None
        ),
    }


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    request: Request,
    task_id: str,
    response: Response,
    x_admin_secret: str = Header(default=""),
) -> dict[str, object]:
    """Cancel a task that is still queued (Phase 2.6).

    **Only a `QUEUED` task can be cancelled in M2**, and the refusal for
    anything else is deliberate rather than a gap left open — see
    `task_queue.cancel_queued_task` for what cancelling in-flight work would
    actually require. An in-flight or already-terminal task comes back
    **409 with its current status**, so the operator learns why rather than
    guessing.

    A second cancel of the same task is also 409 (`CANCELLED`), not a
    silent 200. That is a considered choice against the idempotent reading:
    §3.7's idempotency requirement is about a *worker* retrying a result it
    cannot know landed, whereas an operator repeating a cancel is asking a
    question — "did this call cancel it?" — and the honest answer to the
    second one is no. The state is identical either way; only the report
    differs.

    Takes no body. The credential is the `X-Admin-Secret` header, which is
    also why there is nothing here for a validation error to echo.
    """
    rejection = await _operator_guard(
        request, response, secret=x_admin_secret, event="task_cancel"
    )
    if rejection is not None:
        return rejection

    async with get_session() as session:
        outcome, current = await cancel_queued_task(session, task_id=task_id)
        # Commit only when something was written. A refused cancel must not
        # leave a transaction to be rolled back at an arbitrary later point.
        if outcome == TRANSITIONED:
            await session.commit()

    TASKS_CANCELLED.labels(outcome).inc()

    if outcome == NOT_FOUND:
        response.status_code = 404
        return {"detail": "task not found"}

    if outcome == NOT_CANCELLABLE:
        response.status_code = 409
        logger.info("task_cancel_refused", extra=_admin_log(task_id=task_id, status=current))
        return {
            "task_id": task_id,
            "status": current,
            "detail": (
                f"task is {current}: only a QUEUED task can be cancelled "
                "(cancelling in-flight work is not in M2 scope)"
            ),
        }

    logger.info("task_cancelled", extra=_admin_log(task_id=task_id, previous_status=current))
    return {"task_id": task_id, "status": "CANCELLED", "previous_status": current}
