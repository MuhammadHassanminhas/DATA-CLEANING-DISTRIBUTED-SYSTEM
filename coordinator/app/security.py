"""Worker credential generation and verification.

Raw credentials are high-entropy random tokens (256 bits from
`secrets.token_urlsafe`), so a fast keyed hash (HMAC-SHA256 with a
server-side pepper) is adequate at-rest protection — the slow KDFs used
for human passwords (bcrypt/argon2) exist to slow brute-forcing of
low-entropy secrets, which doesn't apply to a random token. The pepper
is a secret independent of any single credential, from `CREDENTIAL_PEPPER`
(secret-manager-backed per CLAUDE.md's security baseline) — a stolen
`credential_hash` column alone isn't enough to forge a match.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import admin_secret, credential_pepper, enrollment_secret


def generate_worker_credential() -> str:
    return secrets.token_urlsafe(32)


# Access tokens are the same shape as the worker credential (a high-entropy
# random token) and are hashed the same way — `generate_access_token` and
# `hash_credential` are aliases in all but name, kept distinct so call
# sites read clearly about which secret they're handling.
generate_access_token = generate_worker_credential


def hash_credential(raw: str) -> str:
    return hmac.new(credential_pepper().encode(), raw.encode(), hashlib.sha256).hexdigest()


def verify_credential(raw: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_credential(raw), expected_hash)


def verify_enrollment_secret(provided: str) -> bool:
    return hmac.compare_digest(provided, enrollment_secret())


def verify_admin_secret(provided: str) -> bool:
    """Operator authority, deliberately a different credential from the
    worker enrollment secret since Step 2.2.1 — see `config.admin_secret`
    for why that separation matters and what happens before it is set."""
    return hmac.compare_digest(provided, admin_secret())
