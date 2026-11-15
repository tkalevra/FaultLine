"""UNIFIED SIGN-IN (BUGS/login-uth-eq-login-unified-signin-20261101, PART C).

Pins the discriminated /auth/unified-login contract:

  * seat-only credential  -> realm='seat', and the REAL me_login ran (session issuer,
    CSRF, seat-status gate - all realm-native; nothing reimplemented here).
  * tenant-only credential -> realm='tenant', and the REAL tenant_login ran.
  * BOTH realms verify    -> realm='both', a short-TTL SINGLE-USE routing grant is
    issued, and NO session issuer of either realm is touched. The grant redeems exactly
    once at /auth/unified-grant/validate; tampered / replayed / expired grants all get
    the SAME generic refusal.
  * MFA on the single matching realm -> realm='mfa' with that realm's OWN challenge
    token and NO session anywhere (TOTP is never swallowed).
  * neither realm verifies -> the SAME generic 401 both native doors use, with exactly
    ONE lockout failure recorded against the ONE shared email key.
  * lockout: a locked email 429s before any probe.

The direct doors are pinned unchanged by their own suites
(test_saas_disabled_seat_cannot_login.py, test_saas_tenant_auth.py) - and the seat/tenant
paths here drive those REAL functions, so a regression in either fails this file too.
"""
import contextlib
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Response

import saas.audit as _audit_mod
import saas.dashboard_auth as _auth_mod
import saas.mfa as _mfa_mod
import saas.ratelimit as _ratelimit_mod
import saas.tenant_console as _tenant_console
import saas.user_console as _user_console
from saas import unified_login

_TEST_SIGNING_SECRET = "unit-test-only-not-a-real-secret-unified-login"
_EMAIL = "both.realms@example.com"
_PASSWORD = "correct-horse"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SAAS_SESSION_SECRET", _TEST_SIGNING_SECRET)
    monkeypatch.delenv("SAAS_SESSION_SECRET_PREVIOUS", raising=False)
    # the one-click chooser stages credentials under the SAME envelope key family
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", "unit-test-only-not-a-real-cred-key")
    monkeypatch.delenv("SAAS_LLM_CRED_KEY_PREVIOUS", raising=False)


def _individual():
    return {"id": "ind-1", "user_id": "u-1", "email": _EMAIL,
            "password_hash": "seat-hash", "status": "active"}


def _dashboard_user():
    return {"id": "du-1", "account_id": "acct-1", "username": _EMAIL,
            "password_hash": "tenant-hash", "role": "admin"}


def _db_conn_none():
    """A db.connection() mock shaped like the REAL usage (see the dead-mock note in
    test_saas_disabled_seat_cannot_login.py): both context-manager levels wired,
    seat-status fetchone -> None (fail-open: standalone login keeps working)."""
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


class _Counting:
    def __init__(self):
        self.calls = []


def _patch_stack(seat_ok=True, tenant_ok=False, *, seat_mfa=False, tenant_mfa=False,
                 failures=None, locked=False, verify_password=None):
    """The patch set that lets the REAL unified_login / me_login / tenant_login run."""
    failures = failures if failures is not None else _Counting()
    sessions = {"seat": _Counting(), "tenant": _Counting()}
    verify = verify_password or (lambda h, p: True)

    # ExitStack, not a single 20+-item with: CPython refuses >20 STATICALLY NESTED blocks
    # ("too many statically nested blocks") — one giant with-chain of patch.object calls
    # does not compile. The patches themselves are unchanged.
    patches = [
        patch.object(unified_login, "_require_saas", lambda: None),
        patch.object(unified_login, "_get_individual_by_email",
                     lambda e: _individual() if seat_ok else None),
        patch.object(_user_console, "get_individual_by_email",
                     lambda e: _individual() if seat_ok else None),
        patch.object(_auth_mod, "get_user_by_username",
                     lambda e: _dashboard_user() if tenant_ok else None),
        patch.object(_auth_mod, "verify_password", verify),
        patch.object(_ratelimit_mod, "login_locked", lambda e: (locked, 30)),
        patch.object(_ratelimit_mod, "record_login_failure",
                     lambda e: failures.calls.append(e)),
        patch.object(_ratelimit_mod, "record_login_success", lambda e: None),
        patch.object(_mfa_mod, "require_mfa_decision",
                     lambda realm, pid: (seat_mfa if realm == "individual" else tenant_mfa)),
        patch.object(_auth_mod, "issue_individual_mfa_challenge",
                     lambda **k: "seat-mfa-challenge"),
        patch.object(_auth_mod, "issue_mfa_challenge",
                     lambda **k: "tenant-mfa-challenge"),
        patch.object(_mfa_mod, "enabled_factors_for_login", lambda *a, **k: []),
        patch.object(_auth_mod, "issue_individual_session",
                     lambda **k: sessions["seat"].calls.append(k) or "seat-session"),
        patch.object(_auth_mod, "issue_session",
                     lambda **k: sessions["tenant"].calls.append(k) or "tenant-session"),
        patch.object(_auth_mod, "issue_individual_csrf", lambda **k: "seat-csrf"),
        patch.object(_auth_mod, "issue_csrf", lambda **k: "tenant-csrf"),
        patch.object(_auth_mod, "touch_last_login", lambda *a, **k: None),
        patch.object(_user_console, "_touch_individual_login", lambda *a, **k: None),
        patch.object(_user_console, "_set_session_cookie", lambda *a, **k: None),
        patch.object(_tenant_console, "_set_session_cookie", lambda *a, **k: None),
        patch.object(_audit_mod, "record", lambda *a, **k: None),
        patch.object(_user_console.db, "connection", lambda: _db_conn_none()),
        patch.object(_tenant_console._db, "connection", lambda: _db_conn_none()),
    ]
    return patches, failures, sessions


def _drive(seat_ok=True, tenant_ok=False, *, seat_mfa=False, tenant_mfa=False,
           failures=None, locked=False, request=None, response=None):
    """Drive the REAL unified_login with only the credential-table facts varied."""
    patches, failures, sessions = _patch_stack(
        seat_ok=seat_ok, tenant_ok=tenant_ok, seat_mfa=seat_mfa,
        tenant_mfa=tenant_mfa, failures=failures, locked=locked)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        body = unified_login.UnifiedLoginRequest(email=_EMAIL, password=_PASSWORD)
        out = unified_login.unified_login(body, request or MagicMock(),
                                          response or MagicMock())
    return out, failures, sessions


def _grant_nonce(grant):
    return str(_auth_mod.verify_unified_route_grant(grant)["n"])


def _cookie_req(value):
    rq = MagicMock()
    rq.cookies = {unified_login.ROUTE_COOKIE_NAME: value} if value else {}
    return rq


def _set_cookie_header(response):
    for k, v in response.raw_headers:
        if k == b"set-cookie" and unified_login.ROUTE_COOKIE_NAME.encode() in v:
            return v.decode()
    return ""


# -- seat-only -----------------------------------------------------------------------

def test_seat_only_runs_the_real_me_login_and_returns_realm_seat():
    out, failures, sessions = _drive(seat_ok=True, tenant_ok=False)
    assert out["realm"] == "seat"
    assert out["status"] == "authenticated"
    assert out["csrf_token"] == "seat-csrf"
    # the REAL me_login path minted the seat session - exactly once
    assert len(sessions["seat"].calls) == 1
    assert len(sessions["tenant"].calls) == 0
    assert failures.calls == []


# -- tenant-only ---------------------------------------------------------------------

def test_tenant_only_runs_the_real_tenant_login_and_returns_realm_tenant():
    out, failures, sessions = _drive(seat_ok=False, tenant_ok=True)
    assert out["realm"] == "tenant"
    assert out["status"] == "authenticated"
    assert out["csrf_token"] == "tenant-csrf"
    assert len(sessions["tenant"].calls) == 1
    assert len(sessions["seat"].calls) == 0


# -- both realms: routing grant, never a session --------------------------------------

def test_both_realms_returns_grant_and_never_mints_a_session():
    out, _, sessions = _drive(seat_ok=True, tenant_ok=True)
    assert out["realm"] == "both"
    assert out["status"] == "ambiguous"
    assert out["grant_ttl"] == unified_login.GRANT_TTL_SECONDS
    assert unified_login.GRANT_TTL_SECONDS == 120  # owner-approved ~2 min
    assert out["grant"]
    # THE contract: the unified door is not a session minter.
    assert sessions["seat"].calls == []
    assert sessions["tenant"].calls == []


def test_both_grant_redeems_exactly_once():
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    req = unified_login.GrantValidateRequest(grant=out["grant"])
    ok = unified_login.unified_grant_validate(req, MagicMock())
    assert ok["status"] == "ok"
    assert set(ok["realms"]) == {"seat", "tenant"}
    with pytest.raises(HTTPException) as e:
        unified_login.unified_grant_validate(req, MagicMock())  # replay
    assert e.value.status_code == 401


def test_grant_is_not_a_session_not_mfa_not_oauth():
    """Salt-namespace isolation: a routing grant satisfies no other verifier."""
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    grant = out["grant"]
    assert _auth_mod.verify_session(grant) is None
    assert _auth_mod.verify_mfa_challenge(grant) is None
    assert _auth_mod.verify_oauth_session_grant(grant) is None
    assert _auth_mod.verify_individual_mfa_challenge(grant) is None


def test_tampered_grant_refused():
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    req = unified_login.GrantValidateRequest(grant=out["grant"] + "tampered")
    with pytest.raises(HTTPException) as e:
        unified_login.unified_grant_validate(req, MagicMock())
    assert e.value.status_code == 401
    # the SAME generic refusal for every failure shape
    with pytest.raises(HTTPException) as e2:
        unified_login.unified_grant_validate(
            unified_login.GrantValidateRequest(grant="junk"), MagicMock())
    assert e2.value.detail == e.value.detail


def test_grant_expires_within_ttl_window(monkeypatch):
    # itsdangerous timestamps are SECOND-granular, so a real sleep(1.1) raced the
    # second boundary (pre-existing flake). Freeze the shared stdlib clock instead:
    # both the signer and the verifier read the same time.time at call time.
    base = int(time.time())
    monkeypatch.setattr(_auth_mod.time, "time", lambda: float(base))
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    assert _auth_mod.verify_unified_route_grant(out["grant"]) is not None
    monkeypatch.setattr(_auth_mod.time, "time", lambda: float(base) + 5.0)
    assert _auth_mod.verify_unified_route_grant(out["grant"], max_age=1) is None, \
        "an aged-out grant must fail verification"
    monkeypatch.setattr(_auth_mod.time, "time", lambda: float(base) + 5.0)
    assert _auth_mod.verify_unified_route_grant(out["grant"], max_age=30) is not None, \
        "within the TTL window the grant still verifies"


# -- MFA: realm-owned challenge, never a session ---------------------------------------

def test_seat_mfa_returns_realm_mfa_with_seat_challenge_and_no_session():
    out, _, sessions = _drive(seat_ok=True, tenant_ok=False, seat_mfa=True)
    assert out["realm"] == "mfa"
    assert out["realm_source"] == "seat"
    assert out["status"] == "mfa_required"
    assert out["mfa_token"] == "seat-mfa-challenge"
    assert sessions["seat"].calls == [] and sessions["tenant"].calls == []


def test_tenant_mfa_returns_realm_mfa_with_tenant_challenge_and_no_session():
    out, _, sessions = _drive(seat_ok=False, tenant_ok=True, tenant_mfa=True)
    assert out["realm"] == "mfa"
    assert out["realm_source"] == "tenant"
    assert out["mfa_token"] == "tenant-mfa-challenge"
    assert sessions["seat"].calls == [] and sessions["tenant"].calls == []


# -- neither realm: generic 401, ONE shared lockout failure -----------------------------

def test_neither_realm_is_generic_401_with_one_lockout_failure():
    failures = _Counting()
    with pytest.raises(HTTPException) as e:
        _drive(seat_ok=False, tenant_ok=False, failures=failures)
    assert e.value.status_code == 401
    # the EXACT generic string both native doors use - no realm disclosure, no oracle
    assert e.value.detail == "invalid credentials"
    assert len(failures.calls) == 1  # ONE failure, ONE key - never one per realm
    assert failures.calls[0] == _EMAIL


def test_locked_email_429s_before_any_probe():
    with pytest.raises(HTTPException) as e:
        _drive(locked=True)
    assert e.value.status_code == 429
    assert e.value.headers.get("Retry-After") == "30"


def test_missing_password_is_generic_401():
    with patch.object(unified_login, "_require_saas", lambda: None), \
         patch.object(_ratelimit_mod, "login_locked", lambda e: (False, 0)):
        body = unified_login.UnifiedLoginRequest(email=_EMAIL, password="")
        with pytest.raises(HTTPException) as e:
            unified_login.unified_login(body, MagicMock(), MagicMock())
    assert e.value.status_code == 401
    assert e.value.detail == "invalid credentials"


# -- direct doors unchanged: /access.html stays a standalone chooser --------------------

def test_direct_chooser_page_has_no_hard_grant_dependency():
    """access.html must keep working as a direct door: it links the two console login
    pages as plain anchors, with no grant in the URL - landing.js's grant validation is
    a hint, never a gate (the doors are real links, not JS-injected)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "saas", "landing", "access.html"), encoding="utf-8").read()
    assert 'href="https://app.faultline.ca/dashboard/tenant.html"' in html
    assert 'href="https://app.faultline.ca/dashboard/user.html"' in html
    assert "grant=" not in html  # no grant required to render the two doors


# ════════════════════════════════════════════════════════════════════════════════════
# ULG-1 — argon2 timing parity (BUGS/unified-login-critic-20261101)
# ════════════════════════════════════════════════════════════════════════════════════

def _argon2_count(seat_ok, tenant_ok):
    """Count argon2 verify invocations for one unified-login attempt."""
    calls = []
    counter = lambda h, p: calls.append(1) or True
    patches, _, _ = _patch_stack(seat_ok=seat_ok, tenant_ok=tenant_ok,
                                 verify_password=counter)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        body = unified_login.UnifiedLoginRequest(email=_EMAIL, password=_PASSWORD)
        with contextlib.suppress(HTTPException):
            unified_login.unified_login(body, MagicMock(), MagicMock())
    return len(calls)


def test_miss_performs_exactly_the_same_argon2_verifications_as_a_hit():
    """ULG-1 parity pin: an unknown-email miss must run the SAME number of argon2
    verifications as ANY hit. The single-realm outcomes pay 2 probes + their native
    door's own verify = 3, so EVERY outcome is topped up to that uniform floor of 3 —
    timing classifies nothing across {neither, seat, tenant, both}."""
    miss = _argon2_count(False, False)      # neither realm has the email
    seat_only = _argon2_count(True, False)
    tenant_only = _argon2_count(False, True)
    both = _argon2_count(True, True)
    assert miss == seat_only == tenant_only == both == 3


def test_miss_records_exactly_one_login_failure_despite_dummy_verifies():
    failures = _Counting()
    with pytest.raises(HTTPException) as e:
        _drive(seat_ok=False, tenant_ok=False, failures=failures)
    assert e.value.status_code == 401
    assert len(failures.calls) == 1  # dummy verifies never add failure records


# ════════════════════════════════════════════════════════════════════════════════════
# COOKIE-BOUND ONE-CLICK CHOOSER with re-prompt fallback
# ════════════════════════════════════════════════════════════════════════════════════

def test_one_click_happy_path_cookie_present_complete_realm_session():
    resp = Response()
    out, _, sessions = _drive(seat_ok=True, tenant_ok=True, response=resp)
    assert out["realm"] == "both" and out["one_click"] is True
    nonce = _grant_nonce(out["grant"])
    # cookie: HttpOnly + SameSite=Lax + nonce-HMAC value + grant-scoped path
    setc = _set_cookie_header(resp)
    assert setc.startswith(unified_login.ROUTE_COOKIE_NAME + "=")
    assert "HttpOnly" in setc and "SameSite=lax" in setc
    assert "Path=/auth/unified-grant" in setc
    cookie_val = setc.split("=", 1)[1].split(";")[0]
    assert cookie_val != nonce  # the cookie is the server-secret HMAC, not the raw nonce
    assert _auth_mod.verify_route_cookie(cookie_val, nonce=nonce)
    # validate reports one-click AND does NOT consume the nonce
    v = unified_login.unified_grant_validate(
        unified_login.GrantValidateRequest(grant=out["grant"]), _cookie_req(cookie_val))
    assert v["status"] == "ok" and v["one_click"] is True
    # complete establishes the session through the REAL me_login, exactly once
    patches, _, sessions2 = _patch_stack(seat_ok=True, tenant_ok=True)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        out2 = unified_login.unified_grant_complete(
            unified_login.GrantCompleteRequest(grant=out["grant"], destination="seat"),
            _cookie_req(cookie_val), Response())
    assert out2["realm"] == "seat" and out2["next"] == "/dashboard/user.html"
    assert len(sessions2["seat"].calls) == 1
    assert len(sessions2["tenant"].calls) == 0
    # nonce consumed (GETDEL) and the staged credential store DELETED
    assert not unified_login._peek_credentials(nonce)
    # replay of the consumed nonce is refused
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(HTTPException) as e:
            unified_login.unified_grant_complete(
                unified_login.GrantCompleteRequest(grant=out["grant"], destination="seat"),
                _cookie_req(cookie_val), Response())
    assert e.value.status_code == 401


def test_tenant_destination_completes_through_tenant_login():
    resp = Response()
    out, _, _ = _drive(seat_ok=True, tenant_ok=True, response=resp)
    nonce = _grant_nonce(out["grant"])
    cookie_val = _auth_mod.route_cookie_value(nonce=nonce)
    patches, _, sessions2 = _patch_stack(seat_ok=True, tenant_ok=True)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        out2 = unified_login.unified_grant_complete(
            unified_login.GrantCompleteRequest(grant=out["grant"], destination="tenant"),
            _cookie_req(cookie_val), Response())
    assert out2["realm"] == "tenant" and out2["next"] == "/dashboard/tenant.html"
    assert len(sessions2["tenant"].calls) == 1
    assert len(sessions2["seat"].calls) == 0


def test_cookie_missing_validate_reports_no_one_click_and_fallback_consumes():
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    nonce = _grant_nonce(out["grant"])
    # validate WITHOUT the cookie: one_click False, and the nonce is consumed here
    v = unified_login.unified_grant_validate(
        unified_login.GrantValidateRequest(grant=out["grant"]), _cookie_req(None))
    assert v["status"] == "ok" and v["one_click"] is False
    with pytest.raises(HTTPException) as e:
        unified_login.unified_grant_validate(
            unified_login.GrantValidateRequest(grant=out["grant"]), _cookie_req(None))
    assert e.value.status_code == 401  # replay of the consumed grant
    # a forged/mismatched cookie cannot resurrect one-click completion
    with pytest.raises(HTTPException) as e2:
        unified_login.unified_grant_complete(
            unified_login.GrantCompleteRequest(grant=out["grant"], destination="seat"),
            _cookie_req(_auth_mod.route_cookie_value(nonce=nonce)), Response())
    assert e2.value.status_code == 401


def test_url_grant_without_cookie_cannot_complete():
    """THE pin: a stolen URL grant with no route cookie authenticates nothing —
    not even with the raw nonce, which is readable inside the signed payload."""
    out, _, sessions = _drive(seat_ok=True, tenant_ok=True)
    nonce = _grant_nonce(out["grant"])
    patches, _, _ = _patch_stack(seat_ok=True, tenant_ok=True)
    for attacker_cookie in (None, nonce):  # no cookie at all / forged bare-nonce cookie
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(HTTPException) as e:
                unified_login.unified_grant_complete(
                    unified_login.GrantCompleteRequest(grant=out["grant"],
                                                       destination="seat"),
                    _cookie_req(attacker_cookie), Response())
        assert e.value.status_code == 401
    assert sessions["seat"].calls == [] and sessions["tenant"].calls == []
    # and the grant remains a valid routing hint for the REAL browser (nonce intact)
    assert _peek_ok(nonce)


def _peek_ok(nonce):
    return unified_login._peek_credentials(nonce)


def test_cookie_without_grant_or_bad_signature_refused():
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    cookie_val = _auth_mod.route_cookie_value(nonce=_grant_nonce(out["grant"]))
    with pytest.raises(HTTPException) as e:
        unified_login.unified_grant_complete(
            unified_login.GrantCompleteRequest(grant="junk", destination="seat"),
            _cookie_req(cookie_val), Response())
    assert e.value.status_code == 401


def test_replayed_consumed_nonce_refused_on_complete():
    out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    nonce = _grant_nonce(out["grant"])
    cookie_val = _auth_mod.route_cookie_value(nonce=nonce)
    patches, _, _ = _patch_stack(seat_ok=True, tenant_ok=True)
    body = unified_login.GrantCompleteRequest(grant=out["grant"], destination="seat")
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        unified_login.unified_grant_complete(body, _cookie_req(cookie_val), Response())
        with pytest.raises(HTTPException) as e:
            unified_login.unified_grant_complete(body, _cookie_req(cookie_val), Response())
    assert e.value.status_code == 401


class _FakeRedis:
    """Captures every stored value so the plaintext pin can grep them."""

    def __init__(self):
        self.kv = {}
        self.ex = {}

    def set(self, k, v, ex=None, nx=None):
        self.kv[k] = v
        self.ex[k] = ex
        return True

    def get(self, k):
        return self.kv.get(k)

    def getdel(self, k):
        return self.kv.pop(k, None)

    def exists(self, k):
        return 1 if k in self.kv else 0

    def delete(self, k):
        return self.kv.pop(k, None)


def test_credential_material_never_in_plaintext_in_redis_values_or_logs(capsys):
    """Grep-style pin: no Redis value, stderr log line, or audit detail ever carries
    the password in plaintext — the stage is Fernet ciphertext under the shared
    envelope key family."""
    fake = _FakeRedis()
    audit_lines = []

    def audit_rec(*a, **k):
        audit_lines.append(str(a) + str(sorted(k.items())))

    with patch.object(unified_login, "_redis_client", lambda: fake), \
         patch.object(_audit_mod, "record", audit_rec):
        resp = Response()
        out, _, _ = _drive(seat_ok=True, tenant_ok=True, response=resp)
        nonce = _grant_nonce(out["grant"])
    cred_key = unified_login._CRED_PREFIX + nonce
    assert cred_key in fake.kv, "the encrypted credential must be staged under the nonce key"
    ct = _normalize(fake.kv[cred_key])
    assert _PASSWORD not in ct and _EMAIL not in ct
    assert fake.ex[cred_key] == unified_login.GRANT_TTL_SECONDS
    # the ciphertext decrypts back to the verified credential (same envelope key family)
    assert unified_login._decrypt_credentials(ct) == {"email": _EMAIL, "password": _PASSWORD}
    # every stderr log line and audit detail is free of the password
    err = capsys.readouterr().err
    assert _PASSWORD not in err
    assert _PASSWORD not in " ".join(audit_lines)


def _normalize(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def test_ttl_expiry_staged_credential_gone_generic_refusal():
    # force the in-process store (no Redis) so the TTL entry is directly ageable
    with patch.object(unified_login, "_redis_client", lambda: None):
        out, _, _ = _drive(seat_ok=True, tenant_ok=True)
    nonce = _grant_nonce(out["grant"])
    cookie_val = _auth_mod.route_cookie_value(nonce=nonce)
    # age the staged credential past its TTL (the in-process fallback store)
    entry = unified_login._local_cred_store.get(nonce)
    assert entry, "credential must be staged"
    unified_login._local_cred_store[nonce] = (entry[0], 0.0)
    assert not unified_login._peek_credentials(nonce)
    patches, _, _ = _patch_stack(seat_ok=True, tenant_ok=True)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(HTTPException) as e:
            unified_login.unified_grant_complete(
                unified_login.GrantCompleteRequest(grant=out["grant"], destination="seat"),
                _cookie_req(cookie_val), Response())
    assert e.value.status_code == 401
    assert e.value.detail == "invalid or expired sign-in grant"


def test_crypto_unavailable_falls_closed_to_reprompt_only():
    """No SAAS_LLM_CRED_KEY -> no stage, no cookie, plain grant: the chooser renders
    today's re-prompt doors (fail-closed, never a plaintext stage)."""
    patches, _, _ = _patch_stack(seat_ok=True, tenant_ok=True)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        monkeypatch_del = patch.dict("os.environ", {"SAAS_LLM_CRED_KEY": ""}, clear=False)
        monkeypatch_del.start()
        resp = Response()
        out, _, _ = _drive(seat_ok=True, tenant_ok=True, response=resp)
    assert out["one_click"] is False
    assert _set_cookie_header(resp) == ""
