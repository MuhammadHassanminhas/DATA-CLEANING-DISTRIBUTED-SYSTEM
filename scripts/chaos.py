#!/usr/bin/env python3
"""Chaos testing harness — Step 3.8. Scripted, repeatable, versioned (§4).

Step 2.8's load harness asks "how fast, and does anything get lost when
nothing goes wrong". This one asks the opposite question: **things go wrong
continuously, on purpose, for the whole run — does the ledger still add
up.** It reuses that harness rather than reimplementing it. Every simulated
worker here is a `loadtest.SimWorker`: a real registration, a real
WebSocket session, real result envelopes. The coordinator cannot tell one
from a container (invariant §3.5), and that is what makes the faults real
faults rather than a mock of one.

The faults
----------
Four are injected by the harness itself, over the protocol, so they work
identically against local Docker and against public staging over the
Internet — which is what the "runs against staging" criterion needs:

`kill`       `transport.abort()`: the socket dies with no close frame, the
             way a killed container's does. The worker reconnects on its
             existing identity, so this is a restarted container, not a
             machine that never comes back.
`freeze`     The session stops reading, heartbeating and executing for
             `--freeze-seconds` while the socket stays open — `docker
             pause` in a flag. This is the *silent worker* detection path
             (≤65s, Step 3.1), not the close path (≤35s), and on release it
             is what produces a genuine stale submission for a task the
             worker has already lost.
`duplicate`  A completed task's exact envelope, re-sent. Must come back
             `duplicate` and must not create a second result (Step 3.3).
`stale`      A submission carrying an earlier attempt number and a fresh
             idempotency token. Must be refused — `fenced`, `superseded` or
             `not_owner` — and must never transition a task (Step 3.4).

Everything below the protocol — coordinator eviction, a database blip, a
Redis blip — is an environment operation, not something a harness can know
how to do in every environment. It is `--chaos-command`, repeatable, run on
the same schedule as the injected faults, exactly as Step 3.5's
`--restart-command` did:

    --chaos-command "docker compose -p dcds38 restart redis"
    --chaos-command "kubectl -n staging delete pod -l app=coordinator --field-selector=..."

The invariants
--------------
From the design gate's own list for this step (§3.0.13): **zero task loss,
zero double completion, and convergence to no `ASSIGNED`/`RUNNING` rows
after chaos stops.** Every one is counted from the coordinator's tables
through the operator API, never from this process's own tally — the harness
knows what it was told, the rows are what happened (Decision #140).

Any violation exits **1** with a `FAIL:` line naming it. A chaos run that
cannot say which invariant broke is not a chaos run.

Usage
-----
    python scripts/chaos.py \
        --url https://localhost:8443 \
        --enrollment-secret "$ENROLLMENT_SECRET" \
        --admin-secret "$ADMIN_SECRET" \
        --workers 10 --tasks 1000 --insecure

`--insecure` skips certificate verification and is for the local dev CA
only; the public ingress carries a real Let's Encrypt certificate and needs
no such flag.

Needs the `websockets` package and `worker/executors.py` on the path — the
same venv `scripts/loadtest.py` runs in.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import shlex
import sys
import time
import urllib.error
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

# Loaded by path rather than imported by name: `scripts/` is not a package
# and never has been, and adding an `__init__.py` to make one import
# statement prettier would change how every other script in it is run.
_SPEC = importlib.util.spec_from_file_location("loadtest", os.path.join(_HERE, "loadtest.py"))
loadtest = importlib.util.module_from_spec(_SPEC)  # type: ignore[arg-type]
sys.modules["loadtest"] = loadtest
_SPEC.loader.exec_module(loadtest)  # type: ignore[union-attr]

FAULTS = ("kill", "freeze", "duplicate", "stale", "command")

# **Not the load harness's workload, and the difference is the whole
# scenario.** Step 2.8 wants the cheapest possible task so that what
# saturates is the coordinator; chaos wants tasks that are still *running*
# when a fault lands, because a fault that arrives between tasks interrupts
# nothing. The first smoke run of this harness drained 100 `count_to_n`
# tasks in 5.5 seconds — before the first fault fired — and correctly
# failed itself on `chaos_was_actually_applied`.
#
# `sleep` rather than a CPU workload because the fault load has to last
# ~100 seconds: 1,000 CPU tasks would make this process the bottleneck
# rather than the system under test, and a sleeping task holds a real
# lease, a real credit and a real assignment row exactly like a working
# one. The lease is what the faults are aimed at.
DEFAULT_WORKLOAD = {"task_type": "sleep", "parameters": {"seconds": 2}}

# The verdicts a submission the coordinator did not ask for is allowed to
# receive. `transitioned` is absent on purpose and is the whole point: an
# injected duplicate or stale result that completes a task is a double
# completion, which is the invariant this step exists to disprove.
REFUSAL_OUTCOMES = frozenset({"duplicate", "superseded", "fenced", "not_owner",
                              "not_found", "illegal", "rejected"})

# Statuses that mean a task is still somewhere in the pipeline. Convergence
# is their absence.
IN_FLIGHT_STATUSES = ("QUEUED", "ASSIGNED", "RUNNING")


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------

class ChaosRunner:
    """Applies one fault every `--fault-interval` seconds until told to stop.

    Random by design, and seeded so a run can be repeated (§4 asks for
    repeatable, not for unpredictable). What is *not* random is the fault
    set: it is whatever `--faults` names, so a run can isolate one fault
    when a general run finds something.
    """

    def __init__(self, args, workers: list) -> None:
        self.args = args
        self.workers = workers
        self.events: list[dict] = []
        self.skipped: Counter = Counter()
        self.random = random.Random(args.seed)
        self._commands = list(args.chaos_command or [])
        self._command_index = 0
        self._injected: set[str] = set()
        self._started = time.monotonic()
        # Held rather than fired and forgotten: asyncio keeps only a weak
        # reference to a running task, so an unreferenced thaw can be
        # garbage-collected mid-sleep and leave a worker frozen for the
        # rest of the run — which would read as a system that never
        # recovered.
        self._thaws: set[asyncio.Task] = set()
        # task_id -> the worker's session count when a submission was
        # injected into it. What makes an unanswered injection readable:
        # see `unanswered`.
        self._injected_at_session: dict[str, int] = {}

    # -- helpers ------------------------------------------------------

    def _live(self) -> list:
        """Workers currently holding a session and not already frozen."""
        return [w for w in self.workers
                if w.failed is None and w.is_connected and not w.frozen]

    def _record(self, fault: str, worker, detail: dict) -> None:
        self.events.append({
            "t": round(time.monotonic() - self._started, 2),
            "fault": fault,
            "worker_id": (worker.worker_id if worker else None),
            **detail,
        })

    # -- the faults ---------------------------------------------------

    async def _kill(self) -> bool:
        live = self._live()
        if not live:
            return False
        worker = self.random.choice(live)
        killed = await worker.kill_session()
        if killed:
            self._record("kill", worker, {"in_flight": len(worker.received) - len(worker.completed)})
        return killed

    async def _freeze(self) -> bool:
        live = self._live()
        if not live:
            return False
        worker = self.random.choice(live)
        worker.frozen = True
        self._record("freeze", worker, {"seconds": self.args.freeze_seconds,
                                        "in_flight": len(worker.received) - len(worker.completed)})
        # Thawed on its own clock rather than inline, so one frozen worker
        # does not stop the rest of the chaos schedule.
        thaw = asyncio.create_task(self._thaw(worker, self.args.freeze_seconds))
        self._thaws.add(thaw)
        thaw.add_done_callback(self._thaws.discard)
        return True

    async def _thaw(self, worker, seconds: float) -> None:
        await asyncio.sleep(seconds)
        worker.frozen = False
        self._record("thaw", worker, {})

    def _injectable(self):
        """(worker, task_id, envelope) for a task that has been submitted once.

        Only tasks this worker genuinely submitted are candidates, and each
        is used at most once — a second injection into the same task would
        be answered from the same terminal state and would prove nothing the
        first did not.
        """
        candidates = [
            (w, task_id, envelope)
            for w in self.workers
            if w.failed is None and w.is_connected and not w.frozen
            for task_id, envelope in w.submitted.items()
            if task_id not in self._injected
        ]
        return self.random.choice(candidates) if candidates else None

    async def _duplicate(self) -> bool:
        choice = self._injectable()
        if choice is None:
            return False
        worker, task_id, envelope = choice
        self._injected.add(task_id)
        self._injected_at_session[task_id] = worker.sessions
        sent = await worker.resubmit(task_id, dict(envelope))
        if sent:
            self._record("duplicate", worker, {"task_id": task_id,
                                               "attempt_number": envelope.get("attempt_number")})
        return sent

    async def _stale(self) -> bool:
        choice = self._injectable()
        if choice is None:
            return False
        worker, task_id, envelope = choice
        self._injected.add(task_id)
        stale = dict(envelope)
        # An *earlier* attempt, and a token the coordinator has never seen.
        # The token matters: with the stored one this would be a duplicate,
        # which is Step 3.3's path and not this fault. Attempt 0 has no
        # earlier attempt, so it stays 0 — for a task that has since been
        # reassigned that is genuinely stale, and for one that has not it is
        # a second body for a settled task, which must still be refused.
        stale["attempt_number"] = max(0, int(envelope.get("attempt_number") or 0) - 1)
        stale["idempotency_token"] = uuid.uuid4().hex
        self._injected_at_session[task_id] = worker.sessions
        sent = await worker.resubmit(task_id, stale)
        if sent:
            self._record("stale", worker, {"task_id": task_id,
                                           "attempt_number": stale["attempt_number"]})
        return sent

    async def _command(self) -> bool:
        if not self._commands:
            return False
        command = self._commands[self._command_index % len(self._commands)]
        self._command_index += 1
        result = await _run_command(command)
        self._record("command", None, result)
        return True

    # -- the schedule -------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        handlers = {
            "kill": self._kill,
            "freeze": self._freeze,
            "duplicate": self._duplicate,
            "stale": self._stale,
            "command": self._command,
        }
        enabled = [f for f in self.args.faults if f in handlers]
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.args.fault_interval)
                return
            except asyncio.TimeoutError:
                pass
            fault = self.random.choice(enabled)
            try:
                applied = await handlers[fault]()
            except Exception as exc:  # noqa: BLE001 — a fault that fails is data, not a crash
                self.skipped[f"{fault}:{type(exc).__name__}"] += 1
                continue
            if not applied:
                # Counted rather than ignored: a run whose duplicate
                # injections all skipped for want of a candidate proved
                # nothing about duplicates, and the report has to say so.
                self.skipped[fault] += 1

    def unanswered(self, workers: list) -> dict[str, int]:
        """Injections that never got an ack, split by whether that is a fault.

        **The split is the point, and without it this harness fails its own
        run for doing its job.** An injected submission whose socket was
        killed a moment later is an ack that was never going to arrive —
        this harness aborted the connection itself. One on a session that
        stayed up the whole time is different: the coordinator was asked a
        question over a live socket and did not answer, which is a defect
        and must fail the run.

        The discriminator is the worker's session count, captured when the
        submission was injected. More sessions now than then means the
        socket died in between.
        """
        excused = live = 0
        for worker in workers:
            for task_id in worker.awaiting_injection:
                at = self._injected_at_session.get(task_id)
                if at is None or worker.sessions != at:
                    excused += 1
                else:
                    live += 1
        return {"excused_session_ended": excused, "unanswered_on_live_session": live}

    def report(self) -> dict[str, Any]:
        by_fault = Counter(event["fault"] for event in self.events)
        return {
            "seed": self.args.seed,
            "interval_seconds": self.args.fault_interval,
            "enabled": list(self.args.faults),
            "applied": sum(by_fault[f] for f in FAULTS),
            "by_fault": dict(by_fault),
            "skipped": dict(self.skipped),
            "events": self.events,
        }


async def _run_command(command: str) -> dict[str, Any]:
    """Run an environment fault and report what it did.

    `shlex.split` rather than `shell=True`, for the same reason Step 3.5's
    restart command does it: a shell would make the reported return code
    the shell's rather than that of the thing meant to break.
    """
    process = await asyncio.create_subprocess_exec(
        *shlex.split(command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return {
        "command": command,
        "returncode": process.returncode,
        "output": (output or b"").decode(errors="replace").strip()[-1000:],
    }


# --------------------------------------------------------------------------
# Convergence and invariants
# --------------------------------------------------------------------------

async def wait_for_convergence(args, correlation_id: str, expected: int,
                               timeout: float) -> tuple[float | None, dict]:
    """Poll the batch until nothing of it is in flight, or give up.

    Returns `(seconds, rows)` and `None` for the seconds if it never
    converged — so a run that hung cannot be read as one that converged
    slowly. `rows` is the last read either way, because the report is more
    useful with the shape of the failure in it than without.

    **Polling `GET /tasks` rather than `GET /tasks/depth`**: depth counts
    the queue, and a task stuck `RUNNING` on a worker that will never
    answer is not in the queue. Convergence is about the rows.
    """
    started = time.monotonic()
    rows: dict = {}
    while time.monotonic() - started < timeout:
        rows = await asyncio.to_thread(loadtest.read_back, args, correlation_id)
        by_status = rows.get("by_status") or {}
        in_flight = sum(by_status.get(status, 0) for status in IN_FLIGHT_STATUSES)
        if in_flight == 0 and rows.get("rows", 0) >= expected:
            return round(time.monotonic() - started, 3), rows
        await asyncio.sleep(args.converge_poll_seconds)
    return None, rows


def evaluate(*, accepted: int, requested: int, rows: dict, injections: list[dict],
             chaos: dict, converged_seconds: float | None,
             unanswered_on_live_session: int) -> dict[str, bool]:
    """The verdict. Pure, so it can be tested without a coordinator.

    Every check is phrased so that `True` is the healthy answer, because
    `main` fails the run on any `False` and a check that reads the other way
    round would invert silently.
    """
    by_status = rows.get("by_status") or {}
    in_flight = sum(by_status.get(status, 0) for status in IN_FLIGHT_STATUSES)
    refused = [i for i in injections if i["outcome"] in REFUSAL_OUTCOMES]
    return {
        # A green run that injected nothing is the failure mode this whole
        # step exists to avoid: it would report "invariants hold under
        # chaos" having applied no chaos.
        "chaos_was_actually_applied": chaos.get("applied", 0) > 0,
        "every_task_accepted": accepted == requested,
        "every_task_read_back": rows.get("rows", 0) == accepted,
        "no_duplicate_task_rows": rows.get("distinct_task_ids", -1) == rows.get("rows", 0),
        # Zero loss, the criterion's own words. A task that exhausted its
        # retries is FAILED rather than lost, and it still fails this check
        # — deliberately: under a fault load the fleet is meant to survive,
        # a terminal failure is a result worth stopping for, not rounding
        # off. `by_status` in the report says which it was.
        "no_task_lost": rows.get("completed", 0) == accepted,
        "every_completion_has_one_result": rows.get("with_result", -1) == rows.get("completed", 0),
        # Zero double completion. An injected submission that transitioned a
        # task is exactly that, and it is the one outcome not in the refusal
        # set.
        "no_injected_submission_completed_a_task": all(
            i["outcome"] != "transitioned" for i in injections),
        # Every acked injection was refused or deduplicated — no unknown
        # verdict slipped through — and no submission on a *live* socket
        # went unanswered. An injection whose socket this harness then
        # killed is excused and reported; see `ChaosRunner.unanswered`.
        "every_acked_injection_was_refused": len(refused) == len(injections),
        "no_injection_unanswered_on_a_live_session": unanswered_on_live_session == 0,
        # The gate's own words for this step (§3.0.13): convergence is the
        # absence of in-flight *rows*, not an empty queue. Queue depth is
        # global — a deployed environment has work of its own — so it is
        # reported as context and never asserted.
        "converged_no_tasks_in_flight": in_flight == 0,
        "converged_within_timeout": converged_seconds is not None,
    }


# --------------------------------------------------------------------------
# The scenario
# --------------------------------------------------------------------------

async def run_chaos(args) -> dict[str, Any]:
    workers, runners, stop = await loadtest.connect_fleet(args, args.workers)
    connected = sum(1 for w in workers if w.failed is None)

    sampler = loadtest.Sampler(args)
    sampler.start()

    correlation_id = str(uuid.uuid4())
    enqueue_seconds, accepted = await asyncio.to_thread(
        loadtest.enqueue, args, args.task_type, json.loads(args.parameters),
        args.tasks, correlation_id,
    )

    # Chaos starts *after* the enqueue and runs for as long as the drain
    # does. Starting it earlier would spend faults on an idle fleet, which
    # is the one moment they prove nothing.
    chaos_stop = asyncio.Event()
    chaos = ChaosRunner(args, workers)
    chaos_task = asyncio.create_task(chaos.run(chaos_stop))

    drain_seconds = await loadtest.drain_wait(args, workers, accepted, args.timeout)

    # **Chaos stops here, and everything it left frozen is thawed.** The
    # convergence criterion is about what the system does once the faults
    # stop, so leaving a worker frozen into that window would be measuring
    # the fault, not the recovery.
    chaos_stop.set()
    await chaos_task
    for worker in workers:
        worker.frozen = False

    converged_seconds, rows = await wait_for_convergence(
        args, correlation_id, accepted, args.converge_timeout)

    await asyncio.sleep(2.0)
    await sampler.stop()
    try:
        depth_final = (await asyncio.to_thread(loadtest.depth, args)).get("depth")
    except Exception:  # noqa: BLE001 — reported as absent rather than invented (§10)
        depth_final = None

    delivered = [task_id for w in workers for task_id in w.received]
    redeliveries = [t for t, n in Counter(delivered).items() if n > 1]
    injections = [i for w in workers for i in w.injection_outcomes]
    outstanding = chaos.unanswered(workers)
    reconnect_times = [t for w in workers for t in w.reconnect_seconds]
    registrations = sum(w.registrations for w in workers)

    await loadtest.shutdown_fleet(runners, stop)

    completions = rows.pop("_completions", [])
    attempts = await asyncio.to_thread(_recovery_feed, args)

    return {
        "scenario": "chaos",
        "correlation_id": correlation_id,
        "fleet": {
            "requested": args.workers,
            "connected": connected,
            "credits_each": args.max_concurrent,
            "registrations": registrations,
            "sessions_total": sum(w.sessions for w in workers),
            "reconnect_seconds": loadtest.summarise(reconnect_times),
            "kills": sum(w.kills for w in workers),
        },
        "enqueue": {
            "requested": args.tasks,
            "accepted": accepted,
            "seconds": round(enqueue_seconds, 3),
        },
        "chaos": chaos.report(),
        "injections": {
            "acked": len(injections),
            "by_outcome": dict(Counter(i["outcome"] for i in injections)),
            **outstanding,
        },
        "drain_seconds": round(drain_seconds, 3),
        "converge_seconds": converged_seconds,
        "queue_depth_final": depth_final,
        "delivery": {
            "delivered": len(delivered),
            "distinct": len(set(delivered)),
            # Not a fault: a task whose worker was killed mid-execution is
            # meant to be delivered again. That is at-least-once delivery
            # (§3.0.5); the ledger checks are what prove it completed once.
            "redeliveries": len(redeliveries),
            "refusals": sum(len(w.refused) for w in workers),
        },
        "recovery": attempts,
        "latency": {
            "end_to_end_seconds": rows.get("end_to_end_seconds"),
            "queue_wait_seconds": rows.get("queue_wait_seconds"),
        },
        "throughput": loadtest.throughput(completions, drain_seconds, rows.get("completed", 0)),
        "coordinator": sampler.report(),
        "read_back": {k: v for k, v in rows.items()
                      if k not in ("end_to_end_seconds", "queue_wait_seconds")},
        "rate_limited_retries": loadtest._RATE_LIMITED[0],
        "checks": evaluate(
            accepted=accepted, requested=args.tasks, rows=rows, injections=injections,
            chaos=chaos.report(), converged_seconds=converged_seconds,
            unanswered_on_live_session=outstanding["unanswered_on_live_session"],
        ),
    }


def _recovery_feed(args) -> dict[str, Any]:
    """What the coordinator itself recorded about the recovery.

    Step 3.7's `GET /tasks/attempts`, read for its outcome mix. **It is
    fleet-wide and has no correlation filter**, so this is context for the
    run rather than a count of it, and it is labelled that way in the report
    rather than quietly presented as this batch's numbers.
    """
    try:
        status, body = loadtest._request(
            f"{args.url.rstrip('/')}/tasks/attempts?limit=200", None, args.insecure,
            headers={"X-Admin-Secret": args.admin_secret},
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    if status != 200:
        return {"available": False, "detail": f"HTTP {status}"}
    rows = body.get("attempts") or []
    return {
        "available": True,
        "scope": "fleet-wide, newest 200 — not filtered to this run's tasks",
        "by_outcome": dict(Counter(row.get("outcome") for row in rows)),
        "by_reason": dict(Counter(row.get("reason") for row in rows)),
        "returned": len(rows),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=os.environ.get("COORDINATOR_URL", ""),
                        help="coordinator base URL (or COORDINATOR_URL)")
    parser.add_argument("--enrollment-secret", default=os.environ.get("ENROLLMENT_SECRET", ""))
    parser.add_argument("--admin-secret", default=os.environ.get("ADMIN_SECRET", ""))

    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--max-concurrent", type=int, default=4)
    parser.add_argument("--task-type", default=DEFAULT_WORKLOAD["task_type"])
    parser.add_argument("--parameters", default=json.dumps(DEFAULT_WORKLOAD["parameters"]),
                        help="JSON parameters for the workload")

    parser.add_argument("--faults", default="kill,freeze,duplicate,stale",
                        help=f"comma-separated subset of {','.join(FAULTS)}; "
                             "`command` needs at least one --chaos-command")
    parser.add_argument("--fault-interval", type=float, default=5.0,
                        help="seconds between faults")
    parser.add_argument("--freeze-seconds", type=float, default=75.0,
                        help="how long a frozen worker stays silent. Longer than the shipped "
                             "60s TASK_LEASE_TTL_SECONDS plus the 5s reclaim interval on "
                             "purpose: a silent worker keeps its lease until the TTL runs "
                             "out, so a shorter freeze costs it nothing and proves nothing. "
                             "Lower it to match a stack whose TTL is lowered")
    parser.add_argument("--chaos-command", action="append", default=[],
                        help="an environment fault to run on the fault schedule; repeatable")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed the fault schedule so a run can be repeated")

    parser.add_argument("--converge-timeout", type=float, default=300.0,
                        help="seconds to wait for no task to be in flight after chaos stops")
    parser.add_argument("--converge-poll-seconds", type=float, default=5.0)
    parser.add_argument("--connect-batch", type=int, default=10)
    parser.add_argument("--connect-pause", type=float, default=0.5)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds to wait for the drain before giving up")
    parser.add_argument("--json-out", help="also write the report to this file")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (local dev CA only)")
    return parser


def parse_faults(value: str) -> list[str]:
    """Split and validate `--faults`. Raises on an unknown name.

    An unknown fault is refused rather than dropped: a typo that silently
    ran three faults instead of four would produce a green report claiming
    coverage it did not have.
    """
    faults = [f.strip() for f in value.split(",") if f.strip()]
    unknown = [f for f in faults if f not in FAULTS]
    if unknown:
        raise ValueError(f"unknown fault(s): {', '.join(unknown)}; known: {', '.join(FAULTS)}")
    if not faults:
        raise ValueError("--faults must name at least one fault")
    return faults


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.url or not args.enrollment_secret or not args.admin_secret:
        print("need --url, --enrollment-secret and --admin-secret "
              "(or COORDINATOR_URL / ENROLLMENT_SECRET / ADMIN_SECRET)", file=sys.stderr)
        return 2
    try:
        args.faults = parse_faults(args.faults)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if "command" in args.faults and not args.chaos_command:
        print("the `command` fault needs at least one --chaos-command", file=sys.stderr)
        return 2
    if args.seed is None:
        args.seed = random.randrange(2 ** 31)

    started = time.time()
    try:
        report = asyncio.run(run_chaos(args))
    except urllib.error.URLError as exc:
        # The coordinator became unreachable mid-run. Under chaos that is
        # not an impossible outcome — a `--chaos-command` can take it away
        # — and it must read as FAIL rather than as a traceback with no
        # verdict at all (the defect Decision #149 fixed in the load
        # harness).
        report = {
            "scenario": "chaos",
            "aborted": "coordinator_unreachable",
            "detail": str(exc.reason),
            "checks": {"coordinator_reachable_throughout": False},
        }
    report["started_at"] = datetime.fromtimestamp(started).astimezone().isoformat()
    report["wall_seconds"] = round(time.time() - started, 2)
    report["target"] = args.url

    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text)

    checks = report.get("checks") or {}
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print("FAIL: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
