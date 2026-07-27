"""Worker agent.

Phase 1.3: registers with the coordinator using a bootstrap enrollment
credential, persists the coordinator-assigned identity to a local file
(on a volume, so it survives container recreation), and reuses it on
every later startup instead of re-enrolling. Heartbeat protocol proper
is Phase 1.6 — this still only touches the local heartbeat file so
Docker's HEALTHCHECK keeps working.

Duplicate-use handling:
- Same machine, launched twice: an exclusive OS file lock on a lock
  file next to the identity file makes the second process fail fast
  rather than silently share the identity.
- Identity copied to a second machine: every run registers a short-TTL
  "claim" for its worker ID on the coordinator (see
  `coordinator/app/main.py` and `config.worker_claim_ttl_seconds`'s
  docstring for exactly what this does and does not guarantee). A
  concurrent claim attempt while the previous one is still live is
  rejected (409) and logged as a duplicate. On graceful shutdown
  (SIGTERM) the claim is released so a normal restart never trips this.

Phase 1.4: after identity is established, exchanges the long-lived
`worker_credential` for a short-lived access token via
`/workers/token/refresh`.

Phase 1.5: the access token authenticates a persistent WebSocket
connection to `/ws/connect` (Decisions Log #1). The token is exchanged
once per connection attempt, at handshake time — the coordinator never
re-checks it for the life of the socket, so there is nothing to keep
"live" while connected. On disconnect (coordinator drain, network drop,
coordinator restart) the worker reconnects using the Phase 1.7 backoff
policy below.

Phase 1.6: alongside the transport-level ping/pong keepalive, the
worker now sends an application-level `heartbeat` envelope over the
same socket every `WORKER_HEARTBEAT_INTERVAL_SECONDS`, carrying a
sequence number, uptime, agent version, and CPU/memory read via
`psutil` (cross-platform — Linux/Windows/macOS, so native non-Docker
workers report real figures instead of blank; Step 1.5.8). These read
host-level figures, not a container cgroup limit, which is deliberate:
the Step 1.6 exit criterion asks for accuracy "against the host". The
coordinator times
SUSPECT/OFFLINE off its own receipt clock, not this envelope's
`timestamp` field, so a skewed worker clock cannot affect the state
machine (Decisions Log #6).

Phase 1.7: the fixed 3-second reconnect delay is replaced with full-
jitter exponential backoff (`_backoff_delay_seconds`) — also the
thundering-herd mitigation for a mass reconnect after a coordinator
restart, since the jitter alone spreads simultaneous workers across a
window instead of all retrying at the same instant. The backoff resets
to its fastest tier the moment a session is genuinely established
(`hello_ack` received), not merely attempted, so one clean disconnect
never leaves the next retry slower than it needs to be. The coordinator
can also unilaterally end a session via a `session_evicted` message
(Step 1.7's session-conflict resolution: if this worker ID opens a
second connection — e.g. a fast forced-restart racing the old
session's own cleanup — the coordinator always keeps exactly one
winner and terminates the other).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import psutil

try:  # POSIX (Docker/Linux/macOS worker)
    import fcntl

    msvcrt = None
except ImportError:  # native Windows worker (no Docker) — Step 1.5.8 installer
    fcntl = None
    import msvcrt

import websockets
from websockets.exceptions import ConnectionClosed

from protocol.envelope import Envelope

HEARTBEAT_FILE = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat"))
IDENTITY_FILE = Path(
    os.environ.get("WORKER_IDENTITY_FILE", "/var/lib/worker-identity/identity.json")
)
LOCK_FILE = IDENTITY_FILE.with_suffix(".lock")
INTERVAL_SECONDS = int(os.environ.get("WORKER_LOOP_INTERVAL_SECONDS", "5"))
COORDINATOR_URL = os.environ["COORDINATOR_URL"]
# Private dev CA for local Docker (self-signed coordinator cert). Set
# WORKER_CA_FILE="" for a worker reaching the PUBLIC ingress (Step 1.5.5):
# the coordinator there presents a Let's Encrypt cert chaining to a public
# root, so the OS trust store validates it — no dev CA needed.
CA_FILE = os.environ.get("WORKER_CA_FILE", "/certs/dev-ca.crt")
AGENT_VERSION = os.environ.get("WORKER_AGENT_VERSION", "0.1.0")


def _ssl_context() -> ssl.SSLContext:
    # Trust the private dev CA only if the file is actually present (local
    # Docker mounts it at /certs). On every machine without it — non-Docker
    # workers (Step 1.5.8), and anything reaching the public Let's Encrypt
    # endpoint — fall back to the OS trust store. "Missing file" means "use
    # system roots", never "fail": a self-signed coordinator cert won't
    # validate against system roots, so this cannot silently downgrade trust.
    # (Also dodges Windows PowerShell not propagating an empty env var to the
    # child, which would otherwise leave CA_FILE at its dev-CA default.)
    if CA_FILE and os.path.exists(CA_FILE):
        return ssl.create_default_context(cafile=CA_FILE)
    return ssl.create_default_context()
WS_HELLO_ACK_TIMEOUT_SECONDS = 10
# Full-jitter exponential backoff. Recommendations, not measured values.
WS_BACKOFF_BASE_SECONDS = float(os.environ.get("WORKER_WS_BACKOFF_BASE_SECONDS", "1"))
WS_BACKOFF_FACTOR = float(os.environ.get("WORKER_WS_BACKOFF_FACTOR", "2"))
WS_BACKOFF_MAX_SECONDS = float(os.environ.get("WORKER_WS_BACKOFF_MAX_SECONDS", "30"))
# Recommendation, not a measured value. Must stay comfortably below the
# coordinator's HEARTBEAT_SUSPECT_THRESHOLD_SECONDS (default 12s).
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger("worker")

_lock_handle = None  # kept open for the process lifetime; see _acquire_single_instance_lock
_process_start = time.monotonic()


def _read_cpu_percent() -> float | None:
    """Host CPU usage since the previous call, via `psutil` (Linux/
    Windows/macOS). Non-blocking (`interval=None`): the first call
    returns 0.0, later calls the delta. `None` only if psutil errors."""
    try:
        return round(psutil.cpu_percent(interval=None), 1)
    except Exception:
        return None


def _read_memory_percent() -> float | None:
    """Host memory usage via `psutil` — real host figures, not a
    container cgroup limit, which is what the Step 1.6 exit criterion
    ("accurate against the host") asks for."""
    try:
        return round(psutil.virtual_memory().percent, 1)
    except Exception:
        return None


def _log(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, "service": "worker", **fields}, default=str))


def _acquire_single_instance_lock() -> None:
    global _lock_handle
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    # "a" not "w": never truncate a file a first instance may have locked.
    handle = open(LOCK_FILE, "a")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # Windows: same "second instance fails fast" guarantee via msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        _log("duplicate_local_instance_detected", lock_file=str(LOCK_FILE))
        sys.exit(1)
    _lock_handle = handle  # holding the reference keeps the lock held


def _post(path: str, payload: dict, correlation_id: str | None = None) -> tuple[int, dict]:
    ctx = _ssl_context()
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    # Carries the session correlation id onto the HTTP leg (token refresh)
    # so it shares one id with the WebSocket leg — the coordinator's
    # CorrelationIDMiddleware reuses X-Correlation-ID when present (§11).
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    req = urllib.request.Request(
        f"{COORDINATOR_URL}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _load_identity() -> dict | None:
    if not IDENTITY_FILE.exists():
        return None
    return json.loads(IDENTITY_FILE.read_text())


def _save_identity(identity: dict) -> None:
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_FILE.write_text(json.dumps(identity))
    os.chmod(IDENTITY_FILE, 0o600)


def _register_or_reaffirm() -> dict:
    identity = _load_identity()

    if identity is None:
        status, body = _post(
            "/workers/register",
            {"enrollment_secret": os.environ["ENROLLMENT_SECRET"], "agent_version": AGENT_VERSION},
        )
        if status != 201:
            _log("registration_rejected", status=status, detail=body.get("detail"))
            sys.exit(1)
        identity = {"worker_id": body["worker_id"], "worker_credential": body["worker_credential"]}
        _save_identity(identity)
        _log("registered", worker_id=identity["worker_id"])
        return identity

    status, body = _post(
        "/workers/register",
        {
            "worker_id": identity["worker_id"],
            "worker_credential": identity["worker_credential"],
            "agent_version": AGENT_VERSION,
        },
    )
    if status == 409:
        _log("duplicate_identity_detected", worker_id=identity["worker_id"], detail=body.get("detail"))
        sys.exit(1)
    if status == 429:
        _log("registration_rate_limited", worker_id=identity.get("worker_id"))
        sys.exit(1)
    if status != 200:
        _log("reaffirm_rejected", status=status, detail=body.get("detail"))
        sys.exit(1)
    _log("reaffirmed", worker_id=identity["worker_id"])
    return identity


def _release_claim(worker_id: str, worker_credential: str) -> None:
    try:
        _post("/workers/release", {"worker_id": worker_id, "worker_credential": worker_credential})
    except Exception:  # noqa: BLE001 — best-effort on shutdown, never block exit
        pass


def _refresh_access_token(
    worker_id: str, worker_credential: str, correlation_id: str | None = None
) -> tuple[str, int] | None:
    status, body = _post(
        "/workers/token/refresh",
        {"worker_id": worker_id, "worker_credential": worker_credential},
        correlation_id=correlation_id,
    )
    if status != 200:
        _log("access_token_refresh_rejected", worker_id=worker_id, status=status, detail=body.get("detail"))
        return None
    _log(
        "access_token_refreshed",
        worker_id=worker_id,
        expires_in=body["expires_in"],
        correlation_id=correlation_id,
    )
    return body["access_token"], body["expires_in"]


def _ws_url() -> str:
    return COORDINATOR_URL.replace("https://", "wss://", 1) + "/ws/connect"


async def _heartbeat_ws_loop(
    ws, identity: dict, send_lock: asyncio.Lock, correlation_id: str
) -> None:
    """Sends an application-level `heartbeat` envelope every
    `HEARTBEAT_INTERVAL_SECONDS`, separate from the transport-level
    ping/pong keepalive already handled inline in `_hold_connection`.
    Cancelled from that function's `finally` block when the connection
    ends, so this never outlives the socket it's sending on."""
    sequence = 0
    while True:
        sequence += 1
        envelope = Envelope(
            message_type="heartbeat",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={
                "sequence": sequence,
                "uptime_seconds": round(time.monotonic() - _process_start, 1),
                "agent_version": AGENT_VERSION,
                "status": "ONLINE",
                "cpu_percent": _read_cpu_percent(),
                "memory_percent": _read_memory_percent(),
            },
        )
        async with send_lock:
            await ws.send(json.dumps(envelope.to_dict()))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _hold_connection(
    identity: dict, access_token: str, established: dict, correlation_id: str
) -> None:
    """Open one WebSocket session and block until it closes, handling
    the ping/pong keepalive, the Phase 1.6 heartbeat loop, and any
    coordinator-pushed messages inline. Returns normally on a clean
    coordinator-initiated `shutdown` or `session_evicted`; raises on an
    unexpected drop so the caller knows to reconnect. Sets
    `established["value"] = True` the moment a real session exists
    (`hello_ack`) — the caller uses this, not how the function exits, to
    decide whether the Phase 1.7 backoff should reset."""
    ctx = _ssl_context()
    headers = {"Authorization": f"Bearer {access_token}"}
    async with websockets.connect(_ws_url(), extra_headers=headers, ssl=ctx) as ws:
        send_lock = asyncio.Lock()
        hello = Envelope(
            message_type="hello",
            worker_id=identity["worker_id"],
            correlation_id=correlation_id,
            payload={"agent_version": AGENT_VERSION},
        )
        async with send_lock:
            await ws.send(json.dumps(hello.to_dict()))

        ack_raw = await asyncio.wait_for(ws.recv(), timeout=WS_HELLO_ACK_TIMEOUT_SECONDS)
        ack = json.loads(ack_raw)
        if ack.get("message_type") == "error":
            _log("ws_connection_rejected", reason=ack.get("payload", {}).get("reason"))
            return
        if ack.get("message_type") != "hello_ack":
            _log("ws_handshake_unexpected", received=ack.get("message_type"))
            return
        established["value"] = True
        _log(
            "ws_connected",
            worker_id=identity["worker_id"],
            session_epoch=ack.get("session_epoch"),
            correlation_id=correlation_id,
        )

        heartbeat_task = asyncio.create_task(
            _heartbeat_ws_loop(ws, identity, send_lock, correlation_id)
        )
        try:
            async for raw in ws:
                message = json.loads(raw)
                message_type = message.get("message_type")
                if message_type == "ping":
                    pong = Envelope(
                        message_type="pong",
                        worker_id=identity["worker_id"],
                        correlation_id=correlation_id,
                    )
                    async with send_lock:
                        await ws.send(json.dumps(pong.to_dict()))
                    continue
                if message_type == "shutdown":
                    _log("ws_shutdown_received", worker_id=identity["worker_id"])
                    return
                if message_type == "session_evicted":
                    _log(
                        "ws_session_evicted",
                        worker_id=identity["worker_id"],
                        detail=message.get("payload", {}).get("reason"),
                    )
                    return
                _log("ws_message_received", worker_id=identity["worker_id"], message_type=message_type)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


def _backoff_delay_seconds(consecutive_failures: int) -> float:
    """Full-jitter exponential backoff (AWS's "full jitter" algorithm):
    the delay is a random draw from `[0, cap)`, not the cap itself — this
    is also the thundering-herd mitigation for a mass reconnect after a
    coordinator restart, since a fleet of workers all failing at once get
    spread across that window instead of all retrying in lockstep.
    `consecutive_failures=0` (the tier right after a successful session)
    still jitters within `[0, BASE)` rather than reconnecting instantly,
    so even the "just disconnected cleanly" case doesn't thunder."""
    cap = min(WS_BACKOFF_MAX_SECONDS, WS_BACKOFF_BASE_SECONDS * (WS_BACKOFF_FACTOR**consecutive_failures))
    return random.uniform(0, cap)


async def _run_ws_forever(identity: dict, stop_event: asyncio.Event) -> None:
    """Runs until `stop_event` is set. Everything inside one iteration —
    token refresh included — is inside the same try/except: a token
    refresh can fail exactly the way a live connection can (the
    coordinator is mid-restart, DNS hiccups, a network blip), and must
    retry the same way rather than killing this whole background task
    with an unhandled exception (asyncio silently drops an unawaited
    task's exception — that failure mode cost real debugging time here:
    the worker container stayed "healthy" via its unrelated heartbeat
    loop while the connection loop was silently dead).

    `consecutive_failures` drives the Phase 1.7 backoff and resets to 0
    the moment a session is genuinely established, regardless of *how*
    this iteration ends (return or raise) — a clean coordinator-
    initiated close after a real session is not a failure."""
    consecutive_failures = 0
    while not stop_event.is_set():
        established = {"value": False}
        # One correlation id per session (= one connection lifetime). Shared
        # by the token-refresh HTTP call and the WS hello/heartbeat/pong, so
        # a session is traceable end to end by a single id — and across
        # coordinator replicas, since the HTTP leg and the WS leg can land on
        # different replicas behind the Service (§11, Step 1.5.6 C2).
        session_correlation_id = str(uuid.uuid4())
        try:
            refreshed = await asyncio.to_thread(
                _refresh_access_token,
                identity["worker_id"],
                identity["worker_credential"],
                session_correlation_id,
            )
            if refreshed is None:
                raise RuntimeError("token refresh rejected")
            access_token, _ = refreshed
            await _hold_connection(identity, access_token, established, session_correlation_id)
        except (ConnectionClosed, OSError, urllib.error.URLError) as exc:
            _log("ws_connection_lost", worker_id=identity["worker_id"], detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — keep the retry loop alive
            _log("ws_connection_error", worker_id=identity["worker_id"], detail=str(exc))

        consecutive_failures = 0 if established["value"] else consecutive_failures + 1

        if not stop_event.is_set():
            delay = _backoff_delay_seconds(consecutive_failures)
            _log(
                "ws_reconnect_backoff",
                worker_id=identity["worker_id"],
                consecutive_failures=consecutive_failures,
                delay_seconds=round(delay, 2),
            )
            await asyncio.sleep(delay)


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        HEARTBEAT_FILE.touch()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _async_main(identity: dict) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_sigterm() -> None:
        _log("shutting_down", worker_id=identity["worker_id"])
        _release_claim(identity["worker_id"], identity["worker_credential"])
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except NotImplementedError:
        # Windows' Proactor loop has no add_signal_handler. The worker still
        # runs; graceful claim-release on shutdown is skipped, and the
        # short-TTL claim (Decision #11) expires on its own instead.
        _log("signal_handler_unavailable", worker_id=identity["worker_id"])

    heartbeat_task = asyncio.create_task(_heartbeat_loop(stop_event))
    ws_task = asyncio.create_task(_run_ws_forever(identity, stop_event))

    await stop_event.wait()
    heartbeat_task.cancel()
    ws_task.cancel()
    for task in (heartbeat_task, ws_task):
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    _acquire_single_instance_lock()
    identity = _register_or_reaffirm()
    asyncio.run(_async_main(identity))


if __name__ == "__main__":
    main()
