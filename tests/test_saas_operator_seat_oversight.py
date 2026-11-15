"""Operator-scoped per-tenant SEAT oversight — the operator console "Users" section.

Covers the two endpoints added to ``saas.admin`` for the operator console:

  * GET    /admin/accounts/{account_id}/users        — list that account's seats
  * DELETE /admin/accounts/{account_id}/users/{uid}  — deprovision one seat

Auth contract (require_account → _authorize_account_path): the OPERATOR (key or session)
may target ANY account_id in the path; an ACCOUNT KEY may target ONLY its own account — a
mismatching path account_id is 403 and the control-plane function is NEVER reached (BOLA).
A seat that isn't an active seat of the account → 404. Fail-CLOSED throughout; SAAS-off → 404.

These are DISTINCT from the account-key ``GET/DELETE /admin/users`` surface (which resolves
the account from the KEY and 400s the operator sentinel) — that tenant path stays untouched.

Run: python3 -m pytest tests/test_saas_operator_seat_oversight.py -q  (tests/ gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import seats as saas_seats


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UA = "11111111-1111-4111-8111-111111111111"
UB = "22222222-2222-4222-8222-222222222222"
OP_KEY = "op-super-secret-key"

_SEATS_A = [
    {"user_id": UA, "status": "active", "created_at": "2026-06-01T09:00:00",
     "label": "networking agent", "ingest_enabled": True},
    {"user_id": UB, "status": "active", "created_at": "2026-06-02T09:00:00",
     "label": None, "ingest_enabled": False},
]


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "operator-seat-oversight-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def _acct_a_key():
    return {"Authorization": "Bearer flk_acctA_key"}


# ════════════════════════════════════════════════════════════════════════════════
# 1. operator lists ANY account's seats
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_lists_account_seats(client, monkeypatch):
    seen = {}

    def _list(account_id, *, include_upstream_identity=False):
        seen["account_id"] = account_id
        # BUG-06: the operator realm opts IN to the upstream-identity (PII) columns.
        seen["upstream"] = include_upstream_identity
        return list(_SEATS_A)
    monkeypatch.setattr(saas_admin._seats, "list_seats", _list)
    # BUG-06 fold: the login map supplies the individual_id the moderation verbs address.
    import saas.user_console as _uc
    monkeypatch.setattr(_uc, "login_map_for_account",
                        lambda aid: {UA: {"has_login": True, "individual_id": "ind-A1",
                                          "email": "seat-a@example.com", "status": "active",
                                          "mfa_enabled": False}})

    r = client.get(f"/admin/accounts/{ACCT_A}/users", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert seen["account_id"] == ACCT_A
    assert body["account_id"] == ACCT_A
    assert [u["user_id"] for u in body["users"]] == [UA, UB]
    # ingest state + label are carried through (read-only oversight fields).
    assert body["users"][0]["label"] == "networking agent"
    assert body["users"][1]["ingest_enabled"] is False
    # BUG-06 scope extension: the operator oversight read opts IN to the PII columns and
    # folds the moderation individual_id onto every seat with a login.
    assert seen["upstream"] is True
    assert body["users"][0]["login_individual_id"] == "ind-A1"


def test_operator_can_query_a_different_account(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(saas_admin._seats, "list_seats",
                        lambda a, *, include_upstream_identity=False: seen.__setitem__("account_id", a) or [])
    r = client.get(f"/admin/accounts/{ACCT_B}/users", headers=_op())
    assert r.status_code == 200
    assert seen["account_id"] == ACCT_B          # operator may target ANY account


def test_list_control_plane_error_is_500(client, monkeypatch):
    monkeypatch.setattr(saas_admin._seats, "list_seats", lambda a, *, include_upstream_identity=False: None)
    r = client.get(f"/admin/accounts/{ACCT_A}/users", headers=_op())
    assert r.status_code == 500


# ════════════════════════════════════════════════════════════════════════════════
# 2. operator deprovisions a seat
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_deprovisions_seat(client, monkeypatch):
    seen = {}

    def _revoke(account_id, user_id):
        seen["args"] = (account_id, user_id)
        return True
    monkeypatch.setattr(saas_admin._seats, "revoke_seat", _revoke)

    r = client.delete(f"/admin/accounts/{ACCT_A}/users/{UA}", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert seen["args"] == (ACCT_A, UA)
    assert body["status"] == "deprovisioned"
    assert body["account_id"] == ACCT_A and body["user_id"] == UA


def test_deprovision_seat_not_in_account_is_404(client, monkeypatch):
    # revoke_seat scopes by account_id AND user_id AND status='active' → a foreign/absent
    # seat never matches → False → 404.
    monkeypatch.setattr(saas_admin._seats, "revoke_seat", lambda a, u: False)
    r = client.delete(f"/admin/accounts/{ACCT_A}/users/{UB}", headers=_op())
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════════════════
# 3. cross-account safety (BOLA) — account key may target ONLY its own account
# ════════════════════════════════════════════════════════════════════════════════

def test_account_key_own_account_list_ok(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    seen = {}
    monkeypatch.setattr(saas_admin._seats, "list_seats",
                        lambda a, *, include_upstream_identity=False: (seen.__setitem__("account_id", a), seen.__setitem__("upstream", include_upstream_identity)) and list(_SEATS_A))
    r = client.get(f"/admin/accounts/{ACCT_A}/users", headers=_acct_a_key())
    assert r.status_code == 200
    assert seen["account_id"] == ACCT_A
    # BUG-06: an ACCOUNT key through the same route keeps the PII default - the upstream
    # identity columns are the operator realm's read, not the tenant's.
    assert seen["upstream"] is False


def test_account_key_foreign_account_list_is_403_and_never_reads(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    called = {"hit": False}
    monkeypatch.setattr(saas_admin._seats, "list_seats",
                        lambda a, *, include_upstream_identity=False: called.__setitem__("hit", True) or [])
    # ACCT_A's key names ACCT_B in the path → 403, control-plane never touched.
    r = client.get(f"/admin/accounts/{ACCT_B}/users", headers=_acct_a_key())
    assert r.status_code == 403
    assert called["hit"] is False


def test_account_key_foreign_account_deprovision_is_403_and_never_writes(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    called = {"hit": False}
    monkeypatch.setattr(saas_admin._seats, "revoke_seat",
                        lambda a, u: called.__setitem__("hit", True) or True)
    r = client.delete(f"/admin/accounts/{ACCT_B}/users/{UB}", headers=_acct_a_key())
    assert r.status_code == 403
    assert called["hit"] is False              # no cross-account write


def test_account_key_own_account_deprovision_ok(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    seen = {}
    monkeypatch.setattr(saas_admin._seats, "revoke_seat",
                        lambda a, u: seen.__setitem__("args", (a, u)) or True)
    r = client.delete(f"/admin/accounts/{ACCT_A}/users/{UA}", headers=_acct_a_key())
    assert r.status_code == 200
    assert seen["args"] == (ACCT_A, UA)


def test_unresolvable_key_is_401(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: None)
    r = client.get(f"/admin/accounts/{ACCT_A}/users",
                   headers={"Authorization": "Bearer flk_bogus"})
    assert r.status_code == 401


def test_no_credential_is_401(client):
    r = client.get(f"/admin/accounts/{ACCT_A}/users")
    assert r.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════
# 3b. operator ROTATES a seat's connection key (POST .../users/{uid}/rotate-token)
#     Hard cutover: revoke the seat's live token(s) → mint a fresh one (shown ONCE).
#     Same authority contract as list/deprovision + an active-seat BOLA gate + SOC audit.
# ════════════════════════════════════════════════════════════════════════════════

def _minted():
    return {"token": "flu_new_secret_shown_once", "token_id": "tok-1",
            "token_prefix": "flu_newpf"}


def _wire_rotate(monkeypatch, *, active=True, minted=True, revoked=2):
    calls = {"revoked": None, "minted": None, "audit": []}
    monkeypatch.setattr(saas_admin._seats, "is_active_seat", lambda a, u: active)
    monkeypatch.setattr(saas_admin._tokens, "revoke_all_user_tokens",
                        lambda a, u: calls.__setitem__("revoked", (a, u)) or revoked)
    monkeypatch.setattr(saas_admin._tokens, "mint_user_token",
                        lambda a, u: (calls.__setitem__("minted", (a, u)) or _minted()) if minted else None)
    monkeypatch.setattr(saas_admin._onboarding, "public_mcp_url", lambda: "https://fl.example:8002")
    monkeypatch.setattr(saas_admin._onboarding, "claude_desktop_snippet", lambda m, t, u: "CONFIG")
    monkeypatch.setattr(saas_admin._audit, "record",
                        lambda action, **kw: calls["audit"].append((action, kw)) or True)
    return calls


def test_operator_rotates_seat_token(client, monkeypatch):
    calls = _wire_rotate(monkeypatch)
    r = client.post(f"/admin/accounts/{ACCT_A}/users/{UA}/rotate-token", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rotated"
    assert body["revoked"] == 2 and body["token"] == "flu_new_secret_shown_once"
    # hard cutover order: old tokens revoked THEN a fresh one minted, both scoped to (acct, seat).
    assert calls["revoked"] == (ACCT_A, UA) and calls["minted"] == (ACCT_A, UA)
    # SOC audit ledger recorded a success line for the rotate, scoped to the seat.
    assert calls["audit"] and calls["audit"][0][0] == "seat.token_rotate"
    assert calls["audit"][0][1]["result"] == "success"
    assert calls["audit"][0][1]["target_id"] == UA
    assert calls["audit"][0][1]["actor_type"] == "operator"


def test_rotate_foreign_or_absent_seat_is_404_and_never_mints(client, monkeypatch):
    # seat is NOT an active seat of this account → 404 BEFORE any revoke/mint (BOLA fail-closed).
    calls = _wire_rotate(monkeypatch, active=False)
    r = client.post(f"/admin/accounts/{ACCT_A}/users/{UB}/rotate-token", headers=_op())
    assert r.status_code == 404
    assert calls["revoked"] is None and calls["minted"] is None


def test_rotate_malformed_user_id_is_400(client, monkeypatch):
    _wire_rotate(monkeypatch)
    r = client.post(f"/admin/accounts/{ACCT_A}/users/not-a-uuid/rotate-token", headers=_op())
    assert r.status_code == 400


def test_account_key_foreign_account_rotate_is_403_and_never_touches_tokens(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    calls = _wire_rotate(monkeypatch)
    # ACCT_A's key names ACCT_B in the path → 403 at the BOLA gate, token plumbing never reached.
    r = client.post(f"/admin/accounts/{ACCT_B}/users/{UB}/rotate-token", headers=_acct_a_key())
    assert r.status_code == 403
    assert calls["revoked"] is None and calls["minted"] is None


def test_account_key_own_account_rotate_ok(client, monkeypatch):
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    calls = _wire_rotate(monkeypatch)
    r = client.post(f"/admin/accounts/{ACCT_A}/users/{UA}/rotate-token", headers=_acct_a_key())
    assert r.status_code == 200
    assert calls["minted"] == (ACCT_A, UA)
    # actor classified as the account (not operator) when an account key drives its own rotate.
    assert calls["audit"][0][1]["actor_type"] == "account"


def test_rotate_mint_failure_is_404_and_audits_failure(client, monkeypatch):
    calls = _wire_rotate(monkeypatch, minted=False)
    r = client.post(f"/admin/accounts/{ACCT_A}/users/{UA}/rotate-token", headers=_op())
    assert r.status_code == 404
    # the revoke still happened; the failure is captured on the audit ledger.
    assert calls["revoked"] == (ACCT_A, UA)
    assert calls["audit"] and calls["audit"][0][1]["result"] == "failure"


# ════════════════════════════════════════════════════════════════════════════════
# 4. SAAS-off → 404 (router guarded even if mounted)
# ════════════════════════════════════════════════════════════════════════════════

def test_saas_off_is_404(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    app = FastAPI()
    app.include_router(saas_admin.router)
    c = TestClient(app)
    r = c.get(f"/admin/accounts/{ACCT_A}/users", headers=_op())
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════════════════
# 5. routes are registered
# ════════════════════════════════════════════════════════════════════════════════

def test_routes_present():
    paths = {r.path for r in saas_admin.router.routes}
    assert "/admin/accounts/{account_id}/users" in paths
    assert "/admin/accounts/{account_id}/users/{user_id}" in paths
    assert "/admin/accounts/{account_id}/users/{user_id}/rotate-token" in paths
