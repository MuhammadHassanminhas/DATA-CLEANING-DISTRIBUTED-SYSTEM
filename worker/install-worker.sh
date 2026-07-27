#!/usr/bin/env sh
# DCDS worker installer for machines WITHOUT Docker (Linux / macOS).
# Step 1.5.8. Coordinator code is untouched — this only pulls, builds a
# venv, and runs the same worker.py the Docker image runs.
#
# Config is exactly three things:
#   COORDINATOR_URL    public coordinator, e.g.
#                      https://dcds-staging.centralindia.cloudapp.azure.com
#   ENROLLMENT_SECRET  bootstrap enrollment credential (ask the operator)
#   WORKER_CA_FILE     CA trust — leave UNSET/empty for the public endpoint
#                      (its Let's Encrypt cert validates against system roots)
#
# Usage:
#   COORDINATOR_URL=https://... ENROLLMENT_SECRET=... sh install-worker.sh
set -eu

REPO="${WORKER_REPO_URL:-https://github.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM.git}"
REF="${WORKER_REPO_REF:-main}"
DEST="${WORKER_HOME:-$HOME/dcds-worker}"

: "${COORDINATOR_URL:?set COORDINATOR_URL (see header)}"
: "${ENROLLMENT_SECRET:?set ENROLLMENT_SECRET (ask the operator)}"
export WORKER_CA_FILE="${WORKER_CA_FILE:-}"                     # empty = system trust
export WORKER_IDENTITY_FILE="${WORKER_IDENTITY_FILE:-$DEST/identity.json}"
export WORKER_HEARTBEAT_FILE="${WORKER_HEARTBEAT_FILE:-$DEST/heartbeat}"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }

if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch --depth 1 origin "$REF"
  git -C "$DEST" checkout -f FETCH_HEAD
else
  git clone --depth 1 --branch "$REF" "$REPO" "$DEST"
fi

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --upgrade pip
"$DEST/.venv/bin/pip" install --quiet -r "$DEST/worker/requirements.txt"

cd "$DEST"
export PYTHONPATH="$DEST"          # so `from protocol.envelope import ...` resolves
export COORDINATOR_URL ENROLLMENT_SECRET
echo "Starting worker against $COORDINATOR_URL ..."
exec "$DEST/.venv/bin/python" worker/worker.py
