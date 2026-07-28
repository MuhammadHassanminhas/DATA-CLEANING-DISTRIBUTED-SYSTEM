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

import ipaddress
import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")
client_ip_var: ContextVar[str] = ContextVar("client_ip", default="-")


def _is_in_cluster(address: str) -> bool:
    """Whether a socket peer is inside the cluster (or the loopback of a
    local dev run) — i.e. whether an `X-Forwarded-For` it presents can be
    believed.

    Anything that reaches the coordinator through the public endpoint
    arrives from the ingress-nginx pod, which has a cluster-internal
    address. A caller that reaches it from a globally routable address has
    no proxy in front of it and so has no business setting the header.

    Phrased as "not globally routable" rather than `is_private`, which is
    narrower than it looks: `is_private` is True for the RFC 5737
    documentation ranges too, so `203.0.113.x` — the address any example
    reaches for — would have counted as trusted. `is_global` is the
    predicate that actually means "reached us directly from the Internet".
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not parsed.is_global


def _caller_ip(request: Request) -> str:
    """The caller's real address — for logging *and* for rate limiting.

    One function for both, deliberately. An earlier version had two: a
    forward-aware `client_ip` for logs and the raw socket peer for the
    registration rate limiter, on the reasoning that a forgeable value
    must never feed a control. **That reasoning was wrong here, and the
    check that settled it was a measurement, not an argument:** a request
    to the public endpoint carrying `X-Forwarded-For: 9.9.9.9` was logged
    by the coordinator as the sender's actual public address. ingress-nginx
    runs with `use-forwarded-headers` at its default of false, so it
    *overwrites* the header with the peer it observed rather than
    appending to it. Behind this ingress the value is therefore not
    forgeable, and using it as the rate-limit key is sound.

    That fixes a real defect. Keying on the socket peer meant every
    external worker shared one bucket — the nginx pod — so
    `REGISTER_RATE_LIMIT_PER_MINUTE` was a *fleet-wide* cap rather than a
    per-client one. A coordinated fleet restart, exactly what M3's
    fault-tolerance work provokes, would have had most workers rate-limited
    on reconnect. Note the Phase 1.10 result of 49 workers recovering in
    18s was measured on Docker Compose, where every container had its own
    address and the defect could not appear.

    Two guards keep this honest:

      * the header is trusted **only** when the socket peer is inside the
        cluster (`_is_in_cluster`), so a direct caller cannot self-declare;
      * the value must parse as an IP address, so a caller cannot mint
        unlimited Redis buckets by sending junk.

    Falls back to the socket peer whenever either guard fails, which is
    also the normal path for in-cluster callers and local dev, where there
    is no proxy at all.
    """
    peer = request.client.host if request.client else None
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and peer and _is_in_cluster(peer):
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return peer
        return candidate
    return peer or "-"


# Retained name for the admin-log path, which reads more clearly as
# "the client's IP" at its call sites.
_client_ip = _caller_ip


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
