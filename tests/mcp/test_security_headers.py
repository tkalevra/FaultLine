"""Security response headers (FOSS CORE transport hardening) — behavior PROOF.

Covers the additive, safe-default security headers applied to every MCP transport
response (SOC 2 CC6.6/CC6.7 boundary protection). No SaaS, no DB, no live backend.

  1. the pure applier sets nosniff / frame-options / referrer-policy / HSTS / CORP.
  2. an API (JSON) response gets the locked-down API CSP; text/html gets the dashboard CSP.
  3. existing headers are NEVER clobbered (setdefault semantics).
  4. the kill switch (MCP_SECURITY_HEADERS=0) disables the whole thing (legacy behavior).
  5. HSTS max-age=0 drops the HSTS header.
  6. end-to-end: GET /health carries the headers through the real middleware stack.

Run: python3 -m pytest tests/mcp/test_security_headers.py -q
"""

import importlib

import pytest
from starlette.datastructures import MutableHeaders


class _Resp:
    """Minimal response stand-in exposing a mutable, case-insensitive header map."""

    def __init__(self, content_type="application/json", **extra):
        self.headers = MutableHeaders()
        if content_type is not None:
            self.headers["content-type"] = content_type
        for k, v in extra.items():
            self.headers[k] = v


def _fresh_module(monkeypatch, **env):
    """Re-import http_server with a patched env so module-level config is re-read."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import src.mcp.http_server as hs
    return importlib.reload(hs)


@pytest.fixture(autouse=True)
def _restore_module(reload_http_server):
    """Reload the module clean after each test so env patches don't leak.

    Delegated to the shared ``reload_http_server`` fixture (tests/conftest.py), which restores
    the suite's BASELINE environment before reloading. The bare ``importlib.reload`` that used to
    live here reloaded under whatever env was still in force at teardown — including this
    package's own open-core (SAAS_MODE deleted) fixture, whose monkeypatch is undone later. That
    permanently rebuilt the shared http_server module with the SaaS lane off, and every SaaS test
    module collected after this one then 404'd on routes that are mounted at import.
    """
    yield


# ── 1 + 2: base headers + CSP selection ───────────────────────────────────────

def test_base_headers_and_api_csp(monkeypatch):
    hs = _fresh_module(monkeypatch, MCP_SECURITY_HEADERS=None, MCP_HSTS_MAX_AGE=None)
    r = _Resp(content_type="application/json")
    hs._apply_security_headers(r)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "max-age=" in r.headers["Strict-Transport-Security"]
    # JSON path → the locked-down API policy
    assert r.headers["Content-Security-Policy"] == \
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def test_html_gets_dashboard_csp(monkeypatch):
    hs = _fresh_module(monkeypatch)
    r = _Resp(content_type="text/html; charset=utf-8")
    hs._apply_security_headers(r)
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "fonts.googleapis.com" in csp     # dashboard font CSS
    assert "'unsafe-inline'" in csp          # dashboard uses inline style= attrs
    assert "default-src 'none'" not in csp   # NOT the API policy


# ── 3: never clobbers an existing header ───────────────────────────────────────

def test_does_not_clobber_existing(monkeypatch):
    hs = _fresh_module(monkeypatch)
    r = _Resp(content_type="text/html")
    r.headers["X-Frame-Options"] = "SAMEORIGIN"
    r.headers["Content-Security-Policy"] = "default-src 'self'"
    hs._apply_security_headers(r)
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"       # preserved
    assert r.headers["Content-Security-Policy"] == "default-src 'self'"


# ── 4: kill switch ─────────────────────────────────────────────────────────────

def test_kill_switch_disables_all(monkeypatch):
    hs = _fresh_module(monkeypatch, MCP_SECURITY_HEADERS="0")
    assert hs.SECURITY_HEADERS_ENABLED is False
    r = _Resp()
    hs._apply_security_headers(r)
    assert "X-Content-Type-Options" not in r.headers
    assert "Content-Security-Policy" not in r.headers


# ── 5: HSTS opt-out ─────────────────────────────────────────────────────────────

def test_hsts_max_age_zero_drops_header(monkeypatch):
    hs = _fresh_module(monkeypatch, MCP_HSTS_MAX_AGE="0")
    r = _Resp()
    hs._apply_security_headers(r)
    assert "Strict-Transport-Security" not in r.headers
    # the other base headers still apply
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_configurable_overrides(monkeypatch):
    hs = _fresh_module(
        monkeypatch, MCP_FRAME_OPTIONS="SAMEORIGIN",
        MCP_REFERRER_POLICY="strict-origin-when-cross-origin",
    )
    r = _Resp()
    hs._apply_security_headers(r)
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


# ── 6: end-to-end through the real middleware on /health ───────────────────────

def test_health_response_carries_headers(monkeypatch):
    from fastapi.testclient import TestClient
    hs = _fresh_module(monkeypatch)
    with TestClient(hs.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
