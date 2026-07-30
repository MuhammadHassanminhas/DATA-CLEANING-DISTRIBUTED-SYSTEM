FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY worker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY protocol ./protocol
# Phase 2.4: the worker is a package now (worker.py + executors.py), so the
# directory is copied and run with `-m` rather than a single flat file.
COPY worker/__init__.py worker/worker.py worker/executors.py ./worker/

HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "\
import pathlib, sys, time; \
p = pathlib.Path('/tmp/worker-heartbeat'); \
sys.exit(0 if p.exists() and time.time() - p.stat().st_mtime < 60 else 1)"

CMD ["python", "-m", "worker.worker"]
