"""API-access ADD-ON lever (migration 160) — the transparent, opt-in, billable /v1 gate.

Owner-ratified decision (DEV/DESIGN-api-access-addon-lever.md, 2026-07-11): a valid ``flk_`` key
AUTHENTICATES but no longer IMPLIES API entitlement. The public ``/v1`` API is OFF until a BILLING
admin enables the account-level add-on with a signed consent attestation. These proofs cover the
non-negotiable invariants:

  1. /v1 fail-closed   → write AND recall return 403 ``api_not_enabled`` when the add-on is OFF,
                         and 200 when it is ON (the gate runs AFTER auth+BOLA, BEFORE the meter).
  2. no silent bill    → when blocked, the meter is NEVER bumped.
  3. consent required  → PUT /tenant/api-addon {enabled:true} WITHOUT an attestation → 400.
  4. billing-gated     → the toggle is denied (403) to a non-billing admin; allowed for billing.
  5. operator override → the operator support toggle flips the flag (break-glass).

Control-plane data functions are monkeypatched, so this file is green without a control plane.
Run: python3 -m pytest tests/test_saas_api_addon.py -q  (tests/ is gitignored → git add -f).
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UID = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"
OWNER = "11111111-1111-4111-8111-111111111111"     # billing admin
ADMIN_NB = "22222222-2222-4222-8222-222222222222"  # seat/security admin — NO billing
OP_KEY = "op-super-secret-key"


# ════════════════════════════════════════════════════════════════════════════════
# 1–2. The /v1 fail-closed gate (build_router) — 403 off, 200 on, never metered off.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def v1_client():
    """A FastAPI app mounting ONLY the /v1 router, wired with controllable seams. The add-on
    gate closure mirrors http_server._saas_api_addon_block: consult the resolver, 403 when off."""
    from src.mcp import api_v1

    state = {"enabled": False, "meter_calls": [], "provisioned": True}

    class _Tools:
        @staticmethod
        async def remember_facts_tool(text, user_id):
            return {"status": "stored", "committed": 1}

        @staticmethod
        async def recall_memory_tool(query, user_id):
            return {"memory": "you like blue"}

    def _gate(principal, user_id):
        if not state["enabled"]:
            return {"status": "api_not_enabled",
                    "detail": "API access is not enabled for this account.",
                    "enable_at": "/dashboard/tenant#plan"}
        return None

    async def _meter(principal, user_id, tool_name, api=False):
        state["meter_calls"].append(tool_name)
        if api:
            state.setdefault("api_meter_calls", []).append(tool_name)

    async def _prov(user_id):
        return state["provisioned"]

    router = api_v1.build_router(
        require_auth=lambda: "account:" + ACCT_A,
        resolve_user_id=lambda request, body_uid, principal: (body_uid or UID),
        ensure_provisioned=_prov,
        write_block=lambda p, u, t: None,
        quota_block=lambda p, u, t: None,
        api_addon_block=_gate,
        meter=_meter,
        tools=_Tools(),
        log=lambda m: None,
    )
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.gate_state = state
    return c


def test_v1_write_blocked_when_addon_off(v1_client):
    v1_client.gate_state["enabled"] = False
    r = v1_client.post("/v1/memories", json={"text": "My favourite colour is blue."})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "api_not_enabled"
    # NEVER billed when the door is shut.
    assert v1_client.gate_state["meter_calls"] == []


def test_v1_recall_blocked_when_addon_off(v1_client):
    v1_client.gate_state["enabled"] = False
    r = v1_client.post("/v1/recall", json={"query": "what colour do I like?"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "api_not_enabled"
    assert v1_client.gate_state["meter_calls"] == []


def test_v1_write_allowed_when_addon_on(v1_client):
    v1_client.gate_state["enabled"] = True
    r = v1_client.post("/v1/memories", json={"text": "My favourite colour is blue."})
    assert r.status_code == 200
    # metered exactly once now that the door is open.
    assert v1_client.gate_state["meter_calls"] == ["remember_facts"]


def test_v1_recall_allowed_when_addon_on(v1_client):
    v1_client.gate_state["enabled"] = True
    r = v1_client.post("/v1/recall", json={"query": "what colour do I like?"})
    assert r.status_code == 200
    assert r.json()["memory"] == "you like blue"
    assert v1_client.gate_state["meter_calls"] == ["recall_memory"]


def test_v1_health_open_regardless(v1_client):
    v1_client.gate_state["enabled"] = False
    assert v1_client.get("/v1/health").status_code == 200   # spec/health never gated


def test_api_enabled_resolver_fail_closed(monkeypatch):
    """The resolver itself denies on no account_id and on any DB error (fail-CLOSED)."""
    from saas import api_addon
    api_addon.invalidate_api_addon_cache()
    assert api_addon.api_enabled("") is False
    assert api_addon.api_enabled(None) is False

    class _Boom:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(api_addon.db, "connection", lambda: _Boom())
    api_addon.invalidate_api_addon_cache()
    assert api_addon.api_enabled(ACCT_A) is False   # any error → deny


# ════════════════════════════════════════════════════════════════════════════════
# 3–4. Tenant toggle — consent required + billing-gated (BOLA-safe, server-derived)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "api-addon-unit-test-secret-0123456789")
    from saas import tenant_console as tc
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: None)
    yield


@pytest.fixture
def tenant_client(saas_on):
    from saas import tenant_console as tc
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


@pytest.fixture
def roles(monkeypatch):
    from saas import dashboard_auth as auth
    _bill = {OWNER: True, ADMIN_NB: False}
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: _bill.get(str(uid), False))


def _login(client, sub):
    from saas import dashboard_auth as auth
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=sub, account_id=ACCT_A, username=sub[:5]))


def _csrf(sub):
    from saas import dashboard_auth as auth
    return auth.issue_csrf(dashboard_user_id=sub)


def test_enable_requires_attestation(tenant_client, roles, monkeypatch):
    from saas import tenant_console as tc
    called = {"ledger": 0, "flag": 0}
    monkeypatch.setattr(tc._entitlements, "record_api_addon_change",
                        lambda *a, **k: called.__setitem__("ledger", called["ledger"] + 1) or {"ok": True, "status_code": 200})
    monkeypatch.setattr(tc._accounts, "set_api_addon_enabled",
                        lambda *a, **k: called.__setitem__("flag", called["flag"] + 1) or {"enabled": True})
    _login(tenant_client, OWNER)
    r = tenant_client.put("/tenant/api-addon", headers={"x-csrf-token": _csrf(OWNER)},
                          json={"enabled": True})   # no attestation
    assert r.status_code == 400
    # neither the ledger nor the flag was touched — nothing opened without consent.
    assert called == {"ledger": 0, "flag": 0}


def test_enable_denied_for_non_billing_admin(tenant_client, roles):
    _login(tenant_client, ADMIN_NB)
    r = tenant_client.put("/tenant/api-addon", headers={"x-csrf-token": _csrf(ADMIN_NB)},
                          json={"enabled": True, "attestation": "Sam Admin"})
    assert r.status_code == 403


def test_get_denied_for_non_billing_admin(tenant_client, roles):
    _login(tenant_client, ADMIN_NB)
    assert tenant_client.get("/tenant/api-addon").status_code == 403


def test_enable_success_for_billing_admin(tenant_client, roles, monkeypatch):
    from saas import tenant_console as tc
    seen = {}

    def _ledger(account_id, enabled, **k):
        seen["ledger"] = (account_id, enabled, k.get("signature"))
        return {"ok": True, "status_code": 200, "ledger_id": 7, "amount": 99.0}

    def _setflag(account_id, enabled, **k):
        seen["flag"] = (account_id, enabled, k.get("enabled_by"), k.get("terms_version"))
        return {"enabled": enabled, "enabled_at": "2026-07-11T10:00:00",
                "enabled_by": k.get("enabled_by"), "terms_version": k.get("terms_version")}

    monkeypatch.setattr(tc._entitlements, "record_api_addon_change", _ledger)
    monkeypatch.setattr(tc._accounts, "set_api_addon_enabled", _setflag)
    _login(tenant_client, OWNER)
    r = tenant_client.put("/tenant/api-addon", headers={"x-csrf-token": _csrf(OWNER)},
                          json={"enabled": True, "attestation": "Olivia Owner"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["account_id"] == ACCT_A
    # BOLA-safe: the account came from the SESSION, never client input.
    assert seen["ledger"][0] == ACCT_A and seen["ledger"][1] is True
    assert seen["ledger"][2] == "Olivia Owner"        # the typed-name signature reached the ledger
    assert seen["flag"][0] == ACCT_A and seen["flag"][1] is True


def test_disable_needs_no_signature(tenant_client, roles, monkeypatch):
    from saas import tenant_console as tc
    seen = {}
    monkeypatch.setattr(tc._entitlements, "record_api_addon_change",
                        lambda account_id, enabled, **k: seen.setdefault("off", enabled) or {"ok": True, "status_code": 200})
    monkeypatch.setattr(tc._accounts, "set_api_addon_enabled",
                        lambda account_id, enabled, **k: {"enabled": enabled})
    _login(tenant_client, OWNER)
    r = tenant_client.put("/tenant/api-addon", headers={"x-csrf-token": _csrf(OWNER)},
                          json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert seen["off"] is False


def test_get_reads_state(tenant_client, roles, monkeypatch):
    from saas import tenant_console as tc
    monkeypatch.setattr(tc._accounts, "get_api_addon",
                        lambda a: {"enabled": True, "enabled_at": "2026-07-11T10:00:00",
                                   "enabled_by": "olivia", "terms_version": "2026-07-11",
                                   "disabled_at": None})
    _login(tenant_client, OWNER)
    body = tenant_client.get("/tenant/api-addon").json()
    assert body["enabled"] is True and body["account_id"] == ACCT_A
    assert body["base_price"] > 0   # pricing surfaced for the consent panel


# ════════════════════════════════════════════════════════════════════════════════
# 5. Operator support override
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "api-addon-op-secret-0123456789")
    from saas import admin as saas_admin
    monkeypatch.setattr(saas_admin._audit, "record", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def test_operator_override_enables(admin_client, monkeypatch):
    from saas import admin as saas_admin
    seen = {}

    def _led(account_id, enabled, **k):
        seen["led"] = (account_id, enabled)
        return {"ok": True, "status_code": 200}
    monkeypatch.setattr(saas_admin._entitlements, "record_api_addon_change", _led)
    monkeypatch.setattr(saas_admin._accounts, "set_api_addon_enabled",
                        lambda account_id, enabled, **k: {"enabled": enabled})
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/api-addon",
                          headers={"Authorization": f"Bearer {OP_KEY}"},
                          json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert seen["led"] == (ACCT_A, True)


def test_operator_override_requires_operator(admin_client):
    r = admin_client.post(f"/admin/accounts/{ACCT_A}/api-addon",
                          headers={"Authorization": "Bearer flk_not_operator"},
                          json={"enabled": True})
    assert r.status_code in (401, 403)   # not an operator credential → denied
