"""Prometheus metrics for Step 1.5.6 (observability).

Exposes `GET /metrics` in the standard Prometheus text format. Three
sources of truth, matching CLAUDE.md §3.9 (no authoritative state in
process memory):

  - Process CPU/memory come free from prometheus_client's default
    collectors (`process_*`), so "coordinator CPU and memory" needs no
    custom code.
  - Fleet gauges (workers by status, connected count) and dependency
    health are recomputed from Postgres/Redis on every scrape rather
    than cached here — any replica reports the same truth. Every
    coordinator pod therefore exports identical fleet series; dashboards
    and alerts use `max by (...)` to collapse them (documented in the
    Grafana dashboard / alert rules, not assumed away).
  - Request latency is a per-instance histogram fed by MetricsMiddleware,
    keyed by the route *template* (not the concrete path) to keep
    cardinality bounded.

Auth-failure spikes are deliberately NOT a counter here — they are
derived in Grafana from the structured `*_rejected` log events already
emitted (§11), via Loki. One less thing to instrument at every call site.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import admin_secret_is_separate
from app.db import get_session
from app.models import Worker
from app.redis_client import redis_client
from app.task_queue import queue_depth

logger = logging.getLogger("coordinator")

# Fleet state (identical across replicas — collapse with max in queries).
WORKERS_BY_STATUS = Gauge(
    "coordinator_workers_total", "Registered workers by status.", ["status"]
)
WORKERS_CONNECTED = Gauge(
    "coordinator_workers_connected", "Workers with a live connection (Redis registry)."
)
DEPENDENCY_UP = Gauge(
    "coordinator_dependency_up", "External dependency reachable (1) or not (0).", ["dependency"]
)
# Phase 2.2. Same shape as the fleet gauges above: recomputed from
# Postgres per scrape, identical across replicas, collapse with `max by`.
# `QUEUED` here is the cheap index range count `task_queue.queue_depth`
# performs; the per-status breakdown is a grouped scan and stays off the
# scrape path until Step 2.7 needs it.
TASKS_QUEUED = Gauge("coordinator_tasks_queued", "Tasks waiting in the queue.")

# Step 2.2.1 security posture. 1 = the operator credential is distinct
# from the shared worker enrollment secret; 0 = the fallback is active and
# every worker can call the admin endpoints. Exposed as a metric so the
# posture is alertable and visible per replica, rather than only knowable
# by reading a startup log line. Reveals whether the two secrets differ,
# never either value (§12).
ADMIN_CREDENTIAL_SEPARATE = Gauge(
    "coordinator_admin_credential_separate",
    "1 when ADMIN_SECRET is set and differs from ENROLLMENT_SECRET, else 0.",
)

# Phase 2.3 assignment engine. Unlike the fleet gauges above these are
# genuinely **per-instance**, not identical across replicas, and that is
# correct rather than a §3.9 violation: a replica assigns only to the
# worker sockets it holds, so "assignments made" is a property of this
# process. Sum across instances in a query, do not `max`.
TASKS_ASSIGNED = Counter(
    "coordinator_tasks_assigned_total",
    "Tasks delivered to a worker by this coordinator instance.",
)
TASK_ACKS = Counter(
    "coordinator_task_acks_total",
    "Task assignment acknowledgements received, by outcome.",
    ["outcome"],
)
# The idle-cost series. With an empty queue this is the *only* thing the
# assignment engine does, and its rate is what makes the "100 idle workers
# produce negligible load" exit criterion measurable rather than asserted:
# the pass rate is driven by the safety-net poll interval, and does not
# grow with the number of connected workers.
ASSIGNMENT_PASSES = Counter(
    "coordinator_assignment_passes_total",
    "Assignment passes run by this coordinator instance.",
)
ASSIGNMENT_QUERIES = Counter(
    "coordinator_assignment_dequeue_queries_total",
    "Dequeue statements issued by the assignment engine on this instance.",
)
ASSIGNMENTS_IN_FLIGHT = Gauge(
    "coordinator_assignments_in_flight",
    "Tasks assigned by this instance and not yet acknowledged or released.",
)

# Phase 2.4 execution. Per-instance for the same reason as the series above:
# a task is started, progressed and failed against the socket this replica
# holds. `started` minus `failed` is not "completed" — Step 2.5 owns
# completion, and in M2 a task that finishes successfully stays RUNNING.
TASKS_STARTED = Counter(
    "coordinator_tasks_started_total",
    "Tasks moved ASSIGNED -> RUNNING on a worker's task_started report.",
)
TASKS_FAILED = Counter(
    "coordinator_tasks_failed_total",
    "Tasks moved to FAILED after a worker's executor raised.",
)
TASK_PROGRESS_REPORTS = Counter(
    "coordinator_task_progress_reports_total",
    "Progress samples received, by outcome.",
    ["outcome"],
)

# Phase 2.5 result submission. `outcome` carries the full decision — a
# result is completed, a duplicate, rejected as malformed, or refused
# because the task is not this worker's — so a rejection spike is visible
# without reading logs, which is what makes the malformed-input criterion
# alertable rather than only auditable.
TASK_RESULTS = Counter(
    "coordinator_task_results_total",
    "Result submissions received, by outcome.",
    ["outcome"],
)
TASKS_COMPLETED = Counter(
    "coordinator_tasks_completed_total",
    "Tasks moved to COMPLETED with a persisted result.",
)
# Buckets straddle the 128 KB cap so both the ordinary small result and the
# full-size `opaque_payload` echo (~87 KB base64) land in their own bucket,
# and anything truncated is visible in the overflow.
RESULT_SIZE_BYTES = Histogram(
    "coordinator_task_result_size_bytes",
    "Persisted result envelope size.",
    buckets=(256, 1024, 4096, 16384, 65536, 131072, 524288),
)
RESULTS_PURGED = Counter(
    "coordinator_task_results_purged_total",
    "Result bodies deleted by the retention sweep.",
)

# Phase 2.6 operator API. `outcome` carries the whole decision so a refusal
# is visible without reading logs — "cancelled" means a queued task left the
# queue, "not_cancellable" means an operator tried to cancel work already in
# flight, which is the case the M2 scope limit produces (see
# `task_queue.cancel_queued_task`).
TASKS_CANCELLED = Counter(
    "coordinator_task_cancellations_total",
    "Task cancellation requests, by outcome.",
    ["outcome"],
)
# Rejections by the coordinator's own limiter, not the ingress's. Counted
# rather than derived from logs because this is the one control that can
# make a *correct* operator call fail, so it has to be trivially visible
# when it starts firing.
TASK_API_RATE_LIMITED = Counter(
    "coordinator_task_api_rate_limited_total",
    "Operator task API requests rejected by the per-source-IP rate limit.",
)

# Per-instance request latency. Route template keeps label cardinality bounded.
REQUEST_LATENCY = Histogram(
    "coordinator_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route", "status"],
)

_ALL_STATUSES = ("CONNECTING", "ONLINE", "SUSPECT", "OFFLINE", "QUARANTINED")


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        # Only record for matched routes; unmatched paths (404s, scanners)
        # would otherwise blow up label cardinality.
        template = getattr(route, "path", None)
        if template is not None and template != "/metrics":
            REQUEST_LATENCY.labels(request.method, template, str(response.status_code)).observe(
                time.perf_counter() - start
            )
        return response


async def _refresh_fleet_gauges() -> None:
    """Recompute the shared-state gauges from Postgres/Redis. Called on
    each scrape so the numbers are as fresh as the last request, never
    older cached state."""
    counts: dict[str, int] = {status: 0 for status in _ALL_STATUSES}
    try:
        async with get_session() as session:
            result = await session.execute(select(Worker.status, func.count()).group_by(Worker.status))
            for status, count in result.all():
                counts[status] = count
            TASKS_QUEUED.set(await queue_depth(session))
        DEPENDENCY_UP.labels("database").set(1)
    except Exception as exc:  # noqa: BLE001 — a scrape must not raise
        logger.warning("metrics_db_unavailable", extra={"detail": str(exc)})
        DEPENDENCY_UP.labels("database").set(0)
    for status, count in counts.items():
        WORKERS_BY_STATUS.labels(status).set(count)

    try:
        connected = 0
        async for _ in redis_client.scan_iter(match="worker:*:connection"):
            connected += 1
        WORKERS_CONNECTED.set(connected)
        DEPENDENCY_UP.labels("redis").set(1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("metrics_redis_unavailable", extra={"detail": str(exc)})
        DEPENDENCY_UP.labels("redis").set(0)


async def metrics_endpoint() -> Response:
    # Re-read per scrape rather than latch at import, so a Secret rollout
    # is reflected without needing a restart to notice it.
    ADMIN_CREDENTIAL_SEPARATE.set(1 if admin_secret_is_separate() else 0)
    await _refresh_fleet_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
