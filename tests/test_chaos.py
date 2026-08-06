"""Step 3.8 — the chaos harness's own logic, without a coordinator.

What is worth testing here is **the verdict**, not the faults. A fault that
does not fire shows up in a run's own report; a verdict that says PASS when
an invariant broke is the failure nothing else catches, and it is the one
thing this step promises ("any invariant violation fails the run loudly").

So every test below either drives `evaluate` — the pure function `main`
turns into an exit code — or drives the two pieces of bookkeeping that feed
it. No sockets, no database, no event loop except where a coroutine is
under test.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("chaos", _ROOT / "scripts" / "chaos.py")
chaos = importlib.util.module_from_spec(_SPEC)
sys.modules["chaos"] = chaos
_SPEC.loader.exec_module(chaos)


# --------------------------------------------------------------------------
# Fixtures — the shape of a healthy run, so each test can break one thing
# --------------------------------------------------------------------------

def _rows(*, total=100, completed=100, with_result=100, in_flight=0, failed=0):
    by_status = {"COMPLETED": completed}
    if failed:
        by_status["FAILED"] = failed
    if in_flight:
        by_status["RUNNING"] = in_flight
    return {
        "rows": total,
        "distinct_task_ids": total,
        "by_status": by_status,
        "completed": completed,
        "with_result": with_result,
    }


def _healthy(**overrides):
    kwargs = {
        "accepted": 100,
        "requested": 100,
        "rows": _rows(),
        "injections": [{"task_id": "t", "outcome": "duplicate", "accepted": True}],
        "chaos": {"applied": 12},
        "converged_seconds": 4.2,
        "unanswered_on_live_session": 0,
    }
    kwargs.update(overrides)
    return chaos.evaluate(**kwargs)


def _failing(checks: dict[str, bool]) -> list[str]:
    return [name for name, ok in checks.items() if not ok]


class TestHealthyRun:
    def test_a_clean_run_passes_every_check(self):
        assert _failing(_healthy()) == []

    def test_a_reassigned_task_that_still_completed_is_not_a_failure(self):
        # Redelivery is at-least-once delivery working (§3.0.5). The
        # harness must not fail a run for it — only the ledger decides.
        assert _failing(_healthy(rows=_rows())) == []


class TestZeroLoss:
    def test_a_task_that_never_completed_fails_the_run(self):
        checks = _healthy(rows=_rows(completed=99, with_result=99))
        assert checks["no_task_lost"] is False
        assert "no_task_lost" in _failing(checks)

    def test_a_task_that_exhausted_its_retries_fails_the_run(self):
        # FAILED is a terminal state, not a lost row — and it still fails
        # this check on purpose. Under a fault load the fleet is meant to
        # survive, a terminal failure is a result worth stopping for.
        checks = _healthy(rows=_rows(completed=97, with_result=97, failed=3))
        assert checks["no_task_lost"] is False

    def test_a_row_that_never_came_back_fails_the_run(self):
        checks = _healthy(rows=_rows(total=99, completed=99, with_result=99))
        assert checks["every_task_read_back"] is False

    def test_a_rejected_enqueue_fails_before_anything_else(self):
        checks = _healthy(accepted=90, rows=_rows(total=90, completed=90, with_result=90))
        assert checks["every_task_accepted"] is False


class TestZeroDoubleCompletion:
    def test_an_injected_submission_that_completed_a_task_fails_the_run(self):
        checks = _healthy(injections=[{"task_id": "t", "outcome": "transitioned",
                                       "accepted": True}])
        assert checks["no_injected_submission_completed_a_task"] is False

    @pytest.mark.parametrize("outcome", sorted(chaos.REFUSAL_OUTCOMES))
    def test_every_refusal_outcome_is_accepted_as_a_refusal(self, outcome):
        checks = _healthy(injections=[{"task_id": "t", "outcome": outcome, "accepted": False}])
        assert checks["no_injected_submission_completed_a_task"] is True
        assert checks["every_acked_injection_was_refused"] is True

    def test_an_unknown_verdict_fails_rather_than_being_ignored(self):
        checks = _healthy(injections=[{"task_id": "t", "outcome": "something_new",
                                       "accepted": False}])
        assert checks["every_acked_injection_was_refused"] is False

    def test_transitioned_is_not_in_the_refusal_set(self):
        # The mutation that would make this harness useless: adding
        # `transitioned` to REFUSAL_OUTCOMES turns a double completion into
        # a pass. Asserted directly so the constant cannot drift.
        assert "transitioned" not in chaos.REFUSAL_OUTCOMES

    def test_a_completion_without_a_result_fails_the_run(self):
        checks = _healthy(rows=_rows(with_result=99))
        assert checks["every_completion_has_one_result"] is False

    def test_two_rows_for_one_task_fails_the_run(self):
        rows = _rows()
        rows["distinct_task_ids"] = 99
        checks = _healthy(rows=rows)
        assert checks["no_duplicate_task_rows"] is False


class TestConvergence:
    def test_a_task_still_running_after_chaos_stops_fails_the_run(self):
        checks = _healthy(rows=_rows(completed=100, in_flight=1))
        assert checks["converged_no_tasks_in_flight"] is False

    def test_a_task_still_queued_after_chaos_stops_fails_the_run(self):
        rows = _rows()
        rows["by_status"]["QUEUED"] = 1
        assert _healthy(rows=rows)["converged_no_tasks_in_flight"] is False

    def test_never_converging_is_not_reported_as_converging_slowly(self):
        checks = _healthy(converged_seconds=None)
        assert checks["converged_within_timeout"] is False

    def test_queue_depth_is_reported_but_never_asserted(self):
        # Depth is global — a deployed environment has work of its own —
        # so asserting it would fail a correct run against staging.
        assert "queue_drained" not in _healthy()


class TestTheRunMustActuallyDoSomething:
    def test_a_run_that_applied_no_faults_fails(self):
        checks = _healthy(chaos={"applied": 0})
        assert checks["chaos_was_actually_applied"] is False

    def test_an_unanswered_injection_on_a_live_session_fails(self):
        checks = _healthy(unanswered_on_live_session=1)
        assert checks["no_injection_unanswered_on_a_live_session"] is False


class TestFaultParsing:
    def test_the_default_fault_set_parses(self):
        assert chaos.parse_faults("kill,freeze,duplicate,stale") == [
            "kill", "freeze", "duplicate", "stale"]

    def test_whitespace_and_empty_entries_are_tolerated(self):
        assert chaos.parse_faults(" kill , , freeze ") == ["kill", "freeze"]

    def test_an_unknown_fault_is_refused_rather_than_dropped(self):
        # Dropping it would run three faults while the report claimed four.
        with pytest.raises(ValueError, match="unknown fault"):
            chaos.parse_faults("kill,kil,freeze")

    def test_an_empty_fault_set_is_refused(self):
        with pytest.raises(ValueError):
            chaos.parse_faults(",,")

    def test_every_advertised_fault_has_a_handler(self):
        runner = chaos.ChaosRunner(_args(), [])
        handlers = {"kill", "freeze", "duplicate", "stale", "command"}
        assert set(chaos.FAULTS) == handlers
        for name in handlers:
            assert hasattr(runner, f"_{name}")


# --------------------------------------------------------------------------
# The bookkeeping that feeds the verdict
# --------------------------------------------------------------------------

def _args(**overrides) -> argparse.Namespace:
    values = {
        "seed": 7,
        "chaos_command": [],
        "faults": ["kill", "freeze", "duplicate", "stale"],
        "fault_interval": 0.01,
        "freeze_seconds": 0.05,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class FakeWorker:
    """Enough of a `SimWorker` for the runner's bookkeeping."""

    def __init__(self, worker_id="w1", sessions=1):
        self.worker_id = worker_id
        self.failed = None
        self.frozen = False
        self.sessions = sessions
        self.received: list[str] = []
        self.completed: set[str] = set()
        self.submitted: dict[str, dict] = {}
        self.awaiting_injection: set[str] = set()
        self.injection_outcomes: list[dict] = []
        self.kills = 0
        self.resubmitted: list[tuple[str, dict]] = []

    @property
    def is_connected(self) -> bool:
        return True

    async def kill_session(self) -> bool:
        self.kills += 1
        return True

    async def resubmit(self, task_id: str, envelope: dict) -> bool:
        self.awaiting_injection.add(task_id)
        self.resubmitted.append((task_id, envelope))
        return True


class TestUnansweredInjections:
    def test_an_injection_lost_to_a_killed_socket_is_excused(self):
        # The harness aborted that connection itself; an ack was never
        # going to arrive, and failing the run for it would fail it for
        # doing its job.
        worker = FakeWorker(sessions=1)
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0}
        runner = chaos.ChaosRunner(_args(), [worker])
        assert asyncio.run(runner._duplicate()) is True
        worker.sessions = 2                     # the socket died and came back
        assert runner.unanswered([worker]) == {"excused_session_ended": 1,
                                               "unanswered_on_live_session": 0}

    def test_an_injection_ignored_on_a_live_session_is_not_excused(self):
        worker = FakeWorker(sessions=1)
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._duplicate())
        assert runner.unanswered([worker]) == {"excused_session_ended": 0,
                                               "unanswered_on_live_session": 1}

    def test_an_answered_injection_counts_as_neither(self):
        worker = FakeWorker()
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._duplicate())
        worker.awaiting_injection.clear()       # the ack landed
        assert runner.unanswered([worker]) == {"excused_session_ended": 0,
                                               "unanswered_on_live_session": 0}


class TestInjectionShape:
    def test_a_duplicate_is_the_stored_envelope_unchanged(self):
        worker = FakeWorker()
        envelope = {"task_id": "t1", "attempt_number": 2, "idempotency_token": "abc"}
        worker.submitted["t1"] = envelope
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._duplicate())
        _, sent = worker.resubmitted[0]
        assert sent["idempotency_token"] == "abc"
        assert sent["attempt_number"] == 2

    def test_a_stale_submission_names_an_earlier_attempt_and_a_new_token(self):
        worker = FakeWorker()
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 2,
                                  "idempotency_token": "abc"}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._stale())
        _, sent = worker.resubmitted[0]
        assert sent["attempt_number"] == 1
        assert sent["idempotency_token"] != "abc"

    def test_a_stale_submission_of_a_first_attempt_stays_at_zero(self):
        # There is no attempt below 0. The submission is still a second
        # body for a settled task and must still be refused.
        worker = FakeWorker()
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0,
                                  "idempotency_token": "abc"}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._stale())
        assert worker.resubmitted[0][1]["attempt_number"] == 0

    def test_the_stored_envelope_is_not_mutated_by_an_injection(self):
        # It is the worker's record of what it really submitted. Mutating
        # it would corrupt every later comparison.
        worker = FakeWorker()
        envelope = {"task_id": "t1", "attempt_number": 2, "idempotency_token": "abc"}
        worker.submitted["t1"] = envelope
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._stale())
        assert envelope == {"task_id": "t1", "attempt_number": 2, "idempotency_token": "abc"}

    def test_a_task_is_injected_into_at_most_once(self):
        worker = FakeWorker()
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0}
        runner = chaos.ChaosRunner(_args(), [worker])
        assert asyncio.run(runner._duplicate()) is True
        assert asyncio.run(runner._duplicate()) is False   # no candidate left

    def test_an_injection_with_nothing_submitted_yet_is_skipped_not_faked(self):
        runner = chaos.ChaosRunner(_args(), [FakeWorker()])
        assert asyncio.run(runner._duplicate()) is False


class TestFaultSelection:
    def test_a_frozen_worker_is_not_chosen_again(self):
        worker = FakeWorker()
        worker.frozen = True
        runner = chaos.ChaosRunner(_args(), [worker])
        assert asyncio.run(runner._kill()) is False

    def test_a_failed_worker_is_never_chosen(self):
        worker = FakeWorker()
        worker.failed = "register failed"
        runner = chaos.ChaosRunner(_args(), [worker])
        assert asyncio.run(runner._kill()) is False

    def test_a_kill_is_recorded_with_the_work_it_interrupted(self):
        worker = FakeWorker()
        worker.received = ["t1", "t2", "t3"]
        worker.completed = {"t1"}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._kill())
        assert runner.events[0]["fault"] == "kill"
        assert runner.events[0]["in_flight"] == 2

    def test_a_skipped_fault_is_counted_rather_than_ignored(self):
        # A run whose duplicate injections all skipped proved nothing about
        # duplicates, and the report has to be able to say so.
        runner = chaos.ChaosRunner(_args(), [FakeWorker()])
        asyncio.run(runner._duplicate())
        runner.skipped["duplicate"] += 1
        assert runner.report()["skipped"] == {"duplicate": 1}

    def test_the_same_seed_produces_the_same_schedule(self):
        workers = [FakeWorker(f"w{i}") for i in range(8)]
        picks = []
        for _ in range(2):
            runner = chaos.ChaosRunner(_args(seed=99), workers)
            picks.append([runner.random.choice(workers).worker_id for _ in range(20)])
        assert picks[0] == picks[1]

    def test_a_freeze_thaws_itself(self):
        async def scenario():
            worker = FakeWorker()
            runner = chaos.ChaosRunner(_args(freeze_seconds=0.05), [worker])
            await runner._freeze()
            assert worker.frozen is True
            await asyncio.sleep(0.2)
            return worker.frozen

        assert asyncio.run(scenario()) is False

    def test_the_command_fault_needs_a_command(self):
        runner = chaos.ChaosRunner(_args(), [FakeWorker()])
        assert asyncio.run(runner._command()) is False

    def test_the_runner_reports_what_it_applied(self):
        worker = FakeWorker()
        worker.submitted["t1"] = {"task_id": "t1", "attempt_number": 0}
        runner = chaos.ChaosRunner(_args(), [worker])
        asyncio.run(runner._kill())
        asyncio.run(runner._duplicate())
        report = runner.report()
        assert report["applied"] == 2
        assert report["by_fault"] == {"kill": 1, "duplicate": 1}
        assert report["seed"] == 7


class TestExitCode:
    def test_a_failed_check_exits_one_and_names_it(self, capsys, monkeypatch):
        monkeypatch.setattr(chaos.asyncio, "run", lambda _coro: {
            "checks": {"no_task_lost": False, "converged_no_tasks_in_flight": True}})
        code = chaos.main(["--url", "https://x", "--enrollment-secret", "e",
                           "--admin-secret", "a"])
        assert code == 1
        assert "FAIL: no_task_lost" in capsys.readouterr().err

    def test_a_clean_run_exits_zero(self, capsys, monkeypatch):
        monkeypatch.setattr(chaos.asyncio, "run", lambda _coro: {"checks": {"ok": True}})
        code = chaos.main(["--url", "https://x", "--enrollment-secret", "e",
                           "--admin-secret", "a"])
        assert code == 0
        assert "PASS" in capsys.readouterr().err

    def test_missing_credentials_exit_two_without_running_anything(self, monkeypatch):
        # **The environment has to be cleared, and finding that out is the
        # point of running the whole suite rather than this file.** Both
        # credentials default to `os.environ`, so this test passed alone
        # and failed inside a suite run that exports them — it was asserting
        # "missing" against an environment where they were present.
        monkeypatch.delenv("ENROLLMENT_SECRET", raising=False)
        monkeypatch.delenv("ADMIN_SECRET", raising=False)
        monkeypatch.setattr(chaos.asyncio, "run",
                            lambda _coro: pytest.fail("should not have run"))
        assert chaos.main(["--url", "https://x"]) == 2

    def test_an_unreachable_coordinator_is_a_verdict_not_a_traceback(self, capsys, monkeypatch):
        import urllib.error

        def explode(_coro):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(chaos.asyncio, "run", explode)
        code = chaos.main(["--url", "https://x", "--enrollment-secret", "e",
                           "--admin-secret", "a"])
        assert code == 1
        assert "coordinator_reachable_throughout" in capsys.readouterr().err

    def test_the_command_fault_without_a_command_is_refused_up_front(self):
        assert chaos.main(["--url", "https://x", "--enrollment-secret", "e",
                           "--admin-secret", "a", "--faults", "command"]) == 2
