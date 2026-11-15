"""Community-benchmark REPORT admin endpoints — operator-gated + email-injection-safe (phase 5).

All /admin/bench-report/* routes are OPERATOR-gated (require_operator: static op KEY or a valid
operator SESSION). This proves:
  * no credential                          → 403 on every route (config/recipients/cadence/send)
  * email address validation REJECTS header/CRLF injection ("a@b.com\\nBcc: evil@x.com") — 400,
    the value never reaches the SMTP layer;
  * send-now with the operator KEY (no session) + to_self resolves to NO self email (KEY caller
    has no login) → an empty recipient set is a 400, never a silent no-op send.

SAAS_MODE (which decides the import-time SaaS wiring) is the suite-wide baseline set once in
tests/conftest.py. This module's own operator key is bound PER TEST below — never at import,
which is process-wide and let a sibling module's different key decide this file's result.
The routes reject BEFORE any DB access (gate → then validation → then DB), so these run WITHOUT
a control-plane DB. tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug BENCH-REPORT-ADMIN --test tests/test_saas_bench_report_admin.py
"""

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import saas.admin as admin

OP_KEY = "bench-report-op-super-secret-key"


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key for the duration of ONE test, then restore.

    ``saas/admin.py`` reads SAAS_OPERATOR_KEY LIVE per request, so function-scoped monkeypatch is
    exactly the right lifetime — and collection order can no longer decide the result.
    """
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)


_ROUTES = [
    ("GET", "/admin/bench-report/config"),
    ("POST", "/admin/bench-report/recipients"),
    ("DELETE", "/admin/bench-report/recipients"),
    ("PUT", "/admin/bench-report/cadence"),
    ("POST", "/admin/bench-report/send-now"),
    ("GET", "/admin/bench-report/consent-ledger"),
]

_INJECTION = [
    "a@b.com\nBcc: evil@x.com",
    "evil@x.com\r\nSubject: pwned",
    "a@b.com Cc: y@z.com",
    "a@b.com\tx@y.com",
    "not-an-email",
    "",
    "  ",
    "a@" + ("x" * 300) + ".com",   # over length cap
]


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


# ── operator gate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", _ROUTES)
def test_bench_report_routes_require_operator(client, method, path):
    body = None if method == "GET" else {}
    r = client.request(method, path, json=body)
    assert r.status_code == 403, (method, path, r.status_code, r.text[:200])


def test_bad_operator_key_denied(client):
    r = client.get("/admin/bench-report/config", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 403


# ── email validation / injection ──────────────────────────────────────────────

def test_valid_bench_email_unit():
    v = admin._valid_bench_email
    assert v("Admin@Example.COM") == "admin@example.com"
    for bad in _INJECTION:
        assert v(bad) is None, f"should reject {bad!r}"


@pytest.mark.parametrize("bad", _INJECTION)
def test_add_recipient_rejects_injection(client, bad):
    """With a VALID operator key (gate passes), a malformed/injection address is a 400 — the
    value never reaches add_recipient/SMTP."""
    r = client.post("/admin/bench-report/recipients", headers=_op(), json={"email": bad})
    assert r.status_code == 400, (bad, r.status_code, r.text[:200])


@pytest.mark.parametrize("bad", ["good@x.com\nBcc: bad@y.com", "a@b\r\nc@d.com", "nope"])
def test_send_now_rejects_injection_in_custom(client, bad):
    r = client.post("/admin/bench-report/send-now", headers=_op(),
                    json={"to_self": False, "recipients": [bad]})
    assert r.status_code == 400, (bad, r.status_code, r.text[:200])


def test_send_now_key_caller_to_self_is_empty_400(client):
    """A break-glass KEY caller has no operator SESSION → no self email → empty set → 400,
    NOT a silent send to nobody."""
    r = client.post("/admin/bench-report/send-now", headers=_op(),
                    json={"to_self": True, "recipients": []})
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert "recipient" in r.text.lower()
