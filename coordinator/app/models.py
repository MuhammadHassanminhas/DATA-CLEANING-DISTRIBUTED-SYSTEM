"""Worker table.

Columns match the Phase 1.2 spec exactly: identity, hashed credentials,
registration metadata, agent version, lifecycle timestamps, status, and
a revocation flag. No other tables yet — task-related schema is M2.

`status` is a free-text column, not a DB-level enum/CHECK constraint —
the state machine (REGISTERED -> CONNECTING -> ONLINE -> SUSPECT ->
OFFLINE, plus QUARANTINED) is enforced application-side starting Phase
1.6: every write to this column goes through a single transition
helper in `app/main.py` that logs `worker_state_transition` with the
trigger, rather than being set ad hoc at multiple call sites. Actual
issuance of `credential_hash` happens in Phase 1.3 (registration) and
1.4 (auth) — the column exists now so the schema doesn't change under
those phases.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    credential_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    registration_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="REGISTERED")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
