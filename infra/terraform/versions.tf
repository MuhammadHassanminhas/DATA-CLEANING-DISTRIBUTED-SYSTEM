# Step 1.5.1 — Terraform base infrastructure on Microsoft Azure / AKS
# (Decision #57; sub-gate Open Questions #3 resolved 2026-07-24 →
# Option A "Terraform owns the cluster" + node size Standard_B2s).
#
# Supersedes the scrapped k3d thin-Terraform (Decisions #54/#58/#59).
# Terraform now provisions the CLOUD resources too — a resource group,
# a Free-tier AKS cluster, and a single B-series node pool — via the
# azurerm provider, matching CLAUDE.md §4 ("all cloud resources via
# Terraform, no console clicks"). It then owns the in-cluster declarative
# state (namespaces, quotas, sealed-secrets) through the kubernetes +
# helm providers, pointed at the AKS cluster it just created.
#
# AUTH:
#   - azurerm uses Azure CLI auth (`az login`, already done). The
#     subscription is read from the ARM_SUBSCRIPTION_ID env var.
#   - Terraform Cloud (Decision #46) holds remote state. The workspace
#     runs in LOCAL execution mode (same as the k3d apply history) so
#     the azurerm provider can use the local `az` CLI credentials — a
#     REMOTE-execution workspace would instead need a service principal
#     (ARM_CLIENT_ID/SECRET). Token: TF_TOKEN_app_terraform_io from .env
#     (gitignored). Org: TF_CLOUD_ORGANIZATION. Never hardcoded here.
#
# UNVERIFIED until the first `terraform init` on Azure: provider version
# constraints and the sealed-secrets chart version are drafts — confirm
# on init/plan before trusting them.

terraform {
  required_version = ">= 1.7.0"

  cloud {
    # organization sourced from TF_CLOUD_ORGANIZATION env var.
    workspaces {
      name = "data-cleaning-distributed-system"
    }
  }

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
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

provider "azurerm" {
  features {}
  # subscription_id comes from the ARM_SUBSCRIPTION_ID env var
  # (`export ARM_SUBSCRIPTION_ID=$(az account show --query id -o tsv)`).
}

# The kubernetes + helm providers authenticate against the AKS cluster
# Terraform creates in the same run, using its emitted kubeconfig block.
#
# ponytail: single-apply provider config read from the cluster resource.
# HashiCorp warns this can hit "provider configuration unknown" on a
# from-nothing apply. If it does, run once:
#   terraform apply -target=azurerm_kubernetes_cluster.main
# then a full `terraform apply`. Documented in README. Split into two
# root modules only if this actually bites in practice.
provider "kubernetes" {
  host                   = azurerm_kubernetes_cluster.main.kube_config[0].host
  client_certificate     = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].client_certificate)
  client_key             = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].client_key)
  cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].cluster_ca_certificate)
}

provider "helm" {
  kubernetes {
    host                   = azurerm_kubernetes_cluster.main.kube_config[0].host
    client_certificate     = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].client_certificate)
    client_key             = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].client_key)
    cluster_ca_certificate = base64decode(azurerm_kubernetes_cluster.main.kube_config[0].cluster_ca_certificate)
  }
}
