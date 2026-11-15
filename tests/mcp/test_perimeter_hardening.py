"""Perimeter-hardening seam tests (security audit Batch B).

Pure/seam tests for:
  • #10 — Swagger/ReDoc + /v1 docs are DISABLED under SAAS_MODE (spec not anonymously served).
  • #8  — X-Forwarded-Host is honored ONLY from a trusted proxy peer (a direct client cannot
          spoof the served face); unset trusted-proxy list → unchanged (honor the header).

These drive the DECISION helpers directly (they read env live), so no app reimport is needed.

Run: python3 -m pytest tests/mcp/test_perimeter_hardening.py -q   (tests/ gitignored → git add -f)
"""

import pytest

from src.mcp import http_server as h
from src.mcp import api_v1 as v1


# ── #10: docs gating ─────────────────────────────────────────────────────────────────

def test_docs_ui_on_when_saas_off(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("MCP_ENABLE_DOCS", raising=False)
    assert h._docs_ui_enabled() is True
    assert v1._v1_docs_public() is True


def test_docs_ui_off_under_saas_mode(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.delenv("MCP_ENABLE_DOCS", raising=False)
    assert h._docs_ui_enabled() is False
    assert v1._v1_docs_public() is False


def test_docs_ui_reenabled_by_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("MCP_ENABLE_DOCS", "1")
    assert h._docs_ui_enabled() is True
    assert v1._v1_docs_public() is True


# ── #8: X-Forwarded-Host trust ───────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeReq:
    def __init__(self, headers, peer="203.0.113.9"):
        # lowercase keys — request.headers.get() is case-insensitive in Starlette; our helpers
        # query lowercase, and a plain dict.get matches that.
        self.headers = headers
        self.client = _FakeClient(peer)


def test_xfh_honored_when_no_trusted_proxy_configured(monkeypatch):
    """Unset SAAS_TRUSTED_PROXY_IPS → behavior unchanged: the forwarded host is honored."""
    monkeypatch.delenv("SAAS_TRUSTED_PROXY_IPS", raising=False)
    req = _FakeReq({"x-forwarded-host": "app.example-saas.com", "host": "faultline-mcp:8002"},
                   peer="203.0.113.9")
    assert h._effective_host(req) == "app.example-saas.com"


def test_xfh_honored_from_trusted_proxy_peer(monkeypatch):
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    req = _FakeReq({"x-forwarded-host": "app.example-saas.com", "host": "faultline-mcp:8002"},
                   peer="10.1.2.3")
    assert h._effective_host(req) == "app.example-saas.com"


def test_xfh_ignored_from_untrusted_direct_client(monkeypatch):
    """A direct client (untrusted peer) sets X-Forwarded-Host to try to flip the face —
    it is IGNORED and the real Host is used, so it cannot spoof the served face."""
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    req = _FakeReq({"x-forwarded-host": "admin.example-saas.com", "host": "app.example-saas.com"},
                   peer="203.0.113.9")
    assert h._effective_host(req) == "app.example-saas.com"  # real Host wins, not the spoofed XFH


def test_peer_trust_predicate(monkeypatch):
    monkeypatch.setenv("SAAS_TRUSTED_PROXY_IPS", "10.0.0.0/8, 172.18.0.0/16")
    assert h._peer_is_trusted_proxy(_FakeReq({}, peer="10.9.9.9")) is True
    assert h._peer_is_trusted_proxy(_FakeReq({}, peer="172.18.0.5")) is True
    assert h._peer_is_trusted_proxy(_FakeReq({}, peer="8.8.8.8")) is False
    monkeypatch.delenv("SAAS_TRUSTED_PROXY_IPS", raising=False)
    assert h._peer_is_trusted_proxy(_FakeReq({}, peer="8.8.8.8")) is True  # unset → trust all
