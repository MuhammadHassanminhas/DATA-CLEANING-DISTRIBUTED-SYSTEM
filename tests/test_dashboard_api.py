"""Dashboard proxy layer (Phase 2.7).

The dashboard has had no tests until now, and Step 2.7 is what makes that
untenable: Phase 1.8 shipped one read-only proxy, and this step adds five
more plus the first **write** path in the whole GUI.

The coordinator is stubbed rather than run. What is under test here is the
proxy's own behaviour — which parameters it forwards, what it does with a
4xx, what it refuses — and none of that involves the coordinator being
real. The coordinator's side of these calls is covered against a real
Postgres in `test_operator_api.py`.

Two properties are the reason this file exists at all:

* **The operator credential never reaches the browser** (§12). It is
  attached by `_call` and must appear in no response.
* **A write must come from the dashboard's own page.** Edge basic auth is
  attached by the browser to cross-site requests too, so without a header
  guard an operator with the dashboard open could be made to enqueue or
  cancel work by visiting another page.
"""

import importlib.util
import json
from pathlib import Path

import pytest

DASHBOARD_MAIN = Path(__file__).resolve().parent.parent / "dashboard" / "app" / "main.py"

FAKE_ADMIN = "dashboard-test-admin-secret"


def _load_dashboard_module():
    """Import `dashboard/app/main.py` under its own module name.

    Not via `sys.path` and `import app.main`, because the **coordinator** is
    also a package called `app` and `conftest.py` puts it on the path for
    the whole run. Adding the dashboard alongside it would make `app.main`
    mean different things depending on import order, and would break every
    other test module in the suite. The dashboard imports nothing from its
    own package, so loading the file directly is exact.
    """
    spec = importlib.util.spec_from_file_location("dashboard_main", DASHBOARD_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dashboard(monkeypatch):
    """The dashboard app with a stubbed coordinator behind it.

    `calls` records every outbound request the proxy makes, which is how
    "it forwards exactly these parameters" is asserted rather than inferred
    from a response body the stub made up.
    """
    monkeypatch.setenv("COORDINATOR_URL", "https://coordinator.invalid:8443")
    monkeypatch.setenv("ADMIN_SECRET", FAKE_ADMIN)
    monkeypatch.setenv("ENROLLMENT_SECRET", "not-the-admin-secret")

    from fastapi.testclient import TestClient

    dashboard_main = _load_dashboard_module()

    calls: list[dict] = []
    replies: dict = {"status": 200, "body": {"ok": True}}

    def fake_call(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return replies["status"], json.dumps(replies["body"]).encode()

    monkeypatch.setattr(dashboard_main, "_call", fake_call)

    with TestClient(dashboard_main.app) as client:
        client.dashboard_main = dashboard_main
        yield client, calls, replies


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def test_listing_forwards_only_the_documented_filters(dashboard):
    client, calls, _ = dashboard
    client.get(
        "/api/tasks",
        params={
            "status": ["QUEUED", "RUNNING"],
            "task_type": "sleep",
            "limit": "20",
            "offset": "40",
            "not_a_filter": "surprise",
        },
    )
    assert calls[-1]["path"] == "/tasks"
    assert calls[-1]["params"] == {
        "status": ["QUEUED", "RUNNING"],
        "task_type": "sleep",
        "limit": "20",
        "offset": "40",
    }, "an undocumented parameter must not be smuggled through the proxy"


def test_the_literal_paths_are_not_swallowed_by_the_task_id_route(dashboard):
    """`/api/tasks/depth` and `/api/tasks/throughput` share a prefix with
    `/api/tasks/{task_id}`. FastAPI matches in declaration order, so this is
    the test that fails if someone reorders them."""
    client, calls, _ = dashboard
    client.get("/api/tasks/depth")
    assert calls[-1]["path"] == "/tasks/depth"
    client.get("/api/tasks/throughput", params={"minutes": "45"})
    assert calls[-1] == {
        "method": "GET",
        "path": "/tasks/throughput",
        "params": {"minutes": "45"},
        "body": None,
    }
    client.get("/api/tasks/9f1e0c2a-0000-0000-0000-000000000000")
    assert calls[-1]["path"] == "/tasks/9f1e0c2a-0000-0000-0000-000000000000"


def test_a_coordinator_4xx_is_passed_through_with_its_detail(dashboard):
    """A refused cancel says *why* — "task is RUNNING: only a QUEUED task
    can be cancelled". Collapsing that into a generic dashboard error would
    throw away the only part the operator can act on."""
    client, _, replies = dashboard
    replies["status"] = 409
    replies["body"] = {"detail": "task is RUNNING: only a QUEUED task can be cancelled"}
    r = client.post(
        "/api/tasks/abc/cancel", headers={"X-Dashboard-Write": "dashboard"}
    )
    assert r.status_code == 409
    assert "only a QUEUED task" in r.json()["detail"]


def test_an_unreachable_coordinator_is_a_502_not_a_traceback(dashboard):
    import urllib.error

    client, _, _ = dashboard

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    client.dashboard_main._call = boom
    r = client.get("/api/tasks")
    assert r.status_code == 502
    assert r.json()["error"] == "coordinator_unreachable"


def test_the_operator_credential_never_reaches_the_browser(dashboard):
    """§12: credentials are never rendered in the GUI. The proxy exists for
    this property, so it gets an explicit test rather than being taken on
    trust from a code read."""
    client, calls, replies = dashboard
    replies["body"] = {"tasks": [], "count": 0}
    for path in ("/api/workers", "/api/tasks", "/api/tasks/depth", "/api/tasks/throughput"):
        assert FAKE_ADMIN not in client.get(path).text
    # And it is not in the page the browser loads either.
    assert FAKE_ADMIN not in client.get("/").text
    assert FAKE_ADMIN not in client.get("/ui/tasks").text
    assert calls, "the stub should have been called"


def test_both_pages_are_served(dashboard):
    client, _, _ = dashboard
    assert client.get("/").status_code == 200
    tasks_page = client.get("/ui/tasks")
    assert tasks_page.status_code == 200
    assert "throughput" in tasks_page.text
    assert client.get("/static/console.css").status_code == 200


# --------------------------------------------------------------------------
# The recovery console (Phase 3.7)
# --------------------------------------------------------------------------


def test_the_recovery_feed_forwards_only_its_own_filters(dashboard):
    """`outcome` is repeatable so the page can watch reassignments and
    fences together, and everything else is dropped for the same reason
    `/api/tasks` drops it: the dashboard must not become a way to reach
    coordinator parameters that are not part of the documented surface."""
    client, calls, _ = dashboard
    client.get(
        "/api/tasks/attempts",
        params={
            "outcome": ["REASSIGNED", "FENCED"],
            "worker_id": "9f1e0c2a-0000-0000-0000-000000000000",
            "limit": "100",
            "task_id": "smuggled",
        },
    )
    assert calls[-1]["path"] == "/tasks/attempts"
    assert calls[-1]["params"] == {
        "outcome": ["REASSIGNED", "FENCED"],
        "worker_id": "9f1e0c2a-0000-0000-0000-000000000000",
        "limit": "100",
    }


def test_the_attempts_path_is_not_swallowed_by_the_task_id_route(dashboard):
    """`/api/tasks/attempts` shares its prefix with `/api/tasks/{task_id}`,
    exactly as `/depth` and `/throughput` do. Declaration order is the only
    thing keeping it a feed rather than a lookup of a task called
    "attempts", so it gets the same regression test they have."""
    client, calls, _ = dashboard
    client.get("/api/tasks/attempts")
    assert calls[-1]["path"] == "/tasks/attempts"


def test_an_unknown_outcome_is_the_coordinators_refusal_not_the_proxys(dashboard):
    """The proxy does not validate outcome values. The coordinator owns that
    vocabulary, and a second validator here could only ever disagree with
    it — so the 400 must arrive from behind the proxy, with its detail."""
    client, calls, replies = dashboard
    replies["status"] = 400
    replies["body"] = {"detail": "unknown outcome 'REASIGNED'"}
    response = client.get("/api/tasks/attempts", params={"outcome": "REASIGNED"})
    assert response.status_code == 400
    assert "REASIGNED" in response.json()["detail"]
    assert calls[-1]["params"] == {"outcome": ["REASIGNED"]}


def test_the_worker_failure_counters_are_proxied(dashboard):
    client, calls, _ = dashboard
    client.get("/api/workers/failures")
    assert calls[-1] == {
        "method": "GET",
        "path": "/workers/failures",
        "params": None,
        "body": None,
    }


def test_the_recovery_page_is_served_and_carries_no_credential(dashboard):
    client, _, _ = dashboard
    page = client.get("/ui/recovery")
    assert page.status_code == 200
    assert FAKE_ADMIN not in page.text
    # The four panels the step's exit criteria name, identified by the text
    # the operator actually sees rather than by an id only this test knows.
    for panel in ("failed tasks", "failed workers", "worker reliability", "recovery timeline"):
        assert panel in page.text


def test_every_endpoint_the_recovery_page_calls_exists_on_the_proxy(dashboard):
    """The page is static HTML, so a renamed proxy route breaks it silently
    — the browser 404s and a panel stays on "loading…". This walks the
    `/api/...` literals out of the page and asserts each one routes."""
    import re

    client, _, _ = dashboard
    page = client.get("/ui/recovery").text
    # Trailing slash stripped because the page builds one path by
    # interpolation (`/api/tasks/${taskId}`), and what is being checked is
    # the route it lands on, not the id.
    paths = {match.rstrip("/") for match in re.findall(r"/api/[a-z/]+", page)}
    assert paths, "the page should call the dashboard's own API"
    for path in paths:
        assert client.get(path).status_code != 404, f"{path} is called by the page and does not route"


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_a_write_without_the_page_header_is_refused(dashboard):
    """The cross-site forgery guard. A form on another site can make the
    browser POST here with the edge's basic-auth credentials attached, but
    it cannot add a header — so this is what stops that request from
    enqueueing work."""
    client, calls, _ = dashboard
    before = len(calls)

    submit = client.post("/api/tasks", json={"task_type": "sleep", "parameters": {"seconds": 1}})
    cancel = client.post("/api/tasks/abc/cancel")

    assert submit.status_code == 403 and cancel.status_code == 403
    assert len(calls) == before, "a refused write must not reach the coordinator at all"


def test_a_write_from_the_page_is_forwarded_verbatim(dashboard):
    """The body is not re-validated here. The coordinator owns the task-type
    registry and its parameter bounds (Phase 2.1); a second validator in the
    dashboard is a second authority that can disagree with the first."""
    client, calls, replies = dashboard
    replies["status"] = 201
    replies["body"] = {"status": "queued", "count": 3, "correlation_id": "cid-1"}

    body = {"task_type": "hash_rounds", "parameters": {"rounds": 5}, "count": 3, "priority": -1}
    r = client.post("/api/tasks", json=body, headers={"X-Dashboard-Write": "dashboard"})

    assert r.status_code == 201
    assert r.json()["correlation_id"] == "cid-1"
    assert calls[-1] == {"method": "POST", "path": "/tasks", "params": None, "body": body}


def test_a_body_that_is_not_json_is_refused_before_the_coordinator(dashboard):
    client, calls, _ = dashboard
    before = len(calls)
    r = client.post(
        "/api/tasks",
        content=b"not json",
        headers={"X-Dashboard-Write": "dashboard", "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert len(calls) == before
