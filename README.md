# Distributed AI-Orchestrated SQL Database Cleaning Platform

**Version 1 — Distributed Worker Network.** See `CLAUDE.md` for the
guardrails governing this project and `PHASE_STATE.md` for current
status. Architecture reasoning and design decisions live in
`docs/phase-1-reliable-worker-network.md` and `PHASE_STATE.md`'s
Decisions Log — not repeated here.

**Current scope: Milestone 1 complete, Milestone 1.5 in progress.**
Workers register, authenticate, hold a persistent WebSocket session,
heartbeat, and reconnect; the dashboard shows the fleet live. The
platform runs on Azure AKS behind a public TLS ingress with CI/CD and an
observability stack. `PHASE_STATE.md` is the authoritative status — this
paragraph is a summary, not a substitute.

Two ways to run it, both documented below: **Docker Compose** for local
development, and **Azure AKS** for the deployed environments.

## Layout

```
coordinator/       FastAPI coordinator service
worker/            Worker agent + bootstrap installers (Docker and native)
dashboard/         Operator dashboard (live fleet view)
protocol/          Shared wire-protocol definitions (coordinator + worker)
infra/dev-ca/      Local dev CA / TLS certificate generation
infra/terraform/   Azure resource group, AKS, ingress, observability
infra/helm/        Platform chart (coordinator, dashboard, Postgres, Redis)
scripts/           Operational scripts (teardown, etc.)
docs/              Phase specs, runbook, worker onboarding
.github/workflows/ CI and CD pipelines
```

## Prerequisites

- Docker and Docker Compose
- OpenSSL (for local dev certificate generation)
- Bash (Git Bash on Windows is sufficient)

## Fresh-clone startup sequence

```bash
# 1. Configure environment
cp .env.example .env
# edit .env if you want non-default ports/credentials

# 2. Generate local dev TLS certificates (idempotent — safe to rerun)
bash infra/dev-ca/generate-dev-ca.sh

# 3. Build and start the stack
docker compose up --build
```

Once every container reports healthy (`docker compose ps`):

- Coordinator: https://localhost:8443/health
- Dashboard — fleet: https://localhost:8444/
- Dashboard — tasks: https://localhost:8444/ui/tasks
- Dashboard — recovery: https://localhost:8444/ui/recovery

Your browser will warn about the self-signed dev CA — that's expected.
Trust `certs/dev-ca.crt` locally to silence the warning, or click
through it; this CA is dev-only and never used outside your machine.

## Scaling workers

```bash
docker compose up --build --scale worker=5
```

## Teardown

```bash
bash scripts/teardown.sh
```

Stops and removes all containers, networks, and volumes. Generated
certs under `certs/` are left in place.

## Fresh-clone sequence — Azure AKS (Milestone 1.5)

The Compose sequence above is the local path. This is the deployed path,
from a clone to a running cluster.

### Additional prerequisites

- Azure CLI, logged in (`az login`) against a subscription that can
  create an AKS cluster
- Terraform CLI, and an HCP Terraform (app.terraform.io) account whose
  workspace is set to **LOCAL execution mode** — remote execution cannot
  use your local `az` credentials
- `kubectl` and `helm`

```bash
# 1. Secrets. Every key in section 3 and 4 of .env.example is required
#    by infra/helm/bootstrap-secrets.ps1, which throws on a missing key.
cp .env.example .env
# fill in every replace-me value

# 2. Register the Azure resource providers (once per subscription)
az provider register --namespace Microsoft.ContainerService
az provider register --namespace Microsoft.Compute
az provider register --namespace Microsoft.Network

# 3. Provision the cluster and in-cluster platform services
export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
export TF_TOKEN_app_terraform_io=<TF_API_TOKEN from .env>
export TF_CLOUD_ORGANIZATION=<TF_CLOUD_ORGANIZATION from .env>
cd infra/terraform && terraform init && terraform apply
```

On a from-nothing run the kubernetes/helm providers cannot configure
themselves against a cluster that does not exist yet. If the first apply
fails that way, stage it once:

```bash
terraform apply -target=azurerm_kubernetes_cluster.main
terraform apply
```

```powershell
# 4. Point kubectl at the new cluster
az aks get-credentials -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system

# 5. Create the Secrets the platform chart expects. These are committed
#    encrypted; the in-cluster sealed-secrets controller decrypts them.
kubectl apply -f infra/sealed-secrets/
```

The sealed Secrets are encrypted against **this project's** controller
key. Standing up an entirely new cluster gives you a new key, so seal
your own from `.env` instead — see `infra/sealed-secrets/README.md`:

```powershell
./infra/helm/bootstrap-secrets.ps1 -Namespace staging
./infra/helm/bootstrap-secrets.ps1 -Namespace production
```

```bash
# 6. Deploy the platform chart
helm upgrade --install platform infra/helm/platform \
  -n staging -f infra/helm/platform/values-staging.yaml --atomic
```

After the first manual deploy, CD takes over: a merge to `main` runs CI,
and on CI success `cd.yml` deploys the built SHA to staging
automatically and to production behind an approval gate. CD requires the
`ARM_*` and `TF_API_TOKEN` secrets to be configured on the GitHub
repository — a fresh fork will not have them.

Cost discipline, day-to-day operations, rollback, scaling, and teardown
are all in **`docs/runbook.md`**.

## CI

`.github/workflows/ci.yml` runs lint (ruff) and a Docker build on every
push and pull request. Branch protection requiring this check to pass
is configured in the GitHub repository settings, not in this repo —
enable it under Settings → Branches for `main` if not already set.
