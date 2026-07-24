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
  description = "Node count for the system pool. Single node, no autoscaling (Decision #57 cost discipline)."
  type        = number
  default     = 1
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
