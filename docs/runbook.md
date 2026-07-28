# Operations Runbook

Deploy, rollback, scale, teardown, and the failure modes this project has
actually hit. `PHASE_STATE.md` remains the authority on phase status;
this file is operational only.

Everything here targets the Azure AKS deployment. For local Docker
Compose see `README.md`.

| Thing | Value |
|---|---|
| Resource group | `data-cleaning-distributed-system-rg` |
| Cluster | `data-cleaning-distributed-system` |
| Region | `centralindia` |
| Namespaces | `staging`, `production`, plus `observability`, `ingress-nginx`, `cert-manager`, `sealed-secrets` |
| Staging endpoint | `https://dcds-staging.centralindia.cloudapp.azure.com` |
| Images | `ghcr.io/muhammadhassanminhas/data-cleaning-distributed-system-{coordinator,dashboard,worker}`, tagged by full commit SHA |

Run all `az` / `kubectl` / `helm` / `terraform` commands from PowerShell
on Windows. Git Bash here is a minimal sandbox without those binaries on
its PATH.

---

## Start and stop the cluster (cost control)

The node pool is the only thing that costs money; the AKS control plane
is Free tier. Stop the nodes whenever you are not actively testing.

```powershell
az aks stop  -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
az aks start -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
az aks get-credentials -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
```

Check before you walk away — a cluster left running overnight is the
single easiest way to burn the credit:

```powershell
az aks show -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system --query powerState.code -o tsv
```

The public endpoint returns roughly 1–2 minutes after `az aks start`.
Deployments, PVCs, and Postgres data all survive a stop/start.

---

## Deploy

### Normal path — CD

Merge to `main`. CI builds and pushes SHA-tagged images; on CI success
`cd.yml` deploys that SHA to `staging` automatically, then holds
`production` at the GitHub Environment reviewer gate until approved.

CD deploys over the public AKS API and never starts or stops the node —
**if the cluster is stopped, CD fails loudly and spends nothing.** Start
the cluster first.

Verify a deploy landed:

```powershell
kubectl -n staging get pods
curl.exe -s https://dcds-staging.centralindia.cloudapp.azure.com/health   # returns the deployed SHA
```

### Manual path

```powershell
helm upgrade --install platform infra/helm/platform `
  -n staging -f infra/helm/platform/values-staging.yaml `
  --set coordinator.image.tag=<SHA> --set dashboard.image.tag=<SHA> --atomic
```

Always pass the image tags explicitly. Omitting them re-resolves the tag
from chart defaults and can silently roll the cluster back to an older
image.

---

## Rollback

CD rolls back automatically: if the post-deploy smoke check fails, the
job runs `helm rollback` before exiting. Manual rollback:

```powershell
helm history platform -n staging
helm rollback platform <REVISION> -n staging
```

Confirm with `/health` returning the expected SHA.

---

## Scale

**Coordinator replicas** are owned by the HPA, not by a `replicas` field
on the Deployment. Change `coordinator.hpa.minReplicas` / `maxReplicas`
in `infra/helm/platform/values-<env>.yaml` and deploy. Do not
`kubectl patch` the HPA — see the field-manager trap below.

```powershell
kubectl -n staging get hpa coordinator
```

**Cluster nodes**: change `node_count` in `infra/terraform/variables.tf`
and `terraform apply`.

**Namespace quotas**: `namespace_quota` in the same file. A deploy that
cannot schedule is usually quota, not capacity — check with
`kubectl -n staging describe quota`.

---

## Teardown

```powershell
# Remove the platform release only
helm uninstall platform -n staging

# Remove everything Terraform manages
cd infra/terraform ; terraform destroy

# Nuke the whole resource group regardless of Terraform state
az group delete -n data-cleaning-distributed-system-rg --yes
```

`terraform destroy` does not remove the HCP Terraform workspace or the
ghcr.io images. Local Compose teardown is `bash scripts/teardown.sh`.

---

## Known failure modes

These have all occurred. Each cost a debugging session.

**An HPA will not lift a workload off zero replicas.** If a Deployment
sits at `0/0` — usually from a manual scale-to-0 to save credit — a
`helm upgrade` keeps it at 0, no pod starts, the CD smoke check fails,
and CD rolls back. Fix:

```powershell
kubectl -n staging scale deploy/coordinator --replicas=2
```

then re-run the CD job.

**Server-side-apply field-manager conflicts.** `kubectl patch` or
`kubectl scale` against a Helm-managed object hands ownership of that
field to a non-Helm field manager, and the next `helm upgrade` fails with
a conflict. Cleanest fix is to delete the object so Helm recreates and
owns it:

```powershell
kubectl -n production delete hpa coordinator
```

Prefer changing values and running `helm upgrade` over ad-hoc patches.

**`helm --wait` does not wait on operator-created resources.** The
Prometheus and Alertmanager StatefulSets are created by the operator
*after* the Helm release reports success, so an apply can go green while
Alertmanager is still broken. Check the custom resource, not the exit
code:

```powershell
kubectl get alertmanager,prometheus -n observability
```

**PowerShell 5.1 splits `-target=...` on the `=`.** Quote the whole flag:

```powershell
terraform apply "-target=helm_release.kube_prometheus_stack"
```

**Line endings and Terraform drift.** `.gitattributes` pins `.tf`,
`.yaml`, and `.sh` to LF. Without it, Windows checks these out as CRLF
while Terraform state holds LF, and every line of the affected
`helm_release` values shows as changed with identical text on both sides
— a permanently non-empty plan that hides real drift. If `terraform plan`
ever shows large diffs where both sides look identical, check line
endings first.

**Querying a coordinator pod's admin API.** `/workers` needs the
`x-admin-secret` header. PowerShell quoting mangles inline Python, so
base64-encode the script:

```powershell
$code = "import base64;exec(base64.b64decode('<BASE64>').decode())"
kubectl exec -n staging deploy/coordinator -c coordinator -- python -c $code
```

---

## Health checks

```powershell
curl.exe -s https://dcds-staging.centralindia.cloudapp.azure.com/health   # deployed SHA
curl.exe -s https://dcds-staging.centralindia.cloudapp.azure.com/ready    # database + redis
kubectl -n staging get pods,hpa
kubectl -n observability get pods
```

`/health` is liveness and does not touch the database. `/ready` is
readiness and checks Postgres and Redis — so a database outage marks
pods NotReady and removes them from the Service without triggering a
restart loop.

Grafana and Prometheus are in-cluster only:

```powershell
kubectl -n observability port-forward svc/kube-prometheus-stack-grafana 3000:80
kubectl -n observability port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

---

## Task queue (Phase 2.2)

Queue depth, and the per-status lifecycle breakdown behind it:

```powershell
curl.exe -s -H "x-admin-secret: $env:ENROLLMENT_SECRET" `
  https://dcds-staging.centralindia.cloudapp.azure.com/tasks/depth
```

Also exported to Prometheus as `coordinator_tasks_queued`. Every replica
reports the same figure — collapse with `max by (...)` in queries, like
the other fleet gauges.

To load the queue or verify it end to end, run the versioned harness
**in-cluster**. The public ingress rate-limits to a few requests per
second (Step 1.5.5), so driving a drain through it measures nginx rather
than the queue:

```powershell
kubectl -n staging run queue-harness --rm -i --restart=Never `
  --image=python:3.12-slim `
  --env=COORDINATOR_URL=https://coordinator:8443 `
  --env=ADMIN_SECRET=$env:ENROLLMENT_SECRET `
  --command -- python - verify --count 10000 --dequeuers 3 --insecure `
  < scripts/queue_harness.py
```

`python -` reads the script from stdin and still receives the arguments
after it, so there is nothing to build or copy into the cluster.
`--insecure` covers the in-cluster hop to the self-signed pod cert, the
same hop nginx already makes; it is never needed against the public
endpoint. The output names which coordinator pod served each claim, and
`verify` decides pass/fail itself.

---

## Secrets

Cluster Secrets are committed encrypted under `infra/sealed-secrets/`
and decrypted in-cluster by the sealed-secrets controller:

```powershell
kubectl apply -f infra/sealed-secrets/
```

This is the normal path and it works from a fresh clone. Full detail,
including how to re-seal after a rotation and the metadata-stripping
step that keeps plaintext out of Git, is in
`infra/sealed-secrets/README.md`.

`infra/helm/bootstrap-secrets.ps1 -Namespace <env>` remains the way to
create Secrets from a plaintext `.env` — needed for local Docker Compose
work, and for bootstrapping a **new** cluster whose sealed-secrets
controller has a different key than the one these files are sealed
against. Every key in sections 3 and 4 of `.env.example` must be present
or the script throws.

Rotating a worker enrollment secret, and revoking or quarantining a
worker, are covered in `docs/onboarding-a-worker.md`.
