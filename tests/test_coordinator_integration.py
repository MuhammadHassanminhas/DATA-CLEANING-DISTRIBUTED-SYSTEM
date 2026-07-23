"""Coordinator integration test against a real ephemeral Postgres + Redis.

Runs in CI where GitHub Actions `services:` provide both stores; skipped
locally unless POSTGRES_HOST is set. Exercises the real startup path
(TestClient triggers the FastAPI lifespan, which runs Alembic migrations
against the live database) and the Phase 1.3 registration endpoint.
"""

import os
from pathlib import Path

import pytest

if not os.environ.get("POSTGRES_HOST"):
    pytest.skip(
        "integration test requires Postgres/Redis (set POSTGRES_HOST)",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient  # noqa: E402 — after the skip guard


@pytest.fixture(scope="module")
def client():
    # Imported here, after env is guaranteed set, because app.db builds the
    # engine from POSTGRES_* at import time. Entering the context manager
    # runs the lifespan → migrations against the ephemeral database.
    from app.main import app

    # alembic.ini's `script_location = migrations` is resolved relative to
    # the cwd; the app always runs with cwd=coordinator/, so match that here
    # (pytest runs from the repo root).
    coordinator_dir = Path(__file__).resolve().parent.parent / "coordinator"
    prev_cwd = os.getcwd()
    os.chdir(coordinator_dir)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(prev_cwd)


def test_health_is_liveness_only(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_ready_reaches_both_stores(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "redis": "ok"}


def test_register_with_valid_enrollment_issues_identity(client):
    r = client.post(
        "/workers/register",
        json={"enrollment_secret": os.environ["ENROLLMENT_SECRET"], "agent_version": "ci-test"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "registered"
    assert body["worker_id"]
    assert body["worker_credential"]


def test_register_with_bad_enrollment_is_rejected(client):
    r = client.post(
        "/workers/register",
        json={"enrollment_secret": "wrong-secret", "agent_version": "ci-test"},
    )
    assert r.status_code == 401
