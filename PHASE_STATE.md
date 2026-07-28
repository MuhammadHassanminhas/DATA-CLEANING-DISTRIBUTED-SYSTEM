# PHASE_STATE.md

Single source of truth for project status.
Update at every phase transition. Never leave stale.

See `SESSION_HANDOFF.md` for resume-work context from the most recent
session: gotchas hit, open questions mid-decision, uncommitted changes.
This file stays the authority on phase/gate status if the two disagree.

---

## Snapshot

| Field | Value |
|---|---|
| Project | Distributed AI-Orchestrated SQL Database Cleaning Platform |
| Scope in progress | Version 1 — Distributed Worker Network |
| Current milestone | **M2 — Task Distribution, IN PROGRESS.** M1.5 complete and signed off 2026-07-28. **Design gate 2.0 is DONE and user-approved 2026-07-28 (Decisions #79–#82)** — queue = Postgres `FOR UPDATE SKIP LOCKED`, assignment = hybrid credit-based push, results = separate table, task types = coordinator-side registry with the envelope unchanged. **Step 2.1 (task model, schema, state machine) is DONE and APPROVED 2026-07-28** — all 5 exit criteria objectively met (migration upgrade/downgrade/re-upgrade proven against a real ephemeral Postgres; 72 new unit tests; suite 77 passed/1 skipped; ruff clean). **Approved as a step, but still UNSHIPPED: not committed, never run in CI, never deployed — and currently sitting on the M1.5 branch `docs/m15-signoff`. It needs a branch off `main` → PR → CI → CD before Step 2.2 builds on it. Next: Step 2.2 (durable task queue) — NOT STARTED, do not begin without approval (§9).** |
| Current phase | **M1.5 restarting from scratch on Microsoft Azure / AKS.** By user direction 2026-07-24, **all prior Phase 1.5 build work (done on local k3d) is SCRAPPED** and M1.5 is redone on Azure (Decision #57–#58). Design gate 1.5.0 = Azure; **Steps 1.5.1–1.5.9 reset to NOT STARTED** and the scrapped infra code (`infra/terraform/`, `infra/helm/`) **deleted** (Decision #59). **Step 1.5.1 is now BUILT + VERIFIED on AKS (2026-07-24):** `az login` done (Azure-for-Students sub active), `infra/terraform/` rewritten for `azurerm` (Terraform-owns-cluster, Decisions #60–#62), `terraform apply` created the resource group + Free-tier AKS cluster + single `Standard_B2s_v2` node + staging/production namespaces + quotas + sealed-secrets, all verified live via `kubectl`. Node stopped with `az aks stop` after verification to conserve credit. **Personal demo performed and Step 1.5.1 APPROVED DONE 2026-07-24.** **Step 1.5.2 (Helm packaging) then BUILT + VERIFIED LIVE on AKS `staging` and APPROVED DONE 2026-07-24** — `infra/helm/platform/` chart deploys coordinator+dashboard+in-cluster Postgres/Redis; all 7 exit criteria demonstrated (deploy, probes, laptop worker connect, migrations-before-serve, pod-delete-reschedule+reconnect, rolling upgrade no permanent loss, resource limits); images pushed to public ghcr.io tagged by SHA `999772d`; chart fixes Decisions #63–#65; node `az aks stop`'d after (billing halted). **Step 1.5.3 (GitHub Actions CI pipeline) then BUILT + VERIFIED GREEN ON `main` and APPROVED DONE 2026-07-24 (Decision #66)** — 5-job pipeline (lint/test/build×3/scan/terraform); broken-test-blocks-merge and vuln-dep-flagged both proven via a throwaway PR; azurerm `terraform plan` posted to PR checks summary; SHA-tagged images pushed to ghcr.io on the merge of PR #2 to main (`4969097`); branch protection requires all 7 checks (`strict`). Ruff pinned via `ruff.toml`; Azure service-principal secrets + `TF_API_TOKEN` set in the repo; ghcr package write-access granted to the repo. Next phase: 1.5.4 (GitHub Actions CD pipeline). **Update 2026-07-24: Step 1.5.4 (CD, Option C) built, verified LIVE on AKS, and APPROVED DONE (Decisions #67–#68) — commit→CI→staging-auto→approval-gate→production all exercised end to end on SHA `0df7e206`, both env `/health` endpoints returned the deployed SHA. Node `az aks stop`'d after. Next phase: 1.5.5 (Public ingress, TLS, DNS) — also the step that first lets an off-network worker reach the coordinator.** **Update 2026-07-27: Step 1.5.5 (ingress/TLS/DNS) and Step 1.5.6 (Observability) are both DONE + user-APPROVED. 1.5.6 final SHA `b1963f90`; all 6 exit criteria verified live (C1 dashboard, C2 correlation-id across replicas, C3 CoordinatorDown→Discord, C4 auth-spike alert, C5 72h retention, C6 no secrets in logs); verification found + fixed two gaps (C2 session correlation, C3 `absent()` alert) via PR #9, Decision #74. One loose end: #7 (route only `team=dcds` alerts to Discord) is committed but not yet `terraform apply`'d. Next phase: 1.5.7 (Coordinator horizontal scaling proof).** |
| Phase status | **2026-07-24: compute host changed to Microsoft Azure (AKS), and ALL prior M1.5 build work is SCRAPPED to be redone on Azure (Decisions #57–#58).** Azure CLI v2.88.0 installed locally at `C:\Program Files\Microsoft SDKs\Azure\CLI2` and verified; **not yet `az login`'d**, no subscription selected, no resource created. The account is **Azure for Students** ($100 credit / 12 months / no card); strict cost discipline applies — AKS Free-tier control plane ($0), single small B-series node, no autoscaling, `az aks stop` between test sessions, `az group delete` to nuke everything (see Decision #57). **Steps 1.5.1 (thin k3d Terraform), 1.5.2 (Helm deploy on k3d), and 1.5.3 (CI) are all reset to NOT STARTED** — they will be rebuilt from scratch under the Azure method. **`infra/terraform/` and `infra/helm/` were deleted this session** (Decision #59) so M1.5 restarts with no infra code; `infra/dev-ca/` (M1 Phase 1.1) is kept. `.github/workflows/ci.yml` (originated in M1 Phase 1.1, only extended in 1.5.3) and `tests/` (provider-agnostic §11 work) are **retained pending a user decision** — deleting either would regress M1. Provider-agnostic Decisions #39–41 / #45–46 (in-cluster Postgres/Redis, staging/production namespaces, sealed-secrets, ghcr.io, Terraform Cloud remote state) still stand — no Azure Container Registry, no Key Vault, no Azure Storage backend (each would burn student credit for no benefit). Two Azure sub-choices remain OPEN (Open Questions #3): Terraform-owns-cluster vs thin, and node VM size. A later 2026-07-24 session then executed Step 1.5.1 on Azure: `az login`, resource-provider registration, `terraform init` (HCP Terraform remote state), `plan`, and `apply` — a **real Azure resource group + Free-tier AKS cluster + single `Standard_B2s_v2` node + staging/production namespaces + quotas + sealed-secrets now exist and were verified live**, then the node was `az aks stop`'d (compute billing halted; control plane $0). Decisions #60–#62 record the sub-gate resolution and two build fixes (node-RG-name length; `Standard_B2s`→`Standard_B2s_v2` forced availability substitution). |
| Last updated | 2026-07-28 |
| Approval gate | Phases 1.0–1.10 approved 2026-07-22. **Milestone 1 (Reliable Worker Network) is complete.** Phase 1.10's "100 workers" figures were substituted with 50 by explicit user direction (Decisions Log #35), consistent with the same substitution already accepted for Phase 1.9 — both explicitly approved regardless. **Phase 1.5.0 (design gate) went through three rounds this session, all approved by the user in sequence 2026-07-22**: (1) OCI-based plan (Decisions Log #36–41), approved, then (2) rejected once the user learned every cloud requires a card on file even for free tiers — revised to a self-hosted plan (Decisions Log #42–46: k3d on the laptop, Cloudflare Tunnel/DNS, ghcr.io, Terraform Cloud), approved, then (3) **reverted back to OCI** once the user explicitly chose to accept the card requirement after all (Decisions Log #47–49, superseding #42–44 again). OCI + OKE was the 2026-07-22 state. **On 2026-07-23 the design gate was re-opened and re-decided again** (Decisions Log #50–#53): after discovering the user's cloud access is blocked only by a company **network MAC-filter on Google** (not an account restriction), the plan settled on **local Kubernetes via k3d + Cloudflare Tunnel for reachability, no paid cloud provider** — the current authoritative state (see Snapshot's Phase status, Decisions #52–#53, Open Questions #2). Decisions #39–41/#45–46 (self-hosted Postgres/Redis, staging/production namespaces, sealed-secrets, ghcr.io, Terraform Cloud remote state) stood unchanged through every round. CI has not run on the latest commit — not yet pushed to `origin`, pushing was not requested. **On 2026-07-24 the design gate was re-opened and re-decided a fourth time: compute host → Microsoft Azure / AKS (Decision #57), superseding the local-k3d + Cloudflare-Tunnel plan (#52–#53). Azure for Students account; docs updated this session; awaiting user go-ahead before any Azure provisioning.** **Step 1.5.1 was subsequently built, applied, verified live, demoed personally, and APPROVED DONE 2026-07-24 (Decisions #60–#62); committed `5b35db1`.** |

---

## Milestone Progress

| Milestone | Title | Status | Demo done | Failure demo done | Internet tested | Fresh clone verified |
|---|---|---|---|---|---|---|
| M1 | Reliable Worker Network | DONE | Yes | Yes | n/a | Yes |
| M1.5 | Infrastructure & Deployment | **DONE** | Yes (prior-phase evidence, user call) | Yes (prior-phase evidence + live DB-outage test) | Yes (4 of 5 machine types) | Partial — see 1.5.9 |
| M2 | Task Distribution | IN PROGRESS (2.0 + 2.1 DONE, both approved) | No | No | No | No |
| M3 | Fault Tolerance | BLOCKED (M2) | No | No | No | No |
| M4 | Adaptive Scheduling | BLOCKED (M3) | No | No | No | No |

Status values: `NOT STARTED` · `IN PROGRESS` · `AWAITING APPROVAL` · `DONE` · `BLOCKED`

---

## Phase Register

### Milestone 1 — Reliable Worker Network
| Phase | Title | Status |
|---|---|---|
| 1.0 | Design gate — protocol and identity decisions | DONE |
| 1.1 | Repository, environment, CI skeleton | DONE |
| 1.2 | Coordinator service skeleton and data stores | DONE |
| 1.3 | Worker registration and identity | DONE |
| 1.4 | Authentication and token lifecycle | DONE |
| 1.5 | Persistent connection transport | DONE |
| 1.6 | Heartbeat and liveness detection | DONE |
| 1.7 | Reconnection and session conflict | DONE |
| 1.8 | Dashboard v1 — live worker view | DONE |
| 1.9 | Scale simulation 1 → 100 workers | DONE |
| 1.10 | M1 demo and fresh-clone verification | DONE |

### Milestone 1.5 — Infrastructure & Deployment
| Phase | Title | Status |
|---|---|---|
| 1.5.0 | Design gate — cloud topology and cost | DONE (re-decided 2026-07-24 → **Microsoft Azure / AKS**, Decision #57, supersedes k3d #52–#53) |
| 1.5.1 | Terraform base infrastructure | **DONE** — rebuilt on Azure with the `azurerm` provider (Decisions #60–#62). `terraform apply` created resource group + Free-tier AKS + single `Standard_B2s_v2` node + staging/production namespaces + per-namespace quotas + sealed-secrets; verified live via `kubectl`, personal demo performed (`az aks start` → cluster Running, all resources Active/Running), **approved DONE 2026-07-24**. Node `az aks stop`'d after. Terraform Cloud remote state (#46) retained; locking observed real. (Fresh-clone `terraform apply` verify not separately performed — waived by explicit user approval 2026-07-24, same discretion as the Phase 1.9/1.10 substitutions.) |
| 1.5.2 | Kubernetes manifests and Helm packaging | **DONE** — Helm chart (`infra/helm/platform/`) rebuilt for AKS and **verified live on the `staging` namespace 2026-07-24**, **APPROVED DONE**. All 7 exit criteria demonstrated: Helm deploy (coordinator+dashboard+in-cluster Postgres/Redis, 5 pods Running); readiness/liveness probes; **laptop worker connected** (registered + `ws_connected`, TLS SAN valid); **migrations ran in the initContainer before serve** (`alembic_version=0001`); **coordinator pod delete → auto-reschedule + worker self-reconnect** (`session_epoch 1→2`, same `worker_id`); **rolling upgrade with ≥1 replica serving throughout, no permanent worker loss** (`epoch→3`); resource limits on every container. Images built + pushed to public ghcr.io tagged by SHA `999772d`. Chart fixes found+made during verify: HPA owns replicas (#63), replace-in-place rollout + CPU limit 300m for the tight quota (#64), public packages (#65). Node `az aks stop`'d after (compute billing halted). **Known limitations (all environment/cost, not design):** reach is `kubectl port-forward` (same laptop only) — real public endpoint for different-Internet workers is Step 1.5.5; single-node so replicas prove reschedule/rollout mechanics not multi-node HA — a `node_count` dial for later. |
| 1.5.3 | GitHub Actions CI pipeline | **DONE** — rebuilt for the Azure method and **verified green on `main`** (run `30085974120`), **APPROVED DONE 2026-07-24** (Decision #66). All 6 exit criteria objectively verified: full pipeline auto-runs on every PR (push + pull_request); **broken test blocks merge** (proof PR #3 `test` red: `AssertionError`); **vulnerable dep flagged** (proof PR #3 `scan` red: `requests 2.19.1 PYSEC-2026-1872/2275`, exit 1); **azurerm `terraform plan` appears in the PR checks summary** (`Plan: 0 add, 1 change, 0 destroy`); duration ~2 min wall (parallel jobs 20–48s); **images SHA-tagged and pushed to ghcr.io on merge to main** (`…-{coordinator,dashboard,worker}:4969097…`, never only `:latest`). Branch protection on `main` requires all 7 check contexts (`strict:true`). PR #2 (12 commits, all M1.5.1–1.5.3 work) merged to main. **Known limitation (documented, not a defect):** CI plans the **azurerm layer only** (`-target`) — `az aks stop` deallocates the whole control plane so the kubernetes/helm providers can't refresh from CI; their plan is a local, cluster-up task. No Azure compute started by CI (plan is read-only; node stays stopped, $0). Next phase: 1.5.4 (CD pipeline). |
| 1.5.4 | GitHub Actions CD pipeline | **DONE** — Option C CD (Decision #67) **verified LIVE on AKS and APPROVED DONE 2026-07-24** (Decisions #67–#68). Hosted-runner CD over the **public AKS API**, no CD-driven `az aks start/stop` (node up/down = user's manual credit lever). `cd.yml` triggers on **CI success on `main`** (`workflow_run`, avoids the image-push race) → deploys `workflow_run.head_sha`; reusable `_deploy-env.yml`: az login (reuse 1.5.3 ARM_* secrets) → cluster-up guard → `helm upgrade --install platform --atomic` → smoke (**`kubectl exec` into coordinator + python `/ready`+`/health`**, Decision #68) + version assert → rollback-on-failure → timed record. staging auto; production behind the `production` Environment required-reviewer gate. Coordinator `/health` returns `version` from a Helm-injected `GIT_SHA` env. **All 5 exit criteria objectively verified live:** (1) merging PR #5 auto-deployed staging on SHA `0df7e206`; (2) production gate **held** and deployed only after the user's approval; (3) auto-rollback proven — the first run's smoke false-failure (quota, see #68) tripped `helm rollback` cleanly; (4) staging **and** production `/health` both returned `"version":"0df7e206…"` = deployed SHA; (5) commit→staging timed at **82s** in the run summary. Both env stacks ran on the single `B2s_v2` node at 19% CPU / 32% mem. Shipped as **two PRs**: **#4** (build) + **#5** (smoke-exec fix, Decision #68). Node `az aks stop`'d after. **Reach ceiling (not a defect):** deploy verified in-cluster; a worker on a *different* Internet connection still needs the public ingress from Step 1.5.5. |
| 1.5.5 | Public ingress, TLS, DNS | **DONE — built + verified live on AKS staging, all 6 exit criteria objectively met, APPROVED by user 2026-07-27. Final SHA `69028dee1613923daed0c5da2fe4970ffd586e1f`.** Design gate (Decision #69): ingress-nginx + cert-manager (Let's Encrypt HTTP-01) + a Terraform-owned Standard static public IP in the node RG with a free `*.cloudapp.azure.com` FQDN; single host `dcds-staging.centralindia.cloudapp.azure.com` path-routed (`/workers`,`/ws`,`/health`,`/ready`→coordinator; `/`,`/api`→dashboard, no rewrite — worker only hits `/workers`+`/ws` so no collision); backend-protocol HTTPS re-encrypt to the self-signed pods (TLS-everywhere §3.8 kept); ws proxy timeouts 3600s; edge `limit-rps=5` on coordinator; dashboard `auth-basic`. **Staging exposed only; production ingress templated but `enabled:false`** (needs a 2nd IP + node headroom — honest ceiling, same family as single-node HA). `terraform apply` created the IP (`4.240.120.113`)+ingress-nginx+cert-manager (Decision #70; first apply's helm `--wait` timed out because both controllers were `Pending` on a CPU-full node — freed by scaling production down + demo-worker 5→1, then re-applied clean). **Verified live:** (1) ✅ coordinator `https://…/health`=200 returning the deployed SHA `0df7e206`, **valid public Let's Encrypt cert** (issuer `Let's Encrypt CN=YR1`, no `-k`); (2) ✅ dashboard 401 anonymous / 200 with basic-auth; (4) ✅ **cert renewal proven not assumed** — deleted the `platform-tls` secret, cert-manager auto-reissued (notBefore `12:41:06`→`12:46:14`); (5) ✅ **edge rate-limit blocks a register flood** — 15 sequential POST `/workers/register`: first 5 reach the app (401), requests 6–15 get `429` at nginx; (3) ✅ a **real external worker** (published image `…-worker:69028dee…`, `WORKER_CA_FILE=""` → system trust of the public cert) `registered`→`ws_connected` through the ingress and stayed ONLINE via `/api/workers` — **soak PASSED 2026-07-27**: `session_epoch` held at `1` (no reconnect) across the full soak, uptime 176.7s, fresh heartbeat, 77.7ms latency. **Soak duration reduced 60-min→2-min by explicit user direction 2026-07-27** (same user-discretion substitution family as the Phase 1.9/1.10 50-worker calls, Decisions #34–35); recorded honestly as a user scope call, not a discovered result. (6) ✅ **second-network worker CONFIRMED by user 2026-07-27** — ran the worker on a separate PC on a different network and it connected successfully through the public ingress. Worker system-CA fix committed to `worker/worker.py` (`_ssl_context()`). **Update (end of 2026-07-24): PR #7 merged to main; CI published images at SHA `69028dee1613923daed0c5da2fe4970ffd586e1f` (worker image at this SHA carries the CA fix); CD auto-deployed staging to it (`/health` returns `69028dee`). Criterion-3 soak restarted on the published image at `14:05:33Z` (verify at ≥`15:05:33Z`). Criterion-6 network path PROVEN — a worker on a separate PC/network reached the coordinator and got a 401 (a mistyped enrollment secret, not a network failure), so DNS + public LE cert + ingress route all work off-network; awaiting a clean `registered`+`ws_connected` on a hotspot and a corporate network. Production scaled down (coordinator HPA min 2→1, dashboard/redis/postgres→0) + demo-worker 5→1 to fit the ingress controllers on the single node — reversible via next CD/helm.** |
| 1.5.6 | Observability stack | **DONE — all 6 exit criteria verified live on AKS staging, APPROVED by user 2026-07-27. Final SHA `b1963f90d53066a836248e11be231286893670aa`.** Stack (Prometheus/Grafana/Alertmanager/Loki/Alloy) built + applied live (Decisions #71–#73); the formal 6-criteria walkthrough (Decision #74) surfaced two real gaps + the noise item, all fixed in **PR #9** (merged; CI green incl. tests; CD deployed staging on `b1963f90`). **Verified live:** (C1) ✅ Grafana fleet dashboard shows live state (workers 0→2, replicas up, db/redis up, req-rate + p95 latency; screenshot); (C2) ✅ **worker session traceable by ONE correlation id across BOTH replicas** — session `beed3ee8`: HTTP `access_token_issued` on `q4skv`, WS `worker_connected`+heartbeats on `jgbvf` + the worker's own logs (fix: worker mints a per-session id, sends `X-Correlation-ID` on token-refresh + in hello/heartbeat/pong; coordinator binds `hello.correlation_id` to the WS coroutine contextvar — heartbeats used to log `correlation_id="-"`); (C3) ✅ **killing the coordinator fires `CoordinatorDown` to Discord** — scaled staging coordinator→0 → `absent(up)=1` → firing at ~90s → Alertmanager `receiver=chat`, no notify errors → resolved on recovery (fix: expr now `absent(up{...})==1 or max(up{...})==0`, since an all-gone series is empty not 0); (C4) ✅ auth-failure spike fires — 20-event register flood → LogQL count 20 → Grafana alert firing (screenshot); (C5) ✅ Loki `retention_period: 72h` live + documented (`var.loki_retention_hours=72`); (C6) ✅ no credential/token in Loki (enrollment secret, dashboard/grafana passwords, Bearer tokens, Discord webhook all absent). **#7 (quiet built-in alert noise) — CODE MERGED, NOT APPLIED LIVE:** every platform alert labelled `team: dcds`, Alertmanager default receiver → `null`, only `team=dcds` → chat (in `observability.tf` + `prometheusrules.yaml`); the `terraform apply` to push it was killed mid-session, so the route change is committed but not live (a loose end, NOT an exit criterion). Targeted helper: `infra/apply-alertmanager-route.ps1` (untracked). Screenshots: `c1-fleet-dashboard.png`, `c2-correlation-across-replicas.png`, `c4-authspike-firing.png` (untracked in repo root). Node still running at approval (user to `az aks stop`). |
| 1.5.7 | Coordinator horizontal scaling proof | **DONE — all 6 exit criteria objectively met live on AKS staging, APPROVED by user 2026-07-27.** Design gate = Decision #75 (option A2: raise staging namespace quota via Terraform to keep the 300m coordinator CPU limit, + option B1: synthetic in-cluster HTTP load for the autoscale criterion — no M2 task pipeline exists yet, honest ceiling §10). Changes: `namespace_quota` limits.cpu 2→3 / limits.memory 2Gi→3Gi / requests 1→1.5 / 1Gi→1.5Gi (Terraform apply, in-place, both namespaces — prod scaled down so unused); staging coordinator HPA min 2→3 / max 2→5 (`values-staging.yaml`, deployed via `helm upgrade` on SHA `b1963f90`); new versioned load manifest `infra/loadtest-coordinator.yaml`. **Zero application-code changes** — the phase proves invariant §3.9, already built (Redis connection registry #19, epoch pub/sub eviction #29, DB/Redis-backed read path #30). **Verified live:** (1) ✅ 3 replicas serve one fleet simultaneously (spread across both nodes); (2) ✅ worker distribution verified not assumed — with 6 workers reconnected, each of the 3 replicas served distinct `worker_id`s (per-pod `kubectl logs` `worker_connected`); (3) ✅ killed one replica (`fqjnv`, 4 workers) → orphaned workers reconnected to survivors via Phase-1.7 backoff, replacement pod rescheduled (Deployment min=3), fleet `connected` recovered to 6 in ~30s on the DB-backed `/workers` read path (= dashboard's one read path, #30); (4) ✅ **autoscale both directions** — synthetic load Job (`/ready` flood, in-cluster so the edge `limit-rps` doesn't throttle) drove HPA to **191%/70% → scaled 3→5**; on job completion CPU fell to 3%, HPA held 5 through its ~5-min scaleDown stabilization window, then **scaled 5→3** at 15:16:51; (5) ✅ one coherent fleet regardless of replica count — `/workers` queried from **every** replica returned identical `total=37 connected=6 online=6` (37 = cumulative Postgres registrations, 6 = live workers); (6) ✅ no coordinator holds authoritative state — killed all three original replicas in turn (`fqjnv`,`8klwk`,`bclq7`); after every kill the fleet stayed `total=37 connected=6 online=6` (all state in Postgres/Redis, §3.9). **Honest ceilings:** load is synthetic HTTP, not real task load (M2); autoscale ceiling 5 is bounded by the raised quota + 2-node pool, not tested beyond. Config changes uncommitted (no commit requested). Node left running (user to `az aks stop`). |
| 1.5.8 | Real Internet worker onboarding | **DONE — CPU/mem gap closed via `psutil`; APPROVED by user 2026-07-27. The remaining machine-type criteria (VPS-in-another-country, mobile hotspot, and the simultaneous-multi-worker "all on one dashboard with region-latency deltas" capture) were WAIVED by explicit user direction (Decision #77) — not verified, recorded honestly as a user scope call, same discretion family as #34–35.** Design sub-gate decided (Decision #76): non-Docker distribution = **bootstrap script** (A1), enrollment = **keep shared `ENROLLMENT_SECRET`** (B1). Built offline (node stopped): (a) worker single-instance lock made **cross-platform** (`fcntl` POSIX / `msvcrt` Windows) so the bare-Python worker runs on machines without Docker — the `import fcntl` was the one hard blocker for native Windows; CPU/mem `/proc` reads already degrade to `None` off-Linux; verified by `tests/test_worker_lock.py` (passes on this Windows host, exercising the new msvcrt branch); (b) `worker/install-worker.sh` + `worker/install-worker.ps1` bootstrap installers (clone → venv → run, config = 3 values); (c) `docs/onboarding-a-worker.md` — Docker/Linux/Windows onboarding + operator issue/revoke/rotate procedure. The 3 portability fixes + installers + docs were **committed and shipped through the pipeline** (PR #11 merged to main, CI green, CD deployed staging+production on SHA `42d8c4c`) so the non-Docker installer works from a clone and the worker image is published at that SHA. **Verified live 2026-07-27:** native Windows/no-Docker worker (#3) and **friend's PC installed from docs only on their own ISP (#4)** both `registered`+`ws_connected` through the public ingress (see Real Internet Workers Verified table). **CPU/mem gap CLOSED (Decision #77):** `worker/worker.py` now reads CPU/memory via `psutil` (cross-platform Linux/Windows/macOS), replacing the Linux-only `/proc/stat`+`/proc/meminfo` readers that returned `None` on native Windows/macOS and left the §6 dashboard columns blank. Verified on this native Windows host — `_read_cpu_percent()` returns real values (e.g. 44.4 / 37.5), `_read_memory_percent()` 65.4; worker test suite green (5 passed / 1 skipped); `psutil>=5.9,<7` added to `worker/requirements.txt` and shipped through the pipeline (**PR #12**) so the bootstrap installer + published image carry it. **Machine types verified: 4 of 5** — laptop Docker + 2nd PC (1.5.5, #1/#2), native Windows no-Docker (#3), friend's PC from docs on own ISP (#4). **NOT done and WAIVED by the user (Decision #77):** VPS-in-another-country + mobile-hotspot connections, and the "all workers on one dashboard with region-latency deltas" capture — the user directed 1.5.8 be closed after the CPU/mem fix and explicitly not to pursue the rest. Recorded as a user scope call, not a verified result (§10). |
| 1.5.9 | M1.5 demo and verification | **DONE — SIGNED OFF by user 2026-07-28.** Closed on user direction rather than a fresh end-to-end demo run; recorded honestly per §10 as a user scope call, same discretion family as Decisions #34–35 and #77. Final SHA `4575097157debe48d71b9f3aff86248c599d5b1e` (PR #13 merge), deployed by CD to **both** staging and production — `/health` returned that exact SHA in each. **Exit criteria, honestly:** (1) full demo — **NOT re-run**; user directed it be closed on the evidence already gathered in 1.5.1–1.5.8 (commit→CI→staging deploy timed at 82s in 1.5.4; rolling upgrade with no permanent worker loss in 1.5.2; replica scale up/down with a coherent fleet in 1.5.7; public dashboard + multi-network workers in 1.5.5/1.5.8). (2) failure demo — **partly re-run and newly proven this session**: killing a coordinator pod (1.5.7), auto-rollback on a bad deploy (1.5.4), worker disconnect/reconnect (1.7/1.10), credential revoke → cannot return (1.10, Docker Compose only, never re-proven on AKS), and — **new, previously never tested at any point** — **database offline**: scaled staging `postgres` to 0, all 3 coordinators went `0/1` NotReady with **0 restarts** (liveness correctly independent of the DB), public `/ready` 503, `DatabaseDown` fired at T+3m41s and reached Alertmanager `receiver=chat` (Discord), then full recovery **38s** after restore with 45 worker rows and `alembic_version=0001` intact. Note the `max(...)==0` expr fires only because Prometheus still scrapes NotReady pods — it works today but lacks the `absent()` guard `CoordinatorDown` got in 1.5.6 C3. Also learned: while all coordinators are unready the ingress has no upstream, so the **entire public surface including the dashboard returns 503**. (3) five machine types — **4 of 5**; VPS-in-another-country and mobile hotspot **WAIVED** (Decision #77). (4) staging reproducible from Terraform — **improved but never proven from nothing**: the secrets gap is closed (nine SealedSecrets committed, validated 9/9 from a fresh clone), `terraform plan` is now a trustworthy signal (4 phantom changes → 1 real), and the clone→AKS sequence is documented; a true from-destroyed rebuild was never attempted (would cost credit and destroy the environment). (5) runbook — **DONE**, `docs/runbook.md`. (6) cost tracked against estimate — **NOT DONE**, no cost figures were ever recorded. (7) `PHASE_STATE.md` updated — this entry. (8) approval before Phase 2 — given 2026-07-28. |

> **Milestone 1.5 direction (current, 2026-07-24): Microsoft Azure —
> AKS (Azure Kubernetes Service), Free-tier control plane** (Decisions
> Log #57, supersedes local k3d + Cloudflare Tunnel #52–#53). The system
> deploys to a **managed AKS cluster** on an **Azure for Students**
> subscription ($100 credit / 12 months / no card). Because student
> credit is finite, cost discipline is mandatory and explicit: AKS
> **Free-tier control plane ($0)**, a **single small B-series burstable
> node with no autoscaling**, **`az aks stop` whenever a test session
> ends** (deallocates node VMs → compute billing halts), and
> `az group delete` to remove everything. A real managed cloud has a real
> public LoadBalancer/IP, so **Cloudflare Tunnel is retired** (same
> reasoning as the earlier Decision #49). M1.5's Kubernetes/Helm/CI-CD
> content is fully real again — "local cluster" becomes "managed AKS
> cluster", and reachability is a real Azure public endpoint (spun up
> only while demoing to conserve credit). The k3d-era `infra/terraform/`
> (thin, `kubernetes`/`helm` only) is rewritten at Step 1.5.1 to add the
> **`azurerm` provider** (resource group + AKS + node pool); its short
> design sub-gate settles two open sub-choices (Open Questions #3):
> whether Terraform provisions the cluster itself vs `az aks create` +
> thin Terraform, and the node VM size. Provider-agnostic Decisions
> #39–41 / #45–46 (self-hosted Postgres/Redis, staging/production
> namespaces, sealed-secrets, ghcr.io, Terraform Cloud remote state)
> still stand — no ACR, no Key Vault, no Azure Storage backend. **All
> prior M1.5 build work (done on k3d) is scrapped (Decision #58);
> Steps 1.5.1–1.5.9 are NOT STARTED and rebuilt from scratch on Azure.**

### Milestone 2 — Task Distribution
| Phase | Title | Status |
|---|---|---|
| 2.0 | Design gate — queue and assignment model | **DONE — approved by user 2026-07-28 (Decisions #79–#82).** (A) Queue = **PostgreSQL `FOR UPDATE SKIP LOCKED`** on the task table, not Redis — Redis runs with no persistence by Decision #39 and nothing rebuilds a lost queue, and even with AOF added the Redis/Postgres dual-write breaks §3.7. (B) Assignment = **hybrid**: worker advertises `max_concurrent` credits, coordinator pushes over the existing 1.5 pub/sub path — keeps assignment coordinator-authoritative for M4 (§3.3) while borrowing pull's backpressure. (C) Results = separate `task_results` table, recommended 64 KB cap / 7-day body retention. (D) Type extensibility = coordinator-side type registry; **envelope unchanged, `PROTOCOL_VERSION` stays `1.0`**, new `message_type` values only (Decision #6's stated mechanism). All three exit criteria met. **Consequence:** Step 2.2 renamed "Redis-backed queue" → "Durable task queue" in both this register and `docs/phase-2-task-distribution.md`; the old title pre-judged a choice Step 2.0 explicitly left open, and was flagged to the user as a §16 escalation before approval. **Unmeasured (§10):** every throughput/latency claim behind (A) is reasoning, not measurement, and must be verified by 2.2's 10,000-task criterion and the 2.8 harness. |
| 2.1 | Task model, schema, state machine | **DONE — APPROVED by user 2026-07-28.** Approval covered the two judgement calls raised at review: (a) `task_results` created in the 2.1 migration rather than 2.5's, so 2.5 needs no schema change; (b) `priority` is lower-is-more-urgent (Unix nice), so one ascending index serves the whole `ORDER BY`. **Approval is of the step, not of a shipped artifact — at the time of approval the work was still uncommitted, had never run in CI, and was never deployed. It must go through a branch off `main` → PR → CI → CD like every step since 1.5.3.** All 5 exit criteria objectively verified; nothing asserted. New: `coordinator/app/task_states.py` (state machine), `coordinator/app/task_types.py` (four dummy types + parameter registry), `Task`/`TaskResult` in `coordinator/app/models.py`, migration `0002_create_tasks_tables.py`, `tests/test_task_states.py` + `tests/test_task_types.py`, root `conftest.py`. **(1) Migration applies cleanly and is reversible — MEASURED**, not asserted: against an ephemeral `postgres:16-alpine`, `alembic upgrade head` ran `-> 0001 -> 0002` clean on an empty DB (`alembic current` = `0002 (head)`, tables `tasks`+`task_results` present with all 15/4 spec columns), then `downgrade 0001` dropped both tables leaving `workers` intact and **0 orphan indexes**, then `upgrade head` re-applied clean. **(2) Invalid transitions rejected in code — 30 tests**, incl. skip-ahead (`QUEUED→RUNNING`, `ASSIGNED→COMPLETED`), backwards moves, terminal states never moving on, and worker-lifecycle states (`ONLINE`/`OFFLINE`/…) rejected as unknown so the two state machines cannot share a vocabulary through the shared free-text column. Same-state is a **no-op returning False, not an error** — that is what makes duplicate result submission harmless (§3.7). **(3) Four task types validate and reject malformed input — 42 tests**: missing/negative/over-ceiling/wrong-type values, unoffered `algorithm`, non-base64 payload, over-cap payload, unknown type, and **unknown keys rejected not ignored** (`extra="forbid"`, §12). **(4) `lease_expires_at` + `attempt_count` exist, written by nothing in M2** — confirmed present in the live schema dump. **(5) `correlation_id` NOT NULL** — confirmed `null=NO` in the live schema. **Beyond the criteria, Decision #79's index design was smoke-checked and holds:** with 20,000 rows (18,000 `QUEUED`), the real dequeue (`WHERE status='QUEUED' ORDER BY priority, created_at LIMIT 10 FOR UPDATE SKIP LOCKED`) plans as `Index Scan using ix_tasks_queue` with **no sort node** — the index supplies the ordering — at 0.15ms execution. **Labelled honestly (§10): that is a smoke check on a laptop, not Step 2.2's 10,000-task criterion and not the 2.8 load harness.** Full suite **77 passed / 1 skipped** (integration skipped, needs `POSTGRES_HOST`); `ruff check` clean on the CI paths. **Two judgement calls to confirm at approval:** (a) `task_results` is created in *this* migration rather than 2.5's, so 2.5 needs no schema change — same reasoning as the reserved Phase 3 columns; (b) `priority` is **lower-is-more-urgent** (Unix nice), so one ascending index serves the whole `ORDER BY`. Also: root `conftest.py` added because coordinator modules import as `app.*` via CI's `PYTHONPATH` and a bare local `pytest` could not resolve them — local and CI runs now agree. `pydantic>=2,<3` pinned explicitly in `coordinator/requirements.txt` (was transitive via fastapi; validation now depends on v2 semantics directly). **Not verified: nothing has run in CI or on AKS — no commit, no PR, no deploy this session.** |
| 2.2 | Durable task queue (Postgres, Decision #79 — was titled "Redis-backed queue" before the 2.0 gate) | NOT STARTED |
| 2.3 | Assignment engine | NOT STARTED |
| 2.4 | Worker execution runtime | NOT STARTED |
| 2.5 | Result submission and completion | NOT STARTED |
| 2.6 | Operator task APIs | NOT STARTED |
| 2.7 | Dashboard v2 — task lifecycle views | NOT STARTED |
| 2.8 | Load testing harness | NOT STARTED |
| 2.9 | M2 demo and verification | NOT STARTED |

### Milestone 3 — Fault Tolerance
| Phase | Title | Status |
|---|---|---|
| 3.0 | Design gate — failure taxonomy and guarantees | NOT STARTED |
| 3.1 | Lease and timeout engine | NOT STARTED |
| 3.2 | Reassignment and retry | NOT STARTED |
| 3.3 | Idempotency and duplicate suppression | NOT STARTED |
| 3.4 | Stale result fencing | NOT STARTED |
| 3.5 | Coordinator restart and recovery | NOT STARTED |
| 3.6 | Partial completion policy | NOT STARTED |
| 3.7 | Dashboard v3 — failure and recovery views | NOT STARTED |
| 3.8 | Chaos testing harness | NOT STARTED |
| 3.9 | M3 demo and verification | NOT STARTED |

### Milestone 4 — Adaptive Scheduling
| Phase | Title | Status |
|---|---|---|
| 4.0 | Design gate — capability trust model | NOT STARTED |
| 4.1 | Capability reporting | NOT STARTED |
| 4.2 | Telemetry pipeline and decay | NOT STARTED |
| 4.3 | Scoring engine | NOT STARTED |
| 4.4 | Rule-based scheduler | NOT STARTED |
| 4.5 | Scheduler explainability | NOT STARTED |
| 4.6 | Dashboard v4 — scheduling visualization | NOT STARTED |
| 4.7 | Heterogeneous benchmark harness | NOT STARTED |
| 4.8 | M4 demo and Version 1 sign-off | NOT STARTED |

---

## Environments

| Environment | Purpose | Status | URL |
|---|---|---|---|
| local | Docker Compose development | NOT PROVISIONED | — |
| staging | Azure AKS namespace, public Internet testing (Decision #57) | **DEPLOYED via CD to SHA `4575097` (PR #13 merge, 2026-07-28) — `/health` returned that SHA; cluster `az aks stop`'d after. Secrets now supplied by the committed SealedSecrets (Decision #78), not by `bootstrap-secrets.ps1`.** Previously: `94ba53e` (PR #10 merge, 2026-07-27) — `/health` version=`94ba53e`, `/ready` db+redis ok, **coordinator HPA min 3/max 5 now canonical (applied by CD from `main`, 3 pods Running)**, namespace quota raised (limits.cpu=3) — Step 1.5.7 (Decision #75), all 6 criteria verified live. Also carries observability + 1.5.6 fixes (Decision #74). Publicly reachable via ingress (Step 1.5.5); node `az aks stop`'d between sessions (deployments + PVCs intact) | https://dcds-staging.centralindia.cloudapp.azure.com (IP 4.240.120.113) |
| production | Azure AKS namespace, real fleet (Decision #57) | **DEPLOYED via CD to SHA `4575097` on 2026-07-28 — verified in-cluster (`/health` returned that SHA, `/ready` db+redis ok, 2 coordinators Running); rotated secrets applied here too. Cluster stopped after.** Previously: `94ba53e` on 2026-07-27 (was `69028dee`) — user approved the `production` Environment gate; verified live in-cluster (`/health` version=`94ba53e`, `/ready` db+redis ok, 2 coordinators Running, HPA min=2 unchanged — 1.5.7 was staging-only). node stopped between sessions | AKS `data-cleaning-distributed-system` |

---

## Real Internet Workers Verified

| # | Machine type | Network | Phase verified | Notes |
|---|---|---|---|---|
| 1 | Laptop (Docker, published worker image) | Local Internet via public ingress | 1.5.5 | Soak: `registered`+`ws_connected` through `https://dcds-staging.centralindia.cloudapp.azure.com`, `session_epoch` held at 1, ONLINE. 2026-07-27. |
| 2 | Second PC (Docker) | Different network | 1.5.5 | User-run, connected successfully through the public ingress. Confirmed 2026-07-27. |
| 3 | Windows laptop, **native / no Docker** (bootstrap venv path, system-CA trust) | Local Internet via public ingress | 1.5.8 | Ran the fixed `worker.py` (cross-platform lock + CA-fallback + signal-guard) in a venv against `https://dcds-staging.../`. Sequence `registered`→`access_token_refreshed`→`ws_connected` (epoch 1), server-side `status=ONLINE` on `/workers`. cpu/mem report `None` (non-Linux, no `/proc`) — expected. 2026-07-27. |
| 4 | **Friend's PC**, native Windows / no Docker, **installed by them from `docs/onboarding-a-worker.md` (the `install-worker.ps1` one-liner) unaided** | Friend's own ISP / different network | 1.5.8 | User's friend ran the documented PowerShell installer (`irm ... install-worker.ps1` → clone → venv → run). Sequence `registered`→`access_token_refreshed`→`ws_connected` (epoch 1), `worker_id 8b735695…`, agent `0.1.0`; coordinator recorded it. Session ended by Ctrl+C (shows OFFLINE after) — connection itself succeeded. Satisfies "friend's computer, own ISP, from docs only." 2026-07-27. |

Target machine types: second laptop, desktop, VPS, friend's computer,
home Wi-Fi, mobile hotspot.

---

## Decisions Log

Append only. Never rewrite an entry.

| # | Date | Decision | Alternatives | Rationale | Phase |
|---|---|---|---|---|---|
| 1 | 2026-07-22 | Transport: **WebSocket (wss) over TLS as primary**, with **HTTP long-polling as the documented fallback** for networks that block the WS upgrade | gRPC bidirectional streaming; HTTP long-polling as primary | WS runs on 443 like ordinary HTTPS, so it survives the class of uncontrolled networks this project targets (home Wi-Fi, mobile hotspot, corporate proxy) better than gRPC — gRPC rides HTTP/2, and HTTP/2 bidirectional streams are a documented failure point behind TLS-terminating and older L7 proxies. gRPC's typed-contract/multiplexing advantages don't outweigh that traversal risk for this deployment target. Long-polling alone is rejected as primary because it can't give the coordinator low-latency on-demand push (Step 1.5 requires pushing to a specific worker on demand) and holds thousands of long-lived HTTP requests open across proxies with inconsistent read-timeout behavior. It remains the fallback because plain HTTP is the most universally proxyable protocol there is. | 1.0 |
| 2 | 2026-07-22 | Identity: **coordinator-assigned UUIDv4**, issued at registration, persisted by the worker to a local identity file surviving restart/container recreation/reinstall; deleting that file forces re-registration under a new ID | Self-generated worker ID presented at registration | CLAUDE.md §12 requires that anything the coordinator trusts heavily be coordinator-observed, not worker-reported. A self-generated ID is worker-reported and lets a malicious or buggy worker claim or collide with an existing ID. Coordinator-assigned identity removes that attack surface entirely and matches the Step 1.3 exit criteria (same ID across restart/recreate, new ID after deleting stored identity). | 1.0 |
| 3 | 2026-07-22 | Coordinator language/runtime: **Python 3, FastAPI, asyncio** | Go; Node.js/TypeScript | Matches the stack already committed to in `PRD/Project Architecture Specification (Version 1).md` (chosen there for the later AI-planning phases, out of scope for V1 but a stated long-term constraint). Asyncio is adequate for this workload — persistent-connection handling is I/O-bound, not CPU-bound, so the GIL is not disqualifying at the target scale (100 workers now, low thousands eventually). Go's goroutine model is a real technical edge for massive concurrent-connection counts, but splitting the coordinator into a second language ecosystem has a real maintenance cost for a project this size and isn't justified until a measured scale test (Step 1.9 / M1.5.7) shows asyncio is the bottleneck. | 1.0 |
| 4 | 2026-07-22 | Worker language/runtime: **Python 3**, same as coordinator | Go static binary; Node.js/TypeScript | The "zero-install binary" advantage of a Go worker is neutralized because Docker is the stated primary distribution mechanism (CLAUDE.md §4 sequencing rule; PRD workers run as Docker containers first) — the container already bundles the runtime. One language across the V1 codebase reduces operational surface area. Revisit only if real Internet onboarding (M1.5.8, non-Docker machines) surfaces friction this doesn't anticipate. | 1.0 |
| 5 | 2026-07-22 | Relational database: **PostgreSQL** | MySQL/MariaDB; SQLite | SQLite is disqualified outright by architectural invariant §3.9 (coordinator horizontally scalable, no instance holds authoritative state in memory) — it's a single-file embedded DB, incompatible with multiple coordinator replicas sharing state. Between Postgres and MySQL, Postgres matches the PRD's stated stack, has stronger native JSON column support useful for the flexible capability/metadata fields M4 will need, and has mature migration tooling (Alembic) that fits the FastAPI/SQLAlchemy ecosystem chosen above. | 1.0 |
| 6 | 2026-07-22 | Message envelope: **JSON**, fixed top-level shape `{protocol_version, message_type, correlation_id, worker_id, session_epoch, timestamp, payload}` | Protobuf; MessagePack | JSON is directly inspectable in the structured logs CLAUDE.md §11 requires — a wire message can be pasted straight into a log line with no decoder, which matters for a project run under a zero-hallucination rule where every claim needs to be independently verifiable. Protobuf and MessagePack are faster/more compact but that only matters once large data payloads exist, which is explicitly out of scope for V1. `message_type` is a closed enum extended by later phases without changing the envelope shape (satisfies the "must not require a protocol change" exit criterion). `session_epoch` is coordinator-incremented per accepted session and is what Step 1.7's session-conflict resolution keys off. `timestamp` is sender-generated but explicitly non-authoritative — Step 1.6 measures liveness latency coordinator-side and does not trust worker clocks. | 1.0 |
| 7 | 2026-07-22 | Local dev TLS: **each service terminates TLS directly** (uvicorn `--ssl-certfile`/`--ssl-keyfile`) against a self-signed dev CA generated by `infra/dev-ca/generate-dev-ca.sh`; no reverse proxy in front of coordinator/dashboard yet | Nginx or Traefik reverse proxy terminating TLS for both services | Only two services need TLS termination at this stage, so a reverse proxy adds a moving part Phase 1.1 doesn't need — matches CLAUDE.md §4's "do not bury Phase 1 in infrastructure before a single worker connects." A reverse proxy/ingress is the right call once there's real public ingress to manage, which is explicitly M1.5.5's job, not this phase's. | 1.1 |
| 8 | 2026-07-22 | Migration tooling: **Alembic**, async engine, `alembic upgrade head` run automatically from the coordinator's FastAPI lifespan on every startup via `asyncio.to_thread` (Alembic's own migration runner isn't async-native) | Hand-rolled SQL migration runner | Alembic already implied by the Postgres/SQLAlchemy choice (Decision #5's own rationale names it); a hand-rolled runner would just re-implement version tracking and idempotency Alembic already does correctly. Running it automatically on startup (rather than as a separate manual step) is what makes "runs from a fresh clone with zero undocumented manual steps" (CLAUDE.md §13) true for the database layer. | 1.2 |
| 9 | 2026-07-22 | Bootstrap enrollment credential: a **single shared secret** (`ENROLLMENT_SECRET` env var), compared with `hmac.compare_digest` | Per-worker pre-provisioned credentials issued out-of-band | Per-worker issuance needs an admin issuance flow that's out of scope for 1.3 (no operator UI/CLI exists yet). A shared bootstrap secret matches CLAUDE.md §12's own phrasing ("bootstrap enrollment credential, then short-lived access token plus long-lived refresh credential") and is identical across Docker/VPS/laptop workers, preserving the "one protocol, all environments" invariant. Revisit if real fleet onboarding (M1.5.8) shows a shared secret doesn't scale operationally. | 1.3 |
| 10 | 2026-07-22 | Worker credential hashing: **HMAC-SHA256 with a server-side pepper** (`CREDENTIAL_PEPPER` env var) over a `secrets.token_urlsafe(32)` random credential | bcrypt/argon2 (password-style slow KDF) | The credential is a 256-bit random token, not a human password — slow KDFs exist to slow brute-forcing of *low-entropy* secrets, which doesn't apply here. A keyed fast hash is standard practice for high-entropy API-key-style credentials, and the pepper means a stolen `credential_hash` column alone (without the separately-stored pepper) can't be matched against a guessed value. | 1.3 |
| 11 | 2026-07-22 | Duplicate cross-machine identity use: a **short-TTL Redis claim** (`worker:{id}:claim`, default 10s — recommendation, not measured) set on every registration/reaffirm call, released early on graceful worker shutdown (SIGTERM); a concurrent claim attempt while the previous one is unexpired is rejected (409) and logged | Full session-conflict eviction (one winner, loser's live connection terminated) | Full eviction needs a live connection to terminate, which doesn't exist until Phase 1.5, and the "one winner" arbitration rule is explicitly Phase 1.7's job. This claim is a deliberately bounded heuristic for 1.3: it catches a copied identity used concurrently within the TTL window and is released on graceful shutdown so normal restarts don't false-positive, but it **cannot** distinguish a genuine second instance from a very fast forced restart (`docker kill` + immediate relaunch) within that window — documented limitation, not silently assumed away. | 1.3 |
| 12 | 2026-07-22 | Registration rate limiting: **fixed-window counter per source IP** in Redis (`register:ratelimit:{ip}`, 60s window, default 5 requests — recommendation, not measured) | Token bucket / leaky bucket | A fixed window is simpler to reason about and sufficient at this scale (no load test has run to justify a smoother algorithm yet); revisit if Phase 1.9's scale simulation shows burst behavior at the window boundary is a real problem. | 1.3 |
| 13 | 2026-07-22 | Phase 1.4 scope: build and verify the **HTTP-only subset** now (token issuance/refresh/verify, hashing, revocation's currently-available enforcement points, replay protection); explicitly defer the connection-drop and dashboard-visible halves of its exit criteria to Phases 1.5/1.8 | Reorder — build Step 1.5 (transport) before 1.4; or stop and defer all of 1.4 | User's explicit choice when the conflict was raised (1.4's exit criteria as written assume a live connection and a dashboard that don't exist until later steps in this same phase). Reordering was the other option on the table but rejected in favor of building the HTTP-testable subset now and coming back for the rest once 1.5/1.8 exist, rather than delaying token/auth work entirely. | 1.4 |
| 14 | 2026-07-22 | Access token model: **opaque random token** (`secrets.token_urlsafe(32)`), coordinator-observed via a Redis record (worker_id, generation, expires_at) keyed by the token's hash, TTL 60s (recommendation, not measured) | Self-contained signed JWT | Revocation must actively invalidate a token before its natural expiry regardless of format, which means the coordinator needs a lookup/denylist either way — a JWT's self-containedness buys nothing here and adds a signing-key dependency this project doesn't otherwise need. An opaque token also matches CLAUDE.md §12's "coordinator-observed, not worker-reported" trust principle directly. | 1.4 |
| 15 | 2026-07-22 | Replay protection: a **per-worker generation counter** (`worker:{id}:token_gen` in Redis) incremented on every refresh; each token is stamped with the generation current at mint time, and `verify_token` rejects any token whose generation doesn't match the worker's current one | Single-use refresh-token rotation on the long-lived `worker_credential` itself | The long-lived credential (Phase 1.3) is deliberately reusable across every reaffirm, not single-use — rotating it would contradict Decision #10/#11. Rotating the *access token's generation* instead gives the same real, demonstrable guarantee (an old token becomes invalid the instant a newer one is minted) without touching the long-lived credential's design. | 1.4 |
| 16 | 2026-07-22 | Revocation bound: no live connection exists yet to actively terminate (that arrives in Phase 1.5), so "bounded time to effect" for now means **instant** on every enforcement point that exists today (register/reaffirm, token refresh both check `revoked`) and **worst-case the access token TTL** for any token already issued before revocation | Wait for Phase 1.5 before implementing revocation at all | Revocation's DB-level effect (the `revoked` flag and its enforcement in existing endpoints) doesn't need a live connection to exist — implementing it now means Phase 1.5 only has to add connection-drop-on-revoke, not revocation itself. | 1.4 |
| 17 | 2026-07-22 | Revoke endpoint admin credential: **reuses `ENROLLMENT_SECRET`** as a stand-in admin credential (`POST /workers/{id}/revoke` body) | Design a dedicated operator/admin auth model now | No operator/admin auth model has been designed — building one now would be scope creep beyond what Phase 1.4's own written scope (worker-side auth) asks for. Reusing the existing shared secret is a explicitly-flagged stopgap, not a real access-control model; revisit when operator tooling is actually designed (no milestone currently scopes this — flag if one should). | 1.4 |
| 18 | 2026-07-22 | WebSocket handshake: **auth token in the `Authorization: Bearer` header at connect time, then a `hello` envelope naming worker ID and protocol version**, verified before any `hello_ack`. Token is checked once, at handshake — not re-checked for the life of the socket | Re-verify the token periodically over the open connection | The Phase 1.4 access token's only job is proving identity to open the channel; re-checking it mid-connection would require either the coordinator pushing forced-reconnects on expiry (adds complexity with no corresponding exit criterion in 1.4 or 1.5) or the worker re-authenticating over the same socket (protocol complexity not asked for). Revocation is still enforced promptly because the worker's own reconnect loop calls `/workers/token/refresh` fresh on every connection attempt, and that endpoint already checks `revoked` (Decision #16). | 1.5 |
| 19 | 2026-07-22 | Connection registry (`worker:{id}:connection` in Redis): written on accept, **refreshed by TTL on every `pong`**, deleted by the per-connection handler's own `finally` block on disconnect (compares stored `session_epoch` before deleting, so a fast reconnect's new entry is never clobbered by the old connection's late cleanup) | Heartbeat-driven registry (defer to Phase 1.6) | The registry needs to exist now — Phase 1.5's own exit criteria require it ("registry state is visible in Redis and accurate") — but the real liveness state machine (`SUSPECT`/`OFFLINE`, missed-heartbeat thresholds) is explicitly Phase 1.6's job. This is deliberately just "is a socket open," nothing more. | 1.5 |
| 20 | 2026-07-22 | On-demand push (`POST /workers/{id}/push`): **fire-and-forward via a per-worker Redis pub/sub channel** (`worker:{id}:push`), not a queue. Any coordinator instance can publish; whichever instance holds the live connection (if any) forwards it. No delivery confirmation, no persistence if nobody's listening | A durable per-worker outbox/queue in Redis or Postgres | A queue implies "deliver eventually, survive a disconnect" — that's what M2's real task-delivery pipeline is for. Building a durable outbox here would duplicate work M2 does properly, for a phase whose own exit criterion only asks for "push a message to a connected worker on demand." Documented as fire-and-forward, not claimed as more than that. | 1.5 |
| 21 | 2026-07-22 | Graceful shutdown: relies entirely on **uvicorn's own per-connection `Server.shutdown()`** (sends a real WS close, code 1012 "service restart," and waits for each connection handler's task — including its `finally` cleanup — to finish before the app's own lifespan shutdown runs), rather than app-level code that re-sends a close to every connection | An app-level `_drain_local_connections()` step in the FastAPI lifespan shutdown hook that explicitly messages and closes every locally-held socket | Built the app-level version first, then verified live that it never actually ran with anything to do: uvicorn's `Server.shutdown()` (source inspected directly, `uvicorn==0.51.0`) calls `connection.shutdown()` on every live connection and awaits `_wait_tasks_to_complete()` **before** invoking `self.lifespan.shutdown()` — so every `ws_connect` handler's own disconnect cleanup has already completed by the time app-level shutdown code would run. Kept the simpler, verified-correct path and deleted the dead code (`_LocalConnection`, `_local_connections`, `_drain_local_connections`) rather than leave a function whose docstring claimed behavior it could never actually perform. | 1.5 |
| 22 | 2026-07-22 | Worker reconnect: **fixed 3-second delay** (`WORKER_WS_RECONNECT_DELAY_SECONDS`), not exponential backoff | Exponential backoff with jitter now | Explicitly Phase 1.7's job per the phase document's own Step 1.7 scope ("exponential backoff with jitter, a cap, and a reset rule"). Building it now would duplicate work and pre-empt 1.7's own design gate. Documented in both the phase doc's exit criteria note and `worker/worker.py`'s module docstring as a deliberate placeholder, not a silently-incomplete implementation. | 1.5 |
| 23 | 2026-07-22 | Heartbeat transport: an **application-level `heartbeat` envelope sent over the existing `/ws/connect` socket** every `WORKER_HEARTBEAT_INTERVAL_SECONDS` (default 5s), entirely separate from the Phase 1.5 transport-level ping/pong keepalive | Reuse ping/pong itself as the heartbeat signal | Ping/pong (Phase 1.5) only proves the socket is open — it carries no payload and was never meant to. The Step 1.6 heartbeat needs to carry worker-reported metrics (CPU, memory, uptime, sequence, agent version) that ping/pong has no shape for. Overloading ping/pong would conflate "is the transport alive" with "is the application reporting fresh state," which need different failure semantics (transport timeout is 45s; SUSPECT/OFFLINE thresholds are much tighter). Keeping them separate also means a future change to one cadence doesn't silently affect the other. | 1.6 |
| 24 | 2026-07-22 | Liveness thresholds: `HEARTBEAT_SUSPECT_THRESHOLD_SECONDS=12`, `HEARTBEAT_OFFLINE_THRESHOLD_SECONDS=25`, `HEARTBEAT_SWEEP_INTERVAL_SECONDS=5` — all **recommendations, not measured values** | Missed-heartbeat *count* (e.g. "2 missed = SUSPECT") instead of absolute seconds | Absolute-seconds thresholds decouple the coordinator's sweep logic from the worker's configured interval entirely — the coordinator doesn't need to know or trust what interval a given worker claims to run at (coordinator-observed, not worker-reported, per CLAUDE.md §12). A count-based rule would require the coordinator to trust a worker-supplied interval or hardcode an assumption about it. No load test has justified these specific numbers yet; revisit if Phase 1.9's scale simulation shows false positives/negatives at scale. | 1.6 |
| 25 | 2026-07-22 | CPU/memory measurement: **stdlib-only reads of `/proc/stat` and `/proc/meminfo`** inside the worker, no new dependency | `psutil` | Two counters (CPU%, memory%) don't justify a new third-party dependency — `/proc` gives both directly on the Linux containers this project targets. Deliberately reads host-level figures rather than a container cgroup limit, which is what the Step 1.6 exit criterion's own wording ("accurate against the host") calls for, not a coincidence of the simpler approach. | 1.6 |
| 26 | 2026-07-22 | Latency: reuses the **existing Phase 1.5 ping/pong round trip**, coordinator stamping `time.monotonic()` when a `ping` is sent and computing the delta when the matching `pong` arrives | A dedicated latency-probe message | Phase 1.5 already sends a ping every `WS_PING_INTERVAL_SECONDS` and expects a pong back — that round trip already is a latency measurement; a second, separate probe message would duplicate traffic for no new information. Coordinator-side timing only (Step 1.6's own requirement) — never derived from the worker's embedded envelope timestamp. | 1.6 |
| 27 | 2026-07-22 | `QUARANTINED` is a **sticky terminal status** — the single status-transition helper refuses to move a worker out of `QUARANTINED` via any path (heartbeat, sweep, reconnect); only an explicit un-quarantine feature (not yet designed) could do so | Let heartbeats naturally re-establish `ONLINE` once a socket is dropped and reconnected | Found as a real bug during this phase's own verification, not left for later: a socket already open *before* `POST /workers/{id}/revoke` keeps sending heartbeats — Phase 1.5's Decisions Log #18 already documents that revocation doesn't re-check a live socket mid-connection — and those heartbeats were silently flipping the DB `status` back to `ONLINE`, contradicting the revocation an operator just issued. Re-verified live after the fix: five heartbeats on a still-open pre-revocation socket left `status = QUARANTINED` unchanged in Postgres throughout. | 1.6 |
| 28 | 2026-07-22 | Reconnect backoff: **full-jitter exponential** (`delay = random.uniform(0, min(MAX, BASE·FACTOR^consecutive_failures))`, defaults `BASE=1s, FACTOR=2, MAX=30s`), resetting `consecutive_failures` to 0 the moment a session is genuinely established (`hello_ack`), regardless of how that connection later ends | Equal-jitter or decorrelated-jitter backoff; a separate fixed post-restart stagger on top of the backoff | Full jitter is the simplest of the three that still gives real thundering-herd mitigation — a whole fleet failing at once each draws independently from `[0, cap)` rather than following each other's exact intervals, with no extra mechanism needed. A separate stagger step would duplicate what the jitter already provides. Resetting on session establishment (not on "the try/except block returned without raising") matters because a coordinator-initiated clean close after a real session isn't a failure — treating it as one would leave the *next* unrelated failure backed off further than it should be. | 1.7 |
| 29 | 2026-07-22 | Session-conflict resolution: **Redis pub/sub eviction** — every accepted handshake publishes its new `session_epoch` to `worker:{id}:control` before becoming authoritative; whichever coordinator instance holds an older live session for that worker ID (any replica, per CLAUDE.md §3.9) is subscribed and force-closes its own socket (code 4409) the instant it sees a newer epoch | Have the new handshake block and wait for the coordinator to confirm the old session is closed before proceeding | Blocking the new handshake on old-session teardown would need a request/response protocol over pub/sub (ack channels, timeouts) for a guarantee the existing epoch-comparison already gives for free: the loser's own registry-cleanup guard (Decisions Log #19) already no-ops correctly once its epoch is stale, so the only genuinely missing piece was *actively terminating the transport*, not coordinating who's allowed to proceed. The winner never waits on the loser's cleanup. | 1.7 |
| 30 | 2026-07-22 | Dashboard real-time updates: **client polls `GET /api/workers` every 2s** and re-renders | A dashboard-facing WebSocket pushed from the coordinator | A 2s poll already reads as live to a human eye. It also makes "reconnects itself if the browser connection drops" (Step 1.8's own exit criterion) free — a poll that fails just tries again in 2s — instead of requiring a second reconnect/backoff protocol on top of the one Phase 1.7 already built and verified for workers. Revisit only if a real sub-second latency requirement emerges. | 1.8 |
| 31 | 2026-07-22 | Dashboard-to-coordinator auth: **server-side proxy** — the dashboard backend holds the admin secret (reused `ENROLLMENT_SECRET`, same stopgap as `/revoke` and `/push`) and calls the coordinator's new admin-protected `GET /workers`; the browser only ever talks to the dashboard backend, never the coordinator directly | Have the browser call the coordinator's `/workers` endpoint directly with the secret embedded in client JS | Embedding the admin secret in anything the browser can read would violate CLAUDE.md §12 ("never rendered in the GUI") the instant a user opened dev tools — there is no way to call an admin-protected endpoint from client-side JS without exposing the credential used to authenticate it. | 1.8 |
| 32 | 2026-07-22 | Visual identity: **dark "radar/phosphor console" theme** — deep green-tinted black (not neutral or blue-black), a signature animated hub-and-spoke glyph as the wordmark icon (a literal, functional nod to the star-topology invariant, CLAUDE.md §3.1, not decoration), and a multi-hue functional color system (five distinct status colors) rather than one decorative accent color | The generic "near-black background + one bright accent color" AI-default template | Deliberately avoided the single-accent-color cliché: the palette's chroma comes from the five worker states themselves (ONLINE/SUSPECT/OFFLINE/QUARANTINED/CONNECTING), which is what this page is actually *for*, not from a brand accent layered on top. The radar/phosphor console direction is grounded in the subject (a network-operations monitoring tool watching a hub-and-spoke fleet) rather than picked as a generic dark-mode default. A light theme is also implemented (`prefers-color-scheme`) for completeness. | 1.8 |
| 33 | 2026-07-22 | Scale-test worker identity: **`docker-compose.scale.yml` override** replacing the named `worker-identity-data` volume with an anonymous per-replica volume, used only via `-f docker-compose.yml -f docker-compose.scale.yml up --scale worker=N` | Modify the base `docker-compose.yml` itself to use an anonymous volume always | The base compose file's named volume is what makes identity persist across restart/recreation for normal single-worker dev (Phase 1.3's own verified exit criterion) — changing it there would silently undo that. An override file gets `--scale` working for the transient scale-simulation use case without touching the persistence behavior every earlier phase already demonstrated. Identity not persisting across a scaled replica's recreation is acceptable here: a scale simulation is a transient measurement, not a persistence test. | 1.9 |
| 34 | 2026-07-22 | Phase 1.9 scope: **stopped at 50 workers, by explicit user direction**, not a discovered bottleneck | Continue to the 100-worker level and its 10-minute no-heartbeat-loss soak in this same session | All containers stopped simultaneously right after the level-50 measurement. Read initially as a possible Docker Desktop resource-ceiling event and reported to the user as such (CLAUDE.md §16 — a benchmark-adjacent result contradicting the expected outcome is a stop-and-ask trigger, not something to push through). The user then clarified they had stopped the containers themselves, not realizing a test was mid-run — no technical bottleneck was actually found. The user accepted 50 workers as sufficient ("if it worked for 50 it is fine") rather than continuing to 100. Recorded honestly as a user scope call, not fabricated as a discovered limitation. | 1.9 |
| 35 | 2026-07-22 | Phase 1.10 demo: **the "100 workers" figures in Step 1.10's written demo/failure-demo (scale-to-100, restart-coordinator-with-100) were run at 50 workers instead**, by explicit user direction given twice this session ("do not scale up to 100 workers") | Run the literal 100-worker figures as the phase document originally specified | Same substitution already accepted for Phase 1.9 (Decisions Log #34) — 50 workers already demonstrated clean at every measured dimension (zero ID collisions, dashboard/DB count match, modest coordinator resource use) at that level. Every other Step 1.10 demo/failure-demo item was run as written, against a genuine `git clone` of the local repo (not the working tree), not the working tree itself. | 1.10 |
| 36 | 2026-07-22 | Cloud provider: **Oracle Cloud Infrastructure (OCI) Always Free tier** | GCP (GKE Autopilot control-plane fee waiver, but only a 90-day/$300 credit before node costs bill); AWS (EKS control-plane fee alone is ~$73/mo, free tier is a 12-month trial, not perpetual) | User set a hard $0/month cost ceiling (this decision). OCI is the only major provider with a genuinely perpetual (not trial) free tier generous enough to run real Kubernetes: no OKE control-plane fee at all, plus free Ampere A1 compute (4 OCPU/24GB total) for nodes. GCP/AWS free allowances expire and then bill. | 1.5.0 |
| 37 | 2026-07-22 | Region: **nearest OCI home region with Ampere A1 capacity, determined empirically when Step 1.5.1 actually provisions**, not fixed in advance | Committing to a single named region now | User had no region preference ("any available"). OCI's Always Free Ampere A1 shape is frequently reported out of capacity in popular regions — a well-documented, provider-wide constraint outside this project's control — so availability, not preference, is the binding constraint. Region is chosen at provisioning time based on where capacity actually exists, with nearer regions tried first to minimize latency. | 1.5.0 |
| 38 | 2026-07-22 | Kubernetes distribution: **OKE (managed control plane)**, not self-managed k3s | Self-managed k3s installed directly on the same free compute VMs | OKE's control-plane fee is $0 on OCI regardless of tier, so a managed control plane costs nothing extra over self-managed while removing the operational burden of upgrading and securing the control plane itself. | 1.5.0 |
| 39 | 2026-07-22 | Database and Redis: **self-hosted in-cluster** — Postgres and Redis run as Kubernetes StatefulSets on OCI Always Free block storage, not managed services | Managed Postgres/Redis (e.g. OCI Base Database Service, a managed Redis offering) | No provider offers a genuinely free managed database or Redis tier; the user's $0 ceiling rules them out entirely. Documented limitation, not silently assumed away: no automated managed backups or failover — backup/restore becomes the operator's own manual responsibility. | 1.5.0 |
| 40 | 2026-07-22 | Environment topology: **one cluster, two Kubernetes namespaces** (`staging`, `production`) for isolation | Two fully separate clusters, one per environment | Two clusters would need roughly double the Always Free compute quota (8 OCPU/48GB), which OCI does not grant for free. Namespace-level isolation within a single small cluster is a scoped compromise made explicitly for the $0 ceiling — revisit if a real budget becomes available. | 1.5.0 |
| 41 | 2026-07-22 | Secret management: **Kubernetes-native Secrets plus `sealed-secrets`** (free, open-source; encrypts secrets so the encrypted form is safe to commit to Git) | OCI Vault | OCI Vault's free allowance is limited and anything past it is a paid, cloud-specific dependency the project doesn't otherwise need. `sealed-secrets` satisfies CLAUDE.md §12 ("secrets from a secret manager or Kubernetes secrets, never in Git") at $0 cost. | 1.5.0 |
| 42 | 2026-07-22 | Compute platform: **k3d (k3s running as Docker containers) on the user's own laptop** — **supersedes Decision #36 and #38 (OCI, OKE)** | WSL2 + a native k3s install on the same laptop | User rejected OCI once told every major cloud requires a card on file for identity verification even on a free tier, and confirmed no spare always-on machine exists — only the main laptop, on only while testing. k3d reuses Docker Desktop, already the project's dependency for every phase through M1, rather than adding a second virtualization layer (WSL2) for no benefit at a 4–5-worker test scale. Real CNCF-conformant k3s under the hood, not a toy. | 1.5.0 |
| 43 | 2026-07-22 | Region: **not applicable** — compute is local, no cloud region exists — **supersedes Decision #37** | — | Direct consequence of Decision #42; no cloud provider means no region to choose. | 1.5.0 |
| 44 | 2026-07-22 | Public ingress: **Cloudflare Tunnel + Cloudflare-managed DNS** (free plan, no card required) | ngrok free tier (rotating URL on restart, unsuited to a stable public hostname); manual router port-forwarding + Let's Encrypt (opens an inbound port directly on the user's home network) | Cloudflare Tunnel keeps a pod-initiated outbound-only connection to Cloudflare's edge, giving a stable public hostname with zero inbound ports opened on the laptop or router — the same "dial out, never accept inbound" philosophy CLAUDE.md §3.4 already applies to workers, extended here to ingress. Free Cloudflare plan requires no payment method. | 1.5.0 |
| 45 | 2026-07-22 | Container registry: **GitHub Container Registry (ghcr.io)** | Docker Hub free tier (tighter pull-rate limits); a cloud provider's registry (needs a card, per Decision #42's own rejection) | Free, no card, and integrates directly with the GitHub Actions CI pipeline already planned for Step 1.5.3 — no new account or credential type introduced. | 1.5.0 |
| 46 | 2026-07-22 | Terraform remote state: **Terraform Cloud (HCP Terraform) free tier** | Local state file only; cloud object storage backend (S3/GCS-style, needs a cloud account with a card) | The only no-card option that actually satisfies Step 1.5.1's own exit criterion of remote state with real locking (tested by attempting a concurrent apply) — a local state file cannot provide genuine locking against a second machine or session. | 1.5.0 |

**Honest limitation carried into every later step that assumes uptime**: this cluster exists only while the user's laptop is on, awake, and Docker Desktop is running. "Public reachability" (Step 1.5.5), "24/7"-flavored language in the M1.5 demo (Step 1.5.9), and any real Internet worker onboarding (Step 1.5.8) will be scoped honestly to "reachable while the laptop is running," not real always-on production hosting. This is a user-driven scope constraint (no spare hardware), not a silently discovered limitation.

| 47 | 2026-07-22 | Compute platform: **reverted to OCI (Oracle Cloud) Always Free tier + OKE** — **supersedes Decision #42** (k3d on the laptop) | Staying on k3d/laptop (previous decision) | User explicitly reconsidered and confirmed acceptance of OCI's card-on-file requirement for identity verification (Always Free itself is not billed). Restores a genuinely always-on, cloud-reachable cluster — removes the "only reachable while the laptop is on" limitation Decision #42 carried. | 1.5.0 |
| 48 | 2026-07-22 | Region: **restored to "nearest OCI home region with Ampere A1 capacity, determined empirically at Step 1.5.1 apply-time"** — **supersedes Decision #43** (n/a) | Fixing one region in advance | Direct consequence of Decision #47 restoring a cloud region; same reasoning as original Decision #37 — Ampere A1 capacity availability, not preference, is the binding constraint. | 1.5.0 |
| 49 | 2026-07-22 | Public ingress: **retired as a design-gate decision — reverts to Step 1.5.5's original scope** (ingress controller, cert-manager/TLS, DNS zone) — **supersedes Decision #44** (Cloudflare Tunnel) | Keep Cloudflare Tunnel in front of OCI anyway | Cloudflare Tunnel (Decision #44) existed specifically to reach a laptop with no public IP and no router port opened. OCI compute has a real public IP/load-balancer path, so the workaround is unnecessary — keeping it would add a second DNS/ingress system to maintain for no benefit. Ingress specifics (controller choice, DNS zone, certificates) are correctly Step 1.5.5's job, not the design gate's. | 1.5.0 |
| 50 | 2026-07-23 | Cloud provider: **changed to GCP (Google Cloud) + GKE** — **supersedes OCI Decisions #47–49** | Staying on OCI | User directed the switch to GCP for a "test end-to-end cheaply now, migrate to a proper provider later" plan: GKE Autopilot's $300 / 90-day credit is the most generous runway, GKE is the most portable/standard managed K8s (least migration lock-in), and it avoids OCI's Ampere-A1 "out of capacity" lottery. Trade-off accepted honestly: GCP's free allowance is a 90-day credit that then bills, not OCI's perpetual $0 — aligned with the stated "test then migrate" intent. Decisions #39–41 and #45–46 (self-hosted Postgres/Redis, single-cluster namespaces, sealed-secrets, ghcr.io, Terraform Cloud) are provider-agnostic and still stand. | 1.5.0 |
| 51 | 2026-07-23 | **All Milestone 1.5 cloud provisioning and verification is deferred (parked) until the user's GCP account is unblocked.** The user's Google account is currently restricted by their employer; unblock ETA unknown. When access is restored, every cloud-dependent M1.5 phase will be built and tested against the real GKE cluster in one focused pass. No M1.5 phase is marked DONE until then; the OCI-targeted Terraform scaffold from the prior session will be rewritten for GCP at resume time. §8 consequence carried forward honestly: the Internet-testing exit criterion applies to every phase from M1.5 onward, so the internet-verification half of later phases (M2+) also cannot be completed until this same cloud access lands — M2+ *logic* can still be built and locally verified on Docker Compose in the meantime, but such phases would accumulate as "locally verified, internet/cloud-verification pending" and must not be marked fully DONE until the cluster exists. | 1.5.1 |
| 52 | 2026-07-23 | Public reachability / cross-network worker testing: **Cloudflare Tunnel with the coordinator self-hosted on local Docker Compose** — **supersedes the paid-cloud provider choice for reachability (GCP #50, OCI #47–49) and the parked-pending-account state (#51)** | A paid cloud provider (GCP/OCI/DO) hosting an always-on public coordinator | The user's cloud access is blocked only by a company network **MAC-filter on Google specifically** — not an account/identity restriction, and not other domains (this corrects the earlier misread recorded in #50–#51). Cloudflare is not Google → reachable from the company network, free, no card. `cloudflared` runs on the coordinator PC only (dials out to Cloudflare's edge, no inbound ports, no router changes), producing a public HTTPS/WSS URL a worker on any other PC/network dials into — satisfying §8 ("at least one worker outside the local network, per phase") for a live test session with no paid cloud. Revives the reachability half of the earlier-reverted Decision #44, now scoped to testing/reachability, not as the cloud provider. Honest limitation: reachable only while the user's PC and the tunnel run — not always-on production hosting; §8 does not require always-on. Quick-tunnel URL is ephemeral (changes per restart); a stable hostname would need a free Cloudflare account + a domain (named tunnel), deferred until needed. | 1.5.0 |
| 53 | 2026-07-23 | Milestone 1.5 compute host: **local Kubernetes via k3d** (k3s running as Docker containers on the user's own PC) — real CNCF-conformant Kubernetes at $0, **resolving Open Questions #2 and re-closing the re-opened Step 1.5.0** | (B) Stay on Docker Compose for V1 and re-scope M1.5, dropping Kubernetes / Terraform / Helm | User chose Option A to keep M1.5's Kubernetes/Helm/CI-CD deliverable real rather than cut it. k3d reuses Docker Desktop (already a project dependency), needs no cloud account or card, and is unaffected by the company's Google-only network block. Deployment target becomes a local k3d cluster reached via Cloudflare Tunnel (#52); "cloud-hosted" → "local cluster", "always-on public" → "reachable while the PC + tunnel run" (honest limitation — §8 requires only a per-phase cross-network worker, not 24/7 hosting). Provider-agnostic Decisions #39–41 / #45–46 still stand. Terraform's now-reduced scope (no cloud resources to provision) is a Step 1.5.1 design sub-gate — e.g. Terraform managing the k3d cluster + namespaces + Helm releases via the `kubernetes`/`helm` providers, versus creating the cluster with the k3d CLI and using Helm directly — not decided here. | 1.5.0 |
| 54 | 2026-07-23 | Step 1.5.1 Terraform scope: **thin Terraform** — the k3d CLI owns cluster lifecycle (create/destroy); Terraform manages only in-cluster declarative state (`staging`/`production` namespaces, a per-namespace ResourceQuota, the sealed-secrets controller) via the `kubernetes`/`helm` providers; remote state stays Terraform Cloud (Decision #46) | (A) Terraform-as-orchestrator — make the k3d cluster itself a Terraform resource via a `null_resource`+CLI or the unofficial community k3d provider; (C) retire Terraform for local, use k3d CLI + Helm directly | A wraps a CLI in Terraform (fragile, unofficial provider, hand-rolled `destroy`) for no real gain; C drops M1.5's Terraform deliverable (CLAUDE.md §4) and loses the remote-state-locking exit criterion. B keeps Terraform a real IaC deliverable doing what it is good at (declarative in-cluster state) while k3d does cluster lifecycle. `infra/terraform/` was rewritten from the OCI/OKE scaffold to `kubernetes`+`helm` providers accordingly (UNVERIFIED — no terraform CLI installed yet). Exit criteria re-scoped honestly: cluster "from nothing" = k3d CLI (one documented command); "full environment from nothing" = `terraform apply` of all in-cluster resources; "cost after 24h" = n/a ($0 local). User approved Option B 2026-07-23. | 1.5.1 |

| 55 | 2026-07-23 | Step 1.5.2 migrations: **run `alembic upgrade head` in a coordinator `initContainer` before the app container starts**, and disable the in-app startup migration in Kubernetes via `RUN_MIGRATIONS_ON_STARTUP=false` (config gate added; Docker Compose keeps the Decision #8 startup migration, default true). **Refines the design-gate's stated "Helm pre-install hook Job."** | The pre-install hook Job originally described (approved as Option C of the 1.5.2 gate) | Discovered during build: a Helm `pre-install` hook runs *before* the chart's own Postgres exists, so a migration Job in that phase can never reach the same-chart DB on first install — the described approach is structurally broken for an in-chart datastore. The initContainer achieves Decision C's actual intent better: migrations complete before the app container serves (the container literally cannot start until the initContainer exits 0), and with `maxSurge=1`/`minReplicas>=1` only one pod migrates pending revisions at a time so replicas never race on cold start (`ponytail:` ceiling noted in the template — add a DB advisory lock if a true concurrent cold-start path ever becomes real). Verified live: initContainer logged `Running upgrade -> 0001, create workers table`, coordinator then reached readiness (`/ready` passing = Postgres+Redis reachable). | 1.5.2 |

| 56 | 2026-07-23 | Step 1.5.3 CI pipeline scope (user-approved calls): **(A) write a baseline test suite now** — protocol-envelope unit tests + a coordinator integration test run against ephemeral Postgres+Redis (GitHub Actions `services:`) — since M1 shipped no tests at all (a §11 debt); **(B) Terraform in CI = `fmt -check` (always) + `validate` (gated on a `TF_API_TOKEN` repo secret), NO real `terraform plan`** — the thin-Terraform `kubernetes`/`helm` providers target the local k3d cluster (Decision #54), unreachable from GitHub's hosted runners, so `plan` runs locally only (honest limitation, same family as "reachable only while the PC runs"); **(C) build-only, not pushed** this session. | (A) minimal smoke tests / defer tests; (B) a self-hosted runner on the user's PC so CI could reach the local cluster for a real `plan`; (C) push + open a PR to verify green | Baseline tests start paying down the real §11 gap rather than papering over it; validate-only keeps CI honest about what a local cluster can and can't do from a hosted runner; a self-hosted runner was declined for now (setup + exposes the machine to CI jobs). `ci.yml` extended to jobs `lint`/`test`/`build`(SHA-tagged images, never only `latest`)/`scan`(`pip-audit` enforced + Trivy fs report-only)/`terraform`. Verified locally, not on a PR: 8/8 tests pass vs ephemeral Postgres+Redis, `ruff` clean, `actionlint` clean, `terraform fmt -check` clean. Still pending a push: an actual green Actions run, required-status-checks, and branch protection (a manual GitHub setting — standing Blocker #1). | 1.5.3 |

| 57 | 2026-07-24 | Compute host / cloud provider: **Microsoft Azure — AKS (Azure Kubernetes Service), Free-tier control plane** — **supersedes Decisions #52–#53** (local k3d) and retires the Cloudflare-Tunnel reachability role of #52 | Staying on local k3d + Cloudflare Tunnel | User moved back to a real managed cloud now that an **Azure for Students** subscription is available (Azure CLI 2.88.0 installed locally and verified; **not yet `az login`'d**). Azure for Students = **$100 credit / 12 months / no credit card**, and when the credit is exhausted resources stop rather than billing a card — a genuine safety net for the $0-out-of-pocket ceiling, unlike pay-as-you-go. A managed cloud has a real public LoadBalancer/IP, so **Cloudflare Tunnel is retired** (same reasoning as the earlier Decision #49 — a cloud with real ingress doesn't need the tunnel workaround). **Cost discipline is mandatory and explicit** (student credit is finite): AKS **Free-tier control plane ($0)**; a **single small B-series burstable node, no autoscaling**; **`az aks stop` whenever a test session ends** to deallocate node VMs and halt compute billing; `az group delete` to remove everything. Provider-agnostic Decisions **#39–41 / #45–46 still stand** — in-cluster Postgres/Redis, staging/production namespaces, sealed-secrets, ghcr.io for images, Terraform Cloud remote state (**no** Azure Container Registry, **no** Key Vault, **no** Azure Storage backend — each would burn credit for no benefit at this scale). **Consequence:** the k3d-verified work of Steps 1.5.1 (thin Terraform) and 1.5.2 (Helm deploy) must be **re-implemented/re-verified on AKS** — 1.5.1's Terraform is rewritten from `kubernetes`/`helm`-only to add the `azurerm` provider (resource group + AKS + node pool); the 1.5.2 chart is portable and only re-targets the new cluster. Step 1.5.3 CI is provider-agnostic and largely stands. Two sub-choices remain OPEN, deferred to the Step 1.5.1 design sub-gate (Open Questions #3): (a) Terraform provisions the AKS cluster via `azurerm` vs `az aks create` + thin Terraform; (b) node VM size. This session was **docs-only** — no `az login`, no code, no provisioning. | 1.5.0 |

| 58 | 2026-07-24 | **Scrap all prior Milestone 1.5 build work and redo M1.5 from scratch under the Azure method.** Steps 1.5.1 (thin k3d Terraform), 1.5.2 (Helm deploy on k3d) and 1.5.3 (CI pipeline) are reset to NOT STARTED; each existing artifact is reassessed during the redo rather than assumed carried over. | Incrementally port the k3d artifacts to Azure (keep the Helm chart and CI as-is, only rewrite the Terraform) | User directed a clean redo rather than an incremental port. Recorded as a directive for traceability, not a design trade-off. **Docs-only consequence this session:** phase status reset in the register/snapshot; the actual `infra/terraform/`, `infra/helm/platform`, `.github/workflows`, and baseline-test files are **not deleted this session** (deleting code is a separate, explicit step the user has not yet requested) — they remain in the tree, marked scrapped/superseded, to be replaced or reused as the Azure rebuild decides. Provider-agnostic Decisions #39–41 / #45–46 still stand. | 1.5.0 |

| 59 | 2026-07-24 | **Deleted the scrapped M1.5 infra code from the working tree** — `infra/terraform/` (k3d thin Terraform, Step 1.5.1) and `infra/helm/` (Helm chart + bootstrap-secrets, Step 1.5.2), 16 files total — so M1.5 restarts from an empty infra base on Azure. Recoverable from git history (commits `9052662`, `d072962`). | Keep the files in-tree marked scrapped (the #58 docs-only state); or also delete `.github/workflows/ci.yml` and `tests/` | User directed deleting the scrapped infra files to "start from the very beginning." **Kept `infra/dev-ca/`** (M1 Phase 1.1 dev-CA generator — not M1.5, still in use). **`.github/workflows/ci.yml` and `tests/` were NOT deleted** — `ci.yml` originated in M1 Phase 1.1 (lint/build skeleton) and was only extended in 1.5.3, and `tests/` are provider-agnostic §11 debt-paydown; deleting either would regress an approved M1 deliverable, so both are flagged for an explicit user decision rather than removed unilaterally. Note: the pre-existing uncommitted edit to `infra/terraform/variables.tf` is gone with the directory (was part of the scrapped Terraform). No commit made this session. | 1.5.0 |

| 60 | 2026-07-24 | **Step 1.5.1 design sub-gate (Open Questions #3) resolved:** (a) **Terraform owns the AKS cluster** via the `azurerm` provider (resource group + `azurerm_kubernetes_cluster` Free-tier + single node pool); (b) node size **`Standard_B2s`** (2 vCPU / 4GB, cheapest viable). | (a) `az aks create` + thin Terraform; (b) `Standard_B2ms` (8GB) | User approved both 2026-07-24. Thin Terraform (the k3d Decision #54 shape) made sense only because k3d has no cloud API; Azure has a first-class `azurerm` AKS resource, so going thin would be the one place CLAUDE.md §4 ("all cloud resources via Terraform, no console clicks") is actually violated. B2s over B2ms for cost, with an escape hatch on record to bump to 8GB if a memory ceiling appears. | 1.5.1 |

| 61 | 2026-07-24 | AKS **`node_resource_group` set explicitly** to `<cluster>-nodes`. Build fix during apply. | Let Azure auto-name the node RG | Azure's default node RG name `MC_<rg>_<cluster>_<region>` was 84 chars, over the 80-char max → apply failed `400 InvalidParameter`. An explicit short name (`data-cleaning-distributed-system-nodes`, 38 chars) fixes it. | 1.5.1 |

| 62 | 2026-07-24 | Node VM size **substituted `Standard_B2s` → `Standard_B2s_v2`** (2 vCPU / **8GB**), user-approved. | `Standard_B2ls_v2` (2 vCPU / 4GB — exact-spec match, cheapest v2) | Forced availability substitution: `Standard_B2s` (v1 B-series) is **not offered** in `centralindia` for this Azure-for-Students subscription — only v2 B-series is (apply failed `400 BadRequest` listing allowed SKUs). Same burstable class; chose the 8GB `B2s_v2` over the 4GB `B2ls_v2` because a single node running coordinator+Postgres+Redis+sealed-secrets is tight on 4GB (AKS reserves ~1GB), pre-empting the exact memory ceiling Decision #60 flagged. Cost ~$0.083/hr while running, $0 when `az aks stop`'d. **Applied + verified live 2026-07-24:** `terraform apply` = 7 added/0 changed/0 destroyed; cluster up in 4m48s; node Ready (k8s v1.35.6, Ubuntu 24.04); `staging`/`production`/`sealed-secrets` namespaces Active; per-namespace quotas applied; sealed-secrets pod 1/1 Running; remote-state locking observed real (a stale HCP lock had to be cleared via the web UI). | 1.5.1 |
| 63 | 2026-07-24 | **Deployment does NOT set `spec.replicas` for the coordinator; the HPA owns replica count** (`hpa.minReplicas` is the floor — staging 2/2, production 2/2). | Deployment `replicas` + HPA both set | Setting `replicas` in the manifest while an HPA also manages it causes a Helm v4 server-side-apply conflict with `kube-controller-manager`'s `scale` subresource, and the HPA floor silently overrode the intended 2 (scaled to `minReplicas:1`). Standard HPA practice = omit `replicas` from the Deployment. Found + fixed live during 1.5.2 verification. | 1.5.2 |
| 64 | 2026-07-24 | **Coordinator rolling strategy `maxSurge=0` / `maxUnavailable=1` (replace-in-place), and CPU limit lowered 500m→300m.** | `maxSurge=1` / `maxUnavailable=0` (zero-drop surge) | The namespace `limits.cpu=2` quota (Decision, Step 1.5.1) cannot fit a transient 3rd coordinator pod on top of two existing ones (old pods still hold their limits until replaced), so a surge rollout **deadlocks** — can't add the new pod (quota) before removing an old one (`maxUnavailable=0`). Replace-in-place drops one of two replicas first (freeing quota), keeping ≥1 serving (PDB `minAvailable=1`) so the fleet is never dropped. Zero-drop surge becomes available again once node/quota grows (multi-node, later). Found + fixed live during 1.5.2 verification. | 1.5.2 |
| 65 | 2026-07-24 | **ghcr.io coordinator/dashboard packages made PUBLIC; no `imagePullSecret`** (chart supports an optional one if made private later). | Private packages + sealed dockerconfigjson pull secret | User chose the simplest $0 path (Decision #45 ghcr). Public read = AKS node pulls anonymously, one fewer secret to seal. Images built + pushed manually tagged by commit SHA (`999772d`) for 1.5.2; CI/CD automates this in 1.5.3/1.5.4. No secret lives in the image (§12). | 1.5.2 |
| 66 | 2026-07-24 | **Step 1.5.3 CI pipeline rebuilt for the Azure method and APPROVED DONE.** Five parallel jobs (lint, test, build×3 matrix, scan, terraform). Key sub-decisions: (a) **build-only on PR / push SHA-tagged images to ghcr.io on merge to main** (not `:latest`); (b) **CI `terraform plan` scoped to the azurerm layer via `-target`** — `az aks stop` deallocates the whole control plane so the kubernetes/helm providers can't refresh from a runner; (c) **ruff ruleset pinned in `ruff.toml`** (classic `E4/E7/E9/F`) after an unpinned `--with ruff` began firing opt-in rules; (d) **Azure service principal** `ci-terraform-plan-dcds` (Contributor on the RG) → repo secrets `ARM_CLIENT_ID/SECRET/TENANT/SUBSCRIPTION_ID` + `TF_API_TOKEN`; (e) **branch protection on `main`** requires all 7 check contexts (`strict:true`, `enforce_admins:false`). | Split Terraform into two root modules for a full CI plan (rejected — live remote state already applied, splitting risks recreating the AKS cluster; targeting the azurerm layer meets the exit criterion without state surgery); validate-only CI with a waived plan criterion (option B, rejected). | All 6 exit criteria objectively verified: full pipeline on every PR; broken-test-blocks-merge and vuln-dep-flagged both proven by a throwaway PR (#3) going red on `test` (`AssertionError`) and `scan` (`requests 2.19.1 PYSEC-2026-1872/2275`); azurerm plan in the PR checks summary; ~2 min duration; SHA-tagged images pushed on merge (run `30085974120`, tag `4969097…`, all three services). A one-time fix was needed: the hand-created ghcr packages (Decision #65) lacked repo write access → `denied: write_package` on the first main push; granting the repo Actions **Write** role on each package fixed it (worker package auto-created by CI). PR #2 (all M1.5.1–1.5.3 work) merged to main. No Azure compute started by CI. | 1.5.3 |
| 67 | 2026-07-24 | **Step 1.5.4 CD model = Option C: hosted-runner CD deploying over the PUBLIC AKS API, with NO CD-driven `az aks start/stop`.** Node up/down stays the user's manual credit lever at session boundaries. CD (`cd.yml`) triggers on **CI success on `main`** (`workflow_run`), not the raw push, so images are guaranteed pushed before deploy; it deploys `workflow_run.head_sha` (the full SHA CI tagged images with — NOT the short sha the old values-staging comment showed). Reusable `_deploy-env.yml` runs staging then production: az login (reuse 1.5.3 ARM_* SP secrets) → get-credentials → cluster-up guard (fails loudly, $0, if `az aks stop`'d) → `helm upgrade --install platform --atomic --timeout 5m` → in-cluster smoke curl of `/ready` + `/health` (self-signed → `-k`) with a version-match assert → `helm rollback` on failure → timed record. production gated by a `production` GitHub Environment required-reviewer rule. | (A) Hosted runner that `az aks start`→deploy→`az aks stop` every merge — rejected: burns credit + ~8min latency per deploy for hands-off cluster wake the user doesn't need. (B) Self-hosted runner on the laptop — rejected: needs a local runner process alive; no real gain over C once the cluster is up. | User chose C given $100 student credit: continuous node ≈$30–36/mo (~3mo runway) vs session-only ≈pennies/day (~full 12mo). C spends ZERO credit from CD itself — a deploy against a stopped cluster just fails. Built + statically verified (python, both workflow YAMLs, `helm template` renders GIT_SHA); **not yet run live** — awaiting `az aks start` + user verification of the 5 exit criteria and creation of the `production` Environment protection rule. | 1.5.4 |
| 68 | 2026-07-24 | **Step 1.5.4 smoke test rewritten from an ephemeral `kubectl run` pod to `kubectl exec` into the running coordinator; Step 1.5.4 APPROVED DONE after live verification.** The first live CD run (SHA `fe9ce3c`) deployed fine but the smoke pod was **rejected by the namespace `ResourceQuota`** (`must specify requests/limits.cpu/memory`) — an ad-hoc `kubectl run` pod declares none — which failed the smoke step and tripped the `if: failure()` `helm rollback`, undoing a healthy deploy. Fix: no new pod at all — `kubectl exec deploy/coordinator -c coordinator -- python -c …` hits the coordinator's own `https://localhost:8443/{ready,health}` (python guaranteed in the image; self-signed → `ssl._create_unverified_context()`), then asserts `/health`'s `version` == deployed SHA. Verified live against staging before re-merge. | Give the smoke pod explicit resources via `--overrides` (rejected — more brittle JSON, still spawns a pod for no reason); drop smoke and lean only on `helm --atomic --wait` (rejected — loses the explicit version-match proof for criterion 4). | Second run (SHA `0df7e206`) went fully green: staging auto-deployed, production gate held then deployed on the user's approval, both `/health` returned the deployed SHA, node at 19% CPU / 32% mem hosting both stacks. The earlier false-failure incidentally proved criterion 3 (auto-rollback) works. Shipped as PR #4 (build) + PR #5 (this fix). All 5 exit criteria objectively met; user approved DONE. | 1.5.4 |

**Decisions #39–41 and #45–46 are unaffected by this reversal** — self-hosted Postgres/Redis in-cluster, single-cluster namespace topology, `sealed-secrets`, ghcr.io for images, and Terraform Cloud for remote state were never OCI-specific or k3d-specific and still stand. Cost ceiling remains $0/month — a card on file for identity verification does not change that; OCI Always Free is not billed. **User re-confirmed this second reversal on 2026-07-22.** `infra/terraform/` is being rewritten for OCI accordingly (see Step 1.5.1 update below).

| 69 | 2026-07-24 | **Step 1.5.5 design gate:** public reach = **ingress-nginx + cert-manager (Let's Encrypt HTTP-01) + a Terraform-owned Standard static public IP in the AKS node RG with a free `<label>.<region>.cloudapp.azure.com` FQDN** (label `dcds-staging`). Single public host, **path-based routing** (`/workers`,`/ws`,`/health`,`/ready`→coordinator; `/`,`/api`→dashboard) — the worker only ever calls `/workers`+`/ws`, so no path collision, and it avoids a 2nd public IP. Backend-protocol **HTTPS** (nginx re-encrypts to the existing self-signed pods; §3.8 TLS-everywhere kept, and nginx doesn't verify the backend cert by default). ws proxy timeouts **3600s**; edge **`limit-rps=5`** on the coordinator ingress; dashboard behind nginx **`auth-basic`** (htpasswd Secret, not in Git). **Only staging is exposed** (`ingress.enabled` true staging / false production); a second public env needs a second IP + more node headroom than the single `B2s_v2` has — templated and disabled, an honest ceiling in the same family as the single-node HA one. | Azure Application Gateway/AGIC (costs credit); own domain + Azure DNS zone (~$0.50/mo + registrar — user chose the free `cloudapp.azure.com` FQDN); two public IPs for host-based routing (rejected — one IP + path routing is $0 and sufficient since worker paths don't collide); terminate TLS at the edge and speak plain HTTP to pods (rejected — breaks §3.8 on the internal hop). User approved the free-FQDN + live-execution path 2026-07-24. | 1.5.5 |
| 70 | 2026-07-24 | **Step 1.5.5 built + verified live on AKS staging (4/6 criteria met; soak + second-network pending).** `terraform apply` added the static public IP (`4.240.120.113`), ingress-nginx, and cert-manager over the existing cluster (2 add/1 in-place/2 replaced; the 1 in-place change was Azure nulling a default `upgrade_settings` block — no node replacement). **Build fixes found live:** (a) the first apply's helm `--wait` hit `context deadline exceeded` because both controllers were `Pending` — the single `B2s_v2` node was at **97% CPU requests** (staging + production stacks + 5 demo-worker pods); freed by scaling the production stack down (coordinator HPA min 2→1, dashboard/redis/postgres→0) and demo-worker 5→1 (all reversible — production restored by CD/helm), then re-applied clean. (b) `bootstrap-secrets.ps1`'s `openssl passwd -apr1` step fails under Windows PowerShell (openssl is on git-bash's PATH, not PowerShell's) — the `dashboard-basic-auth` htpasswd Secret was created from git-bash instead (`ponytail:` fix the script to locate git's openssl or use a portable hasher if this recurs). (c) `.env` lacked `DASHBOARD_USER/PASSWORD` (user had edited `.env.example`); generated an `operator` credential into `.env`. **Verified:** criteria 1/2/4/5 objectively met (public LE cert + SHA at `/health`; dashboard 401/200; cert reissue on secret-delete; register flood → 429 at edge). Criterion 3 (60-min soak) connection established through the ingress by a real external worker (local image built from current source, system-CA trust) and running from `13:47:18Z`; criterion 6 (mobile hotspot + corporate network) is user-owned. Worker code changed: `WORKER_CA_FILE=""` → system trust (`_ssl_context()`), so a worker off-network trusts the public cert with no dev-CA. **NOT marked DONE** — awaiting soak completion + criterion 6 + user approval. | Uninstall production entirely to free CPU (rejected — scaling down is reversible and keeps the release); give the smoke/controllers explicit tiny requests only (didn't solve the absolute CPU ceiling). | 1.5.5 |
| 71 | 2026-07-27 | **Step 1.5.6 design sub-gate:** observability = **lean self-hosted, $0** — `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics, one release) + `loki` (SingleBinary, filesystem, retention 72h) + `alloy` (DaemonSet, tails pod stdout JSON → Loki), all into an `observability` namespace via Terraform helm_releases (same install pattern as ingress-nginx/cert-manager). Coordinator gains a `/metrics` endpoint (prometheus-client) fed from existing fleet-state queries + a request-latency histogram; process CPU/mem come free from default collectors. **Alerts** = Prometheus rules (coordinator-down, DB/Redis-down, fleet-drop, cert-near-expiry) + a Grafana/Loki log-derived auth-spike alert authored at demo time; delivery to a **Discord/Slack webhook** (user choice) read by Alertmanager from a mounted secret file (URL never in state/values, §12). **Node headroom** (user choice): `node_count` 1→2 for this phase (observability doesn't fit alongside both full app stacks on one B2s_v2); still no autoscaling, `az aks stop` between sessions. Grafana reached via `kubectl port-forward` (no 2nd public IP). Auth-spike derived from existing `*_rejected` logs, not a new counter. | kube-prometheus-stack alone (no logs — fails correlation-ID/retention criteria); Azure Monitor/Container Insights/Managed Grafana (bills credit — same rejection as ACR/Key Vault/Log Analytics, Decision #41 family); full ELK for logs (memory hog vs Loki); keep node_count=1 + scale prod to 0 during demo (rejected by user in favour of a 2nd node); Alertmanager Slack-only via values-embedded URL (rejected — secret in state). User approved channel=webhook + node_count=2 on 2026-07-27. | 1.5.6 |
| 72 | 2026-07-27 | **Step 1.5.6 BUILT (not yet verified live).** Coordinator `/metrics` (`coordinator/app/metrics.py` + `MetricsMiddleware` + route in `main.py`; `prometheus-client` added to requirements) — `py_compile` OK. Terraform: `node_count` default 1→2, new `infra/terraform/observability.tf` (namespace + 3 helm_releases) + obs chart-version/retention/webhook-secret variables — `terraform fmt` + `terraform validate` PASS. Helm platform chart: `observability.enabled` flag (staging on / production off), `templates/servicemonitor.yaml` (scrape coordinator `/metrics` over self-signed HTTPS, insecureSkipVerify), `templates/prometheusrules.yaml` (the metric alerts), coordinator Service port named `https` + `app` label — `helm template` renders clean in staging, correctly absent in production. `bootstrap-secrets.ps1` extended to create `grafana-admin` + `alertmanager-webhook` in the observability ns from `.env`. **NOT verified live** — node stopped; needs cluster-up + `.env` vars (`GRAFANA_ADMIN_USER/PASSWORD`, `ALERTMANAGER_WEBHOOK_URL`) + a real Discord/Slack webhook + the apply-ordering (`-target` namespace → bootstrap → full apply) → then demo the 6 exit criteria (dashboard, correlation-ID trace, coordinator-down alert reaches phone, auth-spike alert, retention documented=72h, no-secrets-in-logs query). Chart values (kube-prometheus/loki v6/alloy River) UNVERIFIED-labeled in-file, confirm on first apply. | — | 1.5.6 |
| 73 | 2026-07-27 | **Step 1.5.6 stack applied LIVE + partially verified.** `apply-observability.ps1` created the observability namespace, secrets, node_count 1→2, and installed kube-prometheus-stack + Loki + Alloy — all pods Running. PR #8 merged to main; CI built the `/metrics` coordinator image at `bc2c02e59a54e23df95daaf8bc467ad5d8a77610`; CD deployed staging. **Objectively verified:** both staging coordinator replicas scraped `up=1` by Prometheus over self-signed HTTPS (`serviceMonitor/staging/coordinator`); Alertmanager→**Discord** delivery proven (built-in alerts arrived ~11:53). **Three live fixes:** (a) PowerShell 5.1 splits `terraform -target=X` on `=` → quote the whole flag; (b) helm `--wait` does NOT wait on the operator-created Alertmanager/Prometheus statefulsets, so the first apply reported "green" while Alertmanager was broken with `undefined receiver "null"` (the chart keeps a Watchdog child-route to a `null` receiver my `alertmanager.config` replaced) → added a `null` receiver + Watchdog route, re-apply's `helm upgrade` brought it `READY 1`; (c) CD staging smoke timed out because the coordinator Deployment sat at `0/0` — no `replicas` field (HPA-owned by design) + session-2's manual scale-to-0 + **HPA won't lift a workload off 0 replicas**, so `helm upgrade` kept 0 and CD rolled back → fixed with `kubectl scale --replicas=2` then CD re-run. **NOT DONE:** formal 6-criteria walkthrough + user approval pending; also quiet the noisy built-in kube-prometheus alerts (route only our PrometheusRule alerts to Discord). Node `az aks stop`'d after (billing $0). | Add a `replicas` default back to the Deployment to avoid the 0-replica trap (rejected — fights the HPA, the documented reason it was removed; the 0 only came from a manual credit-saving scale-down, not normal ops). | 1.5.6 |
| 75 | 2026-07-27 | **Step 1.5.7 design sub-gate + build (verified live, awaiting approval).** Two decisions. **(A2) Fit ≥3 replicas + autoscale headroom by raising the staging namespace quota via Terraform** (`limits.cpu` 2→3, `limits.memory` 2Gi→3Gi, `requests` 1→1.5 / 1Gi→1.5Gi) rather than shrinking the per-pod coordinator CPU limit — user chose A2 over A1 (keep the 300m limit, real infra change). The quota `for_each`es both namespaces; production is scaled down so its raised share is unused headroom; the true bound is now the 2-node × 1900m allocatable, not the quota. Applied with `terraform apply -target=kubernetes_resource_quota.env` (3 in-place, 0 destroy). **(B1) Generate the autoscale load with a synthetic in-cluster HTTP flood** (`infra/loadtest-coordinator.yaml`, a Job hammering `coordinator:8443/ready` with fresh-TLS connections) — chosen because **no task-distribution pipeline exists until M2**, so there is no real task load to drive CPU; hitting the in-cluster Service bypasses the ingress edge `limit-rps` (Step 1.5.5). Labelled an honest ceiling (§10): it proves the HPA *mechanic*, not production task throughput. Staging coordinator HPA raised min 2→3 / max 2→5 (`values-staging.yaml`, `helm upgrade` on SHA `b1963f90`). No application code changed — the phase verifies invariant §3.9, already built. All 6 exit criteria met live (see the 1.5.7 register row). **`terraform apply` and destructive `kubectl` are blocked from the agent by the auto-mode classifier — the user ran the quota apply by hand; pod-delete/scale ran via the agent's PowerShell against staging.** | A1 (shrink coordinator `limits.cpu` 300m→200m staging-only to fit 5 replicas under the old quota=2, no Terraform) — rejected by the user in favour of A2. B2 (drive CPU with many heartbeating workers) — rejected, can't reliably cross the 70% target. Raise HPA max beyond 5 — out of scope, bounded by the 2-node pool. | 1.5.7 |
| 76 | 2026-07-27 | **Step 1.5.8 design sub-gate.** Two decisions. **(A1) Non-Docker worker distribution = a plain bootstrap installer** (`worker/install-worker.sh` for Linux/macOS, `worker/install-worker.ps1` for Windows): clone the repo → build a venv → run the same `worker.py` the Docker image runs, configured by exactly the three values the phase mandates (`COORDINATOR_URL`, `ENROLLMENT_SECRET`, `WORKER_CA_FILE` empty = system trust). Chosen over a PyInstaller per-OS binary (build pipeline + AV false-positives) and a PyPI/pipx package (new publishing scope) — laziest path that adds no new dependency and stays transparent. This forced one root-cause code change: `worker.py`'s single-instance lock imported `fcntl` unconditionally (POSIX-only), which crashes a native Windows worker at import — made cross-platform (`fcntl`/`msvcrt`, `fcntl is not None` guard); the `/proc` CPU/mem reads already return `None` off-Linux, and paths are env-overridable, so the lock was the only non-portable spot. Verified by `tests/test_worker_lock.py`, which passes on this Windows host and therefore actually exercises the new `msvcrt` branch (a second in-process acquire → `SystemExit(1)`). **(B1) Enrollment credential = keep the single shared `ENROLLMENT_SECRET`** (Decision #9, spec-compliant §12): onboarding hands out the shared bootstrap secret; individual workers are revoked by worker-ID (`POST /workers/{id}/revoke`, already built, Decision #17); a leaked secret is handled by rotating the k8s secret + redeploy. Per-worker enrollment tokens (independently revocable at enrollment time) were weighed and deferred as scope creep for a 5-machine verification phase. `docs/onboarding-a-worker.md` documents all three onboarding paths + the operator issue/revoke/rotate procedure. **Built with the node stopped; the live 5-machine verification + dashboard evidence is the remaining work, done in a later node-up session. Not started without user go-ahead (§9).** | A2 PyInstaller binary (rejected — per-OS build + AV false-positives + big artifacts); A3 PyPI/pipx (rejected — new publishing scope); B2 per-worker enrollment tokens via a new admin endpoint (rejected — new endpoint+storage+flow, not justified for V1 onboarding). | 1.5.8 |
| 74 | 2026-07-27 | **Step 1.5.6 formally verified live + APPROVED DONE.** Walked all 6 exit criteria on AKS staging; the verification surfaced **two real gaps + the noise item**, all fixed in **PR #9** (merged to main; CI green incl. tests; CD deployed staging on SHA `b1963f90d53066a836248e11be231286893670aa`). **C2 gap:** WS heartbeat/ping/session log lines carried `correlation_id="-"` and the worker minted a fresh random id per envelope, so a worker *session* was not traceable by one id. Fix: worker mints one session correlation id per connection, sends it as `X-Correlation-ID` on the token-refresh HTTP call and in the hello/heartbeat/pong envelopes; coordinator binds `hello.correlation_id` to the WS coroutine's contextvar. **Verified:** session `beed3ee8` traced by one id across BOTH replicas (HTTP `access_token_issued` on `q4skv`, WS `worker_connected`+heartbeats on `jgbvf`) + the worker's own logs (§11 end-to-end). **C3 gap:** `CoordinatorDown` expr `max(up{service="coordinator"})==0` never fires when every replica is gone (the series disappears; an empty vector is not 0). Fix: `absent(up{...})==1 or max(up{...})==0`. **Verified:** scaled staging coordinator→0 → `absent(up)=1` → pending (`for:1m`) → **firing** at ~90s → Alertmanager `receiver=chat` (Discord), no notify errors → resolved on recovery. **Other criteria verified:** C1 Grafana fleet dashboard live (screenshot); C4 auth-spike LogQL alert fired on a 20-event register flood (screenshot); C5 Loki `retention_period: 72h` live; C6 no secret values in Loki (enrollment secret, passwords, Bearer tokens, webhook all absent). **#7 (quiet noise) — code merged but NOT applied live:** every platform alert now labelled `team: dcds`, Alertmanager default receiver → `null`, only `team=dcds` → chat (in `observability.tf` + `prometheusrules.yaml`). Pushing it needs a `terraform apply` on the observability release; the user's full apply was killed mid-session, so the route change is **committed but not live** — a loose end, not an exit criterion. Helper `infra/apply-alertmanager-route.ps1` (untracked) does the targeted apply. **User approved 1.5.6 DONE 2026-07-27.** Staging left at 2 coordinators + 2 demo-workers, node still running at approval time (user to `az aks stop`). | Keep C2 traceable by `worker_id` only (rejected — the criterion + §11 say "by correlation ID"); demonstrate C3 by breaking a scrape target instead of scaling to 0 (rejected — "killing the coordinator" means pods gone, which is exactly the absent-series case the fix covers). | 1.5.6 |
| 77 | 2026-07-27 | **Step 1.5.8 CPU/mem gap closed + APPROVED DONE (with a user scope waiver).** The last real gap from the 1.5.8 build was that native Windows/macOS workers reported `cpu_percent`/`memory_percent` as `None` — the readers parsed `/proc/stat`+`/proc/meminfo` (Linux only), so the §6 dashboard CPU/memory columns were blank for every non-Docker/non-Linux worker. **Fix (Decision #76's deferred item, resolved):** `worker/worker.py` `_read_cpu_percent()`/`_read_memory_percent()` now use `psutil` (`psutil.cpu_percent(interval=None)` / `psutil.virtual_memory().percent`), cross-platform Linux/Windows/macOS, deleting the `/proc` parsing; `psutil>=5.9,<7` added to `worker/requirements.txt` so the bootstrap installer and the published worker image carry it. **Verified on this native Windows host:** both readers return real values (cpu 44.4/37.5, mem 65.4 — previously `None`); worker test suite green (5 passed/1 skipped). Shipped through the pipeline as **PR #12** (`phase-1.5.8-worker-onboarding`→main). **User scope call (§9, honest per §10):** the user explicitly directed 1.5.8 be marked DONE after this fix and **not** to pursue the remaining checklist — so **VPS-in-another-country, mobile-hotspot, and the simultaneous-multi-worker "all on one dashboard with region-latency deltas" capture were WAIVED, not verified.** Machine types verified stand at 4 of 5 (laptop Docker #1, 2nd PC #2, native Windows no-Docker #3, friend's PC own ISP #4). Same user-discretion family as the 50-worker (#34–35) and 2-min-soak substitutions. **User approved 1.5.8 DONE 2026-07-27.** | Add per-OS `ctypes`/`GlobalMemoryStatusEx` branches instead of psutil (rejected — more code, Windows-only, misses macOS; psutil is ~6 lines and covers all three); actually connect VPS + hotspot before closing (declined by the user for this phase). | 1.5.8 |
| 78 | 2026-07-28 | **Pre-1.5.9 remediation: drift, fresh-clone, and a live credential exposure. PR #13 merged, deployed to both environments, M1.5 signed off.** Assessing whether M1.5 could close surfaced four things, all fixed and shipped on SHA `4575097157debe48d71b9f3aff86248c599d5b1e`. **(a) CREDENTIAL EXPOSURE — `SESSION_HANDOFF.md` held `ENROLLMENT_SECRET` and `DASHBOARD_PASSWORD` in plaintext across 6 occurrences, on a PUBLIC repo**, alongside the public coordinator FQDN and public worker image — everything needed to enroll a worker and sign into the dashboard (§12 violation). Both rotated, plaintext scrubbed, three manifests re-sealed. **Verified live: old enrollment secret → 401, new → 201; old dashboard password → 401, new → 200; anonymous → 401.** Old values remain in Git history and cannot be recalled — rotation, not deletion, is the remediation. **`GRAFANA_ADMIN_PASSWORD` and `POSTGRES_PASSWORD` were found exposed the same way (via `.env.example` history) and deliberately NOT rotated by user decision** — rotating Postgres needs a coordinated `ALTER USER` or the coordinator loses its DB connection; Grafana stores its admin user in its own DB. Both remain public and live, in-cluster only. **(b) FRESH-CLONE (§13) — cluster Secrets existed only in the gitignored `.env`**, so no other machine could stand an environment up. Nine Secrets now committed encrypted under `infra/sealed-secrets/`; **proven by cloning the branch to a temp dir and validating from the clone: 9/9 VALID**. Adoption of the pre-existing Secrets required the `sealedsecrets.bitnami.com/managed=true` annotation plus a controller restart (the controller refuses to take over Secrets it does not own, and logs "giving up" without retrying). Metadata is stripped before sealing — `kubectl get secret -o json` embeds a `last-applied-configuration` annotation containing the whole Secret in cleartext, and kubeseal copies annotations into the SealedSecret template. **(c) TERRAFORM DRIFT was undetectable** — `core.autocrlf=true` gave `.tf` files CRLF while state held LF, so three `helm_release` values diffed every line against identical text; the plan could never come clean, hiding the real #7 Alertmanager route drift. `.gitattributes` pins LF; node-pool `upgrade_settings` now declared to match Azure. **Plan went 4 changes → 1**, the survivor being the #7 route, left unapplied by user direction so Discord alerting stays as-is. **(d) `bootstrap-secrets.ps1` reported success after all 8 applies failed** — a native command failing in a PowerShell pipeline does not trip `$ErrorActionPreference="Stop"`. Both helpers now check `$LASTEXITCODE`; openssl resolution fixed with a Git-bundled fallback. Also tracked `demo-worker.yaml` (a live staging workload no IaC owned) and the three 1.5.6 evidence screenshots (each reviewed for credentials first). **Not verified:** that the old enrollment secret is still rejected *after* the CD redeploy — the cluster was stopped before that check completed. Indirect evidence is strong (all 9 SealedSecrets `synced=True` post-CD, and the Secrets are SealedSecret-owned so Helm cannot revert them), but it is inferred, not measured. | Rewrite Git history to purge the leaked credentials (rejected — force-pushing a public repo does not remove them from forks, clones, or caches; rotation is the only real remediation). Rotate Grafana/Postgres too (declined by the user). Delete and recreate the Secrets instead of annotating for adoption (rejected — leaves a window where the Secret does not exist). | 1.5.9 |

| 79 | 2026-07-28 | **Step 2.0 design gate (A) — task queue technology = PostgreSQL, `SELECT … FOR UPDATE SKIP LOCKED`.** The queue is the `task` table itself (`WHERE status='QUEUED' ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT n`), not a separate structure. | Redis Streams + consumer groups; Redis lists (`BRPOPLPUSH`); a dedicated broker (RabbitMQ/NATS/Kafka). | **Reliability was decisive and is independent of Redis durability.** (1) Redis today has **no persistence at all** — `infra/helm/platform/templates/redis.yaml` runs it with no PVC, deliberately, by Decision #39 ("losing it on restart is acceptable, workers reconnect and re-populate"). That premise holds for claims/registry/metrics/pub-sub, which workers rebuild on reconnect, and fails for a task queue, which nothing rebuilds. Pod reschedule is routine here — proven in 1.5.2 and 1.5.7 — so a Redis-resident queue would be lost on an ordinary reschedule, failing Step 2.2's "restarting all coordinator replicas loses no queued task". (2) **Even with AOF+PVC added, the dual-write remains** — a Redis queue entry and a Postgres task row cannot commit in one transaction, so a crash between the two writes loses or duplicates a task, against §3.7 (idempotency mandatory). Postgres makes dequeue + `QUEUED→ASSIGNED` + lease stamp **one transaction on one row**, so that failure mode cannot exist. (3) **M3 lease reclaim is plain SQL** (`UPDATE … WHERE lease_expiry < now()`) against columns 2.1 already reserves — no `XAUTOCLAIM` semantics and no in-flight structure to reconcile with the task table. (4) No reversal of a signed-off decision (#39) and no new storage cost on student credit; a broker adds a statefulset to a quota-tight 2-node B2s_v2, against the same cost discipline as #45/#46. **On speed, honestly:** Redis wins per-operation (in-memory, ~0.1ms vs a low-single-digit-ms `SKIP LOCKED` dequeue), but that margin lands entirely in headroom this system never uses — target scale is ~50 workers on dummy workloads, while `SKIP LOCKED` sustains thousands of dequeues/sec. End-to-end assignment latency is dominated by the WS push of Decision #80, which the queue technology does not touch, and enqueue is *fewer* network round trips under Postgres because there is no second store to write. The one genuine Postgres deficit is that it has no blocking pop (`XREADGROUP BLOCK`); it is mitigated by #80 making dequeue **event-driven rather than a poll loop** — the coordinator queries on a known trigger (task enqueued, or a worker frees a credit), not on a timer. **Stated as reasoning, not measurement:** the throughput and latency claims above are unmeasured on this deployment and must be verified by Step 2.2's 10,000-task criterion and the 2.8 load harness, not assumed. A `(status, priority, created_at)` index is mandatory. Dequeue stays behind a single function so that, if 2.8 measures the queue as the bottleneck, the swap is contained rather than a rewrite. | 2.0 |
| 80 | 2026-07-28 | **Step 2.0 design gate (B) — assignment = hybrid: worker-advertised capacity, coordinator push.** Worker declares `max_concurrent` at `hello` and emits a `capacity` message when a slot frees; the coordinator pushes a task only against a declared free credit. Delivery reuses the existing path — `POST /workers/{id}/push` → Redis pub/sub `worker:{worker_id}:push` → whichever replica holds that socket (`coordinator/app/main.py:617-627, 852-854`), built in 1.5 and proven at 3 replicas in 1.5.7. | Pure coordinator-push; pure worker-pull. | Pure push has **no backpressure** — the coordinator can overrun a worker. Pure pull has natural backpressure and no lost-push race, but **makes the worker the chooser of its own work**, which contradicts §3.3 (all intelligence in the coordinator) and would have to be torn out at M4, where rule-based adaptive scheduling requires the *coordinator* to decide which worker gets which task. The hybrid keeps assignment coordinator-authoritative from day one while borrowing pull's backpressure, and costs no new transport — new `message_type` values on a proven path. §3.2 is preserved: advertising a free slot is a worker stating a fact about itself, not a scheduling decision; the worker still never selects a task and never learns another worker exists. Credits also bound the blast radius of a worker dying mid-assignment to at most `max_concurrent` stranded tasks, which M3's lease reclaims. **Honest ceiling:** `max_concurrent` is worker-reported, and §12 says trust nothing worker-reported. In M2 assignment is naive, so the worst case is one greedy worker hogging tasks — recoverable and visible on the dashboard. A coordinator-side hard ceiling on credits is applied regardless of the worker's claim. Coordinator-observed capability trust is deferred to the M4 4.0 gate, as specified. | 2.0 |
| 81 | 2026-07-28 | **Step 2.0 design gate (C) — result storage = a separate `task_results` table** keyed by `task_id` (`payload JSONB`, `size_bytes`, `submitted_at`), with a **recommended** 64 KB hard cap per result and **recommended** 7-day retention for result bodies; task rows are kept indefinitely as the audit trail. | A `result` JSONB column on the task row; object/blob storage (Azure Storage). | The task table **is** the queue under #79, so every dequeue scans it; a fat JSONB result column bloats those pages and slows the hot path. Step 2.1's schema already specifies a "result *reference*", which implies the indirection. Object storage adds an Azure resource and burns student credit for results that are tiny in V1 (a count, a hash, an opaque blob) — same reasoning family as #45/#46 (no ACR, no Key Vault, no Storage account). The size cap exists because §12 treats every worker as untrusted: without it, one worker can bomb the shared database. Retention is enforced by a scheduled delete, not a manual step. **Labelled as recommendations, not measurements (§10):** both 64 KB and 7 days are starting values; the defensible numbers are whatever Step 2.8's load harness shows. | 2.0 |
| 82 | 2026-07-28 | **Step 2.0 design gate (D) — task-type extensibility = a coordinator-side type registry; the wire envelope does not change.** `protocol/envelope.py` stays byte-identical and `PROTOCOL_VERSION` stays `"1.0"`. M2 adds only new `message_type` values (`task_assign`, `task_accept`, `task_progress`, `task_result`, `capacity`). Task type travels as `payload.task_type` (string) with `payload.parameters` (object); the coordinator holds a registry mapping `task_type` → a pydantic parameter schema + validator. | A new envelope field for task type; a protocol version bump per task type; per-type message types. | This is exactly the extension mechanism Decision #6 specified — "`message_type` is a closed enum extended by later phases without changing the envelope shape" — so the Step 2.0 exit criterion "reuses the Phase 1 message envelope unchanged" is met literally, not approximately. Adding a fifth task type becomes one registry entry: no protocol change, no migration, and no change at all for workers that do not support it. Workers declare their supported types at `hello`; consistent with #80's ceiling, that declaration is treated as an **eligibility hint only** — the coordinator validates every result regardless of what a worker claimed it could do (§12). | 2.0 |

---

## Measured Benchmarks

Record only measured numbers here. Recommendations belong in phase docs.

| Metric | Value | Conditions | Phase measured |
|---|---|---|---|
| SUSPECT transition latency | 13.66s elapsed since last heartbeat | Local Docker, worker frozen via `docker pause` (socket kept open), threshold 12s | 1.6 |
| OFFLINE transition latency (heartbeat-miss path) | 28.68s elapsed since last heartbeat | Local Docker, worker frozen via `docker pause`, threshold 25s | 1.6 |
| OFFLINE transition latency (transport-disconnect path) | ~1s after `docker kill -s SIGKILL` | Local Docker — OS closes the socket immediately, bypassing the heartbeat sweep entirely | 1.6 |
| Ping/pong round-trip latency | 1.6–1.7 ms | Local Docker, loopback network (coordinator and worker containers on the same host) | 1.6 |
| Memory reading accuracy | Manual calc 11.0% vs worker-reported 11.3% | `/proc/meminfo` inside worker container, few minutes apart | 1.6 |
| Reconnect backoff delays observed | 0.13s, 0.19s, 0.13s, 3.9s, 12.89s at `consecutive_failures` 0–4 | Local Docker, coordinator stopped 12s forcing repeated failures; each delay a random draw within its documented cap | 1.7 |
| Session-conflict eviction latency | <20ms from new handshake to old session's `session_superseded` log line | Local Docker, raw WS client opening a duplicate session | 1.7 |
| Network-partition detection and recovery | SUSPECT at 14.5s elapsed; recovered to ONLINE within ~1s of connectivity returning | Local Docker, `docker network disconnect`/`connect` on the worker container, ~17s partition window | 1.7 |
| Coordinator CPU/mem at 1 worker | 0.83% CPU, 80.75MiB | Local Docker, `docker stats --no-stream`, steady state | 1.9 |
| Coordinator CPU/mem at 5 workers | 0.58% CPU, 80.63MiB | Local Docker, steady state | 1.9 |
| Coordinator CPU/mem at 10 workers | 28.98% CPU (registration burst) settling to 13.97% CPU within 15s; 80.69MiB | Local Docker — burst reading taken immediately after scale-up, steady-state reading taken 15s later; both recorded rather than only the flattering one | 1.9 |
| Coordinator CPU/mem at 50 workers | 11.64% CPU, 84.77MiB | Local Docker, measured 25s after scale-up (settled) | 1.9 |
| Postgres active connections | 2 (levels 1/5/10), 6 (level 50) | `SELECT count(*) FROM pg_stat_activity`, coordinator's own SQLAlchemy pool — not one connection per worker (workers never touch Postgres directly) | 1.9 |
| Redis instantaneous ops/sec | 0 (level 1), 3 (level 5), 14 (level 10), 24 (level 50) | `redis-cli INFO stats`, point-in-time sample at each level | 1.9 |
| Worker-ID collisions | 0 at every level tested (1/5/10/50) | `SELECT count(*), count(DISTINCT id) FROM workers` — totals matched distinct counts exactly every time | 1.9 |
| Dashboard-reported worker count vs DB count | Matched exactly at every level tested (1/5/10/50) | `GET /api/workers` array length vs Postgres row count | 1.9 |
| SIGKILL-to-OFFLINE latency (fresh-clone stack) | ~3s | Genuine fresh clone, single worker, `docker kill -s SIGKILL` | 1.10 |
| Duplicate-session eviction round trip | <200ms each way (raw client won at 11:15:08.306, evicted back at 11:15:08.485) | Genuine fresh clone, raw WS client vs real worker racing for the same worker ID | 1.10 |
| Coordinator restart with 50 workers connected — reconnect completion | 46/49 back ONLINE within 8s; all 49 back ONLINE within 18s (1 pre-existing QUARANTINED row correctly stayed disconnected) | Genuine fresh clone, `docker compose restart coordinator` with 50 scaled worker replicas live | 1.10 |
| Network partition detection and recovery (fresh-clone stack) | SUSPECT at 15s elapsed; recovered to ONLINE within ~9s of `docker network connect` | Genuine fresh clone, single worker, `docker network disconnect`/`connect` | 1.10 |

---

## Open Questions

| # | Question | Raised in | Blocking? | Resolution |
|---|---|---|---|---|
| 1 | Working tree had a large batch of previously-tracked files (coordinator/, README.md, docker-compose.yml, DECISIONS.md, SESSION_HANDOFF.md, pyproject.toml, etc.) showing as unstaged deletions in `git status`. `git show HEAD` revealed these were real prior-session progress (a coordinator skeleton that had reached that session's own "Step 4", with no design gate). | 1.0 | No — resolved | User chose to discard and build fresh under the current CLAUDE.md/PHASE_STATE.md process rather than recover the old code. Old commit remains in git history if ever needed. |
| 2 | Now that no paid cloud provider will host the system (Decision #52 — self-hosted coordinator + Cloudflare Tunnel for reachability), does Milestone 1.5 still run Kubernetes — on a local `k3d`/`kind` cluster (reviving superseded Decisions #42–43) — or does V1 stay on Docker Compose, making much of M1.5's Terraform/OKE/GKE-oriented phase content (1.5.1–1.5.2, parts of 1.5.5–1.5.7) obsolete or re-scoped? | 1.5.0 (re-opened) | No — resolved | **Resolved 2026-07-23: Option A — local Kubernetes via k3d** (real CNCF K8s on the user's PC, $0). V1 keeps Kubernetes; it runs locally instead of on a cloud host. Recorded as Decisions Log #53. Terraform's exact reduced scope is a Step 1.5.1 sub-decision. **Superseded 2026-07-24 by Decision #57** — compute host moved from local k3d to Azure AKS; V1 still runs Kubernetes, now on a managed Azure cluster. |
| 3 | Azure Step 1.5.1 sub-choices (raised by Decision #57): (a) does Terraform provision the AKS cluster itself via the `azurerm` provider (matches CLAUDE.md §4 "all cloud resources via Terraform, no console clicks") or does `az aks create` create it with Terraform staying thin (in-cluster only)? (b) which node VM size for the single node pool — `Standard_B2s` (2 vCPU / 4GB, cheapest viable) vs `Standard_B2ms` (2 vCPU / 8GB, more headroom)? | 1.5.0 (2026-07-24) | No — resolved | **Resolved 2026-07-24 (Decision #60):** (a) **Terraform owns the AKS cluster** via `azurerm`; (b) node **`Standard_B2s`** — but `Standard_B2s` proved unavailable in `centralindia` for the student subscription (only v2 B-series offered), so substituted to **`Standard_B2s_v2`** (2 vCPU / 8GB) at apply time, user-approved (Decision #62). Applied + verified live. |

---

## Deviations From Guardrails

Any departure from `CLAUDE.md` logged here with recorded approval.
Empty is the correct state.

| # | Guardrail | Deviation | Approved by | Date |
|---|---|---|---|---|
| — | — | (none) | — | — |

---

## Current Blockers

1. **Branch protection on `main`** (Phase 1.1 exit criterion) has not
   been configured — it's a GitHub repository setting (Settings →
   Branches), not something this session can set from the local
   working tree without a GitHub token/`gh` CLI (neither available
   here). Needs to be done manually, or delegated with explicit
   authorization.
2. **Azure prerequisites before the Step 1.5.1 rewrite (Decision #57).**
   Azure CLI v2.88.0 is installed (`C:\Program Files\Microsoft SDKs\Azure\CLI2`)
   but this session's shell PATH did not include it — a fresh terminal
   (or the full path) is needed. Terraform CLI (v1.15.8) and Helm are
   already installed from the k3d work. Still to do, in order, before any
   Azure resource exists:
   1. **`az login`** — interactive browser auth; the user must run this
      (an agent cannot complete the browser flow). Suggested:
      `! "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd" login`
   2. Confirm the **Azure for Students** subscription is active
      (`az account show`) and set as default.
   3. Register resource providers: `Microsoft.ContainerService`,
      `Microsoft.Compute`, `Microsoft.Network`.
   4. Resolve Open Questions #3 (Terraform-owns-cluster vs thin; node VM
      size), then rewrite `infra/terraform/` for `azurerm`.

   The prior "k3d/Terraform CLI not installed" and "GCP account
   restricted" blockers are **removed/superseded** — Decisions Log
   #50–#51 by #52–#53, and #52–#53 in turn by #57 (Azure).

Resolved: **CI runs green on a pull request** — GitHub MCP was
reconnected (user updated the token's permissions to include "Pull
requests: write"; `mcp__github__get_me` and `mcp__github__list_pull_requests`
confirmed connectivity first). Existing local commit `5003ad9` (already
on `main`, not yet on `origin`) was pushed on a new branch
`phase-1.1-ci-verification` and PR #1 opened against `main`:
https://github.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM/pull/1.
Verified via the GitHub Actions API (not just PR UI): both the `lint`
and `build` jobs on the `pull_request`-triggered run
(id `29898975760`) completed with `conclusion: success`. PR left open,
not merged — merging was not requested. Also confirmed the discarded
2026-07-21 coordinator skeleton (see Open Question #1) is not present
in this commit or PR — `5003ad9` itself already deleted `shared/`, the
old `coordinator/api/*` tree, and all `__pycache__` artifacts; verified
nothing was left over in the working tree either.

---

## Next Action

**Phase 1.0** — design decisions recorded (Decisions Log #1–#7):
transport, identity, language/runtime, database, message envelope, and
(added during 1.1) local dev TLS termination approach.

**Phase 1.1** — repository/environment/CI skeleton built and verified
directly, not just claimed:
- `docker compose build` — all three custom images (coordinator,
  dashboard, worker) built clean.
- `docker compose up` — all 5 containers (postgres, redis, coordinator,
  dashboard, worker) reached `healthy`.
- `curl` against `https://localhost:8443/health` and
  `https://localhost:8444/health` (and `/`) succeeded, cert chain
  validated against `certs/dev-ca.crt`.
- Worker heartbeat file confirmed updating inside the running container.
- `ruff check` (same command CI runs) passed clean.
- `certs/` and `.env` confirmed gitignored; `.gitignore` was fixed —
  it previously ignored `.env.example` (the safe template) instead of
  `.env` (the real secrets file), inherited from the discarded prior
  attempt.
- `bash scripts/teardown.sh` — full clean teardown confirmed
  (containers, network, volumes removed).

Not yet done, and not something this session can complete: branch
protection and an actual green GitHub Actions run (see Current
Blockers above) — both require pushing to `origin`, which has not been
done and should not happen without being asked.

**Note:** a local commit (`5003ad9 "starting with new technique"`) was
created during this phase's work by what appears to be a plugin-level
auto-commit hook (not a `git commit` this session issued directly — no
hooks are visible in `.claude/settings.json` or
`.claude/settings.local.json`, so likely one of the "6 hooks" loaded by
a plugin at session start). It is local-only, not pushed to `origin`.
Flagged for user awareness, not treated as approval to push.

**Phase 1.2** — coordinator given real persistence, verified directly
against every exit criterion in `docs/phase-1-reliable-worker-network.md`:
- Migrations run automatically on coordinator startup via Alembic
  (`upgrade head` in the FastAPI lifespan) and are idempotent — verified
  by recreating the coordinator container against an already-migrated
  database and confirming no error, `alembic_version` unchanged.
- `workers` table schema verified via `psql \d workers` matches the
  spec exactly: id, credential_hash, registration_metadata, agent_version,
  status, revoked, created_at, updated_at.
- Redis connectivity verified live via `/ready`'s `PING` check; key
  naming convention (`worker:{worker_id}:{field}`) and TTL policy
  documented in `coordinator/app/redis_client.py` (no keys are actually
  written yet — that starts Phase 1.5+, so no TTL numbers are invented
  here per the zero-hallucination rule).
- `/health` (liveness) vs `/ready` (readiness) verified as genuinely
  different: stopped the postgres container, confirmed `/ready` returned
  503 with `{"database": "error: ..."}` while `/health` stayed 200;
  restarted postgres, confirmed `/ready` recovered to 200.
- Logs are valid JSON with `correlation_id` present — but this needed a
  real fix, not just a claim: Alembic's `fileConfig()` (called inside
  the migration runner) defaults to `disable_existing_loggers=True`,
  which silently disabled the coordinator's own logger and reset the
  root logger's level/handler. Fixed by passing
  `disable_existing_loggers=False` in `migrations/env.py` and
  reasserting `configure_logging()` after migrations run in
  `app/main.py`. Verified after the fix: every app-emitted log line
  (`coordinator starting`, `migrations applied`, `readiness check`) is
  valid JSON with a `correlation_id`. Third-party library log lines
  (uvicorn's own startup/access lines, Alembic's internal migration
  progress lines) remain plain text — that's those libraries' own
  logging config, out of scope for "every service emits structured
  logs," which is about services this project builds.
- Two coordinator replicas against the same Postgres/Redis verified by
  running a second, independent container (`docker run`, port 8543)
  alongside the compose-managed one (port 8443): both reached healthy,
  both served `/ready` as 200 concurrently, `alembic_version` stayed a
  single unconflicted row throughout.
- `ruff check` passed; full teardown via `scripts/teardown.sh` confirmed
  clean afterward.

**Phases 1.0, 1.1, and 1.2 approved by user on 2026-07-22.** Status
updated from `AWAITING APPROVAL` to `DONE` in the Phase Register and
Snapshot above.

**Phase 1.3** — worker registration and identity — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md` (see that file for full
verification notes per criterion): fresh registration, same-ID reuse
across restart and container recreation, new ID after identity
deletion, a copied-identity duplicate rejected with a documented and
scope-limited heuristic (Decisions Log #11), registration flood rate
limiting, and invalid-credential rejection that never creates a worker
record — all exercised live against the running stack, not just
written and claimed. Also verified beyond the written checklist:
same-machine double-launch caught by an `fcntl` file lock. `ruff check`
passed before and after; full teardown via `scripts/teardown.sh`
confirmed clean afterward.

New design decisions recorded: enrollment credential mechanism,
credential hashing scheme, duplicate-claim heuristic and its known
limitation, and rate-limit algorithm (Decisions Log #9–#12).

**Phase 1.4** — authentication and token lifecycle — hit a real scope
conflict before any code was written: its exit criteria as originally
drafted assume a live connection (Step 1.5) and a dashboard (Step 1.8),
neither of which exists yet. Raised to the user rather than worked
around silently; user chose to build and verify the HTTP-only subset
now and explicitly defer the rest (Decisions Log #13). Built and
verified against the running stack: short-lived opaque access tokens
minted via `/workers/token/refresh` against the Phase 1.3
`worker_credential`, verified via `/workers/token/verify`; proactive
refresh at half the token TTL confirmed via worker log timestamps 30s
apart; expired, malformed, and replayed tokens each rejected with a
distinct logged event; revocation confirmed instant against both
existing enforcement points (register/reaffirm, token refresh) with the
DB `revoked` flag verified via `psql`; grepped all logs for the raw
credential and both raw tokens used in testing — zero matches;
`credential_hash` confirmed a 64-char HMAC-SHA256 digest, not
plaintext, via direct `psql` inspection. See
`docs/phase-1-reliable-worker-network.md` for the exact criterion-by-
criterion verification notes, including which half of each criterion
is deferred and why. New design decisions: Decisions Log #13–#17.
`ruff check` passed before and after; full teardown confirmed clean.

**Phases 1.3 and 1.4 approved by user on 2026-07-22.** Status updated
from `AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot
above.

**Phase 1.5** — persistent connection transport — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md`: a new `/ws/connect` WebSocket
endpoint (handshake: Bearer access token in the `Authorization` header,
then a `hello` envelope naming worker ID and protocol version, acked
with the assigned session epoch), a Redis connection registry, an
app-level ping/pong keepalive, an on-demand push endpoint, and a worker-
side async WebSocket client replacing the old polling loop. New design
decisions: Decisions Log #18–#22.

- **30-minute hold**: verified live — the connection established at
  `08:15:38Z` was still on the same `session_epoch` (4, no drops, no
  reconnects) at `08:47:30Z`, **31 minutes 52 seconds** later. Registry
  TTL confirmed still refreshing throughout (spot-checked mid-window: 90
  of 90 seconds remaining right after a ping/pong cycle; 76 remaining at
  the final check, both consistent with the 20s ping interval never
  having missed a cycle).
- **Coordinator push on demand**: verified — `POST
  /workers/{id}/push` with `message_type: "test_push"` returned
  `{"status": "published", ...}`; the connected worker's own log shows
  `ws_message_received` with that `message_type` less than a second
  later.
- **Version-mismatched worker rejected**: verified — a raw WebSocket
  client (run from inside the worker container, `websockets` library
  already present) sent a `hello` with `protocol_version: "99.9"`
  against a real access token; coordinator replied with an `error`
  envelope (`reason: "ws_protocol_version_mismatch"`) and closed with
  code 4001, logged with both the expected and received version.
- **Connection registry visible and accurate**: verified via
  `redis-cli GET`/`TTL` on `worker:{id}:connection` — contents
  (`session_epoch`, `coordinator_instance`, `connected_at`) matched the
  live session at every check performed, including immediately after a
  reconnect (new session epoch, new `connected_at`, previous entry gone).
- **Graceful coordinator shutdown drains connections; workers reconnect
  cleanly**: verified via `docker compose restart coordinator` — the
  worker's log shows a clean WS close (code 1012, "service restart"),
  logged as `ws_connection_lost`, followed by a fresh token refresh and
  a new session (`ws_connected`, incremented `session_epoch`) roughly 3
  seconds later, matching `WORKER_WS_RECONNECT_DELAY_SECONDS`. See
  Decisions Log #21 for why this is uvicorn's own per-connection
  shutdown handling plus the existing disconnect cleanup, not a
  dedicated app-level drain step — an initial app-level implementation
  was built, verified to never execute with anything to do, and deleted
  rather than left as misleading dead code.
- **Connection survives an idle period with no traffic — keepalive
  verified**: the entire 30-minute hold above involved zero application
  traffic beyond the ping/pong keepalive itself — this is the same
  evidence as the 30-minute criterion, not a separate test.

**A real bug was found and fixed during this phase's own verification**,
not left for later: the worker's reconnect loop
(`_run_ws_forever`) originally left the token-refresh call outside the
function's `try`/`except`. The first `docker compose restart
coordinator` test reconnected fine, but a second restart (coordinator
image rebuild + recreate) exposed it — the worker's HTTP client hit a
connection-refused window while the new container was still starting,
`_post`'s bare `except urllib.error.HTTPError` didn't catch the
resulting `urllib.error.URLError`, the exception propagated out of an
un-awaited `asyncio.create_task`, and the whole reconnect loop died
silently (the worker container stayed `healthy` throughout, because the
heartbeat-file loop is a separate task unaffected by this). Root-cause
fixed by moving the token refresh inside the same `try`/`except` as the
connection itself and adding `urllib.error.URLError` to the caught
exception types; re-verified live afterward — the next coordinator
restart reconnected cleanly in ~3 seconds. Documented in the worker
module docstring and inline comment, not just here.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.5 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.6** — heartbeat and liveness detection — built and verified
directly against every exit criterion in
`docs/phase-1-reliable-worker-network.md`: an application-level
`heartbeat` envelope sent over the existing Phase 1.5 socket every 5s
(sequence, uptime, agent version, CPU%, memory% — the last two read
from `/proc/stat`/`/proc/meminfo`, stdlib only, no new dependency),
coordinator-side latency measured off the existing ping/pong round
trip, a background liveness sweep independent of any single socket's
own transport timeout, and the full state machine (`REGISTERED ->
CONNECTING -> ONLINE -> SUSPECT -> OFFLINE`, plus `QUARANTINED`)
enforced through one status-transition helper that logs every move
with its trigger. New design decisions: Decisions Log #23–#27.

- **Heartbeats on schedule**: verified — `heartbeat_received` logged
  with an incrementing sequence roughly every 5s over several minutes.
- **Abrupt kill → SUSPECT → OFFLINE, timed**: verified two ways.
  `docker kill -s SIGKILL` closes the socket at the OS level
  immediately, so the coordinator saw a clean disconnect and went
  straight to `OFFLINE` in ~1s (correct, but bypasses the heartbeat
  state machine). `docker pause` (cgroup freezer, socket stays open)
  was used to exercise the heartbeat-miss path specifically: `SUSPECT`
  at 13.66s elapsed (threshold 12s), `OFFLINE` at 28.68s elapsed
  (threshold 25s), both logged with the trigger and elapsed seconds.
- **Skewed clock tracked correctly**: verified — a raw WS client sent a
  `heartbeat` with `timestamp: "2000-01-01T00:00:00Z"`; accepted
  normally, and the Redis-cached `last_heartbeat_at` reflected the
  coordinator's real receipt time, not the fake one.
- **CPU/memory accurate against the host**: verified — hand-computed
  `(1 - MemAvailable/MemTotal) * 100 ≈ 11.0%` from `/proc/meminfo`
  read directly in the container, matching the worker's own
  heartbeat-reported `11.3%` within normal drift.
- **Latency plausible**: verified for local-container only —
  `1.6`–`1.7 ms` round trip, consistent with loopback Docker
  networking. Differing-from-a-remote-machine half of this criterion
  is not yet testable (no worker outside local Docker exists until
  M1.5.8) — not claimed as done.
- **Every transition logged with its trigger**: verified across all
  seven transitions exercised this phase (see decisions log #27 for
  the one genuine bug found and fixed along the way — heartbeats on a
  socket opened *before* revocation were silently un-quarantining a
  worker; fixed by making `QUARANTINED` sticky, re-verified live).

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.6 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.7** — reconnection and session conflict — built and verified
against every exit criterion in
`docs/phase-1-reliable-worker-network.md` except one deliberately
deferred (see below): full-jitter exponential backoff replacing the
Phase 1.5 fixed 3s reconnect delay, and Redis-pub/sub-based session-
conflict eviction ensuring exactly one live session per worker ID at
all times. New design decisions: Decisions Log #28–#29.

- **Automatic reconnect after coordinator restart, no manual
  intervention**: verified — `docker compose stop coordinator` for 12s,
  then `start`; worker reconnected on its own.
- **Backoff intervals match policy**: verified — `consecutive_failures`
  0→1→2→3→4 logged with delays 0.13s/0.19s/0.13s/3.9s/12.89s, each
  inside its documented full-jitter cap.
- **Duplicate session → exactly one winner, loser terminated and
  logged**: verified, and more thoroughly than planned — a raw WS
  client opened a second session while the real worker's (epoch 1) was
  live; the real worker was evicted (`session_superseded` old=1 new=2),
  reconnected within its fast-reset backoff tier, and won back
  immediately (`session_superseded` old=2 new=3), evicting the raw
  client in turn. Exactly one authoritative session at every instant.
- **Session epoch increments and is visible**: verified —
  `worker:{id}:connection` in Redis showed `session_epoch: 3` matching
  the final winner.
- **Restarting the coordinator with 100 connected workers**: **deferred
  to Phase 1.9**, not tested here — see the phase doc's scope note.
  Scaling worker replicas today would just produce
  `duplicate_local_instance_detected` on N-1 of them, since every
  replica currently mounts the same named identity volume; giving each
  replica distinct identity storage is explicitly Phase 1.9's own exit
  criterion. Building that mechanism inside 1.7 would be scope creep
  into 1.9; faking it with a few manually-distinct containers wouldn't
  test what the criterion asks for at 100. Same kind of split as
  Decisions Log #13.
- **Network disconnect/reconnect restores the worker automatically**:
  verified via `docker network disconnect`/`connect` on the worker
  container (the standard proxy for "unplug the cable" in this
  environment, consistent with Phase 1.6's `docker kill`/`docker pause`
  proxies for other hardware failures). The Phase 1.6 heartbeat sweep
  caught the gap (`ONLINE→SUSPECT`, 14.5s elapsed) and recovered
  automatically the instant connectivity returned (`SUSPECT→ONLINE`,
  `heartbeat_received`) — no manual intervention, and the underlying
  socket itself never needed a fresh handshake to recover.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.7 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.8** — dashboard v1 — built and verified against every exit
criterion in `docs/phase-1-reliable-worker-network.md`: a new admin-
protected `GET /workers` on the coordinator merging the durable
Postgres row with Redis-cached metrics and connection registry, a
dashboard backend that proxies it server-side (the browser never sees
the admin secret), and a single-page live worker console. New design
decisions: Decisions Log #30–#32.

- **Design approach**: used the frontend-design skill rather than a
  generic table. Signature element is an animated hub-and-spoke glyph
  in the wordmark — a literal rendering of the star-topology invariant
  (CLAUDE.md §3.1), not decoration — set against a dark "radar/phosphor
  console" theme where the chroma comes from the five worker-state
  colors themselves (ONLINE/SUSPECT/OFFLINE/QUARANTINED/CONNECTING),
  deliberately avoiding the generic "dark background + one accent
  color" AI-default. A light theme is included via
  `prefers-color-scheme`. Reviewed visually via a local mock-data
  preview (Playwright couldn't trust the project's self-signed dev CA,
  so this was rendered separately over plain HTTP with synthetic
  multi-status data, same HTML/CSS/JS as shipped) — a real bug was
  caught this way: an invalid 4-digit hex color in the light theme
  (`--text-faint: #8299`) that silently broke footer text color; fixed
  before shipping.
- **Worker appears online within one heartbeat interval**: verified via
  `GET /api/workers` (the same endpoint the browser polls) — a fresh
  worker reached `ONLINE` within seconds of starting.
- **Killed worker turns offline within the documented timeout**:
  verified — `docker kill -s SIGKILL`, next `/api/workers` poll showed
  `OFFLINE` (near-instant, consistent with Phase 1.6's own finding that
  a hard kill closes the transport immediately).
- **Metrics update continuously**: verified — `sequence`, `cpu_percent`,
  `memory_percent`, `latency_ms`, `last_heartbeat_at` all changed across
  repeated polls of the same running worker.
- **Readable and responsive with 100 workers**: table layout verified
  responsive to a 390px mobile viewport (contained horizontal scroll,
  sticky header, tiles reflow) using a synthetic multi-status dataset —
  only a small number of real workers were available this session. The
  literal "100 workers" figure is Phase 1.9's own job, same scope split
  as the Phase 1.7 restart criterion — not fabricated here.
- **No credential or token rendered anywhere in the GUI**: verified —
  grepped the shipped HTML/JS for secret/credential/token, zero
  matches; the admin secret lives only in the dashboard backend's
  environment.
- **Closing and reopening the browser restores the live view**:
  structurally true — the page holds no client-side session state, a
  reload just re-fetches and renders fresh.

`ruff check` passed before and after every change in this phase; full
teardown via `scripts/teardown.sh` confirmed clean afterward.

**Phase 1.8 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register and Snapshot above.

**Phase 1.9** — scale simulation — **partially complete, left `IN
PROGRESS`, not `DONE` or `AWAITING APPROVAL`**. New
`docker-compose.scale.yml` override solves the shared-identity-volume
problem that Phases 1.7 and 1.8 had each deferred to this phase (new
Decisions Log #33). Levels 1, 5, 10, and 50 workers were run and
measured cleanly:

- All four levels started successfully via the single documented
  command (`docker compose -f docker-compose.yml -f
  docker-compose.scale.yml up -d --scale worker=N`).
- Zero worker-ID collisions at every level tested — verified by direct
  Postgres query, not assumed.
- Every worker appeared on the dashboard (`GET /api/workers` count
  matched the DB row count exactly) at every level tested.
- Coordinator resource usage recorded as measured values in the
  Measured Benchmarks table above, including a burst-vs-steady-state
  distinction at the 10-worker level (28.98% CPU immediately after
  scale-up, settling to 13.97% within 15s) rather than reporting only
  the more flattering number.

**Level 100 and its 10-minute no-heartbeat-loss soak were not
attempted.** All containers stopped simultaneously right after the
level-50 measurement. This was initially reported to the user as a
possible Docker Desktop resource-ceiling event (CLAUDE.md §16: a
benchmark-adjacent result contradicting the expected outcome is a
stop-and-ask trigger). The user then clarified they had stopped the
containers themselves, unaware a test was mid-run — **no technical
bottleneck was actually found**, and this correction is recorded here
rather than leaving the earlier, wrong read as a documented finding
(Decisions Log #34). The user accepted the 50-worker level as
sufficient for this session rather than continuing to 100.

`ruff check` passed before and after; teardown confirmed clean
afterward (twice — once after the unexpected stop, once at the end of
the session).

**Phase 1.9 approved by user on 2026-07-22** — explicit sign-off
accepting the 50-worker level as sufficient. Level 100 and its
10-minute no-heartbeat-loss soak were not run and are waived, not
silently skipped — documented above and in Decisions Log #34. Status
updated from `IN PROGRESS` to `DONE` in the Phase Register and Snapshot
above.

**Phase 1.10** — M1 demo and fresh-clone verification — run against a
genuine `git clone` of the local repository (not the working tree that
built every prior phase), in a scratch directory, following the
README's documented fresh-clone startup sequence exactly (`cp
.env.example .env`, `bash infra/dev-ca/generate-dev-ca.sh`, `docker
compose up --build`). Every exit criterion below was exercised live
against that clone, not the development working tree — a first for
this project, since all prior phases verified against the same
long-running dev stack.

**Prerequisite this step surfaced**: all work through Phase 1.9 was
uncommitted in the working tree (see `SESSION_HANDOFF.md`'s prior
"Open items"), which meant a real fresh clone at the start of this
step would have produced a coordinator missing most of its own code
(`config.py`, `db.py`, `models.py`, `security.py`,
`migrations/`, and more — the entire persistence/auth/transport layer).
Flagged to the user as a blocker rather than worked around silently;
user explicitly instructed the work be committed. Committed as a
single commit (`78277be`, "Phases 1.2-1.9: coordinator persistence,
auth, WS transport, heartbeat, reconnection, dashboard, scale
simulation") covering Phases 1.2 through 1.9 in one commit rather than
fabricating a granular per-phase history that was never actually kept
separate during development — not pushed to `origin` (2 local commits
now ahead; pushing was not requested).

**Demo, run against the fresh clone:**
- **Fresh clone, documented startup command**: verified — `docker
  compose build` then `up -d`, all 5 containers (postgres, redis,
  coordinator, dashboard, worker) reached `healthy`;
  `https://localhost:8443/health` → `{"status":"healthy"}`,
  `https://localhost:8443/ready` → `{"status":"ready", ...}`,
  `https://localhost:8444/` → 200.
- **Worker 1 appears online**: verified — the single worker started by
  `docker compose up` reached `status: "ONLINE"` in `GET /api/workers`
  within seconds, matching Phase 1.8's own verified behavior.
- **Worker 2 appears online**: verified — scaled to 2 replicas via
  `docker compose -f docker-compose.yml -f docker-compose.scale.yml up
  -d --scale worker=2`; the new replica reached `ONLINE` with a
  distinct worker ID.
- **Stop Worker 2 → offline within timeout**: verified — `docker stop`
  (graceful), `OFFLINE` confirmed in `GET /api/workers` at 27s elapsed
  (past the 25s documented threshold).
- **Restart Worker 2 → same worker ID, back online**: verified —
  `docker start` on the same (stopped, not removed) container; came
  back `ONLINE` with the identical worker ID and `created_at`
  timestamp, new `session_epoch`.
- **Scale to N workers → all appear** (**N=50, not 100** — see
  Decisions Log #35): verified — `--scale worker=50`; `GET
  /api/workers` showed 50 total rows (49 fresh `ONLINE` + the 1
  already-`QUARANTINED` row from the revocation test below, correctly
  still present and still quarantined); container count matched (49
  running worker containers, the 50th row being the quarantined one
  whose container was replaced during the scale reconciliation).

**Failure demo, run against the same fresh clone:**
- **`docker kill` a worker (not graceful) → timeout-driven offline**:
  verified — `docker kill -s SIGKILL` on worker 1, `OFFLINE` in
  `GET /api/workers` within 3s (transport-disconnect path, consistent
  with Phase 1.6's own finding that a hard kill bypasses the
  heartbeat-miss state machine).
- **Invalid enrollment credential → rejected, never appears online**:
  verified — `POST /workers/register` with a wrong `enrollment_secret`
  → 401 `{"detail":"invalid enrollment credential"}`, logged as
  `registration_rejected_invalid_credential`.
- **Revoke a live worker → disconnected within the bound, cannot
  reconnect**: verified — `POST /workers/{id}/revoke` with the admin
  secret → `{"status":"revoked"}`; worker's DB status flipped to
  `QUARANTINED` immediately (`revoked: true`). Consistent with
  Decisions Log #18, the live socket wasn't dropped instantly — a
  forced container restart was used to make the worker's own reaffirm
  attempt happen immediately, which was rejected (`reaffirm_rejected`,
  401 `"invalid identity"`); the worker then correctly failed to
  reconnect at all, leaving `connected: false`, status still
  `QUARANTINED` (sticky, per Decisions Log #27).
- **Force a duplicate session → one winner, loser terminated and
  logged**: verified using the same raw-WebSocket-client technique as
  Phase 1.7, run from inside the worker's own container (`docker exec`,
  `PYTHONPATH=/app` so the shared `protocol` package resolved) using
  its real credential read from `identity.json`. The raw client won
  the first race (`session_superseded`, old=3 new=4), then was itself
  evicted less than 200ms later when the real worker reconnected and
  won back (`session_superseded`, old=4 new=5) — exactly the same
  "exactly one winner at every instant" result as Phase 1.7's own
  verification, reproduced independently against the fresh clone.
- **Restart the coordinator with N workers connected → all return
  automatically** (**N=50, not 100** — see Decisions Log #35):
  verified — `docker compose restart coordinator` with 50 scaled
  worker replicas live; 46 of 49 connectable workers were back `ONLINE`
  within 8s, all 49 within 18s, with zero manual intervention. The
  1 already-`QUARANTINED` worker correctly stayed disconnected
  throughout (it's revoked; it isn't supposed to reconnect).
- **Disconnect a machine's network entirely and reconnect it →
  recovers with visible backoff in the logs**: verified — `docker
  network disconnect`/`connect` on a live worker's container; `SUSPECT`
  at 15s elapsed, recovered to `ONLINE` within ~9s of reconnecting the
  network, consistent with Phase 1.7's own finding that the underlying
  socket can recover without a fresh handshake.

**Structured logs present for every named category** (registration,
auth success, auth failure, connect, heartbeat gap, offline, reconnect,
session conflict): verified by grepping the coordinator's own log
stream from this fresh-clone run for each category's distinct event
name — `worker_registered`, `access_token_issued`,
`registration_rejected_invalid_credential`, `ws_authenticated` /
`ws_session_established`, `heartbeat_missed_suspect_threshold`,
`ws_disconnected`, `ws_session_established` (post-reconnect),
`session_superseded` — one real log line per category, not asserted.

`ruff check` (same command CI runs) passed clean against the working
repo. Full teardown via `scripts/teardown.sh` on the fresh-clone stack
confirmed clean afterward (containers, network, both volumes removed).

**Not yet done / known gaps, not silently claimed:**
- **CI green**: not verified on the latest commit. `ruff check` passes
  locally with the exact command the workflow runs, but the commit
  containing all of Phases 1.2–1.9's code (`78277be`) has not been
  pushed to `origin` — pushing was not requested this session. The
  same gap Phase 1.1 originally had for its own first commit (later
  resolved via PR #1, still open, for the older `5003ad9` commit only).
- **Worker ID stability across a real reinstall** (as opposed to
  restart/container-recreate, both of which are verified above and in
  Phase 1.3): not separately re-tested this step — "reinstall" isn't
  distinguishable from "container recreation" in this Docker-only
  environment; no separate installer mechanism exists yet to test
  against.
- **Screenshots/video**: not captured this session — no browser tool
  was available; all verification was done via the same `GET
  /api/workers` endpoint the browser polls, exactly as done in every
  prior phase (Phase 1.8 onward).

**Phase 1.10 approved by user on 2026-07-22.** Status updated from
`AWAITING APPROVAL` to `DONE` in the Phase Register above.

**Milestone 1 — Reliable Worker Network — is complete.** All ten
phases (1.0–1.10) are `DONE` and approved. Milestone Progress table
above updated: `DONE`, demo done, failure demo done, fresh clone
verified all `Yes` (Internet-tested remains `n/a` for M1 — that is
M1.5's own job, per the Milestone Progress table's own column
definition and CLAUDE.md §8).

Current milestone is now **M1.5 — Infrastructure & Deployment**,
starting at **Step 1.5.0 — Design gate — cloud topology and cost**,
`NOT STARTED`. Session ended here at the user's request; no M1.5 work
was started.

**Phase 1.5.0** — design gate — cloud topology and cost — all six
required decisions made and recorded (Decisions Log #36–41): cloud
provider, region strategy, Kubernetes distribution, database/Redis
hosting model, environment topology, and secret management. Driven by
a user-set hard constraint given directly in this session: **$0/month
cost ceiling**, testing scale of 5–7 workers, no region preference.

- **Cost ceiling**: $0/month, stated and accepted by the user before
  any resource is provisioned (satisfies this step's own exit
  criterion). Not a recommendation — an explicit user constraint.
- **Provider**: OCI (Oracle Cloud) Always Free tier — the only major
  cloud with a perpetual (not trial) free tier able to run real
  Kubernetes at $0: no OKE control-plane fee, free Ampere A1 compute
  (4 OCPU/24GB total). Real, flagged risk carried into Step 1.5.1: OCI
  Always Free Ampere A1 capacity is frequently unavailable
  ("out of host capacity") in popular regions — a provider-side
  constraint, not something this project controls. If no region has
  capacity when Step 1.5.1 attempts to provision, that is a blocker to
  surface to the user, not something to silently substitute a paid
  shape for.
- **Region**: not fixed — chosen at Step 1.5.1 apply-time based on
  which OCI home region actually has Ampere A1 capacity, nearest first.
- **Kubernetes**: OKE (managed control plane), not self-managed k3s.
- **Database/Redis**: self-hosted in-cluster (StatefulSets on Always
  Free block storage), not managed — no free managed tier exists on
  any provider. Known limitation recorded: no automated managed backup
  or failover; operator-managed only.
- **Environment topology**: one cluster, two namespaces (`staging`,
  `production`) rather than two clusters — the free compute quota
  cannot support two full clusters.
- **Secrets**: Kubernetes Secrets + `sealed-secrets` (free, encrypts
  secrets safely into Git), not OCI Vault (paid past a small free
  limit).
- **Teardown procedure**: `terraform destroy` against the OCI Terraform
  provider removes the OKE cluster, its node pool, and the VCN it
  runs in. To be written out in full as part of Step 1.5.1 (Terraform
  base infrastructure), per this step's own exit criterion that
  teardown be documented before provisioning — not yet provisioned, so
  not yet exercised.

**Phase 1.5.0 approved by user on 2026-07-22.** Status updated from
`NOT STARTED` to `DONE` in the Phase Register above. Current phase is
now **1.5.1 — Terraform base infrastructure**, `NOT STARTED`. Session
ended here at the user's explicit instruction ("do initial steps and
then stop, do not move further") — no Terraform code written, no OCI
resource created.

**Phase 1.5.0 was reopened the same day and re-decided.** Told every
major cloud (including OCI) requires a card on file for identity
verification even on a free tier, the user rejected the OCI-based plan
above outright and asked for a platform requiring no payment method at
all. Clarified via direct questions: no spare always-on machine exists
— only the user's main laptop, on only while testing. Revised plan
recorded as Decisions Log #42–46, **superseding #36–38** (#39–41
unchanged — they were never OCI-specific): k3d (k3s in Docker) on the
user's own laptop; Cloudflare Tunnel + Cloudflare DNS for public
reachability, no router ports opened; GitHub Container Registry
(ghcr.io) for images; Terraform Cloud free tier for remote state.
Explicitly recorded as an honest limitation, not hidden: the cluster
and its public reachability only exist while the laptop is on. **User
re-confirmed this revised plan on 2026-07-22.**

**Step 1.5.1 — Terraform base infrastructure — started, scaffolded,
not run.** `infra/terraform/{versions.tf,variables.tf,main.tf,README.md}`
written: Terraform Cloud remote-state backend block, Cloudflare
provider, a `cloudflare_zero_trust_tunnel_cloudflared` +
`_config` pair routing two hostnames to the coordinator/dashboard, and
two `cloudflare_record` CNAMEs. Explicitly marked in the code and its
README as **unverified** — the `terraform` CLI is not installed on
this machine, and none of the Terraform Cloud / Cloudflare accounts
this config depends on exist yet, so no `terraform init`, `validate`,
`plan`, or `apply` has run. Not claimed as tested; zero-hallucination
rule applies to infrastructure code exactly as it does to application
code.

**Blocking this step's actual exit criteria (`terraform plan`
producing no unexpected diff, `apply`/`destroy` verified, remote-state
locking tested)**, in order:
1. Terraform CLI installed on this laptop.
2. A Terraform Cloud account, organization, and workspace
   (`data-cleaning-distributed-system`), plus a login token.
3. A Cloudflare account with a real domain added to it, an API token
   scoped to that zone, and the account ID/zone ID.
4. Two hostnames chosen under that domain.

None of these can be created by this session unattended — items 2–3
need the user's own account signup (email/identity, even though no
card). Session ended here at the user's instruction ("proceed with the
next step then stop"); no further Terraform work attempted, nothing
applied.

---

## Session update — 2026-07-23 (cloud work parked)

No code or infrastructure was changed this session. The session
resolved the cloud direction and recorded a deliberate pause:

- **Provider decision changed OCI → GCP + GKE** (Decisions Log #50),
  chosen for the "test end-to-end cheaply now, migrate to a proper
  provider later" plan (GKE Autopilot $300 / 90-day credit,
  portability, no Ampere-A1 capacity lottery).
- **All Milestone 1.5 cloud provisioning and verification is deferred**
  (Decisions Log #51) because the user's GCP account is currently
  restricted by their employer (Current Blockers #2). Every M1.5 phase
  depends on a live cluster, so all are treated as INCOMPLETE / PENDING
  and none is marked DONE.
- **Resume plan** (when GCP access is restored): (1) rewrite
  `infra/terraform/` for the GCP provider + GKE Autopilot, keeping the
  Terraform Cloud backend and the provider-agnostic Decisions #39–41 /
  #45–46; (2) run Step 1.5.1 for real
  (`init`/`plan`/`apply`/`destroy`, remote-state locking); (3) proceed
  through 1.5.2–1.5.9, building and testing every cloud-dependent phase
  against the real GKE cluster in one focused pass.
- **Honest §8 consequence** recorded: from M1.5 onward the
  Internet-testing exit criterion applies to every phase, so later
  milestones (M2+) also cannot be fully completed until this same cloud
  access lands. M2+ *logic* can still be built and locally verified on
  Docker Compose in the meantime, but such phases would remain
  "locally verified, internet/cloud-verification pending" and must not
  be marked fully DONE.

Nothing committed or pushed this session. The two identical phase-state
files (`docs/PHASE_STATE.md` and `phase_state.md`) were both updated in
sync — this duplication is a documentation hazard worth resolving to a
single canonical file in a future session.

---

## Session update — 2026-07-23 (later): Cloudflare Tunnel adopted

Supersedes the "cloud work parked" note above. Clarified with the user
that the company's block is a **network MAC-filter on Google only**, not
an account restriction — so the earlier GCP-account framing (Decisions
#50–#51) was based on a misread and is now superseded by **Decision
#52**:

- **No paid cloud provider.** The coordinator stays self-hosted on local
  Docker Compose; **Cloudflare Tunnel** (`cloudflared` on the coordinator
  PC) provides the public / cross-network reachability §8 needs — free,
  no card, and reachable from the company network (Cloudflare ≠ Google).
- **The cloud-account blocker is gone.** Testing a worker on a different
  PC over a different network is now free and unblocked.
- **Cloudflare Tunnel setup** (given to the user, not yet run): install
  `cloudflared` on the coordinator PC (`winget install
  --id Cloudflare.cloudflared`) → `docker compose up` →
  `cloudflared tunnel --no-tls-verify --url https://localhost:8443` →
  prints a public `trycloudflare.com` URL → point a worker on another
  PC/network at `wss://<that-host>` (port 443, no dev-CA needed).
  Worker/test PCs install nothing.
- **One design question remains open** (Open Questions #2): whether M1.5
  still uses Kubernetes (local `k3d`/`kind`) or V1 stays on Docker
  Compose. To be decided at a re-opened Step 1.5.0 before Step 1.5.1.
- Still nothing committed or pushed this session; docs-only changes.

---

## Session update — 2026-07-23 (final): Option A chosen — local k3d + Cloudflare Tunnel

The re-opened Step 1.5.0 is closed. **Compute host = local Kubernetes
via k3d** (Decisions Log #53); **reachability = Cloudflare Tunnel**
(#52); **no paid cloud provider**. Open Questions #2 resolved. The MD
files are now updated and build-ready for **Step 1.5.1** — no code was
written this session, per user instruction.

Practical prerequisites before 1.5.1 (Current Blockers #2): install
`k3d` and the Terraform CLI, and create a Terraform Cloud workspace if
Terraform is kept for remote state. Step 1.5.1 opens with a short design
sub-gate on Terraform's reduced scope (there are no cloud resources to
provision anymore). Provider-agnostic Decisions #39–41 / #45–46 stand.
Still nothing committed or pushed; docs-only.

---

## Session update — 2026-07-24: moved to Microsoft Azure (AKS)

Supersedes the local-k3d + Cloudflare-Tunnel direction above. The user
switched the compute host **back to a real managed cloud — Microsoft
Azure, AKS** — using a newly available **Azure for Students**
subscription ($100 credit / 12 months / no credit card). Recorded as
**Decisions Log #57**, superseding #52–#53.

- **Why it's viable for the $0 ceiling:** Azure for Students bills
  against a finite credit, not a card — when the credit runs out
  resources stop rather than charging the user. Still requires active
  cost discipline (see below).
- **Cost discipline (mandatory, in Decision #57):** AKS Free-tier
  control plane ($0); one small B-series burstable node, no autoscaling;
  `az aks stop` between test sessions to halt compute billing;
  `az group delete` to remove everything. No ACR, no Key Vault, no Azure
  Storage backend — keep ghcr.io / sealed-secrets / Terraform Cloud.
- **Cloudflare Tunnel retired** — AKS has a real public LoadBalancer/IP.
- **Environment state:** Azure CLI v2.88.0 installed and verified;
  **not yet `az login`'d**; no subscription selected; no resource
  created. Terraform + Helm already installed from the k3d work.
- **Scrap-and-rebuild (Decision #58):** the user then directed that
  **all prior M1.5 build work be scrapped** and M1.5 redone from scratch
  under Azure, rather than incrementally ported. Steps 1.5.1 (thin k3d
  Terraform), 1.5.2 (Helm deploy on k3d) and 1.5.3 (CI pipeline,
  verified green on PR #2) are all reset to **NOT STARTED**.
- **Scrapped infra deleted (Decision #59):** the user then directed
  deleting the scrapped infra files to start M1.5 from the very
  beginning. **`infra/terraform/` and `infra/helm/` were removed** (16
  files; recoverable from git history commits `9052662` / `d072962`).
  **Kept `infra/dev-ca/`** (M1 Phase 1.1). **`.github/workflows/ci.yml`
  and `tests/` were NOT deleted** — `ci.yml` originated in M1 Phase 1.1
  and `tests/` are provider-agnostic §11 work; deleting either regresses
  an approved M1 deliverable, so both await an explicit user decision.
- **Open (Open Questions #3):** Terraform-owns-cluster vs thin, and node
  VM size — deferred to the Step 1.5.1 design sub-gate (recommendation
  on record: Terraform-owns-cluster + `Standard_B2s`).
- **This session:** doc changes to `phase_state.md` +
  `docs/PHASE_STATE.md`, plus deletion of the scrapped `infra/terraform/`
  and `infra/helm/` directories (their README went with them). No
  `az login`, no Azure provisioning. Nothing committed or pushed.
  Stopped here at the user's instruction.
