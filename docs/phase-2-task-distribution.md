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
- [ ] Both major decisions recorded with alternatives and reasoning.
- [ ] Reuses the Phase 1 message envelope unchanged.
- [ ] Approved before code.

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

## Step 2.2 — Redis-backed queue

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
- [ ] A task reaches a worker and is acknowledged.
- [ ] No task is ever assigned to two workers — verified with 500 tasks
      across 50 workers.
- [ ] 100 idle workers with an empty queue produce negligible load —
      measured.
- [ ] A queued task with no eligible worker stays queued and visible, not
      silently dropped.
- [ ] A worker disconnecting between assignment and acknowledgement is
      logged. Recovery is Phase 3; detection exists now.
- [ ] Assignment works identically for local Docker workers and remote
      Internet workers.

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
      uninterrupted — verified on the dashboard.
- [ ] Progress updates appear during execution.
- [ ] Concurrency limit is respected and configurable.
- [ ] Worker deletes temporary state after submission — verified.
- [ ] Unsupported task type is refused cleanly, not crashed on.
- [ ] Worker memory stays flat across 1,000 sequential tasks — no leak.

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
