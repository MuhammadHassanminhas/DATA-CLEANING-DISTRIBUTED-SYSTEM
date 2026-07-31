# Operator API

The task API an operator drives. Everything here is reachable with `curl`
and a credential; **no operation in this document requires touching the
database**, which is the point of Step 2.6.

Companion documents: `docs/onboarding-a-worker.md` (running a worker),
`docs/runbook.md` (credential rotation, incident procedures).

---

## 1. Base URL

| Environment | Base URL |
|---|---|
| Local Docker Compose | `https://localhost:8443` |
| Staging | `https://dcds-staging.centralindia.cloudapp.azure.com` |
| Production | the production ingress host in `infra/helm/platform/values-production.yaml` |

Local Compose serves the **private dev CA** certificate. Windows `curl`
(schannel) cannot validate it — it fails a revocation check it cannot
answer — and neither can Python 3.14's OpenSSL. Either pass `-k` locally,
or run the client inside a container, where Python 3.12 validates
`certs/dev-ca.crt` fine. The public staging and production endpoints carry
a Let's Encrypt certificate and validate normally with no `-k`.

## 2. Authentication

Every endpoint below requires the **operator credential**, `ADMIN_SECRET`.

```
X-Admin-Secret: <ADMIN_SECRET>
```

This is **not** the credential workers enroll with. `ENROLLMENT_SECRET` is
shared with every worker by design, so the two were split in Step 2.2.1 —
otherwise any worker could drain the queue or revoke its peers. A worker's
enrollment secret will be rejected here.

`POST /tasks` **also** accepts `admin_secret` as a body field, and still
does, because existing scripts and the CD smoke test send it that way.
`POST /tasks/dequeue` (§5.6) takes the credential in its body **only** — it
is a primitive, unchanged since Step 2.2. **Prefer the header everywhere it
is accepted.** A body field can end up quoted back in a validation
error; that is exactly how a live `ADMIN_SECRET` leaked once and had to be
rotated. (The coordinator no longer echoes request bodies in 422s — see
§7 — but the header avoids the question.)

A missing or wrong credential returns **401** and is logged with the
caller's apparent address.

## 3. Rate limits

Two independent layers:

| Layer | Limit | Scope |
|---|---|---|
| ingress-nginx (public endpoints only) | `ingress.limitRps` per source IP | all paths |
| coordinator | `TASK_API_RATE_LIMIT_PER_MINUTE`, **default 300/min** per source IP | the operator task API |

The coordinator's limiter is a fixed 60-second window shared across the
task endpoints. Over the limit returns **429** `{"detail": "rate limited"}`.
It is applied **before** authentication, so an unauthenticated flood is
rejected without a credential check.

**`POST /tasks/dequeue` is exempt** — it is a queue primitive, and
`scripts/queue_harness.py` drives about a thousand claim calls as fast as
it can to prove three replicas never double-assign. It is still
credential-guarded, and still behind the edge limit on the public path.

## 4. Conventions

**Correlation ID.** Send `X-Correlation-ID` and it is used verbatim;
otherwise one is generated. It comes back in the `X-Correlation-ID`
response header, is stamped on every log line the request produces across
every replica, and is **stored on every task the request creates**. That is
what makes a batch findable later (§5.2).

**Timestamps** are ISO 8601 with an explicit UTC offset.

**Durations.** Two exist and they are not the same measurement:

* `duration_seconds`, inside `result` — **worker-reported** execution time.
  It is untrusted (a worker can report anything).
* `observed_duration_seconds` — **coordinator-observed**, `assigned_at` to
  `completed_at`. It includes delivery, the worker's admission path and the
  result round trip, so it is always the larger, and it cannot be lied
  about.

**Status codes**

| Code | Meaning |
|---|---|
| 200 / 201 | done |
| 400 | a filter, limit or id that is not valid |
| 401 | missing or wrong operator credential |
| 404 | no such task (including an id that is not a UUID) |
| 409 | the task exists but is not in a state this operation accepts |
| 422 | request body failed schema validation |
| 429 | rate limited |

---

## 5. Endpoints

### 5.1 `POST /tasks` — submit one task or a batch

```bash
curl -sk https://localhost:8443/tasks \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"task_type":"count_to_n","parameters":{"n":2000}}'
```

```json
{"status":"queued","count":1,
 "task_ids":["d17d2dee-c761-4b01-843c-a336fac40b90"],
 "correlation_id":"9bb2cd64-ed86-47d1-9182-70e2e27e58d4"}
```

| Field | Required | Notes |
|---|---|---|
| `task_type` | yes | one of the four types in §6 |
| `parameters` | per type | validated against that type's schema; unknown keys are rejected |
| `payload` | no | opaque JSON, passed to the worker uninterpreted |
| `priority` | no | integer, default 0, **lower is more urgent** (like Unix nice) |
| `count` | no | 1–`TASK_ENQUEUE_MAX_BATCH` (default 10,000) identical tasks in one call |

`count` exists because the public ingress rate-limits requests per second,
so 1,000 tasks must be reachable in one call rather than a thousand.

**`task_ids` is `null` whenever `count > 1`.** Echoing 10,000 UUIDs would
make the response larger than the request that caused it. Use the
correlation id to find the batch — see the next section.

> On PowerShell, write the JSON to a file and use `-d "@file"`. Inline
> `curl.exe -d '{...}'` mangles the body; it has cost this project a round
> trip three sessions running.

### 5.2 `GET /tasks` — list with filters

```bash
curl -sk "https://localhost:8443/tasks?status=RUNNING&limit=20" \
  -H "X-Admin-Secret: $ADMIN_SECRET"
```

| Query parameter | Notes |
|---|---|
| `status` | repeatable — `?status=QUEUED&status=ASSIGNED`. One of `QUEUED`, `ASSIGNED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| `task_type` | one of the four types in §6 |
| `worker_id` | UUID of the assigned worker |
| `correlation_id` | exact match — the batch lookup |
| `limit` | default 50, max `TASK_LIST_MAX_LIMIT` (default 200) |
| `offset` | default 0 |

Filters combine with AND. Order is fixed: **newest first**
(`created_at DESC`, `id DESC`).

```json
{"tasks":[{"task_id":"…","task_type":"count_to_n","status":"COMPLETED",
           "priority":0,"assigned_worker_id":"…","created_at":"…",
           "assigned_at":"…","started_at":"…","completed_at":"…",
           "updated_at":"…","attempt_count":0,"correlation_id":"…",
           "observed_duration_seconds":0.036,"has_result":true}],
 "count":1,"limit":50,"offset":0,"has_more":false,
 "filters":{"status":null,"task_type":null,"worker_id":null,"correlation_id":null}}
```

**An unknown `status` or `task_type` is a 400, not an empty list.** A typo
must not look like an idle fleet.

**There is no total count.** A filtered `COUNT(*)` costs more than the page
it describes, and the task table grows for the lifetime of the system.
`has_more` answers the paging question for one extra row.

**Listings never carry result bodies** — a page of 200 results at the
128 KB cap would be 25 MB. `has_result` says whether one exists; §5.3
fetches it.

**Find everything a batch created:**

```bash
CID=$(curl -sk https://localhost:8443/tasks -H "X-Admin-Secret: $ADMIN_SECRET" \
      -H 'Content-Type: application/json' \
      -d '{"task_type":"count_to_n","parameters":{"n":1},"count":1000}' \
      | python -c 'import sys,json; print(json.load(sys.stdin)["correlation_id"])')

curl -sk "https://localhost:8443/tasks?correlation_id=$CID&limit=200&offset=0" \
     -H "X-Admin-Secret: $ADMIN_SECRET"
```

Page with `offset` until `has_more` is `false`.

### 5.3 `GET /tasks/{task_id}` — inspect one task

Everything a listing row carries, plus the lifecycle timeline and the
stored result.

```json
{"task_id":"d17d2dee-…","task_type":"count_to_n","status":"COMPLETED",
 "priority":0,"assigned_worker_id":"ae0c0a8d-…",
 "created_at":"2026-07-31T10:04:30.103471+00:00",
 "assigned_at":"2026-07-31T10:04:30.113323+00:00",
 "started_at":"2026-07-31T10:04:30.130120+00:00",
 "completed_at":"2026-07-31T10:04:30.148877+00:00",
 "attempt_count":0,"correlation_id":"9bb2cd64-…",
 "observed_duration_seconds":0.036,"has_result":true,
 "timeline":[
   {"state":"QUEUED","at":"2026-07-31T10:04:30.103471+00:00","source":"created_at"},
   {"state":"ASSIGNED","at":"2026-07-31T10:04:30.113323+00:00","source":"assigned_at"},
   {"state":"RUNNING","at":"2026-07-31T10:04:30.130120+00:00","source":"started_at"},
   {"state":"COMPLETED","at":"2026-07-31T10:04:30.148877+00:00","source":"completed_at"}],
 "result":{"task_id":"d17d2dee-…","status":"COMPLETED","attempt_number":0,
           "session_epoch":1,"idempotency_token":"65e64f30…",
           "duration_seconds":0.002,"result":2000,"truncated":false,
           "size_bytes":220},
 "result_size_bytes":220,
 "result_submitted_at":"2026-07-31T10:04:30.147000+00:00"}
```

**About `timeline`.** It is reconstructed from the task's own timestamp
columns, and every entry names the column it came from. `QUEUED`,
`ASSIGNED`, `RUNNING` and `COMPLETED` each have a dedicated column, so
those are what the coordinator recorded. `FAILED` and `CANCELLED` have
none and use `updated_at`, which is correct because a terminal state is the
last write the row receives. A task created before this feature shipped has
no `started_at`, so its `RUNNING` entry is **absent rather than invented**.

**`result` is `null` after the retention period** (`RESULT_RETENTION_DAYS`,
default 7). The task row survives forever as the audit trail; only the body
expires. That is documented behaviour, not a lost result — `completed_at`
and the timestamps stay.

`truncated: true` with `original_size_bytes` means the result exceeded
`TASK_RESULT_MAX_BYTES` (default 128 KB) and the body was dropped. The task
still completed; an oversize result is a fact about the payload, not an
error.

### 5.4 `POST /tasks/{task_id}/cancel` — cancel a queued task

```bash
curl -sk -X POST "https://localhost:8443/tasks/$TASK_ID/cancel" \
  -H "X-Admin-Secret: $ADMIN_SECRET"
```

No body. On success:

```json
{"task_id":"6b618143-…","status":"CANCELLED","previous_status":"QUEUED"}
```

**Only a `QUEUED` task can be cancelled.** Anything else returns 409 with
the status that caused the refusal:

```json
{"task_id":"41813f10-…","status":"RUNNING",
 "detail":"task is RUNNING: only a QUEUED task can be cancelled (cancelling in-flight work is not in M2 scope)"}
```

That limit is deliberate. Marking a running task cancelled in the database
would not stop the worker: it would keep executing, keep its slot, and
submit a result for a task the database calls terminal — which is then
refused, losing real work. Stopping in-flight work needs a cancel message
on the wire and a worker-side cancel path, which M2 does not have.

**Cancelling twice returns 409**, reporting `CANCELLED`, rather than a
second success. The state is the same either way; the second call simply
did not do the cancelling.

Cancellation races the assignment engine safely: the row is locked before
its status is read, so a task is never both cancelled and handed out.

### 5.5 `GET /tasks/depth` — queue depth and lifecycle counts

```json
{"depth":376,"counts":{"QUEUED":376,"ASSIGNED":1,"COMPLETED":626,"CANCELLED":1}}
```

`depth` is the cheap index count of queued work — measured at 2.4 ms
against 320,025 rows. `counts` groups the whole table and is the more
expensive read; they are separate so the cheap path stays cheap.

### 5.6 `GET /tasks/throughput` — completions per minute

The read behind the dashboard's throughput chart (Step 2.7).

```bash
curl -sk "https://localhost:8443/tasks/throughput?minutes=30" \
  -H "X-Admin-Secret: $ADMIN_SECRET"
```

```json
{"window_minutes":30,
 "series":[{"minute":"2026-07-31T11:42:00+00:00","completed":159},
           {"minute":"2026-07-31T11:43:00+00:00","completed":287}],
 "completed_in_window":446}
```

| Query parameter | Notes |
|---|---|
| `minutes` | default 30, max `TASK_THROUGHPUT_MAX_MINUTES` (default 1440) |

**Every minute in the window is present, including the empty ones.** A
series with the quiet minutes omitted draws a busy fleet and an idle one
identically. Oldest first, so a chart reads left to right.

**A bucket is exactly the rows `GET /tasks?status=COMPLETED` would list for
that minute**, because the count comes from `tasks.completed_at` — which
`complete_task` stamps and nothing else does. That is deliberate: the chart
is checkable against §5.2 rather than merely plausible.

**A `FAILED` task is not in here.** `completed_at` means "produced a
result", not "stopped moving", so failures never enter the series rather
than entering it as zeroes. Failure counts are in §5.5's `counts`.

Buckets are cut by Postgres's clock — the same one that stamped the rows —
so a caller in another time zone gets the same buckets.

### 5.7 `POST /tasks/dequeue` — queue primitive (not for normal operation)

Atomically claims queued tasks for a **named** worker. It exists so the
"three replicas never double-assign" property can be proven against real
replicas, and it is what `scripts/queue_harness.py` drives.

**Do not use it to hand out work.** The assignment engine decides which
worker gets what, based on declared capabilities and credits; claiming a
task for a worker by hand bypasses that and the worker will never be told.

```json
{"admin_secret":"…","worker_id":"ae0c0a8d-…","limit":10}
```

### 5.8 `GET /workers` — the fleet

Phase 1.8. Same credential. Returns every registered worker with its live
status, last heartbeat, CPU/memory, latency, declared capabilities and
current tasks. This is what the dashboard reads.

---

## 6. Task types

V1 payloads are dummy workloads only. Parameters are validated before
anything reaches the database, and **unknown keys are rejected** rather
than ignored.

| `task_type` | Parameters | Bounds |
|---|---|---|
| `count_to_n` | `n` (int) | 1 – 100,000,000 |
| `hash_rounds` | `rounds` (int), `algorithm` (`sha256` \| `sha512`, default `sha256`) | rounds 1 – 10,000,000 |
| `sleep` | `seconds` (number) | > 0, ≤ 3600 |
| `opaque_payload` | `payload_b64` (base64 string) | ≤ 64 KB decoded |

The bounds are recommendations chosen so no single task can occupy a worker
indefinitely, not measured limits.

---

## 7. Errors

Failures return `{"detail": ...}`. A 422 returns a list of `{loc, msg,
type}` — the location, the message and the error type, and **deliberately
not the value you sent**:

```json
{"detail":[{"loc":["body","task_type"],"msg":"Field required","type":"missing"}]}
```

The framework default includes the offending input, which for a `missing`
error is the **whole request body**. On an endpoint that accepts a
credential in its body, that is a credential-disclosure path, and it is one
this project hit for real. The caller already knows what it sent.

---

## 8. Limits and tunables

All are environment variables on the coordinator. Every one is a
**recommendation** rather than a measured value; Step 2.8's load harness is
what will produce defensible numbers.

| Variable | Default | Effect |
|---|---|---|
| `TASK_ENQUEUE_MAX_BATCH` | 10000 | max `count` on one `POST /tasks` |
| `TASK_LIST_MAX_LIMIT` | 200 | max `limit` on `GET /tasks` |
| `TASK_THROUGHPUT_MAX_MINUTES` | 1440 | widest `minutes` on `GET /tasks/throughput` |
| `TASK_API_RATE_LIMIT_PER_MINUTE` | 300 | operator API requests per source IP per minute |
| `TASK_DEQUEUE_MAX_BATCH` | 100 | max `limit` on `POST /tasks/dequeue` |
| `TASK_RESULT_MAX_BYTES` | 131072 | result body cap before truncation |
| `RESULT_RETENTION_DAYS` | 7 | how long result bodies are kept (0 disables) |

---

## 9. What this API does not do

Stated so none of it is mistaken for an oversight:

* **No cancellation of running work** — §5.4.
* **No retry, requeue or reassignment.** A task whose worker vanished stays
  `ASSIGNED`, and a task whose result was rejected stays `RUNNING`. Both
  are visible through this API and are Phase 3's to reclaim.
* **No task deletion.** The task row is the audit trail. Only result bodies
  expire, on the retention schedule.
* **No per-operator identity.** `ADMIN_SECRET` is one shared credential, so
  logs record *that* an operator acted and from which apparent address,
  never *which human*.
* **No per-source throttling of the dashboard's own calls.** The dashboard
  proxies these endpoints from one pod, so the whole GUI shares one
  rate-limit bucket. §3's default of 300/minute is set for that; a
  deployment that puts many operators behind one dashboard should raise it.

Since Step 2.7 there **is** a browser view of all of this — the task console
at `/ui/tasks` on the dashboard. It reads the endpoints above through a
server-side proxy, so the operator credential never reaches the browser.
