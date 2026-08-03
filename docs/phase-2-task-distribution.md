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

### Shipped and verified on the deployed object 2026-07-31

PR #39 merged, **`main` at `99c89e7893fc384ac2cfca2061585d8364c3663c`**, CI
green on all 7 checks, CD run `30614723949` `success` on **both**
`staging / deploy` and `production / deploy`. Branch deleted local and remote.

**The green tick was not the check.** The deployed object was read directly,
and both required conditions hold:

```
image:  ghcr.io/.../data-cleaning-distributed-system-worker:99c89e7893fc384ac2cfca2061585d8364c3663c
labels: {"app":"demo-worker","app.kubernetes.io/managed-by":"Helm"}
```

The image tag **equals the deployed SHA**, and the `managed-by: Helm` label
is present. The annotations back the label up rather than leaving it to be
taken on trust — `meta.helm.sh/release-name: platform` and
`meta.helm.sh/release-namespace: staging` — so the object is genuinely owned
by the release and not merely labelled as if it were. `deployment.kubernetes.io/revision`
is `1`: it is a newly created object, not the old one relabelled. Helm release
is at revision 45. The three deliberate changes landed as written: CPU limit
`500m`, `WORKER_MAX_CONCURRENT=1`, and
`WORKER_AGENT_VERSION=demo-worker-99c89e7893fc…`. **Production still runs no
demo-worker at all**, as intended.

**Then the substance, because a correct tag is not the thing that was
broken.** The old worker acknowledged assignments and parked them forever; a
Deployment can carry the right tag and still do that. So a task was run end to
end through the **public ingress**:

- The worker's own log shows `task_runner_started` with `max_concurrent: 1`
  and all four supported task types — a message the pre-2.3 image cannot emit,
  which is what makes it a version proof rather than an inference from a tag
  string.
- `count_to_n(n=2000)` enqueued through `POST /tasks` on the public endpoint
  reached **`COMPLETED`**, assigned to `b2ca7645-f91e-4eac-a5a2-1775d0dcbae7`
  — **the demo-worker's own `worker_id`**, matched against its log, so the
  attribution is not assumed.
- Stored result **`2000`**, the known answer. `observed_duration_seconds` 0.02
  against a worker-reported 0.002, `session_epoch` 2, `attempt_number` 0.

That is the difference between the tag being right and the fleet member
actually working. **The drift is closed, not relocated.**

**Not claimed:** no secret was printed at any point, and the fresh-clone and
CPU-saturation properties of the new `500m` limit were not re-measured — the
headroom figure above is arithmetic against the quota, not a load test.

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
- [x] All operations work via the API.
- [x] Batch submission of 1,000 tasks succeeds.
- [x] Task inspection returns the full lifecycle with timestamps.
- [x] Cancelling a queued task removes it from the queue.
- [x] Unauthenticated requests are rejected.
- [x] API documented well enough to use without reading source.

### Design decisions — #122–#127

**#122 — cancellation covers `QUEUED` only, and the refusal is the
feature.** `task_states` also permits `ASSIGNED -> CANCELLED` and
`RUNNING -> CANCELLED`, so writing the transition was available and was
rejected. A coordinator-side write is not a cancellation: the worker holding
that task keeps executing, keeps its credit, and eventually submits a result
for a task the database calls terminal — which `complete_task` then refuses
as `ILLEGAL`. The task's work is thrown away, the credit is stranded, and
the operator is told "cancelled" about a worker that never stopped. Real
cancellation needs a wire message and a worker-side cancel path (the
executors already carry the cooperative flag from Decision #94), which is
not this step's scope and is not being half-built here. So an in-flight or
terminal task returns **409 with its current status**, and the operator
learns why.

A second cancel is **409 reporting `CANCELLED`**, not a silent 200. §3.7's
idempotency requirement is about a *worker* retrying a submission it cannot
know landed; an operator repeating a cancel is asking whether *this call*
cancelled it, and the honest answer is no. The state is identical either
way; only the report differs.

The race against the assignment engine is settled by the lock, not by
timing: cancel takes `FOR UPDATE` before reading the status, dequeue claims
with `FOR UPDATE SKIP LOCKED`. A dequeue in flight steps over a row being
cancelled; a cancel arriving mid-dequeue blocks, then sees `ASSIGNED` and
refuses. There is no window in which a task is both cancelled and handed
out.

**#123 — the lifecycle is reconstructed from four columns, not replayed
from an event log, and `started_at` is the column that was missing.** Until
this step, the moment a task moved `ASSIGNED -> RUNNING` survived only while
it stayed `RUNNING`: the transition wrote `updated_at`, and completion
overwrote it. Progress samples write nothing to Postgres by design
(Decision #94), so nothing else could reconstruct it. Migration `0004` adds
one nullable column, written **inside the UPDATE that already performs the
transition** — no extra statement on the assignment hot path.

A `task_events` table was the alternative and was rejected for M2: it adds a
write per transition to serve a read that four columns answer. Every
timeline entry names the column it came from, because the reconstruction is
exact for `QUEUED`/`ASSIGNED`/`RUNNING`/`COMPLETED` and **inferred** for
`FAILED`/`CANCELLED`, which have no column and use `updated_at`. That is
correct only because a terminal state is the last write a task row ever
receives — stated rather than assumed, since it stops being true the moment
something updates a terminal task. Phase 3, which introduces retries and
therefore *repeated* transitions per task, is where one column per state
stops being enough.

Tasks created before `0004` keep `started_at` NULL and their timeline omits
the `RUNNING` entry **rather than inventing one from `updated_at`**, which
for a completed task is the completion time (§10). No backfill is attempted.

**#124 — listings return `has_more`, never a total, and never a result
body.** A filtered `COUNT(*)` costs more than the page it describes, and
`tasks` accumulates terminal rows for the lifetime of the system by design
(Decision #79) — so a "3 of 412,905" header would make every listing pay for
a number nobody acts on. `has_more` costs one extra row: the query asks for
`limit + 1`. Result bodies are excluded because a 200-row page at the 128 KB
cap is a 25 MB response; `has_result` says whether one exists and
`GET /tasks/{id}` fetches it, which also keeps the listing query off
`task_results` entirely.

Order is fixed at `created_at DESC, id DESC` rather than caller-selectable.
The `id` tiebreak is not decoration: one multi-row INSERT stamps every row
of a bulk enqueue with an identical `created_at`, so without it a page
boundary could repeat or skip rows. **An unknown `status` or `task_type` is
a 400, not an empty list** — an operator who typos `RUNING` must be told,
not shown "no tasks" and left to conclude the fleet is idle.

**#125 — the coordinator rate-limits the operator API itself, and exempts
the dequeue primitive.** ingress-nginx already limits per source IP, but the
edge is not in the path for a Compose run, an in-cluster caller or a
port-forward — so without this, "the operator API is rate limited" would be
a property of one deployment topology rather than of the coordinator. Same
fixed-window mechanism as registration, a separate key scope, and applied
**before** authentication so an unauthenticated flood does not require a
credential to be rejected.

Default 300/minute because a *program* is the realistic caller: Step 2.7's
dashboard will proxy these endpoints from a single pod, so the whole
dashboard shares one bucket — the trap the registration limiter fell into
before `_caller_ip` was fixed. **`POST /tasks/dequeue` is exempt**, the only
task endpoint that is: `scripts/queue_harness.py` drives roughly a thousand
claim calls as fast as it can to prove three replicas never double-assign,
and limiting it would break a versioned verification for no security gain.

**#126 — validation errors no longer quote the request back.** FastAPI's
default handler echoes the offending value in each error's `input` field,
and for a `missing` error that value is the **whole request body**. In
session 13 a demo helper sent `ADMIN_SECRET` under the wrong field name and
the 422 handed the live secret back, where it was captured in a transcript
and had to be rotated (Decision #119). §12 says credentials are never logged
and never rendered; a response body is a rendering. The handler is
app-wide, not per-endpoint, so a future endpoint that accepts a secret does
not have to remember. `admin_secret` on `POST /tasks` also became optional
in the schema, so omitting it produces a 401 from the auth check rather than
a 422 from pydantic — the header is now the documented path.

**#127 — the credential stays where existing callers put it.** The read
endpoints take `X-Admin-Secret`; `POST /tasks` accepts the header **and**
keeps its body field, because scripts, harnesses and the CD smoke test send
it that way and breaking them buys nothing. `POST /tasks/{id}/cancel` takes
no body at all, so there is nothing there for a validation error to echo.
The inconsistency is documented in `docs/operator-api.md` rather than
resolved by a breaking change.

**One schema change: migration `0004`** — `tasks.started_at`, and
`ix_tasks_created_at` on `(created_at, id)`. No index was added for
`correlation_id` or `assigned_worker_id`: migration 0002 already created
both. That was found by the first live run of the migration failing on a
duplicate relation, not by reading 0002 first, and it is recorded because
the reverse mistake — shipping a second index under a new name — would have
gone unnoticed.

### Verification — measured locally, in Docker

Compose project **`dcds26`** against a real coordinator, worker, Postgres
and Redis over TLS, driven from a container on the same network so the
private dev CA validated. Suite **275 passed** (was 253 at 2.5), `ruff`
clean across `coordinator worker dashboard protocol tests scripts`.

**Criterion 1 — all operations work via the API.** A `count_to_n(2000)`
task submitted through `POST /tasks` was executed by the real worker and
read back `COMPLETED` with the result **2000**, the known answer. Filters by
`task_type`, `status`, `worker_id` and `correlation_id` each returned only
matching rows; a typo'd status returned **400** (`unknown task state:
'RUNING'`) rather than an empty list; a page over the cap returned 400; a
listing carried `has_result` and no result body.

**Criterion 2 — batch submission of 1,000 tasks.** One call, **0.069s**,
`count: 1000`, `task_ids: null`. Paged back by correlation id at 200 per
page: **1,000 unique ids across 5 pages**, no repeats and no gaps — which is
what the `id` tiebreak in #124 exists for, since all 1,000 rows share a
`created_at`.

**Criterion 3 — full lifecycle with timestamps.** The completed task's
timeline was `QUEUED -> ASSIGNED -> RUNNING -> COMPLETED` at
`10:04:30.103471`, `.113323`, `.130120`, `.148877`, each entry naming its
source column, timestamps monotonic. Both durations came back and did not
blur: **coordinator-observed 0.036s against a worker-reported 0.002s**.

**Criterion 4 — cancelling a queued task removes it from the queue.** Run
with the worker **stopped**, so the measurement was not racing the drain:
3 `sleep(120)` tasks all stayed `QUEUED`, one was cancelled, and **depth
went 377 → 376 — exactly one** — with `counts` gaining `CANCELLED: 1`. The
cancelled task had `assigned_worker_id` NULL (it was never handed to
anyone) and a two-entry timeline ending at `CANCELLED`. A second cancel
returned **409 `CANCELLED`**; cancelling the earlier `COMPLETED` task
returned **409 `COMPLETED`**; an unknown id returned 404. The worker was
then restarted and a task it had actually taken returned **409 `RUNNING`**,
with the task still `RUNNING` afterwards — refused, not half-cancelled.

**Criterion 5 — unauthenticated requests are rejected.** 401 on all five
endpoints with no credential, and on a near-miss credential (one character
short), which is `hmac.compare_digest` doing its job. The session 13 leak
was reproduced deliberately — `POST /tasks` with `admin_secret` in the body
and `task_type` missing — and the 422 came back as
`{"detail":[{"loc":["body","task_type"],"msg":"Field required","type":"missing"}]}`
with **the credential absent from the response**.

**Rate limiting**, measured against a fresh window: **300 × 200 then the
first 429 on request #301.** An earlier run of the same check reported the
first 429 at #270, which is the fixed window doing exactly what it should —
the preceding stage's ~31 calls were still inside the same minute. Recorded
because it is the sort of number that looks like a defect until the
mechanism is stated.

**Observability.** `task_cancelled`, `task_cancel_refused`,
`task_list_rejected_invalid_filter`, `task_list_rejected_invalid_admin_secret`
and `task_api_rate_limited` all emitted as structured JSON with
`correlation_id` and `client_ip`. New metrics read from `/metrics`:
`coordinator_task_cancellations_total{outcome="transitioned"} 2`,
`{outcome="not_cancellable"} 3`, `{outcome="not_found"} 1`, and
`coordinator_task_api_rate_limited_total 41`.

**Migration `0004` was applied from an empty database**, not only forward
from an existing one: the database was dropped and recreated, the full suite
re-run, and `alembic_version` read back **0004** with `started_at` present
and `ix_tasks_created_at btree (created_at, id)` on `tasks`.
`lease_expires_at` and `attempt_count` remain written by nothing.

**What the index costs and buys**, measured rather than assumed, on 60,000
rows. `EXPLAIN (ANALYZE)` of the endpoint's own query — no index **20.172
ms** (Parallel Seq Scan + top-N sort), `(created_at)` **4.516 ms** (index
scan, but an Incremental Sort reading 10,001 rows to return 50 because a
bulk enqueue makes one giant tie group), `(created_at, id)` **0.096 ms** (no
sort node, 50 rows read). Write cost on a 10,000-task bulk enqueue, seven
runs each after a discarded warm-up: no index **0.540s**, `(created_at)`
**0.628s**, `(created_at, id)` **0.593s** — so roughly **+0.05s**, with the
difference between the two index shapes inside the run-to-run spread. The
composite is not claimed to be cheaper to write, only not measurably dearer.

### Two things this step did not get for free

**A latent test-isolation defect surfaced.** `assignment._work_available` is
a module-level `asyncio.Event`, and an Event binds itself to the first loop
that awaits it. Earlier test modules await it inside their own
`asyncio.run`, so a later module that starts the whole app through
`TestClient` found the object bound to a loop that no longer existed —
`assignment_loop` died on its first wait and surfaced as an error at
lifespan shutdown. Fixed in the test module by binding a fresh Event before
the app starts. **The production path is unaffected and was not changed**: a
deployed coordinator has one event loop for the life of the process, and
this is a property of running one process across many loops, which only the
suite does.

**§6 is not satisfied by this step, and that is the user's standing scope
call, not a claim that it is.** Step 2.6 adds no dashboard surface, so its
behaviour is **not watchable in a browser** — the same gap recorded for Step
2.5 as Decision #118, which deferred live queue depth, running tasks and
completed tasks to Step 2.7. Nothing is added to 2.7's scope by this; the
demo below is an API-and-terminal demo.

### Shipped and verified on the deployed system 2026-07-31

PR #40 merged, **`main` at `34d8a0486e7ebaed93ad89ef5539d5eb553d88a0`**, CI
green on all 7 checks with **275 passed in CI** against ephemeral
Postgres/Redis — the same count as locally, read from the job log rather
than inferred from the green tick. Branch deleted local and remote. CD run
`30623912130` completed **`success` on BOTH `staging / deploy` and
`production / deploy`**.

**The tick was not the check.** What was read back from the running system:

- Public staging `/health` returns `34d8a0486e7ebaed93ad89ef5539d5eb553d88a0`
  **with no `-k`**, so the Let's Encrypt certificate genuinely validated.
- **The new routes exist and are guarded**: unauthenticated `GET /tasks` and
  `POST /tasks/{id}/cancel` return **401**, not the 405/404 a pre-2.6 build
  would give. That distinction is the point — a 401 cannot be produced by
  the old image.
- **The queries actually run**, which a 401 does not prove: an authenticated
  `GET /tasks?status=COMPLETED&limit=2` over the public endpoint returned
  real rows with the filters echoed and `has_more: true`, `GET /tasks/depth`
  returned `{"depth":0,"counts":{"COMPLETED":5,"RUNNING":4,"ASSIGNED":20636}}`,
  and `?status=RUNING` returned **400** rather than an empty list.
- **Migration `0004` is applied in both namespaces** — `alembic_version`
  reads `0004`, `tasks.started_at` exists, and `ix_tasks_created_at` is
  `btree (created_at, id)` in staging *and* production.
- **Decision #123 confirmed live and unflattering**: those returned rows are
  pre-`0004` tasks and carry `started_at: null`. The timeline omits their
  RUNNING entry rather than inventing one, which is exactly what was
  promised and is visible in production data.

**Two limitations carried from earlier sessions are now closed**, because
`kubectl exec` into a *production* pod was permitted this time where it was
denied in sessions 14 and 15:

- Production **reports its own version** — `/health` from inside a
  production pod returns `34d8a048…`, rather than the version being inferred
  from the Deployment spec as it was for Step 2.5.
- `coordinator_admin_credential_separate` reads **1.0 on production**, which
  session 15 could record for staging only.

**§8, stated honestly rather than claimed.** The operator API was exercised
**over the public Internet from outside the cluster**, which is what this
step's surface is. **No worker outside the local network was run for Step
2.6**, because nothing in its six criteria needs one — so §8 in its literal
"at least one worker running outside the local network" form is **not**
claimed for this step. Step 2.5 satisfied it with the shipped ghcr image,
and Step 2.7's demo is where the fleet comes back into it.

**The demo and failure demo (§15 items 3–4) are outstanding**, deferred by
the user on 2026-07-31 to be run together with Step 2.7's, when the same
behaviour is watchable in a browser. Recorded as a **user scope call, not as
a satisfied criterion** (§10) — the same family as Decisions #34–35, #77,
#118 and #120.

### Demo you run yourself

With the stack up (`docker compose up -d`) and `$env:ADMIN_SECRET` to hand.
Full reference, including the PowerShell `curl.exe` caveat, in
`docs/operator-api.md`.

1. **Submit and watch one task through its whole life.**
   `POST /tasks` with `{"task_type":"count_to_n","parameters":{"n":2000}}`,
   then `GET /tasks/{id}` — the `timeline` shows all four states with
   timestamps, and `result.result` is `2000`.
2. **Submit 1,000 in one call**, then find them all with
   `GET /tasks?correlation_id=…&limit=200`, paging on `offset` until
   `has_more` is false.
3. **Filter**: `GET /tasks?status=COMPLETED&limit=10`, then
   `?task_type=sleep`, then `?worker_id=…` from `GET /workers`.
4. **Cancel a queued task.** Stop the worker
   (`docker compose stop worker`), submit 3 `sleep(120)` tasks, read
   `GET /tasks/depth`, cancel one, read depth again — it drops by exactly
   one, and the task's timeline ends at `CANCELLED`.

**Failure demo**

1. **Cancel something already running.** Start the worker again, wait for a
   sleep task to reach `RUNNING`, cancel it: **409** naming its status, and
   `GET /tasks/{id}` shows it still `RUNNING` — refused, not half-cancelled.
2. **Call anything without the credential**: 401. Then with one character
   changed: 401.
3. **Typo a filter**: `GET /tasks?status=RUNING` → 400 saying so, not an
   empty list.
4. **Trip the rate limit**: set `TASK_API_RATE_LIMIT_PER_MINUTE=5` in
   `.env`, restart the coordinator, and call `GET /tasks/depth` six times —
   the sixth is 429.
5. **Prove the 422 no longer leaks.** `POST /tasks` with `admin_secret` in
   the body and `task_type` omitted: the 422 names the missing field and
   does not contain the secret.

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
- [x] A task is watchable from queued through running to completed with
      no page refresh.
- [x] Per-worker current task visible and accurate.
- [x] Queue depth updates live as tasks drain.
- [x] Tasks are submittable from the browser without a CLI.
- [x] Task detail shows the complete timeline.
- [x] Readable with 100 workers and 1,000 tasks — **with a measured
      coordinator-capacity ceiling recorded below, not a clean pass.**
- [x] Throughput chart matches measured reality.

### Design gate — Decisions #128–#133

The step opened with a short architecture gate (§9). **The user delegated
every design decision to the agent on 2026-07-31**, asking for the most
suitable option to be chosen and implemented. Recorded as a user scope call
on §9's "compare then recommend" step: the alternatives below were compared
and decided, but **not** presented for approval before building (§10).

**#128 — the task console is a second page at `/ui/tasks`, not a rewrite of
`/`.** Alternatives: one page with tabs; a page at `/tasks`.

`/tasks` is impossible and the reason matters — the public ingress routes
that whole prefix to the coordinator's operator API (Step 1.5.5, extended
in 2.6). A dashboard page there would work perfectly in Docker Compose and
be unreachable in staging and production, which is exactly the
environment-dependent difference §3.5 exists to prevent. `/ui/*` falls under
the dashboard's `/` catch-all in every environment, so **no ingress rule
changes and no new path is added to the coordinator's list**.

Two pages rather than tabs because `index.html` was already 677 lines and
this step adds a table, a detail view, a form and a chart. The shared
colour tokens, tiles, table and chip styles moved to `static/console.css`,
which both pages link; each page keeps only what is its own.

**#129 — the browser never holds the operator credential.** Every call goes
through a dashboard proxy under `/api`, exactly as Phase 1.8's
`/api/workers` did. `ADMIN_SECRET` is attached server-side by
`dashboard/app/main.py` and appears in no response and no page (§12).

The listing proxy forwards a **whitelist** of the documented filters rather
than passing `request.query_params` through. A proxy is not a tunnel: an
undocumented parameter silently forwarded is worse than one the API
rejects.

**#130 — submission makes the dashboard a write surface, which needs a
guard the edge does not provide.** The dashboard is protected by HTTP basic
auth at the ingress, and a browser attaches those credentials to *any*
request to that origin — **including a form another site submits**. So the
moment `POST /api/tasks` existed, an operator with the dashboard open could
be made to enqueue or cancel work by visiting an unrelated page. Basic auth
authenticates the browser, not the intent.

Writes therefore require a header the page sets itself. A cross-site HTML
form cannot add headers at all, and a cross-origin `fetch` that adds one
becomes preflighted, which fails because no CORS origin is allowed. The
value is fixed and public — this is a forgery guard, not a second
authentication, and it is documented as such in `_reject_unless_same_origin`.

**#131 — throughput is a coordinator query over `completed_at`, not a
counter the browser accumulates.** The alternative — differencing the
`COMPLETED` count between polls — needs no backend work, and was rejected
on three counts: the history dies on reload, two operators watching see
different charts, and the number is derived from poll timing rather than
from the rows.

The query groups `tasks.completed_at` into per-minute buckets, so **a bucket
is exactly the set of rows `GET /tasks?status=COMPLETED` would list for that
minute**. That is what makes "the chart matches measured reality" something
an operator can check against another endpoint rather than take on trust,
and it is how the criterion was verified below. Buckets are cut by
Postgres's clock — the same one that stamped the rows — so a browser in
another time zone cannot shift them. Empty minutes come back as zero rather
than being omitted: a chart with the quiet minutes missing draws a busy
fleet and an idle one identically.

`completed_at` is stamped by `complete_task` and nowhere else, so a `FAILED`
task never enters the series. It means "produced a result", not "stopped
moving"; failure counts belong to the lifecycle totals.

**Migration `0005`** adds `ix_tasks_completed_at`. The window the chart
shows is minutes wide; the table it would otherwise scan grows for the
lifetime of the system (Decision #79). Those two diverging is the whole
reason for the index — the same reasoning that put
`ix_task_results_submitted_at` in `0003`.

**#132 — polling again, not a dashboard WebSocket.** Phase 1.8's reasoning
is unchanged: a short poll already reads as real-time, and "the view
recovers when the browser connection drops" is then "the next poll
succeeds", not a second reconnect protocol to build and verify alongside
the one Phase 1.7 built for workers. Two intervals, because the reads answer
different questions: 2s for the list and lifecycle counts, **15s for the
chart**, which is bucketed per minute and would otherwise be fetched 30
times to redraw the same bar.

**#133 — readability at 1,000 tasks is server-side paging and server-side
filters, not virtualisation.** The browser never holds 1,000 rows: a page is
50, the filters are pushed to `GET /tasks`, and the chip row and correlation
box map onto its `status` and `correlation_id` parameters. Nothing is
filtered client-side, so what is on screen is what the coordinator returned.

### Built

- **`dashboard/app/static/tasks.html`** — lifecycle tiles with live queue
  depth, a filterable paged task table, a detail drawer (full timeline,
  correlation id, both durations, result summary, cancel), the submission
  form, and an inline-SVG throughput chart. No chart library: the page is
  self-contained, as the fleet view already was.
- **`dashboard/app/static/console.css`** — the shared design tokens and
  table/tile/chip styles, extracted from `index.html` unchanged.
- **`dashboard/app/main.py`** — six proxies, the write guard, a static
  mount, and the `/ui/tasks` route.
- **`index.html`** — a view switch, and the current-task cell is now a link
  into `#/task/<id>`. That link is the whole join between the two pages:
  "this worker is busy" and "this is the task, its timeline and its result"
  stop being separate questions.
- **Coordinator** — `GET /tasks/throughput`, `task_queue.completions_per_minute`,
  migration `0005`, and the connection-pool change below.
- **`tests/test_dashboard_api.py`** — the dashboard's first tests, 9 of them.

### One defect found by the live run, and it was not in this step's code

**Decision #134.** Measured during criterion 6, with 100 workers connected
and a 1,000-task batch draining: an operator page took **0.83s to 48.8s**
through `GET /tasks`, while the SQL behind it — checked with
`EXPLAIN ANALYZE` in the same window — ran in **0.198 ms**. Five orders of
magnitude apart, so the query was never the problem.

`pg_stat_activity` showed the coordinator holding exactly **15**
connections. That is SQLAlchemy's default `pool_size=5` plus
`max_overflow=10`, unchanged since Phase 1.2 and never sized. Every worker
message that writes takes a session — `task_started`, `task_result` — so a
busy fleet holds the pool and an operator read waits behind it.

The pool is now `DB_POOL_SIZE` (15) and `DB_MAX_OVERFLOW` (5), sized against
Postgres's budget rather than picked: `3 replicas × 20 = 60` against a
default `max_connections` of 100, leaving room for migrations, psql and the
scrapes. Both are environment variables because the ceiling is a property of
the deployment, not of the code.

**It helped and it did not fix it, and both halves are reported (§10).**
After the change, the same burst measured **median 0.849s, p95 9.912s, max
12.503s** over 40 samples. The pool was a real constraint and no longer the
binding one — the remaining cost is elsewhere, see the ceiling below.

### Measured, not asserted

All local, Docker Compose project `dcds27`, against a real coordinator,
dashboard, worker fleet, Postgres and Redis over TLS. Every figure below was
read through the **dashboard's own API** — the path the browser uses — not
against the coordinator directly, except where stated.

**Criterion 1 — watchable queued → running → completed, no refresh.** A
14-task batch of `sleep(6)` against a 4-slot worker, sampled on the page's
own 2s timer. Queue depth went **10 → 6 → 2 → 0** while one task picked
*because it was still QUEUED* was watched through
**`QUEUED -> RUNNING -> COMPLETED`**. The first attempt failed to
demonstrate this and the reason is recorded because it is a property of the
system: with a single task and a free slot, `QUEUED -> ASSIGNED` took **9
ms**, so no poll at any human interval can see it. Watching a queue requires
a queue.

**Criterion 3 — queue depth updates live as tasks drain.** The same run;
`depth` and the `QUEUED`/`ASSIGNED`/`RUNNING`/`COMPLETED` counts moved
together, ending `depth=0, running=0, completed=14`.

**Criterion 2 — per-worker current task visible and accurate.** Checked
rather than eyeballed: for every worker reporting a current task, the task
id was fetched and its own row compared. **16 of 16 matched** on both status
(`RUNNING`) and `assigned_worker_id`. This is the check that would catch a
stale Redis current-task entry, which is what the column reads.

**Criterion 4 — submittable from the browser.** `POST /api/tasks` returned
**201** with the correlation id, for single tasks and for 1,000-task
batches. The refusal path was exercised in the same shape: the identical
request **without** the page header returned **403 and never reached the
coordinator**.

**Criterion 5 — detail shows the complete timeline.** All four entries with
timestamps and the column each came from:

```
QUEUED     2026-07-31T11:02:52.195821+00:00  created_at
ASSIGNED   2026-07-31T11:02:52.204716+00:00  assigned_at
RUNNING    2026-07-31T11:02:52.221777+00:00  started_at
COMPLETED  2026-07-31T11:03:04.221512+00:00  completed_at
```

**Criterion 7 — the chart matches measured reality.** Cross-checked twice,
against the API and against the database directly. At 1,016 completions the
chart's `completed_in_window` equalled the listing count **and** the SQL
`GROUP BY date_trunc('minute', completed_at)` bucket for bucket. Repeated at
2,278 completions across 8 minutes: **7 of the 8 buckets identical**, the
differing one being the **current, still-filling minute** (chart 33, database
36, read three seconds later). The index is used, measured rather than
assumed — `Index Only Scan using ix_tasks_completed_at`, **0.714 ms**.

**Criterion 6 — readable with 100 workers and 1,000 tasks, with a ceiling.**

The page itself is bounded by construction and measured to be: one page is
**50 rows / ~22 KB** whatever the table holds, and paging to `offset=950` of
1,000 tasks cost **0.056s**. The fleet view rendered **110 registered
workers, 96 ONLINE**, at **52 KB in 0.175s** — more rows than the criterion
names.

**The honest part.** With **100 workers** connected and a 1,000-task burst
draining, operator reads degrade badly: **median 0.849s, p95 9.912s, max
12.503s**. The cause was isolated rather than guessed, by re-running the
identical burst against the identical 2,800-row table with the fleet scaled
to **4 workers**:

| fleet | median | p95 | max | coordinator CPU |
|---|---|---|---|---|
| 100 workers | 0.849s | 9.912s | 12.503s | **76–91%** of a core |
| 4 workers | **0.025s** | **0.045s** | **0.126s** | **1.84%** |

Same table, same batch, same query. **The degradation tracks fleet size, not
task count** — so the "1,000 tasks" half of the criterion passes cleanly and
the "100 workers" half is bounded by **one coordinator process saturating
one CPU core** while serving 95 WebSocket sessions plus TLS plus JSON
logging. It is a coordinator-capacity ceiling, not a dashboard one, and the
project's answer to it already exists and is already proven: §3.9 horizontal
scaling, demonstrated in Step 1.5.7 with three replicas autoscaling to five.
A single Compose container has no horizontal anything.

**Recorded, not fixed.** Making one process serve 100 workers faster is not
Step 2.7's scope, and Step 2.8's load harness is what should produce the
defensible saturation number (§10).

**A second observation from the same run, and it is not a defect.** 1,372
tasks ended stranded in `ASSIGNED` with `task_assign_delivery_failed`
("Cannot call send once a close message has been sent"), because 100 worker
containers on one laptop churned their sockets — 226 disconnects. That is
exactly Decision #91's designed outcome: commit before send, so a task
recorded `ASSIGNED` that never arrived is **visible and reclaimable**, and
Phase 3 is what reclaims it. A host-capacity artefact demonstrating the
documented behaviour, not a fault.

### Failure demo — run and measured

1. **Cancel something already running** → **409** naming its state:
   `task is RUNNING: only a QUEUED task can be cancelled`. The task stayed
   `RUNNING`; refused, not half-cancelled.
2. **Cancel the same queued task twice** → first **200**
   (`previous_status: QUEUED`), second **409** reporting `CANCELLED`.
3. **Write without the page header** → **403**, and the coordinator was
   never called. With the header, the identical request → **201**.
4. **Coordinator stopped under a running task** → the dashboard returned
   `{"error": "coordinator_unreachable"}`, which is what raises the page's
   banner. On restart the view resumed with no action, **and the task that
   had been executing through the outage completed** — its result landed on
   reconnect, appearing in the 11:05 throughput bucket. Step 2.5's
   outage-survival path, re-demonstrated through the GUI's own data path.

### What is NOT claimed

- **No browser screenshot was captured by the agent.** Playwright cannot
  validate the private dev CA, and no page render was observed. What was
  verified instead: both pages are served, `console.css` is served, both
  scripts **parse** (`node --check`), and every data path behind them was
  measured. **Seeing the pages is your demo (§15 items 3–4).**
- **§8 is not claimed.** No worker outside the local network was run for
  this step; the fleet was local Docker.
- **Not deployed.** No CI run, no staging or production deploy at the time
  of writing.
- **Step 2.6's demo and failure demo** remain outstanding and are to be run
  alongside this step's, as agreed on 2026-07-31.

### Demo you run yourself

`docker compose up -d`, then open `https://localhost:8444/` and
`https://localhost:8444/ui/tasks`. Accept the dev-CA warning.

1. **Watch a task's whole life.** On the tasks page, submit
   `count_to_n {"n": 2000}`. It appears within one poll; click the row for
   the timeline and the result. **To watch it sit in `QUEUED`, submit more
   than the fleet can run at once** — with one 4-slot worker, submit 14
   `sleep {"seconds": 6}` and watch the depth tile drain.
2. **Queue depth live.** Keep the tiles in view during that drain.
3. **Per-worker current task.** Open the fleet view alongside it; each busy
   worker names its task, and **clicking it opens that task's detail**.
4. **Filter and page.** Submit 1,000 with `count`, then use the correlation
   id from the form's confirmation, or "show everything this batch created"
   in any detail drawer. Page with newer/older.
5. **Throughput.** The chart fills a bar per minute. Check it: the window
   total must equal what `GET /tasks?status=COMPLETED` reports over the same
   minutes.

**Failure demo**

1. **Cancel a running task** — start a `sleep {"seconds": 60}`, wait for
   `RUNNING`, open it, press cancel: **409** naming its state.
2. **Stop the coordinator** (`docker compose stop coordinator`) — the banner
   appears within two polls; start it again and the view resumes with no
   reload.
3. **Submit invalid parameters** — `count_to_n` with `{"n": -5}`: the form
   shows the coordinator's own rejection, not a dashboard paraphrase.
4. **Prove the credential is not in the browser** — view source on both
   pages and search the network tab: `ADMIN_SECRET` appears in neither.

---

## Step 2.8 — Load testing harness

- Scripted, versioned, repeatable load generator.
- Scenarios: burst, sustained, mixed durations, saturation.
- Measures throughput, latency percentiles, queue depth over time,
  coordinator resource usage.
- Runs in CI on a schedule.

**Exit criteria**
- [x] A documented single command runs a load scenario.
- [x] 10,000 tasks across 100 workers complete with zero loss — counted.
- [x] Throughput and p50/p95/p99 latency measured and recorded in
      `PHASE_STATE.md`.
- [x] Saturation point identified and documented honestly.
- [x] Results reproducible across runs.
- [x] Load test passes against staging over the real Internet, not only
      locally.

### What shipped

`scripts/loadtest.py` — four scenarios, one JSON report, its own pass/fail
verdict, exit 0/1/2. `tests/test_loadtest.py` covers the verdict logic.
`.github/workflows/loadtest.yml` runs it weekly and on demand.
**`docs/load-testing.md` is the document for this step** — the single
command, every measured table, and what each number does and does not mean.
Nothing below repeats those tables; this section records how the criteria
were met and what is *not* claimed.

**No application code changed.** The harness drives the shipped operator
API and the shipped wire protocol, and imports the real executors from
`worker/executors.py`. That is deliberate: a load test that needed the
system modified to accept it would be measuring something else.

### How the fleet is produced, and the ceiling on it

Every simulated worker registers through `POST /workers/register`, refreshes
a real token, opens a real WebSocket, sends `hello` with declared credits
and task types, acknowledges assignments, sends `task_started`, executes,
and submits a real result envelope that it retries until acknowledged. The
coordinator cannot tell one from a container or a laptop — invariant §3.5.

**Honest ceiling (§10): it is N sessions from one process, not N machines**,
and the report says so on every run. Two simplifications are marked in the
code rather than hidden: `sleep` is awaited instead of executed (a no-CPU
workload simulates faithfully; 400 slots would otherwise need 400 threads),
and there is no bounded pending-result buffer (Decision #112 is worker-side
state that does not change the load the coordinator sees). The harness's own
CPU was **measured** at 45.3% of one core while the coordinator was pinned,
so it is not the thing being measured.

### The criteria, and how each was met

1. **Single documented command** — `docs/load-testing.md` §1.
2. **10,000 across 100 workers, zero loss, counted** — three runs, each
   **10,000 of 10,000 `COMPLETED`** with 10,000 stored results, 10,000
   distinct task rows and **0 duplicate assignments**. The count is the
   coordinator's own rows, paged back through `GET /tasks` by correlation
   id — not the harness's tally of what it was told.
3. **Throughput and p50/p95/p99 recorded** — in `PHASE_STATE.md`'s Measured
   Benchmarks and in `docs/load-testing.md` §4.
4. **Saturation point identified honestly** — §4.3 there. **~110–124 tasks
   per second for one coordinator process, reached at five workers**, with
   the component attribution measured (§4.4): coordinator 92–112% of a core,
   Postgres 43–60%, Redis 3–7%.
5. **Reproducible across runs** — and the honest form of that claim is in
   §4.1: the **pass/fail properties reproduce perfectly** (three runs, three
   times zero loss and zero duplicates) while **the throughput figure
   reproduces to about ±26%**. Both halves are recorded.
6. **Passes against staging over the real Internet** — §5 there, with no
   `-k` and no `--insecure`, so the run is also a certificate validation.

### Two things the run found that were not in the plan

**The saturation ceiling is not what Decision #135 predicted, and both are
right.** #135 measured operator *page latency* degrading with fleet size and
concluded the degradation tracked fleet size. Measured here as *pipeline
throughput*, the ceiling is flat from 5 to 100 workers: one Python process
on one core, whatever is attached to it. Same underlying constraint seen
from two directions, and **#135's outstanding number is now that table**.
The answer is unchanged and already proven — §3.9 horizontal scaling, Step
1.5.7.

**A defect in the harness's own verdict, found by a real run and fixed.**
The first `sustained` implementation judged whether the queue kept up using
the depth sample taken *after* the offer stopped. A run whose queue climbed
monotonically to **2,116** therefore reported `queue_kept_up: true`, because
by the last sample it had drained back to zero. The verdict now uses only
the samples inside the offering window, and
`tests/test_loadtest.py::TestKeptUp` is the regression guard, built from
that run's own numbers. Recorded because it is the same shape as Decisions
#116 and #117: **a check that passes for the wrong reason is worse than no
check**, and only a live run showed it.

### What is NOT claimed

- **The Internet run is small, and that is a finding rather than a
  shortcut.** Staging runs the shipped defaults —
  `REGISTER_RATE_LIMIT_PER_MINUTE` **5 per source IP** plus the ingress's
  `limit-rps: 5` — and a harness on one laptop is one source address. The
  control is working correctly; a hundred-worker fleet from one address is
  exactly the mass fake registration §12 says to refuse. **The Internet run
  proves the path, not the throughput ceiling**, which the local run owns.
  Staging was deliberately left as deployed rather than tuned for the test.
- **Coordinator CPU and memory are `null` in the Internet run.** `/metrics`
  is not routed on the public ingress, on purpose. Absent, not zero, and not
  estimated.
- **No fault injection.** Killing a coordinator mid-drain, partitioning a
  worker, expiring a lease — Phase 3 owns those.
- **No performance budget is asserted.** There is no agreed target to assert
  against, and one invented from a single laptop's numbers would be
  arbitrary. The scheduled CI job asserts the loss and duplication
  properties only.
- **Every published figure is from one laptop** (Intel i5-4460S, 4 cores)
  and transfers to no other machine.

### Demo you run yourself

From a fresh clone with the stack up (`docker compose up -d`) and a venv
holding `worker/requirements.txt`:

```bash
# 1. The single command, small enough to watch.
python scripts/loadtest.py burst --url https://localhost:8443 \
  --enrollment-secret "$ENROLLMENT_SECRET" --admin-secret "$ADMIN_SECRET" \
  --workers 10 --tasks 500 --insecure
```

1. **Watch it in the browser while it runs.** Open
   `https://localhost:8444/ui/tasks` first. The queue-depth tile spikes and
   drains, the throughput chart grows a bar, and the fleet view fills with
   `loadtest` workers — Step 2.7's console is how this step is watchable
   (§6).
2. **Check the harness against the GUI.** Take `correlation_id` from the
   report and paste it into the console's correlation box: the task count
   there must equal `read_back.rows`.
3. **See the pipeline keep up, then not.** Run `sustained --rate 60` and
   then `--rate 150`, both `--workers 25 --seconds 60`. The first reports
   `queue_kept_up: true` with sub-second p95; the second reports `false`
   with the depth series climbing — **and both complete every task.**
4. **Find the ceiling.** `saturation --ramp 5,10,25,50,100 --tasks 2000`
   prints each step as it finishes. Throughput does not improve with fleet
   size, and coordinator CPU sits near one core throughout.

**Failure demo**

1. **Take the workers away** — run `burst --workers 0 --tasks 50
   --timeout 30`. Every task stays `QUEUED`, the depth tile holds at 50, and
   the harness exits **1** with `FAIL: every_task_completed` on stderr.
   Nothing is lost; the work is waiting. (Verified: 20 tasks, all read back
   `QUEUED`, `every_task_completed: false`, exit 1. Pass `--timeout` or the
   default makes it wait ten minutes to tell you.)
2. **Stop the coordinator mid-drain** (`docker compose stop coordinator`
   during a 10,000-task burst). The harness reports the tasks that never
   completed and **fails**; `GET /tasks/depth` after restart shows the queue
   intact. This is a demonstration of the harness's honesty as much as the
   queue's durability — it must not report a green run.
3. **Offer more than the pipeline can take** — `sustained --rate 300
   --seconds 60`. `queue_kept_up` is `false`, the depth series climbs, and
   **every task still completes** after the offer stops. Over-capacity load
   is queued, not dropped.
4. **Prove the limiter is real** — set `REGISTER_RATE_LIMIT_PER_MINUTE` back
   to `5` and run `--workers 100`. Registration is refused with 429s and
   `rate_limited_retries` climbs. That is §12 working, not a bug.

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
