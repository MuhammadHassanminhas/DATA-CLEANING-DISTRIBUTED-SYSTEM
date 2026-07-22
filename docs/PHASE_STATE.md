# PHASE_STATE.md

Single source of truth for project status.
Update at every phase transition. Never leave stale.

---

## Snapshot

| Field | Value |
|---|---|
| Project | Distributed AI-Orchestrated SQL Database Cleaning Platform |
| Scope in progress | Version 1 — Distributed Worker Network |
| Current milestone | M1 — Reliable Worker Network |
| Current phase | 1.0 — Design gate — protocol and identity decisions |
| Phase status | AWAITING APPROVAL |
| Last updated | 2026-07-22 |
| Approval gate | Awaiting approval of Phase 1.0 decisions before Phase 1.1 |

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
| 1.0 | Design gate — protocol and identity decisions | AWAITING APPROVAL |
| 1.1 | Repository, environment, CI skeleton | NOT STARTED |
| 1.2 | Coordinator service skeleton and data stores | NOT STARTED |
| 1.3 | Worker registration and identity | NOT STARTED |
| 1.4 | Authentication and token lifecycle | NOT STARTED |
| 1.5 | Persistent connection transport | NOT STARTED |
| 1.6 | Heartbeat and liveness detection | NOT STARTED |
| 1.7 | Reconnection and session conflict | NOT STARTED |
| 1.8 | Dashboard v1 — live worker view | NOT STARTED |
| 1.9 | Scale simulation 1 → 100 workers | NOT STARTED |
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

---

## Measured Benchmarks

Record only measured numbers here. Recommendations belong in phase docs.

| Metric | Value | Conditions | Phase measured |
|---|---|---|---|
| — | — | — | — |

---

## Open Questions

| # | Question | Raised in | Blocking? | Resolution |
|---|---|---|---|---|
| 1 | Working tree has a large batch of previously-tracked files (coordinator/, README.md, docker-compose.yml, DECISIONS.md, SESSION_HANDOFF.md, pyproject.toml, etc.) showing as unstaged deletions in `git status`. Intentional cleanup, or accidental loss to confirm before Phase 1.1 recreates the repo skeleton? | 1.0 | Yes — blocks Phase 1.1 | Open |

---

## Deviations From Guardrails

Any departure from `CLAUDE.md` logged here with recorded approval.
Empty is the correct state.

| # | Guardrail | Deviation | Approved by | Date |
|---|---|---|---|---|
| — | — | (none) | — | — |

---

## Current Blockers

None.

---

## Next Action

Phase 1.0 design decisions recorded above (Decisions Log #1–#6), covering
transport protocol, identity model, language/runtime, database, and
message envelope. Message envelope specified with `session_epoch`
included per exit criteria.

Await approval of these decisions, and resolution of Open Question #1,
before starting **Phase 1.1 — Repository, environment, CI skeleton**.
