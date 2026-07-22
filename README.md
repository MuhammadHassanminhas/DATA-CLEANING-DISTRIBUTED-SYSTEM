# Distributed AI-Orchestrated SQL Database Cleaning Platform

**Version 1 — Distributed Worker Network.** See `CLAUDE.md` for the
guardrails governing this project and `PHASE_STATE.md` for current
status. Architecture reasoning and design decisions live in
`docs/phase-1-reliable-worker-network.md` and `PHASE_STATE.md`'s
Decisions Log — not repeated here.

Phase 1.1 scope: repository, environment, and CI skeleton only. The
coordinator and dashboard expose a bare `/health` endpoint and nothing
else yet; the worker holds no coordinator connection. Registration,
auth, the real transport, and the live dashboard arrive in Phases
1.2–1.8.

## Layout

```
coordinator/   FastAPI coordinator service
worker/        Worker agent
dashboard/     Operator dashboard (placeholder page for now)
protocol/      Shared wire-protocol definitions (coordinator + worker)
infra/         Local dev CA / TLS certificate generation
scripts/       Operational scripts (teardown, etc.)
docs/          Phase specs and architecture docs
```

## Prerequisites

- Docker and Docker Compose
- OpenSSL (for local dev certificate generation)
- Bash (Git Bash on Windows is sufficient)

## Fresh-clone startup sequence

```bash
# 1. Configure environment
cp .env.example .env
# edit .env if you want non-default ports/credentials

# 2. Generate local dev TLS certificates (idempotent — safe to rerun)
bash infra/dev-ca/generate-dev-ca.sh

# 3. Build and start the stack
docker compose up --build
```

Once every container reports healthy (`docker compose ps`):

- Coordinator: https://localhost:8443/health
- Dashboard: https://localhost:8444/

Your browser will warn about the self-signed dev CA — that's expected.
Trust `certs/dev-ca.crt` locally to silence the warning, or click
through it; this CA is dev-only and never used outside your machine.

## Scaling workers

```bash
docker compose up --build --scale worker=5
```

## Teardown

```bash
bash scripts/teardown.sh
```

Stops and removes all containers, networks, and volumes. Generated
certs under `certs/` are left in place.

## CI

`.github/workflows/ci.yml` runs lint (ruff) and a Docker build on every
push and pull request. Branch protection requiring this check to pass
is configured in the GitHub repository settings, not in this repo —
enable it under Settings → Branches for `main` if not already set.
