"""Dashboard service — Phase 1.1 skeleton.

Placeholder page only. The live worker table (worker ID, status, CPU,
memory, latency, heartbeat, uptime) is a Phase 1.8 deliverable, once
there is a coordinator connection registry to read from.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Dashboard", version="0.1.0")

PLACEHOLDER_PAGE = """<!doctype html>
<html>
<head><title>Distributed Worker Network — Dashboard</title></head>
<body>
<h1>Dashboard</h1>
<p>Live worker view arrives in Phase 1.8.</p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return PLACEHOLDER_PAGE


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}
