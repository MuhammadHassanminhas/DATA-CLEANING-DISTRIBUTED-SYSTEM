"""Structured JSON logging.

Every log line is one JSON object: timestamp, service, level, event
(the logger message), correlation_id (from the request-scoped
contextvar in middleware.py, "-" outside a request), plus any extra
fields passed via `logger.info(event, extra={...})`. This satisfies
CLAUDE.md's "every service emits structured JSON logs" requirement —
worker_id/task_id correlation arrives with the phases that produce
those IDs (1.3+).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from app.middleware import correlation_id_var

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "service": "coordinator",
            "level": record.levelname,
            "event": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
