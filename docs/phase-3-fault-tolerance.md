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

# Step 3.0 — the decided design (2026-08-03)

**Status: BUILT AS A DESIGN, AWAITING APPROVAL. No code has been written.**
Decisions **#152–#168** in `PHASE_STATE.md`.

Everything below was checked against the code that is actually deployed,
not against what the phase plan assumed. Where the two disagree, the code
wins and the disagreement is named.

## 3.0.1 What M3 actually has to add

M2 already ends with a task that is **durably recorded and permanently
stuck** when its worker vanishes. `assignment.log_unacknowledged` names
the stranded tasks and deliberately recovers none of them
(`coordinator/app/assignment.py:342`), `task_queue` has no requeue
primitive on purpose (`coordinator/app/task_queue.py:30`), and
`lease_expires_at` / `attempt_count` exist and are written by nothing
(`coordinator/app/models.py:140`). Staging is carrying ~20,636 rows in
exactly that condition.

So M3 adds **one missing loop**: something that notices a task is not
progressing and puts it back in play, safely, across replicas, without
ever completing it twice.

Everything else M3 needs is already shipped and was verified in place
before this gate was written:

| Already there | Where | M3 uses it for |
|---|---|---|
| `lease_expires_at`, `attempt_count` columns | `models.py:141-144` | lease and retry, **no migration for the core** |
| `attempt_number`, `session_epoch`, `idempotency_token` on the wire | `results.py:127-131` | fencing, **no protocol change** |
| Row-locked, atomic completion returning `DUPLICATE` | `task_queue.complete_task:378` | duplicate suppression, already structural |
| `FOR UPDATE SKIP LOCKED` claim proven across 3 replicas | `task_queue._dequeue_sql:189` | the reclaimer reuses the same primitive |
| Cooperative cancel flag in every executor | `worker/executors.py:92` | stop work that has been reassigned |
| Worker ignores unknown message types | `worker/worker.py:1236-1264` | a new `task_cancel` is backward compatible |
| Idempotent per-replica sweep with no leader election | `task_queue.purge_expired_results:652` | the reclaimer follows the same pattern |
| Coordinator-observed liveness, never worker-supplied clocks | `main._sweep_heartbeats:397` | the crash/slow distinction |

## 3.0.2 The spine of the design

**One recovery trigger: lease expiry in Postgres.** Nothing else in the
system is allowed to reclaim a task — not a closed socket, not `OFFLINE`,
not a missed heartbeat, not a progress percentage.

That is the whole design in one sentence, and it is chosen over the
obvious alternative ("reclaim when the worker goes OFFLINE") for a
concrete, code-level reason:

> `_sweep_heartbeats` marks a worker `OFFLINE` when its Redis metrics key
> is missing — `elapsed is None` and the key holding it is a TTL'd Redis
> key (`main.py:416-427`). **If Redis is flushed, restarted, or
> unreachable, every worker in the fleet is declared OFFLINE within one
> sweep.** In M2 that is cosmetic. If OFFLINE reclaimed tasks, a Redis
> blip would reassign the entire in-flight fleet at once — a self-inflicted
> outage caused by the recovery system.

Lease expiry has none of that coupling: it is a timestamp column in
Postgres, compared against Postgres's own clock, by a query that touches
no Redis at all. **Coordinator-side expiry detection that does not depend
on the worker being reachable** — which is what Step 3.1 asks for — falls
out for free, and so does "does not depend on Redis being healthy", which
the plan did not ask for and needs anyway.

Fast detection is not sacrificed. A socket close **is** coordinator-
observed, so the replica that held it *shortens* the lease to a short
grace instead of reclaiming. That accelerates the one mechanism rather
than adding a second one, and it cannot storm: it only affects tasks
owned by workers whose sockets that replica personally held.

## 3.0.3 The failure taxonomy

Detection is **coordinator-observed in every row**. "Observable outcome"
is what a person sees in the browser, not what a log says.

| # | Failure | Detection | Recovery action | Observable outcome |
|---|---|---|---|---|
| 1 | Worker process crash (`docker kill`, OOM) | Socket close observed → lease shortened to `LEASE_DISCONNECT_GRACE`; if the close was never seen, lease simply expires | Reclaimer requeues: `attempt_count + 1`, previous worker excluded, `not_before` set to the retry backoff | Worker leaves the fleet panel; its task returns to `QUEUED` with attempt 2 and a reassignment event on the task's recovery timeline |
| 2 | Worker machine power loss | No close frame at all — lease expiry only | Same as #1, up to `LEASE_TTL + RECLAIM_INTERVAL` later | Same, with a visibly longer detection gap; the timeline records both timestamps |
| 3 | Network disconnection mid-task | Socket close or lease expiry, whichever the coordinator sees first | Requeue as #1. If the worker reconnects inside the grace, `hello` renews the lease **from the database** and nothing is reassigned | Either a clean reconnect with the task still `RUNNING`, or a reassignment — both visible live |
| 4 | Coordinator pod eviction | Nothing to detect: the tasks are rows, the sockets die with the pod | Workers reconnect to another replica; that replica renews their leases on `hello`; anything not reclaimed by a reconnect expires normally | Fleet re-attaches within the reconnect backoff; queue depth unchanged; no task changes state |
| 5 | Coordinator crash mid-assignment | Commit-before-send (`assignment.py:34`) means the row is already `ASSIGNED`; the send may never have happened | Lease expires (nothing ever renewed it) → requeue | Task sits `ASSIGNED` for at most one lease, then reappears `QUEUED` with attempt 2 |
| 6 | Duplicate task completion | `complete_task` locks the row and finds it `COMPLETED` | Returns `DUPLICATE`, writes **nothing**, acks the worker with `accepted: true` | Exactly one result row; the duplicate is counted in `coordinator_task_results{outcome="duplicate"}`, not shown as an error |
| 7 | Worker timeout (slower than its type's cap) | `MAX_EXECUTION_SECONDS` for the task type is passed — a hard cap that renewal cannot extend | `task_cancel` sent, attempt recorded as `EXPIRED`, task requeued or `FAILED` if attempts are exhausted | Attempt row with reason `execution_deadline_exceeded`, visible on the task's timeline |
| 8 | Stale result after reassignment | `attempt_number` in the envelope ≠ `tasks.attempt_count`, or the sender is not `assigned_worker_id` | Result **rejected**, task state untouched, attempt row written with outcome `FENCED`, worker acked definitively so it stops retrying | The task's timeline shows a fenced submission naming the worker and the superseded attempt; `coordinator_results_fenced_total` increments |
| 9 | Partial task completion | Not detected, by design — see §3.0.6 | Full re-execution | Attempt count increments; no partial state exists to display |
| 10 | Worker reconnect after failure | New `hello` on an existing identity | Coordinator queries its own non-terminal tasks for that worker and renews their leases — **worker-declared ids are never trusted** | Worker returns `ONLINE`; its still-running tasks stay `RUNNING` rather than being reassigned |
| 11 | Alive but not progressing (hung executor) | Heartbeats continue, but nothing renews the task's lease | Lease expires → requeue exactly as for a crash. The worker is **not** quarantined; that is Phase 4 policy | Worker stays `ONLINE` while its task is visibly reassigned — the case that proves recovery is task-scoped, not worker-scoped |
| 12 | Malformed result | `results.validate` refuses it before any write (shipped in 2.5) | Nothing written; definitive rejection ack; attempt row `FENCED` with the reason code | Rejection reason visible on the task; task remains `RUNNING` until its lease expires, then retries |
| 13 | Database unavailable | `/ready` fails; every dequeue, reclaim and completion raises | **Stop**, do not improvise: no assignment, no reclaim, no completion. Sockets stay open, workers keep executing, results buffer in the worker's `pending_results` and retry | Dashboard shows `coordinator_unreachable` / not-ready; **no reassignment storm**, because the reclaimer needs the same database it has lost |
| 14 | Redis unavailable | `/ready` reports it; the doorbell stops ringing | Assignment falls back to the 30s safety-net poll; liveness display degrades; **task recovery is unaffected** | Fleet panel goes stale, queue keeps draining. This is the failure mode that made the design put leases in Postgres |

Two of these — 13 and 14 — are the reason the taxonomy is worth writing.
Both are cases where the *recovery system itself* is the thing most
likely to cause the outage, and in both the answer is to do less.

## 3.0.4 Explicitly NOT recovered

Stated so nobody has to discover it during a demo.

1. **A worker that returns a wrong answer.** Every worker is untrusted
   (§12), but V1 does no cross-verification, no quorum, no redundant
   execution. A confidently wrong result is stored as the truth. Detecting
   it needs duplicate execution and comparison, which is a scheduling
   decision and is not in M1–M4.
2. **A poison task beyond `max_attempts`.** Terminal `FAILED`, kept
   visible and inspectable, never retried again and never silently
   dropped. This is a designed stop, not a gap.
3. **Loss of the Postgres volume.** The in-cluster Postgres is a single
   pod with no replica, no PITR and no backup job. **If it is lost, every
   task, result and worker identity is lost with it, and no part of M3
   changes that.** M3 makes the system survive workers and coordinators,
   not its own database. Naming it here because "fault tolerance" is
   otherwise easy to read as covering it. Fixing it is a real piece of
   infrastructure work and is not smuggled into this phase.
   **⇒ ACCEPTED AS A KNOWN RISK by the user on 2026-08-03 (Decision
   #171). M3 builds nothing for this, tests nothing for it, and no M3
   exit criterion depends on it.** Recorded here so it is an owned
   acceptance rather than an assumption. **The cheap upgrade path,
   written down now so taking it later needs no rediscovery:** a
   `CronJob` in the chart running `pg_dump` from the image Postgres
   already uses. That protects against logical loss — a bad migration, an
   accidental `DELETE`, a corrupt table — and **it does NOT protect
   against loss of the volume itself unless the dump is written somewhere
   other than that volume**, which needs external storage and therefore
   credit. Stated in full so the limitation is not discovered after
   trusting it.
4. **A task whose worker completes it while the coordinator is
   permanently gone.** The result buffer is memory-only and bounded at 64
   (`worker.py:241`), abandoned on shutdown by deliberate choice (§3.6,
   workers are stateless between tasks). The task is re-executed by
   someone else instead.
5. **Duplicate execution.** Not a fault to be recovered — see the
   guarantee below.

## 3.0.5 Delivery guarantee, stated exactly

> **At-least-once execution. Exactly-once completion. At most one stored
> result per task.**

- A task **may execute more than once**. A worker that is merely slow, or
  partitioned, keeps running work that has already been reassigned. That
  is accepted, not prevented.
- A task reaches `COMPLETED` **exactly once**, and stores **exactly one**
  result. This is structural, not a check that could be forgotten: the row
  is locked `FOR UPDATE` before its status is read, and an already-
  completed task returns `DUPLICATE` having written nothing
  (`task_queue.py:378`). Two replicas racing the same submission serialise
  on that lock.
- **Not exactly-once execution, and it is not achievable here.** It would
  require the execution and the completion record to commit atomically,
  and they are on different machines with a network between them.
- **No ordering guarantee across tasks.** `SKIP LOCKED` gives strict
  `(priority, created_at)` order to a single dequeuer only — already
  documented and unchanged (`task_queue.py:12`).

## 3.0.6 Mechanism detail

### Lease lifecycle

```
delivered      → lease_expires_at = now() + ACK_TIMEOUT
task_started   → lease_expires_at = now() + LEASE_TTL,     deadline_at = now() + MAX_EXECUTION
any observed message naming the task (ack, started, progress, result attempt)
               → lease_expires_at = min(now() + LEASE_TTL, deadline_at)
socket close observed by the replica holding it
               → lease_expires_at = min(lease_expires_at, now() + DISCONNECT_GRACE)
hello from that worker
               → leases renewed for its own non-terminal rows, read from the DB
```

**Renewal is worker-driven and therefore untrusted, so it is capped.**
`deadline_at` is coordinator-set at start and no renewal can push past it
— a worker that renews forever still loses the task at its type's
execution cap. The distinction that matters for §12: *when* a lease is
renewed depends on the worker sending something; *how long* it can ever
live does not.

**Renewal writes are lazy.** A renewal only touches the database when the
remaining lease has fallen below half of `LEASE_TTL`. At the defaults
below that is roughly one UPDATE per running task per 30s — ~3.3 writes/s
at 100 concurrent tasks, against a pipeline measured at 110–124 tasks/s
(#141). Renewing on every 10s progress message instead would have tripled
that for nothing.

### The crash / slow-worker distinction, made objective

The decision input is **whether a coordinator-observed message about that
task arrived inside the window**. Nothing else. In particular:

- **`progress` values are never an input.** They are worker-reported
  telemetry, Redis-only, and a lying worker can report 0.99 forever. Their
  *arrival* renews the lease; their *content* decides nothing.
- **Worker status (`ONLINE`/`SUSPECT`/`OFFLINE`) is never an input.** It
  is a fleet-view concern and it depends on Redis (see §3.0.2).
- A worker that is alive and hung is therefore treated exactly like a
  crashed one, at the task level, and left alone at the fleet level. That
  asymmetry is the correct one: the task is what is stuck.

### The reclaimer

One loop per replica, no leader election — the same shape as the shipped
retention sweep and for the same reason (§3.9, no replica holds
authoritative state):

```sql
WITH expired AS (
    SELECT id FROM tasks
    WHERE status IN ('ASSIGNED','RUNNING')
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at < now()
    ORDER BY lease_expires_at
    LIMIT :batch
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks t SET ... FROM expired WHERE t.id = expired.id
```

Double-reclaim is impossible for the same reason double-assignment is:
the second replica's `SKIP LOCKED` steps over the locked row, and by the
time the lock lifts the row no longer matches. This is the identical
primitive already measured at 10,000 claims / 0 duplicates across three
AKS pods in Step 2.2 — the reclaimer inherits that proof rather than
inventing a new mechanism to be proven from scratch.

**The reclaimer never touches in-memory credit.** It usually runs on a
replica that does not hold the worker's socket, so it cannot. Credit is
released by the socket-holding replica when the worker responds, or dies
with the session (`assignment.py:449`). A worker that loses a task it is
still running gets over-committed at worst, refuses `at_capacity`, and
the shipped saturation rule (#101) absorbs it.

### Requeue, retry and exclusion

On reclaim, inside one transaction:

- `attempt_count + 1`; if that reaches the type's `max_attempts` →
  `FAILED`, terminal, with the reason on its attempt row.
  **`attempt_count` is zero-based** — it is already on the wire as
  `attempt: 0` for a first delivery (`assignment.py:920`), so
  `max_attempts = 3` means executions numbered 0, 1, 2. Written out
  because this is exactly where an off-by-one hides.
- otherwise `status = QUEUED`, `assigned_worker_id = NULL`,
  `excluded_worker_id = <the worker that lost it>`,
  `not_before = now() + full_jitter_backoff(attempt)`.
- an attempt row is written recording what happened.

The dequeue predicate gains two conditions:

```sql
AND (not_before IS NULL OR not_before <= now())
AND (excluded_worker_id IS NULL
     OR excluded_worker_id <> :worker_id
     OR not_before + make_interval(secs => :exclusion_window) <= now())
```

**Exclusion expires, deliberately.** A permanent exclusion starves the
task to death on a single-worker fleet — which is exactly the shape of a
laptop demo and of a hotspot Internet worker. Starving a task forever to
avoid one worker is worse than eventually retrying on it, so exclusion is
a bounded window and the window is configuration.

`not_before` doubles as the retry-backoff clock and the exclusion clock.
One column, one meaning: *this row is not eligible yet*.

### Fencing (Step 3.4)

**Accept a result if and only if the sender is the current
`assigned_worker_id` and `attempt_number` equals `tasks.attempt_count`.**
Anything else is fenced: nothing written, definitive rejection ack, one
attempt row with outcome `FENCED`.

The classic race resolves itself with no extra machinery. Worker A's
result is in flight; the lease expires; the task goes to B; both results
arrive. Whoever's submission wins the row lock while it still matches the
current attempt completes the task; the other is fenced. If A's result
lands *before* the reclaimer runs, A wins and the reclaimer's UPDATE
simply matches nothing — its `WHERE status IN ('ASSIGNED','RUNNING')` no
longer holds.

**`session_epoch` is NOT a fencing input, and the phase plan's wording is
wrong on this point.** Step 2.5 *measured* a legitimate result executed
under session epoch 4 and submitted on epoch 5, after a reconnect — that
is the coordinator-outage criterion working as designed. Rejecting on a
stale epoch would break the exact behaviour a previous step proved. The
epoch is recorded and logged; **session** conflicts are already arbitrated
by the shipped Step 1.7 epoch check (`main.py:867-898`), which is a
different question from result validity.

### Cancelling work that has been reassigned

A reclaimed task's original worker may still be burning CPU on it. The
coordinator sends a `task_cancel` naming the task; the worker sets the
cooperative flag the executors already check between chunks
(`executors.py:92`).

It is best-effort by design — the worker may be unreachable, which is why
the task was reclaimed in the first place. No behaviour depends on it
arriving; it saves CPU and nothing more. It needs **no protocol version
bump**: the worker's dispatch already falls through unknown message types
to a log line (`worker.py:1236-1264`), verified before this was chosen.

**Routing:** the reclaimer usually runs on a replica that does not hold
the worker's socket, so the cancel goes over the shipped
`worker:{id}:push` Redis channel that `POST /workers/{id}/push` and
`_push_listener` already implement (`main.py:847`). No new fan-out
mechanism.

**The worker must answer a coordinator-initiated cancel with a
`capacity` naming the task.** Its shipped cancel path deliberately reports
nothing (`worker.py:889`) because until now cancellation only ever
happened during shutdown, when the socket was going away anyway. Under M3
that would leak a credit for the life of the session.

## 3.0.6a One shipped rule that becomes wrong under M3

Found by re-reading the credit accounting against the new reassignment
path, not by running anything.

`handle_task_result` and `handle_task_failed` release **no credit** when
the database answers `NOT_OWNER` or `NOT_FOUND` (`assignment.py:805`,
`assignment.py:666`). In M2 that is correct and is a §12 protection: the
only way to reach `NOT_OWNER` is a worker naming a task that is not its
own, and honouring that would let one worker free its own slots by
guessing another's task ids.

**M3 makes `NOT_OWNER` reachable honestly.** A reassigned task's row no
longer names its original worker, so that worker's perfectly sincere
late result now returns `NOT_OWNER` — and the shipped rule then holds its
credit consumed for the rest of the session. A worker that loses three
tasks to reassignment quietly stops being able to accept work.

**Fix, and it needs no new concept:** release the credit when *this
session actually delivered that task id* — `task_id in session.credited`
— which is the exact discipline the malformed-result path already applies
(`assignment.py:763`). A guessed id is not in `credited`, so the §12
protection is untouched; a genuine one is, so the slot is freed. Decision
**#168**. This is a Step 3.4 implementation constraint, and it is the sort
of thing that would otherwise have surfaced as "the fleet mysteriously
slows down during chaos runs".

## 3.0.7 State machine and schema

**New transitions:** `ASSIGNED → QUEUED` and `RUNNING → QUEUED`.

**`REASSIGNED` becomes an *attempt* outcome, never a task status.** The
queue is `WHERE status = 'QUEUED'` (`task_queue.py:194`), so a recovered
task must actually be `QUEUED` to be claimable; a `REASSIGNED` status
would either be a state nothing can observe or a second queue predicate to
maintain forever. The reserved constant is used where it is meaningful:
the attempt that was superseded.

**This contradicts two shipped docstrings** — `task_states.py:11` and
`task_queue.py:30` both say returning a task to the queue *is* the Phase 3
`REASSIGNED` transition. They were written before the queue predicate
existed in the form it now has. The contradiction is named here rather
than quietly resolved, and the docstrings get corrected in Step 3.2.

**Migration `0006`** (one migration for the whole milestone's schema):

| Change | Why |
|---|---|
| `tasks.excluded_worker_id uuid NULL` | retry exclusion |
| `tasks.not_before timestamptz NULL` | retry backoff + exclusion window |
| `tasks.deadline_at timestamptz NULL` | the renewal cap |
| partial index on `lease_expires_at WHERE status IN ('ASSIGNED','RUNNING')` | the reclaimer's scan; keeps it off `ix_tasks_queue` |
| `task_attempts` table | recovery timeline, per-worker failure counters |
| `task_policies` table | per-type timeouts changeable without a redeploy |
| backfill: pre-M3 stranded rows → `FAILED` | see below |

**`task_attempts` records only attempts that did *not* complete normally.**
A row per attempt always would add a second write to the hot assignment
path on a pipeline already sitting at 92–112% of one core (#141), to
record that nothing went wrong — and a healthy task's timeline is already
fully derivable from `created_at / assigned_at / started_at /
completed_at`. Abnormal endings are exactly what a recovery timeline is
for. Happy path keeps its single write.

**The 20,636 stranded staging rows are closed as `FAILED`, not
resurrected.** Treating a NULL lease as "expired" would have been elegant
and would have dumped ~20k months-old tasks into the queue on the first
production rollout, at a coordinator whose measured ceiling is ~110–124
tasks/s. The backfill marks them terminal with reason
`stranded_pre_m3` and an attempt row each, which is honest, visible, and
finally closes Decision #91's carried item.

## 3.0.8 Configuration

**Every number below is a recommendation, not a measurement** (§10). The
measured ones are named as such. Defaults live in code; per-type overrides
live in `task_policies` and take effect without a redeploy.

| Setting | Default | Basis |
|---|---|---|
| `TASK_ACK_TIMEOUT_SECONDS` | 30 | ~3x the **measured** worst heartbeat gap under saturation (10.52s, Step 2.9) |
| `TASK_LEASE_TTL_SECONDS` | 60 | 6 worker progress intervals (**measured** cadence: 10s, `worker.py:191`) |
| `TASK_LEASE_RENEW_FRACTION` | 0.5 | renew when under half remains — one write per ~30s per running task |
| `TASK_MAX_EXECUTION_SECONDS` (`sleep`) | 3900 | the type's own parameter cap is 3600s (`task_types.py:62`) plus slack |
| `TASK_MAX_EXECUTION_SECONDS` (`hash_rounds`) | 300 | ceiling parameters **measured** at 15.4s on one core; ~20x headroom for slow hardware |
| `TASK_MAX_EXECUTION_SECONDS` (`count_to_n`) | 300 | **no measurement exists** for the 100M ceiling — flagged, to be measured in 3.1 |
| `TASK_MAX_EXECUTION_SECONDS` (`opaque_payload`) | 60 | bounded by a 64 KB echo |
| `TASK_MAX_ATTEMPTS` | 3 | one retry for a transient fault, one for an unlucky second, then stop |
| `TASK_RETRY_BACKOFF_BASE/FACTOR/MAX` | 5s / 2 / 60s | mirrors the worker's shipped full-jitter retry (`worker.py:208`) |
| `TASK_RETRY_EXCLUSION_SECONDS` | 60 | bounded anti-starvation window |
| `LEASE_RECLAIM_INTERVAL_SECONDS` | 5 | matches the shipped heartbeat sweep cadence |
| `LEASE_RECLAIM_BATCH` | 100 | matches `TASK_DEQUEUE_MAX_BATCH`; bounds lock hold time |
| `LEASE_DISCONNECT_GRACE_SECONDS` | 30 | longer than the worker's reconnect backoff so a clean reconnect wins |

**The bound this gives Step 3.1 to verify, stated so it can fail:**

- socket close observed → reassignment starts within
  `DISCONNECT_GRACE + RECLAIM_INTERVAL` = **≤ 35s**
- no close observed (power loss) → within
  `LEASE_TTL + RECLAIM_INTERVAL` = **≤ 65s**

## 3.0.9 Observability

New metrics: `coordinator_leases_expired_total`,
`coordinator_tasks_reassigned_total`,
`coordinator_tasks_exhausted_total`, `coordinator_results_fenced_total{reason}`,
`coordinator_tasks_awaiting_retry` (gauge),
`coordinator_lease_reclaim_seconds` (histogram).

New alerts in `prometheusrules.yaml`: sustained reassignment rate,
any sustained fencing, and a reclaim backlog that stops draining — the
third being the one that catches the reclaimer itself having died.

Dashboard v3 (Step 3.7) extends the existing pages and keeps the shipped
2s poll. **No SSE, no WebSocket, no framework**: the poll is already
proven to show a live lifecycle (Step 2.7), and "watch a reassignment
happen" is the same read at the same cadence.

## 3.0.10 Technology choices, and what was rejected

| Choice | Rejected alternative | Why |
|---|---|---|
| Leases in Postgres | Redis TTL + keyspace notifications | Redis is ephemeral by contract (§4); a flush would silently reset recovery state, and the reclaim needs a DB write anyway |
| Per-replica reclaimer, no leader | Leader election (Redis lock / K8s lease) | `SKIP LOCKED` already makes concurrent reclaim safe; a leader adds a failure mode and a split-brain question for zero gain |
| Per-type policy in a Postgres table | env vars; Redis hash; ConfigMap reload | env vars need a restart, so "configurable without redeploy" would be false; Redis would silently revert operator intent on a flush |
| Structural idempotency in Postgres | a Redis dedup set with a retention window | the row lock plus terminal state already answers it, **measured in 2.5**; a second store would be a second truth that can disagree |
| Python chaos script (`scripts/chaos.py`) | Chaos Mesh / LitmusChaos | a new cluster-wide operator, more student credit, another Terraform surface, to do what `docker kill` and `kubectl delete pod` already do in a harness pattern that is already proven (`scripts/loadtest.py`) |
| Keep the coordinator | Temporal / Celery / Kafka | each replaces the coordinator wholesale, which is precisely the "permanent foundation" §1 says is not being rewritten |

## 3.0.11 Shipped decisions this gate changes

| Decision | Effect |
|---|---|
| **#103** — "no execution timeout is added" | **Superseded.** M3 adds a per-type execution deadline; #103's rationale (duration is bounded by parameter validation) is true for *legal* durations and says nothing about a hung executor |
| **#91** — stranded `ASSIGNED` rows are the designed outcome | Closed. The reclaimer is what #91 was waiting for; the pre-M3 backlog is closed as `FAILED` by backfill |
| **#79** — the queue is the `tasks` table | Unchanged, but the dequeue predicate gains two filters inside the same ordered index walk. Same honest cost caveat as the 2.3 type filter; **to be measured in 3.1, not assumed** |
| **#101** — capacity refusal saturates the session | Unchanged and relied upon: it is what absorbs a worker that had a task taken from it |
| **#105 / #98** | Unchanged |

## 3.0.12 §16 escalations — ALL THREE APPROVED 2026-08-03

Raised as three things needing an explicit call. **All three were
approved by the user on 2026-08-03, in the form each recommendation
proposed, with the user's instruction being to take the variant that
costs the least design and implementation time and adds no load to the
running system.** Decisions **#169**, **#170** and **#171**. The wording
in Steps 3.3 and 3.4 that each deviation contradicted is corrected in
this same commit, so no step now asks for something the gate has decided
against.

1. **`session_epoch` is not used for result fencing** (§3.0.6), which
   contradicted the wording of Step 3.4. Using it would break Step 2.5's
   measured reconnect path. **APPROVED — Decision #169.** Fencing is
   `worker == assigned_worker_id AND attempt_number == attempt_count`,
   evaluated inside the row lock the shipped result path already takes.
   **No new column, no new index, no new query and no extra round trip** —
   it is the same `UPDATE … WHERE` M2 ships, with two more predicates.
   `session_epoch` stays on the wire and stays in the logs as a tracing
   field; it simply is not an input to the accept/reject decision.
   Step 3.4's third bullet and its epoch exit criterion are re-worded
   below to match.
2. **No Redis dedup store** (§3.0.10), which contradicted the wording of
   Step 3.3. **APPROVED — Decision #170.** Postgres answers duplicate
   submission structurally: the row lock plus the terminal-state check
   already measured in Step 2.5. **This is the cheapest possible outcome
   because it is a deletion, not a build** — there is no dedup store to
   write, no retention window to tune, no expiry job to run, no extra
   Redis round trip on the hot result path, and no second store that can
   disagree with the first after a Redis flush. Step 3.3's Redis bullet
   and its retention criterion are re-worded below to describe the
   mechanism that actually exists.
3. **Postgres has no replica, no PITR and no backup** (§3.0.4 item 3).
   **APPROVED AS AN EXPLICIT ACCEPTED RISK — Decision #171**, accepted by
   the user on 2026-08-03. **M3 builds nothing for this and claims
   nothing about it.** The risk is stated plainly in §3.0.4 item 3 and is
   not covered by any M3 exit criterion. It is not an open question and
   not a hidden assumption; it is a recorded, owned acceptance. The cheap
   upgrade path, if it is ever wanted, is written down in §3.0.4 item 3
   so that taking it later needs no rediscovery.

**Nothing in these three approvals adds a query, a store, a background
job, a migration or a protocol message.** Two of them remove work that
the step wording would otherwise have required.

## 3.0.13 What each later step inherits

| Step | Inherits from this gate |
|---|---|
| 3.1 | lease lifecycle, the three timeouts, the reclaimer query, `task_policies`, the ≤35s / ≤65s bounds to verify |
| 3.2 | requeue transitions, `max_attempts`, backoff via `not_before`, exclusion window, `task_attempts` counters |
| 3.3 | nothing to build but the tests — idempotency is already structural; the race's winner rule is fixed here |
| 3.4 | the fencing rule, and the epoch deviation |
| 3.5 | no startup scan is needed; recovery is the continuous reclaimer plus lease renewal on `hello` |
| 3.6 | full re-execution, no checkpointing (executors are pure and were fingerprint-verified in 2.4/2.5) |
| 3.7 | attempt timeline, failed-task reason, fencing events, per-worker counters — all from `task_attempts` |
| 3.8 | invariants to assert: zero loss, zero double completion, convergence to no `ASSIGNED`/`RUNNING` rows after chaos stops |
| 3.9 | the demo script already written below is unchanged by any of this |

## 3.0.14 Exit-criteria self-check for 3.0

| Criterion | Where |
|---|---|
| Every failure has detection, recovery and a dashboard outcome | §3.0.3, all 14 rows |
| Crash vs slow-worker distinction objective, not guesswork | §3.0.6 — coordinator-observed message arrival only; progress values and worker status explicitly excluded |
| Failures explicitly not recovered, with reasoning | §3.0.4, five of them |
| Delivery guarantee stated accurately | §3.0.5 |
| Approved before code | **MET — Step 3.0 approved by the user 2026-08-03, all three §16 escalations approved with it (#169–#171). No code had been written at the time of approval.** |

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
- [x] Leases are created, renewed, and expire correctly.
- [x] Killing a worker mid-task causes lease expiry within the documented
      timeout — timed and recorded.
- [x] A legitimate long task renews its lease and is never reclaimed.
- [x] A hung worker that stops renewing is reclaimed.
- [x] Three coordinator replicas running the reclaimer never
      double-reclaim — verified under load.
- [x] Timeouts configurable per task type without redeploy.

---

## 3.1.1 What was built

Migration **0006** carries the whole milestone's schema (gate §3.0.7), so
Steps 3.2–3.4 ship no migration of their own: `tasks.deadline_at`,
`tasks.not_before`, `tasks.excluded_worker_id`, the partial index
`ix_tasks_lease_expiry`, and the `task_attempts` and `task_policies`
tables. Only `deadline_at`, the index, `task_attempts` and `task_policies`
are written in 3.1; the other two columns are Step 3.2's and are named as
such in the migration.

The lease lifecycle, implemented exactly where the gate put it — on the
statements that were already moving the row, so no path gains a round
trip:

| Event | Write |
|---|---|
| `dequeue` | `lease_expires_at = now() + ack timeout`, `deadline_at = now() + execution cap` |
| `task_started` | `deadline_at` re-stamped from the real start; `lease_expires_at = LEAST(now() + ttl, deadline_at)` |
| any observed message about the task | `lease_expires_at = LEAST(now() + ttl, deadline_at)`, **lazily** |
| socket close observed by the holding replica | `lease_expires_at = LEAST(it, now() + grace)` |
| `hello` | every live lease that worker holds, renewed from the database |
| terminal state | `lease_expires_at = NULL`, `deadline_at = NULL` |
| reclaim | `ASSIGNED`/`RUNNING` → `QUEUED`, `attempt_count + 1`, one `task_attempts` row |

`ASSIGNED -> QUEUED` and `RUNNING -> QUEUED` are added to `task_states`,
and the two shipped docstrings that called this the `REASSIGNED`
transition are corrected. The gate assigned that correction to Step 3.2;
it is done here instead because this is the step that adds the transition,
and leaving the code contradicting itself for a step would have been worse
than moving one paragraph early.

New: `GET /tasks/policies`, `PUT /tasks/policies/{type}`,
`DELETE /tasks/policies/{type}` (operator-credentialled);
`coordinator_leases_expired_total`, `coordinator_lease_renewals_total`,
`coordinator_leases_overdue`, `coordinator_lease_reclaim_seconds`; two
alerts; and `lease` on the task console with an overdue state.

## 3.1.2 Three implementation decisions this step made

**1. Renewal laziness lives in the process, not in the database.** Each
session records, on its own monotonic clock, when each of its tasks is
next worth a write. A message that arrives before then costs **no
database round trip at all** — not a no-op UPDATE, no statement. At the
defaults a 10s progress cadence becomes ~1 write per running task per 30s.
The alternative, a guarded UPDATE that self-skips, would still have sent
10 statements/s per 100 running tasks to a table that is also the queue's
hot path (#79).

**2. `deadline_at` is stamped at delivery as well as at `task_started`.**
This is a deviation from the gate's lifecycle table and it was **found by
a live run, not by review** — see 3.1.4.

**3. Decision #168 is pulled forward from Step 3.4.** `handle_task_result`
and `handle_task_failed` now release the credit when the outcome is
`NOT_OWNER` *and* the id is one this session delivered. The gate scheduled
this for 3.4, but 3.1 is what makes `NOT_OWNER` honestly reachable: a
reclaimed task's row no longer names its original worker, so that worker's
sincere late report would otherwise hold its credit for the life of the
session. A worker losing three tasks to reassignment would quietly stop
accepting work. A guessed id is still not in `credited`, so the §12
protection is untouched.

## 3.1.3 Measurements

Every figure below is **measured on the reference laptop** (Intel i5-4460S,
4 cores) against a real Docker Compose stack, Postgres 16. They are
properties of that machine, not of the design.

**The two detection bounds the gate published, tested against their own
numbers** (`DISCONNECT_GRACE 30` + `RECLAIM_INTERVAL 5` = ≤35s;
`LEASE_TTL 60` + `RECLAIM_INTERVAL 5` = ≤65s):

| Failure | How it was caused | Bound | Measured |
|---|---|---|---|
| Worker process killed mid-task | `docker kill` on a worker running `sleep(600)` | ≤ 35s | **31.2s** |
| Worker alive but frozen, no close observed | `docker pause` — the socket stays open, nothing is sent | ≤ 65s | **49.6s** |

The killed case shows the close being observed: one second after the kill
the task's remaining lease had dropped from 56.8s to **29.3s** — the
disconnect grace — and it was reclaimed 1.2s after that expired. The
frozen case never shortened, ran its full TTL down from its last renewal,
and was caught on the next reclaim tick.

**A legitimate long task is never reclaimed.** A `sleep(600)` task watched
for 2.5 minutes — more than four lease windows — held `attempt_count = 1`
and never returned to `QUEUED`, its lease sawtoothing between ~25s and
~59s as progress messages renewed it lazily.

**Three coordinator replicas, under load, zero double-reclaims.** Three
real coordinator processes (`--scale coordinator=3`) against 3,000 tasks
seeded `ASSIGNED` with already-expired leases:

| | |
|---|---|
| drained in | **5.74s** (≈522 reclaims/second) |
| reclaims per replica | **900 / 1100 / 1000** |
| total reclaim log lines | **3,000 — exactly, not 3,001** |
| tasks with more than one attempt row | **0** |
| tasks whose `attempt_count` ≠ 1 | **0** |

An earlier run of the same shape split **0 / 2900 / 100**, and that is
worth recording rather than tidying away: the reclaimer is race-to-claim,
not load-balanced, so whichever replica ticks first takes most of a
backlog. The total was still exactly 3,000. **Even distribution is not a
property this design has or needs; exactly-once reclaim is, and it held in
both runs.**

Reclaim pass duration on an idle-to-light queue: 6 passes totalling
**30.2ms**, i.e. ~5ms each, from `coordinator_lease_reclaim_seconds`.

**Per-type timeouts change with no redeploy.** `PUT
/tasks/policies/hash_rounds {"max_execution_seconds": 1800}` against the
running coordinator moved the effective cap from 300 (`source: default`)
to 1800 (`source: policy`), and a task claimed seconds later carried a
**1796s** cap. Nothing was restarted, reloaded or redeployed.

**`count_to_n`'s ceiling, which the gate flagged as the one number with no
measurement behind it (§3.0.8).** The 100,000,000 ceiling runs at a median
of **4.801s** over three runs (4.771 / 4.801 / 5.124), single core, through
the executor's own chunked loop. The 300s default is ~62x that. Measured
rather than carried.

## 3.1.4 What the live run found that review did not

**The execution cap could be bypassed entirely on a reassigned task, and
the tests written for this step all passed while it was true.**

Watching a reclaimed task on the running stack, it sat `ASSIGNED` with a
lease that kept renewing and a `deadline_at` of NULL. The cause is a
shipped M2 worker behaviour: `task_assign_duplicate_ignored`. The worker
refuses a re-delivery of a task id it is already executing — correctly —
so after a reclaim handed the task back to the same worker there was no
second `task_started`, and the deadline was only ever written there.

The consequence is exactly the failure `deadline_at` exists to prevent:
progress messages from a **hung** executor would renew that task's lease
forever, because the cap that should have stopped them was never set.

Fixed by stamping `deadline_at` in the dequeue statement as well, so a cap
exists from the moment a task is claimed and `task_started` re-stamps it
from the real start. The cap has to be **per type** at claim time — one
dequeue claims across every type a worker supports — so the code default
is a SQL `CASE` over the registry rather than a single bound parameter.
Two regression tests cover it.

**This is the sixth time a live run has found something review and the
suite both missed**, after #144, #145, #146, #149 and the event-loop lock.
The pattern holds: the defects that survive review are the ones that need
two subsystems in the same room.

Two further defects were found reviewing this step's own implementation
before it ran, and each has a regression test that fails against the first
version:

- **A worker that reconnected mid-execution could not renew.** The new
  session holds no credit key for work delivered to the socket that died
  (Decision #101 has the worker declare a *count*, which cannot be keyed),
  so every progress message about it was skipped and the task would have
  been reclaimed **from a worker that was doing exactly what it should**.
  Fixed with `LocalSession.recovered_tasks`, seeded from what the database
  says the worker holds — never from the worker.
- **The renewal sat behind the telemetry lookup** in `handle_task_progress`,
  which returns early for precisely that case. Moved above it.

## 3.1.5 What Step 3.1 deliberately does NOT do

Stated so it is not discovered in a demo:

- **No retry policy.** The reclaimer requeues with no attempt cap, no
  backoff and no exclusion of the worker that just lost the task, so a
  task whose worker is permanently gone **cycles rather than reaching
  `FAILED`**. `max_attempts`, `not_before` and `excluded_worker_id` all
  exist in the schema and are written by nothing. Step 3.2.
- **No `task_cancel`.** A reclaimed task's original worker keeps burning
  CPU on it. Step 3.2.
- **No fencing.** A late result from a superseded attempt is accepted if
  the row still names that worker. Step 3.4.
- **No recovery timeline in the GUI.** `task_attempts` rows are written
  and are not yet rendered; the console shows the lease, the countdown and
  the attempt count. Step 3.7.
- **No remote Internet worker** took part in any of the above. Everything
  here is local Docker on one laptop, so §8 is **not** claimed for this
  step.
- **Every demo above was agent-run**, not user-run (§15 items 3–4).

## 3.1.6 Demo and failure demo

Runnable from a fresh clone with the documented compose command. Shorten
the windows first if you do not want to wait — the four lease settings are
exposed in `docker-compose.yml` for exactly this:

```bash
TASK_LEASE_TTL_SECONDS=15 LEASE_DISCONNECT_GRACE_SECONDS=5 \
LEASE_RECLAIM_INTERVAL_SECONDS=2 docker compose -p dcds up -d
```

**What is on screen.** Open `https://localhost:${DASHBOARD_PORT}/ui/tasks`.
The `lease` column counts down on every in-flight task and resets as it is
renewed; an expired lease not yet reclaimed shows **red, as "Ns overdue"**.

**Success path.** Submit a `sleep` task of 600 seconds. Watch it go
`QUEUED → ASSIGNED → RUNNING`, and watch its lease sawtooth rather than
run out — that is renewal working, and the task is never reclaimed.

**Failure demo 1 — kill a worker mid-task.**

```bash
docker kill dcds-worker-1
```

The lease drops to the disconnect grace within a second, expires, and the
task reappears **`QUEUED` with `attempt` incremented**. Restart the worker
and it is executed again.

**Failure demo 2 — a worker that is alive but frozen.**

```bash
docker pause dcds-worker-1
```

No close frame is sent, so nothing shortens the lease. It runs down its
full TTL and the task is reclaimed anyway — which is the point of putting
the trigger in Postgres rather than in the connection.

**Failure demo 3 — change a timeout with no redeploy.**

```bash
curl -k -X PUT https://localhost:$PORT/tasks/policies/sleep \
  -H "X-Admin-Secret: $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"max_execution_seconds": 120}'
```

`GET /tasks/policies` shows the value with `source: policy`; the next
`sleep` task claimed carries the new cap. Nothing was restarted.

**Logs to watch.** `task_lease_expired` carries the task id, the worker
that lost it, the previous status, the attempt number and
`overdue_seconds` — the coordinator-observed detection lag.
`task_leases_shortened_on_disconnect` and `task_leases_renewed_on_hello`
are the two accelerator paths.

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
- [x] Killing a worker mid-task causes automatic reassignment; a
      different worker completes it.
- [x] Attempt count increments visibly on the dashboard.
- [x] A poison task exhausts retries and lands in terminal `FAILED`,
      visible in the GUI.
- [x] The failed worker is excluded from the immediate retry — verified.
- [x] Failure counters accumulate per worker and are queryable.
- [ ] **NOT MET. Reassignment works when the failed worker is a remote
      Internet machine, not only a local container.** No worker outside
      this laptop took part in any of the runs below, so §8 is **not**
      claimed for this step. Same gap Step 3.1 recorded, carried
      deliberately rather than quietly.

---

## 3.2.1 What was built

**No migration.** Migration 0006 already carries `tasks.not_before`,
`tasks.excluded_worker_id` and the `task_policies.max_attempts` column,
named there as Step 3.2's and written by nothing in 3.1 (gate §3.0.7's
one-migration rule paying off exactly as intended).

The retry policy lives **inside the reclaim statement**, not in a second
pass over the rows it just wrote:

| Branch | Condition | Write |
|---|---|---|
| retry | `attempt_count + 1 < max_attempts` | `QUEUED`, `attempt_count + 1`, `excluded_worker_id = the worker that lost it`, `not_before = now() + full_jitter(attempt)` |
| exhausted | `attempt_count + 1 >= max_attempts` | terminal `FAILED`, `attempt_count + 1`, **both retry columns NULL** |

Both branches write one `task_attempts` row. The dequeue predicate gains
the two eligibility conditions the gate specified, inside the same ordered
index walk it already performed.

Also new: `max_attempts` as a real per-type policy (`PUT
/tasks/policies/{type}`, no redeploy); `GET /workers/failures`; `attempts`
on `GET /tasks/{id}`; `not_before`, `retry_in_seconds` and
`excluded_worker_id` on every task listing row; a best-effort `task_cancel`
to the worker that lost the task, over the shipped `worker:{id}:push`
channel, and the worker-side handler and `capacity` reply that answers it;
`coordinator_tasks_reassigned_total`, `coordinator_tasks_exhausted_total`
and `coordinator_tasks_awaiting_retry`; three alerts; and an **attempt**
column plus a retry countdown on the task console.

## 3.2.2 Four decisions this step made

**1. A worker-reported `task_failed` is still terminal and is NOT
retried.** The retry engine reacts to lease expiry only. An executor that
raised is a deterministic fault of the task — re-running it burns three
times the CPU for the same exception — and the gate's taxonomy has no
retry row for it. The honest cost: a *transient* executor failure (a
momentary OOM, a full disk) is not retried either, and would need the
worker to distinguish transient from permanent, which V1's dummy workloads
give no basis for. Named here rather than discovered.

**2. `outcome` says what happened to the task, `reason` says why the
attempt ended.** An exhausted task's row is `FAILED` /
`execution_deadline_exceeded`, not a single string carrying both. There is
deliberately no `attempts_exhausted` reason code: "exhausted" is
`attempt_count = max_attempts` on the task, which is derivable and does
not go stale when the policy changes.

**3. The reclaimer distinguishes the two clocks.** `deadline_exceeded` is
evaluated in the reclaim statement against Postgres's own clock, so an
attempt killed by its type's execution cap (taxonomy row 7 — a task too
slow for its policy) is not recorded identically to one whose worker
stopped answering (row 1 — a machine that died). One `CASE`, no extra
query, and it is what makes the poison-task demo below self-explaining.

**4. Failure counters are derived, not accumulated, and they are not on
`GET /workers`.** A counter column on `workers` would be a second write on
the recovery path and a number that can disagree with the rows it
summarises. Keeping the aggregate off the fleet listing matters more than
it looks: that endpoint is the dashboard's 2s poll, and this is a grouped
scan over a table that grows with every abnormal ending.

## 3.2.3 Measurements

Measured on the reference laptop (Intel i5-4460S, 4 cores) against a real
Docker Compose stack with three workers, on **shortened windows** so the
demo runs in a minute: `TASK_LEASE_TTL 15`, `DISCONNECT_GRACE 5`,
`RECLAIM_INTERVAL 2`, `BACKOFF_BASE 3`, `EXCLUSION 30`, `MAX_ATTEMPTS 3`.
Every timestamp below is from the coordinator's own API, polled at 0.4s.

**Kill the worker holding a task; a different worker finishes it.**

| t | state |
|---|---|
| 0.0s | `RUNNING`, attempt 0, worker `d0b60c7d`, lease 4.8s left (the observed close had already cut it to the grace) |
| 5.1s | `QUEUED`, **attempt 1**, `excluded_worker_id = d0b60c7d`, retry in 2.4s |
| 7.6s | `RUNNING`, attempt 1, worker **`a6232283`** — a different machine |
| 52.8s | `COMPLETED` on attempt 1 |

Detection to requeue was **5.1s** against the ≤7s this stack's settings
bound it to (grace 5 + reclaim interval 2), and the task was executing on
another worker **2.5s after that**.

**A poison task exhausts its attempts and stops.** The poison is a task
that cannot finish inside its cap: `sleep(600)` against a `max_execution`
lowered to 10s through the policy API, with nothing restarted.

| t | state |
|---|---|
| 0.1s | `ASSIGNED`, attempt 0, `a6232283` |
| 11.1s | `QUEUED`, attempt 1, excluded `a6232283`, retry in 2.0s |
| 13.1s | `RUNNING`, attempt 1, `89a4152a` |
| 24.9s | `QUEUED`, attempt 2, excluded `89a4152a`, retry in 1.2s |
| 26.1s | `RUNNING`, attempt 2, `a6232283` |
| **37.0s** | **`FAILED`**, attempt_count **3**, `not_before` NULL, `excluded_worker_id` NULL |

Its three attempt rows, read back from `GET /tasks/{id}`:

```
attempt 0: REASSIGNED / execution_deadline_exceeded  worker=a6232283
attempt 1: REASSIGNED / execution_deadline_exceeded  worker=89a4152a
attempt 2: FAILED     / execution_deadline_exceeded  worker=a6232283
```

**A one-worker fleet is not starved by its own exclusion.** Both other
workers stopped, the only worker frozen with `docker pause` (no close
frame — the lease simply ran out), then unpaused:

- reclaimed with `excluded_worker_id` = the only worker there is;
- refused it for the whole exclusion window;
- claimed it again at **30.7s**, against the 30s window;
- `COMPLETED` at 55.8s.

**Per-worker failure counters**, from `GET /workers/failures` after the
runs above — the numbers match the attempt rows exactly:

```
a6232283  REASSIGNED 1
a6232283  FAILED     1
89a4152a  REASSIGNED 1
d0b60c7d  REASSIGNED 1
```

**Metrics on the same stack:** `coordinator_tasks_reassigned_total 3`,
`coordinator_tasks_exhausted_total 1`, `coordinator_leases_expired_total 4`
— the two branches sum to the expiries, which is the arithmetic that says
no reclaim went unaccounted for.

**The cancel was delivered and honoured**, from the worker's own log:
`task_cancel_received` naming the task with `reason: lease_expired`, then
`task_execution_cancelled` **0.28s later** — the cooperative flag being
noticed between chunks.

## 3.2.4 What reviewing this step found, before it ran

Three defects, each fixed with a regression test that fails against the
first version:

- **A retry became eligible and nothing woke the engine.** The doorbell
  rung at reclaim time wakes an assignment pass that finds nothing —
  every task it just requeued is held by `not_before`. Without a second
  wake the retry waited for the safety-net poll: up to
  `ASSIGNMENT_POLL_INTERVAL_SECONDS` (30s) on top of a backoff measured in
  seconds. Not a correctness fault — the task was always claimed
  eventually — but a recovery that is working would have looked stalled to
  anyone watching. Fixed with one sleeping wake-up per reclaim pass, timed
  to the earliest `not_before`, that rings the doorbell and nothing else.
- **A completed task kept the backoff and the exclusion of the attempt
  that failed.** Terminal rows are never claimed, so nothing would have
  acted on them — but the console renders those fields, and a `COMPLETED`
  task showing "retry after …" is a fact every later reader has to know to
  ignore. Cleared in the statements that were already writing the row,
  exactly as Step 3.1 cleared the lease pair.
- **Two flaky assertions in this step's own tests.** The backoff is
  `random() * base`, so asserting the delay was still in the future
  would have failed roughly once in a thousand runs and been blamed on the
  database. Only the ceiling is assertable, and the tests now say so.

## 3.2.5 What Step 3.2 deliberately does NOT do

- **No retry on a worker-reported failure.** Decision 1 above.
- **No fencing.** A late result from a superseded attempt is still
  accepted if the row happens to name that worker again — which the
  exclusion window makes *less* likely but does not prevent. Step 3.4.
- **No recovery timeline in the GUI beyond the task drawer.** The attempt
  rows are rendered in the detail panel; a fleet-wide failure view is
  Step 3.7.
- **No worker quarantine.** A worker that fails everything keeps being
  given work; the failure counters exist so Phase 4 can act on that, and
  M3 does not.
- **No remote Internet worker**, so §8 is not claimed — see the unmet
  exit criterion above.
- **Every demo above was agent-run**, not user-run (§15 items 3–4).

## 3.2.6 Demo and failure demo

Same stack as Step 3.1, with the retry windows shortened too:

```bash
TASK_LEASE_TTL_SECONDS=15 LEASE_DISCONNECT_GRACE_SECONDS=5 \
LEASE_RECLAIM_INTERVAL_SECONDS=2 TASK_RETRY_BACKOFF_BASE_SECONDS=3 \
TASK_RETRY_EXCLUSION_SECONDS=30 docker compose -p dcds up -d
```

**What is on screen.** `https://localhost:${DASHBOARD_PORT}/ui/tasks` now
has an **attempt** column — highlighted from attempt 2 on — and the lease
column shows **"retry in Ns"** for a task waiting out its backoff. The
task drawer lists every attempt that ended abnormally, with the worker and
the reason.

**Failure demo 1 — reassignment.** Submit `sleep` for 45s, `docker kill`
the worker the console shows holding it. The attempt column goes to 2, the
lease column reads "retry in Ns", and a different worker picks it up.

**Failure demo 2 — a poison task reaching terminal `FAILED`.**

```bash
curl -k -X PUT https://localhost:$PORT/tasks/policies/sleep \
  -H "X-Admin-Secret: $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"max_execution_seconds": 10}'
# then submit sleep(600) and watch three attempts end at the cap
```

**Failure demo 3 — no starvation on one worker.** Stop every worker but
one, `docker pause` it until its lease expires, unpause. It is refused the
task for `TASK_RETRY_EXCLUSION_SECONDS` and then gets it back.

**Reads worth running:** `GET /workers/failures` for the per-worker
counters, `GET /tasks/{id}` for the attempt rows, and
`coordinator_tasks_reassigned_total` / `coordinator_tasks_exhausted_total`
on `/metrics`.

**Logs to watch.** `task_lease_expired` now carries `not_before`,
`excluded_worker_id` and `reason`; `task_attempts_exhausted` is its own
event, so "this task will be tried again" and "this task is over" are not
one log line with a field to filter on.

---

## Step 3.3 — Idempotency and duplicate suppression

- Enforce the idempotency token that has been carried since Phase 2.
- **Deduplication is structural in Postgres — the row lock plus the
  terminal-state check — and there is NO separate dedup store.** Amended
  by Decision #170 (§3.0.12); this bullet previously called for
  deduplication state in Redis with a window and retention. There is no
  window and no retention because there is nothing to retain.
- Duplicate submission is a **no-op returning success**, not an error.
- Resolve the classic race: worker A's result is in flight, the lease
  expires, the task reassigns to worker B, and both results arrive.

**Exit criteria**
- [x] Submitting the same result twice completes the task exactly once —
      verified in the database. — **Twenty consecutive submissions of one
      envelope over a real socket: one `COMPLETED`, one result row, one
      `completed_at`** (§3.3.3).
- [x] The in-flight-versus-reassignment race has a stated winner rule,
      implemented and tested by deliberately reproducing it. — **Rule in
      §3.3.2, reproduced live by freezing the worker holding a `sleep(40)`
      task**: reassigned at 12.2s, completed by another worker by 55.8s,
      and the original's honest result answered `superseded` **40.9s after
      that completion**, having written nothing (§3.3.3).
- [x] Duplicate submission returns success, not an error. — **`accepted:
      true`, `outcome: duplicate`**, and the worker drops its pending
      result rather than retrying (§3.3.3).
- [x] **No dedup store exists to grow unbounded** — the terminal-state
      check on the task row is the whole mechanism, and that is asserted
      rather than assumed (amended by #170; previously "dedup state has a
      bounded retention"). — **Asserted in the schema and in Redis**
      (§3.3.3), and by a test that fails if a table is added.
- [x] Dedup works across coordinator replicas. — **The same envelope
      re-sent to a different replica of a three-replica stack answered
      `duplicate` and wrote nothing** (§3.3.3).
- [x] Result ledger count matches task completion count exactly under
      load. — **1,303 completions, 1,303 result rows, 1,303 distinct
      tokens, 0 rows shared by two tasks**, held through a run in which
      **every attempt of 60 tasks lost its lease and 180 late results
      arrived** (§3.3.3).

**Not claimed for this step, and named rather than left to be found:**
**no remote Internet worker took part** (§8 not claimed — the same gap
Steps 3.1 and 3.2 recorded), and **every demo below was run by the agent,
not by the user** (§15 items 3–4).

---

## 3.3.1 What was built

**No migration, no new table, no new key, no new message.** That is the
step, stated as a diff: `complete_task` reorders two guards and reads one
more column, and one small helper decides what to call a submission the
coordinator is not going to store. Everything else here is tests,
measurement and this document.

Before 3.3 the suppression existed — the row lock plus `status ==
COMPLETED` shipped in Step 2.5 — but it answered **two different
situations with one word**:

| Situation | Before 3.3 | After 3.3 |
|---|---|---|
| The same worker retries the submission it already made | `duplicate` | `duplicate` |
| A second, different result arrives for a completed task | `duplicate` | **`superseded`** |
| A late result arrives for a task another attempt completed | **`not_owner`** | **`superseded`** |
| A late result arrives for a task that ended `FAILED` | **`illegal`** | **`superseded`** |
| A late result arrives for a task another worker is still running | `not_owner` | `not_owner` (3.4 fences it) |

Two of those rows were actively misleading after Step 3.2 made
reassignment real. `not_owner` is the answer to *an impostor*, and it is
the same answer §12 gives a worker naming a task id it guessed — but a
worker whose task was reassigned while it was still computing is not an
impostor, it is the honest loser of a race the design says it may lose.
`illegal` was worse: it accused a worker of an invalid transition when
what actually happened was that the coordinator gave up on the task first.

**The mechanism, in full:**

```
lock the task row FOR UPDATE          (already there — Step 2.5)
  terminal?  -> compare the submitted idempotency_token with the token in
                the stored result:  equal -> duplicate,  else -> superseded
  live?      -> must be the assigned worker, then complete it
```

**The idempotency token stops being decoration here.** It has been on the
wire and in the stored envelope since Step 2.5, required and enforced by
nothing — which was that step's stated intent, so that Phase 3 could add
arbitration without a protocol change. It is minted **once per task
execution** on the worker (`worker.py`'s `Task.idempotency_token`) and
re-sent unchanged by every retry, so token equality means *the same
submission*, not merely *the same worker*.

## 3.3.2 Three decisions this step made

**1. The winner rule, stated once so it can be tested:**

> The first submission to find its task non-terminal **while holding it**
> completes the task and stores exactly one result. Every later submission
> for that task writes nothing, and is answered definitively: `duplicate`
> if the stored result is its own, `superseded` if it is not.

The lock is what makes "first" well defined across replicas; nothing about
this rule needs a clock, an ordering service or a second store. And the
race resolves in **both** directions with no extra machinery: if the late
result arrives before the reclaimer ticks, the task is already terminal
and the reclaimer's `WHERE status IN ('ASSIGNED','RUNNING')` matches
nothing — the slow worker simply wins.

**2. The terminal check runs BEFORE the ownership check, and the order is
load-bearing.** Reversed — as it shipped in 2.5 — a reassigned task
answers its original worker `not_owner`, which is both the wrong word and
the wrong behaviour: under Decision #168 that path only releases the
worker's credit as a special case. Putting the terminal check first means
an honest late result is answered on the task's terms, whoever asks.

The §12 question this raises, answered rather than skipped: a worker that
*guesses* a completed task's id now learns it is terminal, where before it
learned it was someone else's. Both answers already reveal that the id
exists (`not_found` is the answer when it does not), so no new class of
information is disclosed — and the credit it can free is bounded by what
it declared as its own reconnect residue, exactly as the shipped
`capacity` message already allows.

**3. `superseded` is a new outcome word rather than a reuse of
`duplicate`.** Collapsing them would make an ack say *your result is
stored* when another attempt's is — the kind of blurring §10 exists to
prevent — and it would hide the one number that distinguishes a fleet
retrying itself from a fleet being reassigned out from under itself.
`accepted` is therefore `false` for `superseded` and `true` for
`duplicate`: nothing of the superseded submission was kept, and the worker
should stop retrying either way.

**It needs no new metric.** `coordinator_task_results_total{outcome=...}`
has counted decisions about submissions since Step 2.5, and a new label
value is what a new decision looks like there.

## 3.3.3 Measurements

Measured on the reference laptop (Intel i5-4460S, 4 cores) against a real
Docker Compose stack: **three coordinator replicas** (`--scale
coordinator=3`) over one Postgres and one Redis, plus real worker
containers, on the shortened windows Step 3.2 introduced — `LEASE_TTL 15`,
`DISCONNECT_GRACE 5`, `RECLAIM_INTERVAL 2`, `BACKOFF_BASE 3`, `EXCLUSION
30`.

Duplicate and superseded submissions are made by a **throwaway protocol
client** driving the shipped `SimWorker` from `scripts/loadtest.py` — the
same wire protocol a real worker speaks, with **no production code
changed**. A real worker cannot be asked to repeat itself on demand; that
is the whole point of the pending buffer it drops on the first ack.

**Twenty identical submissions of one envelope, over one socket** — the
same task id, the same token, the same body, 0.4s apart:

| | |
|---|---|
| acks received | `transitioned`, then **19 × `duplicate`**, every one `accepted: true` |
| the task | `COMPLETED`, one `completed_at`, one `result_id` |
| result rows in the whole database afterwards | **3, for 3 completed tasks, with 3 distinct tokens** |

**A different envelope for the same completed task** (a fresh token and
the body `"a-different-answer"`) was answered **`superseded`, `accepted:
false`**. Checked in the database on the earlier `count_to_n(2000)` task
that got the same treatment: the stored row still held token
`0dbc465c982b…` and the answer **`2000`** — the loser's body was not
stored, and `completed_at` did not move.

**Across replicas.** The same worker reconnected to a **different
coordinator replica** — a different process, sharing nothing but Postgres
— and re-sent the winning envelope. Answer: **`duplicate`**, and the
metric appeared on that replica's own `/metrics` while the first replica's
count did not move:

```
replica A   transitioned 1   duplicate 4   superseded 1
replica B   duplicate 1
```

**The race, deliberately reproduced.** A `sleep(40)` task delivered to one
worker, which was then **frozen with `docker pause`** — alive, socket
open, nothing sent, so no close is observed and only the lease can catch
it:

| t | what the coordinator's API showed |
|---|---|
| 0.0s | `RUNNING`, attempt 0, worker `50716244` |
| 12.2s | `QUEUED`, attempt **1**, no worker — reclaimed and requeued |
| 16.3s | `RUNNING`, attempt 1, worker **`75ead7af`** — a different machine |
| 55.8s | `COMPLETED` by `75ead7af` (the poll before, at 51.8s, still read `RUNNING`) |
| +40.9s | the **unpaused** original worker finished its own 40s of work and submitted — `11:02:39.817` against a completion at `11:01:58.870`, both from the coordinator's own log |

The last line is the one this step exists for. The coordinator answered
`task_result_not_applied outcome=superseded`, and the worker's own log
reads:

```
{"event": "task_result_refused", "outcome": "superseded",
 "was_pending": true, "pending": 0}
```

— it dropped the result and stopped, rather than retrying a verdict that
will never change. In the database: **one result row**, holding
`75ead7af`'s answer with a worker-reported duration of **40.006s**, and
the loser's identical 40.001s of honest work stored **nowhere**. That is
the delivery guarantee (gate §3.0.5) demonstrated end to end: **the task
executed twice and completed once.**

**No dedup store — asserted, not assumed.** After a storm of 20 duplicates
and 2 superseded submissions:

```
                    List of relations
 Schema |      Name       | Type  |    Owner
--------+-----------------+-------+-------------
 public | alembic_version | table | coordinator
 public | task_attempts   | table | coordinator
 public | task_policies   | table | coordinator
 public | task_results    | table | coordinator
 public | tasks           | table | coordinator
 public | workers         | table | coordinator
```

Six tables — the migrations' own, with nothing added. Redis went from 12
keys to 18 across the storm, and **every one of the six is accounted for
by the probe registering as a new worker** (`access_token`,
`session_epoch`, `token_gen`, `metrics`) plus two rate-limit counters. **No
new key *shape* appeared, and 20 duplicate submissions in a row created
not one key.** `tests/test_idempotency.py` asserts the table set exactly,
so adding a store fails CI rather than being noticed later.

**The ledger under load.** Two burst runs plus a deliberate race storm,
counted from the coordinator's own rows:

| run | tasks | outcome |
|---|---|---|
| `burst --workers 10 --tasks 1000` (`count_to_n`) | 1,000 | 1,000 `COMPLETED` in 16.6s, every check green |
| `burst --workers 10 --tasks 300` (`sleep(4)`, lease 5s) | 300 | 300 `COMPLETED` in 37.1s |
| **race storm** — `sleep(4)` with the type's lease policy at **2s**, so **every attempt loses its task** | 60 | 60 exhausted to `FAILED` |

Cumulative, at the end of all three:

| | |
|---|---|
| tasks `COMPLETED` | **1,303** |
| `task_results` rows | **1,303** |
| distinct idempotency tokens stored | **1,303** |
| result rows pointed at by more than one task | **0** |
| tasks `FAILED` with a result row | **0** |

The race storm is the part worth reading. 181 lease expiries produced
**121 reassignments + 60 exhaustions — exactly, with none unaccounted
for** — and the 180 late results those lost attempts eventually submitted
were answered:

```
coordinator_task_results_total{outcome="not_owner"}   120   (task still live, held by a later attempt)
coordinator_task_results_total{outcome="superseded"}   60   (task already terminal FAILED)
```

120 + 60 = 180 = 60 tasks × 3 attempts. **Every single one of those
executions was real work, and not one of them added a row to the ledger.**

## 3.3.4 What running this step found

**The exclusion window expiring makes the loser a legal destination for
its own task, and the first version of the race test got it back.** Step
3.2's `not_before` carries two clocks in one column — the retry backoff
and the bounded exclusion — so a test that ages it to elapse the backoff
elapses the exclusion with it. The reassignment then landed back on the
still-connected worker that had just lost the task, which is *correct
behaviour* (§3.2's anti-starvation decision) and a *wrong test*. Fixed by
dropping the loser's socket first, which is also what the scenario means:
the lease expired because that worker was not answering.

**A cold start of three replicas against an empty database races the first
migration.** `docker compose up --scale coordinator=3` on a fresh volume
gave `duplicate key value violates unique constraint
"pg_type_typname_nsp_index"` — three processes running `alembic upgrade
head` at once. The demo starts one replica, lets it migrate, then scales.
**Recorded, not claimed as a defect of this step**: it is a property of
`RUN_MIGRATIONS_ON_STARTUP` that predates M3, it cannot happen on a
database that has been migrated once, and Kubernetes rollouts do not cold
start every replica simultaneously. It is the local demo's problem and it
now has a documented order.

**`.venv-loadtest` had drifted off `worker/requirements.txt`** — it held
`websockets 17.0.1` against a pinned `>=12,<13`, so the harness died with
`create_connection() got an unexpected keyword argument 'extra_headers'`
before sending a frame. Pinned back. Worth knowing before reading any
"the harness is broken" conclusion into a future run.

## 3.3.5 What Step 3.3 deliberately does NOT do

- **No fencing.** A stale result from an *earlier attempt* of a task the
  same worker is holding *again* is still accepted, because
  `attempt_number` is not compared against `attempt_count`. That is Step
  3.4's exit criterion, and the gate assigned it there (§3.0.6). What 3.3
  does resolve is the far more common shape of the same race — the late
  result arriving after somebody else finished.
- **A late result for a task that is still live gets `not_owner`, not a
  fenced answer with an attempt row.** 120 of them in the storm above. 3.4
  owns turning that into `FENCED` with a row in `task_attempts`.
- **`task_failed` was not touched.** A *failure* report for a task that has
  moved on is still answered by `mark_status` with `not_owner` or
  `illegal`, so the vocabulary is now consistent on the result path and not
  yet on the failure path. Deliberate: this step's criteria are about the
  **result ledger**, a refused failure report writes nothing either way,
  and Decision #168 already gives such a worker its credit back. Naming it
  because a reader of the ack outcomes will notice the asymmetry.
- **No alert, and Steps 3.1 and 3.2 both shipped some, so this is a
  decision.** A sustained `superseded` rate means a fleet is losing races,
  and the *cause* of that is sustained reassignment — which Step 3.2
  already alerts on. A second alert on the same phenomenon one hop
  downstream pages twice for one incident, which is how alert fatigue
  starts. The label is on `/metrics` for anyone diagnosing that page.
- **No dashboard change, and that is a judgement rather than an
  omission.** The task drawer already renders the stored result envelope
  whole, so the **winning submission's `idempotency_token` is on screen**
  next to the attempt row that records who lost — verified live on the
  race task (`idempotency_token: bb0027e1a74a…`, attempts:
  `REASSIGNED / lease_expired / attempt 0`). A per-task duplicate counter
  would need somewhere to count duplicates, which is precisely the store
  this step's fourth criterion forbids. Fleet-wide, the two outcomes are
  on `/metrics` today and belong on the dashboard in **Step 3.7**, whose
  subject is exactly that.
- **A duplicate arriving after retention has purged the body cannot be
  recognised as one** and is answered `superseded` — the token lives in
  the result body and goes with it. Both answers mean "stop retrying,
  nothing is owed", and no submission survives seven days in a buffer that
  lives in a worker's memory. Named in `complete_task` and covered by a
  test rather than left to be discovered.
- **No remote Internet worker** (§8 not claimed), and **no user-run demo**
  (§15 items 3–4). Both carried from 3.1 and 3.2.

## 3.3.6 Demo and failure demo

Same stack as Step 3.2, plus replicas. **Start one coordinator, let it
migrate, then scale** — see §3.3.4:

```bash
docker compose -p dcds --env-file <env> up -d --scale coordinator=1
# wait for healthy, then:
docker compose -p dcds --env-file <env> up -d --scale coordinator=3
```

**Demo 1 — a duplicate is a success.** Submit a task, let a worker
complete it, then re-send the same result envelope. The ack comes back
`{"outcome": "duplicate", "accepted": true}` and `GET /tasks/{id}` shows
one result and an unchanged `completed_at`. A throwaway client is needed
because a real worker drops its pending result on the first ack; the one
used here subclasses `SimWorker` from `scripts/loadtest.py`.

**Demo 2 — across replicas.** Re-send the same envelope to a *different*
replica's port. Same answer, and the count appears on that replica's own
`/metrics`.

**Failure demo 1 — the race.** Submit `sleep(40)`, find the worker holding
it in the task console, and `docker pause` it. Watch the task go `QUEUED`
→ another worker → `COMPLETED`. Then `docker unpause`: the original worker
finishes and submits, and its log line is `task_result_refused` with
`outcome: superseded`. The database still has one result row.

**Failure demo 2 — every attempt loses its lease.** Set the type's lease
below its execution time and run a burst:

```bash
curl -k -X PUT https://localhost:$PORT/tasks/policies/sleep \
  -H "X-Admin-Secret: $ADMIN_SECRET" -H 'Content-Type: application/json' \
  -d '{"lease_ttl_seconds": 2, "max_attempts": 3}'
python scripts/loadtest.py burst --url https://localhost:$PORT \
  --workers 5 --tasks 60 --task-type sleep --parameters '{"seconds":4}' ...
```

Every task exhausts to `FAILED`, every attempt submits a late result, and
the ledger does not move. **Then check it:**

```sql
SELECT (SELECT count(*) FROM tasks WHERE status='COMPLETED') AS completed,
       (SELECT count(*) FROM task_results)                   AS result_rows,
       (SELECT count(DISTINCT payload->>'idempotency_token')
          FROM task_results)                                 AS distinct_tokens;
```

**Reads worth running:** `coordinator_task_results_total` by outcome on
each replica, `\dt` in Postgres (six tables, no store), and
`redis-cli --scan` before and after a duplicate storm.

---

## Step 3.4 — Stale result fencing

- Reject results from superseded attempts using **task ownership and
  attempt number** — `worker == assigned_worker_id AND attempt_number ==
  attempt_count` — both present since Phase 2. Amended by Decision #169
  (§3.0.12); this bullet previously also named `session_epoch`.
  **`session_epoch` is deliberately NOT a fencing input**, because Step
  2.5 measured a legitimate result executed under epoch 4 and submitted
  on 5 after a reconnect. It stays on the wire and in the logs for
  tracing.
- Rejected results never mutate task state.
- The worker receives an unambiguous rejection response.
- Rejections are logged and surfaced on the dashboard.

**Exit criteria**
- [ ] A result from a superseded attempt is rejected — verified by
      deliberately reproducing it.
- [ ] **A result submitted under a NEWER session epoch than the one it
      was executed under is ACCEPTED** — Step 2.5's measured reconnect
      path, asserted rather than assumed (amended by #169; previously "a
      result from an old session epoch is rejected", which would have
      broken that path).
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
