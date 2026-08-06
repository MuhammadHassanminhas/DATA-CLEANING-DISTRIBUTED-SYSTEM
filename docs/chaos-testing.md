# Chaos testing

Step 3.8. The load harness (`docs/load-testing.md`) asks how fast the
pipeline is and whether anything is lost when nothing goes wrong. This one
breaks things continuously on purpose and asks whether the ledger still
adds up.

Everything below is `scripts/chaos.py`, which reuses `scripts/loadtest.py`
— same simulated worker, same real registrations, real WebSocket sessions
and real result envelopes. The coordinator cannot tell one from a container
(§3.5 of `CLAUDE.md`), which is what makes the faults real.

---

## The single command

```bash
python scripts/chaos.py \
  --url https://localhost:8443 \
  --enrollment-secret "$ENROLLMENT_SECRET" \
  --admin-secret "$ADMIN_SECRET" \
  --workers 10 --tasks 1000 --insecure
```

`--insecure` is for the local dev CA only. The public ingress carries a
real Let's Encrypt certificate and needs no such flag — see *Against
staging* below.

Exit codes: **0** every invariant held, **1** at least one did not (the
names are printed on stderr as `FAIL: …`), **2** the arguments were wrong.
The full report is JSON on stdout, and `--json-out FILE` also writes it.

Run it from a virtualenv with `worker/requirements.txt` installed — the
same one the load harness uses:

```bash
python -m venv .venv-loadtest
.venv-loadtest/Scripts/pip install -r worker/requirements.txt   # Windows
```

---

## What it asserts

| Check | Fails when |
|---|---|
| `chaos_was_actually_applied` | no fault fired — a green run that broke nothing proves nothing |
| `every_task_accepted` | the enqueue was short |
| `every_task_read_back` | a task the coordinator accepted is not in its own listing |
| `no_duplicate_task_rows` | one task id produced two rows |
| `no_task_lost` | anything ended other than `COMPLETED` (including terminally `FAILED`) |
| `every_completion_has_one_result` | a completed task has no stored result |
| `no_injected_submission_completed_a_task` | a duplicate or stale submission transitioned a task — a double completion |
| `every_acked_injection_was_refused` | an injected submission got a verdict outside the known refusal set |
| `no_injection_unanswered_on_a_live_session` | a submission on a socket that stayed up was never answered |
| `converged_no_tasks_in_flight` | something is still `QUEUED`, `ASSIGNED` or `RUNNING` after chaos stops |
| `converged_within_timeout` | convergence never happened inside `--converge-timeout` |

Counted from the coordinator's own rows through the operator API, never
from the harness's tally.

**Queue depth is reported, not asserted** — it is global, and a deployed
environment has work of its own.

---

## The faults

| `--faults` name | What it does |
|---|---|
| `kill` | aborts the socket with no close frame, cancels the worker's in-flight executions and drops its unacknowledged results — a killed container, not a network blip |
| `freeze` | the session stops reading, heartbeating and executing while the socket stays open, for `--freeze-seconds` |
| `duplicate` | re-sends a completed task's exact envelope |
| `stale` | re-sends it with an earlier attempt number and a fresh idempotency token |
| `command` | runs each `--chaos-command` in turn |

Default: `kill,freeze,duplicate,stale`. One fault fires every
`--fault-interval` seconds (default 5) for as long as the batch is
draining.

**`--freeze-seconds` must exceed the lease TTL or the fault costs the
worker nothing.** A silent worker keeps its lease until `TASK_LEASE_TTL_SECONDS`
runs out, so the default is 75s against the shipped 60s TTL. Lower it to
match a stack whose TTL is lowered.

### Environment faults

Coordinator eviction, a database blip, a Redis blip — anything below the
protocol — is a command, because a harness cannot know how to do those in
every environment:

```bash
python scripts/chaos.py --url … \
  --faults kill,freeze,duplicate,stale,command \
  --chaos-command "docker compose -p dcds38 restart redis" \
  --chaos-command "docker compose -p dcds38 restart coordinator"
```

Each is run in turn on the fault schedule, and its return code and output
land in the report. **No run recorded in `docs/phase-3-fault-tolerance.md`
§3.8.5 used one**, so this path is unproven there.

---

## Repeatability

`--seed N` fixes the fault schedule: same seed, same sequence of faults on
the same workers. Without it a seed is chosen and **printed in the report**
(`chaos.seed`), so any run can be replayed.

---

## Against staging, over the public Internet

```bash
set -a; . ./.env; set +a
python scripts/chaos.py \
  --url https://dcds-staging.centralindia.cloudapp.azure.com \
  --workers 5 --connect-batch 5 --tasks 300 \
  --faults kill,duplicate,stale --fault-interval 5 \
  --timeout 600 --converge-timeout 300
```

No `--insecure`: the certificate must validate, and if it does not, that
is a finding.

**Five workers, not fifty.** Staging runs the shipped
`REGISTER_RATE_LIMIT_PER_MINUTE` of 5 per source IP and it is left exactly
as deployed rather than raised to flatter the test (the same call Decision
#142 made for the load harness). The run proves the path and the
invariants, not a ceiling.

The cluster has to be running. Staging has workers of its own, so the
recovery feed in the report is fleet-wide context rather than this run's
own numbers — it says so in the report.

---

## Making it fail on purpose

A chaos suite nobody has seen fail is not evidence. This makes every
attempt outlive its lease, so tasks exhaust their retries and end
terminally `FAILED`:

```bash
python scripts/chaos.py --url https://localhost:9495 \
  --workers 3 --tasks 6 --max-concurrent 2 \
  --task-type sleep --parameters '{"seconds": 30}' \
  --faults freeze --freeze-seconds 26 --insecure
```

```
FAIL: no_task_lost
```

exit 1, and `read_back.by_status` shows `{"FAILED": 6}`.

---

## In CI

`.github/workflows/chaos.yml` runs the suite weekly (Mondays 05:23 UTC)
and on `workflow_dispatch`, against a throwaway Compose stack on the
runner with shortened lease windows. It is **not** a required check on
`main`: it deliberately breaks things and waits for lease windows, so it
takes minutes rather than seconds. Every check the harness reports gates
the job — the script exits 1 and names the violation, so the job needs no
threshold of its own.

```bash
gh workflow run "Chaos suite" -f workers=10 -f tasks=500
```

---

## The knobs

| Flag | Default | Notes |
|---|---|---|
| `--workers` | 10 | simulated sessions, not machines |
| `--tasks` | 1000 | one `POST /tasks` batch |
| `--max-concurrent` | 4 | credits each worker declares |
| `--task-type` / `--parameters` | `sleep` / `{"seconds": 2}` | **not** the load harness's cheap workload: a fault that lands between tasks interrupts nothing |
| `--faults` | `kill,freeze,duplicate,stale` | an unknown name is refused, never dropped |
| `--fault-interval` | 5.0 | seconds between faults |
| `--freeze-seconds` | 75.0 | must exceed the lease TTL |
| `--chaos-command` | — | repeatable |
| `--seed` | random, reported | fixes the schedule |
| `--converge-timeout` | 300.0 | seconds to wait for nothing to be in flight |
| `--timeout` | 900.0 | seconds to wait for the drain |
| `--json-out` | — | also write the report to a file |
| `--insecure` | off | local dev CA only |
