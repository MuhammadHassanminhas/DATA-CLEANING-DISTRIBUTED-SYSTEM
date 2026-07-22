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
- [ ] `docker compose up` starts every container from a fresh clone.
- [ ] Every container reports healthy.
- [ ] Coordinator and dashboard reachable over TLS in a browser.
- [ ] CI runs green on a pull request.
- [ ] A teardown command returns the machine to a clean state.
- [ ] No secret or credential committed to Git.

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
- [ ] Migrations run automatically on startup and are idempotent.
- [ ] Coordinator survives restart with the worker table intact.
- [ ] Redis connectivity verified; keys follow the documented convention.
- [ ] Health and readiness endpoints respond correctly, including during
      a simulated database outage.
- [ ] Logs are valid JSON with correlation IDs present.
- [ ] Two coordinator replicas can run against the same stores without
      conflict.

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
- [ ] A fresh worker registers and receives an identity.
- [ ] Restarting the worker reuses the same ID — verified in the database
      and on the dashboard.
- [ ] Recreating the worker container reuses the same ID.
- [ ] Deleting stored identity and re-registering yields a new ID.
- [ ] A copied identity used from a second location is detected and
      handled per the documented rule.
- [ ] Registration flooding is rate limited; rejections are logged.
- [ ] Invalid enrollment credentials are rejected and never create a
      worker record.

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

**Exit criteria**
- [ ] A worker authenticates and connects.
- [ ] Access token expiry triggers automatic refresh with no visible
      disconnect on the dashboard.
- [ ] A revoked worker is disconnected within the documented bound and
      cannot reconnect.
- [ ] Expired, malformed, and replayed credentials are all rejected and
      logged distinctly.
- [ ] No credential material appears in any log or anywhere in the GUI.
- [ ] Credentials verified as hashed in the database by direct
      inspection.

---

## Step 1.5 — Persistent connection transport

Build the live channel.

- Persistent bidirectional connection over TLS using the Step 1.0 choice.
- Envelope serialization and protocol version negotiation.
- Connection registry in Redis mapping worker ID to session epoch and
  serving coordinator instance.
- Graceful shutdown that drains connections rather than dropping them.

**Exit criteria**
- [ ] A worker holds a connection open for 30 minutes without dropping.
- [ ] Coordinator can push a message to a specific worker on demand.
- [ ] A version-mismatched worker is rejected with a clear error.
- [ ] Connection registry state is visible in Redis and accurate.
- [ ] Graceful coordinator shutdown drains connections; workers reconnect
      cleanly.
- [ ] Connection survives an idle period with no traffic — keepalive
      verified.

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
- [ ] Heartbeats arrive on schedule and are visible in logs.
- [ ] Killing a worker abruptly moves it to `SUSPECT` then `OFFLINE`
      within the documented timeouts — timed and recorded.
- [ ] A worker with a badly skewed clock is still tracked correctly.
- [ ] CPU and memory values are accurate against the host, spot-checked.
- [ ] Latency values are plausible and differ between a local container
      and a remote machine.
- [ ] Every state transition is logged with the trigger named.

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

**Exit criteria**
- [ ] Stopping the coordinator and restarting it brings every worker back
      automatically, with no manual intervention.
- [ ] Backoff intervals observed in worker logs match the documented
      policy.
- [ ] Forcing a duplicate session produces exactly one winner; the loser
      is terminated and the event logged.
- [ ] Session epoch increments correctly and is visible.
- [ ] Restarting the coordinator with 100 connected workers does not
      overwhelm it — reconnect spread is measured.
- [ ] Physically disconnecting a machine's network and reconnecting it
      restores the worker automatically.

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

**Exit criteria**
- [ ] Starting a worker makes it appear online within one heartbeat
      interval, watched live in a browser.
- [ ] Killing a worker turns it offline within the documented timeout,
      watched live.
- [ ] Metrics update continuously without refresh.
- [ ] Readable and responsive with 100 workers listed.
- [ ] No credential or token is rendered anywhere in the GUI.
- [ ] Closing and reopening the browser restores the live view.

---

## Step 1.9 — Scale simulation

Prove it holds up locally.

- Scale worker replicas via a single documented command.
- Run at 1, 5, 10, 50, and 100 workers.
- Record coordinator CPU, memory, database connections, and Redis
  operations per second at each level.
- Record dashboard responsiveness at each level.

**Exit criteria**
- [ ] All five scale levels start successfully.
- [ ] Every worker receives a distinct identity; zero ID collisions at
      100 — verified by database query.
- [ ] Every worker appears on the dashboard at every level.
- [ ] Resource usage recorded per level and written to `PHASE_STATE.md`
      as measured values.
- [ ] No heartbeat loss at 100 workers over a 10 minute run.
- [ ] Any bottleneck found is documented, not hidden.

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
