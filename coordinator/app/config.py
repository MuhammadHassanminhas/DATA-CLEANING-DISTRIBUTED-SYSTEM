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


def run_migrations_on_startup() -> bool:
    """Whether the coordinator runs `alembic upgrade head` from its own
    FastAPI lifespan on boot. True by default (Docker Compose dev relies
    on it — Decisions Log #8). Kubernetes sets this false and runs
    migrations once, before the container serves, via an initContainer
    (Decision #55) — so N horizontally-scaled replicas never race an
    upgrade on cold start."""
    return os.environ.get("RUN_MIGRATIONS_ON_STARTUP", "true").lower() != "false"


def register_rate_limit_per_minute() -> int:
    """Recommendation, not a measured value — no load test has run yet."""
    return int(os.environ.get("REGISTER_RATE_LIMIT_PER_MINUTE", "5"))


def access_token_ttl_seconds() -> int:
    """Recommendation, not a measured value — short-lived on purpose per
    CLAUDE.md §12; deliberately short enough that a revoked worker's
    still-outstanding token dies quickly even before Phase 1.5 gives the
    coordinator a live connection it can actively drop."""
    return int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "60"))


def admin_secret() -> str:
    """The operator credential for the admin endpoints.

    Step 2.2.1. Until now this returned `enrollment_secret()`, which meant
    **every worker in the fleet held a credential that could drive every
    admin endpoint** — list the fleet, revoke or push to any peer, and,
    once Step 2.2 landed, drain the entire task queue and self-assign all
    of it. `ENROLLMENT_SECRET` is by design a *shared* bootstrap secret
    handed to every worker (Decision #76 B1), so that made operator
    authority fleet-wide. CLAUDE.md §12 says every worker is untrusted;
    this is what makes that true of the admin surface.

    **Falls back to `ENROLLMENT_SECRET` when `ADMIN_SECRET` is unset**, and
    that is a deliberate rollout affordance, not an oversight: the
    coordinator image ships before the new Secret is applied to a cluster,
    and a hard requirement would CrashLoop every replica in the gap. The
    fallback is announced loudly at startup (`admin_secret_fallback_in_use`,
    WARNING) so a deployment still running on it is visible in the logs
    rather than silently insecure. `admin_secret_is_separate()` is what a
    test or a live check asserts against.
    """
    return os.environ.get("ADMIN_SECRET") or enrollment_secret()


def admin_secret_is_separate() -> bool:
    """True when the operator credential is genuinely distinct from the
    shared worker enrollment secret.

    False means the fallback above is active — the deployment is still in
    the pre-2.2.1 posture where any worker can call the admin endpoints.
    """
    configured = os.environ.get("ADMIN_SECRET")
    return bool(configured) and configured != enrollment_secret()


def task_enqueue_max_batch() -> int:
    """Recommendation, not a measured value (Phase 2.2). Caps how many
    tasks one `POST /tasks` call may create, so a single request cannot
    hold a transaction open unboundedly. Set at 10,000 because that is
    exactly the figure Step 2.2's own exit criterion names — the criterion
    must be reachable in one call, since the public ingress rate-limits
    requests per second (Step 1.5.5). Revise against Step 2.8's harness."""
    return int(os.environ.get("TASK_ENQUEUE_MAX_BATCH", "10000"))


def task_dequeue_max_batch() -> int:
    """Recommendation, not a measured value (Phase 2.2). Caps how many
    tasks one dequeue may claim at once. Small on purpose: a large claim
    locks that many rows for the length of the transaction, which is the
    one way `SKIP LOCKED` concurrency can be made to behave badly."""
    return int(os.environ.get("TASK_DEQUEUE_MAX_BATCH", "100"))


def task_api_rate_limit_per_minute() -> int:
    """Recommendation, not a measured value (Phase 2.6). Requests per
    minute per source IP across the operator task API.

    **This is the second layer, not the only one.** ingress-nginx already
    rate-limits per source IP at the edge (`limitRps`, Step 1.5.5). This one
    exists because the edge is not in the path for a Docker Compose run, an
    in-cluster caller, or a port-forward — so without it, "the operator API
    is rate limited" would be a property of one deployment topology rather
    than of the coordinator.

    Set well above what a human operator generates because a *program* is
    the realistic caller: Step 2.7's dashboard will proxy these endpoints
    from a single pod, so the whole dashboard shares one bucket — the same
    trap the registration limiter fell into before `_caller_ip` was fixed
    (see `middleware._caller_ip`). 300/minute leaves room for a 2-second
    poll plus operators on the same address. Revise against Step 2.8's
    harness.

    **`POST /tasks/dequeue` is deliberately exempt**, and is the only task
    endpoint that is. It is a queue primitive rather than part of the
    operator surface, and `scripts/queue_harness.py` — the versioned harness
    that proves three replicas never double-assign — drives roughly a
    thousand claim calls as fast as it can. Limiting it would break a
    reproducible verification for no security gain: it is admin-
    authenticated already, and the edge limit still applies on the public
    path.
    """
    return int(os.environ.get("TASK_API_RATE_LIMIT_PER_MINUTE", "300"))


def task_list_max_limit() -> int:
    """Recommendation, not a measured value (Phase 2.6). Hard ceiling on
    how many tasks one `GET /tasks` page may return.

    Bounds the response an operator can ask the coordinator to build, in
    the same spirit as the enqueue and dequeue caps. 200 rows of task
    metadata is a response of a few hundred kilobytes; result bodies are
    never joined into a listing, which is what keeps that true (see
    `task_queue.list_tasks`).
    """
    return int(os.environ.get("TASK_LIST_MAX_LIMIT", "200"))


def db_pool_size() -> int:
    """Connections this replica keeps open to Postgres (Phase 2.7).

    **Measured, not recommended** — the default of 5 was observed to be the
    limiting factor, and `app/db.py` records the measurement. Raised to 15
    so a fleet completing tasks cannot starve an operator read.

    It is a per-replica figure and Postgres's `max_connections` is shared,
    so read it together with `db_max_overflow` and the replica count.
    """
    return int(os.environ.get("DB_POOL_SIZE", "15"))


def db_max_overflow() -> int:
    """Extra connections allowed above `db_pool_size` under burst.

    Deliberately smaller than SQLAlchemy's default 10, because the pool it
    tops up is three times larger: the pair is what bounds one replica's
    share of `max_connections`, and a burst allowance that dwarfs the
    steady-state pool makes that ceiling hard to reason about.
    """
    return int(os.environ.get("DB_MAX_OVERFLOW", "5"))


def task_throughput_max_minutes() -> int:
    """Recommendation, not a measured value (Phase 2.7). Widest window
    `GET /tasks/throughput` will chart, in minutes.

    The response is one entry per minute in the window including the empty
    ones, so the window is also the response size: 1,440 is a day of
    per-minute buckets, a few tens of kilobytes, and the point past which
    a caller wanting a longer view wants a coarser bucket rather than a
    bigger list. The dashboard asks for 30.

    It also bounds the range scan the query performs, which is the reason
    a cap exists at all rather than only a default — an uncapped window is
    a full-table aggregate wearing a filter.
    """
    return int(os.environ.get("TASK_THROUGHPUT_MAX_MINUTES", "1440"))


def task_result_max_bytes() -> int:
    """Ceiling on one persisted result body, in bytes (Phase 2.5).

    **Recommendation, not a measured value** — but the number is derived
    rather than picked, and the derivation matters because Decision #81's
    original 64 KB was wrong by arithmetic:

    `opaque_payload` accepts up to `MAX_OPAQUE_PAYLOAD_BYTES` (64 KB) of
    *decoded* bytes and the executor returns them **base64-encoded**, which
    is 4/3 the size — 87,384 bytes for a full-size input. A 64 KB result cap
    would therefore truncate the largest *legal* task's result, so
    `task_types.py`'s claim that a worker echoing its input "should not be
    able to exceed the result cap by construction" compared decoded input to
    encoded output. 128 KB clears 87 KB with room for the envelope's other
    fields (Decision #113).

    Enforced on both sides: the worker caps its own envelope before it
    reaches the wire, and the coordinator caps again on receipt because
    every worker is untrusted (§12). Over-cap is **truncated, not rejected**
    — the task still completes, because an oversize result is a fact about
    the payload, not a malformed message.
    """
    return int(os.environ.get("TASK_RESULT_MAX_BYTES", str(128 * 1024)))


def result_retention_days() -> int:
    """How long a result **body** is kept (Phase 2.5, Decision #81).

    **Recommendation, not a measured value.** The `tasks` row survives
    forever as the audit trail; only the body in `task_results` expires, and
    `tasks.result_id` is `ON DELETE SET NULL` so the reference stays
    consistent when it does. Set to 0 to disable the sweep entirely.
    """
    return int(os.environ.get("RESULT_RETENTION_DAYS", "7"))


def result_retention_sweep_interval_seconds() -> int:
    """How often the retention sweep runs. Recommendation, not measured.

    Hourly rather than daily so the deleted volume per pass stays small and
    predictable, and so a misconfiguration is visible within an hour instead
    of a day. Every replica runs its own sweep; the DELETE is idempotent, so
    concurrent sweeps are harmless and no leader election is needed (§3.9).
    """
    return int(os.environ.get("RESULT_RETENTION_SWEEP_INTERVAL_SECONDS", "3600"))


def assignment_poll_interval_seconds() -> int:
    """Recommendation, not a measured value (Phase 2.3). The assignment
    loop is **event-driven** — an enqueue publishes to the
    `tasks:available` channel and every replica's loop wakes immediately.
    This interval is only the safety net for a notification that was never
    delivered, which Redis pub/sub permits by design (fire-and-forget, no
    delivery guarantee: a message published while a replica's subscriber
    was reconnecting is simply gone).

    It is deliberately slow. A missed notification costs at most this much
    latency on an already-queued task; polling faster would buy little and
    is exactly the cost the "100 idle workers produce negligible load"
    exit criterion is about. Note the poll is **per replica, not per
    worker** — its cost does not grow with fleet size.
    """
    return int(os.environ.get("ASSIGNMENT_POLL_INTERVAL_SECONDS", "30"))


def worker_default_max_concurrent() -> int:
    """Recommendation, not a measured value (Phase 2.3). Credits assumed
    for a worker that connects without declaring `max_concurrent` in its
    `hello` — every worker built before Step 2.3 existed."""
    return int(os.environ.get("WORKER_DEFAULT_MAX_CONCURRENT", "4"))


def worker_max_concurrent_ceiling() -> int:
    """Recommendation, not a measured value (Phase 2.3). Hard ceiling on
    the credits a worker may claim.

    `max_concurrent` is **worker-reported**, and CLAUDE.md §12 says every
    worker is untrusted — a worker claiming a billion credits would
    otherwise drain the queue to itself in one pass. This bounds the lie.
    It does not make the figure trustworthy; coordinator-observed
    capability is Phase 4's problem (§12, "anything the scheduler trusts
    heavily must be coordinator-observed").
    """
    return int(os.environ.get("WORKER_MAX_CONCURRENT_CEILING", "64"))


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


def task_ack_timeout_seconds() -> int:
    """How long a delivered task may sit unacknowledged before its lease
    expires (Phase 3.1, gate default 30).

    **Recommendation, not a measured value** — but derived rather than
    picked: it is roughly 3x the *measured* worst heartbeat gap under
    saturation (10.52s, Step 2.9), so a worker that is merely busy has
    room to answer before the coordinator gives up on the delivery.

    This is the first of the three distinct timeouts the step's own exit
    criterion names. It covers **delivery**: the window between
    `QUEUED -> ASSIGNED` and any coordinator-observed message about that
    task. Once the worker reports `task_started`, `task_lease_ttl_seconds`
    takes over.

    Per-type overrides live in the `task_policies` table and win over this
    default without a redeploy (see `app.task_policies`).
    """
    return int(os.environ.get("TASK_ACK_TIMEOUT_SECONDS", "30"))


def task_lease_ttl_seconds() -> int:
    """How far into the future a renewal pushes a task's lease (Phase 3.1,
    gate default 60).

    **Recommendation, not a measured value.** It is six worker progress
    intervals — the cadence *is* measured at 10s (`worker/worker.py`) — so
    five consecutive dropped progress messages still do not cost a healthy
    task its lease.

    It is also the third of the three timeouts, and the gate is explicit
    that it does double duty rather than getting its own column: the
    **result-submission** window is the lease. A worker that finishes
    executing and then never submits stops sending anything about that
    task, so its lease runs out one TTL after its last observed message.
    A fourth timeout would have been a second name for the same clock.
    """
    return int(os.environ.get("TASK_LEASE_TTL_SECONDS", "60"))


def task_lease_renew_fraction() -> float:
    """Renew a lease only once less than this fraction of it remains
    (Phase 3.1, gate default 0.5).

    **This is the whole cost-control story for lease renewal.** Every
    observed message about a running task is a renewal opportunity, and
    the worker sends progress every 10s; writing on each one would put ~10
    UPDATEs per second per 100 running tasks onto the `tasks` table, which
    under Decision #79 *is* the queue's hot path. At half of a 60s TTL the
    same fleet writes roughly one UPDATE per task per 30s.

    The check is made against this replica's own monotonic clock from the
    session that holds the socket, so a message that is not yet due costs
    **no database round trip at all**, not merely no write.
    """
    return float(os.environ.get("TASK_LEASE_RENEW_FRACTION", "0.5"))


def task_max_execution_seconds_default() -> int:
    """Fallback execution cap for a task type with no per-type default
    (Phase 3.1). Recommendation, not a measured value.

    The per-type defaults live in `app.task_types.DEFAULT_MAX_EXECUTION_SECONDS`
    because an execution cap is a property of the workload, not of the
    deployment. This exists so a type added later without one is still
    bounded rather than unbounded.
    """
    return int(os.environ.get("TASK_MAX_EXECUTION_SECONDS", "300"))


def lease_reclaim_interval_seconds() -> int:
    """How often each replica runs the lease reclaimer (Phase 3.1, gate
    default 5). Recommendation, not a measured value — it matches the
    shipped heartbeat sweep cadence, which is the other loop of this shape.

    It is one half of the detection bound the step has to verify: a socket
    close is followed by reassignment within
    `LEASE_DISCONNECT_GRACE_SECONDS + this`, and a silent worker within
    `TASK_LEASE_TTL_SECONDS + this`.
    """
    return int(os.environ.get("LEASE_RECLAIM_INTERVAL_SECONDS", "5"))


def lease_reclaim_batch() -> int:
    """Most tasks one reclaim pass may take. Recommendation, not measured.

    Matches `TASK_DEQUEUE_MAX_BATCH` for the same reason that one exists:
    the pass holds a row lock per task for the length of its transaction,
    and an unbounded reclaim after a large fleet outage would hold
    thousands. The loop simply runs again on the next tick.
    """
    return int(os.environ.get("LEASE_RECLAIM_BATCH", "100"))


def lease_disconnect_grace_seconds() -> int:
    """How long a task's lease survives its worker's socket closing
    (Phase 3.1, gate default 30). Recommendation, not a measured value.

    Deliberately **longer than the worker's reconnect backoff**, so a
    worker that drops and comes straight back renews its own leases on
    `hello` and loses nothing. Shorter than the lease TTL, so an observed
    close still accelerates detection — which is the entire reason a close
    shortens a lease instead of reclaiming outright (gate §3.0.2).
    """
    return int(os.environ.get("LEASE_DISCONNECT_GRACE_SECONDS", "30"))


def worker_claim_ttl_seconds() -> int:
    """Recommendation, not a measured value.

    Bounds how long a registration/reaffirm claim blocks a second
    concurrent claim for the same worker ID. Deliberately short: this is
    a duplicate-use heuristic, not the real session-conflict resolution
    that lands in Phase 1.7 once Phase 1.5 gives the coordinator an
    actual live connection to arbitrate between.
    """
    return int(os.environ.get("WORKER_CLAIM_TTL_SECONDS", "10"))
