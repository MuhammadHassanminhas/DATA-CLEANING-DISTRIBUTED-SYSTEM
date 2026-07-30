"""Step 2.4 design sub-gate evidence — execution isolation, measured.

**This is not Step 2.4 code.** It contains no worker changes and no
executor the worker uses. It exists so the numbers the sub-gate decided on
can be re-checked by anyone, rather than taken on trust (CLAUDE.md §10) —
the same reason `queue_harness.py` and `assignment_harness.py` are
versioned.

The question the sub-gate turns on: the worker is a single asyncio process
whose heartbeat is a coroutine on the same event loop that would run a
task. `count_to_n` and `hash_rounds` are CPU-bound Python. If a task runs
inline, does the heartbeat stop long enough for the coordinator to declare
a perfectly healthy worker dead?

Judged against the real coordinator defaults in `coordinator/app/config.py`
and `worker/worker.py`:

    HEARTBEAT_SUSPECT_THRESHOLD_SECONDS   12
    HEARTBEAT_OFFLINE_THRESHOLD_SECONDS   25
    WS_PONG_TIMEOUT_SECONDS               45
    WORKER_HEARTBEAT_INTERVAL_SECONDS      5

Stdlib only, so it runs in any Python container with no install — same
constraint as `queue_harness.py`. `psutil` is used for RSS if present and
skipped if not.

Run it inside the deployed base image, not on the host, or the numbers
describe the wrong machine:

    docker run --rm -i --cpus=1 -v "$PWD:/bench:ro" python:3.12-slim \
      python /bench/scripts/exec_isolation_bench.py all

Subcommands: `isolation`, `concurrency`, `ceilings`, `mechanics`, `all`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor

SUSPECT_S = 12.0
OFFLINE_S = 25.0
HEARTBEAT_INTERVAL_S = 5.0

# Declared parameter ceilings, from coordinator/app/task_types.py.
CEILING_COUNT_N = 100_000_000
CEILING_HASH_ROUNDS = 10_000_000
CEILING_SLEEP_SECONDS = 3600

try:
    import psutil

    _PROC = psutil.Process()

    def _rss_mb() -> float:
        return _PROC.memory_info().rss / 1_048_576

except Exception:  # noqa: BLE001 — optional, never a hard dependency

    def _rss_mb() -> float:
        return float("nan")


# ---------------------------------------------------------------------------
# Executors shaped the way Step 2.4 would have to shape them: a chunked loop,
# because a thread cannot be killed in Python and progress has to be
# observable from outside. `progress` is a one-slot list written by the
# worker thread and read by the event loop; `cancel` is cooperative.
# ---------------------------------------------------------------------------


def run_hash_rounds(
    rounds: int,
    algorithm: str = "sha256",
    progress: list | None = None,
    cancel: threading.Event | None = None,
    chunk: int = 100_000,
):
    digest = b"\x00" * 32
    done = 0
    while done < rounds:
        if cancel is not None and cancel.is_set():
            return None
        step = min(chunk, rounds - done)
        for _ in range(step):
            digest = hashlib.new(algorithm, digest).digest()
        done += step
        if progress is not None:
            progress[0] = done / rounds
    return digest.hex()


def run_count_to_n(
    n: int,
    progress: list | None = None,
    cancel: threading.Event | None = None,
    chunk: int = 1_000_000,
):
    total = 0
    done = 0
    while done < n:
        if cancel is not None and cancel.is_set():
            return None
        step = min(chunk, n - done)
        for _ in range(step):
            total += 1
        done += step
        if progress is not None:
            progress[0] = done / n
    return total


# ---------------------------------------------------------------------------
# The heartbeat probe. Stands in for `_heartbeat_ws_loop`: it wakes every
# HEARTBEAT_INTERVAL_S and records the *actual* wall gap between wakeups.
# A gap over SUSPECT_S is a worker the coordinator has started doubting.
#
# It proves itself alive with two clean on-schedule ticks BEFORE any load is
# applied, so a stalled gap afterwards cannot be confused with a probe that
# simply never started.
# ---------------------------------------------------------------------------


async def _measure(label: str, apply_load) -> dict:
    gaps: list[float] = []
    stop = asyncio.Event()
    warm = asyncio.Event()

    async def probe() -> None:
        last = time.monotonic()
        ticks = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            ticks += 1
            if ticks == 2:
                warm.set()

    probe_task = asyncio.create_task(probe())
    await warm.wait()
    baseline = list(gaps)

    t0 = time.monotonic()
    await apply_load()
    workload = time.monotonic() - t0

    stop.set()
    await probe_task

    # Drop the trailing gap: stop() truncates it, so it measures nothing.
    during = gaps[len(baseline) : -1] or gaps[len(baseline) :]
    return {
        "scenario": label,
        "workload_seconds": round(workload, 2),
        "baseline_gaps_s": [round(g, 2) for g in baseline],
        "gaps_during_load_s": [round(g, 2) for g in during],
        "max_gap_s": round(max(during), 2) if during else None,
        "median_gap_s": round(statistics.median(during), 2) if during else None,
        "heartbeats_sent": len(during),
        "heartbeats_expected": round(workload / HEARTBEAT_INTERVAL_S, 1),
        "breached_suspect_12s": sum(1 for g in during if g > SUSPECT_S),
        "breached_offline_25s": sum(1 for g in during if g > OFFLINE_S),
        "margin_to_suspect_s": round(SUSPECT_S - max(during), 2) if during else None,
    }


def _calibrate() -> dict:
    """Measure this machine's rates so every workload below is sized in
    seconds rather than in magic constants."""
    t0 = time.perf_counter()
    run_hash_rounds(200_000)
    hash_rate = 200_000 / (time.perf_counter() - t0)

    t0 = time.perf_counter()
    run_count_to_n(5_000_000)
    count_rate = 5_000_000 / (time.perf_counter() - t0)

    return {"hash_rounds_per_sec": round(hash_rate), "count_per_sec": round(count_rate)}


# ---------------------------------------------------------------------------
# Q1 — do the declared ceilings reach the 10-minute exit criterion?
# ---------------------------------------------------------------------------


def cmd_ceilings() -> dict:
    cal = _calibrate()
    hr = cal["hash_rounds_per_sec"]
    cr = cal["count_per_sec"]

    t0 = time.perf_counter()
    run_hash_rounds(200_000, algorithm="sha512")
    hr512 = 200_000 / (time.perf_counter() - t0)

    needed = round(hr * 600)
    return {
        "calibration": cal,
        "at_declared_ceiling_seconds": {
            "count_to_n_100M": round(CEILING_COUNT_N / cr, 1),
            "hash_rounds_10M_sha256": round(CEILING_HASH_ROUNDS / hr, 1),
            "hash_rounds_10M_sha512": round(CEILING_HASH_ROUNDS / hr512, 1),
            "sleep_ceiling": CEILING_SLEEP_SECONDS,
        },
        "ten_minute_criterion": {
            "hash_rounds_needed_for_600s": needed,
            "declared_ceiling": CEILING_HASH_ROUNDS,
            "over_ceiling_factor": round(needed / CEILING_HASH_ROUNDS, 1),
            "reachable_with_cpu_task": needed <= CEILING_HASH_ROUNDS,
            "reachable_with_sleep": CEILING_SLEEP_SECONDS >= 600,
        },
    }


# ---------------------------------------------------------------------------
# Q2 — inline vs isolated. The decisive comparison.
# ---------------------------------------------------------------------------


async def cmd_isolation() -> dict:
    cal = _calibrate()
    rounds = int(cal["hash_rounds_per_sec"] * 30)  # ~30s of CPU

    async def inline():
        # The mistake under test: CPU-bound work run on the event loop that
        # also owns the heartbeat.
        run_hash_rounds(rounds)

    async def threaded():
        await asyncio.to_thread(run_hash_rounds, rounds)

    return {
        "calibration": cal,
        "rounds": rounds,
        "inline": await _measure("inline on the event loop", inline),
        "to_thread": await _measure("asyncio.to_thread", threaded),
    }


# ---------------------------------------------------------------------------
# Q3/Q4 — concurrency, and the thread-vs-process trade-off as a number
# ---------------------------------------------------------------------------


async def cmd_concurrency() -> dict:
    cal = _calibrate()
    unit = int(cal["hash_rounds_per_sec"] * 15)  # each task ~15s of CPU
    cunit = int(cal["count_per_sec"] * 15)

    def fan(n: int, fn, arg):
        async def run():
            await asyncio.gather(*[asyncio.to_thread(fn, arg) for _ in range(n)])

        return run

    out: dict = {
        "calibration": cal,
        "per_task_cpu_seconds": 15,
        "os_cpu_count": os.cpu_count(),
        "default_executor_max_workers": min(32, (os.cpu_count() or 1) + 4),
        "threads_hash_x1": await _measure("to_thread hash x1", fan(1, run_hash_rounds, unit)),
        "threads_hash_x4": await _measure("to_thread hash x4", fan(4, run_hash_rounds, unit)),
        "threads_count_x4": await _measure("to_thread count x4", fan(4, run_count_to_n, cunit)),
        "threads_hash_x8": await _measure("to_thread hash x8", fan(8, run_hash_rounds, unit)),
    }

    loop = asyncio.get_running_loop()
    try:
        with ProcessPoolExecutor(max_workers=4) as pool:
            await loop.run_in_executor(pool, run_hash_rounds, 1)  # warm; exclude spawn cost

            async def prun():
                await asyncio.gather(
                    *[loop.run_in_executor(pool, run_hash_rounds, unit) for _ in range(4)]
                )

            out["process_hash_x4"] = await _measure("ProcessPoolExecutor hash x4", prun)
    except Exception as exc:  # noqa: BLE001
        out["process_hash_x4"] = {"error": str(exc)}

    threads_s = out["threads_hash_x4"]["workload_seconds"]
    proc_s = out["process_hash_x4"].get("workload_seconds")
    out["throughput"] = {
        "serial_equivalent_seconds": 60,
        "threads_x4_seconds": threads_s,
        "process_x4_seconds": proc_s,
        "process_speedup_over_threads": round(threads_s / proc_s, 2) if proc_s else None,
        "thread_concurrency_speedup_over_serial": round(60 / threads_s, 2),
    }
    return out


# ---------------------------------------------------------------------------
# Mechanics the design depends on: progress readability, cooperative
# cancellation, chunk overhead, and memory across many sequential tasks.
# ---------------------------------------------------------------------------


async def cmd_mechanics() -> dict:
    cal = _calibrate()
    rate = cal["hash_rounds_per_sec"]
    out: dict = {"calibration": cal}

    # Can the event loop read progress out of a running thread with no
    # cross-thread machinery at all? (a list slot is atomic under the GIL)
    progress = [0.0]
    samples: list[float] = []
    task = asyncio.create_task(
        asyncio.to_thread(run_hash_rounds, int(rate * 5), "sha256", progress, threading.Event())
    )
    while not task.done():
        await asyncio.sleep(0.25)
        samples.append(round(progress[0], 3))
    await task
    out["progress_visibility"] = {
        "samples": len(samples),
        "distinct_values": len(set(samples)),
        "monotonic": all(b >= a for a, b in zip(samples, samples[1:])),
        "first": samples[0] if samples else None,
        "last": samples[-1] if samples else None,
        "needed_call_soon_threadsafe": False,
    }

    # A thread cannot be killed. Does the cooperative flag stop it, how fast?
    cancel = threading.Event()
    prog = [0.0]
    t0 = time.perf_counter()
    task = asyncio.create_task(
        asyncio.to_thread(run_hash_rounds, int(rate * 20), "sha256", prog, cancel)
    )
    await asyncio.sleep(1.0)
    cancel.set()
    result = await task
    out["cooperative_cancellation"] = {
        "cancel_signalled_at_s": 1.0,
        "thread_exited_at_s": round(time.perf_counter() - t0, 3),
        "stop_latency_s": round(time.perf_counter() - t0 - 1.0, 3),
        "returned_none": result is None,
        "progress_at_cancel": round(prog[0], 3),
    }

    # Does chunking for progress + cancellation cost anything measurable?
    rounds = int(rate * 3)
    by_chunk = {}
    for chunk in (rounds, 500_000, 100_000, 10_000, 1_000):
        t0 = time.perf_counter()
        run_hash_rounds(rounds, chunk=min(chunk, rounds))
        by_chunk[str(chunk)] = round(time.perf_counter() - t0, 3)
    base = by_chunk[str(rounds)]
    out["chunk_overhead"] = {
        "seconds_by_chunk": by_chunk,
        "delta_vs_unchunked_pct": {
            k: round((v - base) / base * 100, 1) for k, v in by_chunk.items()
        },
        "note": "spread is run-to-run noise, not a speedup from chunking",
    }

    # Memory across many sequential tasks (exit criterion 7's mechanism).
    iterations = 300
    small = max(1, int(rate * 0.02))
    start = _rss_mb()
    marks = []
    for i in range(iterations):
        await asyncio.to_thread(run_hash_rounds, small)
        if i % 60 == 0:
            marks.append(round(_rss_mb(), 1))
    out["memory"] = {
        "iterations": iterations,
        "rss_start_mb": round(start, 1),
        "rss_end_mb": round(_rss_mb(), 1),
        "growth_mb": round(_rss_mb() - start, 1),
        "marks_mb": marks,
    }

    # The trap: a cgroup CPU quota is invisible to os.cpu_count(), so the
    # default executor is sized off the host's cores, not the container's.
    out["executor_sizing_trap"] = {
        "os_cpu_count": os.cpu_count(),
        "default_to_thread_max_workers": min(32, (os.cpu_count() or 1) + 4),
        "worker_max_concurrent_ceiling": 64,
        "default_pool_smaller_than_ceiling": min(32, (os.cpu_count() or 1) + 4) < 64,
        "note": (
            "os.cpu_count() ignores the cgroup quota, and the default "
            "to_thread pool is smaller than WORKER_MAX_CONCURRENT_CEILING. "
            "A worker sizing anything off either would silently queue "
            "accepted tasks inside the executor."
        ),
    }
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["isolation", "concurrency", "ceilings", "mechanics", "all"]
    )
    args = parser.parse_args()

    env = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "os_cpu_count": os.cpu_count(),
        "switch_interval_s": sys.getswitchinterval(),
        "thresholds": {
            "suspect_s": SUSPECT_S,
            "offline_s": OFFLINE_S,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        },
    }
    report: dict = {"environment": env}

    if args.command in ("ceilings", "all"):
        print("measuring declared-ceiling durations...", file=sys.stderr, flush=True)
        report["ceilings"] = cmd_ceilings()
    if args.command in ("isolation", "all"):
        print("measuring inline vs to_thread...", file=sys.stderr, flush=True)
        report["isolation"] = await cmd_isolation()
    if args.command in ("concurrency", "all"):
        print("measuring concurrency and process pool...", file=sys.stderr, flush=True)
        report["concurrency"] = await cmd_concurrency()
    if args.command in ("mechanics", "all"):
        print("measuring mechanics...", file=sys.stderr, flush=True)
        report["mechanics"] = await cmd_mechanics()

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    # The guard is load-bearing, not decoration: ProcessPoolExecutor
    # re-imports this module in every child on spawn platforms (Windows,
    # macOS), and without it that re-import would re-run main().
    asyncio.run(main())
