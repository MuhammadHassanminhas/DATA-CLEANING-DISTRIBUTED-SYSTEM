"""Request/response bodies for worker registration (Phase 1.3) and the
minimal task-queue endpoints (Phase 2.2)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    enrollment_secret: str | None = None
    worker_id: str | None = None
    worker_credential: str | None = None
    agent_version: str | None = None


class ReleaseRequest(BaseModel):
    worker_id: str
    worker_credential: str


class TokenRefreshRequest(BaseModel):
    worker_id: str
    worker_credential: str


class RevokeRequest(BaseModel):
    admin_secret: str


class PushRequest(BaseModel):
    admin_secret: str
    message_type: str
    payload: dict | None = None


class EnqueueTaskRequest(BaseModel):
    """Phase 2.2. `parameters` is validated against the task type's schema
    by `app.task_types`, not here — this model only shapes the envelope.

    `count` enqueues N identical tasks in one call. It exists so the
    10,000-task exit criterion is reachable through the rate-limited
    public ingress, and so Step 2.8's harness has a bulk path that does
    not require loosening the edge limit.

    `admin_secret` is **optional here and required by the endpoint** — the
    credential may instead arrive in the `X-Admin-Secret` header, which is
    the documented path since Step 2.6. Optional rather than required also
    means a caller that omits it gets a 401 from the auth check rather than
    a 422 from pydantic; a 422 is the response that used to quote the
    request back (see `main._validation_error_without_echo`).
    """

    admin_secret: str = ""
    task_type: str
    parameters: dict | None = None
    payload: dict | None = None
    # Lower is more urgent (Unix nice) — see `models.Task`.
    priority: int = 0
    count: int = Field(default=1, ge=1)


class DequeueTaskRequest(BaseModel):
    """Phase 2.2 queue primitive. The caller names the worker; choosing
    *which* worker should get the work is the Step 2.3 assignment engine.
    """

    admin_secret: str
    worker_id: str
    limit: int = Field(default=1, ge=1)
