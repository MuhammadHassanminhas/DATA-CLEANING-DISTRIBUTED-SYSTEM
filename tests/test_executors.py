"""Worker executor tests (Phase 2.4).

**This is how the "all four task types execute correctly and return correct
results" exit criterion is verified**, and it has to be, because Step 2.4
discards every result it computes (Decision #98) — there is nothing in the
database to check against. So correctness is established here, against
known answers, and the live run then evidences that the *same* code
produced the *same* answer via the fingerprint in its log (Decision #106).

The hash vectors below are literals on purpose. Recomputing the expected
digest with the same `hashlib` loop the implementation uses would test
nothing but that the code is deterministic; these were computed
independently and pasted in, so a change in the seed, the iteration order,
or the algorithm fails the test.

No Postgres, no Redis, no coordinator — the executors deliberately import
none of it.
"""

import threading
import time

import pytest

from worker import executors

# Independently computed: digest = sha256(digest) applied `rounds` times to
# a 32-byte zero seed. Recompute with:
#   python -c "import hashlib; d=b'\x00'*32
#   [d:=hashlib.sha256(d).digest() for _ in range(3)]; print(d.hex())"
SHA256_1_ROUND = "66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925"
SHA256_3_ROUNDS = "12771355e46cd47c71ed1721fd5319b383cca3a1f9fce3aa1c8cd3bd37af20d7"
SHA256_1000_ROUNDS = "36c1cb4f826ae42ceba848227e0c5f786178ca9dceca6772e5d728d09c30a2f6"
SHA512_3_ROUNDS = (
    "1a0a596f4aa2cdae5d6f0a1a38cfd9c2e8adbeb9cb7b8e1435d14b549d07a4a9e"
    "c8522020f619c7ecfc0b7c06fed1192fe8d12f921459089d06d3b1ecd462035"
)


# --------------------------------------------------------------------------
# Correct results, against known answers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 7, 1000, 2_500_000])
def test_count_to_n_returns_n(n):
    """Chunked at 1,000,000, so 2.5M crosses the boundary three times — the
    case a single-chunk implementation would pass by accident."""
    assert executors.execute("count_to_n", {"n": n}) == n


@pytest.mark.parametrize(
    "rounds,algorithm,expected",
    [
        (1, "sha256", SHA256_1_ROUND),
        (3, "sha256", SHA256_3_ROUNDS),
        (1000, "sha256", SHA256_1000_ROUNDS),
        (3, "sha512", SHA512_3_ROUNDS),
    ],
)
def test_hash_rounds_matches_its_known_answer(rounds, algorithm, expected):
    result = executors.execute("hash_rounds", {"rounds": rounds, "algorithm": algorithm})
    assert result == expected


def test_hash_rounds_defaults_to_sha256():
    """The coordinator's registry defaults `algorithm`, but a worker must not
    depend on the field being present to agree with it."""
    assert executors.execute("hash_rounds", {"rounds": 1}) == SHA256_1_ROUND


def test_hash_rounds_is_chunk_boundary_independent(monkeypatch):
    """The answer must be a property of `rounds`, not of the chunk size."""
    monkeypatch.setattr(executors, "HASH_CHUNK_ROUNDS", 7)
    assert executors.execute("hash_rounds", {"rounds": 1000}) == SHA256_1000_ROUNDS


def test_sleep_occupies_the_slot_for_its_duration():
    started = time.monotonic()
    elapsed = executors.execute("sleep", {"seconds": 1.2})
    wall = time.monotonic() - started
    assert wall >= 1.2
    # The result is the measured duration, not the requested one.
    assert 1.2 <= elapsed <= 1.2 + 1.0


def test_opaque_payload_round_trips_the_bytes():
    payload_b64 = "aGVsbG8gd29ybGQ="  # "hello world"
    assert executors.execute("opaque_payload", {"payload_b64": payload_b64}) == payload_b64


def test_opaque_payload_rejects_a_payload_over_the_cap():
    import base64

    oversized = base64.b64encode(b"x" * (executors.MAX_OPAQUE_PAYLOAD_BYTES + 1)).decode()
    with pytest.raises(executors.InvalidExecutionParameters):
        executors.execute("opaque_payload", {"payload_b64": oversized})


def test_opaque_payload_rejects_invalid_base64():
    with pytest.raises(executors.InvalidExecutionParameters):
        executors.execute("opaque_payload", {"payload_b64": "not base64 at all!!"})


# --------------------------------------------------------------------------
# The refusal path, and parameters the two sides disagree about
# --------------------------------------------------------------------------


def test_an_unregistered_task_type_is_refused_not_crashed_on():
    """Exit criterion: an unsupported task type is refused cleanly. The
    worker raises a *specific* error, which is what lets `worker.py` refuse
    with a reason code instead of reporting a failure."""
    with pytest.raises(executors.UnsupportedTaskType):
        executors.execute("mine_bitcoin", {"n": 1})


def test_the_registry_covers_exactly_the_four_v1_types():
    """CLAUDE.md §2: dummy workloads only. A fifth entry here without a
    coordinator-side registry entry would be a protocol disagreement."""
    assert set(executors.EXECUTORS) == {"count_to_n", "hash_rounds", "sleep", "opaque_payload"}


@pytest.mark.parametrize(
    "task_type,parameters",
    [
        ("count_to_n", {}),
        ("count_to_n", {"n": 0}),
        ("count_to_n", {"n": "lots"}),
        ("count_to_n", {"n": True}),
        ("hash_rounds", {"rounds": 5, "algorithm": "md5"}),
        ("sleep", {"seconds": 0}),
        ("sleep", {"seconds": -1}),
        ("opaque_payload", {}),
    ],
)
def test_parameters_the_coordinator_would_have_rejected_fail_cleanly(task_type, parameters):
    """These are unreachable if both sides agree — the coordinator validates
    every parameter before a task is queued. Reaching one means the two
    registries disagree, and a named error beats a `KeyError` from inside a
    loop."""
    with pytest.raises(executors.InvalidExecutionParameters):
        executors.execute(task_type, parameters)


# --------------------------------------------------------------------------
# Progress and cooperative cancellation
# --------------------------------------------------------------------------


def test_progress_advances_monotonically_to_one():
    progress = [0.0]
    samples = []

    def watch() -> None:
        while progress[0] < 1.0:
            samples.append(progress[0])
            time.sleep(0.01)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    executors.execute("count_to_n", {"n": 20_000_000}, progress)
    watcher.join(timeout=2)

    assert progress[0] == 1.0
    assert samples == sorted(samples)


def test_progress_reaches_one_for_every_type():
    for task_type, parameters in (
        ("count_to_n", {"n": 1000}),
        ("hash_rounds", {"rounds": 100}),
        ("sleep", {"seconds": 0.05}),
        ("opaque_payload", {"payload_b64": "AAAA"}),
    ):
        progress = [0.0]
        executors.execute(task_type, parameters, progress)
        assert progress[0] == 1.0, task_type


def test_a_cancelled_task_stops_promptly_rather_than_running_to_completion():
    """A Python thread cannot be killed, so this is the only cancellation
    that exists. `n` is large enough that completing it would take far
    longer than the assertion allows."""
    cancel = threading.Event()
    cancel.set()  # already cancelled: the first chunk boundary must notice
    started = time.monotonic()
    with pytest.raises(executors.ExecutionCancelled):
        executors.execute("count_to_n", {"n": 100_000_000}, None, cancel)
    assert time.monotonic() - started < 2.0


def test_cancellation_interrupts_a_sleep_mid_flight():
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(executors.ExecutionCancelled):
        executors.execute("sleep", {"seconds": 30}, None, cancel)
    assert time.monotonic() - started < 3.0


# --------------------------------------------------------------------------
# The result fingerprint (Decision #106)
# --------------------------------------------------------------------------


def test_the_fingerprint_is_deterministic_and_discloses_nothing():
    payload_b64 = "aGVsbG8gd29ybGQ="
    result = executors.execute("opaque_payload", {"payload_b64": payload_b64})
    once = executors.fingerprint(result)
    twice = executors.fingerprint(result)

    assert once == twice
    assert len(once) == executors.FINGERPRINT_HEX_CHARS
    # The point of logging a hash instead of the result: the payload cannot
    # be read out of the log line (§12).
    assert payload_b64 not in once
    assert "hello" not in once


def test_different_results_fingerprint_differently():
    assert executors.fingerprint(1000) != executors.fingerprint(1001)
