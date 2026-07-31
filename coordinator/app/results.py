"""Result envelope validation and normalisation (Phase 2.5).

The worker computes a result and submits it; this is what decides whether
what arrived is a result at all. It is deliberately **pure** — no database,
no Redis, no sockets — because "malformed results are rejected without
corrupting task state" is a claim about a decision made *before* anything
is written, and a test that has to stand up Postgres to prove it is testing
the wrong thing.

**Rejection and truncation are different outcomes, and conflating them is
the mistake this module exists to avoid.**

* **Malformed** means the message is not a result: a task id that is not a
  UUID, a status this step does not accept, a missing idempotency token, a
  duration that is not a number, a body that will not serialise. Nothing is
  written, the task's state is untouched, and the worker is told
  definitively so it stops retrying — a malformed result is malformed on
  every attempt, so retrying it forever would be the worker punishing
  itself for the coordinator's answer.
* **Oversize** means the message *is* a result, and simply carries more
  bytes than are worth storing. The task still completes; the body is
  replaced with a marker recording its real size. An oversize result is a
  fact about the payload, not a protocol error, and refusing to complete
  the task over it would strand real work.

**Every field is worker-reported and therefore untrusted (§12).** Three of
them — `attempt_number`, `session_epoch`, `idempotency_token` — are
required from day one and **enforced by nothing in M2**, which is exactly
what Step 2.5's exit criterion asks for: Phase 3 gets to add replay
rejection and duplicate-attempt arbitration without a protocol change,
because the fields are already on the wire and already in the stored
envelope.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.task_states import COMPLETED

# The only status a `task_result` may carry in M2. Failure has its own
# message (`task_failed`, Decision #102) and its own transition, and folding
# the two into one message would rewrite a shipped Step 2.4 path to buy
# nothing this step needs.
ACCEPTED_STATUSES = frozenset({COMPLETED})

# Bounds on the free-text fields, so a worker cannot make a log line or a
# JSONB row arbitrarily large through a field nothing validates in M2.
MAX_TOKEN_CHARS = 128

# Reason codes. A **wire contract**, not log text: the worker branches on
# the ack to decide whether to drop a pending result or keep retrying, and
# the operator API surfaces them.
REJECT_BAD_TASK_ID = "bad_task_id"
REJECT_BAD_STATUS = "bad_status"
REJECT_MISSING_TOKEN = "missing_idempotency_token"
REJECT_BAD_DURATION = "bad_duration"
REJECT_BAD_ATTEMPT = "bad_attempt_number"
REJECT_BAD_EPOCH = "bad_session_epoch"
REJECT_UNSERIALISABLE = "unserialisable_result"


class MalformedResult(ValueError):
    """A submission that is not a result. Carries a machine-readable code."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def _optional_int(value: Any) -> int | None:
    """A worker-declared integer that is recorded but never relied on."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _require_int(payload: dict[str, Any], key: str, reason_code: str, *, minimum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MalformedResult(reason_code, f"{key} must be an integer >= {minimum}, got {value!r}")
    return value


def validate(payload: dict[str, Any] | None, *, max_bytes: int) -> dict[str, Any]:
    """Turn a `task_result` payload into the envelope that gets persisted.

    Returns the normalised envelope, which is what goes into
    `task_results.payload` verbatim. Raises `MalformedResult` — never
    anything else — so a caller holding a database transaction knows that a
    raise means "nothing to write" rather than "something went wrong".

    The returned envelope always carries `truncated` and `size_bytes`, set
    whether or not truncation happened, so a stored result never has to be
    re-serialised to answer how big it was.
    """
    payload = payload or {}

    raw_task_id = str(payload.get("task_id") or "")
    try:
        task_id = str(uuid.UUID(raw_task_id))
    except (ValueError, AttributeError):
        raise MalformedResult(REJECT_BAD_TASK_ID, f"task_id is not a UUID: {raw_task_id!r}") from None

    status = str(payload.get("status") or "")
    if status not in ACCEPTED_STATUSES:
        raise MalformedResult(
            REJECT_BAD_STATUS, f"status must be one of {sorted(ACCEPTED_STATUSES)}, got {status!r}"
        )

    token = payload.get("idempotency_token")
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise MalformedResult(
            REJECT_MISSING_TOKEN,
            f"idempotency_token must be a non-empty string of at most {MAX_TOKEN_CHARS} characters",
        )

    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
        raise MalformedResult(
            REJECT_BAD_DURATION, f"duration_seconds must be a number >= 0, got {duration!r}"
        )

    # Phase 3's two fields. Validated for shape now and acted on by nothing —
    # see the module docstring.
    attempt_number = _require_int(payload, "attempt_number", REJECT_BAD_ATTEMPT, minimum=0)
    session_epoch = _require_int(payload, "session_epoch", REJECT_BAD_EPOCH, minimum=0)

    # A worker that already truncated says so, and the fact is carried
    # forward rather than recomputed: by the time the envelope arrives the
    # original body is gone, so the coordinator cannot rediscover that
    # anything was dropped. Believing this flag costs nothing — it can only
    # mark a result as *less* complete than it is, which is not an attack.
    truncated = bool(payload.get("truncated"))
    envelope: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "attempt_number": attempt_number,
        "session_epoch": session_epoch,
        "idempotency_token": token,
        "duration_seconds": round(float(duration), 3),
        "result": payload.get("result"),
        "truncated": truncated,
    }
    if truncated:
        envelope["original_size_bytes"] = _optional_int(payload.get("original_size_bytes"))

    # Serialised once, here, and the length reused as `size_bytes` — the
    # column exists (Phase 2.1) precisely so the cap can be audited without
    # re-serialising the body later.
    try:
        encoded = json.dumps(envelope, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise MalformedResult(REJECT_UNSERIALISABLE, f"result is not JSON-serialisable: {exc}") from exc

    size_bytes = len(encoded.encode("utf-8"))
    if size_bytes > max_bytes:
        # The body goes, the envelope stays. Everything Phase 3 needs to
        # reason about the attempt survives; only the payload it would never
        # have looked at is dropped.
        envelope["result"] = None
        envelope["truncated"] = True
        envelope["original_size_bytes"] = size_bytes
        size_bytes = len(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))

    envelope["size_bytes"] = size_bytes
    return envelope
