"""Caller address on admin log lines (Step 2.2.1 follow-up).

`ADMIN_SECRET` is a single shared operator credential, so the coordinator
can record *that* an operator acted but not *which human*. The apparent
client address is the closest thing to attribution that exists, and it is
explicitly a hint — these tests pin the extraction rules, not any security
property, because the value is spoofable and must never gate access.
"""

from types import SimpleNamespace

from app.middleware import _client_ip, client_ip_var


def _request(headers: dict[str, str], peer: str | None = "10.0.0.1"):
    """A stand-in with just the two attributes `_client_ip` reads."""
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=peer) if peer else None,
    )


def test_external_client_is_taken_from_the_forwarded_header():
    """Behind ingress-nginx the socket peer is the ingress pod, so the
    forwarded header is what identifies the real caller."""
    r = _request({"X-Forwarded-For": "203.0.113.7"}, peer="10.244.0.9")
    assert _client_ip(r) == "203.0.113.7"


def test_leftmost_forwarded_entry_wins_through_a_proxy_chain():
    r = _request({"X-Forwarded-For": "203.0.113.7, 10.244.0.9, 10.244.0.3"})
    assert _client_ip(r) == "203.0.113.7"


def test_forwarded_entries_are_stripped_of_whitespace():
    r = _request({"X-Forwarded-For": "  203.0.113.7 ,10.244.0.9"})
    assert _client_ip(r) == "203.0.113.7"


def test_in_cluster_caller_falls_back_to_the_socket_peer():
    """No proxy in front, so no forwarded header — the peer IS the caller."""
    assert _client_ip(_request({}, peer="10.244.1.50")) == "10.244.1.50"


def test_empty_forwarded_header_falls_back_rather_than_logging_blank():
    assert _client_ip(_request({"X-Forwarded-For": ""}, peer="10.244.1.50")) == "10.244.1.50"


def test_missing_peer_degrades_to_a_placeholder_not_an_exception():
    """A log line must never be the thing that takes a request down."""
    assert _client_ip(_request({}, peer=None)) == "-"


def test_default_outside_request_scope_is_a_placeholder():
    """Background work — the heartbeat sweep, WebSocket coroutines — has no
    request, and must read as unknown rather than inherit a stale address."""
    assert client_ip_var.get() == "-"
