# Phase 4 — Adaptive Scheduling (Milestone 4)

**Goal:** Build a scheduler that uses reported hardware and observed
runtime behaviour to make measurably better assignment decisions than
round-robin — and can explain every decision it makes. Rule-based only.

**Out of scope:** Machine-learning scheduling. Everything in Milestone 5
and beyond. **This is the final phase of Version 1.**

**Prerequisite:** Phase 3 complete and approved.

---

## Step 4.0 — Design gate

Define the capability model and, critically, the **trust boundary**.

Three categories:

- **Static**, reported at registration and on agent upgrade: CPU cores,
  total RAM, OS, architecture, agent version, supported task types.
- **Dynamic**, reported on heartbeat: CPU usage, available memory,
  network latency, measured bandwidth, uptime, current task load.
- **Derived**, computed coordinator-side and never self-reported:
  average completion time, historical reliability, success rate,
  lease-loss rate.

**A worker may lie about anything it reports.** Anything the scheduler
trusts heavily must be derived from coordinator-side observation, not
worker claims. State explicitly which fields the scheduler is permitted
to trust and to what degree.

**Exit criteria**
- [ ] All three categories enumerated with type, unit, and collection
      method per field.
- [ ] Self-reported versus coordinator-observed cleanly separated.
- [ ] Trust policy stated per field.
- [ ] Approved before code.

---

## Step 4.1 — Capability reporting

- Static capability collected by the worker at startup and sent on
  registration.
- Dynamic capability added to the existing heartbeat. **No new protocol
  and no new channel** — reuse the Phase 1 heartbeat.
- Capability schema via migration.
- Re-report on agent upgrade or hardware change.

**Exit criteria**
- [ ] Static capability captured accurately, spot-checked against the
      real machine.
- [ ] Dynamic capability arrives on every heartbeat.
- [ ] Heartbeat payload size increase measured and acceptable at 10,000
      workers — calculated and recorded.
- [ ] Capability differs visibly between a constrained container and a
      real machine.
- [ ] Agent upgrade triggers a fresh static report.
- [ ] No new protocol introduced — verified against Phase 1 envelope.

---

## Step 4.2 — Telemetry pipeline and decay

- Latest capability cached in Redis for cheap scheduler reads.
- Historical samples stored with a documented retention period.
- Aggregation so history does not grow unbounded.
- Decay or sliding window so a worker slow last week is not penalised
  forever.
- Bandwidth and latency measurement that does not itself saturate the
  link.

**Exit criteria**
- [ ] Scheduler reads current capability without hitting the relational
      database on every decision.
- [ ] Historical aggregation runs and bounds storage growth — verified
      over a long run.
- [ ] Decay policy demonstrably lets a recovered worker regain standing.
- [ ] Bandwidth measurement overhead measured and negligible.
- [ ] Retention period enforced automatically.
- [ ] Telemetry survives coordinator restart.

---

## Step 4.3 — Scoring engine

- Capacity score from cores, RAM, and current load.
- Reliability score from completion rate, lease-loss rate, and uptime
  stability — fed by the Phase 3 failure counters.
- Responsiveness score from latency and acknowledgement time.
- Normalized ranges, documented.
- Cold start: a brand-new worker has no history. Define the default score
  and a probation policy.

**Exit criteria**
- [ ] Each score computes correctly against known inputs — verified by
      test.
- [ ] Scores are inspectable per worker, not opaque.
- [ ] A new worker gets the documented cold-start default.
- [ ] A worker that fails repeatedly sees its reliability score fall —
      demonstrated.
- [ ] A recovered worker sees its score rise — demonstrated.
- [ ] Scores that matter derive from coordinator observation, not worker
      claims — verified by feeding a lying worker.

---

## Step 4.4 — Rule-based scheduler

Compare candidate policies: round-robin baseline, capacity-weighted,
least-loaded, reliability-gated with capacity ranking, and multi-factor
cost scoring. Evaluate on fairness, starvation risk, tail latency, and
explainability. Recommend one with reasoning.

Then build:
- Eligibility filtering before ranking.
- Deterministic tie-breaking.
- A starvation guard so weak workers still receive some work and can
  rebuild a score.
- Task-size-to-worker-strength matching.
- Bounded decision time at large candidate pools.

**Exit criteria**
- [ ] Policy comparison recorded with a chosen policy and reasoning.
- [ ] Heavier tasks demonstrably go to stronger workers.
- [ ] No worker is starved — verified over a long run.
- [ ] Tie-breaking is deterministic and repeatable.
- [ ] Degrades correctly to round-robin with a homogeneous fleet.
- [ ] Works correctly with exactly one worker online.
- [ ] Decision time bounded and measured with 1,000 candidate workers.
- [ ] Scheduler runs correctly across multiple coordinator replicas.

---

## Step 4.5 — Scheduler explainability

Every assignment must be justifiable afterward from stored data alone.

- Decision record: task ID, candidate pool size, workers filtered out
  and why, ranked shortlist with scores, chosen worker, deciding factor.
- Persisted for every assignment.
- Retention policy so records do not grow unbounded.
- Queryable via API and viewable in the GUI.

**Exit criteria**
- [ ] A decision record exists for every assignment.
- [ ] You can answer "why did task X go to worker Y" from stored data,
      without re-running the scheduler.
- [ ] Records queryable by task and by worker.
- [ ] Retention enforced; storage growth bounded and measured.
- [ ] Records survive coordinator restart.

---

## Step 4.6 — Dashboard v4

Extend the GUI so scheduling intelligence is visible.

- Per-worker capability: cores, RAM, current CPU, available memory,
  latency, bandwidth.
- Per-worker scores: capacity, reliability, responsiveness.
- Live scheduling decision feed as assignments happen.
- Work distribution view across a heterogeneous fleet.
- Decision detail view for any assignment.
- Score history chart per worker.

The distribution view must make an uneven-but-correct spread **legible**.
A strong worker holding more tasks should look intentional, not broken.

**Exit criteria**
- [ ] Capability values visible per worker and updating live.
- [ ] All three scores visible per worker.
- [ ] Scheduling decisions visible in the browser as they occur.
- [ ] Uneven distribution is visually explained, not just displayed.
- [ ] Clicking an assignment shows the full decision record.
- [ ] Score history chart shows degradation and recovery over time.
- [ ] Readable with 100 heterogeneous workers.

---

## Step 4.7 — Heterogeneous benchmark harness

Prove the scheduler is actually better. Honestly.

- Reproducible heterogeneous fleet via Docker resource limits — some
  workers constrained to low CPU and memory, others given more.
- Mixed batch of light and heavy tasks.
- Runs the same batch under round-robin and under the adaptive policy.
- Measures total completion time, tail latency, and per-worker
  utilization.
- Automated and repeatable.

**Exit criteria**
- [ ] Heterogeneous fleet is reproducible from a single command.
- [ ] Both policies run the identical batch.
- [ ] Total completion time and p95/p99 latency measured for both.
- [ ] Results recorded in `PHASE_STATE.md` as measured values —
      **whatever they show**.
- [ ] **If adaptive is not measurably better, that is reported plainly
      and investigated.** No favourable narrative over an unfavourable
      result.
- [ ] Benchmark runs against staging with real Internet workers, not
      only local containers.

---

## Step 4.8 — M4 demo and Version 1 sign-off

**Demo you run yourself**
1. Open the dashboard; workers show visibly different CPU and RAM,
   including real Internet machines.
2. Submit a mixed batch of light and heavy tasks.
3. Watch heavier work go to stronger workers in the live decision feed.
4. Open one decision record; explain the choice from stored data.
5. Run the benchmark; compare round-robin against adaptive.

**Failure demo you run yourself**
- Introduce a repeatedly failing worker → reliability score degrades,
  scheduler routes away from it, visible in the GUI.
- Restore that worker → probation, then gradual score recovery.
- Saturate the strongest worker → scheduler shifts to the next best
  rather than piling on.
- Take all strong workers offline → graceful degradation to weak
  workers, no stall.
- Run a worker reporting obviously false capabilities → coordinator-
  derived scores correct for the lie over time.
- Run with a homogeneous fleet → behaves as round-robin.
- Run with one worker → still works.

**Capturable:** screenshot of heterogeneous capabilities; video of a
mixed batch distributing; side-by-side benchmark timings; screenshot of
a reliability score degrading and recovering; a printed decision record;
dashboard with globally distributed real workers.

**Exit criteria**
- [ ] Full demo performed by you.
- [ ] All seven failure demos performed by you.
- [ ] Benchmark numbers recorded honestly in `PHASE_STATE.md`.
- [ ] Every demo assignment has a retrievable decision record.
- [ ] No worker starved in any scenario.
- [ ] Correct behaviour with homogeneous fleet and with a single worker.
- [ ] Chaos suite from Phase 3 still green with the new scheduler.
- [ ] Full system runs from a fresh clone.
- [ ] Terraform reproduces the environment from scratch.
- [ ] CI and CD green end to end.
- [ ] Runbook complete: deploy, rollback, scale, onboard a worker,
      revoke a worker, teardown.
- [ ] `PHASE_STATE.md` marks Version 1 complete.

---

## End of Version 1

The distributed worker network is now the permanent foundation. Every
later capability — profiling, AI planning, execution, verification,
reporting — builds on this coordinator, this fleet, this protocol.

Do not plan, scaffold, or begin Milestone 5 or any AI, SQL, profiling,
or cleaning work without an explicit new instruction and a fresh scope
definition.
