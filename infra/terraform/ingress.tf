# Step 1.5.5 — Public ingress, TLS, DNS (Decision #57 cloud; design gate
# 2026-07-24: ingress-nginx + cert-manager + a Terraform-owned static
# public IP with a free *.cloudapp.azure.com FQDN).
#
# What lives here (cluster-wide, installed once):
#   - a Standard static public IP in the AKS node resource group, given a
#     domain_name_label → a stable public hostname at $0 (no domain to buy;
#     this IS "DNS records managed in Terraform" for Step 1.5.5).
#   - the ingress-nginx controller, bound to that IP.
#   - cert-manager (with CRDs) to issue + auto-renew a Let's Encrypt cert.
#
# The per-environment Ingress objects and the Let's Encrypt ClusterIssuer
# live in the platform Helm chart (templates/ingress.yaml, clusterissuer.yaml),
# deployed by CD after these controllers exist — so cert-manager's CRDs are
# already present when the chart's ClusterIssuer applies.
#
# COST: one Standard public IP (~pennies/day while allocated) + the same
# single B2s_v2 node. `az aks stop` still halts node billing; the IP is
# released by `terraform destroy` / `az group delete`. Only STAGING is
# exposed (values-staging ingress.enabled=true); production ingress is
# templated but disabled — a second public env needs a second IP + more
# node headroom (honest ceiling, same family as the single-node HA one).

# Static public IP in the AKS-managed node RG. AKS's own identity has
# Network Contributor on that RG already, so ingress-nginx can attach this
# IP by name without extra role grants. depends_on the cluster: the node
# RG does not exist until the cluster is created.
resource "azurerm_public_ip" "ingress" {
  name                = "ingress-nginx"
  resource_group_name = azurerm_kubernetes_cluster.main.node_resource_group
  location            = azurerm_resource_group.main.location
  allocation_method   = "Static"
  sku                 = "Standard" # ingress-nginx uses a Standard-SKU LB
  domain_name_label   = var.ingress_dns_label
  tags                = local.azure_tags

  depends_on = [azurerm_kubernetes_cluster.main]
}

# ingress-nginx controller, pinned to the static IP above.
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  version          = var.ingress_nginx_chart_version

  # loadBalancerIP + the resource-group annotation tell the AKS cloud
  # provider to attach our pre-created IP (which lives in the node RG)
  # rather than allocate a fresh dynamic one.
  set {
    name  = "controller.service.loadBalancerIP"
    value = azurerm_public_ip.ingress.ip_address
  }
  set {
    name  = "controller.service.annotations.service\\.beta\\.kubernetes\\.io/azure-load-balancer-resource-group"
    value = azurerm_kubernetes_cluster.main.node_resource_group
  }
  # externalTrafficPolicy=Local preserves the real client source IP (single
  # node, so no cross-node hop lost) — needed for per-IP edge rate limiting
  # and honest access logs.
  set {
    name  = "controller.service.externalTrafficPolicy"
    value = "Local"
  }
  # One small node: one controller replica, modest requests.
  set {
    name  = "controller.replicaCount"
    value = "1"
  }
  set {
    name  = "controller.resources.requests.cpu"
    value = "100m"
  }
  set {
    name  = "controller.resources.requests.memory"
    value = "128Mi"
  }

  depends_on = [azurerm_public_ip.ingress]
}

# cert-manager — issues and auto-renews the Let's Encrypt certificate.
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  namespace        = "cert-manager"
  create_namespace = true
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_chart_version

  set {
    name  = "crds.enabled"
    value = "true"
  }
  set {
    name  = "resources.requests.cpu"
    value = "50m"
  }
  set {
    name  = "resources.requests.memory"
    value = "64Mi"
  }
}

# --- Outputs -----------------------------------------------------------

output "ingress_public_ip" {
  description = "Static public IP fronting the cluster (ingress-nginx)."
  value       = azurerm_public_ip.ingress.ip_address
}

output "ingress_fqdn" {
  description = "Public hostname for staging coordinator + dashboard. Set this as ingress.host in values-staging.yaml."
  value       = azurerm_public_ip.ingress.fqdn
}
