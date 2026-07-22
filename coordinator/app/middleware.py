"""Correlation ID propagation.

Every request gets a correlation ID: reused from an incoming
`X-Correlation-ID` header if present, generated otherwise. Bound to a
contextvar so `logging_config.JSONFormatter` can stamp every log line
emitted while handling the request, and echoed back in the response
header so a client can tie its own logs to the coordinator's.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
