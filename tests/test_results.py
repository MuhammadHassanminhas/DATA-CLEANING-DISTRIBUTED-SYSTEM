"""Result envelope validation (Phase 2.5).

Pure — no Postgres, no Redis, no sockets — and that is load-bearing rather
than convenient. The exit criterion is "malformed results are rejected
**without corrupting task state**", which is a claim about a decision made
before anything is written. A test that has to stand up a database to prove
it would be proving something weaker: that the write was rolled back, not
that it never started.

These run in CI on every PR, unlike `test_result_submission.py`, which is
Postgres-gated.
"""

import base64
import json

import pytest

from app.results import (
    REJECT_BAD_ATTEMPT,
    REJECT_BAD_DURATION,
    REJECT_BAD_EPOCH,
    REJECT_BAD_STATUS,
    REJECT_BAD_TASK_ID,
    REJECT_MISSING_TOKEN,
    REJECT_UNSERIALISABLE,
    MalformedResult,
    validate,
)
from app.task_types import MAX_OPAQUE_PAYLOAD_BYTES

TASK_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
MAX_BYTES = 128 * 1024


def envelope(**overrides) -> dict:
    body = {
        "task_id": TASK_ID,
        "status": "COMPLETED",
        "attempt_number": 0,
        "session_epoch": 3,
        "idempotency_token": "tok-abc",
        "duration_seconds": 1.25,
        "result": 2000,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# The happy path, and the fields Phase 3 gets for free
# --------------------------------------------------------------------------


def test_a_well_formed_result_normalises_to_the_persisted_envelope():
    stored = validate(envelope(), max_bytes=MAX_BYTES)

    assert stored["task_id"] == TASK_ID
    assert stored["status"] == "COMPLETED"
    assert stored["result"] == 2000
    assert stored["duration_seconds"] == 1.25
    assert stored["truncated"] is False
    assert stored["size_bytes"] > 0


def test_the_phase_3_fields_are_carried_from_day_one():
    """The exit criterion in one assertion: `idempotency_token` and
    `session_epoch` are in every stored result even though **nothing in M2
    enforces either**, so Phase 3 adds replay rejection without a protocol
    change."""
    stored = validate(
        envelope(attempt_number=4, session_epoch=9, idempotency_token="tok-9"),
        max_bytes=MAX_BYTES,
    )

    assert stored["attempt_number"] == 4
    assert stored["session_epoch"] == 9
    assert stored["idempotency_token"] == "tok-9"


def test_size_bytes_is_the_measured_length_not_an_estimate():
    """The column exists (Phase 2.1) so the cap can be audited without
    re-serialising the body. It is only worth having if it is the real
    length of what was measured — the envelope as it stood when the cap was
    applied, which is everything except the `size_bytes` field itself."""
    stored = validate(envelope(result="abc"), max_bytes=MAX_BYTES)
    measured = {key: value for key, value in stored.items() if key != "size_bytes"}
    assert stored["size_bytes"] == len(json.dumps(measured, separators=(",", ":")).encode("utf-8"))


# --------------------------------------------------------------------------
# Malformed: refused before anything is written, with a machine-readable code
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"task_id": "not-a-uuid"}, REJECT_BAD_TASK_ID),
        ({"task_id": None}, REJECT_BAD_TASK_ID),
        ({"status": "RUNNING"}, REJECT_BAD_STATUS),
        ({"status": "FAILED"}, REJECT_BAD_STATUS),
        ({"idempotency_token": ""}, REJECT_MISSING_TOKEN),
        ({"idempotency_token": None}, REJECT_MISSING_TOKEN),
        ({"idempotency_token": "x" * 500}, REJECT_MISSING_TOKEN),
        ({"duration_seconds": "quick"}, REJECT_BAD_DURATION),
        ({"duration_seconds": -1}, REJECT_BAD_DURATION),
        ({"attempt_number": -1}, REJECT_BAD_ATTEMPT),
        ({"attempt_number": "first"}, REJECT_BAD_ATTEMPT),
        ({"session_epoch": None}, REJECT_BAD_EPOCH),
        ({"session_epoch": True}, REJECT_BAD_EPOCH),
    ],
)
def test_malformed_submissions_are_refused_with_their_reason_code(overrides, expected):
    with pytest.raises(MalformedResult) as caught:
        validate(envelope(**overrides), max_bytes=MAX_BYTES)
    assert caught.value.reason_code == expected


def test_an_empty_or_missing_payload_is_refused_rather_than_crashed_on():
    """A worker is untrusted input, not a trusted peer (§12) — it can send
    anything, including nothing."""
    for payload in (None, {}, {"task_id": TASK_ID}):
        with pytest.raises(MalformedResult):
            validate(payload, max_bytes=MAX_BYTES)


def test_a_result_that_will_not_serialise_is_refused_not_stored():
    class Unserialisable:
        pass

    with pytest.raises(MalformedResult) as caught:
        validate(envelope(result=Unserialisable()), max_bytes=MAX_BYTES)
    assert caught.value.reason_code == REJECT_UNSERIALISABLE


def test_status_failed_is_not_smuggled_in_through_the_result_path():
    """Failure has its own message and its own transition (Decision #102).
    Accepting `FAILED` here would give a worker two routes to the same state
    with different guards on each."""
    with pytest.raises(MalformedResult):
        validate(envelope(status="FAILED"), max_bytes=MAX_BYTES)


# --------------------------------------------------------------------------
# Oversize is not malformed
# --------------------------------------------------------------------------


def test_an_oversize_result_is_truncated_and_still_completes():
    stored = validate(envelope(result="x" * (MAX_BYTES + 1000)), max_bytes=MAX_BYTES)

    assert stored["truncated"] is True
    assert stored["result"] is None
    assert stored["original_size_bytes"] > MAX_BYTES
    assert stored["size_bytes"] <= MAX_BYTES
    # The task still completed. Everything Phase 3 needs survives.
    assert stored["status"] == "COMPLETED"
    assert stored["idempotency_token"] == "tok-abc"


def test_the_largest_legal_opaque_payload_result_fits_under_the_cap():
    """Decision #113's arithmetic, asserted rather than argued.

    `opaque_payload` accepts `MAX_OPAQUE_PAYLOAD_BYTES` of **decoded** bytes
    and the executor echoes them back **base64-encoded**, which is 4/3 the
    size. Step 2.1's 64 KB result cap therefore compared decoded input to
    encoded output and would have truncated the largest legal task's result.
    """
    largest = base64.b64encode(b"\xab" * MAX_OPAQUE_PAYLOAD_BYTES).decode("ascii")
    assert len(largest) > 64 * 1024, "the premise: this exceeds the superseded 64 KB cap"

    stored = validate(envelope(result=largest), max_bytes=MAX_BYTES)
    assert stored["truncated"] is False
    assert stored["result"] == largest


def test_a_worker_declared_truncation_is_carried_forward():
    """The worker caps its own envelope before it reaches the wire, so by the
    time it arrives the original body is gone and the coordinator cannot
    rediscover that anything was dropped. Believing the flag can only mark a
    result as less complete than it is, which is not an attack."""
    stored = validate(
        envelope(result=None, truncated=True, original_size_bytes=999_999),
        max_bytes=MAX_BYTES,
    )
    assert stored["truncated"] is True
    assert stored["original_size_bytes"] == 999_999
