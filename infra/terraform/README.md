# Step 1.5.1 — Terraform base infrastructure (Microsoft Azure / AKS)

Status: **written, NOT yet applied.** Files drafted 2026-07-24 under
Decision #57 (Azure) and the Open Questions #3 sub-gate resolution
(Option A "Terraform owns the cluster" + node size `Standard_B2s`). No
`terraform init/plan/apply` has run against Azure yet — provider
versions and the sealed-secrets chart version are UNVERIFIED drafts.

## What this provisions (Decision #57, sub-gate A + B2s)

One `terraform apply` goes from nothing to:

- **`azurerm_resource_group`** — one RG holding everything.
- **`azurerm_kubernetes_cluster`** — **Free-tier** control plane ($0),
  a single **`Standard_B2s`** (2 vCPU / 4GB) node, **no autoscaling**
  (the explicit cost discipline of Decision #57).
- **In-cluster** (kubernetes + helm providers, pointed at the AKS cluster
  just created): `staging` / `production` namespaces (Decision #40), a
  per-namespace ResourceQuota, and the sealed-secrets controller
  (Decision #41).

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
   export TF_TOKEN_app_terraform_io=<terraform_token from .env>
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

- Free control plane = **$0**; the only compute cost is the single
  `Standard_B2s` node while it runs.
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

- **Postgres / Redis** — self-hosted in-cluster (Decision #39) as the
  Helm chart in Step 1.5.2, not here.
- **Public ingress / TLS / DNS** — Step 1.5.5 (real Azure
  LoadBalancer/IP; Cloudflare Tunnel retired, Decision #57).
- **Container images** — ghcr.io (Decision #45).
- **Azure Container Registry / Key Vault / Storage backend** — NOT used
  (Decision #57 — each would burn student credit for no benefit).
