# PHASE_STATE.md

Single source of truth for project status.
Update at every phase transition. Never leave stale.

See `SESSION_HANDOFF.md` for resume-work context from the most recent
session: gotchas hit, open questions mid-decision, uncommitted changes.
This file stays the authority on phase/gate status if the two disagree.

---

## Snapshot

| Field | Value |
|---|---|
| Project | Distributed AI-Orchestrated SQL Database Cleaning Platform |
| Scope in progress | Version 1 — Distributed Worker Network |
| Current milestone | M1 — Reliable Worker Network |
| Current phase | 1.10 — M1 demo and fresh-clone verification |
| Phase status | NOT STARTED |
| Last updated | 2026-07-22 |
| Approval gate | Phases 1.0–1.9 approved 2026-07-22. Phase 1.9 approved by explicit user sign-off accepting the 50-worker level as sufficient; 100-worker level and its 10-minute soak explicitly waived, not silently skipped. |

---

## Milestone Progress

| Milestone | Title | Status | Demo done | Failure demo done | Internet tested | Fresh clone verified |
|---|---|---|---|---|---|---|
| M1 | Reliable Worker Network | NOT STARTED | No | No | n/a | No |
| M1.5 | Infrastructure & Deployment | BLOCKED (M1) | No | No | No | No |
| M2 | Task Distribution | BLOCKED (M1.5) | No | No | No | No |
| M3 | Fault Tolerance | BLOCKED (M2) | No | No | No | No |
| M4 | Adaptive Scheduling | BLOCKED (M3) | No | No | No | No |

Status values: `NOT STARTED` · `IN PROGRESS` · `AWAITING APPROVAL` · `DONE` · `BLOCKED`

---

## Phase Register

### Milestone 1 — Reliable Worker Network
| Phase | Title | Status |
|---|---|---|
| 1.0 | Design gate — protocol and identity decisions | DONE |
| 1.1 | Repository, environment, CI skeleton | DONE |
| 1.2 | Coordinator service skeleton and data stores | DONE |
| 1.3 | Worker registration and identity | DONE |
| 1.4 | Authentication and token lifecycle | DONE |
| 1.5 | Persistent connection transport | DONE |
| 1.6 | Heartbeat and liveness detection | DONE |
| 1.7 | Reconnection and session conflict | DONE |
| 1.8 | Dashboard v1 — live worker view | DONE |
| 1.9 | Scale simulation 1 → 100 workers | DONE |
| 1.10 | M1 demo and fresh-clone verification | NOT STARTED |

### Milestone 1.5 — Infrastructure & Deployment
| Phase | Title | Status |
|---|---|---|
| 1.5.0 | Design gate — cloud topology and cost | NOT STARTED |
| 1.5.1 | Terraform base infrastructure | NOT STARTED |
| 1.5.2 | Kubernetes manifests and Helm packaging | NOT STARTED |
| 1.5.3 | GitHub Actions CI pipeline | NOT STARTED |
| 1.5.4 | GitHub Actions CD pipeline | NOT STARTED |
| 1.5.5 | Public ingress, TLS, DNS | NOT STARTED |
| 1.5.6 | Observability stack | NOT STARTED |
| 1.5.7 | Coordinator horizontal scaling proof | NOT STARTED |
| 1.5.8 | Real Internet worker onboarding | NOT STARTED |
| 1.5.9 | M1.5 demo and verification | NOT STARTED |

### Milestone 2 — Task Distribution
| Phase | Title | Status |
|---|---|---|
| 2.0 | Design gate — queue and assignment model | NOT STARTED |
| 2.1 | Task model, schema, state machine | NOT STARTED |
| 2.2 | Redis-backed queue | NOT STARTED |
| 2.3 | Assignment engine | NOT STARTED |
| 2.4 | Worker execution runtime | NOT STARTED |
| 2.5 | Result submission and completion | NOT STARTED |
| 2.6 | Operator task APIs | NOT STARTED |
| 2.7 | Dashboard v2 — task lifecycle views | NOT STARTED |
| 2.8 | Load testing harness | NOT STARTED |
| 2.9 | M2 demo and verification | NOT STARTED |

### Milestone 3 — Fault Tolerance
| Phase | Title | Status |
|---|---|---|
| 3.0 | Design gate — failure taxonomy and guarantees | NOT STARTED |
| 3.1 | Lease and timeout engine | NOT STARTED |
| 3.2 | Reassignment and retry | NOT STARTED |
| 3.3 | Idempotency and duplicate suppression | NOT STARTED |
| 3.4 | Stale result fencing | NOT STARTED |
| 3.5 | Coordinator restart and recovery | NOT STARTED |
| 3.6 | Partial completion policy | NOT STARTED |
| 3.7 | Dashboard v3 — failure and recovery views | NOT STARTED |
| 3.8 | Chaos testing harness | NOT STARTED |
| 3.9 | M3 demo and verification | NOT STARTED |

### Milestone 4 — Adaptive Scheduling
| Phase | Title | Status |
|---|---|---|
| 4.0 | Design gate — capability trust model | NOT STARTED |
| 4.1 | Capability reporting | NOT STARTED |
| 4.2 | Telemetry pipeline and decay | NOT STARTED |
| 4.3 | Scoring engine | NOT STARTED |
| 4.4 | Rule-based scheduler | NOT STARTED |
| 4.5 | Scheduler explainability | NOT STARTED |
| 4.6 | Dashboard v4 — scheduling visualization | NOT STARTED |
| 4.7 | Heterogeneous benchmark harness | NOT STARTED |
| 4.8 | M4 demo and Version 1 sign-off | NOT STARTED |

---

## Environments

| Environment | Purpose | Status | URL |
|---|---|---|---|
| local | Docker Compose development | NOT PROVISIONED | — |
| staging | Kubernetes, public Internet testing | NOT PROVISIONED | — |
| production | Kubernetes, real fleet | NOT PROVISIONED | — |

---

## Real Internet Workers Verified

| # | Machine type | Network | Phase verified | Notes |
|---|---|---|---|---|
| — | (none yet) | — | — | — |

Target machine types: second laptop, desktop, VPS, friend's computer,
home Wi-Fi, mobile hotspot.

---

## Decisions Log

Append only. Never rewrite an entry.

| # | Date | Decision | Alternatives | Rationale | Phase |
|---|---|---|---|---|---|
| 1 | 2026-07-22 | Transport: **WebSocket (wss) over TLS as primary**, with **HTTP long-polling as the documented fallback** for networks that block the WS upgrade | gRPC bidirectional streaming; HTTP long-polling as primary | WS runs on 443 like ordinary HTTPS, so it survives the class of uncontrolled networks this project targets (home Wi-Fi, mobile hotspot, corporate proxy) better than gRPC — gRPC rides HTTP/2, and HTTP/2 bidirectional streams are a documented failure point behind TLS-terminating and older L7 proxies. gRPC's typed-contract/multiplexing advantages don't outweigh that traversal risk for this deployment target. Long-polling alone is rejected as primary because it can't give the coordinator low-latency on-demand push (Step 1.5 requires pushing to a specific worker on demand) and holds thousands of long-lived HTTP requests open across proxies with inconsistent read-timeout behavior. It remains the fallback because plain HTTP is the most universally proxyable protocol there is. | 1.0 |
| 2 | 2026-07-22 | Identity: **coordinator-assigned UUIDv4**, issued at registration, persisted by the worker to a local identity file surviving restart/container recreation/reinstall; deleting that file forces re-registration under a new ID | Self-generated worker ID presented at registration | CLAUDE.md §12 requires that anything the coordinator trusts heavily be coordinator-observed, not worker-reported. A self-generated ID is worker-reported and lets a malicious or buggy worker claim or collide with an existing ID. Coordinator-assigned identity removes that attack surface entirely and matches the Step 1.3 exit criteria (same ID across restart/recreate, new ID after deleting stored identity). | 1.0 |
| 3 | 2026-07-22 | Coordinator language/runtime: **Python 3, FastAPI, asyncio** | Go; Node.js/TypeScript | Matches the stack already committed to in `PRD/Project Architecture Specification (Version 1).md` (chosen there for the later AI-planning phases, out of scope for V1 but a stated long-term constraint). Asyncio is adequate for this workload — persistent-connection handling is I/O-bound, not CPU-bound, so the GIL is not disqualifying at the target scale (100 workers now, low thousands eventually). Go's goroutine model is a real technical edge for massive concurrent-connection counts, but splitting the coordinator into a second language ecosystem has a real maintenance cost for a project this size and isn't justified until a measured scale test (Step 1.9 / M1.5.7) shows asyncio is the bottleneck. | 1.0 |
| 4 | 2026-07-22 | Worker language/runtime: **Python 3**, same as coordinator | Go static binary; Node.js/TypeScript | The "zero-install binary" advantage of a Go worker is neutralized because Docker is the stated primary distribution mechanism (CLAUDE.md §4 sequencing rule; PRD workers run as Docker containers first) — the container already bundles the runtime. One language across the V1 codebase reduces operational surface area. Revisit only if real Internet onboarding (M1.5.8, non-Docker machines) surfaces friction this doesn't anticipate. | 1.0 |
| 5 | 2026-07-22 | Relational database: **PostgreSQL** | MySQL/MariaDB; SQLite | SQLite is disqualified outright by architectural invariant §3.9 (coordinator horizontally scalable, no instance holds authoritative state in memory) — it's a single-file embedded DB, incompatible with multiple coordinator replicas sharing state. Between Postgres and MySQL, Postgres matches the PRD's stated stack, has stronger native JSON column support useful for the flexible capability/metadata fields M4 will need, and has mature migration tooling (Alembic) that fits the FastAPI/SQLAlchemy ecosystem chosen above. | 1.0 |
| 6 | 2026-07-22 | Message envelope: **JSON**, fixed top-level shape `{protocol_version, message_type, correlation_id, worker_id, session_epoch, timestamp, payload}` | Protobuf; MessagePack | JSON is directly inspectable in the structured logs CLAUDE.md §11 requires — a wire message can be pasted straight into a log line with no decoder, which matters for a project run under a zero-hallucination rule where every claim needs to be independently verifiable. Protobuf and MessagePack are faster/more compact but that only matters once large data payloads exist, which is explicitly out of scope for V1. `message_type` is a closed enum extended by later phases without changing the envelope shape (satisfies the "must not require a protocol change" exit criterion). `session_epoch` is coordinator-incremented per accepted session and is what Step 1.7's session-conflict resolution keys off. `timestamp` is sender-generated but explicitly non-authoritative — Step 1.6 measures liveness latency coordinator-side and does not trust worker clocks. | 1.0 |
| 7 | 2026-07-22 | Local dev TLS: **each service terminates TLS directly** (uvicorn `--ssl-certfile`/`--ssl-keyfile`) against a self-signed dev CA generated by `infra/dev-ca/generate-dev-ca.sh`; no reverse proxy in front of coordinator/dashboard yet | Nginx or Traefik reverse proxy terminating TLS for both services | Only two services need TLS termination at this stage, so a reverse proxy adds a moving part Phase 1.1 doesn't need — matches CLAUDE.md §4's "do not bury Phase 1 in infrastructure before a single worker connects." A reverse proxy/ingress is the right call once there's real public ingress to manage, which is explicitly M1.5.5's job, not this phase's. | 1.1 |
| 8 | 2026-07-22 | Migration tooling: **Alembic**, async engine, `alembic upgrade head` run automatically from the coordinator's FastAPI lifespan on every startup via `asyncio.to_thread` (Alembic's own migration runner isn't async-native) | Hand-rolled SQL migration runner | Alembic already implied by the Postgres/SQLAlchemy choice (Decision #5's own rationale names it); a hand-rolled runner would just re-implement version tracking and idempotency Alembic already does correctly. Running it automatically on startup (rather than as a separate manual step) is what makes "runs from a fresh clone with zero undocumented manual steps" (CLAUDE.md §13) true for the database layer. | 1.2 |
| 9 | 2026-07-22 | Bootstrap enrollment credential: a **single shared secret** (`ENROLLMENT_SECRET` env var), compared with `hmac.compare_digest` | Per-worker pre-provisioned credentials issued out-of-band | Per-worker issuance needs an admin issuance flow that's out of scope for 1.3 (no operator UI/CLI exists yet). A shared bootstrap secret matches CLAUDE.md §12's own phrasing ("bootstrap enrollment credential, then short-lived access token plus long-lived refresh credential") and is identical across Docker/VPS/laptop workers, preserving the "one protocol, all environments" invariant. Revisit if real fleet onboarding (M1.5.8) shows a shared secret doesn't scale operationally. | 1.3 |
| 10 | 2026-07-22 | Worker credential hashing: **HMAC-SHA256 with a server-side pepper** (`CREDENTIAL_PEPPER` env var) over a `secrets.token_urlsafe(32)` random credential | bcrypt/argon2 (password-style slow KDF) | The credential is a 256-bit random token, not a human password — slow KDFs exist to slow brute-forcing of *low-entropy* secrets, which doesn't apply here. A keyed fast hash is standard practice for high-entropy API-key-style credentials, and the pepper means a stolen `credential_hash` column alone (without the separately-stored pepper) can't be matched against a guessed value. | 1.3 |
| 11 | 2026-07-22 | Duplicate cross-machine identity use: a **short-TTL Redis claim** (`worker:{id}:claim`, default 10s — recommendation, not measured) set on every registration/reaffirm call, released early on graceful worker shutdown (SIGTERM); a concurrent claim attempt while the previous one is unexpired is rejected (409) and logged | Full session-conflict eviction (one winner, loser's live connection terminated) | Full eviction needs a live connection to terminate, which doesn't exist until Phase 1.5, and the "one winner" arbitration rule is explicitly Phase 1.7's job. This claim is a deliberately bounded heuristic for 1.3: it catches a copied identity used concurrently within the TTL window and is released on graceful shutdown so normal restarts don't false-positive, but it **cannot** distinguish a genuine second instance from a very fast forced restart (`docker kill` + immediate relaunch) within that window — documented limitation, not silently assumed away. | 1.3 |
| 12 | 2026-07-22 | Registration rate limiting: **fixed-window counter per source IP** in Redis (`register:ratelimit:{ip}`, 60s window, default 5 requests — recommendation, not measured) | Token bucket / leaky bucket | A fixed window is simpler to reason about and sufficient at this scale (no load test has run to justify a smoother algorithm yet); revisit if Phase 1.9's scale simulation shows burst behavior at the window boundary is a real problem. | 1.3 |
| 13 | 2026-07-22 | Phase 1.4 scope: build and verify the **HTTP-only subset** now (token issuance/refresh/verify, hashing, revocation's currently-available enforcement points, replay protection); explicitly defer the connection-drop and dashboard-visible halves of its exit criteria to Phases 1.5/1.8 | Reorder — build Step 1.5 (transport) before 1.4; or stop and defer all of 1.4 | User's explicit choice when the conflict was raised (1.4's exit criteria as written assume a live connection and a dashboard that don't exist until later steps in this same phase). Reordering was the other option on the table but rejected in favor of building the HTTP-testable subset now and coming back for the rest once 1.5/1.8 exist, rather than delaying token/auth work entirely. | 1.4 |
| 14 | 2026-07-22 | Access token model: **opaque random token** (`secrets.token_urlsafe(32)`), coordinator-observed via a Redis record (worker_id, generation, expires_at) keyed by the token's hash, TTL 60s (recommendation, not measured) | Self-contained signed JWT | Revocation must actively invalidate a token before its natural expiry regardless of format, which means the coordinator needs a lookup/denylist either way — a JWT's self-containedness buys nothing here and adds a signing-key dependency this project doesn't otherwise need. An opaque token also matches CLAUDE.md §12's "coordinator-observed, not worker-reported" trust principle directly. | 1.4 |
| 15 | 2026-07-22 | Replay protection: a **per-worker generation counter** (`worker:{id}:token_gen` in Redis) incremented on every refresh; each token is stamped with the generation current at mint time, and `verify_token` rejects any token whose generation doesn't match the worker's current one | Single-use refresh-token rotation on the long-lived `worker_credential` itself | The long-lived credential (Phase 1.3) is deliberately reusable across every reaffirm, not single-use — rotating it would contradict Decision #10/#11. Rotating the *access token's generation* instead gives the same real, demonstrable guarantee (an old token becomes invalid the instant a newer one is minted) without touching the long-lived credential's design. | 1.4 |
| 16 | 2026-07-22 | Revocation bound: no live connection exists yet to actively terminate (that arrives in Phase 1.5), so "bounded time to effect" for now means **instant** on every enforcement point that exists today (register/reaffirm, token refresh both check `revoked`) and **worst-case the access token TTL** for any token already issued before revocation | Wait for Phase 1.5 before implementing revocation at all | Revocation's DB-level effect (the `revoked` flag and its enforcement in existing endpoints) doesn't need a live connection to exist — implementing it now means Phase 1.5 only has to add connection-drop-on-revoke, not revocation itself. | 1.4 |
| 17 | 2026-07-22 | Revoke endpoint admin credential: **reuses `ENROLLMENT_SECRET`** as a stand-in admin credential (`POST /workers/{id}/revoke` body) | Design a dedicated operator/admin auth model now | No operator/admin auth model has been designed — building one now would be scope creep beyond what Phase 1.4's own written scope (worker-side auth) asks for. Reusing the existing shared secret is a explicitly-flagged stopgap, not a real access-control model; revisit when operator tooling is actually designed (no milestone currently scopes this — flag if one should). | 1.4 |
| 18 | 2026-07-22 | WebSocket handshake: **auth token in the `Authorization: Bearer` header at connect time, then a `hello` envelope naming worker ID and protocol version**, verified before any `hello_ack`. Token is checked once, at handshake — not re-checked for the life of the socket | Re-verify the token periodically over the open connection | The Phase 1.4 access token's only job is proving identity to open the channel; re-checking it mid-connection would require either the coordinator pushing forced-reconnects on expiry (adds complexity with no corresponding exit criterion in 1.4 or 1.5) or the worker re-authenticating over the same socket (protocol complexity not asked for). Revocation is still enforced promptly because the worker's own reconnect loop calls `/workers/token/refresh` fresh on every connection attempt, and that endpoint already checks `revoked` (Decision #16). | 1.5 |
| 19 | 2026-07-22 | Connection registry (`worker:{id}:connection` in Redis): written on accept, **refreshed by TTL on every `pong`**, deleted by the per-connection handler's own `finally` block on disconnect (compares stored `session_epoch` before deleting, so a fast reconnect's new entry is never clobbered by the old connection's late cleanup) | Heartbeat-driven registry (defer to Phase 1.6) | The registry needs to exist now — Phase 1.5's own exit criteria require it ("registry state is visible in Redis and accurate") — but the real liveness state machine (`SUSPECT`/`OFFLINE`, missed-heartbeat thresholds) is explicitly Phase 1.6's job. This is deliberately just "is a socket open," nothing more. | 1.5 |
| 20 | 2026-07-22 | On-demand push (`POST /workers/{id}/push`): **fire-and-forward via a per-worker Redis pub/sub channel** (`worker:{id}:push`), not a queue. Any coordinator instance can publish; whichever instance holds the live connection (if any) forwards it. No delivery confirmation, no persistence if nobody's listening | A durable per-worker outbox/queue in Redis or Postgres | A queue implies "deliver eventually, survive a disconnect" — that's what M2's real task-delivery pipeline is for. Building a durable outbox here would duplicate work M2 does properly, for a phase whose own exit criterion only asks for "push a message to a connected worker on demand." Documented as fire-and-forward, not claimed as more than that. | 1.5 |
| 21 | 2026-07-22 | Graceful shutdown: relies entirely on **uvicorn's own per-connection `Server.shutdown()`** (sends a real WS close, code 1012 "service restart," and waits for each connection handler's task — including its `finally` cleanup — to finish before the app's own lifespan shutdown runs), rather than app-level code that re-sends a close to every connection | An app-level `_drain_local_connections()` step in the FastAPI lifespan shutdown hook that explicitly messages and closes every locally-held socket | Built the app-level version first, then verified live that it never actually ran with anything to do: uvicorn's `Server.shutdown()` (source inspected directly, `uvicorn==0.51.0`) calls `connection.shutdown()` on every live connection and awaits `_wait_tasks_to_complete()` **before** invoking `self.lifespan.shutdown()` — so every `ws_connect` handler's own disconnect cleanup has already completed by the time app-level shutdown code would run. Kept the simpler, verified-correct path and deleted the dead code (`_LocalConnection`, `_local_connections`, `_drain_local_connections`) rather than leave a function whose docstring claimed behavior it could never actually perform. | 1.5 |
| 22 | 2026-07-22 | Worker reconnect: **fixed 3-second delay** (`WORKER_WS_RECONNECT_DELAY_SECONDS`), not exponential backoff | Exponential backoff with jitter now | Explicitly Phase 1.7's job per the phase document's own Step 1.7 scope ("exponential backoff with jitter, a cap, and a reset rule"). Building it now would duplicate work and pre-empt 1.7's own design gate. Documented in both the phase doc's exit criteria note and `worker/worker.py`'s module docstring as a deliberate placeholder, not a silently-incomplete implementation. | 1.5 |
| 23 | 2026-07-22 | Heartbeat transport: an **application-level `heartbeat` envelope sent over the existing `/ws/connect` socket** every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 5s), entirely separate from the Phase 1.5 transport-level ping/pong keepalive | Reuse ping/pong itself as the heartbeat signal | Ping/pong (Phase 1.5) only proves the socket is open — it carries no payload and was never meant to. The Step 1.6 heartbeat needs to carry worker-reported metrics (CPU, memory, uptime, sequence, agent version) that ping/pong has no shape for. Overloading ping/pong would conflate "is the transport alive" with "is the application reporting fresh state," which need different failure semantics (transport timeout is 45s; SUSPECT/OFFLINE thresholds are much tighter). Keeping them separate also means a future change to one cadence doesn't silently affect the other. | 1.6 |
| 24 | 2026-07-22 | Liveness thresholds: `HEARTBEAT_SUSPECT_THRESHOLD_SECONDS=12`, `HEARTBEAT_OFFLINE_THRESHOLD_SECONDS=25`, `HEARTBEAT_SWEEP_INTERVAL_SECONDS=5` — all **recommendations, not measured values** | Missed-heartbeat *count* (e.g. "2 missed = SUSPECT") instead of absolute seconds | Absolute-seconds thresholds decouple the coordinator's sweep logic from the worker's configured interval entirely — the coordinator doesn't need to know or trust what interval a given worker claims to run at (coordinator-observed, not worker-reported, per CLAUDE.md §12). A count-based rule would require the coordinator to trust a worker-supplied interval or hardcode an assumption about it. No load test has justified these specific numbers yet; revisit if Phase 1.9's scale simulation shows false positives/negatives at scale. | 1.6 |
| 25 | 2026-07-22 | CPU/memory measurement: **stdlib-only reads of `/proc/stat` and `/proc/meminfo`** inside the worker, no new dependency | `psutil` | Two counters (CPU%, memory%) don't justify a new third-party dependency — `/proc` gives both directly on the Linux containers this project targets. Deliberately reads host-level figures rather than a container cgroup limit, which is what the Step 1.6 exit criterion's own wording ("accurate against the host") calls for, not a coincidence of the simpler approach. | 1.6 |
| 26 | 2026-07-22 | Latency: reuses the **existing Phase 1.5 ping/pong round trip**, coordinator stamping `time.monotonic()` when a `ping` is sent and computing the delta when the matching `pong` arrives | A dedicated latency-probe message | Phase 1.5 already sends a ping every `WS_PING_INTERVAL_SECONDS` and expects a pong back — that round trip already is a latency measurement; a second, separate probe message would duplicate traffic for no new information. Coordinator-side timing only (Step 1.6's own requirement) — never derived from the worker's embedded envelope timestamp. | 1.6 |
| 27 | 2026-07-22 | `QUARANTINED` is a **sticky terminal status** — the single status-transition helper refuses to move a worker out of `QUARANTINED` via any path (heartbeat, sweep, reconnect); only an explicit un-quarantine feature (not yet designed) could do so | Let heartbeats naturally re-establish `ONLINE` once a socket is dropped and reconnected | Found as a real bug during this phase's own verification, not left for later: a socket already open *before* `POST /workers/{id}/revoke` keeps sending heartbeats — Phase 1.5's Decisions Log #18 already documents that revocation doesn't re-check a live socket mid-connection — and those heartbeats were silently flipping the DB `status` back to `ONLINE`, contradicting the revocation an operator just issued. Re-verified live after the fix: five heartbeats on a still-open pre-revocation socket left `status = QUARANTINED` unchanged in Postgres throughout. | 1.6 |
| 28 | 2026-07-22 | Reconnect backoff: **full-jitter exponential** (`delay = random.uniform(0, min(MAX, BASE·FACTOR^consecutive_failures))`, defaults `BASE=1s, FACTOR=2, MAX=30s`), resetting `consecutive_failures` to 0 the moment a session is genuinely established (`hello_ack`), regardless of how that connection later ends | Equal-jitter or decorrelated-jitter backoff; a separate fixed post-restart stagger on top of the backoff | Full jitter is the simplest of the three that still gives real thundering-herd mitigation — a whole fleet failing at once each draws independently from `[0, cap)` rather than following each other's exact intervals, with no extra mechanism needed. A separate stagger step would duplicate what the jitter already provides. Resetting on session establishment (not on "the try/except block returned without raising") matters because a coordinator-initiated clean close after a real session isn't a failure — treating it as one would leave the *next* unrelated failure backed off further than it should be. | 1.7 |
| 29 | 2026-07-22 | Session-conflict resolution: **Redis pub/sub eviction** — every accepted handshake publishes its new `session_epoch` to `worker:{id}:control` before becoming authoritative; whichever coordinator instance holds an older live session for that worker ID (any replica, per CLAUDE.md §3.9) is subscribed and force-closes its own socket (code 4409) the instant it sees a newer epoch | Have the new handshake block and wait for the coordinator to confirm the old session is closed before proceeding | Blocking the new handshake on old-session teardown would need a request/response protocol over pub/sub (ack channels, timeouts) for a guarantee the existing epoch-comparison already gives for free: the loser's own registry-cleanup guard (Decisions Log #19) already no-ops correctly once its epoch is stale, so the only genuinely missing piece was *actively terminating the transport*, not coordinating who's allowed to proceed. The winner never waits on the loser's cleanup. | 1.7 |
| 30 | 2026-07-22 | Dashboard real-time updates: **client polls `GET /api/workers` every 2s** and re-renders | A dashboard-facing WebSocket pushed from the coordinator | A 2s poll already reads as live to a human eye. It also makes "reconnects itself if the browser connection drops" (Step 1.8's own exit criterion) free — a poll that fails just tries again in 2s — instead of requiring a second reconnect/backoff protocol on top of the one Phase 1.7 already built and verified for workers. Revisit only if a real sub-second latency requirement emerges. | 1.8 |
| 31 | 2026-07-22 | Dashboard-to-coordinator auth: **server-side proxy** — the dashboard backend holds the admin secret (reused `ENROLLMENT_SECRET`, same stopgap as `/revoke` and `/push`) and calls the coordinator's new admin-protected `GET /workers`; the browser only ever talks to the dashboard backend, never the coordinator directly | Have the browser call the coordinator's `/workers` endpoint directly with the secret embedded in client JS | Embedding the admin secret in anything the browser can read would violate CLAUDE.md §12 ("never rendered in the GUI") the instant a user opened dev tools — there is no way to call an admin-protected endpoint from client-side JS without exposing the credential used to authenticate it. | 1.8 |
| 32 | 2026-07-22 | Visual identity: **dark "radar/phosphor console" theme** — deep green-tinted black (not neutral or blue-black), a signature animated hub-and-spoke glyph as the wordmark icon (a literal, functional nod to the star-topology invariant, CLAUDE.md §3.1, not decoration), and a multi-hue functional color system (five distinct status colors) rather than one decorative accent color | The generic "near-black background + one bright accent color" AI-default template | Deliberately avoided the single-accent-color cliché: the palette's chroma comes from the five worker states themselves (ONLINE/SUSPECT/OFFLINE/QUARANTINED/CONNECTING), which is what this page is actually *for*, not from a brand accent layered on top. The radar/phosphor console direction is grounded in the subject (a network-operations monitoring tool watching a hub-and-spoke fleet) rather than picked as a generic dark-mode default. A light theme is also implemented (`prefers-color-scheme`) for completeness. | 1.8 |
| 33 | 2026-07-22 | Scale-test worker identity: **`docker-compose.scale.yml` override** replacing the named `worker-identity-data` volume with an anonymous per-replica volume, used only via `-f docker-compose.yml -f docker-compose.scale.yml up --scale worker=N` | Modify the base `docker-compose.yml` itself to use an anonymous volume always | The base compose file's named volume is what makes identity persist across restart/recreation for normal single-worker dev (Phase 1.3's own verified exit criterion) — changing it there would silently undo that. An override file gets `--scale` working for the transient scale-simulation use case without touching the persistence behavior every earlier phase already demonstrated. Identity not persisting across a scaled replica's recreation is acceptable here: a scale simulation is a transient measurement, not a persistence test. | 1.9 |
| 34 | 2026-07-22 | Phase 1.9 scope: **stopped at 50 workers, by explicit user direction**, not a discovered bottleneck | Continue to the 100-worker level and its 10-minute no-heartbeat-loss soak in this same session | All containers stopped simultaneously right after the level-50 measurement. Read initially as a possible Docker Desktop resource-ceiling event and reported to the user as such (CLAUDE.md §16 — a benchmark-adjacent result contradicting the expected outcome is a stop-and-ask trigger, not something to push through). The user then clarified they had stopped the containers themselves, not realizing a test was mid-run — no technical bottleneck was actually found. The user accepted 50 workers as sufficient ("if it worked for 50 it is fine") rather than continuing to 100. Recorded honestly as a user scope call, not fabricated as a discovered limitation. | 1.9 |

---

## Measured Benchmarks

Record only measured numbers here. Recommendations belong in phase docs.

| Metric | Value | Conditions | Phase measured |
|---|---|---|---|
| SUSPECT transition latency | 13.66s elapsed since last heartbeat | Local Docker, worker frozen via `docker pause` (socket kept open), threshold 12s | 1.6 |
| OFFLINE transition latency (heartbeat-miss path) | 28.68s elapsed since last heartbeat | Local Docker, worker frozen via `docker pause`, threshold 25s | 1.6 |
| OFFLINE transition latency (transport-disconnect path) | ~1s after `docker kill -s SIGKILL` | Local Docker — OS closes the socket immediately, bypassing the heartbeat sweep entirely | 1.6 |
| Ping/pong round-trip latency | 1.6–1.7 ms | Local Docker, loopback network (coordinator and worker containers on the same host) | 1.6 |
| Memory reading accuracy | Manual calc 11.0% vs worker-reported 11.3% | `/proc/meminfo` inside worker container, few minutes apart | 1.6 |
| Reconnect backoff delays observed | 0.13s, 0.19s, 0.13s, 3.9s, 12.89s at `consecutive_failures` 0–4 | Local Docker, coordinator stopped 12s forcing repeated failures; each delay a random draw within its documented cap | 1.7 |
| Session-conflict eviction latency | <20ms from new handshake to old session's `session_superseded` log line | Local Docker, raw WS client opening a duplicate session | 1.7 |
| Network-partition detection and recovery | SUSPECT at 14.5s elapsed; recovered to ONLINE within ~1s of connectivity returning | Local Docker, `docker network disconnect`/`connect` on the worker container, ~17s partition window | 1.7 |
| Coordinator CPU/mem at 1 worker | 0.83% CPU, 80.75MiB | Local Docker, `docker stats --no-stream`, steady state | 1.9 |
| Coordinator CPU/mem at 5 workers | 0.58% CPU, 80.63MiB | Local Docker, steady state | 1.9 |
| Coordinator CPU/mem at 10 workers | 28.98% CPU (registration burst) settling to 13.97% CPU within 15s; 80.69MiB | Local Docker — burst reading taken immediately after scale-up, steady-state reading taken 15s later; both recorded rather than only the flattering one | 1.9 |
| Coordinator CPU/mem at 50 workers | 11.64% CPU, 84.77MiB | Local Docker, measured 25s after scale-up (settled) | 1.9 |
| Postgres active connections | 2 (levels 1/5/10), 6 (level 50) | `SELECT count(*) FROM pg_stat_activity`, coordinator's own SQLAlchemy pool — not one connection per worker (workers never touch Postgres directly) | 1.9 |
| Redis instantaneous ops/sec | 0 (level 1), 3 (level 5), 14 (level 10), 24 (level 50) | `redis-cli INFO stats`, point-in-time sample at each level | 1.9 |
| Worker-ID collisions | 0 at every level tested (1/5/10/50) | `SELECT count(*), count(DISTINCT id) FROM workers` — totals matched distinct counts exactly every time | 1.9 |
| Dashboard-reported worker count vs DB count | Matched exactly at every level tested (1/5/10/50) | `GET /api/workers` array length vs Postgres row count | 1.9 |

---

## Open Questions

| # | Question | Raised in | Blocking? | Resolution |
|---|---|---|---|---|
| 1 | Working tree had a large batch of previously-tracked files (coordinator/, README.md, docker-compose.yml, DECISIONS.md, SESSION_HANDOFF.md, pyproject.toml, etc.) showing as unstaged deletions in `git status`. `git show HEAD` revealed these were real prior-session progress (a coordinator skeleton that had reached that session's own "Step 4", with no design gate). | 1.0 | No — resolved | User chose to discard and build fresh under the current CLAUDE.md/PHASE_STATE.md process rather than recover the old code. Old commit remains in git history if ever needed. |

---

## Deviations From Guardrails

Any departure from `CLAUDE.md` logged here with recorded approval.
Empty is the correct state.

| # | Guardrail | Deviation | Approved by | Date |
|---|---|---|---|---|
| — | — | (none) | — | — |

---

## Current Blockers

1. **Branch protection on `main`** (Phase 1.1 exit criterion) has not
   been configured — it's a GitHub repository setting (Settings →
   Branches), not something this session can set from the local
   working tree without a GitHub token/`gh` CLI (neither available
   here). Needs to be done manually, or delegated with explicit
   authorization.

Resolved: **CI runs green on a pull request** — GitHub MCP was
reconnected (user updated the token's permissions to include "Pull
requests: write"; `mcp__github__get_me` and `mcp__github__list_pull_requests`
confirmed connectivity first). Existing local commit `5003ad9` (already
on `main`, not yet on `origin`) was pushed on a new branch
`phase-1.1-ci-verification` and PR #1 opened against `main`:
https://github.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM/pull/1.
Verified via the GitHub Actions API (not just PR UI): both the `lint`
and `build` jobs on the `pull_request`-triggered run
(id `29898975760`) completed with `conclusion: success`. PR left open,
not merged — merging was not requested. Also confirmed the discarded
2026-07-21 coordinator skeleton (see Open Question #1) is not present
in this commit or PR — `5003ad9` itself already deleted `shared/`, the
old `coordinator/api/*` tree, and all `__pycache__` artifacts; verified
nothing was left over in the working tree either.

---

## Next Action

**Phase 1.0** — design decisions recorded (Decisions Log #1–#7):
transport, identity, language/runtime, database, message envelope, and
(added during 1.1) local dev TLS termination approach.

**Phase 1.1** — repository/environment/CI skeleton built and verified
directly, not just claimed:
- `docker compose build` — all three custom images (coordinator,
  dashboard, worker) built clean.
- `docker compose up` — all 5 containers (postgres, redis, coordinator,
  dashboard, worker) reached `healthy`.
- `curl` against `https://localhost:8443/health` and
  `https://localhost:8444/health` (and `/`) succeeded, cert chain
  validated against `certs/dev-ca.crt`.
- Worker heartbeat file confirmed updating inside the running container.
- `ruff check` (same command CI runs) passed clean.
- `certs/` and `.env` confirmed gitignored; `.gitignore` was fixed —
  it previously ignored `.env.example` (the safe template) instead of
  `.env` (the real secrets file), inherited from the discarded prior
  attempt.
- `bash scripts/teardown.sh` — full clean teardown confirmed
  (containers, network, volumes removed).

Not yet done, and not something this session can complete: branch
protection and an actual green GitHub Actions run (see Current
Blockers above) — both require pushing to `origin`, which has not been
done and should not happen without being asked.

**Note:** a local commit (`5003ad9 "starting with new technique"`) was
created during this phase's work by what appears to be a plugin-level
auto-commit hook (not a `git commit` this session issued directly — no
hooks are visible in `.claude/settings.json` or
`.claude/settings.local.json`, so likely one of the "6 hooks" loaded by
a plugin at session start). It is local-only, not pushed to `origin`.
Flagged for user awareness, not treated as approval to push.

**Phase 1.2** — coordinator given real persistence, verified directly
against every exit criterion in `docs/phase-1-reliable-worker-network.md`:
- Migrations run automatically on coordinator startup via Alembic
  (`upgrade head` in the FastAPI lifespan) and are idempotent — verified
  by recreating the coordinator container against an already-migrated
  database and confirming no error, `alembic_version` unchanged.
- `workers` table schema verified via `psql \d workers` matches the
  spec exactly: id, credential_hash, registration_metadata, agent_version,
  status, revoked, created_at, updated_at.
- Redis connectivity verified live via `/ready`'s `PING` check; key
  naming convention (`worker:{worker_id}:{field}`) and TTL policy
  documented in `coordinator/app/redis_client.py` (no keys are actually
  written yet — that starts Phase 1.5+, so no TTL numbers are invented
  here per the zero-hallucination rule).
- `/health` (liveness) vs `/ready` (readiness) verified as genuinely
  different: stopped the postgres container, confirmed `/ready` returned
  503 with `{"database": "error: ..."}` while `/health` stayed 200;
  restarted postgres, confirmed `/ready` recovered to 200.
- Logs are valid JSON with `correlation_id` present — but this needed a
  real fix, not just a claim: Alembic's `fileConfig()` (called inside
  the migration runner) defaults to `disable_existing_loggers=True`,
  which silently disabled the coordinator's own logger and reset the
  root logger's level/handler. Fixed by passing
  `disable_existing_loggers=False` in `migrations/env.py` and
  reasserting `configure_logging()` after migrations run in
  `app/main.py`. Verified after the fix: every app-emitted log line
  (`coordinator starting`, `migrations applied`, `readiness check`) is
  valid JSON with a `correlation_id`. Third-party library log lines
  (uvicorn's own startup/access lines, Alembic's internal migration
  progress lines) remain plain text — that's those libraries' own
  logging config, out of scope for "every service emits structured
  logs," which is about services this project builds.
- Two coordinator replicas against the same Postgres/Redis verified by
  running a second, independent container (`docker run`, port 8543)
  alongside the compose-managed one (port 8443): both reached healthy,
  both served `/ready` as 200 concurrently, `alembic_version` stayed a
  single unconflicted row throughout.
- `ruff check` passed; full teardown via `scripts/teardown.sh` confirmed
  clean afterward.

**Phases 1.0, 1.1, and 1.2 approved by user on 2026-07-22.** Status
updated from `AWAITING APPROVAL` to `DONE` in the Phase Register and
Snapshot above.

**Phase 1.3** — worker registration and identity — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md` (see that file for full
verification notes per criterion): fresh registration, same-ID reuse
across restart and container recreation, new ID after identity
deletion, a copied-identity duplicate rejected with a documented and
scope-limited heuristic (Decisions Log #11), registration flood rate
limiting, and invalid-credential rejection that never creates a worker
record — all exercised live against the running stack, not just
written and claimed. Also verified beyond the written checklist:
same-machine double-launch caught by an `fcntl` file lock. `ruff check`
passed before and after; full teardown via `scripts/teardown.sh`
confirmed clean afterward.

New design decisions recorded: enrollment credential mechanism,
credential hashing scheme, duplicate-claim heuristic and its known
limitation, and rate-limit algorithm (Decisions Log #9–#12).

**Phase 1.4** — authentication and token lifecycle — hit a real scope
conflict before any code was written: its exit criteria as originally
drafted assume a live connection (Step 1.5) and a dashboard (Step 1.8),
neither of which exists yet. Raised to the user rather than worked
around silently; user chose to build and verify the HTTP-only subset
now and explicitly defer the rest (Decisions Log #13). Built and
verified against the running stack: short-lived opaque access tokens
minted via `/workers/token/refresh` against the Phase 1.3
`worker_credential`, verified via `/workers/token/verify`; proactive
refresh at half the token TTL confirmed via worker log timestamps 30s
apart; expired, malformed, and replayed tokens each rejected with a
distinct logged event; revocation confirmed instant against both
existing enforcement points (register/reaffirm, token refresh) with the
DB `revoked` flag verified via `psql`; grepped all logs for the raw
credential and both raw tokens used in testing — zero matches;
`credential_hash` confirmed a 64-char HMAC-SHA256 digest, not
plaintext, via direct `psql` inspection. See
`docs/phase-1-reliable-worker-network.md` for the exact criterion-by-
criterion verification notes, including which half of each criterion
is deferred and why. New design decisions: Decisions Log #13–#17.
`ruff check` passed before and after; full teardown confirmed clean.

**Phases 1.3 and 1.4 approved by user on 2026-07-22.** Status updated
from `AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot
above.

**Phase 1.5** — persistent connection transport — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md`: a new `/ws/connect` WebSocket
endpoint (handshake: Bearer access token in the `Authorization` header,
then a `hello` envelope naming worker ID and protocol version, acked
with the assigned session epoch), a Redis connection registry, an
app-level ping/pong keepalive, an on-demand push endpoint, and a worker-
side async WebSocket client replacing the old polling loop. New design
decisions: Decisions Log #18–#22.

- **30-minute hold**: verified live — the connection established at
  `08:15:38Z` was still on the same `session_epoch` (4, no drops, no
  reconnects) at `08:47:30Z`, **31 minutes 52 seconds** later. Registry
  TTL confirmed still refreshing throughout (spot-checked mid-window: 90
  of 90 seconds remaining right after a ping/pong cycle; 76 remaining at
  the final check, both consistent with the 20s ping interval never
  having missed a cycle).
- **Coordinator push on demand**: verified — `POST
  /workers/{id}/push` with `message_type: "test_push"` returned
  `{"status": "published", ...}`; the connected worker's own log shows
  `ws_message_received` with that `message_type` less than a second
  later.
- **Version-mismatched worker rejected**: verified — a raw WebSocket
  client (run from inside the worker container, `websockets` library
  already present) sent a `hello` with `protocol_version: "99.9"`
  against a real access token; coordinator replied with an `error`
  envelope (`reason: "ws_protocol_version_mismatch"`) and closed with
  code 4001, logged with both the expected and received version.
- **Connection registry visible and accurate**: verified via
  `redis-cli GET`/`TTL` on `worker:{id}:connection` — contents
  (`session_epoch`, `coordinator_instance`, `connected_at`) matched the
  live session at every check performed, including immediately after a
  reconnect (new session epoch, new `connected_at`, previous entry gone).
- **Graceful coordinator shutdown drains connections; workers reconnect
  cleanly**: verified via `docker compose restart coordinator` — the
  worker's log shows a clean WS close (code 1012, "service restart"),
  logged as `ws_connection_lost`, followed by a fresh token refresh and
  a new session (`ws_connected`, incremented `session_epoch`) roughly 3
  seconds later, matching `WORKER_WS_RECONNECT_DELAY_SECONDS`. See
  Decisions Log #21 for why this is uvicorn's own per-connection
  shutdown handling plus the existing disconnect cleanup, not a
  dedicated app-level drain step — an initial app-level implementation
  was built, verified to never execute with anything to do, and deleted
  rather than left as misleading dead code.
- **Connection survives an idle period with no traffic — keepalive
  verified**: the entire 30-minute hold above involved zero application
  traffic beyond the ping/pong keepalive itself — this is the same
  evidence as the 30-minute criterion, not a separate test.

**A real bug was found and fixed during this phase's own verification**,
not left for later: the worker's reconnect loop
(`_run_ws_forever`) originally left the token-refresh call outside the
function's `try`/`except`. The first `docker compose restart
coordinator` test reconnected fine, but a second restart (coordinator
image rebuild + recreate) exposed it — the worker's HTTP client hit a
connection-refused window while the new container was still starting,
`_post`'s bare `except urllib.error.HTTPError` didn't catch the
resulting `urllib.error.URLError`, the exception propagated out of an
un-awaited `asyncio.create_task`, and the whole reconnect loop died
silently (the worker container stayed `healthy` throughout, because the
heartbeat-file loop is a separate task unaffected by this). Root-cause
fixed by moving the token refresh inside the same `try`/`except` as the
connection itself and adding `urllib.error.URLError` to the caught
exception types; re-verified live afterward — the next coordinator
restart reconnected cleanly in ~3 seconds. Documented in the worker
module docstring and inline comment, not just here.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.5 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.6** — heartbeat and liveness detection — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md`: an application-level
`heartbeat` envelope sent over the existing Phase 1.5 socket every 5s
(sequence, uptime, agent version, CPU%, memory% — the last two read
from `/proc/stat`/`/proc/meminfo`, stdlib only, no new dependency),
coordinator-side latency measured off the existing ping/pong round
trip, a background liveness sweep independent of any single socket's
own transport timeout, and the full state machine (`REGISTERED ->
CONNECTING -> ONLINE -> SUSPECT -> OFFLINE`, plus `QUARANTINED`)
enforced through one status-transition helper that logs every move
with its trigger. New design decisions: Decisions Log #23–#27.

- **Heartbeats on schedule**: verified — `heartbeat_received` logged
  with an incrementing sequence roughly every 5s over several minutes.
- **Abrupt kill → SUSPECT → OFFLINE, timed**: verified two ways.
  `docker kill -s SIGKILL` closes the socket at the OS level
  immediately, so the coordinator saw a clean disconnect and went
  straight to `OFFLINE` in ~1s (correct, but bypasses the heartbeat
  state machine). `docker pause` (cgroup freezer, socket stays open)
  was used to exercise the heartbeat-miss path specifically: `SUSPECT`
  at 13.66s elapsed (threshold 12s), `OFFLINE` at 28.68s elapsed
  (threshold 25s), both logged with the trigger and elapsed seconds.
- **Skewed clock tracked correctly**: verified — a raw WS client sent a
  `heartbeat` with `timestamp: "2000-01-01T00:00:00Z"`; accepted
  normally, and the Redis-cached `last_heartbeat_at` reflected the
  coordinator's real receipt time, not the fake one.
- **CPU/memory accurate against the host**: verified — hand-computed
  `(1 - MemAvailable/MemTotal) * 100 ≈ 11.0%` from `/proc/meminfo`
  read directly in the container, matching the worker's own
  heartbeat-reported `11.3%` within normal drift.
- **Latency plausible**: verified for local-container only —
  `1.6`–`1.7 ms` round trip, consistent with loopback Docker
  networking. Differing-from-a-remote-machine half of this criterion
  is not yet testable (no worker outside local Docker exists until
  M1.5.8) — not claimed as done.
- **Every transition logged with its trigger**: verified across all
  seven transitions exercised this phase (see decisions log #27 for
  the one genuine bug found and fixed along the way — heartbeats on a
  socket opened *before* revocation were silently un-quarantining a
  worker; fixed by making `QUARANTINED` sticky, re-verified live).

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.6 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.7** — reconnection and session conflict — built and verified
against every exit criterion in
`docs/phase-1-reliable-worker-network.md` except one deliberately
deferred (see below): full-jitter exponential backoff replacing the
Phase 1.5 fixed 3s reconnect delay, and Redis-pub/sub-based session-
conflict eviction ensuring exactly one live session per worker ID at
all times. New design decisions: Decisions Log #28–#29.

- **Automatic reconnect after coordinator restart, no manual
  intervention**: verified — `docker compose stop coordinator` for 12s,
  then `start`; worker reconnected on its own.
- **Backoff intervals match policy**: verified — `consecutive_failures`
  0→1→2→3→4 logged with delays 0.13s/0.19s/0.13s/3.9s/12.89s, each
  inside its documented full-jitter cap.
- **Duplicate session → exactly one winner, loser terminated and
  logged**: verified, and more thoroughly than planned — a raw WS
  client opened a second session while the real worker's (epoch 1) was
  live; the real worker was evicted (`session_superseded` old=1 new=2),
  reconnected within its fast-reset backoff tier, and won back
  immediately (`session_superseded` old=2 new=3), evicting the raw
  client in turn. Exactly one authoritative session at every instant.
- **Session epoch increments and is visible**: verified —
  `worker:{id}:connection` in Redis showed `session_epoch: 3` matching
  the final winner.
- **Restarting the coordinator with 100 connected workers**: **deferred
  to Phase 1.9**, not tested here — see the phase doc's scope note.
  Scaling worker replicas today would just produce
  `duplicate_local_instance_detected` on N-1 of them, since every
  replica currently mounts the same named identity volume; giving each
  replica distinct identity storage is explicitly Phase 1.9's own exit
  criterion. Building that mechanism inside 1.7 would be scope creep
  into 1.9; faking it with a few manually-distinct containers wouldn't
  test what the criterion asks for at 100. Same kind of split as
  Decisions Log #13.
- **Network disconnect/reconnect restores the worker automatically**:
  verified via `docker network disconnect`/`connect` on the worker
  container (the standard proxy for "unplug the cable" in this
  environment, consistent with Phase 1.6's `docker kill`/`docker pause`
  proxies for other hardware failures). The Phase 1.6 heartbeat sweep
  caught the gap (`ONLINE→SUSPECT`, 14.5s elapsed) and recovered
  automatically the instant connectivity returned (`SUSPECT→ONLINE`,
  `heartbeat_received`) — no manual intervention, and the underlying
  socket itself never needed a fresh handshake to recover.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.7 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.8** — dashboard v1 — built and verified against every exit
criterion in `docs/phase-1-reliable-worker-network.md`: a new admin-
protected `GET /workers` on the coordinator merging the durable
Postgres row with Redis-cached metrics and connection registry, a
dashboard backend that proxies it server-side (the browser never sees
the admin secret), and a single-page live worker console. New design
decisions: Decisions Log #30–#32.

- **Design approach**: used the frontend-design skill rather than a
  generic table. Signature element is an animated hub-and-spoke glyph
  in the wordmark — a literal rendering of the star-topology invariant
  (CLAUDE.md §3.1), not decoration — set against a dark "radar/phosphor
  console" theme where the chroma comes from the five worker-state
  colors themselves (ONLINE/SUSPECT/OFFLINE/QUARANTINED/CONNECTING),
  deliberately avoiding the generic "dark background + one accent
  color" AI-default. A light theme is included via
  `prefers-color-scheme`. Reviewed visually via a local mock-data
  preview (Playwright couldn't trust the project's self-signed dev CA,
  so this was rendered separately over plain HTTP with synthetic
  multi-status data, same HTML/CSS/JS as shipped) — a real bug was
  caught this way: an invalid 4-digit hex color in the light theme
  (`--text-faint: #8299`) that silently broke footer text color; fixed
  before shipping.
- **Worker appears online within one heartbeat interval**: verified via
  `GET /api/workers` (the same endpoint the browser polls) — a fresh
  worker reached `ONLINE` within seconds of starting.
- **Killed worker turns offline within the documented timeout**:
  verified — `docker kill -s SIGKILL`, next `/api/workers` poll showed
  `OFFLINE` (near-instant, consistent with Phase 1.6's own finding that
  a hard kill closes the transport immediately).
- **Metrics update continuously**: verified — `sequence`, `cpu_percent`,
  `memory_percent`, `latency_ms`, `last_heartbeat_at` all changed across
  repeated polls of the same running worker.
- **Readable and responsive with 100 workers**: table layout verified
  responsive to a 390px mobile viewport (contained horizontal scroll,
  sticky header, tiles reflow) using a synthetic multi-status dataset —
  only a small number of real workers were available this session. The
  literal "100 workers" figure is Phase 1.9's own job, same scope split
  as the Phase 1.7 restart criterion — not fabricated here.
- **No credential or token rendered anywhere in the GUI**: verified —
  grepped the shipped HTML/JS for secret/credential/token, zero
  matches; the admin secret lives only in the dashboard backend's
  environment.
- **Closing and reopening the browser restores the live view**:
  structurally true — the page holds no client-side session state, a
  reload just re-fetches and renders fresh.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.8 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.9** — scale simulation — **partially complete, left `IN
PROGRESS`, not `DONE` or `AWAITING APPROVAL`**. New
`docker-compose.scale.yml` override solves the shared-identity-volume
problem that Phases 1.7 and 1.8 had each deferred to this phase (new
Decisions Log #33). Levels 1, 5, 10, and 50 workers were run and
measured cleanly:

- All four levels started successfully via the single documented
  command (`docker compose -f docker-compose.yml -f
  docker-compose.scale.yml up -d --scale worker=N`).
- Zero worker-ID collisions at every level tested — verified by direct
  Postgres query, not assumed.
- Every worker appeared on the dashboard (`GET /api/workers` count
  matched the DB row count exactly) at every level tested.
- Coordinator resource usage recorded as measured values in the
  Measured Benchmarks table above, including a burst-vs-steady-state
  distinction at the 10-worker level (28.98% CPU immediately after
  scale-up, settling to 13.97% within 15s) rather than reporting only
  the more flattering number.

**Level 100 and its 10-minute no-heartbeat-loss soak were not
attempted.** All containers stopped simultaneously right after the
level-50 measurement. This was initially reported to the user as a
possible Docker Desktop resource-ceiling event (CLAUDE.md §16: a
benchmark-adjacent result contradicting the expected outcome is a
stop-and-ask trigger). The user then clarified they had stopped the
containers themselves, unaware a test was mid-run — **no technical
bottleneck was actually found**, and this correction is recorded here
rather than leaving the earlier, wrong read as a documented finding
(Decisions Log #34). The user accepted the 50-worker level as
sufficient for this session rather than continuing to 100.

`ruff check` passed before and after; teardown confirmed clean
afterward (twice — once after the unexpected stop, once at the end of
the session).

**Phase 1.9 approved by user on 2026-07-22** — explicit sign-off
accepting the 50-worker level as sufficient. Level 100 and its
10-minute no-heartbeat-loss soak were not run and are waived, not
silently skipped — documented above and in Decisions Log #34. Status
updated from `IN PROGRESS` to `DONE` in the Phase Register and Snapshot
above. Current phase is now **1.10 — M1 demo and fresh-clone
verification**, `NOT STARTED`.
