# NEW_MACHINE_SETUP.md — resuming this project on a different PC

Everything that defines this project state lives in one of two places:
**git** (code, docs, encrypted secrets) or **Azure/Terraform Cloud**
(the running cluster and its remote state). Nothing is stored only on
this laptop. Moving machines means: clone the repo, point your tools at
the same existing cloud resources, and copy across the one file that's
deliberately never committed (`.env`).

**Goal of this doc: zero new Azure resources, zero new keys, zero
re-provisioning.** You are resuming the existing cluster, not building
a new one.

---

## 1. Install on the new machine

- Git, Docker Desktop, Bash (Git Bash on Windows is enough)
- Azure CLI (`az`)
- Terraform CLI
- `kubectl` and `helm`
- GitHub CLI (`gh`) — used to check PR/CI/workflow status
- Python 3.11+ (for `scripts/` and the test suite)
- OpenSSL (dev-CA cert generation)

## 2. Clone

```bash
git clone <this repo's URL>
cd Data_Cleaning_Distributed_System
git fetch origin
git checkout main          # or the in-progress docs/session-31-close branch, see step 6
```

All branches are already pushed to `origin` — nothing local-only is
being left behind on the old laptop (verified: every branch here tracks
an identical `origin/*` ref).

## 3. Bring over the one file git never has: `.env`

`.env` is gitignored on purpose (CLAUDE.md §12 — secrets never in git).
It holds the passwords/tokens the *existing* deployment already uses:
`POSTGRES_PASSWORD`, `ENROLLMENT_SECRET`, `CREDENTIAL_PEPPER`,
`ADMIN_SECRET`, `TF_API_TOKEN`, `TF_CLOUD_ORGANIZATION`,
`DASHBOARD_USER/PASSWORD`, `GRAFANA_ADMIN_PASSWORD`,
`ALERTMANAGER_WEBHOOK_URL`.

**Copy the actual `.env` file from the old laptop to the new one** over
a channel that isn't git/email/Slack — a password manager entry, an
encrypted USB drive, or `scp` between the two machines directly.

Do **not** regenerate any of these values and do **not** run
`.env.example` → new random secrets. New secrets would stop matching
what's already deployed in the cluster (Postgres, worker enrollment,
the dashboard) and force a re-deploy just to re-sync them. Reusing the
file is the entire point of skipping "new keys."

`infra/sealed-secrets/*.yaml` need **no action** — they're already
committed (encrypted) and decrypt against the existing cluster's
sealed-secrets controller key, which never leaves that cluster.

## 4. Point the new machine at the existing cloud resources

```bash
# Azure — logs into the same subscription, creates nothing
az login

# kubectl — fetches credentials for the EXISTING cluster, does not create one
az aks get-credentials -g data-cleaning-distributed-system-rg \
  -n data-cleaning-distributed-system --overwrite-existing

# Terraform — connects to the EXISTING remote state in Terraform Cloud
export TF_TOKEN_app_terraform_io=<TF_API_TOKEN from .env>
export TF_CLOUD_ORGANIZATION=<TF_CLOUD_ORGANIZATION from .env>
cd infra/terraform && terraform init
cd ../..

# gh CLI — for PR/CI/workflow status
gh auth login
```

`terraform init` just syncs local Terraform to the state that already
exists remotely. **Do not run `terraform apply`** unless you're
intentionally changing infrastructure — the cluster, ingress, and
observability stack already exist; apply is not how you "resume" them.

## 5. Confirm you're looking at the existing cluster, not a new one

```bash
az aks show -g data-cleaning-distributed-system-rg \
  -n data-cleaning-distributed-system --query provisioningState -o tsv
kubectl get nodes
```

The last session (2026-08-06) **stopped the cluster to avoid billing
while idle**. If `provisioningState` is `Stopped` / nodes don't list:

```bash
az aks start -g data-cleaning-distributed-system-rg \
  -n data-cleaning-distributed-system
```

## 6. Read the resume trail, in this order

1. `CLAUDE.md` — guardrails, don't skip §9 (stop-and-wait between
   phases) or §16 (escalation triggers).
2. `PHASE_STATE.md` — authoritative status and decisions log.
3. `SESSION_HANDOFF.md` — top of file, section **"⇒ START HERE NEXT
   SESSION"** — the literal next actions, written at the end of the
   last session.

As of the last session (31): Step 3.8 is merged/deployed/green in CI;
Step 3.9 is NOT started and needs explicit go-ahead before beginning.

## 7. Local Docker Compose dev loop (optional, unrelated to AKS)

```bash
cp .env.example .env   # skip if you copied the real .env in step 3
bash infra/dev-ca/generate-dev-ca.sh
docker compose up --build
```

## Explicitly do NOT do on the new machine

- Don't run `terraform apply` "just to make sure" — it's not needed to
  resume and risks an unintended infra diff.
- Don't regenerate `ENROLLMENT_SECRET`, `ADMIN_SECRET`, `CREDENTIAL_PEPPER`,
  or `TF_API_TOKEN` — reuse the copied `.env`.
- Don't re-run `infra/helm/bootstrap-secrets.ps1` / re-seal secrets —
  that mints secrets against a *new* controller key and only applies
  when standing up a cluster from scratch (see `README.md`'s
  from-nothing AKS sequence, which this doc is deliberately not).
- Don't create a new AKS cluster, resource group, or Terraform Cloud
  workspace — the existing ones are what steps 4-5 connect to.
