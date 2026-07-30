"""Worker agent package.

Phase 2.4 is the first step where the worker is more than one file: the
executors live in `executors.py`, separately from the connection loop in
`worker.py`, because they run on different threads and are tested
differently — the executors against known-answer vectors, the loop against
a real coordinator.

Making this a real package rather than two flat modules keeps one import
form (`from worker import executors`) working in all three places the
worker runs: the Docker image (`python -m worker.worker`), the native
installers (`worker/worker.py` with `PYTHONPATH` at the repo root, which
is already set there so `protocol` resolves), and the test suite.
"""
