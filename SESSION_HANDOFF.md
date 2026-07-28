# SESSION_HANDOFF.md

Read `CLAUDE.md` first (guardrails), then `PHASE_STATE.md` (authoritative
status, decisions log, blockers). This file is a resume-work pointer for
the next session — it is not a source of truth, `PHASE_STATE.md` is.

---

# Where things stand

## 2026-07-28 (session 8) — MILESTONE 1.5 COMPLETE AND SIGNED OFF

**M1.5 is DONE.** Signed off by the user 2026-07-28 on SHA
`4575097157debe48d71b9f3aff86248c599d5b1e` (PR #13 merge), deployed by CD
to **both** staging and production — `/health` returned that exact SHA in
each. Full detail and the honest exit-criteria accounting are in
`PHASE_STATE.md` (1.5.9 register row + Decision #78).

**Next: M2 — Task Distribution, design gate 2.0. NOT STARTED. Do not
begin without the user's go-ahead (§9).**

### What this session did

Started as "is M1.5 actually closeable?" and found four real problems.

1. **Credential exposure (§12 violation).** This file held
   `ENROLLMENT_SECRET` and `DASHBOARD_PASSWORD` in plaintext across six
   occurrences, on a public repo, next to the public coordinator FQDN and
   public worker image. **Both rotated and applied live. Verified: old
   enrollment secret → 401, new → 201; old dashboard password → 401, new
   → 200; anonymous → 401.**
2. **Fresh-clone gap (§13).** Cluster Secrets lived only in the
   gitignored `.env`. Nine Secrets now committed encrypted under
   `infra/sealed-secrets/`, validated 9/9 from an actual fresh clone.
3. **Terraform drift was undetectable.** CRLF-vs-LF made three
   `helm_release` values diff against identical text, so the plan could
   never come clean and real drift hid in the noise. Plan went **4 → 1**.
4. **`bootstrap-secrets.ps1` reported success after every apply failed.**
   Native command failures in a PowerShell pipeline do not trip
   `$ErrorActionPreference="Stop"`. Fixed, plus openssl resolution.

Also newly proven, never tested before at any point: **database offline**
→ readiness fails with 0 restarts, `DatabaseDown` fires to Discord at
T+3m41s, full recovery 38s after restore, no data loss.

### Still open — read before starting M2

- **`GRAFANA_ADMIN_PASSWORD` and `POSTGRES_PASSWORD` are still exposed
  and still live.** Both were public via `.env.example` history. The user
  explicitly decided to leave them. Rotating Postgres needs a coordinated
  `ALTER USER` or the coordinator loses its DB connection; Grafana stores
  its admin user in its own database. Both are in-cluster only.
- **Step 1.5.6 #7 Alertmanager route: committed, deliberately not
  applied.** Discord still receives the noisy kube-prometheus built-ins.
  This is the single change `terraform plan` reports. Apply with
  `infra/apply-alertmanager-route.ps1` whenever wanted.
- **`DatabaseDown`/`RedisDown` use `max(...)==0`** and fire only because
  Prometheus still scrapes NotReady pods. Works today, but lacks the
  `absent()` guard `CoordinatorDown` got in 1.5.6 C3. Worth hardening in
  M3.
- **A DB outage 503s the entire public surface, dashboard included** —
  correct Kubernetes behaviour, but the debugging UI disappears exactly
  when it is most wanted.
- **Not verified:** that the old enrollment secret is still rejected
  *after* the CD redeploy. The cluster was stopped before that check
  finished. Indirect evidence is strong (9/9 SealedSecrets `synced=True`
  post-CD, Secrets are SealedSecret-owned so Helm cannot revert them) but
  it is inferred, not measured. Re-check on next cluster start.
- **Cost was never tracked against estimate** — a 1.5.9 exit criterion
  that was not met.
- One stray `REGISTERED` worker row (`agent_version=rotation-check`) from
  the rotation test; it will never connect.

### Operational notes

- Secrets now come from `kubectl apply -f infra/sealed-secrets/`, **not**
  from `bootstrap-secrets.ps1`. Those Secrets are owned by their
  SealedSecrets, so deleting a SealedSecret garbage-collects its Secret.
- Adopting pre-existing Secrets needed the
  `sealedsecrets.bitnami.com/managed=true` annotation **plus a controller
  restart** — the controller refuses to take over Secrets it does not
  own, and logs "giving up" without retrying.
- `docs/runbook.md` now holds deploy, rollback, scale, teardown, health
  checks, and every failure mode previous sessions hit.
- **Never put a credential in this file again.** Reference the `.env` key
  name. Scrubbing does not remove values from Git history — they persist
  in earlier commits and in every clone and fork. Rotation, not deletion,
  is the remediation.

### Cluster state at session end

**Stopped** (`az aks stop` by the user, billing $0). Resume with
`az aks start` + `az aks get-credentials`; the public endpoint returns
1–2 min after start. Working tree clean; `main` at `4575097`, everything
pushed.

---

## 2026-07-27 (session 7) — Step 1.5.8 DONE + APPROVED (CPU/mem gap fixed; rest waived by user)

**Step 1.5.8 (Real Internet worker onboarding) — DONE, user-APPROVED 2026-07-27.** The one
real gap left from session 6 — native Windows/macOS workers showing blank CPU/memory on the
dashboard — is fixed.

**Fix (Decision #77):** `worker/worker.py` now reads CPU/memory via `psutil`
(`psutil.cpu_percent(interval=None)` / `psutil.virtual_memory().percent`), cross-platform,
replacing the Linux-only `/proc/stat`+`/proc/meminfo` readers that returned `None` off-Linux.
`psutil>=5.9,<7` added to `worker/requirements.txt` so the installer + published image carry it.
**Verified on this native Windows host:** readers return real values (cpu 44.4/37.5, mem 65.4 —
was `None`); worker test suite green (5 passed/1 skipped). **Shipped as PR #12**
(`phase-1.5.8-worker-onboarding`→main) — CI running / to merge, CD then deploys.

**User scope call:** the user directed 1.5.8 be closed after this fix and **explicitly not** to
pursue the rest. So **WAIVED, not verified:** VPS-in-another-country, mobile-hotspot, and the
simultaneous-multi-worker dashboard capture. Recorded honestly (§10) as a user discretion call,
same family as #34–35. Machine types verified: 4 of 5 (#1 laptop Docker, #2 2nd PC, #3 native
Windows no-Docker, #4 friend's PC own ISP).

**State at session end:** node was already stopped at session start (offline work only); no
`az aks start` needed for this fix — verified at the source on Windows. **Uncommitted:**
`PHASE_STATE.md` + this file's DONE edits (about to commit to the PR branch). Untracked loose
ends unchanged (`.claude.backup/`, `.playwright-mcp/`, screenshots, `demo-worker.yaml`,
`infra/apply-*.ps1`). Loose ends from 1.5.6/1.5.7 (#7 alert route committed-not-applied) still
left as-is per user.

**Next step: Step 1.5.9 — M1.5 demo and verification — NOT STARTED.** Do NOT start without user
go-ahead (§9). This is the M1.5 closeout phase (fresh-clone run, full demo). Merge PR #12 first
so `main` carries the psutil fix.

---

## 2026-07-27 (session 6) — Step 1.5.8 IN PROGRESS — non-Docker onboarding built + Windows worker verified live

**Step 1.5.8 (Real Internet worker onboarding) — IN PROGRESS.** Design sub-gate = Decision #76
(A1 bootstrap installer + B1 keep shared `ENROLLMENT_SECRET`).

**Built:** cross-platform worker + two installers + onboarding doc + a lock self-check.
- `worker/worker.py` — **three portability fixes** so the bare-Python worker runs on machines
  without Docker (esp. native Windows): (1) single-instance lock `fcntl`→`fcntl`/`msvcrt`;
  (2) `_ssl_context()` uses the dev-CA only if the file **exists**, else system trust — fixes the
  Windows "empty env var not propagated" crash AND makes the public-endpoint path work with no CA
  file; (3) `add_signal_handler(SIGTERM)` wrapped in `try/except NotImplementedError` (Windows
  Proactor loop has none). All three found by actually running the worker on this Windows host.
- `worker/install-worker.sh` + `install-worker.ps1` — clone→venv→run, config = 3 values.
- `docs/onboarding-a-worker.md` — Docker/Linux/Windows onboarding + operator issue/revoke/rotate.
- `tests/test_worker_lock.py` — passes on Windows (exercises the new msvcrt branch); full suite 5 passed/1 skipped.

**Verified live (this session):** started the node, staging came up (3 coordinators on SHA `94ba53e`,
public `/health`=200). Ran the fixed worker in a venv against `https://dcds-staging.centralindia.cloudapp.azure.com`
(system-CA trust, no Docker): `registered`→`access_token_refreshed`→`ws_connected` (epoch 1),
server-side `status=ONLINE` on `/workers`. Recorded as machine #3 in PHASE_STATE's verified table.
(cpu/mem = None on Windows — no `/proc`, expected.)

**Pipeline SHIPPED:** PR #11 (`phase-1.5.8-worker-onboarding`) merged to main (merge `42d8c4c`);
CI green (incl. the new `test_worker_lock.py` on Linux); **CD deployed staging + production on SHA
`42d8c4c`** (`/health` returns it); worker image published at that SHA. So the non-Docker installer
now works from a clone (`git clone` main → the fixes are there).

**Friend's PC VERIFIED (machine #4):** user's friend ran the documented `install-worker.ps1`
one-liner unaided on their own ISP (native Windows, no Docker) → `registered`+`ws_connected`
(epoch 1, `worker_id 8b735695…`, agent `0.1.0`); coordinator recorded it. Ended by Ctrl+C so it
shows OFFLINE after — the connect itself succeeded. Satisfies "friend's computer, own ISP, from
docs only."

**KNOWN GAP found this session (→ next step #1):** native Windows/macOS workers show **blank
CPU/memory on the dashboard** — `_read_cpu_percent()`/`_read_memory_percent()` read `/proc/stat`
+ `/proc/meminfo` (Linux-only) and return `None` elsewhere. Real §6 GUI gap for non-Docker/non-Linux
workers (Docker workers are fine — Linux inside). **Agreed fix = adopt `psutil`** (recommended over a
stdlib `ctypes` per-OS branch: ~6 lines, deletes the `/proc` parsing, also covers macOS; supersedes
the old Linux-only "no psutil" note), then push through the pipeline.

**NEXT STEPS to finish 1.5.8 (next session, node up):**
1. **Cross-platform CPU/mem via `psutil`** in `worker/worker.py` → commit → PR → CI → CD, so the
   installer + published image carry it and the dashboard shows CPU/mem for every worker.
2. Connect the last two machine types: **VPS in another country** (Docker one-liner, cross-region
   latency) + **mobile hotspot** (tether a laptop).
3. Capture the dashboard with **several workers running simultaneously**, region-latency deltas
   visible (the "all on one dashboard at once" + latency criteria).
4. User approval → **1.5.8 DONE**.
Already verified toward the 5 types: laptop Docker + 2nd PC (1.5.5, #1/#2), native Windows no-Docker
(#3), friend's PC from docs (#4). Loose ends from 1.5.6/1.5.7 still left as-is per user.

**Cluster state at session end:** `az aks stop` issued (billing $0). `az aks start -g
data-cleaning-distributed-system-rg -n data-cleaning-distributed-system` + `az aks get-credentials
...` to resume; endpoint `https://dcds-staging.centralindia.cloudapp.azure.com` comes back ~1-2 min
after start. Enrollment secret `$ENROLLMENT_SECRET`; dashboard `operator` /
`$DASHBOARD_PASSWORD`. **Uncommitted at end:** PHASE_STATE.md + this file's next-step edits only (doc);
untracked loose ends unchanged (`.claude.backup/`, `.playwright-mcp/`, screenshots,
`demo-worker.yaml`, `infra/apply-*.ps1`).

---

## 2026-07-27 (session 5) — Step 1.5.7 BUILT + VERIFIED LIVE, awaiting approval

**Step 1.5.7 (Coordinator horizontal scaling proof) — all 6 exit criteria met live on
AKS staging; APPROVED by user 2026-07-27. Committed on branch `phase-1.5.7-scaling-proof`.**
Design gate = Decision #75.
Almost no new code — it proves invariant §3.9, already built (#19/#29/#30).

**What changed:**
- `infra/terraform/variables.tf` — `namespace_quota` raised (limits.cpu 2→3, limits.memory
  2Gi→3Gi, requests 1→1.5 / 1Gi→1.5Gi). Option **A2** (user's pick): keep the 300m coordinator
  CPU limit, raise the quota instead. Applied via `terraform apply -target=kubernetes_resource_quota.env`
  (3 in-place). **The user ran the apply by hand** — `terraform apply` is blocked from the agent
  by the auto-mode classifier.
- `infra/helm/platform/values-staging.yaml` — coordinator HPA min 2→3, max 2→5. Deployed with
  a local `helm upgrade --install platform ... --set coordinator.image.tag=b1963f90… --set dashboard.image.tag=b1963f90… --atomic` (image tag preserved).
- `infra/loadtest-coordinator.yaml` — NEW versioned synthetic-load Job (§7): floods
  `coordinator:8443/ready` (in-cluster, bypasses the ingress edge `limit-rps`) with fresh-TLS
  connections. **Option B1** — synthetic HTTP load, honest ceiling (no M2 task pipeline yet).

**Verified live (all objective, via kubectl + per-replica `/workers` queries):**
1. 3 replicas serve one fleet (across both nodes). 2. Distribution — 6 workers reconnected,
each of 3 replicas served distinct `worker_id`s (per-pod logs). 3. Killed `fqjnv` (4 workers) →
reconnect to survivors + replacement pod → `connected` back to 6 in ~30s. 4. Load → HPA
**191%/70% → 3→5**; load ends → CPU 3% → held 5 through ~5-min scaleDown window → **5→3** at
15:16:51. 5. `/workers` from **every** replica identical (`total=37 connected=6 online=6`).
6. Killed all 3 originals in turn → fleet unchanged every time (§3.9, no in-memory authoritative state).

**Gotchas this session:**
- `terraform apply` **and** destructive/`kubectl delete` on prod are blocked by the classifier;
  staging pod-delete/scale went through the agent's PowerShell fine. Quota apply = user-run.
- The `!` prompt shell and the Bash tool are a **minimal sandbox bash with no sed/grep/terraform/kubectl
  and no Windows PATH** — all `az`/`kubectl`/`helm`/`terraform` must go through the **PowerShell** tool.
- First load Job was quota-rejected (800m limit + 2400m used > 3000m) — dropped the load pod to 400m
  and demo-workers to 1 so the HPA could still reach 5 while the load pod ran.
- `/ready` is ~700ms/call (DB+Redis+TLS each hit) so it's genuinely CPU-heavy — 80 concurrent
  fresh-TLS floods easily crossed the 70% target.
- To query a pod's `/workers` (needs `x-admin-secret`), `kubectl exec ... python -c` with the
  script **base64-encoded** avoids all the PowerShell/quote breakage.

**Merged + shipped through the pipeline (end of session):** PR #10 merged to `main`
(merge commit `94ba53e15e264803b00d77b4633029184366f5d3`). CI green on `main`
(run `30259092670`). **CD ran exactly per plan (Decision #67):** staging auto-deployed,
production held at the `required_reviewers` gate → **user approved** → deployed. Both envs
verified live on SHA `94ba53e`: `/health` version matches, `/ready` db+redis ok; **staging now
runs HPA min 3/max 5 canonically via CD** (3 coordinator pods), production HPA min 2 unchanged
(1.5.7 was staging-only). So the 1.5.7 config is durable on `main` — no revert-on-next-deploy.

**State at session end:** the load Job is deleted, demo-worker at 1. Node was RUNNING at
verification (CD needs it up; Option C never starts/stops it) — **user is stopping it now
(`az aks stop`)** to halt billing. Loose end from 1.5.6 (#7 alert route) still
committed-not-applied — user said leave Discord alerts as-is, so untouched.

**Next step (after go-ahead): Step 1.5.8 — Real Internet worker onboarding — NOT STARTED.**
Do NOT start without user go-ahead.

---

## 2026-07-27 (session 4) — Step 1.5.6 VERIFIED + APPROVED DONE

**Step 1.5.6 (Observability) — DONE, user-APPROVED 2026-07-27.** Final SHA
`b1963f90d53066a836248e11be231286893670aa`. All 6 exit criteria verified live on
AKS staging (full detail in PHASE_STATE Decision #74 + the 1.5.6 register row).

**What this session did:** started the node, walked the 6 criteria. C1/C4/C5/C6
passed as-is. Verification found **two real gaps + the noise item**, fixed in
**PR #9** (merged to main; CI green incl. tests; CD deployed staging on `b1963f90`):
- **C2** — WS heartbeat/session logs had `correlation_id="-"`; worker minted a
  fresh random id per envelope. Fix: worker mints ONE session id/connection, sends
  `X-Correlation-ID` on token-refresh + in hello/heartbeat/pong; coordinator binds
  `hello.correlation_id` to the WS coroutine contextvar. Verified: session
  `beed3ee8` traced by one id across BOTH replicas (HTTP leg on `q4skv`, WS leg on
  `jgbvf`). (`worker/worker.py`, `coordinator/app/main.py`)
- **C3** — `CoordinatorDown` expr `max(up)==0` can't fire when all pods are gone
  (empty vector ≠ 0). Fix: `absent(up{...})==1 or max(up{...})==0`
  (`prometheusrules.yaml`). Verified: scaled coordinator→0 → firing at ~90s →
  Alertmanager `receiver=chat` (Discord) → resolved on recovery.

**LOOSE END — #7 (quiet built-in alert noise) is committed but NOT applied live.**
Every platform alert now labelled `team: dcds`; Alertmanager default receiver →
`null`, only `team=dcds` → chat (in `observability.tf` + `prometheusrules.yaml`,
both merged in PR #9). Pushing it needs a `terraform apply` on the
kube-prometheus-stack release — the user's full `apply-observability.ps1` was
killed mid-session. To apply just this, run **`infra/apply-alertmanager-route.ps1`**
(untracked helper — targeted `terraform apply -target=helm_release.kube_prometheus_stack`,
~1-2 min). Until then Alertmanager still sends the noisy kube-prometheus built-ins
to Discord. NOT an exit criterion; do it whenever.

**State at session end:**
- Node was **RUNNING** at approval (user was going to `az aks stop` — confirm it's
  stopped, billing). Staging: 2 coordinators + 2 demo-workers + dashboard + pg +
  redis on `b1963f90`. Production untouched (`69028dee`; PR #9's CD prod gate is
  waiting/unapproved — leave it or approve as a maintenance step).
- **Uncommitted/untracked:** the three verification screenshots in repo root
  (`c1-fleet-dashboard.png`, `c2-correlation-across-replicas.png`,
  `c4-authspike-firing.png`), `infra/apply-alertmanager-route.ps1`,
  `PHASE_STATE.md` + this file's doc edits, plus the pre-existing `.claude.backup/`,
  `.playwright-mcp/`, `demo-worker.yaml`. No commit of these requested.
- Grafana fleet dashboard `fleet-15-6` + the `AuthFailureSpike` Loki alert rule
  were created live in Grafana this session (in-cluster only, reachable via
  `kubectl -n observability port-forward svc/kube-prometheus-stack-grafana 3000:80`;
  admin creds in `.env`).

**Next step: Step 1.5.7 — Coordinator horizontal scaling proof — NOT STARTED.**
Open its short design sub-gate (§9), get user approval, then build. Optionally
first apply the #7 route + approve the prod CD gate as quick maintenance.

---

## 2026-07-27 (session 3) — Step 1.5.6 built + stack LIVE, mid-verification

Read first. **Step 1.5.6 (Observability) — IN PROGRESS, NOT DONE.**

**Built + committed** (Decisions #71–#72): coordinator `/metrics` (prometheus-client),
`infra/terraform/observability.tf` (kube-prometheus-stack + Loki + Alloy), node_count
1→2, platform-chart `observability.enabled` + ServiceMonitor + PrometheusRule,
`infra/apply-observability.ps1` runner, bootstrap extended.

**LIVE on AKS:** ran `apply-observability.ps1` → 2 nodes, all 9 observability pods
Running (Prometheus/Grafana/Alertmanager/Loki/Alloy×2/KSM/operator/node-exp×2).
Two live fixes this session: (a) PowerShell 5.1 splits `terraform -target=...` on the
`=` → quote it (`"-target=..."`); (b) Alertmanager stuck `undefined receiver "null"` —
the chart keeps a Watchdog child-route to a `null` receiver my `alertmanager.config`
didn't define; fixed in observability.tf (added `null` receiver + Watchdog route) and a
re-apply's `helm upgrade` brought Alertmanager `READY 1`. NOTE: helm `--wait` does NOT
wait on the operator-created Alertmanager/Prometheus statefulsets, so the first apply went
"green" while Alertmanager was still broken — check the CR (`kubectl get alertmanager -n
observability`) not just the apply exit.

**Discord webhook** in `.env` as `ALERTMANAGER_WEBHOOK_URL` (Discord URL + `/slack`).
Grafana admin creds in `.env`. Alert secret mounted; delivery not yet demoed.

### Shipped to staging + verified
PR #8 **merged to main**; CI built the `/metrics`-capable coordinator image at SHA
**`bc2c02e59a54e23df95daaf8bc467ad5d8a77610`**; CD deployed staging to it. Verified live
(read-only) before stopping the node:
- staging coordinator = 2 pods Running on image `bc2c02e`.
- **Prometheus scrapes both replicas `up=1`** (`serviceMonitor/staging/coordinator`, HTTPS
  8443, insecureSkipVerify) — the `/metrics` pipeline works end to end.
- **Alertmanager → Discord delivery PROVEN** — the kube-prometheus built-in alerts
  (KubeHpaMaxedOut/KubeCPUOvercommit/KubeClientErrors) landed in Discord at ~11:53. Those
  are noisy defaults, not our phase alerts (see "quiet the noise" below).

### CD gotcha hit + fixed (leftover state, not a 1.5.6 bug)
First CD run FAILED: smoke `kubectl exec deploy/coordinator` → "timed out waiting for the
condition" because the staging coordinator Deployment was at **0/0**. Chain: the Deployment
has no `replicas` field (HPA owns it, by design); session-2's manual scale-to-0 left it at
0; **an HPA will not lift a workload off 0 replicas**; so `helm upgrade` kept 0 → no pod →
smoke fails → CD rollback fired (`helm history` rev 12 = "Rollback to 10"). Fix that
unstuck it: `kubectl -n staging scale deploy/coordinator --replicas=2` then re-run CD
(`gh run rerun <id>`). If a future manual scale-to-0 recurs, same fix.

### 6 exit criteria — status (stack proven, formal walkthrough pending)
1 fleet dashboard — metrics ARE flowing (up=1); open Grafana + confirm a panel. 2
correlation-ID trace across coordinator replicas in Loki — pending. 3 kill coordinator →
CoordinatorDown alert to Discord — delivery path proven; fire OUR alert specifically. 4
auth-spike alert — author a LogQL alert in Grafana over `*_rejected`. 5 retention=72h —
done in config (Loki `retention_period`). 6 no secrets in Loki logs — run a query.

### Cluster state
**Node STOPPED (`az aks stop` done, billing $0).** `az aks start -g
data-cleaning-distributed-system-rg -n data-cleaning-distributed-system` to resume, then
`az aks get-credentials ...`. Production still on `69028dee`. Staging on `bc2c02e` with
observability. 2-node pool (node_count=2) — dial back to 1 after M1.5 if credit tight.

### Loose ends for next session
- **Quiet the Discord noise:** built-in kube-prometheus alerts (esp. permanent
  KubeHpaMaxedOut on the pinned prod HPA) spam Discord. Route only OUR PrometheusRule
  alerts to the `chat` receiver; drop/inhibit the generic defaults. Small Alertmanager
  route change + re-apply.
- Then walk criteria 1–4/6, get user approval, mark 1.5.6 DONE.

### Next step
`az aks start` → confirm endpoints → quiet alert noise → walk the 6 criteria → user
approval → 1.5.6 DONE.

---

## 2026-07-27 (session 2): production redeployed to SHA `69028dee`

Read this first. Between-phase maintenance action, NOT a new phase. 1.5.5
stays DONE; 1.5.6 still NOT STARTED (do not start without user go-ahead).

**What happened:** production had been stuck on old SHA `0df7e206` while
staging was on `69028dee`. The CD run `30099341845` (from 2026-07-24) sat 62h
in the `production` approval gate. This session pushed production up to date.

**Path taken (all approved by user in-session):**
1. `az aks start` (node was stopped) + `az aks get-credentials`.
2. Scaled staging fully to 0 (coordinator/dashboard/redis/demo-worker deploys
   + postgres statefulset) and **deleted the staging coordinator HPA** — the
   single B2s_v2 can't hold both full stacks + ingress controllers, so staging
   made room for production's 2-replica stack.
3. First production deploy FAILED — helm SSA conflict: the prod coordinator
   HPA `.spec.minReplicas` was owned by field manager `kubectl-patch` (from the
   1.5.5 manual `kubectl patch` that scaled prod down 2→1). Helm wanted `min=2`
   and refused to override. **Fix = `kubectl -n production delete hpa
   coordinator`, then re-run the CD job** (user ran both — a permission
   classifier blocks destructive prod kubectl/gh from the agent).
4. Re-run + re-approve → deploy GREEN. helm recreated the HPA clean at min=2.

**Verified live in-cluster (read-only):** prod `/health` version=
`69028dee1613923daed0c5da2fe4970ffd586e1f` (matches target), `/ready`
database+redis ok, 2 coordinators + dashboard + postgres + redis all `1/1`,
HPA `2/2` at ~5% CPU. Nothing broke.

**Cluster state at session end:**
- **Node stopped by the user** (billing $0).
- Production: full stack on `69028dee`, HPA min=2 (stale min=1 conflict gone).
- Staging: all deploys at 0, postgres statefulset 0, **staging coordinator HPA
  DELETED** — both restore on the next staging helm/CD deploy. Config + PVCs
  intact; not a data loss.
- `demo-worker` staging at 0.

**Gotcha for next time (SSA field-manager conflict):** any time you `kubectl
patch`/`scale` a helm-managed HPA by hand, you hand `minReplicas` ownership to
a non-helm field manager, and the next `helm upgrade` fails with a conflict on
that field. Cleanest fix is to delete the object so helm recreates+owns it. The
staging coordinator HPA was already deleted this way in 1.5.5 for the same
reason — prefer scaling the underlying Deployment or setting values +
`helm upgrade` over ad-hoc `kubectl patch` on helm-owned objects.

**Uncommitted:** `PHASE_STATE.md` + `SESSION_HANDOFF.md` doc edits on branch
`phase-1.5.5-ingress-tls-dns` (local, this wrap-up + the 2026-07-27 session-1
edits). No commit requested. Untracked, intentional: `.claude.backup/`,
`.playwright-mcp/`, `demo-worker.yaml`.

### Next step (unchanged)
**Step 1.5.6 — Observability stack — NOT STARTED.** Open its short design
sub-gate (§9), get user approval, then build. Resume commands (cluster up,
endpoint check, secrets) in the 2026-07-24 section below. When resuming
staging, its coordinator HPA + workloads come back on the next helm/CD deploy.

---

## 2026-07-27 (session 1) — Step 1.5.5 DONE + APPROVED

Everything below (2026-07-24, k3d/Cloudflare, GCP/OCI) is
superseded history — compute host has been Microsoft Azure / AKS since #57.

**Step 1.5.5 (Public ingress, TLS, DNS) — DONE, user-APPROVED 2026-07-27.**
Final SHA `69028dee1613923daed0c5da2fe4970ffd586e1f`.

All 6 exit criteria met. The 2 that were outstanding on 2026-07-24 closed
this session:
- ✅ **3 soak** — re-run this session on the published worker image via the
  public ingress. **Duration reduced 60-min→2-min by explicit user direction**
  (recorded in PHASE_STATE as a user scope call, same family as the 50-worker
  substitutions #34–35). Worker held `session_epoch: 1`, no reconnect, ONLINE,
  uptime 176.7s, 77.7ms latency. PASS.
- ✅ **6 second-network worker** — user ran the worker on a separate PC on a
  different network; connected successfully through the public ingress. The
  "AWS IP" the user mentioned = just the public AKS ingress FQDN, not a
  separate resource.

PHASE_STATE updated: 1.5.5 register row → DONE; "Real Internet Workers
Verified" table now has 2 entries (laptop Docker soak, second PC).

### State at session end
- **Node is `az aks stop`'d** — billing $0. `az aks start` to resume.
- Production still scaled DOWN (coordinator HPA min 2→1, dashboard/redis/
  postgres→0) + staging `demo-worker` 5→1 from 2026-07-24, to fit the ingress
  controllers on the single B2s_v2 node. Reversible via next CD/helm.
- **Uncommitted:** `PHASE_STATE.md` + `SESSION_HANDOFF.md` doc edits on branch
  `phase-1.5.5-ingress-tls-dns` (local). User did not request a commit this
  session. Untracked, intentional: `.claude.backup/`, `.playwright-mcp/`,
  `demo-worker.yaml`.

### Next step
**Step 1.5.6 — Observability stack — NOT STARTED.** Open its short design
sub-gate (§9) first, get user approval, then build. Do NOT start without the
user's go-ahead (guardrails). Resume commands (cluster up, endpoint check,
secrets) are in the 2026-07-24 section below — unchanged.

---

## 2026-07-24 (evening) — Step 1.5.5 built + LIVE, mid-verification (SUPERSEDED by 2026-07-27 above)

Compute host has been Microsoft Azure / AKS since Decision #57.

**Phase: Step 1.5.5 (Public ingress, TLS, DNS) — IN PROGRESS, NOT DONE.**
Built and applied live on AKS. 4 of 6 exit criteria objectively met; 2
outstanding (both need real elapsed time / a second machine, not more code).

### What is live right now
- Staging is PUBLIC at **https://dcds-staging.centralindia.cloudapp.azure.com**
  (static IP **4.240.120.113**, real Let's Encrypt cert). ingress-nginx +
  cert-manager + a Terraform-owned public IP with the free cloudapp FQDN.
- Design + build recorded in **Decisions #69 (gate)** and **#70 (build)**.
- PR #7 **merged to main**; CI published images at SHA **69028dee1613923daed0c5da2fe4970ffd586e1f**
  (the worker image at this SHA HAS the system-CA fix); CD auto-deployed
  staging to that SHA (`/health` returns it).

### Exit criteria status (the whole point of resuming)
- ✅ 1 coordinator public + valid LE cert (`/health`=200, deployed SHA)
- ✅ 2 dashboard public + protected (401 anon / 200 basic-auth)
- ✅ 4 cert renewal proven (deleted secret → cert-manager reissued)
- ✅ 5 edge rate-limit blocks register flood (6th+ request → 429)
- ⏳ 3 **60-min soak**: worker `dcds-ext-worker` (Docker on the laptop, the
  PUBLISHED image) connected through the ingress at **14:05:33Z**, was still
  `session_epoch: 1` (no reconnect) at 14:26Z. **Verify at ≥15:05:33Z** with
  `docker logs dcds-ext-worker | tail -3` — same epoch, no new
  "registered"/"ws_connected" = survived = criterion met. (If the node was
  stopped or the container removed before 60 min elapsed, RE-RUN the soak
  tomorrow — see resume commands.)
- ⏳ 6 **second-network worker**: run the worker on a DIFFERENT PC on a
  mobile hotspot AND a corporate network. The public network path is already
  proven working (a test from another PC reached the coordinator and got a
  401 — that 401 was a mistyped enrollment secret, capital-L "enroLlment",
  NOT a network failure). Correct command below.

### CRITICAL state notes for resume
- **The AKS node may still be RUNNING** (billing) — user was ending the day.
  If the soak finished, run `az aks stop -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system`.
- **Production was scaled DOWN** to free CPU (coordinator HPA min 2→1,
  dashboard/redis/postgres →0) so the ingress controllers could schedule on
  the single B2s_v2 node. Reversible — the next CD deploy / `helm upgrade`
  restores it. Not a defect; the node just can't hold both full stacks +
  the controllers at once.
- `demo-worker` in staging scaled 5→1 (leftover deployment, `demo-worker.yaml`
  is untracked in the repo).
- **1.5.5 is NOT marked DONE** in the Phase Register — needs criteria 3+6
  confirmed and explicit user approval (§15). When both are confirmed, flip
  the register row to DONE and note the final SHA.

### Resume commands (tomorrow)
```
# bring cluster up (interactive az login if needed)
az aks start -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
az aks get-credentials -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system

# confirm public endpoint back up (may take 1-2 min after node start)
curl -s https://dcds-staging.centralindia.cloudapp.azure.com/health

# criterion 3 — (re)start the 60-min soak if needed, then check an hour later
docker run -d --name dcds-ext-worker --rm=false \
  -e COORDINATOR_URL=https://dcds-staging.centralindia.cloudapp.azure.com \
  -e WORKER_CA_FILE= -e ENROLLMENT_SECRET=$ENROLLMENT_SECRET \
  ghcr.io/muhammadhassanminhas/data-cleaning-distributed-system-worker:69028dee1613923daed0c5da2fe4970ffd586e1f
# ...60 min later:  docker logs dcds-ext-worker | tail -3   (epoch still 1 = pass)

# criterion 6 — on a SEPARATE PC on a different network (copy exactly, do not retype):
docker run --rm -e COORDINATOR_URL=https://dcds-staging.centralindia.cloudapp.azure.com -e WORKER_CA_FILE= -e ENROLLMENT_SECRET=$ENROLLMENT_SECRET ghcr.io/muhammadhassanminhas/data-cleaning-distributed-system-worker:69028dee1613923daed0c5da2fe4970ffd586e1f
```
Enrollment secret and dashboard basic-auth password both come from the
gitignored `.env` (`ENROLLMENT_SECRET`, `DASHBOARD_USER`,
`DASHBOARD_PASSWORD`). They are never written into this file — see the
2026-07-28 credential-exposure note at the top.

### Terraform / secrets gotchas (so they don't bite again)
- `TF_CLOUD_ORGANIZATION=DATA_CLEANING_DISTRIBUTED_SYS` is now saved in
  `.env` (was lost as a shell-only var last time). Terraform apply needs:
  `ARM_SUBSCRIPTION_ID=6a171ca5-3089-48d7-98a8-caec998fa574`,
  `TF_TOKEN_app_terraform_io=$TF_API_TOKEN`, `TF_CLOUD_ORGANIZATION` — all
  from `.env`. Workspace runs in LOCAL execution (uses `az` CLI creds).
- `bootstrap-secrets.ps1`'s `openssl passwd` step fails in Windows
  PowerShell (openssl is on git-bash PATH, not PS). The
  `dashboard-basic-auth` htpasswd Secret was created from git-bash instead.
  `.env` now has `DASHBOARD_USER`/`DASHBOARD_PASSWORD`.

### Uncommitted at session end
`worker/worker.py` and the 1.5.5 infra ARE committed + merged (PR #7).
Only `PHASE_STATE.md` / `SESSION_HANDOFF.md` doc edits from this wrap-up may
be uncommitted on branch `phase-1.5.5-ingress-tls-dns` (local). Untracked,
intentionally not committed: `.claude.backup/`, `.playwright-mcp/`,
`demo-worker.yaml`.

---

## 2026-07-23 — local k3d + Cloudflare Tunnel (SUPERSEDED by Azure #57)

Historical. The compute host is Azure / AKS now; ignore the k3d/Cloudflare
plan below except as decision history.

- **Compute host decided = local Kubernetes via k3d** (k3s-in-Docker,
  real CNCF K8s on the user's PC, $0) — Decisions Log #53, resolving the
  previously-open Open Questions #2. V1 keeps Kubernetes; it just runs
  locally instead of on a cloud host.
- **Reachability = Cloudflare Tunnel** (`cloudflared` on the coordinator
  PC → public `trycloudflare.com` URL) — Decisions Log #52. Free, no
  card, works on the company network (the block is a **MAC-filter on
  Google only**, not an account restriction; Cloudflare isn't Google).
- **No paid cloud provider.** Earlier "GCP account restricted / all M1.5
  parked" framing (Decisions #50–#51) was a misread of the block as an
  account restriction — **superseded by #52–#53**. Cloud-account blocker
  is gone.
- **Provider history (do NOT re-litigate):** OCI → k3d → OCI → GCP →
  (final) local k3d + Cloudflare Tunnel. Stands unless the user says
  otherwise.
- **Cloudflare Tunnel setup** (given to user, not yet run): install
  `cloudflared` on the coordinator PC (`winget install
  --id Cloudflare.cloudflared`) → `docker compose up` →
  `cloudflared tunnel --no-tls-verify --url https://localhost:8443` →
  public URL → point a worker on another PC/network at
  `wss://<that-host>` (port 443, no dev-CA needed). Worker/test PCs
  install nothing.
- **Docs are build-ready; no code written this session** (per user
  instruction). Nothing committed or pushed — docs-only
  (`PHASE_STATE.md` ×2, this file). The two identical phase-state files
  were kept in sync — worth collapsing to one canonical file later.

---

## Prior session history (2026-07-22) — superseded above

**Milestone 1 is complete** (unchanged from before this session).
This session worked entirely inside **Milestone 1.5**, and spent most
of its time on **Step 1.5.0 — design gate**, which was decided,
reopened, and re-decided twice before settling:

1. **First pass**: built a full $0-cost design gate around OCI
   (Oracle Cloud) Always Free tier + OKE — Decisions Log #36–41.
   Approved by the user.
2. **User then rejected OCI outright** on learning every major cloud
   requires a card on file even for free tiers, and asked for a
   platform needing no payment method at all. Clarified the user has
   no spare always-on machine — only their main laptop, on only while
   testing. Redesigned around **k3d (k3s in Docker) on the laptop +
   Cloudflare Tunnel/DNS for public reachability** — Decisions Log
   #42–46, superseding #36/#38/#37/#44. Approved by the user.
3. **User then reversed course again**, explicitly choosing to accept
   OCI's card requirement after all ("we are going to use OCI cloud
   for this"). Confirmed via a direct question before acting, since it
   contradicted the stated reason for rejecting OCI the first time.
   Reverted to OCI/OKE — Decisions Log #47–49, superseding #42/#43/#44
   again (i.e. back to the first pass's compute/region decisions).

**Decisions #39–41 and #45–46 were never OCI- or k3d-specific and
stood unchanged through both pivots**: self-hosted Postgres/Redis
in-cluster, one cluster split into `staging`/`production` namespaces,
`sealed-secrets`, ghcr.io for images, Terraform Cloud for remote
state. **Cost ceiling stayed $0/month throughout** — a card on file
for OCI identity verification doesn't change that; Always Free
resources are never billed.

Full history and reasoning for all three passes: `PHASE_STATE.md`
Decisions Log #36–49. Do not re-litigate this without new information
— it's already been through two reversals this session alone.

**Step 1.5.1 — Terraform base infrastructure — started, scaffolded to
match the final (OCI) decision, not run.** New directory
`infra/terraform/`:
- `versions.tf` — Terraform Cloud remote-state backend block, `oci`
  provider block.
- `variables.tf` — OCI API-key auth variables (`tenancy_ocid`,
  `user_ocid`, `fingerprint`, `private_key_path`, `region`,
  `compartment_ocid`), none of which have real values yet.
- `main.tf` — OKE via the community
  `oracle-terraform-modules/oke/oci` module (not hand-rolled
  VCN/subnet/security-list resources), node pool sized to OCI's
  Always Free `VM.Standard.A1.Flex` allowance (2 OCPU/12GB × 2 nodes
  = 4 OCPU/24GB total).
- `README.md` — exactly what's needed before a real `terraform init`
  (see Open Items below), and what Terraform deliberately does not
  manage in this project (DB/Redis, ingress specifics, container
  registry).

**Every file in `infra/terraform/` is explicitly marked UNVERIFIED in
its own comments** — no `terraform` CLI is installed on this machine,
no Terraform Cloud or OCI account exists yet, so nothing has been
through `init`/`validate`/`plan`/`apply`. The module version (`~> 5.0`
of `oracle-terraform-modules/oke/oci`) and its input variable names are
a draft that must be checked against the module's current Terraform
Registry page before trusting this config — do not assume it's correct
without that check.

---

# Open items — not done, not something this session finished

1. **Nothing committed.** `git status` at session end:
   - Modified, unstaged: `SESSION_HANDOFF.md`, `docs/PHASE_STATE.md`,
     `phase_state.md`.
   - Untracked: `infra/terraform/` (new this session), `.claude.backup/`
     (carried forward, pre-existing, still unexplained/harmless),
     `.playwright-mcp/` (new, likely a tool cache dir, not project
     code — not yet judged safe to delete or gitignore).
   - Committing was not requested this session; per CLAUDE.md working
     discipline, none of the above was staged or committed.
2. **Nothing pushed to `origin`.** `main` is still 2 commits ahead
   (`5003ad9`, `78277be`) from before this session — unchanged.
3. **Terraform CLI not installed** on this machine.
4. **No Terraform Cloud account/org/workspace/token** exists yet.
5. **No OCI account exists yet** — needs: account creation (card on
   file, now accepted, Decisions Log #47), an API signing key
   (`user_ocid`, `fingerprint`, private key), `tenancy_ocid`, a
   dedicated project compartment (`compartment_ocid`), and a check of
   which OCI home region actually has `VM.Standard.A1.Flex` (Ampere,
   Always Free) capacity right now — this shape is frequently reported
   "out of host capacity" in popular regions, a real provider-side
   constraint to expect, not a sign of a config error.
6. **`certs;C`** — same stray empty directory noted in every prior
   session's handoff, still present, still not this project's call to
   delete.
7. **Branch protection on `main`** and **PR #1 unmerged** — unchanged
   from every prior session, not touched this session.

---

# Gotchas discovered this session

- **Every major cloud provider requires a payment method on file for
  free-tier signup**, OCI included — this is universal fraud
  prevention, not something specific to any one provider, and drove
  the first design-gate reversal. If a future session is asked again
  for a "genuinely free, no card" cloud Kubernetes option: there isn't
  one among AWS/GCP/Azure/OCI/DigitalOcean. The only real no-card path
  is self-hosting (k3d/k3s on hardware you already own), which is what
  the second (later reverted) pass used.
- **A cloud-vs-self-host decision cascades into more than just
  "compute"**: switching from OCI to k3d-on-laptop also forced changes
  to region (n/a locally), and public ingress (Cloudflare Tunnel
  becomes necessary specifically because a laptop has no stable public
  IP and no port should be opened on the home router) — three
  Decisions Log entries changed together, not just one. When OCI came
  back, all three reverted together for the same reason. Don't treat
  "compute platform" as an isolated decision from "ingress" and
  "region" — they're coupled once self-hosting is on the table.
- **The community `oracle-terraform-modules/oke/oci` Terraform module
  was used instead of hand-rolling OCI's VCN/subnet/security-list/NAT-
  gateway resources individually** — OKE's networking prerequisites are
  verbose and well-templated already; re-deriving them from memory
  risks wrong resource attributes. Flagged as unverified rather than
  asserted correct, since no `terraform init` has run against it yet.

---

# Verification method used this session

None of this session's `infra/terraform/` work has been executed —
no Terraform CLI available, no cloud/Terraform Cloud accounts exist.
Everything under Step 1.5.1 is unverified draft code, explicitly
labeled as such in-file and in `PHASE_STATE.md`, per the zero-
hallucination rule (`CLAUDE.md` §10) applying to infrastructure code
exactly as it does to application code and benchmark claims.

The `docker compose`-based verification method that closed out every
Phase 1 phase (fresh clone, `docker compose up`, `curl`/`GET
/api/workers`, `docker kill`/`pause`/`network disconnect` for failure
injection) was not used this session — no code from M1 was touched,
and M1.5's own tooling (Terraform, OKE, kubectl) isn't installed yet
for a first real verification pass.

---

# Next step

**Compute host decided (local k3d) — build-ready.** Begin **Step 1.5.1
— Terraform base infrastructure**. Order of operations:

1. **Install tooling** on the coordinator PC: `k3d` and the Terraform
   CLI. (Create a Terraform Cloud workspace only if Step 1.5.1's design
   sub-gate keeps Terraform for remote state — Decision #46.) None of
   these are Google, so the company network block doesn't affect them.

2. **Open Step 1.5.1 with a short design sub-gate** (CLAUDE.md §9):
   decide Terraform's now-reduced scope given there are no cloud
   resources to provision — e.g. Terraform driving the k3d cluster +
   namespaces + Helm releases via the `kubernetes`/`helm` providers,
   versus creating the cluster with the k3d CLI and using Helm directly.
   Present options, recommend, get user approval before writing code.

3. **Rewrite or retire the existing `infra/terraform/`** — it is
   OCI-targeted and must not be built on as-is.

4. **Cloudflare Tunnel for §8 testing** (usable independently, any time):
   install `cloudflared` on the coordinator PC
   (`winget install --id Cloudflare.cloudflared`) → `docker compose up`
   → `cloudflared tunnel --no-tls-verify --url https://localhost:8443`
   → point a worker on a second PC / hotspot laptop at `wss://<host>`
   (port 443, no dev-CA needed) → verify register / heartbeat / reconnect
   across the real network.

Provider-agnostic Decisions #39–41 / #45–46 (self-hosted Postgres/Redis,
staging/production namespaces, sealed-secrets, ghcr.io, Terraform Cloud
remote state) still stand. Nothing has been committed or pushed.
