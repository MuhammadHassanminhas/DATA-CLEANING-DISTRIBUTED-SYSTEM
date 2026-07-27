# DCDS worker installer for Windows machines WITHOUT Docker. Step 1.5.8.
# Runs the same worker.py the Docker image runs (msvcrt single-instance
# lock path, added in 1.5.8, is used on Windows).
#
# Config is exactly three things:
#   COORDINATOR_URL    public coordinator, e.g.
#                      https://dcds-staging.centralindia.cloudapp.azure.com
#   ENROLLMENT_SECRET  bootstrap enrollment credential (ask the operator)
#   WORKER_CA_FILE     CA trust — leave empty for the public endpoint
#
# Usage (PowerShell):
#   $env:COORDINATOR_URL="https://..."; $env:ENROLLMENT_SECRET="..."
#   powershell -ExecutionPolicy Bypass -File install-worker.ps1
$ErrorActionPreference = "Stop"

$repo = if ($env:WORKER_REPO_URL) { $env:WORKER_REPO_URL } else { "https://github.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM.git" }
$ref  = if ($env:WORKER_REPO_REF) { $env:WORKER_REPO_REF } else { "main" }
$dest = if ($env:WORKER_HOME) { $env:WORKER_HOME } else { Join-Path $HOME "dcds-worker" }

if (-not $env:COORDINATOR_URL)   { throw "set COORDINATOR_URL (see header)" }
if (-not $env:ENROLLMENT_SECRET) { throw "set ENROLLMENT_SECRET (ask the operator)" }
if (-not $env:WORKER_CA_FILE)        { $env:WORKER_CA_FILE = "" }             # empty = system trust
if (-not $env:WORKER_IDENTITY_FILE)  { $env:WORKER_IDENTITY_FILE = Join-Path $dest "identity.json" }
if (-not $env:WORKER_HEARTBEAT_FILE) { $env:WORKER_HEARTBEAT_FILE = Join-Path $dest "heartbeat" }

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "python is required" }
if (-not (Get-Command git -ErrorAction SilentlyContinue))    { throw "git is required" }

if (Test-Path (Join-Path $dest ".git")) {
  git -C $dest fetch --depth 1 origin $ref
  git -C $dest checkout -f FETCH_HEAD
} else {
  git clone --depth 1 --branch $ref $repo $dest
}

python -m venv (Join-Path $dest ".venv")
$py = Join-Path $dest ".venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r (Join-Path $dest "worker\requirements.txt")

Set-Location $dest
$env:PYTHONPATH = $dest          # so `from protocol.envelope import ...` resolves
Write-Host "Starting worker against $($env:COORDINATOR_URL) ..."
& $py "worker\worker.py"
