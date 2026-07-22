"""Coordinator service — Phase 1.1 skeleton.

Only enough to prove the container runs, is healthy, and is reachable
over TLS. Persistence, structured logging, and readiness checks against
Postgres/Redis arrive in Phase 1.2.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Coordinator", version="0.1.0")


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "coordinator", "status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}
