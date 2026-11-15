"""SaaS operator account roster — GET /admin/accounts (server-authoritative list).

The operator console must NOT derive its account list from a browser-local sessionStorage
roster: that misses the migration-131 'default' account, API/curl-created accounts, and
anything made in another browser/session. This suite pins the new endpoint's contract:

  * OPERATOR (key) can list EVERY account (control-plane fields + live seat counts, no secrets).
  * a plain ACCOUNT key is NOT an operator → 403 (a tenant can never enumerate the whole SaaS).
  * no credential → 401; SaaS off → 404 (router guarded).
  * accounts.list_accounts() is fail-safe: [] on any DB error (never a wrong/partial roster).
  * the route is actually mounted.

Run: python3 -m pytest tests/test_saas_operator_account_roster.py -q   (tests/ gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin


OP_KEY = "op-super-secret-key"

_ROSTER = [
    {"account_id": "4b266de7-4171-4acd-955b-31df5a53873f", "name": "default",
     "slug": "default", "plan": "legacy", "status": "active", "account_type": "standard",
     "seat_limit": 100, "seats_used": 3, "created_at": "2026-07-05T20:23:20+00:00"},
    {"account_id": "044b9d9c-ec55-43db-a9e6-f751712d504f", "name": "HelpDeskPro",
     "slug": "helpdeskpro", "plan": "standard", "status": "active", "account_type": "standard",
     "seat_limit": 7, "seats_used": 1, "created_at": "2026-07-05T20:56:57+00:00"},
]


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "operator-account-roster-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


# ── 1. operator lists every account ─────────────────────────────────────────────────

def test_operator_lists_all_accounts(client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "list_accounts", lambda: _ROSTER)
    r = client.get("/admin/accounts", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("accounts"), list)
    names = [a["name"] for a in body["accounts"]]
    assert names == ["default", "HelpDeskPro"]  # order preserved (created_at asc)
    # seat counts + control-plane fields surfaced; NO secret/key fields leak.
    d = body["accounts"][0]
    assert d["seats_used"] == 3 and d["seat_limit"] == 100
    assert "key_hash" not in d and "keys" not in d


def test_empty_roster_is_ok(client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "list_accounts", lambda: [])
    r = client.get("/admin/accounts", headers=_op())
    assert r.status_code == 200
    assert r.json() == {"accounts": []}


# ── 2. NOT an operator → 403 (a tenant can never enumerate the whole SaaS) ───────────

def test_account_key_cannot_list_all_accounts(client, monkeypatch):
    # A plain account key is not the operator key → require_operator denies (403) and the
    # roster reader is NEVER reached.
    called = {"n": 0}
    def _poison():
        called["n"] += 1
        return _ROSTER
    monkeypatch.setattr(saas_admin._accounts, "list_accounts", _poison)
    r = client.get("/admin/accounts", headers={"Authorization": "Bearer flk_sometenant_key"})
    assert r.status_code == 403
    assert called["n"] == 0  # never read the roster


def test_no_credential_denied(client):
    # require_operator is a uniform manual gate → 403 "operator credential required" for a
    # missing OR non-operator credential (not the 401 Bearer-scheme require_account uses).
    r = client.get("/admin/accounts")
    assert r.status_code == 403


# ── 3. SaaS off → router guarded (404) ──────────────────────────────────────────────

def test_saas_off_is_404(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    app = FastAPI()
    app.include_router(saas_admin.router)
    c = TestClient(app)
    r = c.get("/admin/accounts", headers=_op())
    assert r.status_code == 404


# ── 4. list_accounts() is fail-safe on a control-plane error ─────────────────────────

def test_list_accounts_fails_safe_to_empty(monkeypatch):
    class _Boom:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(saas_accounts.db, "connection", lambda: _Boom())
    assert saas_accounts.list_accounts() == []


# ── 5. the route is mounted ─────────────────────────────────────────────────────────

def test_route_present():
    paths = {r.path for r in saas_admin.router.routes}
    assert "/admin/accounts" in paths


# ── 6. hard-delete (Trash → permanent) — operator, slug-confirm, archived-first ─────

def test_delete_account_success(client, monkeypatch):
    seen = {}
    def _del(account_id, confirm_slug, *, is_operator, purge_memory=False):
        seen.update(account_id=account_id, slug=confirm_slug, op=is_operator, purge=purge_memory)
        return {"ok": True, "status_code": 200, "seats_freed": 3, "memory_purged": 0,
                "purged_users": [], "reason": "account deleted"}
    monkeypatch.setattr(saas_admin._accounts, "delete_account", _del)
    r = client.request("DELETE", "/admin/accounts/acct-1", headers=_op(),
                       json={"confirm_slug": "helpdeskpro"})
    assert r.status_code == 200
    assert r.json()["seats_freed"] == 3
    # purge_memory defaults to False when omitted (memory kept).
    assert seen == {"account_id": "acct-1", "slug": "helpdeskpro", "op": True, "purge": False}


def test_delete_account_purge_memory_flows_through(client, monkeypatch):
    seen = {}
    def _del(account_id, confirm_slug, *, is_operator, purge_memory=False):
        seen["purge"] = purge_memory
        return {"ok": True, "status_code": 200, "seats_freed": 2, "memory_purged": 2,
                "purged_users": ["u1", "u2"], "reason": "account deleted + memory erased"}
    monkeypatch.setattr(saas_admin._accounts, "delete_account", _del)
    r = client.request("DELETE", "/admin/accounts/acct-1", headers=_op(),
                       json={"confirm_slug": "helpdeskpro", "purge_memory": True})
    assert r.status_code == 200
    assert seen["purge"] is True
    assert r.json()["memory_purged"] == 2


def test_delete_account_not_archived_is_409(client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "delete_account",
                        lambda a, s, *, is_operator, purge_memory=False: {
                            "ok": False, "status_code": 409, "seats_freed": None,
                            "reason": "account must be in Trash (archived) before permanent delete"})
    r = client.request("DELETE", "/admin/accounts/acct-1", headers=_op(),
                       json={"confirm_slug": "helpdeskpro"})
    assert r.status_code == 409


def test_delete_account_slug_mismatch_is_400(client, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "delete_account",
                        lambda a, s, *, is_operator, purge_memory=False: {
                            "ok": False, "status_code": 400, "seats_freed": None,
                            "reason": "confirm slug does not match"})
    r = client.request("DELETE", "/admin/accounts/acct-1", headers=_op(),
                       json={"confirm_slug": "wrong"})
    assert r.status_code == 400


def test_delete_account_not_operator_denied(client, monkeypatch):
    # A plain account key is not the operator → require_operator 403; the deleter never runs.
    called = {"n": 0}
    def _poison(a, s, *, is_operator):
        called["n"] += 1
        return {"ok": True, "status_code": 200, "seats_freed": 0, "reason": "x"}
    monkeypatch.setattr(saas_admin._accounts, "delete_account", _poison)
    r = client.request("DELETE", "/admin/accounts/acct-1",
                       headers={"Authorization": "Bearer flk_sometenant"},
                       json={"confirm_slug": "helpdeskpro"})
    assert r.status_code == 403
    assert called["n"] == 0


def test_delete_route_present():
    routes = {(r.path, m) for r in saas_admin.router.routes for m in getattr(r, "methods", set())}
    assert ("/admin/accounts/{account_id}", "DELETE") in routes
