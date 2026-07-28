"""Task type registry and parameter validation (Phase 2.1).

This is the extensibility mechanism decided at the Step 2.0 design gate
(PHASE_STATE.md Decision #82). A task type is a string key into
`TASK_TYPES` mapping to a pydantic model describing its parameters.
Adding a fifth type is one entry here plus its model — **no wire protocol
change and no migration**: `protocol/envelope.py` is untouched,
`PROTOCOL_VERSION` stays `"1.0"`, and the type travels as
`payload.task_type` with `payload.parameters` inside the existing
envelope, exactly the extension route Decision #6 specified.

V1 payloads are dummy workloads only, per CLAUDE.md §2 — count to N, hash
rounds, fixed sleep, opaque byte payload. No SQL, no AI, no real data.

Every model sets `extra="forbid"` so an unrecognised parameter key is a
rejection rather than a silently ignored field. Every worker is untrusted
(§12), and a task row is created from operator input in Step 2.6, so
unvalidated keys reaching the database are not acceptable.

**The bounds below are recommendations chosen in this step, not measured
values and not figures from the spec** (CLAUDE.md §10). They exist so
that "rejects malformed input" is enforceable and so no single task can
occupy a worker indefinitely or bomb the database. The defensible
numbers are whatever Step 2.8's load harness shows; revise them there.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Recommended ceiling for the opaque payload, in bytes. Matches the
# result-body cap recommended in Decision #81 — a worker that echoes its
# input back should not be able to exceed the result cap by construction.
MAX_OPAQUE_PAYLOAD_BYTES = 64 * 1024


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CountToNParams(_Params):
    """Count to N. CPU-light, predictable duration."""

    n: int = Field(ge=1, le=100_000_000)


class HashRoundsParams(_Params):
    """Iterated hashing. CPU-bound, the knob for loading a worker."""

    rounds: int = Field(ge=1, le=10_000_000)
    algorithm: Literal["sha256", "sha512"] = "sha256"


class SleepParams(_Params):
    """Fixed sleep. Occupies a worker slot without consuming CPU — the
    workload for exercising leases and timeouts in Phase 3."""

    seconds: float = Field(gt=0, le=3600)


class OpaquePayloadParams(_Params):
    """Opaque bytes handed to the worker and echoed back. Exercises the
    payload path without the coordinator interpreting the contents."""

    payload_b64: str

    @field_validator("payload_b64")
    @classmethod
    def _must_be_bounded_base64(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("payload_b64 is not valid base64") from exc
        if len(raw) > MAX_OPAQUE_PAYLOAD_BYTES:
            raise ValueError(
                f"decoded payload is {len(raw)} bytes, "
                f"over the {MAX_OPAQUE_PAYLOAD_BYTES}-byte limit"
            )
        return value


TASK_TYPES: dict[str, type[_Params]] = {
    "count_to_n": CountToNParams,
    "hash_rounds": HashRoundsParams,
    "sleep": SleepParams,
    "opaque_payload": OpaquePayloadParams,
}


class UnknownTaskType(ValueError):
    """Raised when a task type is not registered in `TASK_TYPES`."""


class InvalidTaskParameters(ValueError):
    """Raised when parameters fail their type's schema. Wraps pydantic's
    `ValidationError` so callers do not have to import pydantic to catch
    a bad task, and so the error surface here stays this module's own."""


def validate_parameters(task_type: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Validate `parameters` against `task_type`'s schema.

    Returns the normalised parameter dict — defaults filled in, values
    coerced — which is what should be persisted, so the stored row is the
    validated form rather than whatever the caller sent.
    """
    model = TASK_TYPES.get(task_type)
    if model is None:
        raise UnknownTaskType(f"unknown task type: {task_type!r}")

    try:
        return model(**(parameters or {})).model_dump()
    except ValidationError as exc:
        raise InvalidTaskParameters(f"invalid parameters for {task_type}: {exc}") from exc
