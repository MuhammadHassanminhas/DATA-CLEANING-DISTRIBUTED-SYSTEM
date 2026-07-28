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


# --------------------------------------------------------------------------
# The guards that make this value safe to use as a rate-limit key.
#
# It is only trustworthy because ingress-nginx overwrites a client-supplied
# X-Forwarded-For rather than appending to it — measured, not assumed. These
# tests pin the two guards that keep that true at the edges of the assumption.
# --------------------------------------------------------------------------


def test_forwarded_header_is_ignored_when_the_peer_is_globally_routable():
    """A caller reaching the coordinator directly from the Internet has no
    proxy in front of it, so it has no business setting the header. If this
    regressed, anyone could pick their own rate-limit bucket.

    `8.8.8.8` on purpose, not a `203.0.113.x` documentation address —
    Python's `is_private` counts the documentation ranges as private, which
    is what sent the first version of this guard to the wrong answer.
    """
    r = _request({"X-Forwarded-For": "9.9.9.9"}, peer="8.8.8.8")
    assert _client_ip(r) == "8.8.8.8"


def test_forwarded_header_is_trusted_from_a_cluster_peer():
    """The normal production path: the socket peer is the ingress pod."""
    assert _client_ip(_request({"X-Forwarded-For": "9.9.9.9"}, peer="10.244.0.9")) == "9.9.9.9"


def test_loopback_peer_is_trusted_so_local_dev_behaves_like_production():
    assert _client_ip(_request({"X-Forwarded-For": "9.9.9.9"}, peer="127.0.0.1")) == "9.9.9.9"


def test_junk_in_the_forwarded_header_falls_back_to_the_peer():
    """The value becomes a Redis key. Without this, a caller could mint an
    unbounded number of rate-limit buckets by sending garbage."""
    for junk in ("not-an-ip", "", "1.2.3", "'; DROP", "999.999.999.999"):
        r = _request({"X-Forwarded-For": junk}, peer="10.244.0.9")
        assert _client_ip(r) == "10.244.0.9", junk


def test_ipv6_forwarded_address_is_accepted():
    r = _request({"X-Forwarded-For": "2001:db8::1"}, peer="10.244.0.9")
    assert _client_ip(r) == "2001:db8::1"


def test_unparseable_peer_is_not_treated_as_a_trusted_proxy():
    r = _request({"X-Forwarded-For": "9.9.9.9"}, peer="garbage")
    assert _client_ip(r) == "garbage"


def test_distinct_callers_get_distinct_values():
    """The point of the fix: two external workers must not collapse onto one
    rate-limit bucket the way they did when this was the socket peer."""
    a = _client_ip(_request({"X-Forwarded-For": "203.0.113.10"}, peer="10.244.0.9"))
    b = _client_ip(_request({"X-Forwarded-For": "203.0.113.11"}, peer="10.244.0.9"))
    assert a != b
