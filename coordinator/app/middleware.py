"""Correlation ID propagation, and the request's apparent client address.

Every request gets a correlation ID: reused from an incoming
`X-Correlation-ID` header if present, generated otherwise. Bound to a
contextvar so `logging_config.JSONFormatter` can stamp every log line
emitted while handling the request, and echoed back in the response
header so a client can tie its own logs to the coordinator's.

The same middleware binds the caller's apparent IP, which the admin
endpoints log (Step 2.2.1 follow-up). **That address is a hint, not an
identity.** `ADMIN_SECRET` is a single shared operator credential, so the
coordinator cannot say *which human* acted — only where the request
appeared to come from. `X-Forwarded-For` is set by ingress-nginx but is
client-supplied data on the hop before it, so a determined caller can put
anything in it. It is logged because "an admin call arrived from an
address you do not recognise" is a genuinely useful signal, especially on
the rejection path, and because it costs nothing. It is not authentication
and must never be treated as such.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")
client_ip_var: ContextVar[str] = ContextVar("client_ip", default="-")


def _client_ip(request: Request) -> str:
    """The caller's apparent address.

    Prefers the first entry of `X-Forwarded-For` — with ingress-nginx in
    front and `externalTrafficPolicy: Local` preserving the source IP
    (Step 1.5.5), that is the real external client rather than the
    ingress pod. Falls back to the socket peer for in-cluster callers,
    which have no proxy in between.

    **Deliberately NOT the same value as `source_ip` on the registration
    path**, which is `request.client.host` — the raw socket peer. The two
    names mean different things and both are correct for their purpose:

      * `client_ip` (here, admin endpoints) is forward-aware, so it names
        the real caller, and is used only for logging. It is spoofable,
        which is acceptable for a hint and unacceptable for a control.
      * `source_ip` (registration) is the unspoofable socket peer, because
        it feeds `_rate_limited` — a control, where forgeability would let
        a caller mint unlimited buckets and evade the limit entirely.

    Behind the ingress that makes registration's `source_ip` the nginx
    pod, so every external worker currently shares one rate-limit bucket.
    That is a known consequence, recorded rather than silently changed
    here: swapping it to the forwarded address would fix the bucketing and
    simultaneously make the limit evadable, which is a trade-off for an
    explicit decision, not a side effect of a logging change. The edge
    `limit-rps` at nginx (Step 1.5.5) is the unspoofable primary control
    either way.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
        token = correlation_id_var.set(correlation_id)
        ip_token = client_ip_var.set(_client_ip(request))
        try:
            response = await call_next(request)
        finally:
            client_ip_var.reset(ip_token)
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = correlation_id
        return response
