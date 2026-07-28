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

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Header, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import select, text

from app.config import (
    access_token_ttl_seconds,
    admin_secret_is_separate,
    heartbeat_offline_threshold_seconds,
    heartbeat_suspect_threshold_seconds,
    heartbeat_sweep_interval_seconds,
    register_rate_limit_per_minute,
    run_migrations_on_startup,
    task_dequeue_max_batch,
    task_enqueue_max_batch,
    worker_claim_ttl_seconds,
    ws_ping_interval_seconds,
    ws_pong_timeout_seconds,
    ws_registry_ttl_seconds,
)
from app.db import engine, get_session
from app.logging_config import configure_logging
from app.metrics import MetricsMiddleware, metrics_endpoint
from app.middleware import CorrelationIDMiddleware, client_ip_var, correlation_id_var
from app.models import Worker
from app.redis_client import redis_client
from app.schemas import (
    DequeueTaskRequest,
    EnqueueTaskRequest,
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
    QueueLimitExceeded,
    counts_by_status,
    dequeue,
    enqueue_batch,
    queue_depth,
)
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
    yield
    # No app-level connection drain here: uvicorn's own `Server.shutdown()`
    # calls `connection.shutdown()` on every live WebSocket (a proper
    # close, code 1012 "service restart") and waits for each connection's
    # handler task to finish *before* this lifespan shutdown code runs —
    # verified live (Decisions Log #21). By this point every `ws_connect`
    # handler has already run its own `finally` cleanup below, so there is
    # nothing left here to drain.
    sweep_task.cancel()
    try:
        await sweep_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()
    await redis_client.aclose()
    logger.info("coordinator stopped")


app = FastAPI(title="Coordinator", version="0.2.0", lifespan=lifespan)
app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(MetricsMiddleware)

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


async def _rate_limited(source_ip: str) -> bool:
    """Fixed-window counter per source IP. See `register_rate_limit_per_minute`
    docstring — the limit itself is a recommendation, not a measured value."""
    key = f"register:ratelimit:{source_ip}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    return count > register_rate_limit_per_minute()


@app.post("/workers/register", status_code=201)
async def register_worker(request: Request, body: RegisterRequest, response: Response) -> dict[str, object]:
    """First-contact enrollment, and reaffirmation on every subsequent
    worker startup (the worker CLI reuses its persisted identity rather
    than re-enrolling). Both paths share one endpoint; which one runs is
    decided by whether the caller presents a fresh enrollment secret or
    an already-issued worker_id/worker_credential pair.
    """
    source_ip = request.client.host if request.client else "unknown"

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

        item: dict[str, object] = {
            "worker_id": str(worker.id),
            "status": worker.status,
            "revoked": worker.revoked,
            "agent_version": worker.agent_version,
            "created_at": worker.created_at.isoformat(),
            "connected": registry is not None,
            "session_epoch": registry.get("session_epoch") if registry else None,
        }
        for field in _WORKER_METRICS_FIELDS:
            item[field] = metrics.get(field)
        items.append(item)

    return {"workers": items, "server_time": datetime.now(timezone.utc).isoformat()}


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

            logger.info("worker_message_received", extra={"worker_id": worker_id, "message_type": message_type})
    except WebSocketDisconnect:
        logger.info("worker_disconnected", extra={"worker_id": worker_id})
    finally:
        ping_task.cancel()
        push_task.cancel()
        control_task.cancel()
        current = await redis_client.get(registry_key)
        if current is not None and json.loads(current)["session_epoch"] == session_epoch:
            await redis_client.delete(registry_key)
            await _transition_status(worker_uuid, "OFFLINE", "ws_disconnected")


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
# Task queue (Phase 2.2)
#
# Deliberately minimal: enqueue, depth, and the dequeue primitive. That is
# what Step 2.2's own exit criteria need to be demonstrable, and no more.
# The full operator surface — list, inspect, cancel, filter — is Step 2.6,
# and the logic that *decides* which worker gets a task is Step 2.3.
#
# Guarded by the operator credential, same as /workers and revoke. Since
# Step 2.2.1 that is `ADMIN_SECRET` rather than the shared enrollment
# secret — these endpoints are exactly why that split had to happen, since
# otherwise any worker could drain the queue (see `config.admin_secret`).
# --------------------------------------------------------------------------


@app.post("/tasks", status_code=201)
async def enqueue_tasks(body: EnqueueTaskRequest, response: Response) -> dict[str, object]:
    """Create one or more queued tasks.

    Every task carries the request's correlation ID from creation (§11 and
    a Phase 2.1 exit criterion), so a task is traceable back to the call
    that made it, and a bulk enqueue shares one ID across the batch.
    """
    if not verify_admin_secret(body.admin_secret):
        response.status_code = 401
        logger.warning("enqueue_rejected_invalid_admin_secret", extra=_admin_log())
        return {"detail": "invalid admin credential"}

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
    # Ids are returned only for a single-task create. Echoing 10,000 UUIDs
    # back would make the response bigger than the request that caused it,
    # for no caller that needs them — a bulk enqueue is a load operation.
    return {
        "status": "queued",
        "count": len(task_ids),
        "task_ids": [str(task_id) for task_id in task_ids] if len(task_ids) == 1 else None,
        "correlation_id": correlation_id,
    }


@app.get("/tasks/depth")
async def task_queue_depth(response: Response, x_admin_secret: str = Header(default="")) -> dict[str, object]:
    """Queue depth — the cheap read (Step 2.2 exit criterion) that backs
    the dashboard's queue-size figure (§6).

    `counts` is the fuller lifecycle breakdown for Step 2.7 and is a
    grouped scan, not the cheap path; `depth` is the index range count.
    Both are recomputed from Postgres per call — nothing is cached here,
    so every replica answers identically (§3.9).
    """
    if not verify_admin_secret(x_admin_secret):
        response.status_code = 401
        logger.warning("task_depth_rejected_invalid_admin_secret", extra=_admin_log())
        return {"detail": "invalid admin credential"}

    async with get_session() as session:
        depth = await queue_depth(session)
        counts = await counts_by_status(session)

    return {"depth": depth, "counts": counts}


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
            }
            for task in claimed
        ],
        "coordinator_instance": COORDINATOR_INSTANCE_ID,
    }
