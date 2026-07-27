# Creates the Secrets the platform chart references, in a target namespace,
# from the gitignored .env and certs/ — for bootstrap use. Nothing here is
# committed to Git (CLAUDE.md §12). Production path is sealed-secrets
# (kubeseal), controller already installed in Step 1.5.1 — wiring deferred
# to the CI/CD phase (1.5.3/1.5.4). Runs against whatever cluster kubectl's
# current-context points at (AKS after `az aks get-credentials`).
#
# Usage:  ./bootstrap-secrets.ps1 -Namespace staging
param(
  [string]$Namespace = "staging"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\..\.."
$envFile = Join-Path $root ".env"
$certs = Join-Path $root "certs"

function Get-EnvVal($name) {
  $line = Get-Content $envFile | Where-Object { $_ -match "^\s*$name\s*=" }
  if (-not $line) { throw "$name not found in .env" }
  return ($line -split '=', 2)[1].Trim()
}

$enroll = Get-EnvVal "ENROLLMENT_SECRET"
$pepper = Get-EnvVal "CREDENTIAL_PEPPER"
$pgpass = Get-EnvVal "POSTGRES_PASSWORD"
# Step 1.5.5 dashboard edge basic-auth (nginx auth-secret). User/password
# from .env; the Secret holds an htpasswd line under key `auth`.
$dashUser = Get-EnvVal "DASHBOARD_USER"
$dashPass = Get-EnvVal "DASHBOARD_PASSWORD"

# apply pattern = create --dry-run | apply, so re-running is idempotent.
# Note: param must NOT be named $args ($args is a reserved automatic var).
function Apply-Secret($secretArgs) {
  kubectl create secret generic @secretArgs --namespace $Namespace --dry-run=client -o yaml | kubectl apply -f -
}

Apply-Secret @("app-secrets", "--from-literal=ENROLLMENT_SECRET=$enroll", "--from-literal=CREDENTIAL_PEPPER=$pepper")
Apply-Secret @("postgres-secret", "--from-literal=POSTGRES_PASSWORD=$pgpass")
Apply-Secret @(
  "tls-certs",
  "--from-file=coordinator.crt=$certs\coordinator.crt",
  "--from-file=coordinator.key=$certs\coordinator.key",
  "--from-file=dashboard.crt=$certs\dashboard.crt",
  "--from-file=dashboard.key=$certs\dashboard.key",
  "--from-file=dev-ca.crt=$certs\dev-ca.crt"
)

# htpasswd line via openssl apr1 (openssl is already a project dependency —
# infra/dev-ca generates the dev CA with it). nginx-ingress accepts apr1.
$hash = (& openssl passwd -apr1 $dashPass)
if ($LASTEXITCODE -ne 0) { throw "openssl passwd failed - is openssl on PATH?" }
Apply-Secret @("dashboard-basic-auth", "--from-literal=auth=$dashUser`:$hash")

Write-Host "Secrets app-secrets, postgres-secret, tls-certs, dashboard-basic-auth applied to namespace '$Namespace'."

# --- Step 1.5.6 observability secrets (always the `observability` ns) ----
# Grafana admin login + the Alertmanager Discord/Slack webhook URL. Read
# from .env; never committed (§12). Discord: use the webhook URL with
# `/slack` appended (Discord's Slack-compatible endpoint).
function Apply-ObsSecret($secretArgs) {
  kubectl create secret generic @secretArgs --namespace observability --dry-run=client -o yaml | kubectl apply -f -
}
$grafUser = Get-EnvVal "GRAFANA_ADMIN_USER"
$grafPass = Get-EnvVal "GRAFANA_ADMIN_PASSWORD"
$webhook  = Get-EnvVal "ALERTMANAGER_WEBHOOK_URL"
Apply-ObsSecret @("grafana-admin", "--from-literal=admin-user=$grafUser", "--from-literal=admin-password=$grafPass")
Apply-ObsSecret @("alertmanager-webhook", "--from-literal=webhook-url=$webhook")
Write-Host "Secrets grafana-admin, alertmanager-webhook applied to namespace 'observability'."
