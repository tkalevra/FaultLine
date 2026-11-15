"""A seat disabled by a tenant admin must not be able to sign in — and must be TOLD.

THE BUG THIS PINS (owner-reported, then MEASURED on pre-prod before the fix):

    email                     seat_status  login_status  verdict
    chris-test@example.com    revoked      active        CAN STILL LOG IN
    test@test.ca              revoked      active        CAN STILL LOG IN
    matest@example.invalid    revoked      active        CAN STILL LOG IN
    chris-test2@example.com   revoked      active        CAN STILL LOG IN

TWO SEPARATE STATUSES, only one was checked. `get_individual_by_email` filters
`individual_users.status = 'active'` — the LOGIN account. The tenant console's "disable user"
writes `account_users.status = 'revoked'` (saas/seats.py:924) — the SEAT. Nothing consulted the
seat, so an administrator could disable someone who then kept signing in and reading their
memory as though nothing had happened.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from saas import user_console

# An OBVIOUS test constant — never mistakable for a real secret, and never a library default.
# `dashboard_auth._session_secret()` is fail-CLOSED by design: unset ⇒ raise, so that an
# unsigned (forgeable) session can never be issued in production. That control is itself
# pinned by tests/test_saas_tenant_auth.py::test_session_secret_unset_fails_closed, which
# DELETES this variable. Nothing here may weaken it — we only give this module a value to
# sign WITH, exactly as ~40 sibling saas tests already do.
_TEST_SIGNING_SECRET = "unit-test-only-not-a-real-secret-disabled-seat-login"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """Make this module independent of the operator's shell.

    Three of these tests reach the SUCCESS path, and `me_login` mints a real CSRF token there
    (`_auth.issue_individual_csrf` → itsdangerous). Without a signing secret in the environment
    they raised `DashboardAuthError: SAAS_SESSION_SECRET unset` — so the file passed 10/10 for
    whoever happened to have the variable exported and 7/10 for everyone else. A suite whose
    result depends on the caller's shell cannot be a baseline.

    `monkeypatch` (not a module-level `os.environ.setdefault`) so the value is scoped to this
    module and RESTORED afterwards — a process-wide set would leak into the fail-closed test
    above when the whole suite runs in one process.
    """
    monkeypatch.setenv("SAAS_SESSION_SECRET", _TEST_SIGNING_SECRET)
    monkeypatch.delenv("SAAS_SESSION_SECRET_PREVIOUS", raising=False)


def _individual(uid="u-1"):
    return {"id": "ind-1", "user_id": uid, "email": "person@example.com",
            "password_hash": "x", "status": "active"}


def _login(seat_status, *, raises=None):
    """Drive the REAL me_login with the credential check satisfied, varying only the seat row."""
    cur = MagicMock()
    if raises:
        cur.execute.side_effect = raises
    else:
        cur.fetchone.return_value = (seat_status,) if seat_status is not None else None
    conn = MagicMock()
    # db.connection() is used as `with db.connection() as _c, _c.cursor() as _cur:` — BOTH
    # levels are context managers. Without __enter__ returning conn, fetchone() yields a
    # MagicMock that is truthy and never equals "active", so every login 403s and the test
    # "proves" a bug that does not exist. (Dead-mock trap: wire the mock to the REAL shape.)
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur

    # NOT mocked, on purpose: `_auth.issue_individual_csrf`. An earlier revision patched
    # `issue_csrf` — the TENANT-realm issuer, which `me_login` never calls — so the patch was
    # inert and the real signer ran regardless. That inert patch is what made this module depend
    # on SAAS_SESSION_SECRET. It is REMOVED rather than "corrected": letting the real CSRF mint
    # run is the stronger test, and a mock that silently intercepts nothing is precisely the
    # dead-mock trap warned about just above.
    with patch.object(user_console, "_require_saas", lambda: None), \
         patch.object(user_console, "get_individual_by_email", lambda e: _individual()), \
         patch.object(user_console._auth, "verify_password", lambda h, p: True), \
         patch.object(user_console._ratelimit, "login_locked", lambda e: (False, 0)), \
         patch.object(user_console._ratelimit, "record_login_success", lambda e: None), \
         patch.object(user_console._ratelimit, "record_login_failure", lambda e: None), \
         patch.object(user_console.M, "require_mfa_decision", lambda *a, **k: False), \
         patch.object(user_console._auth, "issue_individual_session", lambda **k: "sess"), \
         patch.object(user_console, "_set_session_cookie", lambda *a, **k: None), \
         patch.object(user_console.db, "connection", lambda: conn):
        body = MagicMock(email="person@example.com", password="pw")
        return user_console.me_login(body, MagicMock(), MagicMock())


def test_revoked_seat_is_refused():
    """THE regression. Before the fix this returned an authenticated session."""
    with pytest.raises(HTTPException) as e:
        _login("revoked")
    assert e.value.status_code == 403


def test_the_refusal_actually_explains_itself():
    """Owner's requirement: a disabled user is TOLD, not silently failed. Someone who cannot
    tell 'wrong password' from 'disabled' files a support ticket for the wrong problem."""
    with pytest.raises(HTTPException) as e:
        _login("revoked")
    detail = e.value.detail.lower()
    assert "disabled" in detail
    assert "administrator" in detail
    # It must NOT masquerade as a credential failure.
    assert "invalid credentials" not in detail


@pytest.mark.parametrize("status", ["suspended", "archived", "locked", "REVOKED"])
def test_any_non_active_seat_is_refused(status):
    """Deny on anything that is not 'active' — a future status must fail CLOSED, not sail past
    an allow-list nobody remembered to extend."""
    with pytest.raises(HTTPException) as e:
        _login(status)
    assert e.value.status_code == 403


def test_active_seat_still_signs_in():
    """The control. A disable gate that also blocks healthy logins is a worse bug."""
    assert _login("active")["status"] == "authenticated"


def test_a_seatless_login_still_works():
    """A standalone/house login with no account_users row predates seat linkage and must keep
    working — fail OPEN on absence, deny only on an EXPLICIT non-active status."""
    assert _login(None)["status"] == "authenticated"


def test_unreadable_control_plane_fails_open_not_shut():
    """A DB blip must not lock every tenant out of their own console. Denying here would turn a
    transient read error into a total outage; the loud log is the compensating control."""
    assert _login(None, raises=RuntimeError("db down"))["status"] == "authenticated"


def test_the_gate_runs_after_the_credential_check():
    """NOT an account-enumeration oracle: an anonymous caller with a WRONG password must get the
    generic 401, never 'that account is disabled' — otherwise the endpoint confirms which
    addresses exist. Only a caller who already proved the password learns the seat is disabled.
    """
    cur = MagicMock()
    cur.fetchone.return_value = ("revoked",)
    conn = MagicMock()
    # db.connection() is used as `with db.connection() as _c, _c.cursor() as _cur:` — BOTH
    # levels are context managers. Without __enter__ returning conn, fetchone() yields a
    # MagicMock that is truthy and never equals "active", so every login 403s and the test
    # "proves" a bug that does not exist. (Dead-mock trap: wire the mock to the REAL shape.)
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(user_console, "_require_saas", lambda: None), \
         patch.object(user_console, "get_individual_by_email", lambda e: _individual()), \
         patch.object(user_console._auth, "verify_password", lambda h, p: False), \
         patch.object(user_console._ratelimit, "login_locked", lambda e: (False, 0)), \
         patch.object(user_console._ratelimit, "record_login_failure", lambda e: None), \
         patch.object(user_console.db, "connection", lambda: conn):
        with pytest.raises(HTTPException) as e:
            user_console.me_login(MagicMock(email="person@example.com", password="wrong"),
                                  MagicMock(), MagicMock())
    assert e.value.status_code == 401
    assert "disabled" not in str(e.value.detail).lower()
