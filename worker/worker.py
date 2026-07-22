"""Worker agent — Phase 1.1 skeleton.

No coordinator connection yet: registration is Phase 1.3, the transport
that would carry it is Phase 1.5. This just proves the container runs,
stays alive, and can report its own liveness via a local heartbeat file
— checked by Docker HEALTHCHECK, never over an inbound port (workers
dial out only, per CLAUDE.md architectural invariant #4).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat"))
INTERVAL_SECONDS = int(os.environ.get("WORKER_LOOP_INTERVAL_SECONDS", "5"))


def main() -> None:
    print("worker starting; no coordinator connection until Phase 1.3+", flush=True)
    while True:
        HEARTBEAT_FILE.touch()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
