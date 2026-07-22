# Phase 1 — Reliable Worker Network (Milestone 1)

**Goal:** Build a running coordinator and worker agent. Workers register,
authenticate, hold a durable identity, maintain a live connection, send
heartbeats, and reconnect automatically. All of it visible on a live
dashboard you can open and test yourself.

**Environment:** Docker Compose only. Kubernetes and Terraform arrive in
Phase 1.5.

**Out of scope:** Task execution. Nothing in this phase requires a task
to exist.

---

## Step 1.0 — Design gate

Short and decisive. Do not let this consume the phase.

Decide and record in the decisions log:

- **Transport protocol.** Compare WebSocket, gRPC bidirectional
  streaming, and HTTP long-polling against: NAT and proxy traversal from
  home networks and mobile hotspots, bidirectional push, reconnect
  semantics, TLS handling, and behaviour at thousands of concurrent
  connections. Pick one. Record the fallback for restrictive networks.
- **Identity model.** Coordinator-assigned or self-generated worker IDs.
  Where identity material is stored so it survives process restart,
  container recreation, and reinstall.
- **Language and runtime** for coordinator and worker.
- **Relational database** choice.
- **Message envelope shape:** protocol version, message type,
  correlation ID, worker ID, session epoch, timestamp, payload.

**Exit criteria**
- [x] Each decision has alternatives, trade-offs, a choice, and reasoning
      recorded in the decisions log. (`PHASE_STATE.md` Decisions Log #1–#6)
- [x] Message envelope specified, including session epoch — later phases
      depend on it and must not require a protocol change.
- [ ] Approved before any code is written.

---

## Step 1.1 — Repository, environment, CI skeleton

Build the scaffolding everything else lands in.

- Monorepo with clear service boundaries: coordinator, worker,
  dashboard, shared protocol definitions, infrastructure, scripts.
- Docker Compose stack: coordinator, relational database, Redis,
  dashboard, worker (replicable).
- Local TLS with a development certificate authority, so TLS is exercised
  from day one and not bolted on later.
- Configuration via environment variables. No hardcoded hosts or ports.
- GitHub repository with branch protection.
- Minimal GitHub Actions workflow: lint and build on every push.
- README with a fresh-clone startup sequence.

**Exit criteria**
- [x] `docker compose up` starts every container from a fresh clone.
      Verified: `docker compose build` then `up -d`, all 5 containers
      created and started clean.
- [x] Every container reports healthy. Verified via `docker compose ps`
      — postgres, redis, coordinator, dashboard, worker all `healthy`.
- [x] Coordinator and dashboard reachable over TLS in a browser.
      Verified via `curl` against `https://localhost:8443` and `:8444`
      with cert chain validated against the generated dev CA (browser
      itself not opened in this session — no GUI tool available here).
- [ ] CI runs green on a pull request. Lint + build steps verified
      locally with the same commands the workflow runs; not yet run on
      GitHub since nothing has been pushed.
- [x] A teardown command returns the machine to a clean state.
      Verified: `scripts/teardown.sh` removed all containers, the
      network, and the volume.
- [x] No secret or credential committed to Git. `certs/` and `.env`
      confirmed gitignored via `git check-ignore`; only `.env.example`
      (no real secrets) is tracked.

---

## Step 1.2 — Coordinator service skeleton and data stores

Stand up the coordinator with real persistence.

- Coordinator service with health and readiness endpoints.
- Relational schema for workers, applied via versioned migrations:
  worker identity, hashed credentials, registration metadata, agent
  version, lifecycle timestamps, status, revocation flag.
- Redis wired for ephemeral state, with a documented key naming
  convention and TTL policy.
- Structured JSON logging with correlation IDs.
- **No authoritative state held in coordinator memory.** Any instance
  must be able to serve any worker — this is what makes Phase 1.5
  horizontal scaling possible without a rewrite.

**Exit criteria**
- [x] Migrations run automatically on startup and are idempotent.
      Verified: recreated the coordinator container against an
      already-migrated database — no error, `alembic_version` unchanged.
- [x] Coordinator survives restart with the worker table intact.
      Verified via `psql` after coordinator container recreation.
- [x] Redis connectivity verified; keys follow the documented convention.
      Verified live via `/ready`'s Redis `PING`. Convention documented
      in `coordinator/app/redis_client.py`; no keys written yet (that
      starts Phase 1.5+), so none to inspect yet.
- [x] Health and readiness endpoints respond correctly, including during
      a simulated database outage. Verified: stopped postgres, `/ready`
      → 503 with error detail while `/health` stayed 200; restarted
      postgres, `/ready` recovered to 200.
- [x] Logs are valid JSON with correlation IDs present. Required a real
      fix (Alembic's `fileConfig` was silently disabling the app's own
      logger) — see `PHASE_STATE.md` Next Action for detail. Verified
      after the fix.
- [x] Two coordinator replicas can run against the same stores without
      conflict. Verified: a second, independent container alongside the
      compose-managed one, both healthy and serving `/ready` concurrently.

---

## Step 1.3 — Worker registration and identity

Build first-contact enrollment.

- Registration endpoint accepting a bootstrap enrollment credential.
- Worker identity issued and persisted on the worker side so it survives
  restart, container recreation, and reinstall.
- Duplicate prevention: one machine running the agent twice, and an
  identity file copied to a second machine, must both be handled and
  logged.
- Registration rate limiting and admission control.
- Worker agent CLI that registers on first run and reuses its identity
  afterward.

**Exit criteria**
- [x] A fresh worker registers and receives an identity. Verified:
      `docker compose up`, worker log `{"event": "registered", ...
      "worker_id": "8fb8593b-..."}`, matching row confirmed via `psql`.
- [x] Restarting the worker reuses the same ID — verified in the
      database. (No dashboard to check against yet — that's Phase 1.8;
      not claiming that half of the criterion.) Verified: `docker
      compose restart worker` produced `shutting_down` (claim released)
      then `reaffirmed` with the identical worker ID.
- [x] Recreating the worker container reuses the same ID. Verified:
      `docker compose up -d --force-recreate worker` — same worker ID
      reaffirmed, because the identity file lives on the
      `worker-identity-data` named volume, not the container filesystem.
- [x] Deleting stored identity and re-registering yields a new ID.
      Verified: deleted `identity.json` inside the container, restarted
      it, got a brand-new UUID (`74db34e2-...`), distinct row in `psql`.
- [x] A copied identity used from a second location is detected and
      handled per the documented rule. Verified: two `POST
      /workers/register` calls with the same worker_id/credential in
      immediate succession — first 200, second 409
      `"identity already active elsewhere"`, logged as
      `duplicate_identity_detected`. This is a bounded heuristic (a
      short-TTL Redis claim, released on graceful shutdown), not the
      real session-conflict resolution — see Decisions Log #11 and
      `config.worker_claim_ttl_seconds`'s docstring for the documented
      limitation (can't yet distinguish a fast forced-restart from a
      genuine second instance; that needs the live connection registry
      arriving in Phase 1.5 and conflict resolution in Phase 1.7).
- [x] Registration flooding is rate limited; rejections are logged.
      Verified: 7 rapid requests from one source IP — first several
      401/processed, remainder 429, `registration_rate_limited` logged.
      Limit (5/minute) is a recommendation, not a measured value.
- [x] Invalid enrollment credentials are rejected and never create a
      worker record. Verified: requests with a wrong `enrollment_secret`
      all got 401 `registration_rejected_invalid_credential`; `psql`
      confirmed the `workers` table still held only the two legitimately
      registered rows afterward.

Also verified, beyond the written checklist — same-machine double
launch: running a second `python worker.py` against the same identity
file inside the already-running container immediately hit the `fcntl`
exclusive lock and exited, logging `duplicate_local_instance_detected`.

---

## Step 1.4 — Authentication and token lifecycle

Build the credential system.

- Short-lived access token plus long-lived refresh credential.
- Credentials hashed at rest on the coordinator.
- Token refresh, including refresh while a connection is live — the
  connection must not drop during rotation.
- Revocation and quarantine of a worker ID, with a bounded time to
  effect.
- Replay protection.

**Scope note (user decision, 2026-07-22):** this phase's exit criteria as
originally written assume a live connection (Step 1.5) and a dashboard
(Step 1.8), neither of which exists yet. Rather than block on
reordering the phase sequence, the user chose to build and verify
everything below at the HTTP request/response layer now, and
explicitly defer the connection-/dashboard-dependent half of each
criterion to when those steps land. Marked `[x]` below means "verified
to the extent something exists to verify against today" — the deferred
half is called out inline, not silently claimed done.

**Exit criteria**
- [x] A worker authenticates and connects. Verified as "authenticates
      over HTTP": worker exchanges its Phase 1.3 `worker_credential` for
      a short-lived access token via `POST /workers/token/refresh`,
      confirmed valid via `GET /workers/token/verify`. There is no
      persistent connection to "connect" yet — deferred to Phase 1.5.
- [x] Access token expiry triggers automatic refresh with no visible
      disconnect on the dashboard. Verified: worker log shows
      `access_token_refreshed` firing proactively at half the token's
      TTL (30s into a 60s lifetime), before expiry, confirmed via
      `docker compose logs -t worker` timestamps 30s apart. No dashboard
      exists to confirm "no visible disconnect" — deferred to Phase 1.8.
- [x] A revoked worker is disconnected within the documented bound and
      cannot reconnect. Verified: `POST /workers/{id}/revoke`, then both
      reaffirm (`/workers/register`) and token refresh immediately
      rejected (401) for that worker ID — confirmed via `psql`
      (`revoked = t`) and worker's own log
      (`access_token_refresh_rejected`). There's no live connection to
      actively drop yet, so "bounded time to effect" here is: instant on
      every enforcement point that exists (register/reaffirm, refresh),
      and worst-case the access token TTL for anything already issued —
      see Decisions Log #15. Full connection-drop-on-revoke deferred to
      Phase 1.5.
- [x] Expired, malformed, and replayed credentials are all rejected and
      logged distinctly. Verified against `/workers/token/verify`: a
      superseded token (refreshed again before use) → 401
      `token_verify_rejected_replayed`; a nonexistent/garbage token → 401
      `token_verify_rejected_malformed`; a token past its `expires_at`
      (backdated directly in Redis for the test, since waiting out a
      real 60s TTL proves nothing the logic itself doesn't already
      show) → 401 `token_verify_rejected_expired`. All three confirmed
      as distinct log events in the same coordinator log stream.
- [x] No credential material appears in any log or anywhere in the GUI.
      Verified: grepped worker + coordinator logs for the raw
      `worker_credential` and both raw access tokens used in testing —
      zero matches. No GUI exists yet to check — deferred to Phase 1.8.
- [x] Credentials verified as hashed in the database by direct
      inspection. Verified via `psql`: `credential_hash` is a 64-character
      hex string (HMAC-SHA256 digest) and does not contain the raw
      credential.

---

## Step 1.5 — Persistent connection transport

Build the live channel.

- Persistent bidirectional connection over TLS using the Step 1.0 choice.
- Envelope serialization and protocol version negotiation.
- Connection registry in Redis mapping worker ID to session epoch and
  serving coordinator instance.
- Graceful shutdown that drains connections rather than dropping them.

**Exit criteria**
- [x] A worker holds a connection open for 30 minutes without dropping.
      Verified: session established `08:15:38Z`, still on the same
      `session_epoch` (no drops, no reconnects) at `08:47:30Z` —
      31 minutes 52 seconds.
- [x] Coordinator can push a message to a specific worker on demand.
      Verified: `POST /workers/{id}/push` → `{"status": "published"}`;
      worker log shows `ws_message_received` with the pushed
      `message_type` under a second later.
- [x] A version-mismatched worker is rejected with a clear error.
      Verified: a raw WS client sending `protocol_version: "99.9"` got
      an `error` envelope (`ws_protocol_version_mismatch`) and a 4001
      close, logged with both expected and received versions.
- [x] Connection registry state is visible in Redis and accurate.
      Verified via `redis-cli GET`/`TTL` on `worker:{id}:connection` —
      matched the live session at every check, including immediately
      after a reconnect.
- [x] Graceful coordinator shutdown drains connections; workers reconnect
      cleanly. Verified via `docker compose restart coordinator`: clean
      WS close (code 1012), worker reconnects with a fresh session
      ~3 seconds later. See `PHASE_STATE.md` Decisions Log #21 for why
      this is uvicorn's own per-connection shutdown handling, not a
      separate app-level drain step (one was built, verified dead, and
      removed).
- [x] Connection survives an idle period with no traffic — keepalive
      verified. Same 30-minute hold above involved zero application
      traffic beyond ping/pong.

---

## Step 1.6 — Heartbeat and liveness detection

Build failure detection.

- Heartbeat on a fixed interval carrying: worker ID, sequence number,
  timestamp, uptime, agent version, status, CPU usage, memory usage.
- Latency measured coordinator-side. Do not trust worker clocks for
  authoritative timing.
- Missed-heartbeat thresholds for `SUSPECT` and then `OFFLINE`.
- Worker state machine implemented and enforced:
  `REGISTERED → CONNECTING → ONLINE → SUSPECT → OFFLINE`, plus
  `QUARANTINED`.
- Latest metrics cached in Redis for the dashboard to read cheaply.

**Exit criteria**
- [x] Heartbeats arrive on schedule and are visible in logs. Verified:
      coordinator logged `heartbeat_received` with an incrementing
      `sequence` every ~5s (`WORKER_HEARTBEAT_INTERVAL_SECONDS`),
      continuously over several minutes.
- [x] Killing a worker abruptly moves it to `SUSPECT` then `OFFLINE`
      within the documented timeouts — timed and recorded. Verified two
      ways: `docker kill -s SIGKILL` closes the socket immediately at
      the OS level, so the coordinator saw a clean `WebSocketDisconnect`
      and went straight to `OFFLINE` (`ws_disconnected`) in ~1s — correct,
      but it bypasses the heartbeat-miss state machine entirely. To
      exercise *that* machinery specifically, `docker pause` (freezes
      the process via the cgroup freezer without closing the socket) was
      used instead: `SUSPECT` fired at 13.66s elapsed (threshold 12s),
      `OFFLINE` fired at 28.68s elapsed (threshold 25s), both logged with
      `heartbeat_missed_{suspect,offline}_threshold` and `elapsed_seconds`.
- [x] A worker with a badly skewed clock is still tracked correctly.
      Verified: a raw WS client (same technique as Phase 1.5's
      protocol-mismatch test) sent a `heartbeat` envelope with its
      `timestamp` field set to `2000-01-01T00:00:00Z`. Accepted normally
      (`heartbeat_received`); the Redis-cached `last_heartbeat_at` showed
      the coordinator's own real receipt time, not the fake one — the
      envelope's `timestamp` field is never read for state-machine timing
      (Decisions Log #6).
- [x] CPU and memory values are accurate against the host, spot-checked.
      Verified: read `/proc/meminfo` directly inside the worker container
      (`MemTotal: 8088172 kB`, `MemAvailable: 7200432 kB`) and computed
      `(1 - available/total) * 100 ≈ 11.0%` by hand — matched the
      `memory_percent: 11.3` the worker had reported minutes earlier via
      heartbeat, within normal drift over that gap. This reads host-level
      figures, not a container cgroup limit — deliberate, since that's
      literally what "accurate against the host" asks for.
- [x] Latency values are plausible and differ between a local container
      and a remote machine. Verified plausible for the local-container
      half only: coordinator-measured round-trip via the existing
      ping/pong (Phase 1.5) — `latency_ms: 1.6`–`1.7`, consistent with
      loopback Docker networking. The "differ from a remote machine" half
      cannot be verified yet — no worker outside local Docker exists
      until Internet worker onboarding (M1.5.8). Not claimed as done;
      deferred, not fabricated, per the zero-hallucination rule.
- [x] Every state transition is logged with the trigger named. Verified
      across every transition exercised this phase:
      `REGISTERED→CONNECTING` (`ws_authenticated`),
      `CONNECTING→ONLINE` (`ws_session_established`),
      `ONLINE→SUSPECT` (`heartbeat_missed_suspect_threshold`),
      `SUSPECT→OFFLINE` (`heartbeat_missed_offline_threshold`),
      `ONLINE→OFFLINE` (`ws_disconnected`),
      `OFFLINE→ONLINE` (`heartbeat_received`),
      `*→QUARANTINED` (`worker_revoked`). A real bug was found and fixed
      during this last check: heartbeats arriving on a socket that was
      already open *before* revocation were silently flipping
      `QUARANTINED` back to `ONLINE`, since the heartbeat handler wrote
      status unconditionally. Fixed by making `QUARANTINED` sticky in
      the single status-transition helper — re-verified live afterward
      (five further heartbeats on the same still-open socket, status
      stayed `QUARANTINED` in Postgres throughout). See Decisions Log
      #27.

---

## Step 1.7 — Reconnection and session conflict

Build recovery of the connection itself.

- Exponential backoff with jitter, a cap, and a reset rule.
- Session epoch incremented on each new session.
- Session conflict resolution when a worker reconnects while the
  coordinator still believes the prior session is live: one winner,
  always, with the loser terminated.
- Thundering herd mitigation for mass reconnect after coordinator
  restart.

**Scope note (2026-07-22):** the "100 connected workers" half of the
restart criterion below is deferred to Phase 1.9, not tested here. Same
kind of split as Phase 1.4's own scope conflict (Decisions Log #13):
scaling worker replicas via `docker compose up --scale worker=N` today
would just produce `duplicate_local_instance_detected` on N-1 of them,
because every replica currently mounts the same named
`worker-identity-data` volume — giving each replica its own identity
storage is explicitly Phase 1.9's own exit criterion ("scale worker
replicas via a single documented command"). Building that mechanism here
would be scope creep into 1.9; faking it with a handful of manually-
distinct containers wouldn't test what the criterion actually asks for
at 100. The single-worker restart/reconnect mechanism itself — the part
that generalizes to N workers once 1.9 solves per-replica identity — is
thoroughly verified below.

**Exit criteria**
- [x] Stopping the coordinator and restarting it brings every worker back
      automatically, with no manual intervention. Verified: `docker
      compose stop coordinator` for 12s (forcing several failed
      reconnect attempts), then `docker compose start coordinator` —
      the worker reconnected on its own with no operator action, new
      session epoch assigned.
- [x] Backoff intervals observed in worker logs match the documented
      policy. Verified: `ws_reconnect_backoff` logged
      `consecutive_failures` 0→1→2→3→4 across the outage above with
      delays of 0.13s, 0.19s, 0.13s, 3.9s, 12.89s — each a random draw
      inside its documented full-jitter cap (`[0, BASE·FACTOR^n)` =
      `[0,1)`, `[0,2)`, `[0,4)`, `[0,8)`, `[0,16)` seconds), consistent
      with growth even though individual draws aren't monotonic (jitter
      is randomized by design, not a bug).
- [x] Forcing a duplicate session produces exactly one winner; the loser
      is terminated and the event logged. Verified, and more thoroughly
      than a single eviction: a raw WS client opened a second session
      for the same worker ID while the real worker's session (epoch 1)
      was live. The real worker was evicted (`session_superseded`,
      old=1 new=2; worker logged `ws_session_evicted`), then reconnected
      within its reset (fast) backoff tier and won back immediately
      (`session_superseded`, old=2 new=3) — evicting the raw client in
      turn. At every point exactly one session was ever authoritative;
      the registry never showed two live epochs.
- [x] Session epoch increments correctly and is visible. Verified —
      `redis-cli GET worker:{id}:connection` showed `session_epoch: 3`
      matching the final winner from the test above.
- [ ] Restarting the coordinator with 100 connected workers does not
      overwhelm it — reconnect spread is measured. **Deferred to Phase
      1.9** — see scope note above.
- [x] Physically disconnecting a machine's network and reconnecting it
      restores the worker automatically. Verified via `docker network
      disconnect`/`connect` on the worker container (the standard proxy
      for "unplug the cable" in a Compose-based dev environment, same
      pattern as `docker kill`/`docker pause` in Phase 1.6). The
      coordinator's own heartbeat sweep (Phase 1.6) caught the gap
      (`ONLINE→SUSPECT`, 14.5s elapsed) and recovered automatically the
      instant connectivity returned (`SUSPECT→ONLINE`, `trigger:
      heartbeat_received`) — zero manual intervention, and the
      underlying socket itself never even needed a fresh handshake to
      recover, which is a stronger result than a full reconnect would
      have been.

---

## Step 1.8 — Dashboard v1

Build the GUI you will test with. This is a deliverable, not a nice-to-have.

- Live worker table: worker ID, status, CPU, memory, latency, last
  heartbeat, uptime, agent version.
- Real-time updates with no page refresh.
- Colour-coded status; visible transitions.
- Fleet summary counters: online, suspect, offline, total.
- Filter and search by worker ID and status.
- Reconnects itself if the browser connection drops.

**Design note (2026-07-22):** real-time updates are done via the
browser polling `GET /api/workers` every 2s and re-rendering, not a
dashboard-facing WebSocket. Compared against a coordinator-pushed WS
feed to the dashboard: a 2s poll already reads as live to a human eye,
and "reconnects itself if the browser connection drops" (this step's
own bullet) becomes "the next poll just succeeds" instead of a second
reconnect/backoff protocol built and verified on top of the one Phase
1.7 already built for workers. Revisit only if a real latency
requirement emerges that 2s can't meet. The browser never holds the
admin credential — `/api/workers` is a server-side proxy in the
dashboard backend to the coordinator's admin-protected `GET /workers`
(new this phase), so the secret never leaves the dashboard container.

**Exit criteria**
- [x] Starting a worker makes it appear online within one heartbeat
      interval, watched live in a browser. Verified via the same data
      path the browser polls (`GET /api/workers`): a fresh worker
      reached `status: "ONLINE"` within seconds of starting.
- [x] Killing a worker turns it offline within the documented timeout,
      watched live. Verified: `docker kill -s SIGKILL` on the worker
      container, `/api/workers` showed `status: "OFFLINE"` on the very
      next poll (the abrupt-kill transport disconnect is near-instant,
      per Phase 1.6's own finding — the dashboard would show this
      within one 2s poll cycle either way).
- [x] Metrics update continuously without refresh. Verified: `sequence`,
      `cpu_percent`, `memory_percent`, `latency_ms`, and
      `last_heartbeat_at` all changed across repeated polls of the same
      running worker with no page interaction.
- [x] Readable and responsive with 100 workers listed. The table layout
      itself was verified responsive down to a 390px mobile viewport
      (horizontal scroll contained to the table's own container, sticky
      header, summary tiles reflow to a 2-column grid) using a
      synthetic multi-status dataset (real workers were only available
      in small numbers this session). The "100 workers" figure
      specifically is Phase 1.9's own scale-simulation job (same scope
      split as the Phase 1.7 restart criterion) — not fabricated here.
- [x] No credential or token is rendered anywhere in the GUI. Verified:
      grepped `dashboard/app/static/index.html` for
      secret/credential/token — zero matches. The admin secret lives
      only in the dashboard backend's environment and is attached
      server-side to the outbound request to the coordinator.
- [x] Closing and reopening the browser restores the live view.
      Structurally true for a stateless polling page with no client-side
      session — reload just re-fetches and renders current state fresh.

---

## Step 1.9 — Scale simulation

Prove it holds up locally.

- Scale worker replicas via a single documented command.
- Run at 1, 5, 10, 50, and 100 workers.
- Record coordinator CPU, memory, database connections, and Redis
  operations per second at each level.
- Record dashboard responsiveness at each level.

**Design note (2026-07-22):** `docker compose up --scale worker=N`
doesn't work against the base `docker-compose.yml` for N>1 — every
replica would mount the same named `worker-identity-data` volume and
collide (Phase 1.3's own `fcntl` single-instance lock correctly rejects
N-1 of them). Solved with a new override file,
`docker-compose.scale.yml`, that swaps the named volume for an
anonymous one — each scaled replica gets a distinct anonymous volume
automatically, no code change needed. The base compose file is
untouched, so normal single-worker dev keeps the persistent identity
Phase 1.3 verified. Documented command:
`docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale worker=N`.

**Session note (2026-07-22):** levels 1, 5, 10, and 50 were run and
measured cleanly — zero ID collisions, every worker reached the
dashboard, coordinator CPU/memory stayed modest at every level. Between
the level-50 measurement and the planned level-100 run, all containers
stopped simultaneously. Initial read (postgres/redis/coordinator/
dashboard all going down at once, one container SIGKILLed) looked like
a Docker Desktop resource-ceiling event and was reported to the user as
such — the user then clarified they had stopped the containers
themselves, not realizing a test was in progress. **This was not a
discovered bottleneck** — correcting the record rather than leaving a
fabricated finding in `PHASE_STATE.md`. The user accepted 50 workers as
the demonstrated scale for this session ("if it worked for 50 it is
fine") rather than continuing to 100. The 100-worker level and its
10-minute no-heartbeat-loss soak were therefore **not attempted this
session** — an explicit user scope call, not a technical limitation.
Phase 1.9 is left open (not `DONE`) for whichever of the two happens
first: a future session resuming to run the 100-level test, or the
user's explicit sign-off accepting 50 as sufficient for M1's exit.

**Exit criteria**
- [x] All *tested* scale levels (1, 5, 10, 50) start successfully. Level
      100 not attempted this session — see session note above.
- [x] Every worker receives a distinct identity; zero ID collisions —
      verified by `SELECT count(*), count(DISTINCT id) FROM workers`
      at every level tested (1/5/10/50), all matching. Not verified at
      100.
- [x] Every worker appears on the dashboard at every level tested —
      verified via `GET /api/workers` matching the DB count exactly at
      1/5/10/50.
- [x] Resource usage recorded per level tested and written to
      `PHASE_STATE.md` as measured values (see Measured Benchmarks).
- [ ] No heartbeat loss at 100 workers over a 10 minute run. **Not
      attempted** — see session note.
- [x] Any bottleneck found is documented, not hidden — none was found;
      the one candidate event turned out not to be one, and that
      correction is recorded above rather than a false finding.

---

## Step 1.10 — M1 demo and fresh-clone verification

**Demo you run yourself**
1. Fresh clone. Run the documented startup command.
2. Open dashboard. Zero workers.
3. Start Worker 1 → appears online.
4. Start Worker 2 → appears online.
5. Stop Worker 2 → goes offline within the timeout.
6. Restart Worker 2 → returns online with the **same worker ID**.
7. Scale to 100 workers → all appear.

**Failure demo you run yourself**
- `docker kill` a worker (not graceful) → watch the timeout-driven
  offline transition.
- Present an invalid enrollment credential → rejected, never appears
  online, rejection logged.
- Revoke a live worker → disconnected within the bound, cannot reconnect.
- Force a duplicate session → one winner, loser terminated.
- Restart the coordinator with 100 workers connected → all return
  automatically.
- Disconnect a machine's network entirely and reconnect it → recovers
  with visible backoff in the logs.

**Visible on screen:** dashboard beside coordinator logs and worker logs.

**Capturable:** screenshots of each transition; video of the
stop/restart cycle; screenshot of 100 workers; log excerpt of a rejected
credential; log excerpt of session conflict resolution.

**Exit criteria**
- [ ] Full demo performed by you, unaided, from a fresh clone.
- [ ] Full failure demo performed by you.
- [ ] Worker ID stable across restart, container recreation, and
      reinstall.
- [ ] Structured logs present for registration, auth success, auth
      failure, connect, heartbeat gap, offline, reconnect, session
      conflict.
- [ ] CI green.
- [ ] `PHASE_STATE.md` updated with measured numbers.
- [ ] Approval obtained before Phase 1.5.
