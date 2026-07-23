# Thin-Terraform inputs for the local k3d cluster (Decision #54).
# No cloud/OCI auth variables anymore — the k3d cluster is a documented
# prerequisite created by the k3d CLI, not a Terraform resource.

variable "cluster_name" {
  description = "k3d cluster name. Terraform targets the kube context k3d-<cluster_name>."
  type        = string
  default     = "data-cleaning-distributed-system"
}

variable "kube_config_path" {
  description = "Path to the kubeconfig k3d writes on cluster create."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Override the kube context. Empty = k3d-<cluster_name> (see locals in versions.tf)."
  type        = string
  default     = ""
}

variable "environments" {
  description = "Namespaces isolating staging from production in one cluster (Decision #40)."
  type        = list(string)
  default     = ["staging", "production"]
}

# Per-namespace resource quota. RECOMMENDATION, not measured — sized to
# comfortably hold one coordinator + dashboard + Postgres + Redis per
# environment on a laptop k3d node. Revisit if Step 1.5.2's real pods
# exceed it.
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
    requests_cpu    = "2"
    requests_memory = "2Gi"
    limits_cpu      = "4"
    limits_memory   = "4Gi"
    pods            = "30"
  }
}

# sealed-secrets controller (Decision #41) — encrypts secrets so the
# encrypted form is safe to commit. UNVERIFIED chart version.
variable "sealed_secrets_chart_version" {
  description = "sealed-secrets Helm chart version (bitnami.github.io/sealed-secrets repo). Verified present 2026-07-23."
  type        = string
  default     = "2.19.1"
}
