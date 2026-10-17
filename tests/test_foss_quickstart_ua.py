"""FOSS-001 pins: the wizard's HTTP helper sends a real User-Agent on every outbound call.

Cerebras' edge (Cloudflare-class bot filtering) 403s urllib's default "Python-urllib/3.x"
while the same key+endpoint answers 200 to a named client — so the wizard told people with
valid keys that their auth failed. Stdlib-only: a local stub server stands in for the
gateway; no external network, no heavy imports."""
from __future__ import annotations

import importlib
import inspect
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _qs():
    sys.path.insert(0, str(_ROOT))
    try:
        return importlib.import_module("quickstart")
    finally:
        sys.path.remove(str(_ROOT))


_BLOCK_PAGE = b"<html>error code: 1020</html>"  # Cloudflare-class block page: no auth wording


class _Gateway(BaseHTTPRequestHandler):
    """Stub gateway. mode='pass': 403s Python-urllib UAs (the bug), 200s named clients.
    mode='block': 403s EVERYTHING with a non-auth body (gateway still filtering)."""
    mode = "pass"
    seen_ua = None
    seen_auth = None

    def _respond(self, status, body, ctype="text/html"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).seen_ua = self.headers.get("User-Agent")
        type(self).seen_auth = self.headers.get("Authorization")
        if self.mode == "block":
            self._respond(403, _BLOCK_PAGE)
            return
        if (self.seen_ua or "").startswith("Python-urllib"):
            self._respond(403, _BLOCK_PAGE)
            return
        self._respond(200, json.dumps({"data": [{"id": "m1"}]}).encode(), "application/json")

    def log_message(self, *a):  # keep test output clean
        pass


class _AuthRefuser(_Gateway):
    """A provider that genuinely refuses the key (its own body says so)."""

    def do_GET(self):  # noqa: N802
        type(self).seen_ua = self.headers.get("User-Agent")
        self._respond(401, json.dumps(
            {"error": {"message": "Invalid API key"}}).encode(), "application/json")


@pytest.fixture()
def gateway():
    _Gateway.mode = "pass"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def blocked():
    _Gateway.mode = "block"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(srv.server_address[1])
    srv.shutdown()
    srv.server_close()
    _Gateway.mode = "pass"


@pytest.fixture()
def auth_refuser():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AuthRefuser)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


# ── the helper sets the UA, once ─────────────────────────────────────────────

def test_http_get_sends_the_faultline_user_agent(gateway):
    qs = _qs()
    status, body = qs._http_get(gateway + "/v1/models")
    assert status == 200
    assert _Gateway.seen_ua == qs.USER_AGENT, "the default UA must be replaced on every call"
    assert qs.USER_AGENT.startswith("FaultLine-Quickstart/")


def test_http_get_merges_auth_headers_under_the_ua(gateway):
    qs = _qs()
    qs._http_get(gateway + "/v1/models", headers={"Authorization": "Bearer sk-test"})
    assert _Gateway.seen_auth == "Bearer sk-test"
    assert _Gateway.seen_ua == qs.USER_AGENT, "auth headers ride ON TOP of the UA, not instead"


def test_ua_is_set_once_in_the_shared_helper_no_per_call_literals():
    qs = _qs()
    src = (_ROOT / "quickstart.py").read_text(encoding="utf-8")
    assert src.count("urlopen(") == 1, "every outbound call must ride the shared _http_get"
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("Python-urllib" in ln for ln in code_lines), \
        "the buggy default UA must not be sent (mentioning it in a comment is fine)"
    assert "USER_AGENT" in inspect.getsource(qs._http_get), "the helper owns the UA"
    assert src.count('"User-Agent"') == 1, \
        "exactly one User-Agent header literal in the file — inside the shared helper"
    for fn in (qs.probe_models, qs._poll_health):
        assert '"User-Agent"' not in inspect.getsource(fn), \
            fn.__name__ + " must not set per-call UA headers"


# ── the probe survives a gateway that 403s Python-urllib ─────────────────────

def test_probe_models_succeeds_where_python_urllib_was_403d(gateway):
    qs = _qs()
    ok, models, detail = qs.probe_models("openai", gateway, "sk-test")
    assert ok and models == ["m1"], detail


def test_bare_403_is_not_called_auth_failed(blocked):
    """A gateway 403 with a non-auth body must not read as 'auth failed (check your API
    key)' — that misdiagnosis sent people rotating valid keys."""
    qs = _qs()
    ok, models, detail = qs.probe_models("openai", blocked, "sk-test")
    assert not ok and models == []
    assert "403" in detail
    assert "auth failed" not in detail
    assert "user agent" in detail.lower(), "the hint must name the UA-filtering failure mode"


def test_genuine_auth_failure_still_says_auth_failed(auth_refuser):
    """'auth failed' is reserved for endpoints whose own body says so, and the provider
    body is surfaced so the user can tell the difference."""
    qs = _qs()
    ok, models, detail = qs.probe_models("openai", auth_refuser, "sk-bad")
    assert not ok
    assert "auth failed" in detail
    assert "Invalid API key" in detail, "the provider response body must be surfaced"
