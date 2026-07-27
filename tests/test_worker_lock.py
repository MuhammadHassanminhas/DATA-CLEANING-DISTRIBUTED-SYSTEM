"""Single-instance identity-lock self-check (Step 1.5.8).

The worker's identity lock must make a SECOND worker process on the same
machine fail fast instead of silently sharing the coordinator-assigned
identity. Step 1.5.8 made that lock cross-platform (fcntl on POSIX,
msvcrt on Windows) so the bare-Python worker runs on machines without
Docker. This test exercises whichever branch the running OS uses — so on
a Windows host it actually covers the new msvcrt path.

No third-party deps: worker.py imports `websockets` at module top and
reads COORDINATOR_URL at import, neither of which the lock needs, so both
are stubbed/set here.
"""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest


def _load_worker(identity_file: Path):
    os.environ.setdefault("COORDINATOR_URL", "https://example.invalid")
    os.environ["WORKER_IDENTITY_FILE"] = str(identity_file)
    if "websockets" not in sys.modules:
        ws = types.ModuleType("websockets")
        exc = types.ModuleType("websockets.exceptions")
        exc.ConnectionClosed = type("ConnectionClosed", (Exception,), {})
        ws.exceptions = exc
        sys.modules["websockets"] = ws
        sys.modules["websockets.exceptions"] = exc
    sys.modules.pop("worker.worker", None)
    return importlib.import_module("worker.worker")


def test_second_instance_is_rejected(tmp_path):
    worker = _load_worker(tmp_path / "identity.json")

    worker._acquire_single_instance_lock()  # first instance holds the lock

    with pytest.raises(SystemExit) as exit_info:
        worker._acquire_single_instance_lock()  # second must fail fast
    assert exit_info.value.code == 1
