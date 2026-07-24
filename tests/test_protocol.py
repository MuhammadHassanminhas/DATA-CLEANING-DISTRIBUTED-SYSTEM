"""Unit tests for the shared wire-protocol envelope (Decisions Log #6).

Pure, no external deps — these run anywhere pytest does.
"""

from protocol import PROTOCOL_VERSION, Envelope

ENVELOPE_FIELDS = (
    "protocol_version",
    "message_type",
    "correlation_id",
    "worker_id",
    "session_epoch",
    "timestamp",
    "payload",
)


def test_defaults():
    e = Envelope(message_type="ping")
    assert e.protocol_version == PROTOCOL_VERSION
    assert e.session_epoch == 0
    assert e.worker_id is None
    assert e.payload == {}
    assert e.correlation_id  # generated, non-empty


def test_correlation_ids_are_unique():
    assert Envelope(message_type="ping").correlation_id != Envelope(message_type="ping").correlation_id


def test_to_dict_has_the_fixed_top_level_shape():
    d = Envelope(message_type="hello").to_dict()
    assert set(d.keys()) == set(ENVELOPE_FIELDS)


def test_dict_roundtrip_is_lossless():
    e = Envelope(message_type="heartbeat", worker_id="w1", session_epoch=3, payload={"sequence": 1})
    assert Envelope.from_dict(e.to_dict()) == e
