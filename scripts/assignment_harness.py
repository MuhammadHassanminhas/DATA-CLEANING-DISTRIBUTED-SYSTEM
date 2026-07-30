"""Assignment engine harness (Phase 2.3), versioned per CLAUDE.md §4.

Drives real worker sessions against a real coordinator over TLS. Each
simulated worker is a genuine registered identity with its own WebSocket
session, its own access token and its own declared capabilities — the
coordinator cannot tell one of these from a container or a laptop, which
is invariant §3.5 and is exactly why this is a fair way to produce a
fleet. What it is *not* is 100 separate machines; it is 100 sessions from
one process, and every measurement below should be read that way (§10).

Unlike `queue_harness.py` this needs the `websockets` package — a
WebSocket client is not in the standard library. Run it from a venv with
the worker's requirements, or in-cluster from the worker image, which
already has it.

Modes
-----
`fanout`    N workers that acknowledge, T tasks. Reports how the work
            split, and whether any task id was delivered twice.
`idle`      N workers connected with an empty queue, held for S seconds.
            Samples the coordinator's own /metrics before and after, so
            "negligible load" is a number rather than an adjective.
`stranded`  One worker that takes an assignment and deliberately never
            acknowledges it, then drops the socket — the deterministic
            way to produce the disconnect-before-ack case, which is
            otherwise a millisecond-wide race.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

import websockets

PROTOCOL_VERSION = "1.0"
ALL_TYPES = ["count_to_n", "hash_rounds", "sleep", "opaque_payload"]


def _ctx(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(url: str, payload: dict | None, insecure: bool, headers: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, context=_ctx(insecure), timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class HarnessWorker:
    """One simulated worker: register, token, connect, behave."""

    def __init__(self, base: str, enrollment_secret: str, insecure: bool, *,
                 max_concurrent: int, types: list[str], ack: bool = True) -> None:
        self.base = base.rstrip("/")
        self.enrollment_secret = enrollment_secret
        self.insecure = insecure
        self.max_concurrent = max_concurrent
        self.types = types
        self.ack = ack
        self.worker_id: str | None = None
        self.credential: str | None = None
        self.received: list[str] = []      # task ids delivered to this worker
        self.acked: list[str] = []
        self.ready = asyncio.Event()

    def _register(self) -> None:
        status, body = _request(
            f"{self.base}/workers/register",
            {"enrollment_secret": self.enrollment_secret, "agent_version": "assignment-harness"},
            self.insecure,
        )
        if status != 201:
            raise RuntimeError(f"register failed: {status} {body}")
        self.worker_id = body["worker_id"]
        self.credential = body["worker_credential"]

    def _token(self) -> str:
        status, body = _request(
            f"{self.base}/workers/token/refresh",
            {"worker_id": self.worker_id, "worker_credential": self.credential},
            self.insecure,
        )
        if status != 200:
            raise RuntimeError(f"token refresh failed: {status} {body}")
        return body["access_token"]

    async def run(self, stop: asyncio.Event, *, strand_after: int = 0) -> None:
        await asyncio.to_thread(self._register)
        token = await asyncio.to_thread(self._token)
        ws_url = self.base.replace("https://", "wss://", 1) + "/ws/connect"

        async with websockets.connect(
            ws_url, extra_headers={"Authorization": f"Bearer {token}"}, ssl=_ctx(self.insecure)
        ) as ws:
            hello = {
                "message_type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "worker_id": self.worker_id,
                "correlation_id": str(uuid.uuid4()),
                "session_epoch": 0,
                "timestamp": "",
                "payload": {
                    "agent_version": "assignment-harness",
                    "max_concurrent": self.max_concurrent,
                    "supported_task_types": self.types,
                },
            }
            await ws.send(json.dumps(hello))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if ack.get("message_type") != "hello_ack":
                raise RuntimeError(f"handshake failed: {ack}")
            self.ready.set()

            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                message = json.loads(raw)
                kind = message.get("message_type")

                if kind == "ping":
                    await ws.send(json.dumps({
                        "message_type": "pong", "protocol_version": PROTOCOL_VERSION,
                        "worker_id": self.worker_id, "correlation_id": message.get("correlation_id"),
                        "session_epoch": 0, "timestamp": "", "payload": {},
                    }))
                    continue

                if kind == "task_assign":
                    task_id = message["payload"]["task_id"]
                    self.received.append(task_id)
                    if strand_after and len(self.received) >= strand_after:
                        # Take the task and vanish without acknowledging.
                        return
                    if self.ack:
                        await ws.send(json.dumps({
                            "message_type": "task_ack", "protocol_version": PROTOCOL_VERSION,
                            "worker_id": self.worker_id,
                            "correlation_id": message.get("correlation_id"),
                            "session_epoch": 0, "timestamp": "",
                            "payload": {"task_id": task_id, "accepted": True},
                        }))
                        self.acked.append(task_id)


async def _connect_fleet(args, count: int, *, ack: bool = True, types=None):
    stop = asyncio.Event()
    workers = [
        HarnessWorker(args.url, args.enrollment_secret, args.insecure,
                      max_concurrent=args.max_concurrent, types=types or ALL_TYPES, ack=ack)
        for _ in range(count)
    ]
    tasks = [asyncio.create_task(w.run(stop)) for w in workers]
    await asyncio.gather(*(w.ready.wait() for w in workers))
    return workers, tasks, stop


async def _shutdown(tasks, stop):
    stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _depth(args) -> dict:
    status, body = _request(
        f"{args.url.rstrip('/')}/tasks/depth", None, args.insecure,
        headers={"x-admin-secret": args.admin_secret},
    )
    if status != 200:
        raise RuntimeError(f"depth failed: {status} {body}")
    return body


def _metrics(args) -> dict[str, float]:
    req = urllib.request.Request(f"{args.url.rstrip('/')}/metrics")
    with urllib.request.urlopen(req, context=_ctx(args.insecure), timeout=30) as resp:
        text = resp.read().decode()
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


async def mode_fanout(args) -> int:
    workers, tasks, stop = await _connect_fleet(args, args.workers)
    print(f"connected {len(workers)} workers, {args.max_concurrent} credits each "
          f"({len(workers) * args.max_concurrent} total)")

    status, body = _request(
        f"{args.url.rstrip('/')}/tasks",
        {"admin_secret": args.admin_secret, "task_type": "count_to_n",
         "parameters": {"n": 1}, "count": args.tasks},
        args.insecure,
    )
    if status != 201:
        await _shutdown(tasks, stop)
        raise RuntimeError(f"enqueue failed: {status} {body}")
    print(f"enqueued {args.tasks} tasks")

    started = time.monotonic()
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        delivered = sum(len(w.received) for w in workers)
        if delivered >= args.tasks:
            break
        await asyncio.sleep(0.25)
    elapsed = time.monotonic() - started
    await asyncio.sleep(1.0)  # let the last acks land
    await _shutdown(tasks, stop)

    all_ids = [task_id for w in workers for task_id in w.received]
    duplicates = [t for t, n in Counter(all_ids).items() if n > 1]
    per_worker = sorted((len(w.received) for w in workers), reverse=True)

    print(json.dumps({
        "tasks_requested": args.tasks,
        "delivered": len(all_ids),
        "unique": len(set(all_ids)),
        "duplicates": len(duplicates),
        "acknowledged": sum(len(w.acked) for w in workers),
        "workers_that_got_work": sum(1 for w in workers if w.received),
        "per_worker_min": per_worker[-1] if per_worker else 0,
        "per_worker_max": per_worker[0] if per_worker else 0,
        "elapsed_seconds": round(elapsed, 2),
        "final_depth": _depth(args),
    }, indent=2))

    ok = (len(all_ids) == args.tasks and len(set(all_ids)) == args.tasks and not duplicates)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


async def mode_idle(args) -> int:
    workers, tasks, stop = await _connect_fleet(args, args.workers)
    print(f"connected {len(workers)} idle workers; queue depth {_depth(args)['depth']}")

    before = _metrics(args)
    await asyncio.sleep(args.seconds)
    after = _metrics(args)
    await _shutdown(tasks, stop)

    def delta(name: str) -> float:
        return round(after.get(name, 0.0) - before.get(name, 0.0), 4)

    print(json.dumps({
        "idle_workers": len(workers),
        "window_seconds": args.seconds,
        "assignment_passes": delta("coordinator_assignment_passes_total"),
        "dequeue_queries": delta("coordinator_assignment_dequeue_queries_total"),
        "tasks_assigned": delta("coordinator_tasks_assigned_total"),
        "coordinator_cpu_seconds": delta("process_cpu_seconds_total"),
        "coordinator_cpu_percent_of_one_core": round(
            100 * delta("process_cpu_seconds_total") / args.seconds, 2),
        "resident_memory_bytes": after.get("process_resident_memory_bytes"),
    }, indent=2))
    return 0


async def mode_stranded(args) -> int:
    """One worker takes a task and disconnects without acknowledging."""
    worker = HarnessWorker(args.url, args.enrollment_secret, args.insecure,
                           max_concurrent=1, types=ALL_TYPES, ack=False)
    stop = asyncio.Event()
    runner = asyncio.create_task(worker.run(stop, strand_after=1))
    await worker.ready.wait()
    print(f"stranded worker connected: {worker.worker_id}")

    status, body = _request(
        f"{args.url.rstrip('/')}/tasks",
        {"admin_secret": args.admin_secret, "task_type": "count_to_n",
         "parameters": {"n": 1}, "count": 1},
        args.insecure,
    )
    if status != 201:
        raise RuntimeError(f"enqueue failed: {status} {body}")

    try:
        await asyncio.wait_for(runner, timeout=args.timeout)
    except asyncio.TimeoutError:
        stop.set()
        runner.cancel()
        print("FAIL: no task was delivered before the timeout")
        return 1

    if not worker.received:
        print("FAIL: worker disconnected without receiving a task")
        return 1

    print(json.dumps({
        "worker_id": worker.worker_id,
        "task_id_taken_and_never_acked": worker.received[0],
        "acked": worker.acked,
    }, indent=2))
    print("PASS — now check the coordinator log for task_unacknowledged_at_disconnect")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["fanout", "idle", "stranded"])
    parser.add_argument("--url", required=True, help="coordinator base URL")
    parser.add_argument("--enrollment-secret", required=True)
    parser.add_argument("--admin-secret", required=True)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--max-concurrent", type=int, default=10)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (local dev CA only)")
    args = parser.parse_args()

    runner = {"fanout": mode_fanout, "idle": mode_idle, "stranded": mode_stranded}[args.mode]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    sys.exit(main())
