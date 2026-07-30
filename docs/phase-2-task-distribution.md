# Phase 2 — Task Distribution (Milestone 2)

**Goal:** Build the task execution pipeline. The coordinator queues
tasks, assigns them to workers, workers execute and return results, the
coordinator marks them complete — visible end to end on the dashboard,
tested locally and across the public Internet.

**Task payloads:** dummy workloads only — count to N, hash rounds, fixed
sleep, opaque byte payload. No SQL, no AI.

**Out of scope:** Failure handling, reassignment, retries, timeout
recovery. Those are Phase 3. Here the happy path must work, be
observable, and be fast. Assignment is deliberately naive — capability
awareness is Phase 4.

**Prerequisite:** Phase 1.5 complete and approved.

---

## Step 2.0 — Design gate

Decide and record:

- **Queue technology.** Compare Redis Streams, Redis lists, a relational
  table used as a queue, and a dedicated broker. Evaluate on durability,
  ordering, dashboard visibility, operational weight, and the Phase 3
  requirement to reclaim expired leases. Recommend one with reasoning.
- **Pull versus push.** Compare worker-pull against coordinator-push over
  the existing connection. Evaluate on backpressure, idle-worker
  latency, assignment races, and behaviour when a push lands on a worker
  that is about to disconnect. A hybrid is acceptable if justified.
- **Result storage.** Where results live and their retention period.
- **Task type extensibility** so new types are added without a protocol
  change.

**Exit criteria**
- [x] Both major decisions recorded with alternatives and reasoning.
- [x] Reuses the Phase 1 message envelope unchanged.
- [x] Approved before code.

**DECIDED 2026-07-28 — Decisions #79–#82 in `PHASE_STATE.md`.**

- **Queue technology → PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED`** on
  the task table. Redis was rejected on reliability, not preference:
  `infra/helm/platform/templates/redis.yaml` runs Redis with no PVC and
  no persistence by Decision #39, and unlike claims/registry/metrics a
  lost queue is rebuilt by nothing. Adding AOF would not save it — a
  Redis queue entry and a Postgres task row cannot commit in one
  transaction, so the dual-write can lose or duplicate a task against
  §3.7. Postgres makes dequeue + `QUEUED→ASSIGNED` + lease stamp one
  transaction on one row, and gives M3 lease reclaim as plain SQL.
- **Pull versus push → hybrid.** Worker declares `max_concurrent` at
  `hello` and emits `capacity` when a slot frees; the coordinator pushes
  only against a free credit, over the existing pub/sub push path. Pure
  pull would make the worker choose its own work, contradicting §3.3 and
  forcing a rewrite at M4.
- **Result storage → separate `task_results` table**, recommended 64 KB
  cap and 7-day body retention (recommendations, not measurements). Kept
  off the task row because that row is now the queue's hot path.
- **Task type extensibility → coordinator-side type registry.** Envelope
  unchanged, `PROTOCOL_VERSION` stays `1.0`; new `message_type` values
  only, per Decision #6.

---

## Step 2.1 — Task model, schema, state machine

- Task table via migration: task ID, type, payload, parameters,
  priority, created timestamp, assigned worker, assignment timestamp,
  lease expiry, attempt count, status, result reference, completion
  timestamp, correlation ID.
- Task state machine enforced in code: `QUEUED → ASSIGNED → RUNNING →
  COMPLETED`, with terminal `FAILED` and `CANCELLED`. The `REASSIGNED`
  transition is reserved and unused until Phase 3.
- The four dummy task types with validated parameter shapes.
- **All state transitions are coordinator-authoritative.** A worker never
  writes task state directly.

**Exit criteria**
- [ ] Migration applies cleanly and is reversible.
- [ ] Invalid state transitions are rejected in code — verified by test.
- [ ] Each of the four task types validates its parameters and rejects
      malformed input.
- [ ] Lease expiry and attempt count columns exist now, unused, so
      Phase 3 needs no schema change.
- [ ] Every task carries a correlation ID from creation.

---

## Step 2.2 — Durable task queue

> Renamed from "Redis-backed queue" after the Step 2.0 gate chose
> PostgreSQL (Decision #79). The old title pre-judged a choice Step 2.0
> explicitly left open.

- Queue implementation per the Step 2.0 decision.
- Atomic dequeue — two coordinator replicas must never hand out the same
  task.
- Queue depth readable cheaply for the dashboard.
- Priority support or an explicit, justified deferral.
- Queue state survives coordinator restart.

**Exit criteria**
- [ ] 10,000 tasks enqueue and dequeue without loss — counted.
- [ ] Three coordinator replicas dequeuing concurrently never produce a
      duplicate assignment — verified under load.
- [ ] Queue depth accurate and cheap to read.
- [ ] Restarting all coordinator replicas loses no queued task.
- [ ] Ordering guarantee stated and verified by test.

**Ordering guarantee, as built.** A single dequeuer receives tasks in
strict `(priority ASC, created_at ASC)` order — `priority` is
lower-is-more-urgent, so both keys ascend and one index direction serves
the whole clause. Under N concurrent dequeuers the order is *not*
globally total: `SKIP LOCKED` is what buys the concurrency, so a
dequeuer stepping over a locked row may claim a slightly later task
first. What holds under concurrency is that no task is handed out twice,
none is lost, and none is starved. Tasks enqueued inside one transaction
share a `created_at` (Postgres `now()` is the transaction timestamp) and
have no defined order among themselves.

**How to verify it yourself.** `scripts/queue_harness.py` is the
versioned harness; it decides pass/fail itself rather than leaving
numbers to be eyeballed.

```bash
# Local — three coordinator processes against one Postgres.
export COORDINATOR_URL=https://127.0.0.1:18443,https://127.0.0.1:18444,https://127.0.0.1:18445
export ADMIN_SECRET=$ENROLLMENT_SECRET
python scripts/queue_harness.py verify --count 10000 --dequeuers 3 --insecure

# AKS staging — one Service URL, so the replicas are real pods. Run it
# in-cluster: the public ingress rate-limits to a few requests per second,
# which would measure nginx rather than the queue.
kubectl -n staging run queue-harness --rm -i --restart=Never \
  --image=python:3.12-slim \
  --env=COORDINATOR_URL=https://coordinator:8443 \
  --env=ADMIN_SECRET=<enrollment secret> \
  --command -- python - verify --count 10000 --dequeuers 3 --insecure \
  < scripts/queue_harness.py
```

`by_coordinator_instance` in the output names the pods that served the
claims — that is the evidence three replicas took part, rather than an
assumption drawn from three pods being Running. Locally the processes
share a hostname, so `by_target` carries the same evidence instead.

Restart criterion: read `python scripts/queue_harness.py depth`, kill
every coordinator, bring them back, read depth again.

---

## Step 2.2.1 — Operator credential separation

Inserted between 2.2 and 2.3 rather than folded into either. It is a
security fix with its own failure demo, and burying it inside the
assignment engine would put two unrelated concerns in one change.

**The problem.** `verify_admin_secret` compared against
`ENROLLMENT_SECRET` — the *shared* bootstrap credential every worker holds
by design (Decision #76 B1). So every worker could call every admin
endpoint: list the whole fleet, revoke or push to any peer, and, once
Step 2.2 landed, drain the entire task queue and self-assign all of it.
CLAUDE.md §12 says every worker is untrusted; that was not true of the
admin surface.

This predates Step 2.2 — `/workers/{id}/revoke` has had it since Phase
1.4, and `config`'s own docstring flagged it as deferred. Step 2.2 is what
made it matter enough to fix: the blast radius went from "grief your
peers" to "take all the work", and it stops being bounded once Step 2.4
executes real work.

- Distinct `ADMIN_SECRET`, never given to a worker.
- Falls back to `ENROLLMENT_SECRET` when unset, so an image can roll out
  before the Secret is applied, but announces the fallback at WARNING and
  exports the posture as a metric — insecure-but-silent is not an option.
- The dashboard is an operator tool and carries the operator credential;
  the worker does not.
- Sealed Secrets committed for both namespaces (§13, Decision #78).

**Exit criteria**
- [ ] A worker's enrollment secret is rejected by every admin endpoint.
- [ ] The operator credential is accepted by every admin endpoint.
- [ ] Workers can still enroll, connect, and heartbeat — the split does
      not lock the fleet out.
- [ ] The dashboard still reads the fleet.
- [ ] A deployment without `ADMIN_SECRET` still serves, and says so.
- [ ] The posture is observable without reading a log line.

**How to verify it yourself**

```bash
# Must be 401 — this is the whole point of the step.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "x-admin-secret: $ENROLLMENT_SECRET" "$BASE/tasks/depth"

# Must be 200.
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "x-admin-secret: $ADMIN_SECRET" "$BASE/tasks/depth"

# Must still be 201 — workers are not locked out.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/workers/register" \
  -H 'Content-Type: application/json' \
  -d "{\"enrollment_secret\":\"$ENROLLMENT_SECRET\",\"agent_version\":\"check\"}"
```

Posture, per replica, without touching a log:
`coordinator_admin_credential_separate` — 1 means separated, 0 means the
fallback is active and every worker can still call the admin endpoints.

---

## Step 2.3 — Assignment engine

- Assignment logic per the Step 2.0 pull/push decision.
- Atomic assignment: a task moves to exactly one worker.
- Eligibility filter — only online workers supporting the task type.
- Idle policy when no work is queued.
- Waiting policy when no worker is available.
- Assignment acknowledgement from the worker.
- Assignment events logged with the task correlation ID.

**Exit criteria**
- [x] A task reaches a worker and is acknowledged.
- [x] No task is ever assigned to two workers — verified with 500 tasks
      across 50 workers.
- [x] 100 idle workers with an empty queue produce negligible load —
      measured.
- [x] A queued task with no eligible worker stays queued and visible, not
      silently dropped.
- [x] A worker disconnecting between assignment and acknowledgement is
      logged. Recovery is Phase 3; detection exists now.
- [x] Assignment works identically for local Docker workers and remote
      Internet workers.

**DECIDED 2026-07-30 — Decisions #89–#92 in `PHASE_STATE.md`.**

- **One engine per replica, assigning only to the sockets that replica
  holds** (#89). No leader election: two replicas cannot hand out the
  same task because the claim is `FOR UPDATE SKIP LOCKED` on one row in
  one transaction, so correctness is the queue's property, not the
  scheduler's. Delivery goes straight down the socket rather than through
  the `worker:{id}:push` channel — the assigning replica always holds the
  socket, so the Redis hop would buy nothing.
- **Event-driven, with a slow safety net** (#90). An enqueue publishes to
  `tasks:available` and every replica wakes; a per-replica poll (default
  30s) covers a notification Redis dropped. A pass checks queue depth
  **once** before looking at any worker, which is what keeps the idle
  cost flat in fleet size.
- **Commit before send** (#91). A task is durably `ASSIGNED` before a
  byte reaches the worker, because a stranded-but-recorded task is
  recoverable by Phase 3 and an unrecorded one is not.
- **Declared capabilities are sanitised, not trusted** (#92). Credits are
  clamped, unknown types dropped, and "eligible for nothing" is never
  widened into "eligible for everything". An ack means *received*, not
  *started* — `ASSIGNED -> RUNNING` belongs to Step 2.4.

**New wire messages.** `task_assign` (coordinator to worker), `task_ack`
and `capacity` (worker to coordinator). The envelope is untouched and
`PROTOCOL_VERSION` stays `"1.0"` — new `message_type` values only, which
is the extension route Decision #6 specified. `capacity` is handled now
but nothing emits it until Step 2.4, since nothing finishes a task yet.

**Verification** — `scripts/assignment_harness.py`, versioned:

```bash
# 500 tasks across 50 workers, each with 10 credits.
python scripts/assignment_harness.py fanout --url https://localhost:18443 \
  --workers 50 --tasks 500 --max-concurrent 10 --insecure \
  --enrollment-secret "$ENROLLMENT_SECRET" --admin-secret "$ADMIN_SECRET"

# 100 idle workers, empty queue: samples the coordinator's own /metrics.
python scripts/assignment_harness.py idle --url https://localhost:18443 \
  --workers 100 --seconds 60 --insecure \
  --enrollment-secret "$ENROLLMENT_SECRET" --admin-secret "$ADMIN_SECRET"

# One worker that takes a task and vanishes without acknowledging —
# the deterministic way to produce a millisecond-wide race.
python scripts/assignment_harness.py stranded --url https://localhost:18443 \
  --insecure --enrollment-secret "$ENROLLMENT_SECRET" \
  --admin-secret "$ADMIN_SECRET"
```

Unlike `queue_harness.py` this one needs the `websockets` package, so run
it from a venv with the worker's requirements or from the worker image.
Its simulated workers are real registered identities with real WebSocket
sessions — indistinguishable from containers to the coordinator (§3.5) —
but they are **sessions from one process, not separate machines**, and
the measurements should be read that way (§10).

---

## Step 2.4 — Worker execution runtime

- Task executor implementing all four dummy types.
- Execution isolated from the connection loop — **heartbeats continue
  during long task execution.** A busy worker must never be mistaken for
  a dead one.
- Progress reporting at a defined interval.
- Configurable concurrency: how many tasks a worker runs at once.
- Local task state deleted after submission.
- Refusal path for an unsupported task type.

**Exit criteria**
- [ ] All four task types execute correctly and return correct results.
- [ ] A 10-minute task runs to completion while heartbeats continue
      uninterrupted — verified on the dashboard. **Two parts, both
      required (Decisions #99–#100, option (c), user-chosen 2026-07-30):**
  - [ ] **(1) The letter.** One `sleep` task with `seconds: 600` runs to
        completion, holding a slot for the full ten minutes, heartbeats
        uninterrupted.
  - [ ] **(2) The substance.** The worker is additionally held
        **CPU-saturated for ≥600s** by repeated `hash_rounds` tasks at the
        existing 10,000,000 ceiling, heartbeats uninterrupted throughout.
        A sleeping task consumes no CPU, so part (1) alone cannot show
        that a *busy* worker keeps heartbeating — which is the only thing
        this criterion is really about.
  - **Pass condition for both:** ≥600s of continuous execution, **zero**
    transitions out of `ONLINE` in the coordinator's
    `worker_state_transition` log (no SUSPECT, no OFFLINE), and no
    observed heartbeat gap above the 12s SUSPECT threshold.
  - **Sizing:** ~40 ceiling-parameter tasks covers 600s *as measured on
    this laptop's Docker at one CPU* (15.4s each). **Re-derive the count
    on the AKS worker from a fresh measurement — do not copy 40**, since
    it is a property of the machine, not of the criterion.
  - No parameter ceiling is raised and no Step 2.1 artefact is changed.
- [ ] Progress updates appear during execution.
- [ ] Concurrency limit is respected and configurable.
- [ ] Worker deletes temporary state after submission — verified.
- [ ] Unsupported task type is refused cleanly, not crashed on.
- [ ] Worker memory stays flat across 1,000 sequential tasks — no leak.

### Design sub-gate — ACCEPTED WITH AMENDMENTS 2026-07-30

Decisions #93–#104 in `PHASE_STATE.md`. **Implementation is authorised but
has NOT begun** — held by explicit user instruction.

The §16 escalation this gate raised (#99, the 10-minute criterion) is
resolved as option (c), recorded as #100 and folded into the exit criteria
above.

**Review outcome (#104): accepted with amendments, not as-is.** The
approval was delegated to the agent by the user, so — stated plainly
because it limits what the approval is worth — **this is self-review, not
independent validation.** It was run against the shipped code rather than
against the gate's own prose, and it found one real defect and one
omission:

- **#101 supersedes #97, which was wrong.** #97 let a worker refuse an
  assignment it had no local capacity for. But Step 2.3 built the refusal
  path for a *rare* cause (unsupported type, which the eligibility filter
  makes nearly unreachable), and it **frees the credit and rings the
  doorbell** (`assignment.py:287-298`), while `assign_once` picks any
  session with `free_credits > 0` (`assignment.py:398-400`) and a refusal
  leaves the task `ASSIGNED` with no state write. Introducing a *frequent*
  refusal cause therefore livelocks: assign → refuse → credit freed →
  doorbell → assign, **permanently stranding every task in `ASSIGNED` at
  loop speed**, with no Phase 3 in M2 to reclaim them. Against staging's
  ~20.6k queued rows that strands the queue in seconds. **Fix:** a
  capacity refusal *saturates* the credit instead of freeing it, only the
  worker's `capacity` message reopens it, only an unsupported-type refusal
  frees one, and `hello` carries `tasks_in_flight` so a reconnecting
  worker's running work is visible up front.
- **#102 closes a gap the gate simply never addressed:** what happens when
  an executor raises. A new `task_failed` message moves the task to
  `FAILED` (already reachable from `ASSIGNED` and `RUNNING`) and frees the
  credit. Without it a crashed task would sit `RUNNING` forever in M2.
  Carries the exception type, **never a traceback** — tracebacks can
  contain payload data (§12).
- **#103** states explicitly that 2.4 adds **no** execution timeout:
  duration is already bounded by Step 2.1's parameter validation, and
  `lease_expires_at` must stay written-by-nothing through all of M2.

#93–#96, #98 and #100 survived review unchanged.

Every figure below was measured with `scripts/exec_isolation_bench.py`
inside `python:3.12-slim` — the worker's actual base image — constrained
to **one CPU**, because that is the shape of a CPU-limited container on
the `Standard_B2s_v2` node. Nothing here is a recommendation dressed as a
measurement (§10); the values that *are* recommendations say so.

- **Execution runs in `asyncio.to_thread`, one thread per running task**
  (#93). The problem is real and was measured, not assumed: run a
  CPU-bound task inline on the event loop and **the heartbeat gap becomes
  the entire task duration** — a 24.58s task emitted exactly *one*
  heartbeat where 4.9 were due, breaching the 12s SUSPECT threshold, and
  any CPU task over 25s breaches OFFLINE too. The coordinator would
  declare a healthy worker dead. With `to_thread` the gap held at
  **5.01s** (1 task), **5.19s** (4 tasks), **5.47s** (4 × `count_to_n`,
  the harsher pure-Python case that holds the GIL between bytecodes) and
  **5.65s** at 8 tasks — never closer than 6.3s to SUSPECT.
- **`ProcessPoolExecutor` was measured and lost** (#93). It is the
  textbook answer for CPU-bound Python and it is the wrong one here:
  **68.32s against the threads' 58.04s** for the same 4 × 15s of work
  (0.85x; a second run gave 0.75x), with no heartbeat advantage. A
  CPU-limited container has no parallelism to win, so all it buys is IPC,
  pickling and four extra interpreters.
- **Honest ceiling, stated because it is easy to oversell threads:** this
  buys heartbeat survival, **not throughput**. Four concurrent CPU tasks
  on one core measured **1.03x** against running them serially — that is
  nothing. In V1 throughput comes from more workers, not more threads per
  worker.
- **Executors are chunked loops carrying a progress slot and a cooperative
  cancel flag** (#94). A Python thread cannot be killed, so cancellation
  is cooperative or it does not exist; measured, the flag stops a running
  task in **0.04–0.11s**. Progress needs no cross-thread machinery at
  all — a one-slot list written by the thread and read by the loop is
  atomic under the GIL, and gave 20 distinct monotonic samples. Chunking
  is free: sizes from 1,000 to 500,000 all landed inside run-to-run noise
  (−5.5% to +3.2%), so that spread is noise and **not** a speedup from
  chunking.
- **`ASSIGNED -> RUNNING` is driven by an explicit `task_started`
  message** (#95), not inferred from the first progress report. A state
  transition and a telemetry sample have to stay separable, because Phase
  3 will want to throttle or drop progress under load and must never drop
  a state transition.
- **Concurrency is enforced worker-side by an explicit semaphore and a
  `ThreadPoolExecutor` sized to `WORKER_MAX_CONCURRENT`** (#96) — never
  the default `to_thread` pool. A measured trap: `os.cpu_count()`
  returned **4 inside a `--cpus=1` container**, so a cgroup quota is
  invisible to it, and the default pool is `min(32, cpu_count+4)` = **8**,
  smaller than the 64 `WORKER_MAX_CONCURRENT_CEILING` allows. A worker
  accepting 64 tasks would silently queue 56 *inside the executor* —
  acknowledged, apparently running, not started.
- **In-flight work survives a reconnect; over-commit is refused, never
  queued locally** (#97, **amended by #101 — read #101, not this line, for
  the refusal mechanics; #97 as written livelocks**). Invariant: **the
  worker never holds a task it is not executing.** A local backlog would
  be the worker making a scheduling decision, against §3.2/§3.3.
- **2.4 computes results and discards them** (#98). Step 2.5 owns the
  result envelope, persistence and retry; a buffer built now would be half
  of 2.5 with none of its design, and a buffer is exactly what breaks the
  flat-memory criterion. Measured: 300 sequential tasks moved RSS
  **28.4 → 28.4 MB (+0.1)**.

**New wire messages.** `task_started` and `task_progress` (worker to
coordinator). `capacity` already exists and is already handled — Step 2.3
built the coordinator side, and 2.4 is what finally emits it. The envelope
is untouched and `PROTOCOL_VERSION` stays `"1.0"`.

**Progress cadence.** One reporter task **per worker, not per task**,
emitting one `task_progress` per running task every
`WORKER_PROGRESS_INTERVAL_SECONDS` (recommended 10s, not measured) and
suppressing unchanged values. The coordinator keeps the latest sample in
Redis beside the existing worker-metrics hash rather than writing
Postgres per sample: a DB write per task per interval would put avoidable
write load on the `tasks` table, which under Decision #79 *is* the queue's
hot path. The dashboard reads it for §6's "current task".

**Reproducing the evidence:**

```bash
docker run --rm -i --cpus=1 -v "$PWD:/bench:ro" python:3.12-slim \
  python /bench/scripts/exec_isolation_bench.py all
```

`psutil` is absent from the bare image, so the RSS figures report `NaN`
unless it is installed first; the memory number above came from a run with
`pip install 'psutil>=5.9,<7'` ahead of it.

---

## Step 2.5 — Result submission and completion

- Result envelope: task ID, attempt number, session epoch, status,
  result payload, execution duration, idempotency token.
- Coordinator validates and persists results, then transitions the task
  to completed.
- Submission retry with backoff if the coordinator is briefly
  unreachable.
- Result storage with the documented retention period.

**Exit criteria**
- [ ] Results persist and tasks reach completed.
- [ ] **Idempotency token and session epoch are present in every result
      from day one**, even though enforcement is Phase 3 — so Phase 3
      requires no protocol change.
- [ ] A worker that finishes during a brief coordinator outage retries
      and succeeds on reconnect.
- [ ] Malformed results are rejected without corrupting task state.
- [ ] Execution duration recorded and visible.
- [ ] Large result payloads handled without breaking the connection.

---

## Step 2.6 — Operator task APIs

- Submit a task, submit a batch, list tasks with filters, inspect one
  task with full history, cancel a queued task.
- Authenticated and rate limited.
- No operator ever needs to touch the database directly.

**Exit criteria**
- [ ] All operations work via the API.
- [ ] Batch submission of 1,000 tasks succeeds.
- [ ] Task inspection returns the full lifecycle with timestamps.
- [ ] Cancelling a queued task removes it from the queue.
- [ ] Unauthenticated requests are rejected.
- [ ] API documented well enough to use without reading source.

---

## Step 2.7 — Dashboard v2

Extend the GUI so the full lifecycle is watchable live.

- Queue depth, live.
- Running tasks with assigned worker and elapsed time.
- Completed tasks with duration.
- Per-worker current task on the worker row.
- Task history, searchable and filterable.
- Task detail view: full timeline, correlation ID, result summary.
- Task submission form so you can create tasks from the browser.
- Throughput chart: tasks completed per minute.

**Exit criteria**
- [ ] A task is watchable from queued through running to completed with
      no page refresh.
- [ ] Per-worker current task visible and accurate.
- [ ] Queue depth updates live as tasks drain.
- [ ] Tasks are submittable from the browser without a CLI.
- [ ] Task detail shows the complete timeline.
- [ ] Readable with 100 workers and 1,000 tasks.
- [ ] Throughput chart matches measured reality.

---

## Step 2.8 — Load testing harness

- Scripted, versioned, repeatable load generator.
- Scenarios: burst, sustained, mixed durations, saturation.
- Measures throughput, latency percentiles, queue depth over time,
  coordinator resource usage.
- Runs in CI on a schedule.

**Exit criteria**
- [ ] A documented single command runs a load scenario.
- [ ] 10,000 tasks across 100 workers complete with zero loss — counted.
- [ ] Throughput and p50/p95/p99 latency measured and recorded in
      `PHASE_STATE.md`.
- [ ] Saturation point identified and documented honestly.
- [ ] Results reproducible across runs.
- [ ] Load test passes against staging over the real Internet, not only
      locally.

---

## Step 2.9 — M2 demo and verification

**Demo you run yourself**
1. Open the dashboard with local and remote Internet workers online.
2. Submit five tasks from the browser.
3. Watch them move queued → running → completed.
4. Open one task detail; show the full timeline.
5. Submit 1,000 tasks; watch the queue drain and the throughput chart
   respond.

**Failure demo you run yourself**
- Submit a malformed task → rejected at submission, never dispatched.
- Submit a task type no worker supports → stays queued and visible.
- Submit 5,000 tasks to 5 workers → queue drains steadily, coordinator
  stays healthy.
- Run a 10-minute task → worker stays online throughout, no false
  offline.
- Restart the coordinator with tasks queued → queue survives.

**Capturable:** video of five tasks through all states; screenshot of
queue depth under load; throughput chart; log excerpt of one task's full
lifecycle by correlation ID; dashboard showing remote workers executing.

**Exit criteria**
- [ ] Full demo performed by you, including remote Internet workers.
- [ ] Full failure demo performed by you.
- [ ] Every task lifecycle traceable by a single correlation ID.
- [ ] Zero duplicate assignments under the 5,000-task load — verified.
- [ ] Zero task loss across every scenario — counted.
- [ ] CI green including load test.
- [ ] Runs from a fresh clone.
- [ ] `PHASE_STATE.md` updated with measured throughput and latency.
- [ ] Approval obtained before Phase 3.
