"""CSRF token DELIVERY on the individual ("My Memory") console — the signup→save journey.

THE BUG (live on production, blocking every new signup): a brand-new account created through
faultline.ca could not save an LLM endpoint — ``403 missing or invalid CSRF token``. Binding a
brain is step 1 of onboarding and, under the CPU-cloud fail-closed model, a seat with no brain
gets empty results from everything. So the account was permanently useless, and the error
pointed at security rather than at anything the user could act on.

ROOT CAUSE — a DELIVERY hole, not a verification bug. ``POST /me/signup`` returned the CSRF
token in its response body, and ``require_user_session`` verified tokens correctly. But
``saas/landing/signup.js`` read that body for ``status`` only and DISCARDED the token, then
navigated to the console; ``user.js::restoreSession`` re-hydrates the token from
``sessionStorage['fl_user_csrf']``, found nothing, set ``S.csrf = ''`` — and ``api()`` omits the
``x-csrf-token`` header entirely when ``S.csrf`` is falsy. Every write from that session went
out with NO header. The console's OWN signup step (``user.js::doSignup``) always persisted it,
which is why signup-from-the-console never showed the bug.

WHY IT TAKES THIS HARNESS. Every seam in isolation was correct: the token was issued, it was
verifiable, the header was attached when a token existed. Nothing but running the REAL pages in
order can see that no token ever reached the console. A unit test on the verifier proves the
lock works; it says nothing about whether the browser was handed a key.

WHAT IS REAL / WHAT IS NOT. Real: the shipped ``signup.html`` + ``signup.js`` and
``user.html`` + ``user.js``, a real HTTP server running the real FastAPI router, the real
itsdangerous signing, the real session cookie, the real ``x-csrf-token`` header, and the real
``require_user_session`` gate. Stubbed: the control-plane DB functions (there is no Postgres
here) and jsdom's missing ``fetch``/cookie-jar. NOTHING on the CSRF path is stubbed.

⚠️ jsdom IS NOT A BROWSER — no layout, no CSS, no real navigation.

tests/ is gitignored (git add -f).
Run: python3 tools/fltest.py --bug CSRFSIGNUP --test tests/test_saas_individual_csrf_delivery.py
"""

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import dashboard_auth as auth
from saas import user_console as uc


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "dashboard" / "render_signup_journey.js"

ACCT = "5111e5-0000-4000-8000-000000000001"
IND_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
PASSWORD = "correct-horse-battery-staple"
# A PUBLIC address — the SSRF egress guard legitimately blocks private/loopback targets, and
# that guard is NOT what this test is about. (The reported failure was CSRF, not egress.)
LLM_URL = "http://142.198.45.51:8080"


def _individual(**over):
    row = {
        "id": IND_ID, "user_id": USER_ID, "account_id": ACCT,
        "email": "journey@example.com", "password_hash": None, "totp_secret": None,
        "mfa_enabled": False, "display_name": "CSRF Journey", "plan": "individual",
        "status": "active", "tenant_context": "none", "can_control_endpoint": True,
        "transfer_locked": False,
    }
    row.update(over)
    return row


@pytest.fixture
def saas_stubs(monkeypatch):
    """SAAS on + the control-plane DB functions stubbed. The auth/CSRF path stays REAL."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "csrf-journey-secret-0123456789abcdef")

    state = {"row": None, "brain": None}

    def _create(*, user_id, account_id, email, password_hash, display_name):
        state["row"] = _individual(user_id=user_id, account_id=account_id, email=email,
                                   password_hash=password_hash, display_name=display_name)
        return IND_ID

    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(uc._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(uc._ratelimit, "login_locked", lambda e: (False, 0))
    monkeypatch.setattr(uc._ratelimit, "record_login_failure", lambda e: None)
    monkeypatch.setattr(uc._ratelimit, "record_login_success", lambda e: None)
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt", lambda *a, **k: None)
    monkeypatch.setattr(uc, "_email_taken", lambda e: False)
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", lambda: ACCT)
    monkeypatch.setattr(uc, "_provision_individual_seat", lambda a, u: {"ok": True})
    monkeypatch.setattr(uc, "_create_individual", _create)
    monkeypatch.setattr(uc, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(uc._tokens, "mint_user_token", lambda a, u: None)
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda i: {"delivery": "skipped"})
    monkeypatch.setattr(uc, "_retention_notice", lambda: "test retention notice")
    monkeypatch.setattr(uc, "_voice_state", lambda uid: (None, "personal"))
    monkeypatch.setattr(uc, "get_individual_by_id",
                        lambda i: state["row"] if str(i) == IND_ID else None)
    monkeypatch.setattr(uc, "get_individual_by_email",
                        lambda e: state["row"] if state["row"] and
                        state["row"]["email"] == str(e).lower() else None)
    monkeypatch.setattr(uc.M, "require_mfa_decision", lambda realm, i: False)
    # The BYO-brain write: captured, not persisted. The egress guard + crypto are separate
    # controls with their own tests; this test is about whether the write is ALLOWED to happen.
    monkeypatch.setattr(uc, "_brain_upsert",
                        lambda ind, fields: state.__setitem__("brain", dict(fields)))
    monkeypatch.setattr(uc, "_brain_get_redacted", lambda ind: None)
    monkeypatch.setattr(uc, "_brain_seat_scoped", lambda ind: True)
    return state


@pytest.fixture
def app(saas_stubs):
    a = FastAPI()
    a.include_router(uc.router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def live_server(app):
    """The real router on a real port — jsdom cannot talk to an in-process TestClient."""
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    if not getattr(server, "started", False):
        pytest.skip("uvicorn did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _journey(base, mode):
    if not shutil.which("node") or not (ROOT / "tests/dashboard/node_modules/jsdom").exists():
        pytest.skip("node/jsdom harness unavailable")
    email = f"journey-{uuid.uuid4().hex[:8]}@example.com"
    out = subprocess.run(
        ["node", str(HARNESS), base, email, PASSWORD, LLM_URL, mode],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT / "tests/dashboard"))
    if out.returncode != 0:
        pytest.fail(f"jsdom journey failed:\n{out.stderr[-4000:]}")
    return json.loads(out.stdout)


# ══════════════════════════════════════════════════════════════════════════════════
# THE REPORTED JOURNEY — sign up, then save a brain WITHOUT logging in again
# ══════════════════════════════════════════════════════════════════════════════════

def test_signup_then_save_llm_endpoint_without_relogin(live_server, saas_stubs):
    r = _journey(live_server, "signup")
    detail = json.dumps(r, indent=2)

    assert r["signup_request"]["status"] == 200, detail
    assert r["session_cookie_set"], detail
    # The hand-off itself: the signup page must PARK the token where the console looks.
    assert r["signup_stored_csrf"], (
        "signup.js did not store the CSRF token — the console will send no header\n" + detail)
    assert r["console_signed_in"], detail

    puts = r["put_llm_endpoint"]
    assert puts, "the console never issued PUT /me/llm-endpoint\n" + detail
    assert puts[-1]["sent_csrf_header"], "PUT went out with NO x-csrf-token header\n" + detail
    assert r["save_status"] == 200, detail
    assert "saved" in r["user_visible_message"].lower(), detail
    assert saas_stubs["brain"] and saas_stubs["brain"].get("base_url") == LLM_URL, detail


def test_login_then_save_llm_endpoint(live_server, saas_stubs):
    """The control arm: fixing signup must not break the path that already worked."""
    r = _journey(live_server, "login")
    detail = json.dumps(r, indent=2)

    assert r["precreate_status"] == 200, detail
    assert r["console_signed_in"], detail
    puts = r["put_llm_endpoint"]
    assert puts and puts[-1]["sent_csrf_header"], detail
    assert r["save_status"] == 200, detail
    assert saas_stubs["brain"] and saas_stubs["brain"].get("base_url") == LLM_URL, detail


# ══════════════════════════════════════════════════════════════════════════════════
# CSRF IS STILL STRICTLY REQUIRED — the fix delivers the token, it never relaxes the check
# ══════════════════════════════════════════════════════════════════════════════════

def _signup(client):
    email = f"api-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/me/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def test_write_without_csrf_header_is_still_403(client):
    _signup(client)                     # cookie is live in the client jar
    r = client.put("/me/llm-endpoint", json={"base_url": LLM_URL})
    assert r.status_code == 403


def test_write_with_a_foreign_csrf_token_is_still_403(client):
    _signup(client)
    foreign = auth.issue_individual_csrf(individual_id="99999999-9999-4999-8999-999999999999")
    r = client.put("/me/llm-endpoint", json={"base_url": LLM_URL},
                   headers={"x-csrf-token": foreign})
    assert r.status_code == 403


def test_tenant_realm_csrf_cannot_satisfy_the_individual_realm(client):
    """Realm separation is a salt-namespace property — the new classifier must not blur it."""
    _signup(client)
    cross = auth.issue_csrf(dashboard_user_id=IND_ID)          # TENANT realm, same principal id
    r = client.put("/me/llm-endpoint", json={"base_url": LLM_URL},
                   headers={"x-csrf-token": cross})
    assert r.status_code == 403


def test_a_valid_csrf_token_is_accepted(client):
    csrf = _signup(client)
    r = client.put("/me/llm-endpoint", json={"base_url": LLM_URL},
                   headers={"x-csrf-token": csrf})
    assert r.status_code == 200, r.text


# ══════════════════════════════════════════════════════════════════════════════════
# GET /me/csrf — the delivery route
# ══════════════════════════════════════════════════════════════════════════════════

def test_csrf_route_requires_a_session(client):
    """No session → no token. This is the property that keeps the route from being a hole."""
    assert client.get("/me/csrf").status_code == 401


def test_csrf_route_mints_a_token_that_the_write_gate_accepts(client):
    _signup(client)
    got = client.get("/me/csrf")
    assert got.status_code == 200
    r = client.put("/me/llm-endpoint", json={"base_url": LLM_URL},
                   headers={"x-csrf-token": got.json()["csrf_token"]})
    assert r.status_code == 200, r.text


def test_csrf_route_binds_to_the_session_principal_not_client_input(client):
    """BOLA: the token is bound to the SIGNED session's id. A query/body hint changes nothing."""
    _signup(client)
    tok = client.get("/me/csrf?individual_id=99999999-9999-4999-8999-999999999999").json()["csrf_token"]
    assert auth.verify_individual_csrf(tok, individual_id=IND_ID)
    assert not auth.verify_individual_csrf(
        tok, individual_id="99999999-9999-4999-8999-999999999999")


# ══════════════════════════════════════════════════════════════════════════════════
# DIAGNOSABILITY — a rejection names its reason (never the token) and tells the user what to do
# ══════════════════════════════════════════════════════════════════════════════════

def test_rejection_reason_is_logged_and_never_the_token(client, capsys):
    csrf = _signup(client)
    client.put("/me/llm-endpoint", json={"base_url": LLM_URL})                       # absent
    client.put("/me/llm-endpoint", json={"base_url": LLM_URL},
               headers={"x-csrf-token": "not-a-token"})                              # invalid
    err = capsys.readouterr().err
    assert "csrf REJECT reason=absent" in err, err
    assert "csrf REJECT reason=invalid" in err, err
    assert csrf not in err, "the token VALUE leaked into the log"


def test_rejection_message_is_actionable_for_a_human(client):
    _signup(client)
    d = client.put("/me/llm-endpoint", json={"base_url": LLM_URL}).json()["detail"]
    assert "CSRF" in d                       # the console keys its one retry off this marker
    assert "reload" in d.lower() or "sign in" in d.lower()
    assert d != "missing or invalid CSRF token"


@pytest.mark.parametrize("reason,expect", [
    ("absent", auth.CSRF_ABSENT), ("garbage", auth.CSRF_INVALID),
])
def test_classifier_vocabulary(reason, expect, saas_stubs):
    token = "" if reason == "absent" else "garbage.token.value"
    assert auth.classify_individual_csrf(token, individual_id=IND_ID) == expect


def test_classifier_separates_expired_from_forged(saas_stubs, monkeypatch):
    """A genuine-but-stale token must not be reported as tampering — the user needs the truth."""
    tok = auth.issue_individual_csrf(individual_id=IND_ID)
    assert auth.classify_individual_csrf(tok, individual_id=IND_ID) == auth.CSRF_OK
    monkeypatch.setattr(auth, "CSRF_TTL_SECONDS", -1)     # everything is now stale
    assert auth.classify_individual_csrf(tok, individual_id=IND_ID) == auth.CSRF_EXPIRED
    assert auth.classify_individual_csrf("garbage", individual_id=IND_ID) == auth.CSRF_INVALID


def test_classifier_reports_a_principal_mismatch(saas_stubs):
    tok = auth.issue_individual_csrf(individual_id="99999999-9999-4999-8999-999999999999")
    assert auth.classify_individual_csrf(tok, individual_id=IND_ID) == auth.CSRF_MISMATCH


def test_classifier_never_decides_admission(saas_stubs):
    """It is diagnostic ONLY: whatever it says, verify_individual_csrf is what gates."""
    tok = auth.issue_individual_csrf(individual_id=IND_ID)
    assert auth.verify_individual_csrf(tok, individual_id=IND_ID) is True
    assert auth.verify_individual_csrf("garbage", individual_id=IND_ID) is False


# ══════════════════════════════════════════════════════════════════════════════════
# SAAS OFF — the whole realm stays invisible
# ══════════════════════════════════════════════════════════════════════════════════

def test_saas_off_hides_the_csrf_route(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    a = FastAPI()
    a.include_router(uc.router)
    assert TestClient(a).get("/me/csrf").status_code in (401, 404)
