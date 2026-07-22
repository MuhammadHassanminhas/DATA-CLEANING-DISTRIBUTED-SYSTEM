# SESSION_HANDOFF.md

Read `CLAUDE.md` first (guardrails), then `PHASE_STATE.md` (authoritative
status, decisions log, blockers). This file is a resume-work pointer for
the next session — it is not a source of truth, `PHASE_STATE.md` is.

---

# Where things stand

Phases 1.0–1.4 are `DONE` (approved in earlier sessions and at the start
of this one). **Phase 1.5 — persistent connection transport — was built
and verified this session and is `AWAITING APPROVAL`.** Don't silently
flip it to `DONE`; surface it and let the user confirm, same as always.

- **Phase 1.5**: added a `/ws/connect` WebSocket endpoint to the
  coordinator (handshake: Bearer access token in the `Authorization`
  header, then a `hello` envelope naming worker ID and protocol
  version, acked with an assigned session epoch), a Redis connection
  registry (`worker:{id}:connection`, TTL refreshed by an app-level
  ping/pong every ~20s), an on-demand push endpoint
  (`POST /workers/{id}/push`, fire-and-forward via Redis pub/sub, no
  delivery guarantee — that's M2's job), and rewrote the worker's main
  loop from synchronous polling to an async WebSocket client
  (`websockets` library, new dependency). All six exit criteria in
  `docs/phase-1-reliable-worker-network.md` Step 1.5 verified live — see
  that file and `PHASE_STATE.md` Decisions Log #18–#22 for exact
  verification notes and design reasoning. Headline result: a real
  connection was held open **31 minutes 52 seconds** with zero drops
  (exceeds the 30-minute exit criterion), verified by wall-clock
  timestamps, not simulated.
- **A real bug was found and fixed during this phase's own
  verification, not left for later**: the worker's reconnect loop
  (`_run_ws_forever` in `worker/worker.py`) originally left the
  token-refresh call outside its `try`/`except`. A connection-refused
  window during a coordinator container recreate raised
  `urllib.error.URLError`, which wasn't caught, which silently killed
  the entire reconnect task — the worker container kept reporting
  `healthy` throughout because its Docker healthcheck only watches a
  separate heartbeat-file loop, unaffected by the dead connection loop.
  Root-caused and fixed (moved the refresh inside the same
  `try`/`except`, added `URLError` to the caught types); re-verified
  live afterward — reconnect now completes in ~3 seconds every time.
- **Dead code was found and removed, not left in**: an app-level
  `_drain_local_connections()` step was built first for graceful
  coordinator shutdown, then verified live to never actually run with
  anything to do — uvicorn's own `Server.shutdown()` (source inspected
  directly at `uvicorn==0.51.0`) closes every live WebSocket and waits
  for its handler task to finish *before* the app's lifespan shutdown
  code runs, so the per-connection handler's own `finally` cleanup had
  already done the real work. Deleted rather than left as a function
  whose docstring claimed behavior it could never perform. See
  `PHASE_STATE.md` Decisions Log #21.

**Next step**: awaiting approval of Phase 1.5, then **Phase 1.6 —
Heartbeat and liveness detection**, per
`docs/phase-1-reliable-worker-network.md`.

---

# Open items — not done, not something this session finished

1. **Branch protection on `main`** — still not configured. Unchanged
   from prior sessions; not revisited this session (no reason to —
   nothing this session touched CI or `origin`).
2. **PR #1 is still open and unmerged.** Nobody asked for it to be
   merged. Unchanged from prior sessions.
3. **Everything from this session is uncommitted** in the working tree
   on `main`, on top of the prior session's own uncommitted state:
   modified `.env.example`, `coordinator/Dockerfile`,
   `coordinator/app/main.py`, `coordinator/requirements.txt`,
   `docker-compose.yml`, both `PHASE_STATE.md` copies,
   `docs/phase-1-reliable-worker-network.md`, `worker/Dockerfile`,
   `worker/worker.py`; new untracked
   `coordinator/app/{config,db,logging_config,middleware,models,
   redis_client,schemas,security}.py`, `coordinator/migrations/`, and
   new this session: `worker/requirements.txt`. Nobody asked for a
   commit this session, so none was made.
4. **`.claude.backup/` and a stray `certs;C` directory** at the repo
   root — both still present, unchanged, still unexplained/harmless,
   carried forward from prior sessions (see prior gotchas below for
   `certs;C`'s likely cause). Still not cleaned up since it wasn't this
   session's to create or judge whether safe to delete.
5. **The real `.env`** (gitignored) still has the dev-only
   `ENROLLMENT_SECRET`/`CREDENTIAL_PEPPER` values set in a prior
   session. Unchanged.

---

# Gotchas discovered this session (save yourself the debugging time)

- **Uvicorn closes live WebSocket connections before the ASGI lifespan
  shutdown event fires.** `Server.shutdown()` (uvicorn 0.51.0) calls
  `connection.shutdown()` on every active connection and awaits
  `_wait_tasks_to_complete()` **before** calling
  `self.lifespan.shutdown()`. If you're relying on FastAPI's `lifespan`
  post-`yield` code to message every live WebSocket before the process
  exits, it won't see any — they're already closed and their handler
  tasks already finished by the time that code runs. Do per-connection
  cleanup in the connection handler's own `finally` block instead;
  don't build a separate app-level drain step expecting to reach live
  sockets during lifespan shutdown.
- **An exception inside an `asyncio.create_task`'d coroutine that's
  never awaited (until process shutdown) dies silently.** In
  `worker/worker.py`, the WS reconnect loop is a background task
  (`ws_task = asyncio.create_task(_run_ws_forever(...))`) that's only
  ever `await`ed in the final shutdown path. An unhandled exception
  partway through the loop body kills the task with no visible error
  in the normal log stream — the process itself keeps running (other
  tasks, like the heartbeat-file loop, are unaffected), which makes it
  look healthy while actually being half-dead. Wrap the *entire* loop
  body in the retry `try`/`except`, not just the part you expect to
  fail — a narrower `try` around just the "risky-looking" call left a
  gap around the token refresh that took a real coordinator restart to
  expose.
- **`websockets` library's `extra_headers` param name is version-
  dependent.** Pinned `websockets>=12,<13` specifically because v14+
  renamed it to `additional_headers` (and removed the old name in v15).
  If bumping this dependency later, check that param name changed
  before assuming a bump is a no-op.
- **Git Bash / MSYS path-mangling still applies** to any
  `docker compose exec`/`cp` call taking an absolute Linux path — same
  fix as documented in prior sessions: prefix with `MSYS_NO_PATHCONV=1`.
  Hit again this session copying a scratch test script into the worker
  container.
- **The Bash tool itself was intermittently flaky this session** —
  several plain commands (even `cat`, `exit 0`) randomly failed with
  `MSYS bash: fatal error - add_item(...) failed, errno 1` or silently
  moved to background despite short expected runtimes. Not caused by
  anything in this repo; switching to the PowerShell tool for the same
  command worked when Bash was stuck. If a trivial command mysteriously
  hangs or fails, try the other shell tool before assuming a real bug.

---

# Verification method used this session (keep using it)

Same discipline as before, extended for a live-connection phase:
- `docker compose build` / `up -d` / `ps` for container health.
- `docker compose logs <service> -t --no-log-prefix` for structured
  JSON log inspection, including timestamp deltas (this session: proving
  a 31m52s hold with zero reconnects, and a ~3s reconnect after a
  coordinator restart).
- `curl -sk` directly against `https://localhost:8443/...` for the push
  endpoint and a throwaway worker registration used only to obtain a
  real access token for the version-mismatch test.
- A raw WebSocket client script run *inside* the worker container
  (`docker compose cp` + `docker compose exec worker python ...`,
  `MSYS_NO_PATHCONV=1` required) to exercise a protocol-version
  mismatch — something the worker's own client, which always sends the
  correct version, can't demonstrate on its own.
- `docker compose exec redis redis-cli GET`/`TTL` for direct connection-
  registry inspection.
- `docker compose exec coordinator python -c "..."` to inspect
  installed library source directly (`uvicorn.server.Server.shutdown`)
  rather than guess at its behavior from docs/memory.
- `docker compose restart coordinator` (twice — once before the
  reconnect-loop bug was found, once after the fix) to verify graceful
  shutdown and reconnect end to end.
- `uvx --with ruff ruff check coordinator worker dashboard protocol`
  before and after every change — same command CI runs.
- `bash scripts/teardown.sh` at the end, confirmed clean (containers,
  network, both volumes removed).

Phase 1.6 (heartbeat and liveness) will need the same rigor applied to
sustained-run scenarios: missed-heartbeat timing has to be measured
against real elapsed time the same way the 30-minute hold was this
session, not asserted.
