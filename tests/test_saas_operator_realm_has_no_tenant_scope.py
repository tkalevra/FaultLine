"""An operator signing in to the TENANT console got a session scoped to the string "None".

MEASURED ON PRE-PROD, reported by the owner as a confusing 503:

    "Your endpoint was accepted, but we could not save it — this is a fault on our side"

    [tenant-console] tenant llm-config SAVE storage failure account=None:
                     upsert_config failed to persist the endpoint (InvalidTextRepresentation)

THE CHAIN. `dashboard_users.account_id` is NULL for the OPERATOR realm by design (the global
super-admin has no tenant). All three tenant-console login paths mint with
`account_id=user["account_id"]`, and `issue_session` did `"acct": str(account_id)` —
`str(None) == "None"`, a **truthy** string. So:

  1. the session signs cleanly with a nonsense account scope;
  2. `require_dashboard_session`'s fail-closed `if not session.get("acct")` does NOT fire,
     because "None" is truthy;
  3. the value rides all the way to a Postgres uuid parameter and dies there;
  4. the user is told the save failed on our side — true, but eight layers from the cause,
     and it reads as "you are not allowed to add this endpoint".

Note the egress allowlist was NEVER involved: SAAS_LLM_ALLOWED_EGRESS=192.168.40.0/25 covers
192.168.40.20, and the message itself says the endpoint was ACCEPTED. Blaming the SSRF guard
would have been the obvious wrong turn.

FIXED AT THREE LAYERS, because each is independently correct:
  - LOGIN  refuse an account-less principal at the tenant door, naming the right console (403);
  - MINT   `issue_session` refuses to sign a session it cannot scope truthfully (ValueError);
  - GUARD  `require_dashboard_session` rejects a non-uuid scope, so cookies ALREADY issued with
           "None" cannot keep working until they expire.
"""
import uuid

import pytest

from saas import dashboard_auth as A

_TEST_SIGNING_SECRET = "unit-test-only-not-a-real-secret-operator-realm-scope"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """Make this module independent of the operator's shell — same reasoning, and the same
    mechanism, as tests/test_saas_disabled_seat_cannot_login.py.

    Every test here signs or verifies a session, so without a secret they raise
    `DashboardAuthError: SAAS_SESSION_SECRET unset` and the file's result depends on who ran it.

    `monkeypatch`, NOT a module-level `os.environ.setdefault`: the value is restored afterwards
    so it cannot leak into `test_saas_tenant_auth.py::test_session_secret_unset_fails_closed`,
    which proves the fail-closed control by DELETING this variable.
    """
    monkeypatch.setenv("SAAS_SESSION_SECRET", _TEST_SIGNING_SECRET)
    # The guard tests call require_dashboard_session, whose first act is `_require_saas()` —
    # 404 "not found" when the flag is off (the public core must not expose a SaaS surface).
    # Set via monkeypatch and NOT process-wide at import: `http_server.py` mounts /admin at
    # IMPORT time gated on this flag, so a module-level set changes what a later import sees
    # and makes results depend on test ORDER.
    monkeypatch.setenv("SAAS_MODE", "1")


# ── MINT ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "   ", "None", "none", "NULL", "null"])
def test_a_session_with_no_account_scope_is_refused(bad):
    """THE root cause. `str(None)` is truthy, so signing it defeats every downstream
    `if not acct` check. Refuse to mint rather than emit an unscoped credential."""
    with pytest.raises(ValueError, match="no account scope"):
        A.issue_session(dashboard_user_id="u-1", account_id=bad, username="chris")


def test_a_real_account_still_mints_and_round_trips():
    """The control. A tenant admin must be unaffected."""
    acct = str(uuid.uuid4())
    token = A.issue_session(dashboard_user_id="u-1", account_id=acct, username="cthompson")
    session = A.verify_session(token)
    assert session["acct"] == acct
    assert session["sub"] == "u-1"


def test_a_uuid_object_is_accepted_not_just_a_string():
    """`account_id` arrives from psycopg2 as a uuid.UUID on some paths and str on others.
    Rejecting the object form would break login for real tenants — a worse bug than the one
    being fixed."""
    acct = uuid.uuid4()
    session = A.verify_session(
        A.issue_session(dashboard_user_id="u-1", account_id=acct, username="x")
    )
    assert session["acct"] == str(acct)


# ── GUARD ─────────────────────────────────────────────────────────────────────────

def _request_with_session(token, method="GET"):
    from unittest.mock import MagicMock
    req = MagicMock()
    req.cookies = {A.SESSION_COOKIE_NAME: token}
    req.method = method
    req.headers = {}
    return req


def test_an_already_issued_none_scoped_cookie_is_rejected_at_the_guard():
    """Defence in depth, and it matters operationally: the mint fix cannot retroactively
    invalidate cookies already signed with "None" — those stay valid until SESSION_TTL expires.
    The guard must refuse them NOW, rather than let them reach a uuid cast.

    Signs the payload directly, bypassing issue_session, precisely because issue_session will
    no longer produce this shape — this reproduces the LEGACY cookie.
    """
    from fastapi import HTTPException

    from saas import tenant_console as T

    legacy = A._sign(A._SALT_SESSION, {"sub": "u-1", "acct": "None", "usr": "chris", "iat": 0})
    assert A.verify_session(legacy)["acct"] == "None", "precondition: the bad cookie verifies"

    with pytest.raises(HTTPException) as e:
        T.require_dashboard_session(_request_with_session(legacy))
    assert e.value.status_code == 401


def test_the_guard_still_admits_a_valid_scoped_session():
    """The control. A guard that also rejects healthy sessions is the worse bug."""
    from saas import tenant_console as T

    acct = str(uuid.uuid4())
    token = A.issue_session(dashboard_user_id="u-1", account_id=acct, username="cthompson")
    assert T.require_dashboard_session(_request_with_session(token)) == acct
