"""Integration tests for the realm-agnostic multi-factor MFA core (saas.mfa) against a live DB.

Requires POSTGRES_DSN pointing at a database with migration 180 applied AND SAAS_SESSION_SECRET set
(saas.mfa._code_hash signs email codes with it). Skips cleanly otherwise, so the suite stays green
without infra — the authoritative validation is this file run against pre-prod (where it passes).
"""
import os
import uuid

import pytest

pytest.importorskip("pyotp")
import pyotp  # noqa: E402

if not os.environ.get("POSTGRES_DSN"):
    pytest.skip("MFA core integration test needs POSTGRES_DSN (+ migration 180)", allow_module_level=True)

# SAAS_MODE + SAAS_SESSION_SECRET are the suite-wide baseline set once in tests/conftest.py —
# writing them here would be a process-wide, import-time mutation of shared state.
import saas.mfa as M  # noqa: E402
import saas.smtp_sender as _smtp  # noqa: E402

REALM = "dashboard"


def _skip_if_no_table():
    try:
        from saas import db
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.mfa_factors')")
                if cur.fetchone()[0] is None:
                    pytest.skip("public.mfa_factors missing — apply migration 180")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"no DB: {e}")


@pytest.fixture()
def pid():
    _skip_if_no_table()
    p = str(uuid.uuid4())
    yield p
    # best-effort cleanup: drop every factor this principal created
    for f in M.list_factors(REALM, p):
        M.remove_factor(REALM, p, f["id"])


def test_totp_add_confirm_verify_remove(pid):
    r = M.add_totp_factor(REALM, pid, label="iPhone", account_label="smoke@test")
    assert r["ok"] and r["factor_id"] and r["secret"] and r["provisioning_uri"]
    assert M.principal_has_enabled_factor(REALM, pid) is False  # pending, not yet active
    # wrong code does not enable
    assert M.confirm_totp_factor(REALM, pid, r["factor_id"], "000000") is False
    assert M.principal_has_enabled_factor(REALM, pid) is False
    # valid code enables it
    assert M.confirm_totp_factor(REALM, pid, r["factor_id"], pyotp.TOTP(r["secret"]).now()) is True
    assert M.principal_has_enabled_factor(REALM, pid) is True
    assert M.verify_totp_any(REALM, pid, pyotp.TOTP(r["secret"]).now()) is True
    assert M.verify_totp_any(REALM, pid, "000000") is False
    assert M.remove_factor(REALM, pid, r["factor_id"]) is True
    assert M.principal_has_enabled_factor(REALM, pid) is False


def test_multiple_totp_authenticators(pid):
    a = M.add_totp_factor(REALM, pid, label="phone", account_label="x")
    b = M.add_totp_factor(REALM, pid, label="bitwarden", account_label="x")
    assert M.confirm_totp_factor(REALM, pid, a["factor_id"], pyotp.TOTP(a["secret"]).now())
    assert M.confirm_totp_factor(REALM, pid, b["factor_id"], pyotp.TOTP(b["secret"]).now())
    # a code from EITHER authenticator satisfies login
    assert M.verify_totp_any(REALM, pid, pyotp.TOTP(a["secret"]).now()) is True
    assert M.verify_totp_any(REALM, pid, pyotp.TOTP(b["secret"]).now()) is True
    assert len([f for f in M.list_factors(REALM, pid) if f["enabled"]]) == 2


def test_email_factor_send_confirm(pid, monkeypatch):
    import re
    captured = {}

    def fake_send(to, subject, text_body, **kw):
        m = re.search(r"\b(\d{6})\b", text_body)
        captured["code"] = m.group(1) if m else None
        captured["to"] = to
        return {"ok": True}

    monkeypatch.setattr(_smtp, "send_email", fake_send)
    monkeypatch.setattr(M.smtp_sender, "send_email", fake_send)

    er = M.add_email_factor(REALM, pid, label="work", email_address="Jane@Example.com")
    assert er["ok"] and er["email_masked"] == "j***@example.com"
    assert M.send_email_code(REALM, pid, er["factor_id"])["ok"] is True
    assert captured["to"] == "jane@example.com" and captured["code"]
    # wrong code fails, right code confirms + enables
    assert M.verify_email_code(REALM, pid, er["factor_id"], "999999", confirm_enroll=True) is False
    assert M.verify_email_code(REALM, pid, er["factor_id"], captured["code"], confirm_enroll=True) is True
    assert M.principal_has_enabled_factor(REALM, pid) is True


def test_email_code_attempts_cap(pid, monkeypatch):
    import re
    captured = {}
    monkeypatch.setattr(M.smtp_sender, "send_email",
                        lambda to, subject, text_body, **kw: (
                            captured.__setitem__("code", re.search(r"\b(\d{6})\b", text_body).group(1))
                            or {"ok": True}))
    er = M.add_email_factor(REALM, pid, label="e", email_address="a@b.co")
    M.send_email_code(REALM, pid, er["factor_id"])
    # exhaust the attempt cap with wrong codes → even the correct code then fails (code burned)
    for _ in range(M.MAX_EMAIL_CODE_ATTEMPTS):
        assert M.verify_email_code(REALM, pid, er["factor_id"], "111111") is False
    assert M.verify_email_code(REALM, pid, er["factor_id"], captured["code"]) is False


def test_unknown_realm_is_failclosed(pid):
    assert M.add_totp_factor("bogus", pid, label="x", account_label="x")["ok"] is False
    assert M.principal_has_enabled_factor("bogus", pid) is False
    assert M.list_factors("bogus", pid) == []
