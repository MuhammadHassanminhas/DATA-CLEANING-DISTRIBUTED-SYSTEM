# Step 1.5.6 #7 fast path: apply ONLY the Alertmanager-route change (the
# kube-prometheus-stack helm_release). Skips planning Loki/Alloy/AKS, so it
# finishes in ~1-2 min instead of the full apply-observability.ps1.
# RUN THIS YOURSELF (agent is gated from mutating cloud infra).
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
  Write-Host "== targeted apply: helm_release.kube_prometheus_stack (Alertmanager route) ==" -ForegroundColor Cyan
  terraform apply "-target=helm_release.kube_prometheus_stack" -auto-approve
  if ($LASTEXITCODE -ne 0) { throw "targeted apply failed" }
  Write-Host "DONE. Alertmanager now routes only team=dcds alerts to Discord." -ForegroundColor Green
}
finally { Pop-Location }
