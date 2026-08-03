"""Tests for the Step 2.8 load harness's decision logic.

The harness's *measurements* can only come from a live run — that is the
whole point of it. What can be tested here is everything that turns a run
into a verdict: the percentile estimator, the summary shape, timestamp
parsing, the throughput source choice, and the pass/fail checks. Those are
pure functions, and they are the ones that would silently report a green
run as green while a task went missing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("loadtest", _ROOT / "scripts" / "loadtest.py")
loadtest = importlib.util.module_from_spec(_SPEC)
sys.modules["loadtest"] = loadtest
_SPEC.loader.exec_module(loadtest)


class TestPercentile:
    def test_nearest_rank_returns_a_value_that_is_in_the_sample(self):
        values = [0.1, 0.2, 0.3, 0.4, 0.5]
        for fraction in (0.5, 0.95, 0.99):
            assert loadtest.percentile(values, fraction) in values

    def test_p50_of_a_hundred_ordered_values(self):
        assert loadtest.percentile([float(i) for i in range(1, 101)], 0.50) == 50.0

    def test_p95_and_p99_of_a_hundred_ordered_values(self):
        values = [float(i) for i in range(1, 101)]
        assert loadtest.percentile(values, 0.95) == 95.0
        assert loadtest.percentile(values, 0.99) == 99.0

    def test_unsorted_input_is_ordered_first(self):
        assert loadtest.percentile([9.0, 1.0, 5.0], 0.5) == 5.0

    def test_single_value(self):
        assert loadtest.percentile([2.5], 0.99) == 2.5

    def test_empty_is_none_rather_than_zero(self):
        # Zero would read as "every task was instant" on an empty run.
        assert loadtest.percentile([], 0.95) is None


class TestSummarise:
    def test_empty_reports_only_a_count(self):
        assert loadtest.summarise([]) == {"count": 0}

    def test_carries_every_percentile_the_exit_criteria_name(self):
        summary = loadtest.summarise([float(i) for i in range(1, 101)])
        assert summary["count"] == 100
        for key in ("min", "p50", "p95", "p99", "max", "mean"):
            assert key in summary
        assert summary["min"] == 1.0
        assert summary["max"] == 100.0

    def test_percentiles_are_monotonic(self):
        summary = loadtest.summarise([0.5, 1.0, 2.0, 8.0, 30.0, 100.0])
        assert summary["min"] <= summary["p50"] <= summary["p95"] <= summary["p99"] <= summary["max"]


class TestParseTimestamp:
    def test_parses_the_operator_api_timestamp_format(self):
        assert loadtest._parse_ts("2026-07-31T10:04:30.103471+00:00") is not None

    def test_offsets_are_honoured_rather_than_dropped(self):
        # A naive parse would make these the same instant and understate
        # every latency computed across a UTC-offset boundary.
        utc = loadtest._parse_ts("2026-07-31T10:00:00+00:00")
        plus_one = loadtest._parse_ts("2026-07-31T10:00:00+01:00")
        assert utc - plus_one == pytest.approx(3600.0)

    def test_none_and_garbage_are_none(self):
        assert loadtest._parse_ts(None) is None
        assert loadtest._parse_ts("") is None
        assert loadtest._parse_ts("not-a-timestamp") is None


class TestThroughput:
    def test_prefers_the_coordinator_stamps_when_they_span_a_window(self):
        stamps = [100.0, 101.0, 102.0, 103.0, 104.0]
        result = loadtest.throughput(stamps, fallback_seconds=99.0, count=5)
        assert result["source"] == "coordinator completed_at span"
        assert result["window_seconds"] == 4.0
        assert result["tasks_per_second"] == 1.2

    def test_falls_back_to_the_wall_clock_when_every_stamp_is_identical(self):
        # A bulk completion inside one clock tick has no span to divide by.
        result = loadtest.throughput([5.0, 5.0, 5.0], fallback_seconds=2.0, count=3)
        assert result["source"] == "harness wall clock"
        assert result["tasks_per_second"] == 1.5

    def test_falls_back_when_there_are_no_stamps_at_all(self):
        result = loadtest.throughput([], fallback_seconds=4.0, count=8)
        assert result["source"] == "harness wall clock"
        assert result["tasks_per_second"] == 2.0

    def test_none_stamps_are_ignored_not_counted_as_zero(self):
        result = loadtest.throughput([None, 10.0, 12.0], fallback_seconds=99.0, count=3)
        assert result["tasks"] == 2
        assert result["window_seconds"] == 2.0


class TestBurstChecks:
    @staticmethod
    def _rows(**overrides):
        rows = {"rows": 100, "distinct_task_ids": 100, "completed": 100, "with_result": 100}
        rows.update(overrides)
        return rows

    def test_a_clean_run_passes_every_check(self):
        checks = loadtest._burst_checks(100, 100, self._rows(), [])
        assert all(checks.values())

    def test_a_task_that_never_completed_fails(self):
        checks = loadtest._burst_checks(100, 100, self._rows(completed=99, with_result=99), [])
        assert checks["every_task_completed"] is False

    def test_a_task_row_that_never_appeared_fails(self):
        # The loss case the whole harness exists to catch: enqueued, and
        # not in the coordinator's own listing afterwards.
        checks = loadtest._burst_checks(
            100, 100, self._rows(rows=99, distinct_task_ids=99, completed=99, with_result=99), []
        )
        assert checks["every_task_read_back"] is False

    def test_a_duplicate_assignment_fails(self):
        checks = loadtest._burst_checks(100, 100, self._rows(), ["dup-task-id"])
        assert checks["no_duplicate_assignments"] is False

    def test_a_completion_with_no_stored_result_fails(self):
        checks = loadtest._burst_checks(100, 100, self._rows(with_result=99), [])
        assert checks["every_completion_has_a_result"] is False

    def test_a_short_accept_fails_rather_than_rescaling_the_target(self):
        checks = loadtest._burst_checks(
            90, 100, self._rows(rows=90, distinct_task_ids=90, completed=90, with_result=90), []
        )
        assert checks["every_task_accepted"] is False


class TestKeptUp:
    """The regression guard for a verdict that was wrong on a real run.

    The first version judged the depth *after* the offer stopped, so a
    sustained run whose queue climbed to 2,116 during the hold reported
    `queue_kept_up: true` — it had drained back to zero by the time the
    last sample was taken. The samples handed to `kept_up` must come from
    the offering window, and the rule must catch accumulation inside it.
    """

    def test_a_flat_queue_kept_up(self):
        assert loadtest.kept_up([0, 0, 4, 0, 8, 0], batch=60) is True

    def test_a_monotonically_climbing_queue_did_not(self):
        climbing = [0, 82, 204, 306, 456, 732, 1018, 1516, 2116]
        assert loadtest.kept_up(climbing, batch=150) is False

    def test_the_real_over_capacity_run_is_not_rescued_by_its_drain(self):
        # The exact defect: these are the hold samples, and the tail the
        # old rule looked at is deliberately absent.
        assert loadtest.kept_up([0, 82, 2116], batch=150) is False

    def test_a_backlog_within_one_batch_is_not_accumulation(self):
        # One batch lands at once; carrying it briefly is keeping up.
        assert loadtest.kept_up([0, 60], batch=60) is True

    def test_a_backlog_over_one_batch_is(self):
        assert loadtest.kept_up([0, 61], batch=60) is False

    def test_too_few_samples_does_not_fail_the_run(self):
        assert loadtest.kept_up([], batch=60) is True
        assert loadtest.kept_up([500], batch=60) is True


class TestParser:
    def test_every_scenario_is_reachable_from_the_command_line(self):
        parser = loadtest.build_parser()
        for scenario in loadtest.SCENARIOS:
            assert parser.parse_args([scenario, "--url", "https://x"]).scenario == scenario

    def test_missing_credentials_exit_two_rather_than_running(self, monkeypatch):
        # The environment must be cleared explicitly. Both credentials fall
        # back to environment variables, and CI sets both — so without this
        # the guard is satisfied, `main` goes on to run a **real load
        # scenario** against a bogus host, and the test asserts nothing
        # while adding half a minute of DNS failures to the suite.
        monkeypatch.delenv("ENROLLMENT_SECRET", raising=False)
        monkeypatch.delenv("ADMIN_SECRET", raising=False)
        monkeypatch.delenv("COORDINATOR_URL", raising=False)
        assert loadtest.main(["burst", "--url", "https://x"]) == 2

    def test_a_missing_url_also_exits_two(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_URL", raising=False)
        assert loadtest.main(["burst", "--enrollment-secret", "e",
                              "--admin-secret", "a"]) == 2
