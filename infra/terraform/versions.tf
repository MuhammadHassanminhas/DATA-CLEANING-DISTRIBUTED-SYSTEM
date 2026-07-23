# Step 1.5.1 — Terraform base infrastructure (thin Terraform, Decision #54).
#
# Shift OFF every cloud provider (OCI/GKE/OKE, Decisions Log #36-53) to
# LOCAL Kubernetes via k3d (Decision #53) + Cloudflare Tunnel for
# reachability (Decision #52). Terraform no longer provisions cloud
# resources: the k3d CLI owns cluster lifecycle; Terraform owns only the
# declarative in-cluster state (namespaces, quotas, sealed-secrets)
# through the kubernetes + helm providers.
#
# Remote state stays Terraform Cloud (Decision #46 — never cloud-
# specific). Auth token is read from the environment at apply time
# (TF_TOKEN_app_terraform_io), sourced from .env — NEVER hardcoded here,
# NEVER committed. Organization comes from TF_CLOUD_ORGANIZATION.
#
# UNVERIFIED: no `terraform` CLI installed yet — provider version
# constraints below are a draft. Cross-check on the first `terraform
# init` before trusting them.

terraform {
  required_version = ">= 1.7.0"

  cloud {
    # organization sourced from TF_CLOUD_ORGANIZATION env var.
    workspaces {
      name = "data-cleaning-distributed-system"
    }
  }

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.31"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
  }
}

locals {
  # k3d writes this context into the default kubeconfig on cluster create.
  kube_context = coalesce(var.kube_context, "k3d-${var.cluster_name}")
}

provider "kubernetes" {
  config_path    = var.kube_config_path
  config_context = local.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kube_config_path
    config_context = local.kube_context
  }
}
