"""Request/response bodies for worker registration (Phase 1.3)."""

from __future__ import annotations

from pydantic import BaseModel


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
