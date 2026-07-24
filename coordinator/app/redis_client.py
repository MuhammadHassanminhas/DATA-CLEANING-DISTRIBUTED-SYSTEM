"""Redis connection factory and key-naming convention.

Key naming convention: `worker:{worker_id}:{field}`, e.g.
`worker:<uuid>:session`, `worker:<uuid>:heartbeat`. All keys under the
`worker:` prefix are ephemeral, coordinator-observed state — never the
source of truth for anything durable (that's Postgres).

TTL policy: every key under this convention carries an explicit TTL set
where it is first written, sized to the liveness window of whatever
wrote it. No key naming convention entries were populated before Phase
1.3 — heartbeat and full session-registry keys start appearing in
Phases 1.5-1.6, which will set their own TTLs against numbers decided
in those phases' design gates, not invented here.

Phase 1.3 keys, both written in `app/main.py`'s registration endpoints:
- `worker:{worker_id}:claim` — set (NX) on registration/reaffirm, TTL
  from `config.worker_claim_ttl_seconds()`. A duplicate-use heuristic,
  not the real session registry (see that function's docstring for the
  limitation). Deleted early by `/workers/release` on graceful worker
  shutdown.
- `register:ratelimit:{source_ip}` — fixed-window request counter, TTL
  60s, limit from `config.register_rate_limit_per_minute()`.

Phase 1.4 keys:
- `access_token:{hash}` — JSON record (worker_id, generation,
  expires_at), TTL `access_token_ttl_seconds() * 3`.
- `worker:{worker_id}:token_gen` — integer generation counter, no TTL
  (must survive as long as the worker does; replay protection depends
  on it never resetting).

Phase 1.5 keys:
- `worker:{worker_id}:session_epoch` — integer, incremented once per
  accepted WebSocket handshake. No TTL — this is a monotonic counter,
  not a liveness signal.
- `worker:{worker_id}:connection` — JSON (session_epoch,
  coordinator_instance, connected_at). TTL `ws_registry_ttl_seconds()`,
  refreshed on every `pong`; deleted on graceful disconnect or drain.
  This is the connection registry: which coordinator instance is
  currently holding this worker's live socket.
- `worker:{worker_id}:push` — pub/sub channel, not a key. A coordinator
  instance publishes here to deliver an on-demand message to a worker
  regardless of which instance holds its live connection.

Phase 1.6 keys:
- `worker:{worker_id}:metrics` — JSON (sequence, uptime_seconds,
  agent_version, reported_status, cpu_percent, memory_percent,
  latency_ms, last_heartbeat_at). `last_heartbeat_at` is stamped with
  the coordinator's own receipt clock, never the worker-supplied
  envelope timestamp — this is what the SUSPECT/OFFLINE sweep reads,
  so a skewed worker clock cannot affect it. Seeded at `hello_ack` (so
  the sweep has a baseline before the first real heartbeat), updated on
  every heartbeat and every pong. TTL is
  `heartbeat_offline_threshold_seconds() * 3` — long enough to survive
  a couple of missed sweep cycles without going stale mid-window.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.config import redis_url

redis_client = redis.from_url(redis_url(), decode_responses=True)
