# Inputs for Step 1.5.1 on Azure/AKS (Decision #57, sub-gate A + B2s).
# subscription_id is NOT a variable — azurerm reads ARM_SUBSCRIPTION_ID
# from the environment (see versions.tf).

variable "resource_group_name" {
  description = "Azure resource group holding all M1.5 resources. `terraform destroy` or `az group delete` on it removes everything."
  type        = string
  default     = "data-cleaning-distributed-system-rg"
}

variable "location" {
  description = "Azure region. RECOMMENDATION: a region near the user (centralindia) that has AKS Free tier + Standard_B2s. Confirm B-series vCPU quota for the Azure-for-Students subscription in this region at apply time; change if capacity/quota is short."
  type        = string
  default     = "centralindia"
}

variable "cluster_name" {
  description = "AKS cluster name."
  type        = string
  default     = "data-cleaning-distributed-system"
}

# Node size — sub-gate Open Questions #3(b) picked Standard_B2s (2 vCPU /
# 4GB). That v1 B-series is NOT offered in centralindia for the student
# subscription (only v2 B-series is), so substituted with the closest
# available burstable SKU: Standard_B2s_v2 (2 vCPU / 8GB), user-approved
# 2026-07-24. The extra RAM also pre-empts the 4GB-tight memory ceiling
# the sub-gate flagged for a single-node full stack.
variable "node_vm_size" {
  description = "VM size for the single AKS node pool."
  type        = string
  default     = "Standard_B2s_v2"
}

variable "node_count" {
  description = "Node count for the system pool. Raised 1->2 for Step 1.5.6 (user-approved 2026-07-27): observability (Prometheus/Grafana/Loki/Alloy) does not fit alongside both full app stacks on one B2s_v2. Still no autoscaling; `az aks stop` between sessions keeps the extra node's cost bounded. Dial back to 1 after M1.5 if credit is tight."
  type        = number
  default     = 2
}

variable "environments" {
  description = "Namespaces isolating staging from production in one cluster (Decision #40)."
  type        = list(string)
  default     = ["staging", "production"]
}

# Per-namespace resource quota. RECOMMENDATION, not measured — sized to
# hold one coordinator + dashboard + Postgres + Redis per environment.
# Note: a single Standard_B2s node is 2 vCPU / 4GB, so both namespaces'
# quotas together are a ceiling, not a reservation. Revisit against
# Step 1.5.2's real pods.
variable "namespace_quota" {
  description = "ResourceQuota applied to each environment namespace."
  type = object({
    requests_cpu    = string
    requests_memory = string
    limits_cpu      = string
    limits_memory   = string
    pods            = string
  })
  default = {
    requests_cpu    = "1"
    requests_memory = "1Gi"
    limits_cpu      = "2"
    limits_memory   = "2Gi"
    pods            = "30"
  }
}

# sealed-secrets controller (Decision #41). UNVERIFIED chart version —
# confirm on first `terraform init` against the Azure cluster.
variable "sealed_secrets_chart_version" {
  description = "sealed-secrets Helm chart version (bitnami.github.io/sealed-secrets repo)."
  type        = string
  default     = "2.19.1"
}

# --- Step 1.5.5 ingress inputs -----------------------------------------

# DNS label for the static public IP → <label>.<region>.cloudapp.azure.com.
# Must be unique within the region. This is the public hostname workers and
# the dashboard reach staging at.
variable "ingress_dns_label" {
  description = "Azure domain_name_label for the ingress public IP (region-unique)."
  type        = string
  default     = "dcds-staging"
}

# UNVERIFIED chart versions — confirm against the repos on first apply.
variable "ingress_nginx_chart_version" {
  description = "ingress-nginx Helm chart version (kubernetes.github.io/ingress-nginx)."
  type        = string
  default     = "4.11.3"
}

variable "cert_manager_chart_version" {
  description = "cert-manager Helm chart version (charts.jetstack.io)."
  type        = string
  default     = "v1.16.2"
}

# --- Step 1.5.6 observability inputs -----------------------------------
# UNVERIFIED chart versions — confirm against the repos on first apply
# (same discipline as the ingress chart versions above).

variable "kube_prometheus_stack_chart_version" {
  description = "kube-prometheus-stack Helm chart version (prometheus-community). Bundles Prometheus + Grafana + Alertmanager + node-exporter + kube-state-metrics in one release."
  type        = string
  default     = "65.5.1"
}

variable "loki_chart_version" {
  description = "grafana/loki Helm chart version. Deployed in SingleBinary mode (filesystem storage) for the single small cluster."
  type        = string
  default     = "6.18.0"
}

variable "alloy_chart_version" {
  description = "grafana/alloy Helm chart version. DaemonSet that tails pod stdout and ships to Loki."
  type        = string
  default     = "0.9.2"
}

variable "loki_retention_hours" {
  description = "Log retention window in Loki. RECOMMENDATION, not measured — short to bound disk on the single node. Documented log-retention value for Step 1.5.6 exit criterion."
  type        = number
  default     = 72
}

# Pre-created Secret (sealed-secret / bootstrap) holding the Alertmanager
# Discord/Slack webhook URL. The URL itself is NEVER in Terraform state or
# values (CLAUDE.md §12) — Alertmanager reads it from a mounted secret file.
variable "alertmanager_webhook_secret_name" {
  description = "Name of the Secret in the observability namespace holding key `webhook-url` (the Discord/Slack incoming webhook). Created out-of-band before apply."
  type        = string
  default     = "alertmanager-webhook"
}
