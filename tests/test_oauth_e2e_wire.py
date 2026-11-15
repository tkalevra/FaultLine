"""Wire-level OAuth E2E — real HTTP, real redirects, real cookies, fake provider.

Complements tests/test_oauth_signin.py (route pins): this drives the FULL round trip
over the wire — GET /start (303 + Set-Cookie on the redirect) -> fake provider
authorize (302 back with code+state) -> GET /callback (state binding over real cookie
jar, code exchange against the fake token endpoint, userinfo) -> session cookie +
#oauth=ok redirect. Only the provider's URLs are pointed at the local fake; every
FaultLine seam is the real one.

Run: python3 -m pytest tests/test_oauth_e2e_wire.py -q
"""

import inspect
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from saas import user_console as uc
from saas import oauth_login as ol

def make_provision_seat_stub(recorder):
    """SIGNATURE-FAITHFUL stub for saas/user_console.py::_provision_individual_seat.

    House style: a monkeypatch stub must mirror the real signature, because the
    production call site passes seat_type= as a KWARG — a two-positional-param
    lambda raises TypeError mid-flow and the wire test rots (BUGS/
    oauth-e2e-wire-seat-type-drift-20261031). The explicit mirrored signature (not
    *_a, **_k) keeps the stub honest about WHAT it accepts; the recorder keeps the
    assertion honest about the call HAPPENING with the expected account/user.
    test_provision_seat_stub_signature_tracks_real pins the mirror so a FUTURE
    parameter addition fails fast BY NAME, not as a TypeError inside the round trip.
    """

    def _provision_individual_seat(account_id, user_id, seat_type=None):
        recorder.append({"account_id": account_id, "user_id": user_id,
                         "seat_type": seat_type})
        return {"ok": True, "reason": ""}

    return _provision_individual_seat


def test_provision_seat_stub_signature_tracks_real():
    """Drift pin: the real function's signature must not grow a parameter the stub
    does not accept. Fails with the NAMED parameter, never a TypeError mid-flow."""
    from saas.user_console import _provision_individual_seat as real
    stub = make_provision_seat_stub([])
    real_params = inspect.signature(real).parameters
    stub_params = inspect.signature(stub).parameters
    missing = [name for name in real_params if name not in stub_params]
    assert not missing, (
        "saas/user_console.py::_provision_individual_seat grew parameter(s) "
        "{" + repr(missing) + "} that the make_provision_seat_stub stub does not accept — "
        "update the stub (tests/test_oauth_e2e_wire.py) to mirror the signature")
    # and the defaults must stay compatible where the stub declares them
    for name in real_params:
        if name in stub_params:
            assert stub_params[name].default == real_params[name].default, (
                "stub default for " + repr(name) + " drifted from the real function's")


BASE_PORT = 48711
PROV_PORT = 48712
APP = f"http://127.0.0.1:{BASE_PORT}"
PROV = f"http://127.0.0.1:{PROV_PORT}"


@pytest.fixture(scope="module")
def app_server(monkeypatch_session):
    app = FastAPI()
    app.include_router(uc.router)
    config = uvicorn.Config(app, host="127.0.0.1", port=BASE_PORT, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            httpx.get(APP + "/me/oauth/providers", timeout=1.0)
            break
        except Exception:
            time.sleep(0.1)
    yield app
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="module")
def monkeypatch_session(request):
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    request.addfinalizer(mp.undo)
    return mp


@pytest.fixture(scope="module")
def provider_server(monkeypatch_session):
    """A minimal Google-shaped provider: authorize -> 302 back with code+state; token ->
    JSON access_token; userinfo -> verified email. Records the exchanged code_verifier."""
    seen = {}
    papp = FastAPI()

    @papp.get("/authorize")
    def authorize(request: Request):
        q = request.query_params
        seen["challenge"] = q.get("code_challenge")
        seen["method"] = q.get("code_challenge_method")
        state = q.get("state")
        return RedirectResponse(
            f"{APP}/me/oauth/google/callback?code=fake-code&state={state}", status_code=302)

    @papp.post("/token")
    async def token(request: Request):
        form = await request.form()
        seen["verifier"] = form.get("code_verifier")
        seen["redirect_uri"] = form.get("redirect_uri")
        return {"access_token": "fake-access-token"}

    @papp.get("/userinfo")
    def userinfo():
        return {"email": "e2e@example.com", "email_verified": True,
                "name": "E2E Walker", "sub": "e2e-sub"}

    config = uvicorn.Config(papp, host="127.0.0.1", port=PROV_PORT, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            httpx.get(PROV + "/userinfo", timeout=1.0)
            break
        except Exception:
            time.sleep(0.1)

    # point the REAL google provider at the fake (URLs only — logic untouched)
    mp = monkeypatch_session
    mp.setenv("SAAS_OAUTH_GOOGLE_CLIENT_ID", "e2e-client")
    mp.setenv("SAAS_OAUTH_GOOGLE_CLIENT_SECRET", "e2e-secret")
    mp.setattr(ol, "redirect_base", lambda: APP)
    prov = ol.get_provider("google")
    prov.authorize_url = PROV + "/authorize"
    prov.token_url = PROV + "/token"
    prov._userinfo = (lambda p, tok: {"email": "e2e@example.com", "name": "E2E Walker",
                                      "provider_account": "e2e-sub"})
    # the routes re-resolve get_provider() per request (fresh instance from env), so the
    # FAKE-URLed instance must be handed out by the resolver itself
    mp.setattr(ol, "get_provider", lambda key: prov if (key or "").lower() == "google" else None)
    yield seen
    server.should_exit = True
    t.join(timeout=5)


def test_full_round_trip_over_real_http(app_server, provider_server, monkeypatch):
    seen = provider_server
    # fresh mailbox: no individual, signup path with approval OFF provisions + session
    monkeypatch.setattr(uc, "get_individual_by_email", lambda e: None)
    monkeypatch.setattr(uc, "_email_taken", lambda e: False)
    monkeypatch.setattr(uc._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(uc._name_guard, "check_display_name",
                        lambda dn: type("D", (), {"ok": True})())
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e: type("F", (), {"allowed": True})())
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"degraded": False, "banned": False})
    monkeypatch.setattr(uc._signup_ledger, "decide",
                        lambda e: uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt",
                        lambda e, outcome, ip: None)
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval",
                        lambda: False)
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", lambda: "s-1")
    provisioned = []
    monkeypatch.setattr(uc, "_provision_individual_seat",
                        make_provision_seat_stub(provisioned))
    monkeypatch.setattr(uc, "_create_individual", lambda **kw: "e2e-ind")
    monkeypatch.setattr(uc, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda t: {"delivery": "none"})
    monkeypatch.setattr(uc._auth, "hash_password", lambda pw: "argon2-fake")
    created = {}
    monkeypatch.setattr(uc._uuid, "uuid4", lambda: uuid.UUID("12345678123456781234567812345678"))

    with httpx.Client(follow_redirects=False) as c:
        # 1) /start — real 303 + real Set-Cookie
        r1 = c.get(APP + "/me/oauth/google/start")
        assert r1.status_code == 303
        assert r1.headers["location"].startswith(PROV + "/authorize")

        # 2) the provider redirect back (real 302 with code+state)
        r2 = c.get(r1.headers["location"])
        assert r2.status_code == 302
        assert r2.headers["location"].startswith(APP + "/me/oauth/google/callback")

        # 3) /callback with the browser's REAL cookie jar (httpx carries the state cookie)
        r3 = c.get(r2.headers["location"])
        assert r3.status_code == 303, r3.headers.get("location")
        assert r3.headers["location"].endswith("#oauth=ok")
        setc = r3.headers.get("set-cookie", "")
        assert uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME in setc

        # 4) PKCE travelled end-to-end: challenge at authorize, verifier at token,
        #    and they MATCH (the fake provider recorded both)
        import base64, hashlib
        v, ch = seen.get("verifier"), seen.get("challenge")
        assert v and ch
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert ch == expect
        assert seen.get("method") == "S256"
        assert seen.get("redirect_uri") == APP + "/me/oauth/google/callback"

        # 5) the issued session cookie verifies as a REAL individual session
        cookie_val = [p for p in setc.split(";")
                      if p.strip().startswith(uc._auth.INDIVIDUAL_SESSION_COOKIE_NAME + "=")]
        token = cookie_val[0].split("=", 1)[1].strip('"')
        payload = uc._auth.verify_individual_session(token)
        assert payload and payload.get("eml") == "e2e@example.com"

        # 6) the seat WAS provisioned, over the stub, with the expected account/user
        #    (signup path, approval OFF; state carries no tier -> seat_type is None)
        assert len(provisioned) == 1, provisioned
        call = provisioned[0]
        assert call["account_id"] == "s-1"
        assert uuid.UUID(call["user_id"])  # the fresh uuid4 we pinned above
        assert call["seat_type"] is None
