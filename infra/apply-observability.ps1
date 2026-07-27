# Step 1.5.6 live-apply runner. RUN THIS YOURSELF (the agent is gated from
# mutating cloud infra). Loads Terraform auth + the observability secrets
# from the gitignored .env and applies in the required order:
#   1. create the observability namespace
#   2. create grafana-admin + alertmanager-webhook secrets (must pre-exist
#      or Grafana/Alertmanager pods hang — the 1.5.5 --wait deadlock)
#   3. full terraform apply (scales node_count 1->2 + installs the stack)
#
# Prereqs: cluster started (`az aks start ...`), kubectl context on the AKS
# cluster (`az aks get-credentials ...`), .env has GRAFANA_ADMIN_USER,
# GRAFANA_ADMIN_PASSWORD, ALERTMANAGER_WEBHOOK_URL, TF_API_TOKEN,
# TF_CLOUD_ORGANIZATION. Nothing here is committed with secrets in it.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\.."
$envFile = Join-Path $root ".env"
function G($n) {
  $l = Get-Content $envFile | Where-Object { $_ -match "^\s*$n\s*=" } | Select-Object -First 1
  if (-not $l) { throw "$n not found in .env" }
  ($l -split '=', 2)[1].Trim()
}

$env:TF_CLOUD_ORGANIZATION     = G "TF_CLOUD_ORGANIZATION"
$env:TF_TOKEN_app_terraform_io = G "TF_API_TOKEN"
try { $env:ARM_SUBSCRIPTION_ID = G "ARM_SUBSCRIPTION_ID" } catch { $env:ARM_SUBSCRIPTION_ID = (az account show --query id -o tsv) }

Push-Location (Join-Path $root "infra\terraform")
try {
  Write-Host "`n== 1/3 create observability namespace ==" -ForegroundColor Cyan
  terraform apply "-target=kubernetes_namespace.observability" -auto-approve
  if ($LASTEXITCODE -ne 0) { throw "namespace apply failed" }

  Write-Host "`n== 2/3 create observability secrets ==" -ForegroundColor Cyan
  $gu = G "GRAFANA_ADMIN_USER"; $gp = G "GRAFANA_ADMIN_PASSWORD"; $wh = G "ALERTMANAGER_WEBHOOK_URL"
  kubectl create secret generic grafana-admin -n observability `
    --from-literal=admin-user=$gu --from-literal=admin-password=$gp `
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl create secret generic alertmanager-webhook -n observability `
    --from-literal=webhook-url=$wh `
    --dry-run=client -o yaml | kubectl apply -f -

  Write-Host "`n== 3/3 full apply (scales to 2 nodes + installs stack, ~10-15 min) ==" -ForegroundColor Cyan
  terraform apply -auto-approve
  if ($LASTEXITCODE -ne 0) { throw "full apply failed" }

  Write-Host "`nDONE. Reach Grafana:  kubectl -n observability port-forward svc/kube-prometheus-stack-grafana 3000:80" -ForegroundColor Green
}
finally { Pop-Location }
