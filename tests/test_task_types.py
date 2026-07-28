"""Task type parameter validation tests (Phase 2.1).

Covers the exit criterion "each of the four task types validates its
parameters and rejects malformed input". Pure, no database.
"""

import base64

import pytest

from app.task_types import (
    MAX_OPAQUE_PAYLOAD_BYTES,
    TASK_TYPES,
    InvalidTaskParameters,
    UnknownTaskType,
    validate_parameters,
)

FOUR_DUMMY_TYPES = {"count_to_n", "hash_rounds", "sleep", "opaque_payload"}


def test_exactly_the_four_dummy_types_are_registered():
    """CLAUDE.md §2 permits dummy workloads only. A fifth type appearing
    here without a design gate is scope creep, so pin the set."""
    assert set(TASK_TYPES) == FOUR_DUMMY_TYPES


# --- valid input ---------------------------------------------------------


@pytest.mark.parametrize(
    ("task_type", "params"),
    [
        ("count_to_n", {"n": 1000}),
        ("hash_rounds", {"rounds": 500}),
        ("hash_rounds", {"rounds": 500, "algorithm": "sha512"}),
        ("sleep", {"seconds": 1.5}),
        ("opaque_payload", {"payload_b64": base64.b64encode(b"hello").decode()}),
    ],
)
def test_valid_parameters_are_accepted(task_type, params):
    assert validate_parameters(task_type, params)


def test_defaults_are_filled_in_on_the_returned_dict():
    """The normalised form is what gets persisted, so the default has to
    be present in the return value, not just implied."""
    assert validate_parameters("hash_rounds", {"rounds": 1})["algorithm"] == "sha256"


# --- malformed input -----------------------------------------------------


@pytest.mark.parametrize(
    ("task_type", "params", "why"),
    [
        ("count_to_n", {}, "required field missing"),
        ("count_to_n", {"n": 0}, "below minimum"),
        ("count_to_n", {"n": -5}, "negative"),
        ("count_to_n", {"n": 10**12}, "over the ceiling"),
        ("count_to_n", {"n": "lots"}, "not a number"),
        ("hash_rounds", {}, "required field missing"),
        ("hash_rounds", {"rounds": 0}, "below minimum"),
        ("hash_rounds", {"rounds": 10, "algorithm": "md5"}, "algorithm not offered"),
        ("sleep", {}, "required field missing"),
        ("sleep", {"seconds": 0}, "must be positive"),
        ("sleep", {"seconds": -1}, "negative"),
        ("sleep", {"seconds": 99999}, "over the ceiling"),
        ("opaque_payload", {}, "required field missing"),
        ("opaque_payload", {"payload_b64": "not!valid!base64"}, "not base64"),
    ],
)
def test_malformed_parameters_are_rejected(task_type, params, why):
    with pytest.raises(InvalidTaskParameters):
        validate_parameters(task_type, params)


@pytest.mark.parametrize("task_type", sorted(FOUR_DUMMY_TYPES))
def test_none_is_rejected_for_every_type(task_type):
    """No type has an all-defaults parameter set, so `None` is malformed
    for all four rather than meaning "use defaults"."""
    with pytest.raises(InvalidTaskParameters):
        validate_parameters(task_type, None)


@pytest.mark.parametrize(
    ("task_type", "params"),
    [
        ("count_to_n", {"n": 10, "extra": "smuggled"}),
        ("hash_rounds", {"rounds": 10, "cmd": "rm -rf /"}),
        ("sleep", {"seconds": 1, "seconds_typo": 99}),
        ("opaque_payload", {"payload_b64": "aGk=", "unexpected": True}),
    ],
)
def test_unknown_keys_are_rejected_not_ignored(task_type, params):
    """`extra="forbid"`. Every worker is untrusted (§12) and operator input
    creates these rows in 2.6 — silently dropping unrecognised keys would
    hide both a typo and an injection attempt."""
    with pytest.raises(InvalidTaskParameters):
        validate_parameters(task_type, params)


def test_opaque_payload_is_size_capped():
    over = base64.b64encode(b"x" * (MAX_OPAQUE_PAYLOAD_BYTES + 1)).decode()
    with pytest.raises(InvalidTaskParameters):
        validate_parameters("opaque_payload", {"payload_b64": over})


def test_opaque_payload_accepts_exactly_the_limit():
    at = base64.b64encode(b"x" * MAX_OPAQUE_PAYLOAD_BYTES).decode()
    assert validate_parameters("opaque_payload", {"payload_b64": at})


def test_unknown_task_type_is_rejected():
    with pytest.raises(UnknownTaskType):
        validate_parameters("run_sql", {"query": "SELECT 1"})
