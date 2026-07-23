# Step 1.5.1 — Terraform base infrastructure (thin Terraform)

Status: **applied and verified against a local k3d cluster on
2026-07-23** — `terraform init` (HCP Terraform remote state),
`plan`, and `apply` all ran clean; `staging`, `production`, and
`sealed-secrets` namespaces plus the sealed-secrets controller exist in
the cluster. sealed-secrets chart repo is `https://bitnami.github.io/sealed-secrets`
(the old `bitnami-labs.github.io` URL now 404s).

## What changed (Decision #54)

Shifted OFF every cloud provider (OCI/OKE/GKE — Decisions Log #36–53) to
**local Kubernetes via k3d** (Decision #53) reached via **Cloudflare
Tunnel** (Decision #52). Consequence for Terraform's scope:

- **k3d CLI owns the cluster** (create/destroy). The cluster is a
  documented prerequisite, the same role a cloud account played before.
- **Terraform owns only in-cluster declarative state**: the `staging` /
  `production` namespaces (Decision #40), a per-namespace ResourceQuota,
  and the sealed-secrets controller (Decision #41) — via the
  `kubernetes` + `helm` providers, no cloud providers.
- **Remote state stays Terraform Cloud** (Decision #46 — never
  cloud-specific), which still gives the real state-locking this step's
  exit criteria require.

## Prerequisites before `terraform init`

1. **Install tooling** (both non-Google, unaffected by the company
   network block):
   ```
   winget install --id k3d.k3d
   winget install --id Hashicorp.Terraform
   ```
2. **Create the k3d cluster** (this is the "environment from nothing"
   the exit criteria mean, at the cluster level):
   ```
   k3d cluster create data-cleaning-distributed-system
   ```
   k3d writes the `k3d-data-cleaning-distributed-system` context into
   your default kubeconfig; Terraform targets that context.
3. **Terraform Cloud auth** — token lives in `.env` as `terraform_token`
   (gitignored, never committed). Export it before running Terraform,
   and set the org:
   ```
   export TF_TOKEN_app_terraform_io=<terraform_token from .env>
   export TF_CLOUD_ORGANIZATION=<your Terraform Cloud org>
   ```
   The token is **never** written into any `.tf` file.

## Run

```
cd infra/terraform
terraform init
terraform plan
terraform apply
kubectl get ns          # staging, production, sealed-secrets present
```

## Teardown

```
terraform destroy                                   # removes in-cluster resources
k3d cluster delete data-cleaning-distributed-system # removes the cluster
```

Documented before any `apply`, per this step's own exit criterion.
$0 cost — local only — so the "cost after 24h" criterion is n/a here.

## What Terraform does NOT manage

- **Postgres / Redis** — self-hosted in-cluster (Decision #39) as
  Kubernetes manifests in Step 1.5.2, not here.
- **Public ingress / TLS / DNS** — Cloudflare Tunnel (Decision #52) +
  Step 1.5.5, not Terraform.
- **Container images** — ghcr.io (Decision #45).
- **The k3d cluster itself** — k3d CLI, not Terraform (Decision #54).
