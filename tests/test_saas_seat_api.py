"""PER-SEAT /v1 API grant (migration 161) — least-privilege BELOW the account add-on ceiling.

Owner requirement: enabling the ACCOUNT add-on (migration 160) must NOT silently arm every seat's
flu_ token as a live API credential ("Jenn from accounting who got phished shouldn't have a live
API door she never uses"). /v1 access is now TWO levels:

  1. account CEILING — accounts.api_addon_enabled (migration 160): did the account BUY API?
  2. per-SEAT GRANT  — account_users.api_enabled (migration 161): is THIS seat armed? (default OFF)

A /v1 call is allowed ONLY when BOTH hold. These proofs cover the non-negotiable invariants:

  1. account OFF               → /v1 403 ``api_not_enabled`` (regardless of the seat), never metered.
  2. account ON + seat OFF     → /v1 403 ``seat_api_not_enabled`` — THE Jenn case — never metered.
  3. account ON + seat ON      → /v1 200, metered exactly once.
  4. resolver fail-closed      → seat_api_enabled denies on missing ids / any DB error.
  5. grant ceiling             → set_seat_api_enabled ENABLE is refused (409) when the account is off.
  6. tenant grant plain-admin  → PUT /tenant/seats/{id}/api works for a NON-billing admin; BOLA-safe.
  7. operator override         → PUT /admin/accounts/{id}/users/{id}/api flips the seat (break-glass).

Control-plane data functions are monkeypatched, so this file is green without a control plane.
Run: python3 -m pytest tests/test_saas_seat_api.py -q  (tests/ is gitignored → git add -f).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UID = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"           # the granted seat
JENN = "beadfeed-2222-4222-8222-222222222222"          # the ungranted "Jenn from accounting" seat
FOREIGN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"        # a seat of ANOTHER account (BOLA target)
ADMIN_NB = "22222222-2222-4222-8222-222222222222"       # seat/security admin — NO billing rights
OP_KEY = "op-super-secret-key"


# ════════════════════════════════════════════════════════════════════════════════
# 1–3. The REAL two-level gate wired into the /v1 router: 403 codes + never-metered.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def v1_client(monkeypatch):
    """Mount ONLY the /v1 router, wired with the REAL ``http_server._saas_api_addon_block`` so the
    two-level (account ∧ seat) gate is exercised end-to-end. account/seat flags are controllable."""
    monkeypatch.setenv("SAAS_MODE", "1")
    from src.mcp import api_v1, http_server
    from saas import api_addon, config

    state = {"account": False, "seat": False, "meter_calls": []}

    # Force SAAS on + a fixed account, and drive the two levels from `state`.
    monkeypatch.setattr(config, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(http_server, "_saas_account_for", lambda principal, user_id: ACCT_A)
    monkeypatch.setattr(api_addon, "api_enabled", lambda account_id: state["account"])
    monkeypatch.setattr(api_addon, "seat_api_enabled",
                        lambda account_id, user_id: state["seat"])

    class _Tools:
        @staticmethod
        async def remember_facts_tool(text, user_id):
            return {"status": "stored", "committed": 1}

        @staticmethod
        async def recall_memory_tool(query, user_id):
            return {"memory": "you like blue"}

    async def _meter(principal, user_id, tool_name, api=False):
        state["meter_calls"].append(tool_name)

    router = api_v1.build_router(
        require_auth=lambda: "account:" + ACCT_A,
        resolve_user_id=lambda request, body_uid, principal: (body_uid or UID),
        ensure_provisioned=lambda user_id: _true(),
        write_block=lambda p, u, t: None,
        quota_block=lambda p, u, t: None,
        api_addon_block=http_server._saas_api_addon_block,   # THE REAL two-level gate
        meter=_meter,
        tools=_Tools(),
        log=lambda m: None,
    )
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.gate_state = state
    return c


async def _true():
    return True


def test_account_off_blocks_regardless_of_seat(v1_client):
    # account OFF, seat ON → still the account-ceiling 403 (the ceiling dominates).
    v1_client.gate_state.update(account=False, seat=True)
    r = v1_client.post("/v1/memories", json={"text": "My favourite colour is blue."})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "api_not_enabled"
    assert v1_client.gate_state["meter_calls"] == []          # never billed when shut


def test_account_on_seat_off_is_the_jenn_case(v1_client):
    """THE Jenn case: the account BOUGHT API, but her seat was never granted → a distinct,
    non-billed 403 ``seat_api_not_enabled``. Her phished flu_ token is NOT a live API door."""
    v1_client.gate_state.update(account=True, seat=False)
    rw = v1_client.post("/v1/memories", json={"text": "My favourite colour is blue."})
    assert rw.status_code == 403
    assert rw.json()["error"]["code"] == "seat_api_not_enabled"
    # recall is a billable /v1 transaction too — gated the same way.
    rr = v1_client.post("/v1/recall", json={"query": "what colour do I like?"})
    assert rr.status_code == 403
    assert rr.json()["error"]["code"] == "seat_api_not_enabled"
    assert v1_client.gate_state["meter_calls"] == []          # PROVE: never a meter bump


def test_account_on_seat_on_allows_and_meters(v1_client):
    v1_client.gate_state.update(account=True, seat=True)
    rw = v1_client.post("/v1/memories", json={"text": "My favourite colour is blue."})
    assert rw.status_code == 200
    rr = v1_client.post("/v1/recall", json={"query": "what colour do I like?"})
    assert rr.status_code == 200 and rr.json()["memory"] == "you like blue"
    assert v1_client.gate_state["meter_calls"] == ["remember_facts", "recall_memory"]


# ════════════════════════════════════════════════════════════════════════════════
# 4. seat_api_enabled resolver — fail-CLOSED.
# ════════════════════════════════════════════════════════════════════════════════

def test_seat_api_resolver_fail_closed(monkeypatch):
    from saas import api_addon
    api_addon.invalidate_seat_api_cache()
    assert api_addon.seat_api_enabled("", UID) is False
    assert api_addon.seat_api_enabled(ACCT_A, "") is False
    assert api_addon.seat_api_enabled(None, None) is False

    class _Boom:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(api_addon.db, "connection", lambda: _Boom())
    api_addon.invalidate_seat_api_cache()
    assert api_addon.seat_api_enabled(ACCT_A, UID) is False   # any error → deny


# ════════════════════════════════════════════════════════════════════════════════
# 5. set_seat_api_enabled — the ceiling gate (can't grant what the account hasn't bought).
# ════════════════════════════════════════════════════════════════════════════════

class _FakeCur:
    def __init__(self, store): self._store = store; self._row = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params):
        # UPDATE ... RETURNING api_enabled
        want, account_id, user_id = params
        self._store["api_enabled"] = bool(want)
        self._row = (bool(want),) if self._store.get("is_seat", True) else None
    def fetchone(self): return self._row


class _FakeConn:
    def __init__(self, store): self._store = store
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _FakeCur(self._store)
    def commit(self): self._store["committed"] = True


@pytest.fixture
def seat_db(monkeypatch):
    from saas import seats, api_addon
    store = {"api_enabled": False, "is_seat": True, "committed": False, "ceiling": False}
    monkeypatch.setattr(seats.db, "connection", lambda: _FakeConn(store))
    monkeypatch.setattr(seats._audit, "record", lambda *a, **k: None)
    monkeypatch.setattr(api_addon, "api_enabled", lambda account_id: store["ceiling"])
    return seats, store


def test_grant_refused_when_account_ceiling_off(seat_db):
    seats, store = seat_db
    store["ceiling"] = False
    out = seats.set_seat_api_enabled(ACCT_A, UID, True)
    assert out["ok"] is False and out["status_code"] == 409
    assert store["committed"] is False                        # no write attempted


def test_grant_succeeds_when_ceiling_on(seat_db):
    seats, store = seat_db
    store["ceiling"] = True
    out = seats.set_seat_api_enabled(ACCT_A, UID, True)
    assert out["ok"] is True and out["api_enabled"] is True and store["committed"] is True


def test_revoke_always_allowed_even_if_ceiling_off(seat_db):
    seats, store = seat_db
    store["ceiling"] = False                                  # ceiling off, revoke still ok
    out = seats.set_seat_api_enabled(ACCT_A, UID, False)
    assert out["ok"] is True and out["api_enabled"] is False


def test_grant_404_when_not_a_seat(seat_db):
    seats, store = seat_db
    store["ceiling"] = True; store["is_seat"] = False
    out = seats.set_seat_api_enabled(ACCT_A, UID, True)
    assert out["ok"] is False and out["status_code"] == 404


# ════════════════════════════════════════════════════════════════════════════════
# 6. Tenant grant endpoint — PLAIN-admin (non-billing) + BOLA-safe.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "seat-api-unit-test-secret-0123456789")
    from saas import tenant_console as tc
    monkeypatch.setattr(tc._audit, "record", lambda *a, **k: None)
    # BOLA: only UID and JENN are seats of ACCT_A; FOREIGN is not.
    monkeypatch.setattr(tc._seats, "is_active_seat",
                        lambda account_id, user_id: account_id == ACCT_A and user_id in (UID, JENN))
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _login(client):
    from saas import dashboard_auth as auth
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       auth.issue_session(dashboard_user_id=ADMIN_NB, account_id=ACCT_A, username="samad"))


def _csrf():
    from saas import dashboard_auth as auth
    return auth.issue_csrf(dashboard_user_id=ADMIN_NB)


def test_tenant_grant_allowed_for_non_billing_admin(tenant_client, monkeypatch):
    """The grant is an ACCESS act, not a spend act — a NON-billing admin may flip it."""
    from saas import tenant_console as tc
    seen = {}
    monkeypatch.setattr(tc._seats, "set_seat_api_enabled",
                        lambda a, u, e: (seen.__setitem__("args", (a, u, e)),
                                         {"ok": True, "status_code": 200, "api_enabled": e})[1])
    _login(tenant_client)
    r = tenant_client.put(f"/tenant/seats/{UID}/api", headers={"x-csrf-token": _csrf()},
                          json={"enabled": True})
    assert r.status_code == 200 and r.json()["api_enabled"] is True
    # BOLA-safe: account came from the SESSION, the seat from the path.
    assert seen["args"] == (ACCT_A, UID, True)


def test_tenant_grant_bola_foreign_seat_403(tenant_client):
    _login(tenant_client)
    r = tenant_client.put(f"/tenant/seats/{FOREIGN}/api", headers={"x-csrf-token": _csrf()},
                          json={"enabled": True})
    assert r.status_code == 403                               # not a seat of this account


def test_tenant_grant_ceiling_off_bubbles_409(tenant_client, monkeypatch):
    from saas import tenant_console as tc
    monkeypatch.setattr(tc._seats, "set_seat_api_enabled",
                        lambda a, u, e: {"ok": False, "status_code": 409,
                                         "reason": "account API add-on is not enabled"})
    _login(tenant_client)
    r = tenant_client.put(f"/tenant/seats/{JENN}/api", headers={"x-csrf-token": _csrf()},
                          json={"enabled": True})
    assert r.status_code == 409


# ════════════════════════════════════════════════════════════════════════════════
# 7. Operator override.
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "seat-api-op-secret-0123456789")
    from saas import admin as saas_admin
    monkeypatch.setattr(saas_admin._audit, "record", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def test_operator_override_grants_seat(admin_client, monkeypatch):
    from saas import admin as saas_admin
    seen = {}
    monkeypatch.setattr(saas_admin._seats, "set_seat_api_enabled",
                        lambda a, u, e: (seen.__setitem__("args", (a, u, e)),
                                         {"ok": True, "status_code": 200, "api_enabled": e})[1])
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/{JENN}/api",
                         headers={"Authorization": f"Bearer {OP_KEY}"},
                         json={"enabled": True})
    assert r.status_code == 200 and r.json()["api_enabled"] is True
    assert seen["args"] == (ACCT_A, JENN, True)


def test_operator_override_requires_operator(admin_client):
    r = admin_client.put(f"/admin/accounts/{ACCT_A}/users/{JENN}/api",
                         headers={"Authorization": "Bearer flk_not_operator"},
                         json={"enabled": True})
    assert r.status_code in (401, 403)
