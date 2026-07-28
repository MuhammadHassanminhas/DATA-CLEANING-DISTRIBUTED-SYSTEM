# Step 1.5.1 — Terraform base infrastructure (Microsoft Azure / AKS)

Status: **applied and live.** Written 2026-07-24 under Decision #57
(Azure) and the Open Questions #3 sub-gate resolution (Option A
"Terraform owns the cluster"). Applied and verified live against Azure in
Step 1.5.1, and extended since by Steps 1.5.5 (ingress) and 1.5.6
(observability).

Node size is **`Standard_B2s_v2`**, not the `Standard_B2s` originally
chosen — a forced availability substitution recorded in Decision #62.

## What this provisions

One `terraform apply` goes from nothing to:

- **`azurerm_resource_group`** — one RG holding everything.
- **`azurerm_kubernetes_cluster`** — **Free-tier** control plane ($0),
  `Standard_B2s_v2` (2 vCPU / 8GB) nodes, **no autoscaling** (the
  explicit cost discipline of Decision #57). `node_count` defaults to
  **2**, raised from 1 in Step 1.5.6 to fit the observability stack.
- **In-cluster** (kubernetes + helm providers, pointed at the AKS cluster
  just created): `staging` / `production` namespaces (Decision #40), a
  per-namespace ResourceQuota, and the sealed-secrets controller
  (Decision #41).
- **Public ingress** (`ingress.tf`, Step 1.5.5): a static public IP with
  a free `*.cloudapp.azure.com` FQDN, ingress-nginx, and cert-manager
  issuing Let's Encrypt certificates over HTTP-01.
- **Observability** (`observability.tf`, Step 1.5.6): kube-prometheus-stack
  (Prometheus, Grafana, Alertmanager), Loki, and Alloy.

Remote state stays **Terraform Cloud** (Decision #46) — never
cloud-specific; still gives the real state-locking this step's exit
criteria require.

## Prerequisites before `terraform init`

1. **Azure CLI logged in** (done): `az login`, `az account show` shows
   the *Azure for Students* subscription as default.
2. **Register resource providers** (once per subscription):
   ```
   az provider register --namespace Microsoft.ContainerService
   az provider register --namespace Microsoft.Compute
   az provider register --namespace Microsoft.Network
   ```
3. **Environment variables** (nothing secret is written into any `.tf`):
   ```
   export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)
   export TF_TOKEN_app_terraform_io=<TF_API_TOKEN from .env>
   export TF_CLOUD_ORGANIZATION=<your Terraform Cloud org>
   ```
   The Terraform Cloud **workspace must run in LOCAL execution mode** so
   the azurerm provider can use the local `az` CLI credentials. A
   remote-execution workspace would instead need a service principal
   (`ARM_CLIENT_ID`/`ARM_CLIENT_SECRET`/`ARM_TENANT_ID`).

## Run

```
cd infra/terraform
terraform init
terraform plan
terraform apply
```

If the first apply errors with a provider-configuration-unknown message
(the kubernetes/helm providers read their config from the cluster that
doesn't exist yet on a from-nothing run), do a staged apply once:

```
terraform apply -target=azurerm_kubernetes_cluster.main
terraform apply
```

Then point kubectl at the cluster:

```
az aks get-credentials -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
kubectl get ns          # staging, production, sealed-secrets present
```

## Cost discipline (Decision #57)

- Free control plane = **$0**; the only compute cost is the
  `Standard_B2s_v2` node pool (`node_count`, currently 2) while it runs.
- **Stop the node between test sessions** (billing halts):
  ```
  az aks stop  -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
  az aks start -g data-cleaning-distributed-system-rg -n data-cleaning-distributed-system
  ```
- **Nuke everything** when done:
  ```
  terraform destroy
  # or, to remove the whole RG regardless of Terraform state:
  az group delete -n data-cleaning-distributed-system-rg --yes
  ```

## What Terraform does NOT manage

- **Postgres / Redis / coordinator / dashboard** — the platform Helm
  chart in `infra/helm/platform` (Decision #39), deployed by CD or by
  `helm upgrade`, not by Terraform.
- **Cluster Secrets** — Terraform installs the sealed-secrets
  controller, but the Secrets themselves live encrypted in
  `infra/sealed-secrets/` and are applied with `kubectl apply -f`.
  See `infra/sealed-secrets/README.md`.
- **Container images** — ghcr.io (Decision #45).
- **Azure Container Registry / Key Vault / Storage backend** — NOT used
  (Decision #57 — each would burn student credit for no benefit).
