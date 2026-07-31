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

**Exit criteria** — all verified locally in Docker 2026-07-30, re-verified
on staging **over the public Internet** with the shipped ghcr image (§8),
and then **demonstrated by the user personally**. The figures are in
`PHASE_STATE.md`'s 2.4 register row.

**Step 2.4 is DONE and APPROVED by the user 2026-07-30.** §15 items 3–4
were satisfied by the user running the demo and the failure demo
themselves — worth noting because 2.2, 2.2.1, 2.3 and this step's own
design sub-gate were each approved on recorded evidence as a user scope
call instead (§10). This one was not.

The demo covered: assigning all four workload types with live progress on
the dashboard, result correctness checked against independently computed
fingerprints, the concurrency limit holding at 2 slots against 6 queued
jobs, four classes of malformed submission rejected before dispatch, and a
**coordinator restart mid-execution** after which the in-flight work
completed, the reconnecting worker declared `tasks_in_flight: 2`, and
queued work resumed automatically.

Two parts of the evidence were **not** re-run in that session and were
accepted on the recorded measurements instead, which is recorded rather
than blurred (§10): the 13-minute two-part endurance run, and the
injected-fault crash path.

- [x] All four task types execute correctly and return correct results.
      Known-answer vectors in `tests/test_executors.py`, and live runs whose
      logged fingerprints match values computed independently — including
      1,000 of 1,000 tasks at one fingerprint (Decision #106).
- [x] A 10-minute task runs to completion while heartbeats continue
      uninterrupted — verified on the dashboard. **Two parts, both
      required (Decisions #99–#100, option (c), user-chosen 2026-07-30):**
  - [x] **(1) The letter.** One `sleep` task with `seconds: 600` runs to
        completion, holding a slot for the full ten minutes, heartbeats
        uninterrupted.
  - [x] **(2) The substance.** The worker is additionally held
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
- [x] Progress updates appear during execution.
- [x] Concurrency limit is respected and configurable.
- [x] Worker deletes temporary state after submission — verified.
- [x] Unsupported task type is refused cleanly, not crashed on.
- [x] Worker memory stays flat across 1,000 sequential tasks — no leak.

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

### What was built (Decisions #105–#108)

**`worker/executors.py`** — the four workloads as chunked loops, each
taking a one-slot progress list and a cooperative cancel flag. It imports
nothing from the coordinator: the two type registries are joined by the
wire protocol, not by a shared module, because the worker ships in its own
image with its own requirements.

**`worker/worker.py`** — a `TaskRunner` created once at startup and
therefore outliving every session, holding a `ThreadPoolExecutor` sized to
`WORKER_MAX_CONCURRENT`, an `asyncio.Semaphore` of the same size, and the
map of running tasks that *is* the worker's entire local task state.
Admission control refuses in two ways and never queues: an unadvertised
type is `unsupported_task_type`, no free slot is `at_capacity`. A duplicate
assignment is acknowledged and not started twice.

The worker is a package from this step on (`python -m worker.worker` in the
image), because it is the first step where it is more than one file.

**Coordinator** — `task_queue.mark_status` is the only new write path, and
every guard on it exists because the caller is an untrusted worker (§12):
the id must parse, the row is locked `FOR UPDATE`, `assigned_worker_id`
must be the reporting worker, and the move goes through
`task_states.check_transition`. `assignment.py` gained
`handle_task_started`, `handle_task_progress` and `handle_task_failed`, and
`handle_task_ack` now branches on the refusal's `reason_code` — the
anti-livelock rule from #101.

**Credits are keyed by task id** rather than counted (#107). A named
release is exactly-once by construction; only the reconnect residue
declared in `hello` may be released by count.

**Dashboard** — a live `current task` column with a progress bar, fed from
Redis via `GET /workers`, so §6's "current task" is satisfied in the phase
that first produces one. The full lifecycle view remains Step 2.7's.

**Three things this deliberately does not do**, so they are not mistaken
for oversights: no task reaches `COMPLETED` (#105 — a successful task
stays `RUNNING` until Step 2.5 persists its result), no execution timeout
exists (#103), and a task that survives a reconnect reports no *progress*
to the new session until it finishes, because the new session never saw it
start — its completion still lands, and the credit is still released.

### Verification — measured locally, in Docker

Every figure below was produced against a real coordinator, Postgres and
Redis over TLS, with the long-duration run on a worker constrained to
`--cpus=1` to match the shape the design was measured in.

- **All four types, live, and correct.** `count_to_n`, `hash_rounds` and
  `opaque_payload` each logged the exact fingerprint computed
  independently (`36877f3dcf65`, `adcac83e02f9`, `42898f37607b`);
  `sleep`'s fingerprint resolved to a result of exactly `14.0` for a
  14-second task. At scale: **1,000 sequential `count_to_n` tasks, 1,000
  distinct ids, every one fingerprinted `81a83544cf93`** — the value
  computed independently for the result `2000`.
- **The 10-minute criterion, both parts, in one window** — see the figures
  recorded in `PHASE_STATE.md`. Running them simultaneously is harsher
  than running them apart: one slot held by `sleep(600)` while the other
  ran ceiling-parameter `hash_rounds` back to back on a single CPU.
  **Sizing was re-derived on this machine as the criterion requires** —
  one ceiling task measured **12.916s** here, so 47 covered 600s and 60
  were enqueued for margin. The `~40` in the criterion above is the bench
  machine's number and was deliberately not reused.
- **Concurrency respected and configurable:** 6 tasks at a worker
  declaring `max_concurrent: 2` — every `task_execution_started` reported
  `tasks_in_flight` of 1 or 2, never 3, with zero refusals.
- **Progress observed live:** a 14s `sleep` reported 0.25 → 0.61 → 0.97 at
  the configured interval, each sample carrying the task's own correlation
  id, and no Postgres write for any of them.
- **Local state deleted after submission:** the worker's `running` map and
  the coordinator's Redis `current_tasks` key both return to empty, and
  the dashboard shows the worker idle.
- **Memory flat:** worker RSS **31,184 kB → 31,488 kB (+0.3 MB)** across
  the 1,000-task run.
- **Both refusal paths, live**, forced with `POST /workers/{id}/push`
  because the eligibility filter and credit accounting make the
  coordinator refuse to over-commit on its own: an unregistered type was
  refused `unsupported_task_type`, and a third task pushed at a
  2-slot worker with both slots busy was refused `at_capacity` rather than
  queued locally.
- **The failure path, live, with an injected fault.** There is no natural
  way to make an executor raise — the two registries agree, which is the
  point — so a throwaway worker was run with a patched `executors.py` that
  raises `ZeroDivisionError`. **No production code was changed to produce
  this.** The task reached `FAILED`, the credit was freed, and the
  exception message never appeared anywhere: the log and the wire carried
  `error_type` only.
- **Reconnect under running work:** two 75-second tasks kept computing
  through a **coordinator restart**, the new session's `hello` declared
  `tasks_in_flight: 2` so the coordinator opened at zero free credits, a
  task enqueued meanwhile correctly stayed `QUEUED`, and both credits were
  released on the *new* session the moment the work finished — after which
  the queued task was assigned within 100 ms.

**This last test is what found Decision #108's bug**, and it is worth
recording as the thing that justified running it: the first implementation
captured the session's socket when execution began, so the two completions
reported down a socket the coordinator had already thrown away. The sends
failed silently, the credits were never released, and the worker sat
permanently "full" with nothing running while the queue waited behind it.
Unit tests did not catch it and neither did review.

**Test suite: 203 passed** (was 136 at Step 2.3), `ruff` clean.

### Verified over the public Internet (§8)

`main` at **`fc33815`**, CI green on all 7 checks, `staging / deploy`
succeeded, and the deployment was checked rather than taken off CD's tick:
public `/health` returns `fc33815d…` **with no `-k`**, so the Let's Encrypt
certificate genuinely validated. `production / deploy` sits parked on its
required-reviewer gate.

The worker was **the ghcr image CI built for that SHA**, not a local build,
running on this laptop against
`https://dcds-staging.centralindia.cloudapp.azure.com` with
`WORKER_CA_FILE` empty — so the OS trust store validated the coordinator,
no dev CA involved. Same handshake, same protocol, same code path as a
Docker worker (§3.5).

- Registered, `ws_connected`, epoch 1, and declared `max_concurrent: 2`.
- **Executed 4 tasks over the Internet**: two `sleep(25)` (25.012s,
  25.010s) and two ceiling `hash_rounds`. **Both hash tasks returned
  fingerprint `2c7324ca2eca`** — the same value recomputed independently
  from a 10,000,000-round SHA-256 chain, so correctness holds on the
  shipped artefact over the real network, not just locally.
- **Progress and the current task were readable through the public admin
  API mid-execution** — the worker showed `running: 2` with both tasks at
  0.46 / 0.47 and elapsed 11.8 / 11.9s, which is exactly the data path the
  dashboard reads.
- Concurrency held at 2 with `max_concurrent: 2`.

**One finding that is not a 2.4 defect but will break M2 verification if
left alone.** Four `count_to_n` tasks enqueued in the same batch were
assigned to another staging worker and never reached `RUNNING`. **Measured,
not inferred:** `kubectl` shows `demo-worker` running the worker image
`b1963f90` — pre-2.3 — so it acknowledges an assignment and holds the slot
without executing, which is exactly Decision #92's documented
backwards-compatibility behaviour and not a fault.

**Root cause, and it is a deployment gap rather than a code one:**
`demo-worker.yaml` at the repo root is a **hand-applied manifest with a
hardcoded image tag**, living outside `infra/helm/platform/` and therefore
outside CD. Every coordinator and dashboard rollout updates itself; this one
never has, and nothing fails when it drifts. It also carries a `100m` CPU
limit, which would throttle a real `hash_rounds` task hard.

Left for a decision rather than changed here: it is outside Step 2.4's
scope, and it is live cluster configuration. **Resolve it before Step 2.9**,
whose exit criteria count tasks end to end — a worker that silently parks
every task it is given makes those counts unreadable.

### RESOLVED 2026-07-31 — Decision #121, the manifest is now in the chart

**Fixed in the repository; the live cutover is a one-time manual step and
has NOT been performed** (§10 — this is a code change, not a verified
deployment).

`demo-worker.yaml` is deleted from the repo root. The Deployment is now
`infra/helm/platform/templates/demo-worker.yaml`, gated on
`demoWorker.enabled` (default `false`, `true` in `values-staging.yaml`), and
`_deploy-env.yml` passes `--set demoWorker.image.tag="$SHA"` alongside the
coordinator and dashboard tags. **The drift class is closed rather than the
one instance of it:** the tag now comes from the deployed SHA on every
rollout, so it cannot fall behind again.

Three things changed with the move, each for a reason:

1. **CPU limit `100m` → `500m`.** The old limit throttled a ceiling
   `hash_rounds` task hard. Worst-case namespace draw at the coordinator's
   HPA ceiling of 5 is now **2900m against the `limits.cpu: 3` quota** —
   it fits, with 100m of headroom. That is tight and is recorded as such.
2. **`WORKER_MAX_CONCURRENT: 1`**, explicit. The worker's default is 4, and
   it declares that number as credits, so a 0.5-core pod would claim four
   CPU tasks it cannot serve. Raise it only with the CPU limit.
3. **`WORKER_AGENT_VERSION` carries the deployed SHA** (`demo-worker-<sha>`)
   instead of the fixed string `demo-worker-1`, so any future drift is
   visible on the dashboard rather than needing a `kubectl` to find. The
   original drift went unnoticed across four steps for exactly that reason.

**One-time cutover — PERFORMED 2026-07-31.** The hand-applied Deployment was
deleted from `staging`:

```powershell
kubectl -n staging delete deployment demo-worker
```

Its unmanaged status was **confirmed on the live object before deleting it**,
not assumed: no `app.kubernetes.io/managed-by: Helm` label, no `meta.helm.sh/*`
annotations, and a `kubectl.kubernetes.io/last-applied-configuration`
annotation. Helm would have refused to adopt it and the `--atomic` deploy
would have rolled back. **The drift was worse than the repo suggested** — the
running image was `b1963f90`, while the deleted root manifest pinned
`0df7e206`, so the manifest had drifted from the cluster as well as from `main`.

Verified after the delete: zero `demo-worker` pods, `coordinator 3/3`,
`dashboard 1/1`, `redis 1/1` still healthy, and public `/health` returns
`1cdaff2a32ab75ab824889e982917c21dfc55037` over a validated certificate.

A **server-side dry run** of the chart against the live `staging` namespace
then rendered the Deployment at the release's own SHA with no ownership
conflict and no quota rejection:

```powershell
helm upgrade --install platform infra/helm/platform -n staging `
  --values infra/helm/platform/values-staging.yaml `
  --set coordinator.image.tag=$SHA --set dashboard.image.tag=$SHA `
  --set demoWorker.image.tag=$SHA --dry-run=server
```

**What this does NOT mean (§10): staging currently has NO demo worker.** The
dry run applied nothing, and the chart change is not merged, so nothing
recreates it until this lands on `main` and CD deploys. That is the intended
order — delete, then let CD create the managed copy — but until then the
staging fleet has only whatever external workers are connected. Confirm the
managed copy after the next deploy with:

```powershell
kubectl -n staging get deploy demo-worker -o json | ConvertFrom-Json | ForEach-Object { $_.spec.template.spec.containers[0].image; $_.metadata.labels }
```

The image tag must equal the deployed SHA and the labels must include
`app.kubernetes.io/managed-by: Helm`.

**Regression check after the change: `253 passed`, `ruff` clean.** Run in a
`python:3.12-slim` container against ephemeral Postgres 16 / Redis 7 with
CI's environment, so it matches CI's posture rather than the host's Python
3.14. Same count as the Step 2.5 baseline — the chart change touches no
application code, and this confirms it.

**Not changed:** the worker still writes its identity file to the container
filesystem, so a rescheduled pod registers as a new `worker_id`. Pre-existing
behaviour, unrelated to the drift, and noise rather than a fault.

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

### Design sub-gate — Decisions #110–#117

Six decisions, each recorded because it was a real fork rather than the
only option.

**#110 — a success submits `task_result`, and that message carries the
credit release.** Step 2.4's success path sent `capacity`; it no longer
does. Sending both would be a second release for the same task — harmless,
because a keyed release is exactly-once (Decision #107), but it would spread
one credit's accounting across two messages and two code paths.

`capacity` is **kept and still handled**, and that is not dead code: a
worker built before this step sends nothing else on success, and §3.5 says
the coordinator cannot tell one worker generation from another. The
in-cluster `demo-worker` is a live example — it still runs a pre-2.3 image.

**#111 — the coordinator acknowledges every result, and the ack is always
definitive.** A worker retries an unacknowledged result, so an ack that only
ever meant success would leave a *malformed* result retrying forever — the
worker punishing itself for an answer that will never change. `accepted`
says whether the result was stored; either way the worker stops.

The alternative considered and rejected was no ack at all, treating a
successful send as a successful submission. That loses a result whenever a
coordinator dies between reading the frame and committing the row, which is
exactly the window this step exists to close.

**#112 — the pending-result buffer exists, and it is bounded.** Step 2.4
shipped with no buffer and said so plainly (Decision #98): a report with no
live session was dropped, because a buffer is worker-side state with nothing
to release it if the process dies. That reasoning has not stopped being
true — but 2.5's "finishes during a brief coordinator outage" criterion
cannot be met without one, because a result is the only message carrying
data nothing else can reconstruct. A lost progress sample is nothing; a lost
`capacity` is recovered by the next `hello`; a lost result is work done and
thrown away.

So the buffer is bounded at `WORKER_MAX_PENDING_RESULTS`, never written to
disk, and **abandoned openly on shutdown rather than persisted** — a worker
that survives its own restart with state is not stateless (§3.6). At the cap
the oldest entry is dropped and logged; its task stays `RUNNING`, visible,
and is Phase 3's to reclaim. `tasks_in_flight` deliberately excludes pending
results: the thread is back in the pool, so counting them would understate
the worker's capacity for as long as the coordinator was away — the wrong
direction.

**#113 — the result cap is 128 KB, and Decision #81's 64 KB was wrong by
arithmetic.** `opaque_payload` accepts up to 64 KB of **decoded** bytes and
the executor echoes them back **base64-encoded**, which is 4/3 the size —
87,384 bytes for a full-size input. A 64 KB result cap would therefore have
truncated the largest *legal* task's result, so `task_types.py`'s note that
a worker echoing its input "should not be able to exceed the result cap by
construction" compared decoded input to encoded output. Found by building
this step, not by review. 128 KB clears 87 KB with room for the envelope.

**Oversize is truncated, never rejected**, and that split is the point: a
malformed message is not a result and is refused before anything is written;
an oversize one *is* a result that simply carries more bytes than are worth
storing. Refusing to complete a task over its payload size would strand real
work. The cap is enforced on both sides — the worker keeps an oversize frame
off the wire (which is what "without breaking the connection" is actually
about), and the coordinator re-checks because the worker is untrusted (§12).

**#114 — a result arriving for an `ASSIGNED` task is walked through
`RUNNING`, not granted a new transition.** A `task_started` can be lost to a
socket that died between it and the result. The obvious fix is to add
`ASSIGNED -> COMPLETED` to the state machine; it was rejected. That edge
would make "completed without ever running" legal everywhere and forever, to
serve one lossy corner, and Phase 2.1 has an approved test asserting it
illegal. Instead `complete_task` performs both legal transitions inside the
same locked transaction. Two legal moves, no new edge, and the audit trail
still says the task ran.

**#115 — retention is enforced by a per-replica sweep, not documented and
left.** `RESULT_RETENTION_DAYS` (7, Decision #81) deletes bodies from
`task_results`; the `tasks` row survives as the permanent audit trail and
its `result_id` becomes NULL through the existing `ON DELETE SET NULL`, so
retention needs no second write. Every replica sweeps — the DELETE is
idempotent and row-scoped, so concurrent sweeps race harmlessly and leader
election would buy nothing but a failure mode (the same reasoning as
Decision #89). A retention period nothing implements is a hypothesis, and
this project does not record those as results (§10).

**Idempotency is structural, and the token is not what provides it.** Worth
stating because the two are easy to conflate. `complete_task` locks the task
row before reading its status, so a task already `COMPLETED` returns
`DUPLICATE` having written nothing — no second result row, no re-stamped
`completed_at`. That holds for a retry, for two replicas racing the same
resubmission, and for a worker simply repeating itself. The
`idempotency_token` is **recorded and enforced by nothing in M2**, which is
precisely what the exit criterion asks for: it is what lets Phase 3 tell a
retry apart from a genuinely second attempt, once there are second attempts.

**What this step does not do**, so none of it is mistaken for an oversight:
it adds no execution timeout (Decision #103 still holds), writes neither
`lease_expires_at` nor `attempt_count` — it *reads* the latter to put it on
the wire, which is a different thing — and does not decide what happens to a
task whose result was rejected or abandoned. Those stay `RUNNING`, visible,
and are Phase 3's.

**One schema change: migration `0003`, a single index.** Phase 2.1 built
`task_results` so writing results would need no migration, and it did — the
envelope lives in the existing JSONB `payload` and its length in the
existing `size_bytes`. What 2.1 could not foresee is the *read* pattern
retention introduces: `DELETE ... WHERE submitted_at < ...` on a schedule
forever, which without an index scans every result ever stored on every
sweep. Recorded as a recommendation, not a measurement — no sweep has been
run against a large table.

**New wire messages.** `task_result` (worker to coordinator) and
`task_result_ack` (coordinator to worker). `task_assign` gains an `attempt`
field. The envelope is untouched and `PROTOCOL_VERSION` stays `"1.0"`.

**New read path.** `GET /tasks/{task_id}` — admin-authenticated, returning
the task row and its stored result envelope. It exists because "execution
duration recorded and visible" is otherwise unverifiable without opening the
database. It is a **primitive**, in the same sense `POST /tasks/dequeue` was
kept as one in Step 2.2: Step 2.6 owns the operator task API — filters,
listing, full lifecycle history, cancellation — and builds on this rather
than around it. It reports two durations and does not blur them (§10): the
worker-reported `duration_seconds` inside the result, and the
coordinator-observed `observed_duration_seconds` from `assigned_at` to
`completed_at`, which includes delivery and the result round trip and is the
one that cannot be lied about.

### Two defects live testing found that review and unit tests did not

Recorded prominently because this is the second step running where the
live run — not the design review, not the suite — is what found the
problems, and both were **invisible to a passing test**.

**#116 — every result went over the wire twice.** A completing task submits
its result *and* records it as pending, which wakes the retry loop; the loop
then found a pending result that had been on the wire for a millisecond and
sent it again. Both landed, the second as a `duplicate` — so idempotency did
exactly its job, nothing broke, no test failed, and the only symptom was a
`duplicate` ack in a log nobody had a reason to read. What it cost was the
wire: for the full-size `opaque_payload` echo that is **87 KB duplicated per
task**, fleet-wide. Fixed with a per-result "sent at" stamp and a grace
period; `attach` clears the stamps on reconnect, because a send is void the
moment its socket dies.

**#117 — the retry loop could not be woken from its backoff.** `attach` set
the `results_pending` event on reconnect, but only the *idle* branch of the
submission loop waited on that event; the retry branch waited on
`stop_event`. So a worker that had climbed its backoff during an outage kept
sleeping after the coordinator came back. **Measured before the fix:** the
result was delivered, but 26.23s of backoff after the reconnect. **Measured
after:** `ws_connected` at `05:17:56.405738`, delivery at `05:17:56.405770`
— 32 µs — and acknowledged 131 ms after reconnect.

Neither was a correctness failure, which is exactly why they are worth
recording: the exit criterion passed on the pre-fix build. A criterion can
be met by something that is quietly wasteful or quietly slow, and only a
live run shows the difference.

### The §6 dashboard gap — deferred to Step 2.7 by the user

**Step 2.5 adds nothing to the dashboard, and that is a known gap rather
than an oversight.** The dashboard shows worker-status tiles and the
current-task column Step 2.4 added; it shows no queue depth, no running
count and no completed count. Step 2.5's entire visible behaviour is tasks
reaching `COMPLETED`, so **that behaviour is not watchable in a browser** —
it was verified through the admin API and the database instead.

This sits against §6 ("a first-class deliverable in every phase... after
each phase the user must be able to open a browser and watch that phase's
behaviour happen live") and §7. Step 2.4 met the same point by adding the
minimum surface for its own phase and leaving the rest to 2.7.

**The user was offered the minimum tile row and chose to defer the whole
thing to Step 2.7 (2026-07-31).** Recorded as a **user scope call on §6 and
§7, not as a satisfied criterion** (§10) — the same family as Decisions
#34–35, #77, and the 2.2/2.2.1/2.3 approvals. Step 2.7 already owns queue
depth updating live and completed tasks with duration, so nothing new is
added to that step; what changes is that 2.5's own demo is an API-and-
database demo.

### Verification — measured locally, in Docker

Against a real coordinator, worker, Postgres and Redis over TLS.

- **All four types complete, with results checked against independently
  computed answers**: `count_to_n(2000)` → `2000`; `hash_rounds(200000)` →
  `c4773d4f…fecd`, recomputed from a 200,000-round SHA-256 chain outside the
  system; `opaque_payload` → its exact base64 round trip; `sleep(4)` → `4.0`.
  Every row `COMPLETED` with `completed_at` stamped and `result_id` set.
- **Volume:** 200 tasks → **200 COMPLETED, 200 result rows, 200 distinct
  result ids, 200 distinct idempotency tokens, 0 wrong answers.** Worker RSS
  **31,224 → 31,412 kB (+0.19 MB)**, so the bounded buffer does not leak.
- **The outage criterion, live:** a `sleep(25)` task was started, the
  coordinator was **stopped** 6s in, the task finished with nowhere to send
  (`task_report_dropped_no_session`, `pending: 1`, backoff climbing 2.35 →
  15.16 → 26.23s), and on restart the result was delivered and the task
  reached `COMPLETED`. The stored envelope shows **`duration_seconds`
  25.002 against an observed 68.67s** — the two measurements differing by
  the outage is the clearest possible illustration of why both are reported.
  Its `session_epoch` is **4**, the epoch it *executed* under, not the 5 it
  submitted on.
- **Malformed, over the real socket**, using a throwaway protocol client —
  **no production code was changed to produce it**, the same discipline
  Step 2.4's injected fault used. A result with no idempotency token was
  answered `accepted: false / rejected / missing_idempotency_token`, and the
  task row afterwards was **`RUNNING`, `result_id` NULL, `completed_at`
  NULL, zero result rows** — untouched. The log carried the reason and never
  the body (§12).
- **Duplicate submission over the real socket:** the same envelope three
  times → acks `transitioned`, `duplicate`, `duplicate`, and **one** result
  row.
- **Large payloads:** a full-size `opaque_payload` (65,536 decoded bytes)
  produced an **87,384-character result stored whole** in an 87,602-byte
  envelope, `truncated` false, connection intact — the case Decision #113's
  arithmetic is about, since the superseded 64 KB cap would have truncated
  it. An over-cap result truncates and still completes.
- **Retention, live:** four aged bodies, one sweep, `result_retention_purged
  rows: 4`. Afterwards **9 tasks still `COMPLETED` with timestamps intact
  and 4 carrying `result_id` NULL** — the audit trail survives its body.
- **One correlation id spans enqueue → assigned → acknowledged → started →
  progress → completed across both services** (§11). Step 2.4's trace
  stopped at `capacity`; it now reaches completion.
- **Phase 3 columns untouched:** `lease_expires_at` NULL and
  `attempt_count` 0 on every completed row, and `attempt` is on the wire in
  the assignment at 0.
- **Test suite: 253 passed** (was 203 at Step 2.4), `ruff` clean.

### Shipped and verified over the public Internet (§8)

PR #36 merged, `main` at **`94636a6`**, **CI green on all 7 checks** with
**253 passed** in CI against ephemeral Postgres/Redis — the same count as
locally. `staging / deploy` succeeded and the deployment was **checked
rather than taken off CD's green tick**: public `/health` returns
`94636a61b994ef00f1807eee0411cdd03afe335c` **with no `-k`**, so the Let's
Encrypt certificate genuinely validated.

The worker was **the ghcr image CI built for that SHA**
(`sha256:23580cfb…`), not a local build, run against
`https://dcds-staging.centralindia.cloudapp.azure.com` with `WORKER_CA_FILE`
empty so the OS trust store validated the coordinator — no dev CA involved.
Same handshake, same protocol, same code path as a Docker worker (§3.5).

`demo-worker` was scaled to 0 for the duration and **restored to 1**, to
remove attribution ambiguity — it still runs the pre-2.3 image `b1963f90`
and would have acked and parked whatever it was given (the standing drift
noted for Step 2.9).

**All four types reached `COMPLETED` over the real network**, enqueued
through the **public ingress** and read back through the **public admin
API**:

| type | status | worker duration | observed | result |
|---|---|---|---|---|
| `count_to_n` | COMPLETED | 0.003 | 0.113 | `2000` |
| `hash_rounds` | COMPLETED | 0.378 | 0.457 | `c4773d4f7ba4…` |
| `sleep` | COMPLETED | 8.002 | 8.081 | `8.0` |
| `opaque_payload` | COMPLETED | 0.002 | 0.096 | exact base64 round trip |

`hash_rounds` returned the **same digest recomputed independently outside
the system**, so correctness holds on the shipped artefact over the real
network and not only locally. Every stored envelope carried
`session_epoch` 1 and `attempt_number` 0, and every result was
acknowledged `transitioned` with `pending: 0` — the buffer drained.

**`production / deploy` is PARKED on its required-reviewer gate.** That is
the user's to approve; the agent is blocked from approving production gates.

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
