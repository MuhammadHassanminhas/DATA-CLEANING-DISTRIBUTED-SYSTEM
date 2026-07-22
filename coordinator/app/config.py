"""Environment-derived configuration. No hardcoded hosts or ports —
every value here comes from an environment variable set in
docker-compose.yml / .env, per CLAUDE.md's configuration rule.
"""

from __future__ import annotations

import os


def database_url() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    db = os.environ["POSTGRES_DB"]
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


def redis_url() -> str:
    host = os.environ["REDIS_HOST"]
    port = os.environ["REDIS_PORT"]
    return f"redis://{host}:{port}/0"


def enrollment_secret() -> str:
    return os.environ["ENROLLMENT_SECRET"]


def credential_pepper() -> str:
    return os.environ["CREDENTIAL_PEPPER"]


def register_rate_limit_per_minute() -> int:
    """Recommendation, not a measured value — no load test has run yet."""
    return int(os.environ.get("REGISTER_RATE_LIMIT_PER_MINUTE", "5"))


def access_token_ttl_seconds() -> int:
    """Recommendation, not a measured value — short-lived on purpose per
    CLAUDE.md §12; deliberately short enough that a revoked worker's
    still-outstanding token dies quickly even before Phase 1.5 gives the
    coordinator a live connection it can actively drop."""
    return int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "60"))


def enrollment_admin_secret() -> str:
    """Reuses ENROLLMENT_SECRET as a stand-in admin credential for the
    revoke endpoint. No real operator/admin auth model exists yet — that's
    an undesigned, out-of-scope feature, not invented here. Revisit when
    one is."""
    return enrollment_secret()


def ws_ping_interval_seconds() -> int:
    """Recommendation, not a measured value. How often the coordinator
    sends an application-level `ping` envelope over a live connection —
    proves the socket is alive and is also what refreshes the Redis
    connection-registry TTL (see `ws_registry_ttl_seconds`)."""
    return int(os.environ.get("WS_PING_INTERVAL_SECONDS", "20"))


def ws_pong_timeout_seconds() -> int:
    """Recommendation, not a measured value. If the coordinator's read
    loop for a connection sees nothing at all (not just no `pong`) for
    this long, the connection is treated as dead and closed. Must be
    larger than `ws_ping_interval_seconds` so one ping/pong round trip
    fits comfortably inside it."""
    return int(os.environ.get("WS_PONG_TIMEOUT_SECONDS", "45"))


def ws_registry_ttl_seconds() -> int:
    """Recommendation, not a measured value. TTL on the
    `worker:{id}:connection` Redis registry key, refreshed on every
    `pong`. Bounds how long a stale entry can survive a coordinator
    process that dies ungracefully (SIGKILL) and skips the normal
    disconnect cleanup."""
    return int(os.environ.get("WS_REGISTRY_TTL_SECONDS", "90"))


def heartbeat_suspect_threshold_seconds() -> int:
    """Recommendation, not a measured value. Coordinator-side: seconds
    since the last coordinator-observed heartbeat (not the worker's own
    embedded timestamp — Decisions Log #6) before a worker is marked
    SUSPECT."""
    return int(os.environ.get("HEARTBEAT_SUSPECT_THRESHOLD_SECONDS", "12"))


def heartbeat_offline_threshold_seconds() -> int:
    """Recommendation, not a measured value. Coordinator-side: seconds
    since the last coordinator-observed heartbeat before a worker is
    marked OFFLINE. Must be greater than
    `heartbeat_suspect_threshold_seconds`."""
    return int(os.environ.get("HEARTBEAT_OFFLINE_THRESHOLD_SECONDS", "25"))


def heartbeat_sweep_interval_seconds() -> int:
    """Recommendation, not a measured value. How often the coordinator's
    background liveness sweep re-checks every non-terminal worker's
    last-heartbeat freshness against the two thresholds above."""
    return int(os.environ.get("HEARTBEAT_SWEEP_INTERVAL_SECONDS", "5"))


def worker_claim_ttl_seconds() -> int:
    """Recommendation, not a measured value.

    Bounds how long a registration/reaffirm claim blocks a second
    concurrent claim for the same worker ID. Deliberately short: this is
    a duplicate-use heuristic, not the real session-conflict resolution
    that lands in Phase 1.7 once Phase 1.5 gives the coordinator an
    actual live connection to arbitrate between.
    """
    return int(os.environ.get("WORKER_CLAIM_TTL_SECONDS", "10"))
