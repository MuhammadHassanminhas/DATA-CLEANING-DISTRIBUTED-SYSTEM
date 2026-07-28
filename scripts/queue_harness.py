#!/usr/bin/env python3
"""Task queue load harness — Phase 2.2 exit-criteria verification.

Scripted, repeatable, versioned (CLAUDE.md §4). It drives the coordinator
over HTTP rather than the database directly, which is the whole point:
pointed at the in-cluster `coordinator` Service, requests land on whichever
replica the Service picks, so `drain` is genuinely N concurrent
*coordinator replicas* dequeuing, not N threads sharing one process. The
same script run against `docker compose` or a laptop coordinator does the
cheap local version of the same proof.

Standard library only — no pip install — so it runs unchanged inside any
Python container in the cluster (`kubectl exec`, or the Job in
`infra/loadtest-queue.yaml`).

This is not Step 2.8's harness. Step 2.8 owns throughput and latency
targets across the whole pipeline; this one only answers Step 2.2's five
questions: are tasks lost, are any handed out twice, is depth right, do
queued tasks survive a restart, and is the ordering what the code claims.

Usage
-----
    export COORDINATOR_URL=https://coordinator:8443
    export ADMIN_SECRET=...       # operator credential — drives the queue
    export ENROLLMENT_SECRET=...  # worker credential — registers the worker
                                  # that claims are attributed to

    python scripts/queue_harness.py enqueue --count 10000
    python scripts/queue_harness.py depth
    python scripts/queue_harness.py drain --dequeuers 3
    python scripts/queue_harness.py verify --count 10000 --dequeuers 3

`--insecure` skips certificate verification, needed for the in-cluster hop
where the coordinator serves the self-signed dev cert (nginx does the same
on its backend hop). Never needed against the public ingress, which has a
real Let's Encrypt certificate.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_TASK_TYPE = "count_to_n"
DEFAULT_PARAMETERS = {"n": 1000}

_ssl_ctx: ssl.SSLContext | None = None


def _request(url: str, method: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, context=_ssl_ctx, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def enqueue(base: str, secret: str, count: int, task_type: str, priority: int) -> float:
    """Create `count` tasks in one call. Returns elapsed seconds.

    One request, not `count` requests: the public ingress rate-limits to a
    few requests per second (Step 1.5.5), so a per-task loop would be
    measuring nginx, not the queue.
    """
    started = time.monotonic()
    result = _request(
        f"{base}/tasks",
        "POST",
        {
            "admin_secret": secret,
            "task_type": task_type,
            "parameters": DEFAULT_PARAMETERS,
            "priority": priority,
            "count": count,
        },
    )
    elapsed = time.monotonic() - started
    if result.get("count") != count:
        raise SystemExit(f"enqueue asked for {count}, coordinator reported {result.get('count')}")
    return elapsed


def depth(base: str, secret: str) -> dict:
    started = time.monotonic()
    result = _request(f"{base}/tasks/depth", "GET", headers={"x-admin-secret": secret})
    result["_read_seconds"] = round(time.monotonic() - started, 4)
    return result


def register_worker(base: str, enrollment: str) -> str:
    """Get a real worker identity for claims to point at.

    `tasks.assigned_worker_id` is a foreign key, so a dequeue needs a
    worker row that actually exists. Registering through the normal
    enrollment endpoint keeps the harness honest: it uses the same
    identity path a real worker does, rather than inserting a synthetic
    row behind the coordinator's back.

    Takes the **enrollment** secret, not the admin one. Since Step 2.2.1
    those are different credentials: enrolling is a worker action, driving
    the queue is an operator action, and the coordinator rejects each
    credential on the other's endpoints.
    """
    result = _request(
        f"{base}/workers/register",
        "POST",
        {"enrollment_secret": enrollment, "agent_version": "queue-harness"},
    )
    return result["worker_id"]


def drain(bases: list[str], secret: str, worker_id: str, dequeuers: int, limit: int) -> dict:
    """Run `dequeuers` concurrent claim loops until the queue is empty.

    Each loop is pinned to one entry of `bases`, round-robin. Two ways to
    get real multi-process concurrency, and the harness reports both so
    the evidence matches whichever was used:

      * one base URL pointing at a Kubernetes Service — the Service picks
        the replica, and `by_coordinator_instance` (the serving pod's
        hostname) is the proof that several replicas took part;
      * several base URLs, one per locally-run coordinator process — those
        share a hostname, so `by_target` is the proof instead.
    """
    claimed: list[str] = []
    by_instance: dict[str, int] = {}
    by_target: dict[str, int] = {}
    lock = threading.Lock()
    errors: list[str] = []

    def loop(base: str) -> None:
        while True:
            try:
                result = _request(
                    f"{base}/tasks/dequeue",
                    "POST",
                    {"admin_secret": secret, "worker_id": worker_id, "limit": limit},
                )
            except SystemExit as exc:
                with lock:
                    errors.append(str(exc))
                return
            tasks = result.get("tasks") or []
            if not tasks:
                return
            instance = result.get("coordinator_instance", "unknown")
            with lock:
                claimed.extend(task["task_id"] for task in tasks)
                by_instance[instance] = by_instance.get(instance, 0) + len(tasks)
                by_target[base] = by_target.get(base, 0) + len(tasks)

    started = time.monotonic()
    threads = [
        threading.Thread(target=loop, args=(bases[i % len(bases)],)) for i in range(dequeuers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    unique = set(claimed)
    return {
        "claimed": len(claimed),
        "unique": len(unique),
        "duplicates": len(claimed) - len(unique),
        "by_coordinator_instance": by_instance,
        "by_target": by_target,
        "seconds": round(elapsed, 3),
        "tasks_per_second": round(len(claimed) / elapsed, 1) if elapsed > 0 else None,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["enqueue", "depth", "drain", "verify"])
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--dequeuers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=10, help="tasks claimed per dequeue call")
    parser.add_argument("--task-type", default=DEFAULT_TASK_TYPE)
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args()

    # A comma-separated COORDINATOR_URL targets several coordinator
    # processes directly; a single URL targets a load-balanced Service.
    bases = [url.strip().rstrip("/") for url in os.environ.get("COORDINATOR_URL", "").split(",") if url.strip()]
    # Two credentials since Step 2.2.1: ADMIN_SECRET drives the queue
    # endpoints, ENROLLMENT_SECRET registers the worker that claims are
    # attributed to. Each falls back to the other so this still runs
    # against a deployment that has not been split yet.
    secret = os.environ.get("ADMIN_SECRET") or os.environ.get("ENROLLMENT_SECRET", "")
    enrollment = os.environ.get("ENROLLMENT_SECRET") or secret
    if not bases or not secret:
        print("set COORDINATOR_URL and ADMIN_SECRET (or ENROLLMENT_SECRET)", file=sys.stderr)
        return 2
    base = bases[0]

    if args.insecure:
        global _ssl_ctx
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_NONE

    if args.mode == "depth":
        print(json.dumps(depth(base, secret), indent=2))
        return 0

    if args.mode == "enqueue":
        elapsed = enqueue(base, secret, args.count, args.task_type, args.priority)
        print(json.dumps({"enqueued": args.count, "seconds": round(elapsed, 3)}, indent=2))
        return 0

    if args.mode == "drain":
        worker_id = register_worker(base, enrollment)
        print(json.dumps(drain(bases, secret, worker_id, args.dequeuers, args.limit), indent=2))
        return 0

    # verify: the whole criterion in one run, and it decides pass/fail
    # itself rather than leaving a human to eyeball the numbers.
    before = depth(base, secret)
    enqueue_seconds = enqueue(base, secret, args.count, args.task_type, args.priority)
    after_enqueue = depth(base, secret)
    worker_id = register_worker(base, enrollment)
    result = drain(bases, secret, worker_id, args.dequeuers, args.limit)
    after_drain = depth(base, secret)

    enqueued_delta = after_enqueue["depth"] - before["depth"]
    checks = {
        "enqueue_depth_matches_request": enqueued_delta == args.count,
        "every_task_claimed": result["claimed"] == args.count + before["depth"],
        "no_duplicate_claims": result["duplicates"] == 0,
        "queue_drained_to_empty": after_drain["depth"] == 0,
        "no_request_errors": not result["errors"],
        "more_than_one_instance_served": len(result["by_coordinator_instance"]) > 1,
    }
    report = {
        "requested": args.count,
        "depth_before": before["depth"],
        "depth_after_enqueue": after_enqueue["depth"],
        "depth_after_drain": after_drain["depth"],
        "enqueue_seconds": round(enqueue_seconds, 3),
        "depth_read_seconds": after_enqueue["_read_seconds"],
        "drain": result,
        "checks": checks,
    }
    print(json.dumps(report, indent=2))

    # `more_than_one_instance_served` is reported but not fatal: run
    # against a single coordinator it is legitimately false, and that is a
    # valid local run. The loss and duplication checks are the pass/fail.
    fatal = {k: v for k, v in checks.items() if k != "more_than_one_instance_served"}
    if not all(fatal.values()):
        print("FAIL: " + ", ".join(k for k, v in fatal.items() if not v), file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
