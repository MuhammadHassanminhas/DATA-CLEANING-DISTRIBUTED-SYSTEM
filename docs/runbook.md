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

## Start and stop the cluster (daily cycle)

The node pool is the only thing that costs money; the AKS control plane
is Free tier.

**Run it on a daily rhythm: start once when you begin work, stop once
when you finish.** Earlier practice was to stop between individual test
sessions, which meant repeatedly waiting 3–5 minutes for nodes before any
live check. With the credit budget comfortable that traded real time for
very little money, so the cycle is now per-day rather than per-task
(Decision #88).

```powershell
# Beginning of the working day
az aks start -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
az aks get-credentials -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system

# End of the working day
az aks stop  -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
```

**Two rules that have both been broken in practice:**

1. **Stop it at the end of every day.** A per-day cycle is still a cycle.
   A cluster left running overnight is the single easiest way to burn the
   credit, and it has happened.
2. **Do not stop it while a CD run is in flight.** Any merge to `main`
   triggers CD; stopping the cluster underneath it fails the deploy with
   "AKS unreachable — likely `az aks stop`'d". That is the cluster-up
   guard behaving correctly, but it leaves a red run and a gap between
   `main` and the deployed SHA. Check the run finished, then stop.

Confirm the state either way:

```powershell
az aks show -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system --query powerState.code -o tsv
gh run list --workflow=cd.yml --limit 1
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

## Rotate the operator credential (`ADMIN_SECRET`)

`ADMIN_SECRET` guards every admin endpoint: `GET /workers`, the task
endpoints, revoke and push. It is **one shared secret with no per-user
identity** — the system can record that an operator acted, not which
human. So rotation is all-or-nothing: everyone who holds it is cut off at
the same moment and must be given the new value. There is no way to
revoke one person's access.

**Rotate when:** someone who held it leaves; you suspect it leaked; or on
whatever schedule you decide. Nothing rotates it automatically.

**Before you start:** this cuts off the dashboard and any harness run
mid-flight. Workers are unaffected — they hold `ENROLLMENT_SECRET`, a
different credential, and never see this one.

```powershell
# 1. New value. Keep it out of your shell history if that matters to you.
#    32 random bytes, base64url, no padding - 43 characters.
$b = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$new = [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_')
# The equivalent python one-liner is `python -c "import secrets;
# print(secrets.token_urlsafe(32))"`, but do NOT reach for it on the
# Windows operator host: `python` on PATH there is the WindowsApps stub,
# which opens the Store instead of running. Verified 2026-07-31.

# 2. Sanity check: it must NOT equal ENROLLMENT_SECRET, or you have
#    silently reverted the Step 2.2.1 separation.
$enroll = (Select-String -Path .env -Pattern '^ENROLLMENT_SECRET=' |
           ForEach-Object { $_.Line -replace '^ENROLLMENT_SECRET=','' }).Trim()
if ($new -eq $enroll) { throw "collision - generate again" }

# 3. Re-seal for both namespaces. Offline against the committed public
#    cert, so the cluster does not need to be running for this step.
foreach ($ns in @('staging','production')) {
  kubectl create secret generic admin-secret --namespace $ns `
      --from-literal=ADMIN_SECRET=$new --dry-run=client -o yaml |
    kubeseal --cert infra/sealed-secrets/pub-cert.pem --format yaml --scope strict |
    Out-File -FilePath "infra/sealed-secrets/$ns-admin-secret.yaml" -Encoding ascii
}

# 4. Update your own .env (gitignored) so local tooling keeps working.
(Get-Content .env) -replace '^ADMIN_SECRET=.*', "ADMIN_SECRET=$new" |
  Set-Content .env -Encoding ascii

# 5. Commit the re-sealed files. They are ciphertext; the plaintext is
#    never committed. Verify that before pushing:
git diff --cached | Select-String -SimpleMatch $new   # must print NOTHING

# 6. Apply, then restart so the pods pick up the new env value. A Secret
#    change does NOT restart pods on its own.
kubectl apply -f infra/sealed-secrets/staging-admin-secret.yaml
kubectl apply -f infra/sealed-secrets/production-admin-secret.yaml
foreach ($ns in @('staging','production')) {
  kubectl -n $ns rollout restart deploy/coordinator deploy/dashboard
  kubectl -n $ns rollout status  deploy/coordinator --timeout=5m
}
```

**Verify the rotation actually took** — do not trust the rollout alone:

```powershell
$b = "https://dcds-staging.centralindia.cloudapp.azure.com"
# Old value must now be rejected.
curl.exe -s -o /dev/null -w "%{http_code}`n" -H "x-admin-secret: $old" "$b/tasks/depth"   # expect 401
# New value must work.
curl.exe -s -o /dev/null -w "%{http_code}`n" -H "x-admin-secret: $new" "$b/tasks/depth"   # expect 200
# Workers must be unaffected — they never held this credential.
curl.exe -s -o /dev/null -w "%{http_code}`n" -X POST "$b/workers/register" `
  -H 'Content-Type: application/json' `
  -d "{`"enrollment_secret`":`"$enroll`",`"agent_version`":`"post-rotation`"}"           # expect 201
```

Then confirm no replica fell back to the shared secret:

```powershell
foreach ($ns in @('staging','production')) {
  foreach ($p in (@((kubectl -n $ns get pods -l app=coordinator -o name)) -replace '^pod/','')) {
    kubectl -n $ns exec $p -- python -c "import ssl,urllib.request;ctx=ssl._create_unverified_context();print([l for l in urllib.request.urlopen('https://localhost:8443/metrics',context=ctx).read().decode().splitlines() if l.startswith('coordinator_admin_credential_separate')][0])"
  }
}
```

Every replica must print `1.0`. A `0.0` means that pod is running on the
`ENROLLMENT_SECRET` fallback — every worker can call the admin endpoints
until it is fixed. The same pods log `admin_secret_fallback_in_use` at
WARNING on startup when that happens.

**If you cannot `exec` into a pod** (production is more locked down than
staging), two weaker checks together cover the same ground without ever
printing the credential — the decrypted Secret matches the value you
generated, and no replica logged the fallback:

```powershell
$b64 = kubectl -n production get secret admin-secret -o jsonpath='{.data.ADMIN_SECRET}'
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) -eq $new   # expect True
kubectl -n production logs -l app=coordinator --tail=400 |
  Select-String -SimpleMatch 'admin_secret_fallback_in_use'                   # expect nothing
```

**This procedure was exercised end to end on 2026-07-31** — both
namespaces re-sealed, applied, restarted and verified. It is no longer
untested.

**If `kubeseal` is not installed**, fetch the binary and seal offline —
no cluster round trip is needed, only the committed public cert:

```powershell
# https://github.com/bitnami-labs/sealed-secrets/releases
# kubeseal-<version>-windows-amd64.tar.gz -> kubeseal.exe
```

**Who to tell:** anyone who runs the harness, anyone using the dashboard
API directly, and any CI or automation holding the old value. The
dashboard *deployment* picks it up automatically from the Secret; a human
using `x-admin-secret` by hand does not.

### Where the caller came from

Admin endpoints log `client_ip` on both the success and the rejection
path — `tasks_enqueued`, `task_dequeued`, `worker_revoked`,
`push_published`, and every `*_rejected_invalid_admin_secret`. It is taken
from `X-Forwarded-For` (ingress-nginx sets it; `externalTrafficPolicy:
Local` preserves the real source IP) and falls back to the socket peer
for in-cluster callers.

**Treat it as a hint, never as identity.** The header is client-supplied
on the hop before the ingress, so it can be forged, and one shared
credential means it cannot tell you *who* acted. It is useful for exactly
one thing: noticing that an admin call — or a burst of rejected ones —
came from somewhere you do not recognise. Pair it with the Step 1.5.6
auth-spike alert, which fires on `*_rejected` events.

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
