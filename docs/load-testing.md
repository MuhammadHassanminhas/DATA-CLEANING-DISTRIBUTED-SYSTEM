# Load testing

Step 2.8. The harness is `scripts/loadtest.py` — scripted, versioned and
repeatable (CLAUDE.md §4), and it decides its own pass/fail rather than
leaving a human to eyeball numbers.

Companion documents: `docs/operator-api.md` (the endpoints it drives),
`docs/onboarding-a-worker.md` (running a real worker), `docs/runbook.md`.

---

## 1. The single command

```bash
python scripts/loadtest.py burst \
  --url https://localhost:8443 \
  --enrollment-secret "$ENROLLMENT_SECRET" \
  --admin-secret "$ADMIN_SECRET" \
  --workers 100 --tasks 10000 --insecure
```

`COORDINATOR_URL`, `ENROLLMENT_SECRET` and `ADMIN_SECRET` are read from the
environment when the flags are omitted. `--insecure` is for the local dev
CA only; the public staging endpoint carries a real Let's Encrypt
certificate and needs no such flag.

Every run prints one JSON report to stdout, writes it to `--json-out` if
asked, and exits **0 on PASS, 1 on FAIL, 2 on a missing credential**.

### Prerequisites

The harness needs `websockets` (a WebSocket client is not in the standard
library) and imports the **real** executors from `worker/executors.py`:

```bash
python -m venv .venv && . .venv/Scripts/activate   # or bin/activate
pip install -r worker/requirements.txt
```

### Two coordinator settings a local run needs

Both are security controls doing their job, not obstacles to route around,
so they are raised deliberately and only for the harness's own stack:

| Variable | Default | Why a load run needs more |
|---|---|---|
| `REGISTER_RATE_LIMIT_PER_MINUTE` | 5 | Per source IP. A hundred workers arriving from **one** address is exactly the mass fake registration §12 says to refuse. A real fleet arrives from a hundred addresses; a harness cannot. |
| `TASK_API_RATE_LIMIT_PER_MINUTE` | 300 | The harness is a program, not an operator: it samples depth on a timer and pages the whole batch back. Decision #125 already notes a deployment driving these endpoints from one place should raise it. |

`docker-compose.yml` exposes both. **Neither is raised on staging or
production**, which is why the Internet run in §5 is deliberately small.

---

## 2. Scenarios

| Scenario | What it answers |
|---|---|
| `burst` | One batch at a connected fleet. Throughput, latency percentiles, and the zero-loss count. |
| `sustained` | A fixed offered rate held for a fixed time. Does the pipeline keep up, or does the queue accumulate? |
| `mixed` | Four task types and four durations in flight together. Makes head-of-line queueing visible instead of averaging it away. |
| `saturation` | The same batch at increasing fleet sizes. Where the ceiling is, and what it is made of. |

```bash
python scripts/loadtest.py sustained   --workers 25 --rate 60 --seconds 60 --insecure
python scripts/loadtest.py mixed       --workers 25 --tasks 2000 --insecure
python scripts/loadtest.py saturation  --ramp 5,10,25,50,100 --tasks 2000 --insecure
```

### What the numbers mean, and what they do not

**Burst latency is a drain measurement, not a per-task one.** A 10,000-task
batch offered at once to a pipeline that runs ~110/s takes ~90 seconds to
clear, so almost all of every task's end-to-end time is queue wait. That is
arithmetic, not a defect. **The per-task latency figure is `sustained` at a
rate the pipeline can serve** — §4.2.

**Every latency is coordinator-observed.** `end_to_end_seconds` is
`created_at` to `completed_at` and `queue_wait_seconds` is `created_at` to
`assigned_at`, both read back from the task rows through `GET /tasks`. Every
timestamp comes from one Postgres clock, so nothing depends on the
harness's clock agreeing with anything, and nothing is worker-reported —
which matters, because every worker is untrusted (§12).

**Percentiles are nearest-rank**, so every figure reported is a latency some
task actually had and can be checked against its row.

---

## 3. Honest limits of the harness

Stated here so no number below is read as more than it is (§10).

1. **It is N sessions from one process, not N machines.** The coordinator
   side is entirely real — real registrations, real tokens, real WebSocket
   sessions, real result envelopes, and the coordinator cannot tell one of
   these from a container or a laptop (§3.5). The worker side shares one
   event loop, one CPU and one network stack.
2. **`sleep` tasks are awaited, not executed.** A sleep workload consumes no
   CPU by definition, so simulating it with `asyncio.sleep` is faithful, and
   400 concurrent slots would otherwise need 400 OS threads. **The other
   three types run the real executor**, so their results are genuine.
3. **No bounded pending-result buffer.** The real worker's Decision #112
   machinery is worker-side state that does not change the load the
   coordinator sees. Results are retried until acked; that part is kept.
4. **The harness was measured, not assumed, not to be the bottleneck** —
   see §4.4.

---

## 4. Measured results

Every figure in this section was measured on **2026-08-03**. Conditions are
stated with each table because none of them transfers to other hardware.

**Local environment.** Intel i5-4460S, 4 physical cores / 4 logical,
15.9 GB RAM, Windows 11; Docker Desktop 29.6.2 with 4 CPUs and 7.7 GiB.
Compose project `dcds28`: **one** coordinator container (`python:3.12-slim`,
no CPU limit), `postgres:16-alpine`, `redis:7-alpine`, TLS throughout on
the dev CA. Workload `count_to_n {"n": 1000}` unless stated. Simulated
workers declare **4 credits** each.

### 4.1 Burst — 10,000 tasks across 100 workers

The Step 2.8 exit criterion, run three times.

| run | throughput | drain | p50 | p95 | p99 | max | coordinator CPU | completed |
|---|---|---|---|---|---|---|---|---|
| 1 | **114.0/s** | 87.9s | 45.13s | 83.96s | 87.51s | 88.21s | 96.16% | **10,000 / 10,000** |
| 2 | **84.5/s** | 118.7s | 72.52s | 115.35s | 118.82s | 119.57s | 95.64% | **10,000 / 10,000** |
| 3 | **111.3/s** | 90.2s | 45.70s | 85.91s | 89.54s | 90.39s | 97.64% | **10,000 / 10,000** |

All three runs: 100 of 100 workers connected, every worker got work,
**0 duplicate assignments, 0 tasks lost, 10,000 stored results**, peak queue
depth 9,780–9,888. Bulk enqueue of 10,000 tasks in **0.763s** (one
`POST /tasks`).

**Reproducibility, honestly (exit criterion 5).** The **pass/fail properties
are perfectly reproducible** — three runs, three times zero loss, zero
duplicates, every completion carrying a result. **The throughput figure is
not**: 84.5–114.0/s is a **26% spread** around a median of 111.3/s. Run 2 is
the outlier and nothing in the run explains it beyond host contention. Read
the throughput as **~110/s with a quarter of run-to-run noise**, and do not
quote a single-run figure as the system's speed. What *is* stable across
all three is the coordinator's CPU: **95.6–97.6% of one core, every time.**
That stability is the real result — see §4.3.

### 4.2 Sustained — the per-task latency figure

25 workers, 60 seconds, two offered rates, each run twice.

| offered | achieved | p50 | p95 | p99 | peak depth | growth over the hold | coordinator CPU | completed |
|---|---|---|---|---|---|---|---|---|
| **60/s** | 60.3/s | 0.390s | 0.711s | 1.427s | 44 | — | 58.75% | 3,600 / 3,600 |
| **60/s** | 59.9/s | 0.372s | 0.553s | 0.679s | 28 | **0** | 54.65% | 3,600 / 3,600 |
| **150/s** | 116.5/s | 9.896s | 17.037s | 17.821s | 2,116 | — | 95.33% | 9,000 / 9,000 |
| **150/s** | 119.4/s | 8.802s | 15.258s | 15.991s | 1,837 | **+1,789** | 94.81% | 9,000 / 9,000 |

**At 60/s the pipeline keeps up exactly** and the queue returns to zero
between batches, so this is where the honest per-task latency lives:
**p50 ~0.38s, p95 ~0.6s, p99 ~0.7–1.4s.** Compare that with §4.1's p50 of
45s for the same task type — the difference is entirely queue wait.

**At 150/s it does not.** The queue climbs monotonically through the hold
(0 → 82 → 204 → … → 2,116) and drains to zero 17.5s after the offer stops.
**Not one task was lost at either rate.** Over-capacity load is queued, not
dropped, which is the durable-queue property of Decision #79 behaving as
designed.

### 4.3 Saturation — where the ceiling is, and what it is

2,000 tasks per step, only the fleet size changed.

| workers | throughput | p50 | p95 | p99 | drain | coordinator CPU | completed |
|---|---|---|---|---|---|---|---|
| 5 | 107.5/s | 9.24s | 17.92s | 18.64s | 18.8s | **87.29%** | 2,000 / 2,000 |
| 10 | 120.1/s | 8.62s | 16.07s | 16.69s | 16.9s | **86.18%** | 2,000 / 2,000 |
| 25 | **124.0/s** | 8.29s | 15.60s | 16.23s | 16.3s | **90.81%** | 2,000 / 2,000 |
| 50 | 118.3/s | 8.80s | 16.30s | 16.96s | 17.2s | **91.67%** | 2,000 / 2,000 |
| 100 | 110.5/s | 9.52s | 17.49s | 18.22s | 18.3s | **89.60%** | 2,000 / 2,000 |

**The saturation point is ~110–124 tasks per second for one coordinator
process, and it is reached at five workers.** Twenty times the fleet buys
nothing: throughput peaks at 25 workers and is *lower* at 100 than at 10.
The coordinator sits at 86–92% of one core at **every** step.

**This is a different result from Decision #135 and does not contradict it.**
#135 measured *operator page latency* degrading with fleet size against a
background of tasks, and concluded the degradation tracked fleet size. What
is measured here is *pipeline throughput*, and it is bounded by one Python
process on one core no matter how many workers are attached. Both are the
same underlying ceiling — a single-process coordinator — seen from two
directions. **#135's outstanding number is now this table.**

### 4.4 Which component is actually saturated

Sampled during a 25-worker / 4,000-task burst, so the attribution is
measured rather than inferred:

| component | CPU (percent of one core) |
|---|---|
| **coordinator** | **92 – 112%** |
| postgres | 43 – 60% |
| redis | 3 – 7% |
| the harness process itself | **45.3%** |

The coordinator is pinned; Postgres has roughly half a core spare, Redis is
idle, and **the harness has more than half a core of headroom, so it is not
the thing being measured**. Two of the host's four cores are in use, so the
host is not saturated either. Coordinator RSS held at **~131 MB** and did
not grow across a 10,000-task run (peak 128.7 MB reported by its own
`process_resident_memory_bytes`).

**The answer to this ceiling already exists and is already proven:** §3.9
horizontal scaling, demonstrated in Step 1.5.7 with three replicas
autoscaling to five. A single Compose container has no horizontal anything.

### 4.5 Mixed durations — head-of-line queueing

25 workers, 2,000 tasks, 500 of each type, enqueued in the order listed.

| type | p50 | p95 | p99 | max | completed |
|---|---|---|---|---|---|
| `count_to_n {"n":1000}` | 2.29s | 4.14s | 4.24s | 4.27s | 500 / 500 |
| `hash_rounds {"rounds":20000}` | 12.46s | 20.83s | 20.95s | 20.98s | 500 / 500 |
| `sleep {"seconds":2}` | 26.83s | 31.53s | 31.72s | 31.80s | 500 / 500 |
| `opaque_payload` | **32.66s** | 34.38s | 34.48s | 34.51s | 500 / 500 |

2,000 of 2,000 completed, 0 duplicate assignments.

**`opaque_payload` is the cheapest of the four workloads and has the worst
end-to-end latency**, purely because it was enqueued last, behind 500
two-second sleeps. That is head-of-line queueing, and it is worth stating
plainly: **M2 schedules by `priority` and then FIFO, so a cheap task
enqueued behind expensive work waits for it.** Nothing here is a defect —
it is the documented behaviour made visible, and the `priority` column
(lower is more urgent) is the lever that exists today.

---

## 5. Against staging, over the real Internet

Exit criterion 6.

```bash
python scripts/loadtest.py burst \
  --url https://dcds-staging.centralindia.cloudapp.azure.com \
  --enrollment-secret "$ENROLLMENT_SECRET" --admin-secret "$ADMIN_SECRET" \
  --workers 3 --tasks 300 --connect-batch 1 --connect-pause 3
```

**No `--insecure`** — the public endpoint carries a Let's Encrypt
certificate, so a run that completes is also a certificate validation.

**The fleet is small on purpose and this is the finding, not a shortcut.**
Staging runs the shipped defaults: `REGISTER_RATE_LIMIT_PER_MINUTE` is **5
per source IP** and the ingress adds `limit-rps: 5`. A load harness on one
laptop is one source address, so **the Internet run is capped at five
registrations a minute by a control that is working correctly**. The local
run in §4 raises that limit against its own stack; staging is deliberately
left as deployed. What the Internet run proves is the *path* — public DNS,
the Let's Encrypt certificate, the nginx ingress, the WebSocket upgrade,
assignment, execution, result submission and completion across the real
network — not the throughput ceiling, which §4 owns.

Two other consequences of running over the public ingress:

* **`/metrics` is not routed publicly**, on purpose. Coordinator CPU and RSS
  are therefore reported as `null` in an Internet run. They are absent, not
  zero, and not estimated (§10).
* **Staging runs three coordinator replicas** behind a Service, so an
  Internet run is not measuring the single-process ceiling of §4.3 at all.

### 5.1 The run, 2026-08-03

Against `https://dcds-staging.centralindia.cloudapp.azure.com` on
`3949c53b893309b65113171a4aadf1f1c9209d66`, 3 workers × 4 credits, 300
`count_to_n {"n":1000}` tasks, **no `--insecure`**:

| | |
|---|---|
| **Result** | **PASS — 300 / 300 `COMPLETED`**, 300 stored results, 300 distinct rows, **0 duplicate assignments** |
| Throughput | **93.2 tasks/second** over the public ingress |
| End-to-end latency | **p50 1.745s, p95 3.128s, p99 3.243s**, max 3.289s |
| Queue wait | p50 1.630s, p95 3.015s |
| Bulk enqueue | 300 tasks in **0.333s** |
| Coordinator CPU / RSS | **`null`** — `/metrics` is not routed publicly |
| Rate-limit retries | **0** |

**The certificate validated.** The run completed with no `-k` and no
`--insecure`, so a real Let's Encrypt chain was verified from the host.

**`delivery.delivered` was 222 against 300 completed, and that is the
interesting number.** The other **78 tasks were executed by staging's own
`demo-worker`** — a real deployed fleet member (Decision #121) that the
harness has no visibility into. Nothing was lost and nothing was
duplicated; the work was simply shared with a worker the harness did not
create. It is also what forced Decision #146: judging the drain purely on
acknowledgements arriving at the harness's own workers means a run can
**never finish** in an environment that has workers of its own, and the
first attempt did exactly that — every task `COMPLETED` in the database
within a minute while the harness waited on acks that were never coming.

---

## 6. In CI, on a schedule

`.github/workflows/loadtest.yml` runs the `burst` scenario every Monday at
04:17 UTC, and on demand via `workflow_dispatch` with the scenario, fleet
size and task count as inputs. It stands up the whole stack in the runner
with generated certificates and throwaway credentials, runs the harness,
attaches the JSON report as an artifact and tears the stack down.

**It is deliberately not a required check on `main`.** A load test is a
timing measurement on shared, noisy hardware, and a flaky timing gate on
every pull request trains people to re-run it until it passes. What it does
assert is the part that is not a timing measurement and must never regress:
every task enqueued is read back, completed, carries a stored result, and
was assigned exactly once. Those hold at any speed. The throughput and
latency figures it produces are recorded as an artifact, **not** compared
against a threshold — a hosted runner is not the machine §4 was measured on.

---

## 7. Reading a report

```json
{
  "scenario": "burst",
  "fleet":    {"requested": 100, "connected": 100, "credits_total": 400},
  "enqueue":  {"requested": 10000, "accepted": 10000, "seconds": 0.763},
  "throughput": {"tasks_per_second": 114.0, "source": "coordinator completed_at span"},
  "latency":  {"end_to_end_seconds": {"p50": 45.13, "p95": 83.96, "p99": 87.51}},
  "delivery": {"delivered": 10000, "distinct": 10000, "duplicate_assignments": 0},
  "read_back": {"rows": 10000, "completed": 10000, "with_result": 10000},
  "checks":   {"every_task_completed": true, "no_duplicate_assignments": true}
}
```

| Field | Read it as |
|---|---|
| `checks` | The verdict. **Any `false` exits 1.** |
| `read_back` | The coordinator's own rows for **this batch's correlation id** — the authoritative count, not the harness's tally |
| `delivery.*` | Everything the fleet was handed, which on a queue that was not empty beforehand includes **older tasks that were waiting**. `delivery.distinct` legitimately exceeds `read_back.rows` in that case; it is not a duplicate |
| `delivery.duplicate_assignments` | A task id delivered to two workers. Must be 0 |
| `throughput.source` | `coordinator completed_at span` where the stamps span a window, `harness wall clock` where they do not |
| `coordinator.queue_depth_series` | `[seconds, depth]` pairs — the difference between "drained" and "never built up" |
| `coordinator.*cpu*` | `null` means `/metrics` was unreachable (normal against a public ingress), never zero |
| `rate_limited_retries` | How often a 429 was backed off. Large means the run spent its time waiting, and its timing means something different |

---

## 8. What this harness does not do

* **No fault injection.** Killing a coordinator mid-drain, partitioning a
  worker, expiring a lease — Phase 3 owns reassignment and recovery, and
  building half of it here is the mistake that produced the demo-worker
  drift.
* **No multi-host fleet.** §3 item 1.
* **No assertion against a performance budget.** There is no agreed target
  to assert against; §4 is the first measurement, and a budget set from one
  laptop's numbers would be arbitrary.
* **No cleanup.** Every run leaves its worker rows and task rows behind, by
  design — the task row is the audit trail (`docs/operator-api.md` §9), and
  a harness that deleted its own evidence could not be checked afterwards.
  Use a fresh Compose project, or `down -v`, to start clean.
