"""FIXAUTH — two CONFIRMED pre-existing operator-realm auth defects, proved against a REAL DB.

Everything here drives the REAL routes with a REAL signed cookie against REAL
``public.dashboard_users`` rows. NOTHING about the control plane is monkeypatched — a green
suite over dict fixtures is exactly how both defects survived: a fixture cannot notice that a
row was deleted, and a mocked perms resolver cannot notice that its gate admits its own subject.

FIX 1 — DELETING AN OPERATOR DID NOT END THEIR SESSION.
  ``verify_operator_session`` checks a SIGNATURE and an AGE and nothing else, and the sliding
  refresh re-issued the token on every request, so a deleted global operator (``account_id IS
  NULL`` ⇒ reach into EVERY tenant) who simply kept clicking held access indefinitely. Deletion
  is the control an owner reaches for when someone leaves or is compromised.
  Primary source for the contract being asserted:
    * RFC 6265 §4.1.2.2 — a cookie's Max-Age/Expires is advisory; the server cannot rely on the
      user agent honouring it (and, symmetrically, on it ceasing to present the cookie).
    * OWASP Session Management Cheat Sheet, "Session Expiration" — the session must be closed
      SERVER-side; administrative termination must invalidate it on the server, not wait out a
      client-side clock.
  Asserted here: revocation is effective on the NEXT REQUEST, for deletion AND for a status
  flip away from 'active'.

FIX 2 — AN OPERATOR COULD SELF-GRANT EVERY BILLING PERMISSION.
  ``PUT /admin/billing/operators/{id}/perms`` gated on ``is_operator_superadmin``, which EVERY
  operator row sets True (it is a realm discriminator — "global operator, not tenant admin" —
  not a privilege tier). The four migration-240 permissions restrict exactly that population, so
  the gate admitted its own subject. Re-gated to mirror the shipped tenant realm
  (``PUT /tenant/billing-access``: owner-only, no self-grant): PLATFORM OWNER (earliest-created
  operator login) or the break-glass KEY, and no self-grant for any session principal.

Run:
  python3 tools/fltest.py --bug FIXAUTH \
      --test tests/test_operator_session_revocation_and_perm_grant.py
DB lane: pass POSTGRES_DSN (e.g. postgresql://faultline:faultline@172.20.0.3:5432/
faultline_fixauth_test, migrations >= 131). Without it the ``_DB_RAN`` tripwire at the bottom
turns the run RED rather than letting it look comfortably green.
"""

import os
import uuid

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from saas import admin as _admin
from saas import billing_dashboard_routes as bdr
from saas import dashboard_auth as _auth
from saas import operator_console as _opc


OP_KEY = "fixauth-break-glass-operator-key"
DSN = os.environ.get("POSTGRES_DSN")
_DB_RAN = {"n": 0}

# Every DB-backed case is counted so a silent skip cannot pass for a pass. Set to the ACTUAL
# count, never a guess — a low threshold is a tripwire that does not trip.
_EXPECTED_DB_CASES = 18


def _need_db():
    if not DSN:
        pytest.skip("POSTGRES_DSN not set — control-plane lane skipped")
    _DB_RAN["n"] += 1


@pytest.fixture(scope="module", autouse=True)
def _leave_the_operator_realm_empty():
    """Clean up AFTER the module, not only before each test.

    This suite deliberately mints real operator rows. Leaving them behind makes
    ``test_saas_auto_seat_and_operator::test_operator_bootstrap_from_key_creates_login`` 409 on a
    shared throwaway DB — a cross-suite artifact that reads exactly like a product regression."""
    yield
    if DSN:
        try:
            _sql("DELETE FROM public.dashboard_users "
                 "WHERE role = 'operator' AND account_id IS NULL")
        except Exception:
            pass


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "fixauth-test-session-secret-0123456789")
    yield


@pytest.fixture
def app_client(saas_on):
    """A minimal app carrying BOTH gated realms plus the real sliding-refresh middleware."""
    app = FastAPI()
    app.include_router(bdr.router)
    app.include_router(_opc.router)

    @app.get("/probe/operator-gated")
    def _probe(_op: bool = _admin.Depends(_admin.require_operator)):  # noqa: B008
        return {"ok": True}

    @app.middleware("http")
    async def _slide(request, call_next):  # noqa: ANN001, ANN201
        response = await call_next(request)
        _auth.refresh_operator_session_sliding(request, response)
        return response

    return TestClient(app)


# ── real control-plane row helpers (no mocks) ──────────────────────────────────────

def _sql(query, params=None, fetch=None):
    # params stays None (never ``()``) when there is nothing to bind: psycopg2 only skips
    # placeholder interpolation for None, so a literal '%' in a LIKE would otherwise raise
    # "IndexError: tuple index out of range".
    from saas import db
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchone() if fetch == "one" else (
                cur.fetchall() if fetch == "all" else None)
        conn.commit()
    return out


def _mint_operator(*, tag: str, created_at_sql: str = "NOW()") -> str:
    """INSERT a real global-operator login (role='operator', account_id IS NULL)."""
    username = f"fixauth-{tag}-{uuid.uuid4().hex[:10]}@example.test"
    row = _sql(
        "INSERT INTO public.dashboard_users "
        "  (account_id, username, password_hash, role, status, created_at) "
        f"VALUES (NULL, %s, %s, 'operator', 'active', {created_at_sql}) "
        "RETURNING id::text",
        (username, "argon2-placeholder-not-used-on-these-paths"), fetch="one")
    return row[0]


def _wipe_operators():
    """Clear the operator realm so 'earliest-created' is deterministic for this test."""
    _sql("DELETE FROM public.dashboard_users "
         "WHERE role = 'operator' AND account_id IS NULL AND username LIKE 'fixauth-%'")
    _sql("DELETE FROM public.dashboard_users "
         "WHERE role = 'operator' AND account_id IS NULL")


def _cookie_for(client, operator_id: str) -> None:
    # CLEAR FIRST. The sliding-refresh middleware emits its own Set-Cookie (domain=testserver,
    # path=/) which the jar keeps ALONGSIDE a manually-set cookie (domain=""), so switching
    # principals without clearing sends TWO fl_op_session cookies and the server reads the
    # stale one — a harness bug that reads exactly like a product bug.
    client.cookies.clear()
    client.cookies.set(_auth.OPERATOR_SESSION_COOKIE_NAME,
                       _auth.issue_operator_session(dashboard_user_id=operator_id,
                                                    username="fixauth-op"))


def _csrf(operator_id: str) -> dict:
    return {"x-csrf-token": _auth.issue_operator_csrf(dashboard_user_id=operator_id)}


def _perm(operator_id: str, field: str) -> bool:
    return bool(_sql(f"SELECT {field} FROM public.dashboard_users WHERE id = %s",
                     (operator_id,), fetch="one")[0])


# ══════════════════════════════════════════════════════════════════════════════════
# FIX 1 — revocation is effective on the NEXT REQUEST
# ══════════════════════════════════════════════════════════════════════════════════

def test_a_deleted_operators_live_session_is_refused_on_the_very_next_request(app_client):
    """THE headline. Same cookie, same route, one DELETE in between."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="doomed")
    _cookie_for(app_client, op)

    before = app_client.get("/probe/operator-gated")
    assert before.status_code == 200, f"live operator should be admitted, got {before.text}"

    _sql("DELETE FROM public.dashboard_users WHERE id = %s", (op,))

    after = app_client.get("/probe/operator-gated")
    assert after.status_code == 403, (
        "a DELETED operator kept global access — revocation is not effective on the next "
        f"request (got {after.status_code}: {after.text})")


def test_a_suspended_operators_live_session_is_refused_on_the_very_next_request(app_client):
    """The status arm: 'active' is the only status that may act."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="suspended")
    _cookie_for(app_client, op)
    assert app_client.get("/probe/operator-gated").status_code == 200

    _sql("UPDATE public.dashboard_users SET status = 'suspended' WHERE id = %s", (op,))

    assert app_client.get("/probe/operator-gated").status_code == 403, \
        "a SUSPENDED operator kept global access"


def test_the_operator_console_realm_refuses_the_revoked_principal_too(app_client):
    """A guard on one of two doors leaves it live. ``require_operator_session`` (operator
    console) is a SEPARATE dependency from ``require_operator`` (admin/billing) — both must
    re-validate, or the console remains reachable after deletion."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="console")
    _cookie_for(app_client, op)
    assert app_client.get("/operator/me").status_code == 200

    _sql("DELETE FROM public.dashboard_users WHERE id = %s", (op,))

    resp = app_client.get("/operator/me")
    assert resp.status_code == 401, f"operator console admitted a deleted principal: {resp.text}"
    assert "revoked" in resp.text.lower()


def test_the_billing_read_view_is_refused_to_a_revoked_operator(app_client):
    """The perms resolver used to fall through to a read-only billing view when the row did not
    resolve, on the premise that a TENANT admin lands there. Unreachable: getting that far
    requires an OPERATOR-salt token. What actually landed there was a revoked operator, and it
    was handed customer/subscription/dunning data."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="billingread")
    _cookie_for(app_client, op)
    assert app_client.get("/admin/billing/whoami").status_code == 200

    _sql("DELETE FROM public.dashboard_users WHERE id = %s", (op,))

    assert app_client.get("/admin/billing/whoami").status_code == 403


def test_the_perms_resolver_itself_refuses_a_revoked_operator_not_just_the_route(saas_on):
    """⚠️ COUNT THE GUARDS. The route-level test above passes even with the resolver's fallback
    restored, because ``require_operator`` refuses first — it proves the GATE, not the resolver.
    This drives ``resolve_operator_perms`` DIRECTLY, which is the seam that used to hand a
    revoked operator ``can_view_billing: True``, so a partial repair cannot hide behind the
    outer gate."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="resolver")
    token = _auth.issue_operator_session(dashboard_user_id=op, username="fixauth-op")
    scope = {"type": "http", "method": "GET", "path": "/", "headers":
             [(b"cookie", f"{_auth.OPERATOR_SESSION_COOKIE_NAME}={token}".encode())],
             "scheme": "http", "server": ("test", 80), "query_string": b""}

    live = bdr.resolve_operator_perms(Request(scope), None)
    assert live["can_view_billing"] is True and live["is_operator_superadmin"] is True

    _sql("DELETE FROM public.dashboard_users WHERE id = %s", (op,))

    with pytest.raises(Exception) as excinfo:
        bdr.resolve_operator_perms(Request(scope), None)
    assert getattr(excinfo.value, "status_code", None) == 403, (
        "the perms resolver still returns a permission set for a DELETED operator — it must "
        f"deny, got {excinfo.value!r}")


def test_the_sliding_refresh_does_not_extend_a_revoked_session(saas_on):
    """Sliding refresh is what turned a bounded window into unbounded access. It must not
    re-issue for a principal that no longer resolves — and it clears the stale cookie."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="slider")
    token = _auth.issue_operator_session(dashboard_user_id=op, username="fixauth-op")

    def _req():
        scope = {"type": "http", "method": "GET", "path": "/", "headers":
                 [(b"cookie", f"{_auth.OPERATOR_SESSION_COOKIE_NAME}={token}".encode())],
                 "scheme": "http", "server": ("test", 80), "query_string": b""}
        return Request(scope)

    live_resp = Response()
    _auth.refresh_operator_session_sliding(_req(), live_resp)
    live_cookies = live_resp.headers.getlist("set-cookie")
    assert any(_auth.OPERATOR_SESSION_COOKIE_NAME in c and "Max-Age=0" not in c
               for c in live_cookies), "a LIVE operator must still get a slid cookie"

    _sql("DELETE FROM public.dashboard_users WHERE id = %s", (op,))

    dead_resp = Response()
    _auth.refresh_operator_session_sliding(_req(), dead_resp)
    dead_cookies = dead_resp.headers.getlist("set-cookie")
    assert not any(_auth.OPERATOR_SESSION_COOKIE_NAME in c and "Max-Age=0" not in c
                   and 'Max-Age="0"' not in c and "expires=Thu, 01 Jan 1970" not in c.lower()
                   for c in dead_cookies), \
        f"the refresh EXTENDED a revoked session: {dead_cookies}"


def test_the_break_glass_key_is_unaffected_by_the_revalidation(app_client):
    """The KEY has no dashboard_users row and must remain the recovery path — including during
    a control-plane outage, which is precisely when the re-validation fails closed."""
    _need_db()
    _wipe_operators()
    resp = app_client.get("/probe/operator-gated",
                          headers={"Authorization": f"Bearer {OP_KEY}"})
    assert resp.status_code == 200


def test_an_unreadable_control_plane_denies_rather_than_admits(app_client, monkeypatch):
    """FAIL CLOSED on an auth path: an unresolvable principal is denied, never admitted."""
    _need_db()
    _wipe_operators()
    op = _mint_operator(tag="failclosed")
    _cookie_for(app_client, op)
    assert app_client.get("/probe/operator-gated").status_code == 200

    def _boom():
        raise RuntimeError("control plane unreachable")
    monkeypatch.setattr(_auth.db, "connection", _boom)

    assert app_client.get("/probe/operator-gated").status_code == 403


# ══════════════════════════════════════════════════════════════════════════════════
# FIX 2 — the permission gate may not be opened by its own subject
# ══════════════════════════════════════════════════════════════════════════════════

_PERMS = ("can_issue_refunds", "can_manage_branding",
          "can_manage_email_controls", "can_register_webhook")


def _two_operators():
    """(owner, rank_and_file) — owner is the EARLIEST-created operator login."""
    _wipe_operators()
    owner = _mint_operator(tag="owner", created_at_sql="NOW() - INTERVAL '10 days'")
    other = _mint_operator(tag="rank", created_at_sql="NOW()")
    return owner, other


@pytest.mark.parametrize("field", _PERMS)
def test_a_rank_and_file_operator_cannot_self_grant_any_billing_permission(app_client, field):
    """THE defect, one case per permission column."""
    _need_db()
    _owner, other = _two_operators()
    _cookie_for(app_client, other)

    resp = app_client.put(f"/admin/billing/operators/{other}/perms",
                          json={field: True}, headers=_csrf(other))
    assert resp.status_code == 403, (
        f"an operator self-granted {field} (status {resp.status_code}) — the gate admits its "
        "own subject")
    assert _perm(other, field) is False, f"{field} was written despite the refusal"


def test_a_rank_and_file_operator_cannot_grant_another_operator_either(app_client):
    """Not merely self-grant: the whole rank-and-file tier is below the grant."""
    _need_db()
    owner, other = _two_operators()
    _cookie_for(app_client, other)
    resp = app_client.put(f"/admin/billing/operators/{owner}/perms",
                          json={"can_issue_refunds": True}, headers=_csrf(other))
    assert resp.status_code == 403
    assert _perm(owner, "can_issue_refunds") is False


def test_the_platform_owner_may_grant_another_operator(app_client):
    """The legitimate admin flow is PRESERVED — this is a re-gating, not a removal."""
    _need_db()
    owner, other = _two_operators()
    _cookie_for(app_client, owner)
    resp = app_client.put(f"/admin/billing/operators/{other}/perms",
                          json={"can_issue_refunds": True, "can_manage_branding": True},
                          headers=_csrf(owner))
    assert resp.status_code == 200, resp.text
    assert _perm(other, "can_issue_refunds") is True
    assert _perm(other, "can_manage_branding") is True


def test_even_the_platform_owner_may_not_self_grant(app_client):
    """CHOSEN RULE, stated: self-grant is refused for EVERY session principal, owner included.
    The owner outranks OTHER operators, not their own restriction; a self-grant would put the
    granting tier back inside the gate it guards."""
    _need_db()
    owner, _other = _two_operators()
    _cookie_for(app_client, owner)
    resp = app_client.put(f"/admin/billing/operators/{owner}/perms",
                          json={"can_register_webhook": True}, headers=_csrf(owner))
    assert resp.status_code == 403, resp.text
    assert _perm(owner, "can_register_webhook") is False


def test_self_revoke_is_allowed_because_de_escalation_is_not_escalation(app_client):
    """Dropping your OWN privilege is never a privilege escalation, and refusing it would trap
    an operator inside a grant they want gone."""
    _need_db()
    owner, _other = _two_operators()
    _sql("UPDATE public.dashboard_users SET can_issue_refunds = true WHERE id = %s", (owner,))
    _cookie_for(app_client, owner)
    resp = app_client.put(f"/admin/billing/operators/{owner}/perms",
                          json={"can_issue_refunds": False}, headers=_csrf(owner))
    assert resp.status_code == 200, resp.text
    assert _perm(owner, "can_issue_refunds") is False


def test_the_break_glass_key_can_grant_the_platform_owner_out_of_the_deadlock(app_client):
    """CANARY #1 (deadlock check): a platform owner minted after migration 240 defaults FALSE on
    all four and cannot self-grant. The KEY is the named recovery path, and it is not a login —
    it has no self to grant to."""
    _need_db()
    owner, _other = _two_operators()
    resp = app_client.put(f"/admin/billing/operators/{owner}/perms",
                          json={"can_issue_refunds": True},
                          headers={"Authorization": f"Bearer {OP_KEY}"})
    assert resp.status_code == 200, resp.text
    assert _perm(owner, "can_issue_refunds") is True


def test_whoami_carries_the_grant_affordance_so_no_control_is_rendered_that_will_403(app_client):
    """CANARY #2 (affordance parity): one boolean the console can render the grant control
    against, so it is never shown to an operator who would be refused for pressing it."""
    _need_db()
    owner, other = _two_operators()

    _cookie_for(app_client, owner)
    owner_who = app_client.get("/admin/billing/whoami").json()
    assert owner_who["is_platform_owner"] is True
    assert owner_who["can_grant_operator_perms"] is True

    _cookie_for(app_client, other)
    other_who = app_client.get("/admin/billing/whoami").json()
    assert other_who["can_grant_operator_perms"] is False
    # The old, misread flag is STILL True for a rank-and-file operator. That is the whole point:
    # it is a realm discriminator, and gating a grant on it is what opened the hole.
    assert other_who["is_operator_superadmin"] is True


def test_zzz_db_lane_actually_ran():
    """Tripwire — a silent skip must turn the run RED, not look comfortably green."""
    if not DSN:
        pytest.fail("POSTGRES_DSN was not set: the ENTIRE control-plane lane silently skipped. "
                    "Pass a throwaway DSN (migrations >= 131).")
    assert _DB_RAN["n"] >= _EXPECTED_DB_CASES, (
        f"only {_DB_RAN['n']} of {_EXPECTED_DB_CASES} DB-backed cases ran")
