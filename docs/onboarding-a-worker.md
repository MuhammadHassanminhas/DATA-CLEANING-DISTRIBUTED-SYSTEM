# Onboarding a worker (Step 1.5.8)

How to bring a worker online from any machine, anywhere, over the public
Internet. **No inbound port is ever opened on the worker** — it dials out
to the coordinator on 443 only.

The coordinator code is identical in every environment. Only these three
values change per machine:

| Value | What it is | Public-endpoint value |
|---|---|---|
| `COORDINATOR_URL` | Public coordinator base URL | `https://dcds-staging.centralindia.cloudapp.azure.com` |
| `ENROLLMENT_SECRET` | Bootstrap enrollment credential | **ask the operator** (never committed to Git) |
| `WORKER_CA_FILE` | CA trust | **leave empty** — the public endpoint has a Let's Encrypt cert that validates against the OS trust store |

The enrollment secret is the shared bootstrap credential (Decision #9). It
lets a machine register once; after that the worker holds its own
long-lived per-worker credential and never needs the enrollment secret
again. Do not paste the enrollment secret into any file that gets
committed.

---

## Option 1 — Docker (any OS with Docker)

Simplest. Nothing to build.

```bash
docker run --rm \
  -e COORDINATOR_URL=https://dcds-staging.centralindia.cloudapp.azure.com \
  -e WORKER_CA_FILE= \
  -e ENROLLMENT_SECRET=<ask-the-operator> \
  ghcr.io/muhammadhassanminhas/data-cleaning-distributed-system-worker:main
```

Pin to a specific SHA tag instead of `:main` for a reproducible run.

## Option 2 — Linux / macOS without Docker

Needs `python3` and `git`. One command:

```sh
curl -fsSL https://raw.githubusercontent.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM/main/worker/install-worker.sh -o install-worker.sh
COORDINATOR_URL=https://dcds-staging.centralindia.cloudapp.azure.com \
ENROLLMENT_SECRET=<ask-the-operator> \
sh install-worker.sh
```

It clones the repo to `~/dcds-worker`, builds a venv, and runs the worker.
The identity file persists in `~/dcds-worker/identity.json`, so a restart
keeps the same worker ID.

## Option 3 — Windows without Docker

Needs Python (from python.org, "Add to PATH" checked) and Git. In
PowerShell:

```powershell
irm https://raw.githubusercontent.com/MuhammadHassanminhas/DATA-CLEANING-DISTRIBUTED-SYSTEM/main/worker/install-worker.ps1 -OutFile install-worker.ps1
$env:COORDINATOR_URL="https://dcds-staging.centralindia.cloudapp.azure.com"
$env:ENROLLMENT_SECRET="<ask-the-operator>"
powershell -ExecutionPolicy Bypass -File install-worker.ps1
```

---

## Verify it connected

The worker prints structured JSON. A successful onboard logs, in order,
`registered` (first run) or `reaffirmed` (later runs), then
`access_token_refreshed`, then `ws_connected` with a `session_epoch`.

It should also appear on the dashboard alongside every other worker:
`https://dcds-staging.centralindia.cloudapp.azure.com/` (basic-auth — ask
the operator for the login). Region latency differences are visible per
worker on that dashboard.

---

## Operator: issuing and revoking access

**Issue** — give the person exactly two things out of band: the
`COORDINATOR_URL` and the current `ENROLLMENT_SECRET`. Nothing else.

**Revoke a single worker** — quarantines that worker ID; it is
disconnected within one heartbeat sweep and can never re-register or
refresh a token again. `admin_secret` is the same shared secret
(stand-in admin credential, Decision #17).

```bash
curl -X POST https://dcds-staging.centralindia.cloudapp.azure.com/workers/<worker_id>/revoke \
  -H 'Content-Type: application/json' \
  -d '{"admin_secret":"<enrollment-secret>"}'
```

Find a worker's ID from the dashboard, or:

```bash
curl -H 'x-admin-secret: <enrollment-secret>' \
  https://dcds-staging.centralindia.cloudapp.azure.com/workers
```

**Rotate / mass-revoke** — if the enrollment secret leaks, rotate the
`enrollment-secret` Kubernetes secret and redeploy the coordinator. New
onboards need the new secret; already-enrolled workers keep running on
their own per-worker credentials (revoke those individually if needed).

> Per-worker enrollment tokens (independently revocable at enrollment
> time) were considered for this phase and deferred as scope — the shared
> bootstrap secret plus per-worker-ID revocation above covers V1
> onboarding (Decision #76).
