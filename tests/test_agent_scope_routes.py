"""Scope-config + GAUNTLET route tests — session lane (tenant console) + the pure gauntlet
judge/win-loss wiring, no DB (data functions monkeypatched).

Proves the non-negotiable invariants of the CONSTRAINED customer-agent control surface added in
``saas/tenant_console.py`` (``/tenant/agent/scope`` + ``/tenant/agent/gauntlet*``):
  1. no session            → 401 (fail-closed) on every new endpoint.
  2. seat required         → structured ``needs_seat`` (HTTP 200), never a bare 403, when no
                             seat is selected (mirrors the seat-config convention).
  3. foreign seat          → 403, no side effect.
  4. CSRF                  → unsafe methods require the session-bound CSRF token.
  5. save/read round-trip  → the patched accounts.set_agent_scope_config / gauntlet config are
                             reached with the SESSION's account_id + the requested seat.
  6. gauntlet run          → run_round is reached and its streak/passed is surfaced.

Run: python3 -m pytest tests/test_agent_scope_routes.py -q (tests/ is gitignored).
"""

import pytest

from saas import dashboard_auth as auth
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # an ACTIVE agent seat of ACCT_A
USER_B = "22222222-2222-4222-8222-222222222222"   # a seat of ACCT_B (NOT of A)
MISSING = "33333333-3333-4333-8333-333333333333"  # not a seat of A


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=USER_A)


def _login(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    return {"x-csrf-token": _csrf_a()}


def _seats_session_a():
    from fastapi import Request
    def _dep(request: Request) -> str:
        return _session_a()
    return _dep


@pytest.fixture
def seat_ownership(monkeypatch):
    """USER_A is an ACTIVE AGENT seat of ACCT_A only; USER_B belongs to ACCT_B; MISSING is nobody's."""
    def _get_seat(account_id, user_id):
        if (account_id, user_id) == (ACCT_A, USER_A):
            return {"user_id": USER_A, "seat_type": "agent", "status": "active",
                    "ingest_enabled": True, "label": "primary agent"}
        if (account_id, user_id) == (ACCT_B, USER_B):
            return {"user_id": USER_B, "seat_type": "agent", "status": "active",
                    "ingest_enabled": True, "label": "other account seat"}
        return None
    monkeypatch.setattr(tc._seats, "get_seat", _get_seat)


# ── the data-layer functions under test ───────────────────────────────────────────────

_scope_calls = []
_gauntlet_calls = []
_run_calls = []


@pytest.fixture(autouse=True)
def control_data(monkeypatch):
    _scope_calls.clear()
    _gauntlet_calls.clear()
    _run_calls.clear()

    def _get_scope(aid, sid):
        _scope_calls.append(("get_scope", aid, sid))
        return {"enabled": True, "allowed_topics": ["hours"], "forbidden_topics": [],
                "redirect_target": "https://x.example", "escalation_triggers": []}
    monkeypatch.setattr(tc._accounts, "get_agent_scope_config", _get_scope)

    def _set_scope(aid, sid, **kw):
        _scope_calls.append(("set_scope", aid, sid, kw))
        return {"enabled": bool(kw.get("enabled")), "allowed_topics": list(kw.get("allowed_topics") or []),
                "forbidden_topics": list(kw.get("forbidden_topics") or []),
                "redirect_target": str(kw.get("redirect_target") or ""),
                "escalation_triggers": list(kw.get("escalation_triggers") or [])}
    monkeypatch.setattr(tc._accounts, "set_agent_scope_config", _set_scope)

    def _get_gauntlet(aid, sid):
        _gauntlet_calls.append(("get_gauntlet", aid, sid))
        return {"enabled": True, "reference_url": "", "gold_answers": [], "passes_required": 3}
    monkeypatch.setattr(tc._accounts, "get_agent_gauntlet_config", _get_gauntlet)

    def _set_gauntlet(aid, sid, **kw):
        _gauntlet_calls.append(("set_gauntlet", aid, sid, kw))
        return {"enabled": bool(kw.get("enabled")), "reference_url": str(kw.get("reference_url") or ""),
                "gold_answers": list(kw.get("gold_answers") or []),
                "passes_required": int(kw.get("passes_required") or 3)}
    monkeypatch.setattr(tc._accounts, "set_agent_gauntlet_config", _set_gauntlet)

    async def _run_round(aid, sid, **kw):
        _run_calls.append(("run_round", aid, sid))
        return {"ok": True, "round_no": 1, "question": "q", "agent_won": True,
                "judge_verdict": "agent", "gap_feedback": None, "streak": 1,
                "passes_required": 3, "passed": False}
    from saas import agent_gauntlet as _gg
    monkeypatch.setattr(_gg, "run_round", _run_round)

    # CHAT-AGENT ENTITLEMENT (the "hinged on the Agent" gate): default ALLOW so the happy-path
    # run test reaches run_round; gauntlet entitlement tests flip this to refuse.
    _entitled = {"yes": True}
    from saas import agent_entitlement as _ae_mod
    monkeypatch.setattr(_ae_mod, "serve_allowed",
                        lambda aid, sid: (_entitled["yes"], None))
    yield _entitled


# ════════════════════════════════════════════════════════════════════════════════════
# no session → 401 (fail-closed) on every new endpoint
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/agent/scope"),
    ("post", "/tenant/agent/scope"),
    ("get", "/tenant/agent/gauntlet"),
    ("post", "/tenant/agent/gauntlet"),
    ("post", "/tenant/agent/gauntlet/run"),
    ("get", "/tenant/agent/gauntlet/rounds"),
])
def test_no_session_401(client, method, path):
    r = getattr(client, method)(path, params={"seat_id": USER_A})
    assert r.status_code == 401


# ════════════════════════════════════════════════════════════════════════════════════
# no seat selected → structured needs_seat (HTTP 200), never a bare 403
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method,path", [
    ("get", "/tenant/agent/scope"),
    ("get", "/tenant/agent/gauntlet"),
])
def test_scope_and_gauntlet_missing_seat_is_needs_seat(client, seat_ownership, method, path):
    headers = _login(client)
    r = getattr(client, method)(path, params={"seat_id": ""}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body.get("needs_seat") is True


# ════════════════════════════════════════════════════════════════════════════════════
# foreign seat → 403, and NO side effect (the data setter is never reached)
# ════════════════════════════════════════════════════════════════════════════════════

def test_scope_post_foreign_seat_403_no_side_effect(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/scope", params={"seat_id": USER_B},
                    json={"enabled": True},
                    headers=headers)
    assert r.status_code == 403
    assert _scope_calls == []  # the setter was NEVER reached — BOLA held


def test_scope_post_missing_seat_403_no_side_effect(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/scope", params={"seat_id": MISSING},
                    json={"enabled": True},
                    headers=headers)
    assert r.status_code == 403
    assert _scope_calls == []


def test_gauntlet_post_foreign_seat_403_no_side_effect(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/gauntlet", params={"seat_id": USER_B},
                    json={"enabled": True},
                    headers=headers)
    assert r.status_code == 403
    assert _gauntlet_calls == []


# ════════════════════════════════════════════════════════════════════════════════════
# CSRF — unsafe methods require the session-bound token
# ════════════════════════════════════════════════════════════════════════════════════

def test_scope_post_without_csrf_403(client, seat_ownership):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.post("/tenant/agent/scope", params={"seat_id": USER_A},
                    json={"enabled": True})
    assert r.status_code == 403
    assert _scope_calls == []


# ════════════════════════════════════════════════════════════════════════════════════
# happy path — the setter is reached with the SESSION account + requested seat
# ════════════════════════════════════════════════════════════════════════════════════

def test_scope_post_saves_with_session_account(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/scope", params={"seat_id": USER_A},
                    json={"enabled": True, "allowed_topics": ["hours", "delivery"],
                          "forbidden_topics": ["pricing"],
                          "redirect_target": "https://optimus.example/contact",
                          "escalation_triggers": ["sponsorship"]},
                    headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == ACCT_A
    assert body["seat_id"] == USER_A
    assert body["saved"] is True
    assert body["allowed_topics"] == ["hours", "delivery"]
    # The setter was called with the SESSION's account, never a client-supplied one.
    assert _scope_calls[0][:2] == ("set_scope", ACCT_A)


def test_scope_get_reads_with_session_account(client, seat_ownership):
    headers = _login(client)
    r = client.get("/tenant/agent/scope", params={"seat_id": USER_A}, headers=headers)
    assert r.status_code == 200
    assert r.json()["account_id"] == ACCT_A
    assert _scope_calls[0][:2] == ("get_scope", ACCT_A)


def test_gauntlet_post_saves(client, seat_ownership, monkeypatch):
    headers = _login(client)
    # Reference URLs are SSRF-guarded; use a benign public-looking host for the test.
    r = client.post("/tenant/agent/gauntlet", params={"seat_id": USER_A},
                    json={"enabled": True, "reference_url": "https://www.optimuscannabis.ca",
                          "passes_required": 3},
                    headers=headers)
    assert r.status_code == 200
    assert r.json()["account_id"] == ACCT_A
    assert _gauntlet_calls[0][:2] == ("set_gauntlet", ACCT_A)


def test_gauntlet_run_reaches_run_round(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/gauntlet/run", params={"seat_id": USER_A}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert _run_calls[0][:2] == ("run_round", ACCT_A)
    assert body.get("agent_won") is True
    assert body.get("streak") == 1
    assert body.get("passed") is False


def test_gauntlet_run_refused_without_chat_agent_feature(client, seat_ownership, control_data):
    """The critic is HINGED ON THE AGENT + CHAT-AGENT: a live agent seat WITHOUT an active
    chat-agent feature must NOT run the gauntlet (it would burn the seat brain's tokens)."""
    control_data["yes"] = False   # flip the chat-agent entitlement to refuse
    headers = _login(client)
    r = client.post("/tenant/agent/gauntlet/run", params={"seat_id": USER_A}, headers=headers)
    assert r.status_code == 402
    assert _run_calls == [], "run_round must NOT be reached without the chat-agent feature"


def test_gauntlet_rounds_log(client, seat_ownership, monkeypatch):
    from saas import agent_gauntlet as _gg
    monkeypatch.setattr(_gg, "recent_rounds", lambda aid, sid, limit=25: [
        {"round_no": 2, "question": "q", "agent_won": False, "agent_answer": "a",
         "reference_answer": "r", "judge_verdict": "reference", "gap_feedback": "g",
         "cortex_usage": {"recall_calls": 1}, "hiccups": {}},
    ])
    monkeypatch.setattr(_gg, "consecutive_wins",
                        lambda aid, sid: {"count": 0, "passes_required": 3,
                                          "passed": False, "total_rounds": 1})
    headers = _login(client)
    r = client.get("/tenant/agent/gauntlet/rounds", params={"seat_id": USER_A}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["rounds"]) == 1
    assert body["streak"]["passed"] is False


# ════════════════════════════════════════════════════════════════════════════════
# AutoScope — POST /tenant/agent/scope/auto (draft from the agent's training)
# ════════════════════════════════════════════════════════════════════════════════

_auto_calls = []


@pytest.fixture(autouse=True)
def auto_scope_patch(monkeypatch):
    _auto_calls.clear()
    async def _draft(aid, sid):
        _auto_calls.append(("draft_scope", aid, sid))
        return {"status": "draft", "draft": {
            "allowed_topics": ["free consultations", "delivery"],
            "forbidden_topics": ["pricing", "bulk orders"],
            "redirect_target": "https://optimus.example/contact",
            "escalation_triggers": ["sponsorship"],
        }, "grounded_chars": 250}
    from saas import agent_autoscope as _aa
    monkeypatch.setattr(_aa, "draft_scope", _draft)


def test_scope_auto_no_session_401(client):
    r = client.post("/tenant/agent/scope/auto", params={"seat_id": USER_A})
    assert r.status_code == 401


def test_scope_auto_foreign_seat_403_no_side_effect(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/scope/auto", params={"seat_id": USER_B}, headers=headers)
    assert r.status_code == 403
    assert _auto_calls == []


def test_scope_auto_drafts_and_does_not_save(client, seat_ownership):
    headers = _login(client)
    r = client.post("/tenant/agent/scope/auto", params={"seat_id": USER_A}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert _auto_calls[0][:2] == ("draft_scope", ACCT_A)
    assert body["status"] == "draft"
    assert body["draft"]["allowed_topics"] == ["free consultations", "delivery"]
    # AutoScope returns a DRAFT — it must NOT have written anything.
    assert _scope_calls == [] or all(c[0] == "get_scope" for c in _scope_calls)
    assert not any(c[0] == "set_scope" for c in _scope_calls)
