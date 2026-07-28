"""Dashboard service.

Phase 1.8: live worker view. The browser only ever talks to this
service — `/api/workers` is a server-side proxy to the coordinator's
admin-protected `GET /workers` (Phase 1.8), so the admin secret lives
in this backend's environment only and is never sent to, or rendered
in, the browser (CLAUDE.md §12: "never rendered in the GUI").

The frontend polls `/api/workers` every few seconds and re-renders —
no dashboard-facing WebSocket layer for v1. A short poll interval
already reads as real-time to a human eye, and it means "reconnect if
the browser connection drops" (Step 1.8's own exit criterion) is just
"the next poll succeeds," not a second reconnect protocol to build and
verify on top of the one Phase 1.7 already built for workers.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse

COORDINATOR_URL = os.environ["COORDINATOR_URL"]
# Step 2.2.1: the dashboard is an operator tool, so it carries the operator
# credential — not the shared worker enrollment secret it used to reuse.
# Same fallback as the coordinator (`config.admin_secret`) so the image can
# roll out before the Secret exists; both must resolve to the same value or
# the coordinator answers 401.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET") or os.environ["ENROLLMENT_SECRET"]
CA_FILE = os.environ.get("DASHBOARD_CA_FILE", "/certs/dev-ca.crt")
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Dashboard", version="0.2.0")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


def _fetch_workers() -> dict[str, object]:
    ctx = ssl.create_default_context(cafile=CA_FILE)
    req = urllib.request.Request(
        f"{COORDINATOR_URL}/workers",
        headers={"X-Admin-Secret": ADMIN_SECRET},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        return json.loads(resp.read())


@app.get("/api/workers")
async def api_workers(response: Response) -> dict[str, object]:
    try:
        return await asyncio.to_thread(_fetch_workers)
    except urllib.error.URLError as exc:
        response.status_code = 502
        return {"error": "coordinator_unreachable", "detail": str(exc)}
