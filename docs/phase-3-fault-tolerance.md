# Phase 3 — Fault Tolerance (Milestone 3)

**Goal:** Build automatic recovery. The system survives worker crashes,
network loss, coordinator restarts, duplicate completions, timeouts,
stale results, and partial completion — without an operator restarting
anything. Verified by deliberate chaos testing, locally and across the
public Internet.

**Out of scope:** Capability-aware scheduling. Reassignment here picks
any eligible worker; intelligence is Phase 4.

**Prerequisite:** Phase 2 complete and approved.

---

## Step 3.0 — Design gate

Build a failure taxonomy. For every failure, name the detection
mechanism, the recovery action, and the observable outcome.

Required entries: worker process crash; worker machine power loss;
network disconnection mid-task; coordinator pod eviction; coordinator
crash mid-assignment; duplicate task completion; worker timeout; stale
result arriving after reassignment; partial task completion; worker
reconnect after failure; alive-but-not-progressing worker; malformed
result; database unavailable; Redis unavailable.

Then state the **delivery guarantee** honestly: at-least-once delivery
with idempotent completion. Not exactly-once. Do not overclaim.

**Exit criteria**
- [ ] Every failure has a named detection mechanism, recovery action, and
      dashboard outcome.
- [ ] Crash versus slow-worker distinction is objective, not guesswork.
- [ ] Failures explicitly not recovered are stated with reasoning.
- [ ] Delivery guarantee stated accurately.
- [ ] Approved before code.

---

## Step 3.1 — Lease and timeout engine

- Leases over assignments, with duration relative to expected task
  duration.
- Worker lease renewal during long execution.
- Coordinator-side expiry detection that **does not depend on the worker
  being reachable** — this is the entire point.
- Distinct timeouts for assignment acknowledgement, execution, and result
  submission. Configurable per task type.
- A lease reclaimer that runs safely across multiple coordinator
  replicas without double-reclaiming.

**Exit criteria**
- [ ] Leases are created, renewed, and expire correctly.
- [ ] Killing a worker mid-task causes lease expiry within the documented
      timeout — timed and recorded.
- [ ] A legitimate long task renews its lease and is never reclaimed.
- [ ] A hung worker that stops renewing is reclaimed.
- [ ] Three coordinator replicas running the reclaimer never
      double-reclaim — verified under load.
- [ ] Timeouts configurable per task type without redeploy.

---

## Step 3.2 — Reassignment and retry

- Reassignment on lease expiry.
- Maximum attempt count with a terminal `FAILED` state.
- Retry backoff between attempts.
- Exclusion of the previously failed worker from the immediate retry.
- Per-worker failure counters recorded for Phase 4 consumption.
- Failed tasks are always visible and inspectable, never silently
  dropped.

**Exit criteria**
- [ ] Killing a worker mid-task causes automatic reassignment; a
      different worker completes it.
- [ ] Attempt count increments visibly on the dashboard.
- [ ] A poison task exhausts retries and lands in terminal `FAILED`,
      visible in the GUI.
- [ ] The failed worker is excluded from the immediate retry — verified.
- [ ] Failure counters accumulate per worker and are queryable.
- [ ] Reassignment works when the failed worker is a remote Internet
      machine, not only a local container.

---

## Step 3.3 — Idempotency and duplicate suppression

- Enforce the idempotency token that has been carried since Phase 2.
- Deduplication state in Redis with a documented window and retention.
- Duplicate submission is a **no-op returning success**, not an error.
- Resolve the classic race: worker A's result is in flight, the lease
  expires, the task reassigns to worker B, and both results arrive.

**Exit criteria**
- [ ] Submitting the same result twice completes the task exactly once —
      verified in the database.
- [ ] The in-flight-versus-reassignment race has a stated winner rule,
      implemented and tested by deliberately reproducing it.
- [ ] Duplicate submission returns success, not an error.
- [ ] Dedup state has a bounded retention and does not grow unbounded.
- [ ] Dedup works across coordinator replicas.
- [ ] Result ledger count matches task completion count exactly under
      load.

---

## Step 3.4 — Stale result fencing

- Reject results from superseded attempts using attempt number and
  session epoch, both present since Phase 2.
- Rejected results never mutate task state.
- The worker receives an unambiguous rejection response.
- Rejections are logged and surfaced on the dashboard.

**Exit criteria**
- [ ] A result from a superseded attempt is rejected — verified by
      deliberately reproducing it.
- [ ] A result from an old session epoch is rejected.
- [ ] Rejected results leave task state untouched.
- [ ] The worker handles rejection gracefully without crashing.
- [ ] Rejections visible on the dashboard, not buried in logs.
- [ ] **No protocol change was required** versus Phase 2.

---

## Step 3.5 — Coordinator restart and recovery

- Startup recovery: reload durable task state, identify assignments with
  expired or unknown leases, decide their fate deterministically, rebuild
  ephemeral state.
- Workers reconnect with existing identities; no re-enrollment.
- Thundering herd mitigation via the Phase 1 backoff and jitter.
- Graceful shutdown that drains in-flight work.

**Exit criteria**
- [ ] Restarting all coordinator replicas loses no durable task record.
- [ ] Tasks in flight at restart have a deterministic documented outcome.
- [ ] Workers reconnect automatically without re-enrollment.
- [ ] Restart with 100 workers and 1,000 queued tasks converges cleanly —
      timed.
- [ ] Rolling Kubernetes upgrade completes with no task loss.
- [ ] Reconnect storm does not overwhelm the coordinator — measured.

---

## Step 3.6 — Partial completion policy

Decide and implement the policy for interrupted work.

For V1 dummy workloads, full re-execution is likely correct — the tasks
are pure and side-effect free. If so, say so and justify it rather than
building checkpointing machinery nobody needs. Document what would
change for real SQL workloads later, without building it.

**Exit criteria**
- [ ] Policy stated with alternatives and reasoning.
- [ ] If checkpointing is deferred, deferral is explicit and justified.
- [ ] Re-execution safety established — dummy tasks verified pure.
- [ ] Repeated re-execution produces identical results — verified.
- [ ] Forward path for future stateful workloads noted, not built.

---

## Step 3.7 — Dashboard v3

Extend the GUI so recovery is watchable as it happens.

- Failed workers panel.
- Failed tasks panel, inspectable, with failure reason.
- Attempt count per task.
- Reassignment events visible **in real time**, not only in retrospect.
- Rejected stale results surfaced.
- Per-worker reliability counters.
- Recovery timeline for any given task.

**Exit criteria**
- [ ] Killing a worker produces a visible reassignment in the browser as
      it happens.
- [ ] Attempt count increments visibly.
- [ ] Failed tasks are inspectable with a reason.
- [ ] Stale rejections appear in the GUI.
- [ ] A task's full recovery timeline is viewable.
- [ ] Panels stay readable during a chaos run.

---

## Step 3.8 — Chaos testing harness

Automate the failures.

- Scripted chaos scenarios: random worker kills, network partition,
  coordinator pod eviction, database blips, Redis blips, duplicate
  submission injection, stale result injection.
- Runs against staging over the real Internet.
- Verifies invariants automatically: zero task loss, zero double
  completion, full convergence.
- Runs in CI on a schedule.

**Exit criteria**
- [ ] Chaos suite runs from a single documented command.
- [ ] 1,000 tasks with continuous random worker kills complete with zero
      loss — counted automatically.
- [ ] Zero double completions across the run — verified against the
      result ledger.
- [ ] System converges to a clean state after chaos stops — verified.
- [ ] Chaos run passes against staging over the public Internet.
- [ ] Any invariant violation fails the run loudly.

---

## Step 3.9 — M3 demo and verification

**Demo you run yourself**
1. Submit a long-running task; watch it enter running on a worker.
2. Kill that worker's container abruptly.
3. Watch the coordinator detect lease expiry within the timeout.
4. Watch reassignment; attempt count increments to 2.
5. A different worker completes it; task reaches completed.

**Failure demo you run yourself**
- Restart the coordinator mid-task → workers reconnect, no task lost.
- Kill a Kubernetes coordinator pod during load → fleet migrates,
  work continues.
- Force a duplicate completion → second submission deduplicated.
- Force a stale result: pause a worker, let the lease expire and
  reassign, then release the paused result → fenced and rejected.
- Sever a real Internet worker's network mid-task → reassignment, then
  clean reconnect.
- Exhaust retries on a poison task → terminal failed, visible.
- Run the full chaos suite → invariants hold.

**Capturable:** video of kill-and-reassign; screenshot of attempt count
incrementing; log excerpt of a fenced stale result; screenshot of a
terminal failed task; chaos suite output showing zero loss.

**Exit criteria**
- [ ] Full demo performed by you.
- [ ] Full failure demo performed by you, including remote Internet
      workers.
- [ ] Zero task loss across every scenario — verified by count.
- [ ] Zero double completion — verified by result ledger.
- [ ] Coordinator restart under load loses no durable state.
- [ ] Chaos suite green in CI.
- [ ] Runs from a fresh clone.
- [ ] `PHASE_STATE.md` updated with measured recovery times.
- [ ] Approval obtained before Phase 4.
