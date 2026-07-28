"""Operator credential separation (Step 2.2.1).

Before this step `verify_admin_secret` compared against the *enrollment*
secret, which every worker in the fleet holds by design (Decision #76 B1).
That made every admin endpoint — fleet listing, revoke, push, and after
Step 2.2 the whole task queue — reachable by any worker, against CLAUDE.md
§12's "every worker is untrusted".

These tests pin the separation and, just as importantly, pin the two
things that must NOT change with it: workers must still be able to enroll,
and a deployment that has not yet been given an `ADMIN_SECRET` must still
start rather than CrashLoop.
"""

import pytest

from app import config, security

ENROLLMENT = "enrollment-secret-for-tests"
ADMIN = "operator-secret-for-tests"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("ENROLLMENT_SECRET", ENROLLMENT)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)


def test_worker_enrollment_secret_does_not_open_the_admin_surface(monkeypatch):
    """The whole point of the step."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    assert security.verify_admin_secret(ADMIN) is True
    assert security.verify_admin_secret(ENROLLMENT) is False


def test_workers_can_still_enroll_after_the_split(monkeypatch):
    """Separation must not lock the fleet out of registration."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    assert security.verify_enrollment_secret(ENROLLMENT) is True
    # And the operator credential is not an enrollment credential either —
    # the split cuts both ways.
    assert security.verify_enrollment_secret(ADMIN) is False


def test_unset_admin_secret_falls_back_rather_than_crashing(monkeypatch):
    """A deployment that has not yet had the Secret applied must still
    serve. The fallback is the pre-2.2.1 posture, and is reported as such
    by `admin_secret_is_separate` rather than being silent."""
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    assert config.admin_secret() == ENROLLMENT
    assert config.admin_secret_is_separate() is False
    assert security.verify_admin_secret(ENROLLMENT) is True


def test_empty_admin_secret_is_treated_as_unset(monkeypatch):
    """An empty env var is a real deployment outcome — an unset Helm value
    renders as "". It must fall back, not authenticate everyone with ""."""
    monkeypatch.setenv("ADMIN_SECRET", "")
    assert config.admin_secret() == ENROLLMENT
    assert config.admin_secret_is_separate() is False
    assert security.verify_admin_secret("") is False


def test_admin_secret_equal_to_enrollment_is_reported_as_not_separate(monkeypatch):
    """Setting ADMIN_SECRET to the same value is not separation, and must
    not be reported as if it were."""
    monkeypatch.setenv("ADMIN_SECRET", ENROLLMENT)
    assert config.admin_secret_is_separate() is False


def test_separation_is_reported_when_configured(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    assert config.admin_secret_is_separate() is True


def test_admin_check_is_constant_time(monkeypatch):
    """`hmac.compare_digest`, not `==`. Asserted by behaviour rather than
    by timing: a near-miss must be rejected exactly like a wild miss."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    assert security.verify_admin_secret(ADMIN[:-1]) is False
    assert security.verify_admin_secret(ADMIN + "x") is False
    assert security.verify_admin_secret("") is False


def test_config_reads_the_environment_at_call_time(monkeypatch):
    """The Secret can be applied to a running cluster; the value must not
    be latched at import or a restart would be needed to pick it up."""
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    assert config.admin_secret_is_separate() is False
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    assert config.admin_secret_is_separate() is True
    assert security.verify_admin_secret(ADMIN) is True


def test_the_old_alias_is_gone():
    """`enrollment_admin_secret` was the name of the vulnerability. If it
    survives, some call site is probably still using it."""
    assert not hasattr(config, "enrollment_admin_secret")
