# CLAUDE.md — Project Guardrails

Distributed AI-Orchestrated SQL Database Cleaning Platform
**Version 1 — Distributed Worker Network**

This file governs every task. Read it first.

---

## 1. What We Are Building

A **real, running, production-grade distributed worker network** over the
public Internet. Code is written, deployed, and tested. This is not a
documentation exercise.

This network is the **permanent foundation**. Every later capability —
distributed profiling, AI planning, execution, verification, reporting —
runs on this same coordinator, this same worker fleet, this same
protocol. It will not be rewritten in V2. Build it to carry that weight.

## 2. Hard Exclusions (V1)

Do not build, design, or scaffold:

- AI / LLM integration
- SQL profiling, ingestion, schema extraction
- Data cleaning, deduplication, semantic correction
- Verification engine, result assembly, reporting service
- Customer-facing UI, billing, multi-tenancy
- CSV, Excel, PDF, image, OCR, NoSQL support
- Direct worker-to-worker communication
- ML-based scheduling

Task payloads in V1 are dummy workloads only: count to N, hash rounds,
fixed sleep, opaque byte payload. If a task requires an excluded item,
**stop and ask**.

## 3. Architectural Invariants

Not negotiable without written approval:

1. **Star topology.** One coordinator cluster, N workers. Workers never
   talk to each other or learn other workers exist.
2. **Workers are dumb.** Connect, authenticate, receive, execute,
   return, heartbeat, reconnect. Zero scheduling decisions.
3. **All intelligence lives in the coordinator.**
4. **Workers dial out.** No inbound ports on workers, ever. This is what
   makes home Wi-Fi, NAT, corporate proxies, and mobile hotspots work.
5. **One protocol, all environments.** The coordinator cannot tell a
   Docker worker from a VPS worker from a laptop on a hotspot. Same
   handshake, same auth, same messages, same code path.
6. **Workers are stateless between tasks.** Killing any worker loses no
   coordinator state.
7. **Idempotency is mandatory.** Duplicate result submission is a no-op.
8. **TLS everywhere**, including local Docker.
9. **The coordinator is horizontally scalable.** No coordinator instance
   holds authoritative state in memory. Any instance can serve any
   worker. This is a design property from Phase 1, not a later retrofit.

## 4. Technology Stack

| Layer | Technology |
|---|---|
| Containers | Docker, Docker Compose (local dev) |
| Orchestration | Kubernetes (staging and production) |
| Infrastructure | Terraform (all cloud resources, no console clicks) |
| CI/CD | Git + GitHub Actions |
| Durable state | Relational database with migrations |
| Ephemeral state, cache, queue, pub/sub | Redis |
| Transport | Persistent bidirectional connection over TLS |
| Dashboard | Real-time web GUI |
| Observability | Structured JSON logs, metrics, dashboards, alerts |
| Load testing | Scripted, repeatable, versioned |

**Sequencing rule:** Docker Compose is the local development environment
from Phase 1 onward and never goes away. Kubernetes and Terraform are
introduced in a dedicated deployment phase — real and in scope, but
after the distributed system exists. Do not bury Phase 1 in
infrastructure before a single worker connects.

## 5. Milestones

| Milestone | Delivers | Not before this |
|---|---|---|
| M1 | Registration, auth, identity, heartbeat, reconnect, live GUI | — |
| M1.5 | Terraform + Kubernetes + CI/CD, public Internet deployment | M1 |
| M2 | Task distribution pipeline, dummy workloads | M1.5 |
| M3 | Fault tolerance, reassignment, dedup, recovery | M2 |
| M4 | Capability reporting, rule-based adaptive scheduling | M3 |

**Stop at M4.** Do not plan or scaffold M5+.

## 6. GUI Requirement

The dashboard is a **first-class deliverable in every phase**, not a
final-phase add-on. After each phase the user must be able to open a
browser and watch that phase's behaviour happen live.

The dashboard is an engineering and debugging interface, not a customer
product. It must show, updating in real time: connected workers, worker
IDs, status, CPU usage, memory usage, network latency, last heartbeat,
current task, task history, queue size, failed workers, completed tasks,
running tasks. Scheduling decisions are added in M4.

It must remain usable and readable with 100 workers listed.

## 7. Self-Testability Requirement

Every phase ends in something **the user can run and verify personally**,
without the author present.

Every phase document must state:
- What can be demonstrated
- What is visible on screen
- How success is verified
- How failure is deliberately triggered and demonstrated
- What screenshots or video are capturable
- What logs are visible

A phase with no runnable demo is not done.

## 8. Internet Testing Requirement

Local Docker is the starting point, never the end point. Every phase
from M1.5 onward must be verified with **at least one worker running
outside the local network** — a VPS, another laptop, a friend's machine,
or a mobile hotspot.

The coordinator does not change between environments. Only worker
configuration changes: coordinator URL, enrollment credential, CA trust.

## 9. Working Discipline

- **One change at a time.** One concern per change and per commit.
- **Phase-gating with stop-and-wait.** Finish a phase, present exit
  criteria evidence, **stop and wait for approval.**
- **Update `PHASE_STATE.md` at every phase transition.**
- **Short architecture gate, then build.** Each phase opens with a brief
  design decision step, then produces running software. Design does not
  consume the phase.
- **Compare then recommend.** Where valid alternatives exist, state
  them, state trade-offs, recommend one, justify it.
- **Challenge assumptions.** Do not agree by default.

## 10. Zero-Hallucination Rule

Non-negotiable:

- Never invent a metric, timeout, port, schema field, or endpoint not
  present in the spec or `PHASE_STATE.md`.
- Never claim something is implemented, deployed, or tested when it is
  not.
- Label recommended values as recommendations, measured values as
  measurements. Never blur the two.
- Report benchmark results honestly, including unfavourable ones.
- If information is missing, say so. Do not fill the gap.

## 11. Engineering Standards

- Every service emits structured JSON logs: timestamp, service, level,
  worker ID or task ID, event name, correlation ID.
- Every task and every worker session is traceable end to end by a
  single correlation ID.
- Every service exposes health and readiness endpoints.
- Database changes ship as versioned migrations. No manual schema edits.
- Secrets come from a secret manager or Kubernetes secrets. Never in
  Git, never in an image, never in a log.
- Every phase adds tests: unit, integration, and load where relevant.
- CI must pass before merge from M1.5 onward.

## 12. Security Baseline

- TLS everywhere.
- Bootstrap enrollment credential, then short-lived access token plus
  long-lived refresh credential.
- Credentials hashed at rest; never logged, never rendered in the GUI.
- **Every worker is untrusted.** Assume any worker may return wrong
  results, replay old messages, lie about its capabilities, or attempt
  mass fake registration.
- Rate-limit registration. Support revocation and quarantine of any
  worker ID with a bounded time to effect.
- Anything the scheduler trusts heavily must be **coordinator-observed**,
  not worker-reported.

## 13. Fresh-Clone Rule

At every milestone boundary the project must run from a fresh clone with
a documented command sequence and zero undocumented manual steps. If it
does not, the milestone is not complete.

## 14. Documentation Rule

- `CLAUDE.md` — this file. Under 300 lines.
- `PHASE_STATE.md` — current state. Updated every transition.
- `docs/phases/phase-N-*.md` — one per phase, small numbered steps with
  objectively verifiable exit criteria.
- Architecture reasoning lives in phase documents, not in code comments.

## 15. Definition of Done (every phase)

1. Every step complete.
2. Every exit criterion objectively verified, not asserted.
3. Demo performed end to end by the user personally.
4. Failure demo performed end to end.
5. Tests passing in CI (from M1.5 onward).
6. Runs from a fresh clone.
7. `PHASE_STATE.md` updated.
8. Approval given to proceed.

## 16. Escalation Triggers

Stop and ask when:

- A requirement conflicts with an invariant in section 3.
- A phase needs something from a later milestone.
- A design decision would make the coordinator distinguish Docker
  workers from Internet workers.
- Scope appears to expand beyond M1–M4.
- An exit criterion cannot be objectively verified as written.
- A benchmark result contradicts the expected outcome.
- Do not proceed to the next phase or the next step in the phase withouth my permission ask permission and then start 