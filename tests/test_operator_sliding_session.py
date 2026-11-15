"""Sliding-idle refresh for the OPERATOR session (saas.dashboard_auth.refresh_operator_session_sliding).

Covers the contract that makes the operator logout a SLIDING 1h-idle window instead of an absolute
1h-from-login cap, and the load-bearing guards that keep it from breaking login/logout.
"""
import pytest
from starlette.responses import Response

# The helper fail-closes without a session secret + SAAS mode + SAAS_COOKIE_SECURE; all three are
# the suite-wide baseline set once in tests/conftest.py, so nothing is written to os.environ here
# (a module-level write is process-wide and made import order decide other modules' config).
from saas import dashboard_auth as A

OP_COOKIE = A.OPERATOR_SESSION_COOKIE_NAME


class _Req:
    """Minimal request stub: the helper only touches .cookies and (via cookie_secure, which
    SAAS_COOKIE_SECURE short-circuits) nothing else."""
    def __init__(self, cookies=None):
        self.cookies = cookies or {}
        self.headers = {}


def _set_cookie_names(resp):
    return {sc.split("=", 1)[0].strip() for sc in resp.headers.getlist("set-cookie")}


def _cleared(header: str) -> bool:
    """True iff this Set-Cookie CLEARS the cookie rather than re-issuing it."""
    low = header.lower()
    return ("max-age=0" in low or 'max-age="0"' in low
            or "expires=thu, 01 jan 1970" in low or header.startswith(OP_COOKIE + "=;"))


def _reissued(resp) -> bool:
    """True iff a LIVE operator cookie was written (a clear does not count).

    ⚠️ NAME-ONLY IS NOT ENOUGH ANY MORE. The refresh now CLEARS the cookie when the principal has
    been revoked, and a clear is also a Set-Cookie under the same name — so
    ``OP_COOKIE in _set_cookie_names(resp)`` is satisfied by a revocation, which is the exact
    opposite of a refresh. Every "was it slid" assertion below goes through this instead."""
    return any(h.startswith(OP_COOKIE + "=") and not _cleared(h)
               for h in resp.headers.getlist("set-cookie"))


@pytest.fixture
def live_principal(monkeypatch):
    """The signed token's principal still EXISTS and is ACTIVE in the control plane.

    The refresh now re-validates the principal against ``public.dashboard_users`` before
    extending anything (deleting an operator used to leave their session sliding forever). This
    suite is a pure-unit suite with no control plane and synthetic ids like ``op-1``, so the
    lookup is stubbed to the LIVE answer — the same way the suite already stubs everything else
    DB-shaped. The REVOKED answer is asserted explicitly below, and end-to-end against real rows
    in tests/test_operator_session_revocation_and_perm_grant.py."""
    monkeypatch.setattr(A, "operator_principal_active", lambda uid: True)
    yield


def test_valid_session_is_reissued(live_principal):
    tok = A.issue_operator_session(dashboard_user_id="op-1", username="root")
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    assert _reissued(resp), "a valid operator session must be re-issued (sliding refresh)"


def test_a_revoked_principal_is_not_slid_forward_and_the_cookie_is_cleared(monkeypatch):
    """The defect this guard exists for: sliding refresh re-issued on every request, so a
    DELETED operator who kept clicking never reached an expiry. Revocation must land on the next
    request, not at natural expiry."""
    monkeypatch.setattr(A, "operator_principal_active", lambda uid: False)
    tok = A.issue_operator_session(dashboard_user_id="op-gone", username="root")
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    assert not _reissued(resp), "the refresh EXTENDED a revoked operator session"
    assert any(_cleared(h) for h in resp.headers.getlist("set-cookie")), \
        "a revoked session's stale cookie should be cleared"


def test_an_unreadable_control_plane_does_not_slide_the_session(monkeypatch):
    """FAIL CLOSED on an auth path — an unresolvable principal is never admitted. (The real
    ``operator_principal_active`` swallows the error and returns False; this pins that the
    refresh honours that answer rather than treating 'unknown' as 'fine'.)"""
    def _boom(uid):
        raise RuntimeError("control plane unreachable")
    monkeypatch.setattr(A, "operator_principal_active", _boom)
    tok = A.issue_operator_session(dashboard_user_id="op-1", username="root")
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    assert not _reissued(resp)


def test_reissued_token_still_verifies_as_operator(live_principal):
    from http.cookies import SimpleCookie
    tok = A.issue_operator_session(dashboard_user_id="op-42", username="admin")
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    # Parse the Set-Cookie header properly: http.cookies quotes+escapes the '=' padding in the
    # itsdangerous token, and SimpleCookie un-quotes it back exactly as the browser does.
    sc = [h for h in resp.headers.getlist("set-cookie") if h.startswith(OP_COOKIE + "=")][0]
    fresh = SimpleCookie(sc)[OP_COOKIE].value
    payload = A.verify_operator_session(fresh)
    assert payload and payload["sub"] == "op-42" and payload["role"] == "operator"


def test_no_cookie_is_noop():
    resp = Response()
    A.refresh_operator_session_sliding(_Req({}), resp)
    assert OP_COOKIE not in _set_cookie_names(resp)


def test_invalid_token_is_noop():
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: "garbage.not.a.token"}), resp)
    assert OP_COOKIE not in _set_cookie_names(resp)


def test_logout_guard_handler_already_cleared_cookie_is_not_overridden(live_principal):
    """THE load-bearing guard: if the handler (logout) already emitted a Set-Cookie for the
    operator cookie, the sliding refresh MUST defer — otherwise it re-sets a fresh session right
    after logout cleared it and the operator can never log out."""
    tok = A.issue_operator_session(dashboard_user_id="op-1", username="root")
    resp = Response()
    resp.delete_cookie(OP_COOKIE, path="/")  # simulate the logout handler clearing it
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    # Exactly ONE set-cookie for the operator name — the logout's deletion, NOT a re-issue.
    op_cookies = [h for h in resp.headers.getlist("set-cookie") if h.startswith(OP_COOKIE + "=")]
    assert len(op_cookies) == 1
    # The deletion sets an empty value + an expiry in the past — assert it wasn't replaced.
    assert op_cookies[0].startswith(OP_COOKIE + "=;") or "Max-Age=0" in op_cookies[0] \
        or "max-age=0" in op_cookies[0] or "expires=Thu, 01 Jan 1970" in op_cookies[0].lower()


def test_login_guard_handler_already_set_cookie_is_not_double_issued(live_principal):
    """If the handler (login) already set the operator cookie, the refresh defers (no duplicate)."""
    tok = A.issue_operator_session(dashboard_user_id="op-1", username="root")
    resp = Response()
    resp.set_cookie(OP_COOKIE, tok, path="/")  # simulate the login handler setting it
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    op_cookies = [h for h in resp.headers.getlist("set-cookie") if h.startswith(OP_COOKIE + "=")]
    assert len(op_cookies) == 1, "refresh must not double-set a cookie the handler already set"


def test_saas_mode_off_is_noop(monkeypatch):
    monkeypatch.setattr(A, "saas_mode_enabled", lambda: False)
    tok = A.issue_operator_session(dashboard_user_id="op-1", username="root")
    resp = Response()
    A.refresh_operator_session_sliding(_Req({OP_COOKIE: tok}), resp)
    assert OP_COOKIE not in _set_cookie_names(resp)
