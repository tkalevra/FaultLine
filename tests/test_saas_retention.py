"""Self-serve DATA-RETENTION + 3-strikes repeat-signup ladder (migration 179) — CLOSED-SOURCE.

Individual accounts are NOT kept beyond a grace window past the free trial (privacy). Repeat
signups walk a 3-strikes ladder; the purge reaper is DRY-RUN/OFF by default; activity compiles
into an operator digest.

What this pins:
  * the ledger HASH is salted + normalization-stable (same address → same hash; case-insensitive)
    and holds NO plaintext (privacy decision);
  * the 3-strikes ladder: attempt 1 → allow, 2 → allow + cheeky email, 3+ → REJECT (no invite
    spend, no provision), plus FAIL-SAFE ALLOW on a ledger read error;
  * the grace valve reads the operator setting + clamps + defaults to 7;
  * the reaper DRY-RUNS by default (deletes NOTHING, logs candidates) and, when ARMED, purges the
    EXACT resolved user_id (delete mocked — never a real destructive DB hit);
  * the ladder emails go to the ATTEMPT-TIME address;
  * the operator digest endpoints are operator-gated (403 without the operator credential).

SAAS_MODE + SAAS_SIGNUP_LEDGER_SALT (which decide the import-time SaaS wiring) are the suite-wide
baseline set once in tests/conftest.py. This module's own operator key is bound PER TEST below —
never at import, which is process-wide and let a sibling module's different key decide this file's
result. tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug RETENTION --test tests/test_saas_retention.py
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.mcp.http_server as h
from saas import signup_ledger as sl
from saas import retention as ret
from saas import platform_settings as saas_platform
from saas import memory_ops as saas_memory_ops
from saas import user_console as uc

OP_KEY = "retention-op-super-secret-key"


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key for the duration of ONE test, then restore.

    ``saas/admin.py`` reads SAAS_OPERATOR_KEY LIVE per request, so function-scoped monkeypatch is
    exactly the right lifetime — and collection order can no longer decide the result.
    """
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)



def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


@pytest.fixture(autouse=True)
def _clear_platform_cache():
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture(autouse=True)
def _moderation_guards_neutral(monkeypatch):
    """Migrations 228/229: /me/signup now also consults the platform BAN LIST and the global
    SIGNUP SPAM FILTER, both control-plane reads this DB-less suite cannot serve (the ban check
    fails CLOSED by design). Stub them NEUTRAL so the repeat-signup ladder this file is about
    stays the thing under test; the guards themselves are proven in
    tests/test_saas_account_moderation.py."""
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e, **kw: uc._signup_filter.Decision(True))


# ══════════════════════════════════════════════════════════════════════════════════
# (1) LEDGER HASH — salted, normalization-stable, no plaintext
# ══════════════════════════════════════════════════════════════════════════════════

def test_same_email_same_hash():
    assert sl.hash_email("person@example.com") == sl.hash_email("person@example.com")


def test_hash_is_case_and_whitespace_insensitive():
    """Normalization = trim + lowercase, matching the signup path's own email handling."""
    assert sl.hash_email("Person@Example.COM") == sl.hash_email("  person@example.com ")


def test_different_emails_different_hash():
    assert sl.hash_email("a@example.com") != sl.hash_email("b@example.com")


def test_hash_is_salted(monkeypatch):
    """A different salt yields a different hash for the SAME address — a stolen DB alone can't
    re-derive the address space without the salt."""
    base = sl.hash_email("person@example.com")
    monkeypatch.setenv("SAAS_SIGNUP_LEDGER_SALT", "a-totally-different-salt")
    assert sl.hash_email("person@example.com") != base


def test_hash_is_hex_sha256_length():
    assert len(sl.hash_email("person@example.com")) == 64


def test_ip_hash_none_when_absent():
    assert sl.hash_ip(None) is None
    assert sl.hash_ip("") is None
    assert sl.hash_ip("1.2.3.4") is not None and sl.hash_ip("1.2.3.4") != "1.2.3.4"


# ══════════════════════════════════════════════════════════════════════════════════
# (2) 3-STRIKES LADDER DECISION (from the stored count) + FAIL-SAFE
# ══════════════════════════════════════════════════════════════════════════════════

def test_first_attempt_allows(monkeypatch):
    monkeypatch.setattr(sl, "get_attempt_count", lambda e: 0)
    assert sl.decide("x@example.com") == sl.DECISION_ALLOW


def test_second_attempt_is_cheeky(monkeypatch):
    monkeypatch.setattr(sl, "get_attempt_count", lambda e: 1)
    assert sl.decide("x@example.com") == sl.DECISION_ALLOW_CHEEKY


@pytest.mark.parametrize("count", [2, 3, 9])
def test_third_plus_attempt_rejects(monkeypatch, count):
    monkeypatch.setattr(sl, "get_attempt_count", lambda e: count)
    assert sl.decide("x@example.com") == sl.DECISION_REJECT


def test_ledger_read_error_fails_safe_to_allow(monkeypatch):
    """An unreadable ledger (None) must NEVER lock out a legitimate user — default ALLOW."""
    monkeypatch.setattr(sl, "get_attempt_count", lambda e: None)
    assert sl.decide("x@example.com") == sl.DECISION_ALLOW


# ══════════════════════════════════════════════════════════════════════════════════
# (3) GRACE VALVE — reads the operator setting, clamps, defaults to 7
# ══════════════════════════════════════════════════════════════════════════════════

def test_grace_reads_configured_value(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"retention_grace_days": 14})
    assert saas_platform.retention_grace_days() == 14


def test_grace_defaults_to_7_when_missing(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {})
    assert saas_platform.retention_grace_days() == 7


def test_grace_clamps_high(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"retention_grace_days": 100000})
    assert saas_platform.retention_grace_days() == 365


def test_grace_zero_is_valid(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"retention_grace_days": 0})
    assert saas_platform.retention_grace_days() == 0


def test_grace_failsafe_when_db_unreadable(monkeypatch):
    def _boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(saas_platform.db, "connection", _boom)
    assert saas_platform.retention_grace_days() == 7


def test_grace_out_of_range_rejected_400():
    """Server-side bounds check on the operator write (400, not 422) — rejected before any DB."""
    client = TestClient(h.app, follow_redirects=False)
    for bad in (-1, 366, 5000):
        r = client.put("/admin/platform-settings", json={"retention_grace_days": bad},
                       headers=_op())
        assert r.status_code == 400, r.text


# ══════════════════════════════════════════════════════════════════════════════════
# (4) PURGE REAPER — DRY-RUN by default (deletes NOTHING), ARMED purges the exact user
# ══════════════════════════════════════════════════════════════════════════════════

_CANDIDATE = {"individual_id": "ind-1", "user_id": "usr-abc",
              "account_id": "acc-1", "email": "gone@example.com", "trial_ends_at": None}


def test_purge_enabled_defaults_off(monkeypatch):
    """The DESTRUCTIVE flag is OFF unless explicitly, well-formedly armed."""
    monkeypatch.delenv("SAAS_RETENTION_PURGE_ENABLED", raising=False)
    assert ret.purge_enabled() is False
    for v in ("0", "false", "no", "off", "banana", ""):
        monkeypatch.setenv("SAAS_RETENTION_PURGE_ENABLED", v)
        assert ret.purge_enabled() is False
    for v in ("1", "true", "YES", "On"):
        monkeypatch.setenv("SAAS_RETENTION_PURGE_ENABLED", v)
        assert ret.purge_enabled() is True


def test_dry_run_deletes_nothing(monkeypatch):
    """Default sweep: candidates are LOGGED into would_purge, and purge_individual is NEVER called."""
    monkeypatch.setattr(ret, "list_candidates", lambda **k: [dict(_CANDIDATE)])
    monkeypatch.setattr(ret, "purge_enabled", lambda: False)

    def _must_not_purge(c):
        raise AssertionError("DRY-RUN must not purge anything")

    monkeypatch.setattr(ret, "purge_individual", _must_not_purge)
    summary = ret.run_reaper()
    assert summary["armed"] is False
    assert summary["candidates"] == 1
    assert len(summary["would_purge"]) == 1
    assert summary["would_purge"][0]["user_id"] == "usr-abc"
    assert summary["purged"] == []


def test_armed_purges_the_exact_user(monkeypatch):
    """ARMED sweep: purge_user_data is called with EXACTLY the candidate's user_id (delete mocked —
    no real destructive DB hit), and the control-plane rows are removed."""
    seen = {}
    monkeypatch.setattr(ret, "list_candidates", lambda **k: [dict(_CANDIDATE)])
    monkeypatch.setattr(saas_memory_ops, "purge_user_data",
                        lambda uid: seen.update(purged=uid) or {"schema_dropped": True})
    monkeypatch.setattr(ret, "_delete_login_and_seat",
                        lambda i, a, u: {"login_deleted": True, "seat_deleted": True})
    monkeypatch.setattr(ret._signup_ledger, "record_purge", lambda e, **k: True)

    summary = ret.run_reaper(force_enabled=True)
    assert summary["armed"] is True
    assert seen["purged"] == "usr-abc"          # the EXACT resolved user_id
    assert summary["purged"] == ["usr-abc"]
    assert summary["failed"] == []


def test_armed_purge_refuses_incomplete_candidate(monkeypatch):
    """purge_individual never deletes on a missing id — a broad/blank delete is refused."""
    called = {"purge": False}
    monkeypatch.setattr(saas_memory_ops, "purge_user_data",
                        lambda uid: called.update(purge=True))
    res = ret.purge_individual({"individual_id": "", "user_id": "", "account_id": ""})
    assert res["ok"] is False
    assert called["purge"] is False


def test_purge_preserves_ledger_before_teardown(monkeypatch):
    """The salted-hash ledger row is written BEFORE the account (and its plaintext email) is torn
    down, so a re-signup still walks the 3-strikes ladder."""
    order = []
    monkeypatch.setattr(ret._signup_ledger, "record_purge",
                        lambda e, **k: order.append("ledger"))
    monkeypatch.setattr(saas_memory_ops, "purge_user_data",
                        lambda uid: order.append("memory") or {})
    monkeypatch.setattr(ret, "_delete_login_and_seat",
                        lambda i, a, u: order.append("rows") or {"login_deleted": True,
                                                                 "seat_deleted": True})
    ret.purge_individual(dict(_CANDIDATE))
    assert order == ["ledger", "memory", "rows"]


def test_run_reaper_noop_without_candidates(monkeypatch):
    monkeypatch.setattr(ret, "list_candidates", lambda **k: [])
    summary = ret.run_reaper(force_enabled=True)
    assert summary["candidates"] == 0
    assert summary["purged"] == [] and summary["would_purge"] == []


# ══════════════════════════════════════════════════════════════════════════════════
# (5) THE LADDER AT THE SIGNUP ENDPOINT — transitions + attempt-time email address
# ══════════════════════════════════════════════════════════════════════════════════

SIGNUP_BODY = {"email": "Repeat.Person@Example.com", "password": "correct-horse",
               "invite_code": "INVITE", "display_name": "Repeat Person"}


@pytest.fixture
def signup_client(monkeypatch) -> TestClient:
    """A /me/signup client with the control-plane collaborators stubbed (no live DB). Approval is
    forced OFF so an ALLOWED attempt runs the direct provision+session flow."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(uc, "_email_taken", lambda e: False)
    monkeypatch.setattr(uc._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account",
                        lambda: "acc00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(uc._audit, "record", lambda *a, **k: None)
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(uc._platform_settings, "retention_grace_days", lambda: 7)
    # direct-provision flow collaborators
    monkeypatch.setattr(uc, "_provision_individual_seat", lambda a, u: {"ok": True, "reason": ""})
    monkeypatch.setattr(uc, "_create_individual",
                        lambda **kw: "ind00000-0000-4000-8000-000000000000")
    monkeypatch.setattr(uc._auth, "issue_individual_session", lambda **kw: "session-token")
    monkeypatch.setattr(uc._auth, "issue_individual_csrf", lambda **kw: "csrf")
    monkeypatch.setattr(uc, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(uc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_secret", "token_prefix": "flu_abc"})
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def test_attempt1_normal_signup(monkeypatch, signup_client):
    ledger = {}
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: sl.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt",
                        lambda e, **kw: ledger.update(email=e, outcome=kw.get("outcome")))
    spent = {"invite": False}
    monkeypatch.setattr(uc, "_consume_invite", lambda c: spent.update(invite=True) or True)
    monkeypatch.setattr(uc._smtp_sender, "send_cheeky_repeat_email",
                        lambda *a, **k: pytest.fail("no cheeky email on attempt 1"))

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200
    assert r.json()["status"] == "authenticated"
    assert spent["invite"] is True                                   # invite consumed
    assert ledger["outcome"] == sl.OUTCOME_ALLOWED
    assert ledger["email"] == "repeat.person@example.com"


def test_attempt2_cheeky_allows_and_emails_the_attempt_address(monkeypatch, signup_client):
    ledger = {}
    sent = {}
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: sl.DECISION_ALLOW_CHEEKY)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt",
                        lambda e, **kw: ledger.update(outcome=kw.get("outcome")))
    monkeypatch.setattr(uc, "_consume_invite", lambda c: True)
    monkeypatch.setattr(uc._smtp_sender, "send_cheeky_repeat_email",
                        lambda to, **kw: sent.update(to=to))
    monkeypatch.setattr(uc._smtp_sender, "send_final_rejection_email",
                        lambda *a, **k: pytest.fail("no rejection on attempt 2"))

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200                                       # ALLOWED (fresh trial)
    assert r.json()["status"] == "authenticated"
    assert ledger["outcome"] == sl.OUTCOME_ALLOWED_CHEEKY
    # The cheeky email goes to the ATTEMPT-TIME address (normalized), never a stored plaintext.
    assert sent["to"] == "repeat.person@example.com"


def test_attempt3_rejects_no_provision_no_invite_spend(monkeypatch, signup_client):
    ledger = {}
    sent = {}
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: sl.DECISION_REJECT)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt",
                        lambda e, **kw: ledger.update(outcome=kw.get("outcome")))
    monkeypatch.setattr(uc._smtp_sender, "send_final_rejection_email",
                        lambda to, **kw: sent.update(to=to))

    def _no_invite(c):
        raise AssertionError("a REJECTED signup must NOT spend the invite")

    def _no_provision(a, u):
        raise AssertionError("a REJECTED signup must NOT provision")

    monkeypatch.setattr(uc, "_consume_invite", _no_invite)
    monkeypatch.setattr(uc, "_provision_individual_seat", _no_provision)

    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 202                                       # bot-opaque, not activated
    body = r.json()
    assert body["status"] == "not_eligible"
    assert "api_key" not in body and "csrf_token" not in body
    assert not r.cookies                                             # no session issued
    assert ledger["outcome"] == sl.OUTCOME_REJECTED
    assert sent["to"] == "repeat.person@example.com"                 # rejection to attempt address


def test_signup_response_carries_retention_notice(monkeypatch, signup_client):
    """The confirmation tells the user their data is deleted after the trial unless they subscribe."""
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: sl.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt", lambda e, **kw: None)
    monkeypatch.setattr(uc, "_consume_invite", lambda c: True)
    r = signup_client.post("/me/signup", json=SIGNUP_BODY)
    assert r.status_code == 200
    notice = r.json().get("retention_notice", "").lower()
    assert "delete" in notice and "trial" in notice and "subscribe" in notice


# ══════════════════════════════════════════════════════════════════════════════════
# (6) OPERATOR DIGEST ENDPOINTS — operator-gated (403 without the operator credential)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


def test_digest_config_requires_operator(client):
    assert client.get("/admin/retention-report/config").status_code == 403


def test_digest_cadence_requires_operator(client):
    assert client.put("/admin/retention-report/cadence", json={"cadence_days": 7}).status_code == 403


def test_digest_send_now_requires_operator(client):
    assert client.post("/admin/retention-report/send-now", json={"to_self": True}).status_code == 403


def test_candidates_view_requires_operator(client):
    assert client.get("/admin/retention/candidates").status_code == 403


def test_non_operator_credential_cannot_read_digest(client):
    r = client.get("/admin/retention-report/config",
                   headers={"Authorization": "Bearer not-the-operator-key"})
    assert r.status_code == 403
