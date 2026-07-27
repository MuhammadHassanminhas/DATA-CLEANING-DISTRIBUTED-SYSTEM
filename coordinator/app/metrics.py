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

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.db import get_session
from app.models import Worker
from app.redis_client import redis_client

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
    await _refresh_fleet_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
