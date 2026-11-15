"""Phase 2A control plane — the appliance-facing adapter over the tenant console.

Covers the non-negotiable invariants of the ``/v1/agent/*`` control verbs added by
``saas/agent_control.py`` (attached to the existing agent-proxy router). These are the endpoints
the ``faultline-agent`` appliance calls and which, before this change, all 404'd live:

  auth / gating
    1.  SAAS gate            → 404 on every control verb when SAAS_MODE is off.
    2.  delegate required    → 401 with no bearer, a garbage bearer, or a TAMPERED delegate.
    3.  realm isolation      → a tenant SESSION token cannot stand in for a delegate token.
    4.  seat lane            → /agent/state + /agent/expand use the require_auth (flu_) seam.

  BOLA (the critical one)
    5.  foreign seat         → 403 on bind AND on every per-seat config verb, with NO side effect
                               (the token mint / the console setter is never reached).
    6.  non-agent seat       → 403.  malformed seat id → 400.
    7.  account scope        → comes from the SIGNED delegate token, never from the client.

  delegation (this is an ADAPTER — it must not reimplement)
    8.  seats                → accounts.list_agent_seats + agent_entitlement.serve_allowed.
    9.  bind                 → tenant_console._mint_seat_token_payload (the existing mint).
    10. trial                → tenant_console.start_chatbot_trial (shared with the console route).
    11. expand               → src.mcp.server._learn_via_llm, behind the serve entitlement AND
                               the per-seat ingest freeze.
    12. per-seat config      → the tenant_console handlers themselves, with account_id from the
                               delegate.

  source restriction (production / WireGuard, dedicated IP)
    13. UNSET                → the gate is a pure no-op (existing deployments unaffected).
    14. SET + inside         → allowed.   SET + outside → 403.   SET but unparseable → 403.
    15. XFF spoofing         → an untrusted peer's X-Forwarded-For is IGNORED (it cannot place
                               itself inside the allow-list); honoured only via SAAS_TRUSTED_PROXY_IPS.

Every control-plane + LLM call is monkeypatched, so this file is green with no DB and no network.
Run: python3 -m pytest tests/test_agent_control.py -q
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

ACCT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_ACCT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SEAT = "10d7d879-63cd-4f31-92ce-f2c9edb760ab"
FOREIGN_SEAT = "20d7d879-63cd-4f31-92ce-f2c9edb760ab"

# A peer address the tests can put inside / outside an allow-list. TestClient's default peer is
# the non-IP string "testclient", which is itself a useful fail-closed case.
PEER = "10.99.0.7"


@pytest.fixture
def control(monkeypatch):
    """Mount the agent router (proxy + control plane) with every SaaS seam under test control.

    Returns ``(app, state, calls, log_lines)``. ``calls`` records which underlying function each
    handler reached — that is how the "this is an adapter, not a reimplementation" invariants are
    asserted."""
    # monkeypatch, not os.environ: these are process-wide and were never restored, so this
    # fixture's values (and its two deletions) outlived the test and leaked into every module
    # collected after it.
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "test-secret-for-agent-control")
    monkeypatch.delenv("AGENT_CONTROL_ALLOWED_CIDRS", raising=False)
    monkeypatch.delenv("SAAS_TRUSTED_PROXY_IPS", raising=False)

    from saas import (accounts, agent_control, agent_entitlement, agent_mode, agent_proxy,
                      metering, seat_brain, seats, tenant_console)

    state = {
        "saas_on": True,
        "seats": [{"user_id": SEAT, "label": "primary agent", "name": None, "email": None,
                   "status": "active", "created_at": None, "ingest_enabled": True}],
        # (account_id, seat_id) -> row. Only rows for the ASKING account are ever returned, which
        # is exactly how saas.seats.get_seat behaves — that is what makes BOLA structural.
        "seat_rows": {(ACCT, SEAT): {"user_id": SEAT, "label": "primary agent",
                                     "seat_type": "agent", "status": "active",
                                     "ingest_enabled": True}},
        "serve": (True, None),
        "brain": {"backend_type": "openai", "base_url": "https://b.test", "model": "m"},
        "chat_llm_cfg": {"provider": "openai-compat", "endpoint": "", "model": "",
                         "api_key_set": False},
        "write_block": None,
        "expand_result": {"memory": "Building concept map for 'robotics' …"},
    }
    calls: list[tuple] = []

    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: bool(state["saas_on"]))

    # ── control-plane reads/writes: every one of these is a REAL function the adapter must call ──
    def _list_agent_seats(account_id):
        calls.append(("list_agent_seats", account_id))
        return list(state["seats"]) if account_id == ACCT else []
    monkeypatch.setattr(accounts, "list_agent_seats", _list_agent_seats)
    monkeypatch.setattr(accounts, "get_agent_seat_config",
                        lambda aid, sid: {"seat_id": sid, "chat_llm": state["chat_llm_cfg"],
                                          "memory_backend": {}, "prompt": "", "frontend": {}})
    monkeypatch.setattr(seat_brain, "resolve_brain", lambda uid, aid: state["brain"])
    monkeypatch.setattr(agent_entitlement, "serve_allowed", lambda aid, uid: state["serve"])
    monkeypatch.setattr(seats, "get_seat",
                        lambda aid, uid: state["seat_rows"].get((aid, uid)))
    monkeypatch.setattr(agent_mode, "write_gate_block",
                        lambda p, u, t: state["write_block"])

    def _mint(account_id, user_id):
        calls.append(("mint", account_id, user_id))
        return {"user_id": user_id, "token": "flu_MINTED_SECRET", "token_id": "tid",
                "token_prefix": "flu_abcd", "endpoint_url": "https://fl.test",
                "mcp_endpoint": "https://fl.test/mcp", "config": {}}
    monkeypatch.setattr(tenant_console, "_mint_seat_token_payload", _mint)

    def _trial(account_id, **kw):
        calls.append(("start_chatbot_trial", account_id, kw))
        return {"account_id": account_id, "default_trial_days": 15, "already_active": False,
                "trial": {"active": True, "expires_at": "2026-08-22T00:00:00+00:00"}}
    monkeypatch.setattr(tenant_console, "start_chatbot_trial", _trial)

    def _seat_config(seat_id=None, account_id=None):
        calls.append(("tenant_get_agent_seat_config", account_id, seat_id))
        return {"account_id": account_id, "seat_id": seat_id, "prompt": "",
                "chat_llm": state["chat_llm_cfg"], "memory_backend": {}, "frontend": {},
                "bound_seat": {"seat_id": seat_id, "name": "primary agent",
                               "ingest_enabled": True}}
    monkeypatch.setattr(tenant_console, "tenant_get_agent_seat_config", _seat_config)

    for name, out in (("tenant_set_agent_chat_llm", {"provider": "openai-compat",
                                                     "api_key_set": True}),
                      ("tenant_set_agent_memory_backend", {"provider": "faultline",
                                                           "api_key_set": False, "saved": True}),
                      ("tenant_set_agent_prompt", {"system_prompt": "be terse"}),
                      ("tenant_set_agent_frontend", {"position": "corner", "theme": "clean",
                                                     "text_color": "#ffffff", "saved": True})):
        def _mk(_name=name, _out=out):
            def _fn(body=None, request=None, seat_id=None, account_id=None):
                calls.append((_name, account_id, seat_id, body))
                return dict(_out, account_id=account_id, seat_id=seat_id)
            return _fn
        monkeypatch.setattr(tenant_console, name, _mk())

    async def _bump(*a, **k):
        calls.append(("meter",) + tuple(a))
    monkeypatch.setattr(metering, "bump_for_tool", _bump)

    async def _learn(topic, user_id, source_url=None, online=False):
        calls.append(("_learn_via_llm", topic, user_id, online))
        return state["expand_result"]
    from src.mcp import server as _mcp_server
    monkeypatch.setattr(_mcp_server, "_learn_via_llm", _learn)

    # ── the transport seams (identical stubs to tests/test_agent_proxy.py) ────────────
    def _require_auth(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth or auth.endswith("bad"):
            raise HTTPException(status_code=401, detail="Unauthorized",
                                headers={"WWW-Authenticate": "Bearer"})
        return "seat-principal"

    log_lines: list[str] = []
    app = FastAPI()
    app.include_router(agent_proxy.build_agent_proxy_router(
        require_auth=_require_auth,
        resolve_user_id=lambda request, body_uid, principal: SEAT,
        account_for=lambda principal, uid: ACCT,
        log=log_lines.append,
    ))
    assert agent_control  # imported for clarity; the router is what is exercised
    return app, state, calls, log_lines


def _delegate_token(account_id=ACCT, sub="dash-1"):
    from saas import dashboard_auth
    return dashboard_auth.issue_agent_delegate(
        dashboard_user_id=sub, account_id=account_id, username="admin@example.test")


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _client(app, peer=PEER):
    """TestClient with a real IPv4 peer so the source gate has something to evaluate."""
    return TestClient(app, client=(peer, 51234))


# ════════════════════════════════════════════════════════════════════════════════════
# 1–3. SAAS gate + delegate auth + realm isolation
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/v1/agent/seats"),
    ("post", f"/v1/agent/seats/{SEAT}/bind"),
    ("post", "/v1/agent/trial"),
    ("get", f"/v1/agent/seats/{SEAT}/config"),
])
def test_control_404_when_saas_off(control, method, path):
    """1. Fail-closed off-core — the whole control surface does not exist when SAAS_MODE is off."""
    app, state, _, _ = control
    state["saas_on"] = False
    with _client(app) as c:
        r = getattr(c, method)(path, headers=_hdr(_delegate_token()))
    assert r.status_code == 404


@pytest.mark.parametrize("headers", [
    {},                                   # no credential at all
    {"Authorization": "Bearer garbage"},  # not a signed token
    {"Authorization": "flx_notbearer"},   # not even a Bearer scheme
])
def test_seats_rejects_missing_or_invalid_delegate(control, headers):
    """2. Fail-closed delegate auth — anything that is not a valid delegate token is 401."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=headers)
    assert r.status_code == 401


def test_seats_rejects_tampered_delegate(control):
    """2b. A tampered delegate token fails the signature check (verify_agent_delegate)."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token() + "tampered"))
    assert r.status_code == 401


def test_seats_rejects_tenant_session_token(control):
    """3. Realm isolation — a valid tenant SESSION token is NOT a delegate token. The salt
    namespaces are distinct and the control plane must not accept a cross-realm credential."""
    from saas import dashboard_auth
    app, _, _, _ = control
    session = dashboard_auth.issue_session(
        dashboard_user_id="dash-1", account_id=ACCT, username="admin@example.test")
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(session))
    assert r.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════════
# 8. GET /v1/agent/seats — delegates to list_agent_seats + the existing entitlement gate
# ════════════════════════════════════════════════════════════════════════════════════

def test_seats_happy_path_shape_and_delegation(control):
    """8. 200 + the exact shape faultline_client.list_seats_full consumes, sourced from
    accounts.list_agent_seats (called with the DELEGATE's account, not client input)."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert ("list_agent_seats", ACCT) in calls          # delegated, scoped to the token's account
    assert body["entitlement"] == {"state": "active", "reason": "", "message": "",
                                   "provision_url": ""}
    seat = body["seats"][0]
    # The client filters on seat_type == 'agent' and keys on seat_id — both must be present.
    assert seat["seat_id"] == SEAT and seat["seat_type"] == "agent"
    assert seat["label"] == "primary agent"
    assert seat["ingest_enabled"] is True
    assert seat["brain_configured"] is True


def test_seats_scope_comes_from_the_token_not_the_client(control):
    """7. A delegate scoped to ANOTHER account sees that account's seats (here: none) — the
    account_id is never taken from the request."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token(OTHER_ACCT)))
    assert r.status_code == 200
    assert r.json()["seats"] == []
    assert ("list_agent_seats", OTHER_ACCT) in calls


def test_seats_entitlement_blocked_when_no_agent_seat(control):
    """8b. Zero agent seats → the client's 'Add agent' trigger (blocked / no_seat)."""
    app, state, _, _ = control
    state["seats"] = []
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    ent = r.json()["entitlement"]
    assert ent["state"] == "blocked" and ent["reason"] == "no_seat"
    assert ent["provision_url"]


def test_seats_entitlement_mirrors_the_serve_gate(control):
    """8c. A refusal from the EXISTING serve gate is surfaced verbatim — the banner can never
    disagree with what /v1/agent/chat would do."""
    app, state, _, _ = control
    state["serve"] = (False, {"status": 402, "reason_code": "chatbot_trial_expired",
                              "message": "the chatbot-integration trial has ended for this account.",
                              "enable_at": "/dashboard/agent-feature"})
    with _client(app) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    ent = r.json()["entitlement"]
    assert ent["state"] == "blocked"
    assert ent["reason"] == "chatbot_trial_expired"
    assert ent["provision_url"] == "/dashboard/agent-feature"


# ════════════════════════════════════════════════════════════════════════════════════
# 5, 6, 9. POST /v1/agent/seats/{id}/bind — the existing mint, behind BOLA
# ════════════════════════════════════════════════════════════════════════════════════

def test_bind_happy_path_uses_the_existing_mint(control):
    """9. 200 + {seat_token, seat_label, brain_configured}, minted by the EXISTING
    tenant_console._mint_seat_token_payload (not a new minter)."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{SEAT}/bind", headers=_hdr(_delegate_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert ("mint", ACCT, SEAT) in calls
    assert body["seat_token"] == "flu_MINTED_SECRET"
    assert body["seat_label"] == "primary agent"
    assert body["brain_configured"] is True


def test_bind_never_logs_the_minted_token(control):
    """9b. The flu_ token is returned ONCE and never written to a log line (only its prefix)."""
    app, _, _, log_lines = control
    with _client(app) as c:
        c.post(f"/v1/agent/seats/{SEAT}/bind", headers=_hdr(_delegate_token()))
    assert not any("flu_MINTED_SECRET" in line for line in log_lines)
    assert any("flu_abcd" in line for line in log_lines)   # the prefix IS logged


def test_bind_foreign_seat_403_with_no_side_effect(control):
    """5. BOLA — a seat belonging to ANOTHER account 403s, and the token mint is NEVER reached.
    A foreign seat and a non-existent seat are indistinguishable (no existence oracle)."""
    app, state, calls, _ = control
    # The seat exists, but under a DIFFERENT account — get_seat is account-scoped, so the
    # delegate's account sees nothing, exactly as the tenant console's guard does.
    state["seat_rows"][(OTHER_ACCT, FOREIGN_SEAT)] = {
        "user_id": FOREIGN_SEAT, "seat_type": "agent", "status": "active"}
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{FOREIGN_SEAT}/bind", headers=_hdr(_delegate_token()))
    assert r.status_code == 403
    assert not any(k[0] == "mint" for k in calls), "the token mint must not run on a BOLA failure"


def test_bind_non_agent_seat_403(control):
    """6. A standard (non-agent) seat of the OWN account is still refused — the chat-agent
    control plane only ever operates on agent seats."""
    app, state, _, _ = control
    state["seat_rows"][(ACCT, SEAT)]["seat_type"] = "standard"
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{SEAT}/bind", headers=_hdr(_delegate_token()))
    assert r.status_code == 403
    assert "not an agent seat" in r.json()["detail"]


def test_bind_inactive_seat_403(control):
    """6b. A revoked/inactive agent seat is refused."""
    app, state, _, _ = control
    state["seat_rows"][(ACCT, SEAT)]["status"] = "revoked"
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{SEAT}/bind", headers=_hdr(_delegate_token()))
    assert r.status_code == 403


def test_bind_malformed_seat_id_400(control):
    """6c. A non-UUID seat id is a 400, matching _require_agent_seat_of_session."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.post("/v1/agent/seats/not-a-uuid/bind", headers=_hdr(_delegate_token()))
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════════════
# 4. GET /v1/agent/state — the SEAT-token lane (the appliance's ingest banner)
# ════════════════════════════════════════════════════════════════════════════════════

def test_state_happy_path(control):
    """4. The bound seat's ingest lever + label, via the require_auth (flu_) seam."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/state", headers=_hdr("flu_seat"))
    assert r.status_code == 200, r.text
    assert r.json() == {"object": "agent_state", "seat_id": SEAT,
                        "seat_label": "primary agent", "ingest_enabled": True}


def test_state_reports_frozen_seat(control):
    """4b. A frozen (serve-mode) seat reports ingest_enabled false — this is what turns the
    appliance's red banner on."""
    app, state, _, _ = control
    state["seat_rows"][(ACCT, SEAT)]["ingest_enabled"] = False
    with _client(app) as c:
        r = c.get("/v1/agent/state", headers=_hdr("flu_seat"))
    assert r.json()["ingest_enabled"] is False


def test_state_401_without_seat_token(control):
    """4c. No bearer → 401 from the SAME require_auth seam /v1/agent/chat uses."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/state")
    assert r.status_code == 401


def test_state_rejects_a_delegate_token_shaped_call(control):
    """4d. The seat lane is the seat lane: the stub require_auth rejects the ``…bad`` credential
    exactly as the real one rejects a non-flu_ bearer."""
    app, _, _, _ = control
    with _client(app) as c:
        r = c.get("/v1/agent/state", headers=_hdr("totally-bad"))
    assert r.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════════
# 11. POST /v1/agent/expand — the REAL learn path, behind both write gates
# ════════════════════════════════════════════════════════════════════════════════════

def test_expand_delegates_to_learn_via_llm(control):
    """11. The shipped ``_learn_via_llm`` is called with the BOLA-resolved seat; the honest
    status is 'pending' (the ontology build is fire-and-forget)."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.post("/v1/agent/expand", json={"topic": "robotics", "online": False},
                   headers=_hdr("flu_seat"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert ("_learn_via_llm", "robotics", SEAT, False) in calls


def test_expand_requires_a_topic(control):
    app, _, _, _ = control
    with _client(app) as c:
        r = c.post("/v1/agent/expand", json={"topic": "   "}, headers=_hdr("flu_seat"))
    assert r.status_code == 400


def test_expand_blocked_on_a_frozen_seat(control):
    """11b. /expand IS a write — a frozen (serve) seat is refused 409 and NO learn is scheduled."""
    app, state, calls, _ = control
    state["write_block"] = {"status": "ingest_disabled", "message": "read-only (serve mode)"}
    with _client(app) as c:
        r = c.post("/v1/agent/expand", json={"topic": "robotics"}, headers=_hdr("flu_seat"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ingest_disabled"
    assert not any(k[0] == "_learn_via_llm" for k in calls)


def test_expand_refused_when_not_entitled(control):
    """11c. The serve entitlement is checked BEFORE the write gate, same as /v1/agent/chat."""
    app, state, calls, _ = control
    state["serve"] = (False, {"status": 402, "reason_code": "agent_feature_off",
                              "message": "off", "enable_at": "/dashboard/agent-feature"})
    with _client(app) as c:
        r = c.post("/v1/agent/expand", json={"topic": "robotics"}, headers=_hdr("flu_seat"))
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "agent_feature_off"
    assert not any(k[0] == "_learn_via_llm" for k in calls)


def test_expand_surfaces_a_global_ingest_freeze(control):
    """11d. A GLOBAL backend freeze comes back from _learn_via_llm as a status, not an exception —
    surface it as an honest 409 rather than a fake 'pending' ack."""
    app, state, _, _ = control
    from src.mcp.server import _INGEST_DISABLED_STATUS
    state["expand_result"] = {"status": _INGEST_DISABLED_STATUS, "memory": "ingest is off"}
    with _client(app) as c:
        r = c.post("/v1/agent/expand", json={"topic": "robotics"}, headers=_hdr("flu_seat"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ingest_disabled"


# ════════════════════════════════════════════════════════════════════════════════════
# 10. POST /v1/agent/trial — the shared console helper
# ════════════════════════════════════════════════════════════════════════════════════

def test_trial_delegates_to_the_shared_console_helper(control):
    """10. Delegates to tenant_console.start_chatbot_trial with the DELEGATE's account, and
    reports auto_subscribe=False (SPEC §13 — the trial never auto-subscribes)."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.post("/v1/agent/trial", headers=_hdr(_delegate_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trial_active"] is True
    assert body["trial_days"] == 15
    assert body["auto_subscribe"] is False
    hit = [k for k in calls if k[0] == "start_chatbot_trial"]
    assert hit and hit[0][1] == ACCT
    # Attribution rides through to the ledger/audit as the appliance, with the source IP.
    assert hit[0][2]["source_ip"] == PEER


def test_trial_requires_a_delegate(control):
    app, _, _, _ = control
    with _client(app) as c:
        r = c.post("/v1/agent/trial")
    assert r.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════════
# 12. Per-seat CONFIG surface — the /tenant/agent/* handlers over the delegate token
# ════════════════════════════════════════════════════════════════════════════════════

def test_seat_config_read_delegates(control):
    """12. GET config → tenant_console.tenant_get_agent_seat_config with the delegate's account."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.get(f"/v1/agent/seats/{SEAT}/config", headers=_hdr(_delegate_token()))
    assert r.status_code == 200, r.text
    assert ("tenant_get_agent_seat_config", ACCT, SEAT) in calls
    assert r.json()["bound_seat"]["ingest_enabled"] is True


CONFIG_WRITES = [
    ("chat-llm", "tenant_set_agent_chat_llm",
     {"provider": "openai-compat", "endpoint": "https://llm.test", "model": "glm-5.2",
      "api_key": "sk-x"}),
    ("memory-backend", "tenant_set_agent_memory_backend", {"provider": "faultline"}),
    ("prompt", "tenant_set_agent_prompt", {"system_prompt": "be terse"}),
    ("frontend", "tenant_set_agent_frontend",
     {"position": "corner", "theme": "clean", "accent_color": "#00ff88",
      "surface_color": "#111111", "text_color": "#ffffff"}),
]


@pytest.mark.parametrize("path,fn,body", CONFIG_WRITES)
def test_config_write_delegates_to_the_console_handler(control, path, fn, body):
    """12b. Each setter reaches the EXISTING tenant-console handler with the delegate's account
    and the requested seat — including frontend.text_color, which the appliance now sets."""
    app, _, calls, _ = control
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{SEAT}/{path}", json=body, headers=_hdr(_delegate_token()))
    assert r.status_code == 200, r.text
    hit = [k for k in calls if k[0] == fn]
    assert hit, f"{fn} was not called"
    assert hit[0][1] == ACCT and hit[0][2] == SEAT


@pytest.mark.parametrize("path,fn,body", CONFIG_WRITES)
def test_config_write_bola_foreign_seat_403(control, path, fn, body):
    """5b. THE critical one — a foreign account's seat 403s on every config setter, and the
    console handler (and therefore the write) is NEVER reached."""
    app, state, calls, _ = control
    state["seat_rows"][(OTHER_ACCT, FOREIGN_SEAT)] = {
        "user_id": FOREIGN_SEAT, "seat_type": "agent", "status": "active"}
    with _client(app) as c:
        r = c.post(f"/v1/agent/seats/{FOREIGN_SEAT}/{path}", json=body,
                   headers=_hdr(_delegate_token()))
    assert r.status_code == 403
    assert not any(k[0] == fn for k in calls), f"{fn} must not run on a BOLA failure"


def test_config_read_bola_foreign_seat_403(control):
    """5c. The config READ is guarded identically — no leaking another tenant's agent config."""
    app, state, calls, _ = control
    state["seat_rows"][(OTHER_ACCT, FOREIGN_SEAT)] = {
        "user_id": FOREIGN_SEAT, "seat_type": "agent", "status": "active"}
    with _client(app) as c:
        r = c.get(f"/v1/agent/seats/{FOREIGN_SEAT}/config", headers=_hdr(_delegate_token()))
    assert r.status_code == 403
    assert not any(k[0] == "tenant_get_agent_seat_config" for k in calls)


# ════════════════════════════════════════════════════════════════════════════════════
# 13–15. The optional source-CIDR gate (production / WireGuard dedicated IP)
# ════════════════════════════════════════════════════════════════════════════════════

def test_cidr_gate_unset_is_a_noop(control, monkeypatch):
    """13. UNSET → unchanged behaviour. Pre-prod and every existing deployment keep working."""
    app, _, _, _ = control
    monkeypatch.delenv("AGENT_CONTROL_ALLOWED_CIDRS", raising=False)
    with _client(app, peer="203.0.113.9") as c:      # an address in no allow-list anywhere
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 200


def test_cidr_gate_allows_a_listed_source(control, monkeypatch):
    """14. SET + the peer inside the list → allowed."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24, 192.168.40.10")
    with _client(app, peer=PEER) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 200


def test_cidr_gate_refuses_an_unlisted_source(control, monkeypatch):
    """14b. SET + the peer outside the list → 403, BEFORE the credential is even examined."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    with _client(app, peer="203.0.113.9") as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 403
    assert "source address" in r.json()["detail"]


def test_cidr_gate_refuses_before_auth(control, monkeypatch):
    """14c. The source gate runs ahead of the token check — an out-of-policy source is never told
    whether its credential was any good."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    with _client(app, peer="203.0.113.9") as c:
        r = c.get("/v1/agent/seats")          # no credential at all
    assert r.status_code == 403               # 403 (source), not 401 (auth)


def test_cidr_gate_fails_closed_on_an_unparseable_value(control, monkeypatch):
    """14d. Configured but nothing usable (a typo'd list) → refuse everything. A misconfiguration
    must never silently reopen the door."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "not-an-ip, 999.999.999.999/12")
    with _client(app, peer=PEER) as c:
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 403


def test_cidr_gate_fails_closed_on_a_non_ip_peer(control, monkeypatch):
    """14e. An unresolvable peer address cannot be matched against the list → refuse."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    with TestClient(app) as c:                # starlette's default peer is the string "testclient"
        r = c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert r.status_code == 403


def test_cidr_gate_ignores_a_spoofed_forwarded_header(control, monkeypatch):
    """15. THE spoofing hole this gate must not have: an UNTRUSTED peer sets X-Forwarded-For to an
    allow-listed address. Without SAAS_TRUSTED_PROXY_IPS the header is ignored outright, so the
    real peer decides — and is refused."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    monkeypatch.delenv("SAAS_TRUSTED_PROXY_IPS", raising=False)
    with _client(app, peer="203.0.113.9") as c:
        r = c.get("/v1/agent/seats", headers={**_hdr(_delegate_token()),
                                              "X-Forwarded-For": "10.99.0.7"})
    assert r.status_code == 403


def test_cidr_gate_honours_forwarded_from_a_trusted_proxy(control, monkeypatch):
    """15b. The real deployment shape: Caddy is the peer and is listed in SAAS_TRUSTED_PROXY_IPS,
    so the appliance's own WireGuard address (the first XFF hop) is what the gate evaluates."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "172.18.0.0/16")
    with _client(app, peer="172.18.0.5") as c:                       # Caddy
        ok = c.get("/v1/agent/seats", headers={**_hdr(_delegate_token()),
                                               "X-Forwarded-For": "10.99.0.7, 172.18.0.5"})
        bad = c.get("/v1/agent/seats", headers={**_hdr(_delegate_token()),
                                                "X-Forwarded-For": "203.0.113.9"})
    assert ok.status_code == 200
    assert bad.status_code == 403


def test_cidr_gate_ignores_a_prepended_forwarded_hop(control, monkeypatch):
    """15c. THE Caddy-shaped spoof (SECURITY audit M3). Caddy APPENDS the address it observed
    rather than replacing the header, so a client that sends ``X-Forwarded-For: 10.99.0.7``
    arrives as ``10.99.0.7, <its real address>``. Taking the LEFTMOST hop would hand the attacker
    the allow-list; the rightmost UNTRUSTED hop is the real client, and it is refused."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "172.18.0.0/16")
    with _client(app, peer="172.18.0.5") as c:                       # Caddy
        r = c.get("/v1/agent/seats",
                  headers={**_hdr(_delegate_token()),
                           # left = spoofed by the client, right = appended by Caddy
                           "X-Forwarded-For": "10.99.0.7, 203.0.113.9"})
    assert r.status_code == 403


def test_cidr_gate_skips_chained_trusted_proxies(control, monkeypatch):
    """15d. With two proxies in front, the right-to-left walk skips the trusted hops and lands on
    the real client — so the gate does not degrade into "whatever the innermost proxy is"."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "172.18.0.0/16")
    with _client(app, peer="172.18.0.5") as c:
        r = c.get("/v1/agent/seats",
                  headers={**_hdr(_delegate_token()),
                           "X-Forwarded-For": "10.99.0.7, 172.18.0.9"})
    assert r.status_code == 200


def test_source_gate_also_guards_the_seat_lane(control, monkeypatch):
    """15c. The seat-token verbs (/state, /expand) carry the same gate — the appliance uses ONE
    dedicated address for its whole conversation with FaultLine."""
    app, _, _, _ = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    with _client(app, peer="203.0.113.9") as c:
        assert c.get("/v1/agent/state", headers=_hdr("flu_seat")).status_code == 403
        assert c.post("/v1/agent/expand", json={"topic": "x"},
                      headers=_hdr("flu_seat")).status_code == 403


# ════════════════════════════════════════════════════════════════════════════════════
# Attribution — every control action is traceable to (source IP, account, seat)
# ════════════════════════════════════════════════════════════════════════════════════

def test_control_actions_log_source_ip_account_and_seat(control):
    """Each control action emits a line carrying the three attribution facts, and no secret."""
    app, _, _, log_lines = control
    with _client(app) as c:
        c.post(f"/v1/agent/seats/{SEAT}/bind", headers=_hdr(_delegate_token()))
    line = next(x for x in log_lines if "agent_control bind" in x)
    assert f"ip={PEER}" in line
    assert ACCT[:8] in line
    assert SEAT[:8] in line


def test_refused_source_is_logged(control, monkeypatch):
    """A refused source is logged with its address so the dedicated-IP policy is auditable."""
    app, _, _, log_lines = control
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_CIDRS", "10.99.0.0/24")
    with _client(app, peer="203.0.113.9") as c:
        c.get("/v1/agent/seats", headers=_hdr(_delegate_token()))
    assert any("SOURCE REFUSED" in x and "203.0.113.9" in x for x in log_lines)
