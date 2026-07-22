#!/usr/bin/env bash
# Stops and removes all containers, networks, and volumes for this
# stack, returning the machine to a clean state. Generated dev certs
# under certs/ are left in place — delete that directory manually, or
# rerun infra/dev-ca/generate-dev-ca.sh, if you want fresh ones.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker compose down --volumes --remove-orphans
echo "Teardown complete."
