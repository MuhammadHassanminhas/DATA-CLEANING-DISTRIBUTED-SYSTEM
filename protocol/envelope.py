"""Shared wire-protocol message envelope.

Decided in Phase 1.0 (see PHASE_STATE.md Decisions Log #6). Not yet
imported by any service — coordinator and worker start exchanging these
once Phase 1.5 (persistent connection transport) begins. Exists now so
the shape is fixed before any transport code is written, per the Phase
1.0 exit criteria ("later phases depend on it and must not require a
protocol change").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "1.0"


@dataclasses.dataclass
class Envelope:
    message_type: str
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    protocol_version: str = PROTOCOL_VERSION
    correlation_id: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))
    worker_id: str | None = None
    session_epoch: int = 0
    timestamp: str = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        return cls(**data)
