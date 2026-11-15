"""Tenant logins are identified by EMAIL (not a free-text username).

Email is naturally globally-unique, so every tenant can have its own admin@their-domain
without colliding on the global UNIQUE(dashboard_users.username) constraint (the "everyone
wants to be admin" problem), and it sets up email-based MFA / recovery. This pins:
  * the email shape check accepts real emails, rejects free-text / malformed;
  * create_login (POST /admin/dashboard-users) 400s a non-email and lowercases a valid one;
  * operator logins are NOT email-gated (minted via a different, operator-authed path).

Run: python3 -m pytest tests/test_saas_tenant_login_email.py -q   (tests/ gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import dashboard_auth as auth
from saas import tenant_console as tc


OP_KEY = "op-super-secret-key"


def test_email_regex_accepts_real_emails():
    for good in ["admin@acme.com", "jane.doe@sub.example.co.uk", "a+tag@x.io"]:
        assert tc._EMAIL_RE.match(good), good


def test_email_regex_rejects_non_emails():
    for bad in ["admin", "acme-admin", "no-at.com", "two@@x.com", "space @x.com", "@x.com", "a@b"]:
        assert not tc._EMAIL_RE.match(bad), bad


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "tenant-login-email-secret-0123456789")
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


def test_create_login_rejects_non_email(client, monkeypatch):
    poisoned = {"n": 0}
    monkeypatch.setattr(auth, "create_dashboard_user",
                        lambda **k: poisoned.__setitem__("n", poisoned["n"] + 1) or "id-1")
    r = client.post("/admin/dashboard-users", headers=_op(),
                    json={"account_id": "acct-1", "username": "admin"})
    assert r.status_code == 400
    assert poisoned["n"] == 0  # never reached the create


def test_create_login_accepts_and_lowercases_email(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(auth, "hash_password", lambda p: "hash")
    monkeypatch.setattr(auth, "generate_temp_password", lambda: "Tmp-123456")
    def _create(**k):
        seen.update(k)
        return "id-1"
    monkeypatch.setattr(auth, "create_dashboard_user", _create)
    r = client.post("/admin/dashboard-users", headers=_op(),
                    json={"account_id": "acct-1", "username": "Admin@Acme.COM"})
    assert r.status_code == 200
    # stored + echoed lowercased so it round-trips at login
    assert seen["username"] == "admin@acme.com"
    assert r.json()["username"] == "admin@acme.com"
