"""Per-tenant SEAT-MANAGEMENT permission (migration 227) — the operator's checkbox.

The owner's requirement: "on the global admin I need the ability to, via checkmark, allow
tenant admins the ability to add/remove seats (incurred cost)."

This is a THIRD lever on seat plumbing, and the suite pins that it stays distinct from the
two that already exist:

  * ``can_view_billing`` (migration 159) gates BUYING capacity  — that is SPEND.
  * ``seat_limit``       (migration 213) is the licensed ceiling — that is a CAP.
  * ``seat_management_enabled`` (migration 227) gates CONSUMING/RELEASING an already-licensed
    seat — that is a PERMISSION, and it is the one the operator now owns per tenant.

Contract pinned here:
  1. flag ON  → all four seat endpoints work exactly as before.
  2. flag OFF → all four 403, fail-CLOSED, BEFORE any side effect, and the denial is audited.
  3. unreadable control plane → 403 too (an outage must not open a provisioning window).
  4. the migration's column default is TRUE (nobody loses an ability they have today).
  5. POST /admin/accounts/{id}/seat-management flips it and is OPERATOR-ONLY (an account key
     → 403; no credential → 403). GET is operator-or-own-account (BOLA-checked).
  6. the flag never reads or implies can_view_billing (a non-billing admin may still add a
     seat when the flag is ON — this change must not quietly re-gate seats behind billing).
  7. GET /tenant/me carries ``can_manage_seats`` so the console can hide dead controls.

The control-plane data functions are monkeypatched, so this file is green without a DB.
Run: python3 -m pytest tests/test_saas_seat_management_flag.py -q  (tests/ gitignored →
git add -f).
"""

import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import accounts as saas_accounts
from saas import admin as saas_admin
from saas import dashboard_auth as auth
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # a seat of ACCT_A
AGENT_A = "33333333-3333-4333-8333-333333333333"  # an AGENT seat of ACCT_A
KEY_A = "flk_aaaaaaaa_secretA"
OP_KEY = "op-super-secret-key"


# ════════════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "seat-management-flag-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


@pytest.fixture
def op_client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _cookies(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A,
                                          username="alice"))


def _csrf():
    return {"x-csrf-token": auth.issue_csrf(dashboard_user_id=USER_A)}


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


@pytest.fixture
def audit(monkeypatch):
    """Capture every audit row both routers write."""
    rows = []

    def _rec(action, **kw):
        rows.append({"action": action, **kw})
        return True

    monkeypatch.setattr(tc._audit, "record", _rec)
    monkeypatch.setattr(saas_admin._audit, "record", _rec)
    return rows


@pytest.fixture
def flag(monkeypatch):
    """Control what accounts.get_seat_management returns. Default: ON (migration default)."""
    state = {"value": {"enabled": True, "changed_at": None, "changed_by": None}}

    def _get(account_id):
        assert account_id == ACCT_A, "the gate must read the SESSION account, never client input"
        return state["value"]

    monkeypatch.setattr(tc._accounts, "get_seat_management", _get)
    return state


@pytest.fixture
def seat_plumbing(monkeypatch):
    """Stub everything the four handlers touch AFTER the gate, so a 200 proves the gate opened
    and a 403 proves nothing downstream ran."""
    calls = {"register": [], "revoke": []}

    def _register(account_id, user_id, seat_type="standard", **kw):
        calls["register"].append((account_id, user_id, seat_type))
        return {"ok": True, "status_code": 200, "reason": None}

    def _revoke(account_id, user_id):
        calls["revoke"].append((account_id, user_id))
        return True

    monkeypatch.setattr(tc._seats, "register_seat", _register)
    monkeypatch.setattr(tc._seats, "revoke_seat", _revoke)
    monkeypatch.setattr(tc._seats, "update_seat", lambda *a, **k: True)
    monkeypatch.setattr(tc._seats, "is_active_seat", lambda a, u: (a, u) == (ACCT_A, USER_A))
    monkeypatch.setattr(tc._seats, "get_seat",
                        lambda a, u: {"seat_type": "agent", "status": "active"}
                        if (a, u) == (ACCT_A, AGENT_A) else None)
    monkeypatch.setattr(tc._tokens, "revoke_all_user_tokens", lambda *a, **k: True)
    monkeypatch.setattr(tc._accounts, "list_agent_seats", lambda a: [])
    monkeypatch.setattr(tc._accounts, "get_agent_seat_count", lambda a: 0)
    monkeypatch.setattr(tc._accounts, "set_agent_back_end", lambda *a, **k: None)
    monkeypatch.setattr(tc._accounts, "_read_agent_settings_raw", lambda a: {})
    monkeypatch.setattr(tc, "_mint_seat_token_payload",
                        lambda a, u: {"user_id": u, "token": "flu_test", "token_prefix": "flu_te",
                                      "config": "{}"})
    return calls


# The four seat-management operations, as (method, path, body).
FOUR = [
    ("post", "/tenant/seats", {}),
    ("delete", f"/tenant/seats/{USER_A}", None),
    ("post", "/tenant/agent-seats", {"label": "bot"}),
    ("delete", f"/tenant/agent-seats/{AGENT_A}", None),
]


def _call(client, method, path, body):
    kw = {"headers": _csrf()}
    if body is not None:
        kw["json"] = body
    return getattr(client, method)(path, **kw)


# ════════════════════════════════════════════════════════════════════════════════
# 1. flag ON → every seat operation still works
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path,body", FOUR, ids=["create", "deprov", "agent-create",
                                                        "agent-delete"])
def test_flag_on_allows_each_operation(client, flag, seat_plumbing, audit, method, path, body):
    flag["value"] = {"enabled": True, "changed_at": None, "changed_by": None}
    _cookies(client)
    r = _call(client, method, path, body)
    assert r.status_code == 200, r.text


def test_flag_on_actually_provisions(client, flag, seat_plumbing, audit):
    """Sanity: the ON path reaches the real work, so the OFF assertions below mean something."""
    _cookies(client)
    assert _call(client, "post", "/tenant/seats", {}).status_code == 200
    assert seat_plumbing["register"] and seat_plumbing["register"][0][0] == ACCT_A
    assert _call(client, "delete", f"/tenant/seats/{USER_A}", None).status_code == 200
    assert seat_plumbing["revoke"] == [(ACCT_A, USER_A)]


# ════════════════════════════════════════════════════════════════════════════════
# 2. flag OFF → every seat operation 403s, before any side effect
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path,body", FOUR, ids=["create", "deprov", "agent-create",
                                                        "agent-delete"])
def test_flag_off_403s_each_operation(client, flag, seat_plumbing, audit, method, path, body):
    flag["value"] = {"enabled": False, "changed_at": "2026-08-07T00:00:00+00:00",
                     "changed_by": "operator"}
    _cookies(client)
    r = _call(client, method, path, body)
    assert r.status_code == 403, r.text
    # Nothing downstream ran — the gate is a dependency, so it fires before the handler body.
    assert seat_plumbing["register"] == [] and seat_plumbing["revoke"] == []


def test_flag_off_denial_is_audited(client, flag, seat_plumbing, audit):
    flag["value"] = {"enabled": False, "changed_at": None, "changed_by": "operator"}
    _cookies(client)
    assert _call(client, "post", "/tenant/seats", {}).status_code == 403
    denials = [a for a in audit if a["action"] == "seat.manage" and a.get("result") == "denied"]
    assert len(denials) == 1
    d = denials[0]
    assert d["actor_type"] == "tenant"
    assert d["actor_id"] == USER_A          # server-derived from the signed session
    assert d["account_id"] == ACCT_A
    assert "seat_management_enabled" in (d.get("detail") or "")


def test_successful_seat_create_and_delete_name_the_actor(client, flag, seat_plumbing, audit):
    """The success must name WHO, not just that the license moved.

    ``seats.register_seat``/``revoke_seat`` already write actorless ``seat.register`` /
    ``seat.deprovision`` rows at the DATA layer (shared with operator assign and OWUI
    auto-discovery). Without these handler rows a DENIED seat create named the admin while a
    SUCCESSFUL one named nobody — backwards for SOC 2 CC6.1. Same split the agent-seat handlers
    already use (data-layer seat.register + handler agent.seat_create)."""
    _cookies(client)
    assert _call(client, "post", "/tenant/seats", {}).status_code == 200
    created = [a for a in audit if a["action"] == "seat.create"]
    assert len(created) == 1
    assert created[0]["result"] == "success"
    assert created[0]["actor_type"] == "tenant"
    assert created[0]["actor_id"] == USER_A       # server-derived from the signed session
    assert created[0]["account_id"] == ACCT_A
    assert created[0]["target_type"] == "seat"

    assert _call(client, "delete", f"/tenant/seats/{USER_A}", None).status_code == 200
    deleted = [a for a in audit if a["action"] == "seat.delete"]
    assert len(deleted) == 1
    assert deleted[0]["actor_type"] == "tenant" and deleted[0]["actor_id"] == USER_A
    assert deleted[0]["target_id"] == USER_A


def test_denied_and_successful_seat_actions_are_both_attributed(client, flag, seat_plumbing,
                                                                audit):
    """Deny then allow: every row about this admin's seat actions carries their id."""
    _cookies(client)
    flag["value"] = {"enabled": False, "changed_at": None, "changed_by": None}
    assert _call(client, "post", "/tenant/seats", {}).status_code == 403
    flag["value"] = {"enabled": True, "changed_at": None, "changed_by": None}
    assert _call(client, "post", "/tenant/seats", {}).status_code == 200
    seat_rows = [a for a in audit if a["action"] in ("seat.manage", "seat.create")]
    assert {a["action"] for a in seat_rows} == {"seat.manage", "seat.create"}
    assert all(a["actor_id"] == USER_A and a["actor_type"] == "tenant" for a in seat_rows)


def test_control_plane_unreadable_is_fail_closed(client, monkeypatch, seat_plumbing, audit):
    """get_seat_management → None (account gone / DB down) must DENY, not allow."""
    monkeypatch.setattr(tc._accounts, "get_seat_management", lambda a: None)
    _cookies(client)
    assert _call(client, "post", "/tenant/seats", {}).status_code == 403
    assert seat_plumbing["register"] == []


def test_gate_runs_after_session_and_csrf(client, flag, seat_plumbing):
    """The flag never becomes a way IN: no session is still 401, and a missing CSRF token is
    still 403-for-CSRF — both decided by the chained require_dashboard_session."""
    flag["value"] = {"enabled": True, "changed_at": None, "changed_by": None}
    assert client.post("/tenant/seats", json={}).status_code == 401
    _cookies(client)
    r = client.post("/tenant/seats", json={})     # session, but no CSRF header
    assert r.status_code == 403
    assert "csrf" in r.json()["detail"].lower()


def test_flag_does_not_consult_billing_role(client, flag, seat_plumbing, monkeypatch):
    """Consuming a seat is configuration, not spend: a NON-billing admin may still do it when the
    flag is ON. This pins that migration 227 did not quietly re-gate seats behind billing."""
    monkeypatch.setattr(tc._auth, "can_view_billing", lambda sub: False)
    flag["value"] = {"enabled": True, "changed_at": None, "changed_by": None}
    _cookies(client)
    assert _call(client, "post", "/tenant/seats", {}).status_code == 200


# ════════════════════════════════════════════════════════════════════════════════
# 3. the column default is TRUE — no existing tenant loses an ability on deploy
# ════════════════════════════════════════════════════════════════════════════════

_MIGRATION = (pathlib.Path(__file__).resolve().parent.parent
              / "migrations" / "227_accounts_seat_management_enabled.sql")


def test_migration_default_is_true_and_idempotent():
    assert _MIGRATION.is_file(), f"missing {_MIGRATION}"
    sql = _MIGRATION.read_text()
    flat = " ".join(sql.split()).lower()
    assert ("alter table public.accounts add column if not exists "
            "seat_management_enabled boolean not null default true") in flat
    # Control-plane only — never a tenant schema, and never a destructive statement.
    assert "public.accounts" in flat
    for forbidden in ("drop column", "drop table", "delete from", "create schema"):
        assert forbidden not in flat
    assert "comment on column public.accounts.seat_management_enabled" in flat


def test_account_without_an_explicit_setting_reads_as_enabled(monkeypatch):
    """A row carrying the migration DEFAULT (never touched by an operator) reads enabled=True
    with no change provenance — i.e. every existing tenant keeps seat management."""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return (True, None, None)   # DEFAULT TRUE, never changed

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def commit(self): pass

    monkeypatch.setattr(saas_accounts.db, "connection", lambda: _Conn())
    assert saas_accounts.get_seat_management(ACCT_A) == {
        "enabled": True, "changed_at": None, "changed_by": None}


def test_accessor_is_fail_safe_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("control plane down")
    monkeypatch.setattr(saas_accounts.db, "connection", _boom)
    assert saas_accounts.get_seat_management(ACCT_A) is None
    assert saas_accounts.set_seat_management_enabled(ACCT_A, False, changed_by="op") is None


# ════════════════════════════════════════════════════════════════════════════════
# 4. the OPERATOR endpoint — flips it, and is operator-only
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def op_store(monkeypatch):
    """In-memory stand-in for the accounts row, shared by get/set."""
    store = {ACCT_A: {"enabled": True, "changed_at": None, "changed_by": None}}

    def _get(account_id):
        return dict(store[account_id]) if account_id in store else None

    def _set(account_id, enabled, *, changed_by=None):
        if account_id not in store:
            return None
        store[account_id] = {"enabled": bool(enabled),
                             "changed_at": "2026-08-07T12:00:00+00:00",
                             "changed_by": changed_by}
        return dict(store[account_id])

    monkeypatch.setattr(saas_admin._accounts, "get_seat_management", _get)
    monkeypatch.setattr(saas_admin._accounts, "set_seat_management_enabled", _set)
    return store


def test_operator_turns_seat_management_off_then_on(op_client, op_store, audit):
    r = op_client.post(f"/admin/accounts/{ACCT_A}/seat-management",
                       json={"enabled": False}, headers=_op())
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["account_id"] == ACCT_A
    assert r.json()["changed_by"] == "operator-key"      # break-glass key identity, server-side
    assert op_store[ACCT_A]["enabled"] is False

    r = op_client.post(f"/admin/accounts/{ACCT_A}/seat-management",
                       json={"enabled": True}, headers=_op())
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert op_store[ACCT_A]["enabled"] is True


def test_operator_flip_is_audited(op_client, op_store, audit):
    op_client.post(f"/admin/accounts/{ACCT_A}/seat-management",
                   json={"enabled": False}, headers=_op())
    rows = [a for a in audit if a["action"] == "account.seat_management"]
    assert len(rows) == 1
    assert rows[0]["actor_type"] == "operator" and rows[0]["result"] == "success"
    assert rows[0]["account_id"] == ACCT_A
    assert "False" in (rows[0].get("detail") or "")


def test_operator_flip_unknown_account_404s(op_client, op_store):
    r = op_client.post(f"/admin/accounts/{ACCT_B}/seat-management",
                       json={"enabled": False}, headers=_op())
    assert r.status_code == 404


@pytest.mark.parametrize("headers", [
    {},                                          # no credential at all
    {"Authorization": f"Bearer {KEY_A}"},        # a TENANT account key is not an operator
], ids=["anonymous", "account-key"])
def test_non_operator_cannot_flip(op_client, op_store, headers):
    r = op_client.post(f"/admin/accounts/{ACCT_A}/seat-management",
                       json={"enabled": False}, headers=headers)
    assert r.status_code == 403
    assert op_store[ACCT_A]["enabled"] is True   # unchanged — no partial write


def test_tenant_console_has_no_self_service_route(client, flag):
    """The whole point: a tenant admin cannot grant itself seat management."""
    _cookies(client)
    for method in ("post", "put"):
        r = getattr(client, method)("/tenant/seat-management", json={"enabled": True},
                                    headers=_csrf())
        assert r.status_code in (404, 405)


# ── GET is operator-or-own-account (BOLA) ───────────────────────────────────────

def test_operator_reads_state(op_client, op_store):
    r = op_client.get(f"/admin/accounts/{ACCT_A}/seat-management", headers=_op())
    assert r.status_code == 200
    assert r.json()["enabled"] is True and r.json()["account_id"] == ACCT_A


def test_account_key_cannot_read_another_account(op_client, op_store, monkeypatch):
    monkeypatch.setattr(saas_admin._accounts, "resolve_key_to_account",
                        lambda presented: ACCT_A if presented == KEY_A else None)
    ok = op_client.get(f"/admin/accounts/{ACCT_A}/seat-management",
                       headers={"Authorization": f"Bearer {KEY_A}"})
    assert ok.status_code == 200
    bola = op_client.get(f"/admin/accounts/{ACCT_B}/seat-management",
                         headers={"Authorization": f"Bearer {KEY_A}"})
    assert bola.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# 5. the flag reaches the browser — GET /tenant/me
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
def test_tenant_me_surfaces_can_manage_seats(client, flag, monkeypatch, enabled, expected):
    monkeypatch.setattr(tc._auth, "can_view_billing", lambda sub: False)
    monkeypatch.setattr(tc._auth, "is_account_owner", lambda a, s: True)
    monkeypatch.setattr(tc._auth, "role_for", lambda s: "admin")
    flag["value"] = {"enabled": enabled, "changed_at": None, "changed_by": None}
    _cookies(client)
    r = client.get("/tenant/me")
    assert r.status_code == 200
    body = r.json()
    assert body["can_manage_seats"] is expected
    # …and it is a SEPARATE field from the billing role, not derived from it.
    assert body["can_view_billing"] is False


def test_tenant_me_can_manage_seats_is_fail_closed(client, monkeypatch):
    monkeypatch.setattr(tc._accounts, "get_seat_management", lambda a: None)
    monkeypatch.setattr(tc._auth, "can_view_billing", lambda sub: True)
    monkeypatch.setattr(tc._auth, "is_account_owner", lambda a, s: True)
    monkeypatch.setattr(tc._auth, "role_for", lambda s: "admin")
    _cookies(client)
    assert client.get("/tenant/me").json()["can_manage_seats"] is False


# ════════════════════════════════════════════════════════════════════════════════
# 6. routes are actually mounted with the new dependency
# ════════════════════════════════════════════════════════════════════════════════

def test_the_four_endpoints_use_the_seat_management_gate():
    """Structural proof, so a future refactor cannot silently drop the gate off one of them."""
    wanted = {
        ("POST", "/tenant/seats"),
        ("DELETE", "/tenant/seats/{user_id}"),
        ("POST", "/tenant/agent-seats"),
        ("DELETE", "/tenant/agent-seats/{user_id}"),
    }
    seen = set()
    for route in tc.router.routes:
        for method in getattr(route, "methods", set()) or set():
            key = (method, route.path)
            if key not in wanted:
                continue
            deps = [d.call for d in route.dependant.dependencies]
            assert tc.require_seat_management in deps, f"{key} lost the seat-management gate"
            seen.add(key)
    assert seen == wanted


def test_capacity_endpoints_keep_the_billing_gate():
    """The buy-capacity lever is untouched: still require_billing_access, not the new flag."""
    wanted = {("POST", "/tenant/capacity"), ("POST", "/tenant/capacity/bundle"),
              ("DELETE", "/tenant/capacity")}
    seen = set()
    for route in tc.router.routes:
        for method in getattr(route, "methods", set()) or set():
            key = (method, route.path)
            if key not in wanted:
                continue
            deps = [d.call for d in route.dependant.dependencies]
            assert tc.require_billing_access in deps
            assert tc.require_seat_management not in deps
            seen.add(key)
    assert seen == wanted


def test_non_count_changing_seat_actions_stay_ungated():
    """Renaming a seat / issuing a token does not change the seat COUNT, so it is not seat
    management and must NOT pick up the new gate."""
    for route in tc.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if route.path in ("/tenant/seats/{user_id}", "/tenant/seats/{user_id}/token"):
            if "DELETE" in methods and route.path == "/tenant/seats/{user_id}":
                continue    # that one IS deprovision, gated above
            deps = [d.call for d in route.dependant.dependencies]
            assert tc.require_seat_management not in deps, route.path
