# Azure resources + in-cluster state for Step 1.5.1 (Decision #57,
# sub-gate A + Standard_B2s). One `terraform apply` goes from nothing to
# a running Free-tier AKS cluster with staging/production namespaces,
# per-namespace quotas, and the sealed-secrets controller.

locals {
  common_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "project"                      = "data-cleaning-distributed-system"
  }
  azure_tags = {
    project    = "data-cleaning-distributed-system"
    managed-by = "terraform"
    milestone  = "M1.5"
  }
}

# --- Cloud resources (azurerm, Decision #57 / sub-gate Option A) --------

resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.azure_tags
}

# Free-tier control plane ($0). Single system node pool, one B-series
# burstable node, NO autoscaling — the explicit cost discipline of
# Decision #57. `az aks stop` deallocates this node between test
# sessions; `az group delete` (or `terraform destroy`) removes everything.
resource "azurerm_kubernetes_cluster" "main" {
  name                = var.cluster_name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = var.cluster_name
  sku_tier            = "Free"

  # Azure's auto-generated node RG (MC_<rg>_<cluster>_<region>) is 84
  # chars — over the 80 max. Name it explicitly and short.
  node_resource_group = "${var.cluster_name}-nodes"

  default_node_pool {
    name       = "system"
    vm_size    = var.node_vm_size # Standard_B2s (sub-gate Open Questions #3)
    node_count = var.node_count   # single node, autoscaling left disabled
  }

  identity {
    type = "SystemAssigned"
  }

  tags = local.azure_tags
}

# --- In-cluster declarative state (kubernetes + helm) ------------------
# Carried over unchanged from the k3d build — provider-agnostic
# (Decisions #40, #41). Only the cluster they target changed.

# staging + production namespaces (Decision #40 — one cluster, namespace
# isolation, not two clusters).
resource "kubernetes_namespace" "env" {
  for_each = toset(var.environments)

  metadata {
    name = each.value
    labels = merge(local.common_labels, {
      "environment" = each.value
    })
  }
}

# Bound each environment's resource use so one can't starve the other on
# the single node.
resource "kubernetes_resource_quota" "env" {
  for_each = kubernetes_namespace.env

  metadata {
    name      = "environment-quota"
    namespace = each.value.metadata[0].name
    labels    = local.common_labels
  }

  spec {
    hard = {
      "requests.cpu"    = var.namespace_quota.requests_cpu
      "requests.memory" = var.namespace_quota.requests_memory
      "limits.cpu"      = var.namespace_quota.limits_cpu
      "limits.memory"   = var.namespace_quota.limits_memory
      "pods"            = var.namespace_quota.pods
    }
  }
}

# sealed-secrets controller (Decision #41) — installed once, cluster-wide,
# into its own namespace. Lets encrypted secrets be committed to Git
# safely (CLAUDE.md §12), at $0.
resource "kubernetes_namespace" "sealed_secrets" {
  metadata {
    name   = "sealed-secrets"
    labels = local.common_labels
  }
}

resource "helm_release" "sealed_secrets" {
  name       = "sealed-secrets"
  namespace  = kubernetes_namespace.sealed_secrets.metadata[0].name
  repository = "https://bitnami.github.io/sealed-secrets"
  chart      = "sealed-secrets"
  version    = var.sealed_secrets_chart_version
}

# --- Outputs -----------------------------------------------------------

output "resource_group" {
  description = "Azure resource group holding all M1.5 resources."
  value       = azurerm_resource_group.main.name
}

output "cluster_name" {
  description = "AKS cluster name (for `az aks stop/start`, `az aks get-credentials`)."
  value       = azurerm_kubernetes_cluster.main.name
}

output "namespaces" {
  description = "Environment namespaces created by Terraform."
  value       = [for ns in kubernetes_namespace.env : ns.metadata[0].name]
}
