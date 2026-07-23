# In-cluster resources Terraform owns for the local k3d cluster
# (Decision #54). The cluster itself is created by the k3d CLI (see
# README) — Terraform starts from an existing, reachable cluster.
#
# UNVERIFIED: not yet through `terraform init/validate/plan/apply` — no
# terraform CLI installed. Attribute names/chart version are a draft.

# Common labels so every Terraform-managed object is attributable
# (the local-cluster equivalent of cloud resource tagging).
locals {
  common_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "project"                      = "data-cleaning-distributed-system"
  }
}

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
# a single laptop node.
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

output "namespaces" {
  description = "Environment namespaces created by Terraform."
  value       = [for ns in kubernetes_namespace.env : ns.metadata[0].name]
}

output "cluster_context" {
  description = "kube context Terraform is managing."
  value       = local.kube_context
}
