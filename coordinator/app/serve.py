"""Phase 3.5 — the coordinator's entrypoint, and the only reason it exists
is graceful shutdown.

Until this step the container ran `uvicorn app.main:app` directly, and a
SIGTERM went straight into uvicorn's own `handle_exit`: stop accepting,
close every WebSocket with 1012, run the lifespan's shutdown code, exit.
Correct, and abrupt. Two things are thrown away by that abruptness, and
neither is recovered for free:

1. **A task claimed microseconds before the signal.** `dequeue` moves a
   row to `ASSIGNED` durably before the frame is written, so a delivery
   that dies in the socket buffer leaves a task nobody is executing and
   nobody is watching. It comes back — the reclaimer sees the ack timeout
   — but a full `TASK_ACK_TIMEOUT_SECONDS` later, on a queue that another
   live replica could have served immediately.
2. **The replica's readiness.** `/ready` answers 200 right up to the
   moment the process dies, so a Service or ingress can route a
   reconnecting worker straight back into a coordinator that is closing.

Both are fixed by doing something before uvicorn's shutdown rather than
after it, and *after* is the only hook FastAPI's lifespan offers: by the
time the code past `yield` runs, uvicorn has already closed every
connection (see the comment at that `yield` in `main.py`). So the signal
has to be intercepted one level up, which is what this module is.

**What it is not.** It is not a second lifecycle, it holds no state, and
it makes no decision the coordinator does not already make — a drained
replica exits exactly the way an undrained one did, through uvicorn's own
`handle_exit`. The whole of the addition is *when* that call happens.

The same code path runs in Docker Compose and in Kubernetes (§3.5): there
is no Kubernetes-only preStop hook doing half of this, because a
coordinator that behaves differently in one environment is exactly what
CLAUDE.md §3.5 forbids. What the two environments each supply is a kill
deadline big enough for the window — `stop_grace_period` in Compose,
`terminationGracePeriodSeconds` in the chart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import FrameType

import uvicorn

from app.assignment import begin_drain, drain_local_sessions
from app.config import shutdown_drain_seconds
from app.logging_config import configure_logging
from app.main import app

logger = logging.getLogger("coordinator")


class DrainingServer(uvicorn.Server):
    """A uvicorn server that stops taking work before it stops serving.

    `handle_exit` runs in the event loop (uvicorn installs its signal
    handlers with `loop.add_signal_handler` on unix), so scheduling the
    drain as a task from here is safe and does no work in signal context.

    **A second signal exits immediately.** Not a convenience: an operator
    who has decided a coordinator must stop *now* is answering a question
    this module cannot see, and a shutdown that ignores the second Ctrl-C
    is a shutdown people learn to `kill -9` instead.
    """

    def __init__(self, config: uvicorn.Config, drain_seconds: float) -> None:
        super().__init__(config)
        self.drain_seconds = drain_seconds
        self._draining = False
        self._loop: asyncio.AbstractEventLoop | None = None

    async def serve(self, sockets=None) -> None:  # noqa: ANN001 — uvicorn's signature
        # Captured rather than looked up in the handler. uvicorn installs
        # its signal handlers with `loop.add_signal_handler` on unix, where
        # a lookup would work — but falls back to `signal.signal` where
        # that is unsupported, and there the handler can run with no
        # running loop at all. One reference, correct either way.
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._draining or self.drain_seconds <= 0 or self._loop is None:
            if self._draining:
                logger.warning("shutdown_drain_interrupted", extra={"signal": int(sig)})
            super().handle_exit(sig, frame)
            return

        self._draining = True
        # Synchronous, and it must be: the flag has to be true before this
        # returns, or the assignment pass already running gets one more
        # claim in and `/ready` answers 200 to a probe in flight.
        begin_drain()
        logger.info(
            "shutdown_drain_started",
            extra={"signal": int(sig), "drain_seconds": self.drain_seconds},
        )
        loop = self._loop
        loop.call_soon_threadsafe(
            lambda: loop.create_task(self._drain_then_exit(sig, frame))
        )

    async def _drain_then_exit(self, sig: int, frame: FrameType | None) -> None:
        try:
            observed = await drain_local_sessions(self.drain_seconds)
            logger.info("shutdown_drain_complete", extra=observed)
        except Exception as exc:  # noqa: BLE001 — a failed drain must still exit
            logger.warning("shutdown_drain_failed", extra={"detail": str(exc)})
        finally:
            # Not `self.handle_exit`: that would see `_draining` and log an
            # interruption for the ordinary path.
            uvicorn.Server.handle_exit(self, sig, frame)


def build_config() -> uvicorn.Config:
    """The CMD arguments that used to live in the Dockerfile, as env vars.

    Defaults are the values that shipped in that CMD, so a container built
    before this step and one built after listen identically.
    """
    return uvicorn.Config(
        app,
        host=os.environ.get("COORDINATOR_HOST", "0.0.0.0"),  # noqa: S104 — container-local
        port=int(os.environ.get("COORDINATOR_PORT", "8443")),
        ssl_certfile=os.environ.get("COORDINATOR_TLS_CERT", "/certs/coordinator.crt"),
        ssl_keyfile=os.environ.get("COORDINATOR_TLS_KEY", "/certs/coordinator.key"),
    )


def main() -> None:
    configure_logging()
    DrainingServer(build_config(), shutdown_drain_seconds()).run()


if __name__ == "__main__":
    main()
