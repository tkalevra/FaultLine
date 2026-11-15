"""SAME-PAGE CROSS-ORIGIN LOGIN (BUGS/same-page-login-20261102) - CORS pin suite.

Pins the transport contract of UnifiedAuthCorsMiddleware (saas/unified_login.py) so the
marketing card at faultline.ca can POST the unified sign-in door cross-origin WITHOUT
widening anything else. One pin per lane constraint:

  1. EXACT-ORIGIN allowlist only (faultline.ca, www.faultline.ca). No wildcard, and an
     attacker Origin receives NO Access-Control-* header at all - even when an inner
     wide-reflection layer (the prod _dynamic_cors analogue) would have reflected it.
  2. Allow-Credentials: true; Methods exactly "POST, OPTIONS"; Headers exactly
     "Content-Type"; Vary: Origin + Cache-Control: no-store on every CORS response -
     200, 401, 422 AND preflight OPTIONS.
  3. Path scoping: /me/login and /query carry no ACAO even with an allowlisted Origin.
  4. Session issuing untouched: this suite never asserts on cookies; the realm-native
     issuing is pinned by tests/test_saas_unified_login.py (label/flow pins).
  5. no-store on the three doors (with Origin present).
  6. Origin ABSENT -> byte-identical to an app WITHOUT the middleware (status, body,
     and the full header set).
  7. JSON-only bodies: a form-encoded POST gets FastAPI's 422 before any credential is
     read - third-party origins cannot simple-POST the door.
"""
import pytest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from saas import unified_login

_TEST_SIGNING_SECRET = 'unit-test-only-not-a-real-secret-unified-cors'
_ALLOWED = 'https://faultline.ca'
_ALLOWED_WWW = 'https://www.faultline.ca'
_ATTACKER = 'https://attacker.example'
_DOORS = ("/auth/unified-login", "/auth/unified-grant/validate", "/auth/unified-grant/complete")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", _TEST_SIGNING_SECRET)
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", "unit-test-only-not-a-real-cred-key")


@contextmanager
def patch_stack():
    """Both realm probes miss -> the SAME generic 401 both native doors use."""
    with ExitStack() as stack:
        for target, value in [
            (unified_login, "_get_individual_by_email"),
            (unified_login._auth, "get_user_by_username"),
            (unified_login, "_parity_verify"),
        ]:
            stack.enter_context(patch.object(target, value, return_value=None))
        stack.enter_context(
            patch.object(unified_login._ratelimit, "login_locked", return_value=(False, 0)))
        stack.enter_context(patch.object(unified_login._ratelimit, "record_login_failure"))
        yield


def _build_app(inner_reflection=False):
    """The unified router behind the strict middleware. inner_reflection mimics the
    prod-wide per-tenant _dynamic_cors layer (reflects ANY origin) so the pin proves
    the strict middleware is the final word on these paths."""
    app = FastAPI()

    @app.middleware("http")
    async def _wide_reflection(request, call_next):  # noqa: ANN001, ANN201
        response = await call_next(request)
        origin = request.headers.get("origin")
        if origin and inner_reflection:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    app.add_middleware(unified_login.UnifiedAuthCorsMiddleware)
    app.include_router(unified_login.router)

    @app.post("/me/login")
    def _me_login_stub():  # pragma: no cover - transport stub, never credential logic
        return JSONResponse({"ok": True})

    @app.post("/query")
    def _query_stub():  # pragma: no cover
        return JSONResponse({"ok": True})
    return app


def _build_app_raw():
    """Baseline: the same routes with NO strict middleware (today's transport)."""
    app = FastAPI()
    app.include_router(unified_login.router)
    return app


@pytest.fixture()
def client():
    # NO inner reflection on this client: the assertions below must observe THIS
    # middleware's action alone. The wide-reflection analogue is used only by the
    # attacker-over-reflection pin.
    with patch_stack():
        yield TestClient(_build_app(inner_reflection=False))


def _preflight(client, path, origin, method="POST", headers="content-type"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def _assert_no_cors_headers(response):
    for name in response.headers:
        assert not name.lower().startswith('access-control-'), \
            f"leaked {name}: {response.headers[name]}"


# -- constraint 1: exact-origin allowlist, no wildcard, no reflection -----------------

def test_preflight_allowlisted_exact_headers(client):
    r = _preflight(client, "/auth/unified-login", _ALLOWED)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == _ALLOWED
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.headers["access-control-allow-methods"] == "POST, OPTIONS"
    assert r.headers["access-control-allow-headers"] == "Content-Type"
    assert "Origin" in r.headers.get("vary", "")
    for name, value in r.headers.items():
        if name.lower().startswith("access-control-"):
            assert value.strip() != "*", f"wildcard leaked in {name}"


def test_preflight_allowlisted_www(client):
    r = _preflight(client, "/auth/unified-login", _ALLOWED_WWW)
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == _ALLOWED_WWW


def test_preflight_all_three_doors(client):
    for door in _DOORS:
        r = _preflight(client, door, _ALLOWED)
        assert r.status_code == 200, door
        assert r.headers["access-control-allow-origin"] == _ALLOWED, door
        assert r.headers["access-control-allow-credentials"] == "true", door


def test_attacker_origin_preflight_gets_no_acao(client):
    r = _preflight(client, "/auth/unified-login", _ATTACKER)
    _assert_no_cors_headers(r)
    assert "Origin" in r.headers.get("vary", "")


def test_attacker_origin_401_gets_no_acao_even_over_inner_reflection(client):
    """The inner wide-reflection layer (prod _dynamic_cors analogue) reflects ANY origin;
    the strict middleware must strip every Access-Control-* header on these doors."""
    r = client.post("/auth/unified-login", json={"email": "a@b.co", "password": "x"},
                    headers={"Origin": _ATTACKER})
    assert r.status_code == 401
    _assert_no_cors_headers(r)


# -- constraint 2: Vary + no-store on every CORS-carrying status -----------------------

def test_401_allowlisted_carries_strict_cors_vary_and_no_store(client):
    r = client.post("/auth/unified-login", json={"email": "a@b.co", "password": "x"},
                    headers={"Origin": _ALLOWED})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == _ALLOWED
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.headers["access-control-allow-methods"] == "POST, OPTIONS"
    assert r.headers["access-control-allow-headers"] == "Content-Type"
    assert "Origin" in r.headers.get("vary", "")
    assert r.headers["cache-control"] == "no-store"


def test_422_allowlisted_carries_strict_cors_vary_and_no_store(client):
    r = client.post("/auth/unified-login", data={"email": "a@b.co", "password": "x"},
                    headers={"Origin": _ALLOWED})
    assert r.status_code == 422
    assert r.headers["access-control-allow-origin"] == _ALLOWED
    assert r.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in r.headers.get("vary", "")
    assert r.headers["cache-control"] == "no-store"


# -- constraint 3: scope - no CORS change on any other endpoint ------------------------

@pytest.mark.parametrize("path", ["/me/login", "/query"])
def test_other_endpoints_get_no_acao_even_allowlisted(client, path):
    r = client.post(path, json={}, headers={"Origin": _ALLOWED})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    assert "cache-control" not in r.headers


# -- constraint 5+6: no-store (Origin present), byte-identical passthrough (absent) -----

def test_no_origin_byte_identical_to_app_without_middleware():
    with patch_stack():
        with_mw = TestClient(_build_app(inner_reflection=True))
        without_mw = TestClient(_build_app_raw())
        r1 = with_mw.post("/auth/unified-login", json={"email": "a@b.co", "password": "x"})
        r2 = without_mw.post("/auth/unified-login", json={"email": "a@b.co", "password": "x"})
        assert r1.status_code == r2.status_code == 401
        assert r1.content == r2.content
        assert dict(r1.headers) == dict(r2.headers)


# -- constraint 7: JSON-only bodies (no simple-POST door) -------------------------------

def test_form_encoded_post_is_422_json_only(client):
    r = client.post(
        "/auth/unified-login",
        data={"email": "a@b.co", "password": "x"},
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": _ALLOWED},
    )
    assert r.status_code == 422
    assert b"invalid credentials" not in r.content


def test_multipart_post_is_422_json_only(client):
    r = client.post(
        "/auth/unified-login",
        files={"email": (None, "a@b.co"), "password": (None, "x")},
        headers={"Origin": _ALLOWED},
    )
    assert r.status_code == 422


# -- allowlist is EXACT: scheme/host variants must not match ----------------------------

@pytest.mark.parametrize("origin", [
    "http://faultline.ca",
    "https://evil.faultline.ca",
    "https://faultline.ca.evil.example",
    "null",
])
def test_allowlist_is_exact_no_prefix_or_scheme_slack(client, origin):
    r = _preflight(client, "/auth/unified-login", origin)
    acao = r.headers.get("access-control-allow-origin")
    if origin not in ("https://faultline.ca", "https://www.faultline.ca"):
        assert acao is None
