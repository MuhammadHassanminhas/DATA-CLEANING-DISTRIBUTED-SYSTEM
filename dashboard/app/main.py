"""Dashboard service.

Phase 1.8: live worker view. Phase 2.7: the task lifecycle view.

The browser only ever talks to this service. Every `/api/*` route is a
server-side proxy to a coordinator endpoint that requires the operator
credential, so `ADMIN_SECRET` lives in this backend's environment only and
is never sent to, or rendered in, the browser (CLAUDE.md §12: "never
rendered in the GUI"). That was true of `/api/workers` in Phase 1.8 and is
the reason Step 2.7 adds proxies rather than letting the page call the
coordinator directly with a credential handed to it.

The frontend polls and re-renders — no dashboard-facing WebSocket layer,
same call as Phase 1.8. A short poll interval already reads as real-time
to a human eye, and it means "reconnect if the browser connection drops"
is just "the next poll succeeds," not a second reconnect protocol to build
and verify on top of the one Phase 1.7 already built for workers.

**Step 2.7 makes this service a write surface for the first time** — task
submission and cancellation. See `_reject_unless_same_origin` for why that
needs a guard beyond the edge's basic auth.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

COORDINATOR_URL = os.environ["COORDINATOR_URL"]
# Step 2.2.1: the dashboard is an operator tool, so it carries the operator
# credential — not the shared worker enrollment secret it used to reuse.
# Same fallback as the coordinator (`config.admin_secret`) so the image can
# roll out before the Secret exists; both must resolve to the same value or
# the coordinator answers 401.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET") or os.environ["ENROLLMENT_SECRET"]
CA_FILE = os.environ.get("DASHBOARD_CA_FILE", "/certs/dev-ca.crt")
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Sent by the page's own fetch() calls on every write. See
# `_reject_unless_same_origin`.
WRITE_HEADER = "X-Dashboard-Write"

app = FastAPI(title="Dashboard", version="0.3.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ui/tasks")
def tasks_page() -> FileResponse:
    """The task console (Phase 2.7).

    Served under `/ui/` rather than `/tasks` because the public ingress
    routes the whole `/tasks` prefix to the coordinator's operator API
    (Step 1.5.5 / 2.6). A page at `/tasks` would be unreachable in staging
    and production while working perfectly in Docker Compose — the exact
    class of environment-dependent difference CLAUDE.md §3.5 exists to
    prevent. `/ui/*` falls under the dashboard's `/` catch-all in every
    environment, so no ingress rule changes.
    """
    return FileResponse(STATIC_DIR / "tasks.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


def _call(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    """One blocking call to the coordinator, credential attached.

    Returns `(status, raw body)` — including for a 4xx, which is a normal
    answer here rather than an exception: the coordinator's 400s, 404s and
    409s carry the `detail` the operator needs to read ("task is RUNNING:
    only a QUEUED task can be cancelled"), and swallowing them into a
    generic dashboard error would throw that away.
    """
    url = f"{COORDINATOR_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    headers = {"X-Admin-Secret": ADMIN_SECRET}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    ctx = ssl.create_default_context(cafile=CA_FILE)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _proxy(
    response: Response,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    try:
        status, raw = await asyncio.to_thread(_call, method, path, params=params, body=body)
    except urllib.error.URLError as exc:
        response.status_code = 502
        return {"error": "coordinator_unreachable", "detail": str(exc)}
    response.status_code = status
    return json.loads(raw) if raw else {}


def _reject_unless_same_origin(request: Request, response: Response) -> dict[str, str] | None:
    """Refuse a write that did not come from the dashboard's own page.

    The dashboard is protected by HTTP basic auth at the ingress (Step
    1.5.5). A browser attaches those credentials automatically to any
    request at that origin — **including a form another site submits**. So
    the moment Step 2.7 added `POST /api/tasks`, an operator with the
    dashboard open could be made to enqueue work, or cancel it, by
    visiting an unrelated page. Basic auth authenticates the browser, not
    the intent.

    Requiring a header the page sets itself closes it, because the two ways
    a cross-site request can be made cannot set one: an HTML form has no
    mechanism to add headers at all, and a cross-origin `fetch` that adds a
    custom header becomes preflighted, which fails here since no CORS
    origin is allowed. Same-origin calls from the dashboard's own JavaScript
    are unaffected.

    This is a guard against forgery, not a second authentication: the value
    is fixed and public. Authentication is the edge's, and the operator
    credential is still never in the browser.
    """
    if request.headers.get(WRITE_HEADER) != "dashboard":
        response.status_code = 403
        return {"detail": "write requests must originate from the dashboard page"}
    return None


@app.get("/api/workers")
async def api_workers(response: Response) -> Any:
    return await _proxy(response, "GET", "/workers")


@app.get("/api/tasks")
async def api_tasks(request: Request, response: Response) -> Any:
    """Proxy `GET /tasks`, forwarding only the filters the API defines.

    A whitelist rather than a pass-through of `request.query_params`: the
    dashboard should not become a way to reach coordinator parameters that
    are not part of the documented operator surface, and an unknown filter
    silently ignored by a proxy is worse than one rejected by the API.
    `status` is repeatable, so it is collected as a list.
    """
    params: dict[str, Any] = {}
    statuses = request.query_params.getlist("status")
    if statuses:
        params["status"] = statuses
    for name in ("task_type", "worker_id", "correlation_id", "limit", "offset"):
        value = request.query_params.get(name)
        if value:
            params[name] = value
    return await _proxy(response, "GET", "/tasks", params=params)


@app.get("/api/tasks/depth")
async def api_depth(response: Response) -> Any:
    return await _proxy(response, "GET", "/tasks/depth")


@app.get("/api/tasks/throughput")
async def api_throughput(request: Request, response: Response) -> Any:
    minutes = request.query_params.get("minutes", "30")
    return await _proxy(response, "GET", "/tasks/throughput", params={"minutes": minutes})


@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str, response: Response) -> Any:
    return await _proxy(response, "GET", f"/tasks/{urllib.parse.quote(task_id)}")


@app.post("/api/tasks")
async def api_submit_task(request: Request, response: Response) -> Any:
    """Submit a task from the browser (a Step 2.7 exit criterion).

    The body is forwarded as sent and validated by the coordinator, which
    already owns the task-type registry and its parameter schemas (Phase
    2.1). Re-validating here would be a second authority that can disagree
    with the first.
    """
    rejection = _reject_unless_same_origin(request, response)
    if rejection is not None:
        return rejection
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        response.status_code = 400
        return {"detail": "request body is not valid JSON"}
    return await _proxy(response, "POST", "/tasks", body=body)


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str, request: Request, response: Response) -> Any:
    rejection = _reject_unless_same_origin(request, response)
    if rejection is not None:
        return rejection
    return await _proxy(
        response, "POST", f"/tasks/{urllib.parse.quote(task_id)}/cancel", body={}
    )
