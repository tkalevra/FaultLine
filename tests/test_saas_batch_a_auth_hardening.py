"""Batch A — SaaS auth hardening (SECURITY-AUDIT 2026-07-15). Behavior + fail-safe PROOF.

DB-free unit tests for the six Batch-A fixes:
  #1  MFA second-factor brute-force lockout       (saas.ratelimit.mfa_verify_locked/…)
  #3  login MFA-gate fails CLOSED on a DB error    (saas.mfa.require_mfa_decision)
  #4  WebAuthn RP/origin server-side allow-list    (saas.mfa.resolve_webauthn_rp)
  #11 MFA email-code resend throttle               (saas.ratelimit.mfa_email_send_allowed)
  #9  bench per-ACCOUNT concurrency/cooldown cap    (saas.bench_selftest.start_run)
  #12 bench teardown scrubs the copied brain key   (saas.bench_selftest._teardown_sandbox)

Drives a tiny in-memory FAKE Redis (no live Redis) + fake DB context managers (no live PG).
Run: python3 -m pytest tests/test_saas_batch_a_auth_hardening.py -q
"""

import fnmatch

import pytest

from saas import ratelimit as rl
from saas import mfa as M


# ── minimal in-memory FAKE Redis (only the commands the new code uses) ───────────────

class FakeRedis:
    def __init__(self, clock=None, fail=False):
        import time as _t
        self._store: dict[str, list] = {}
        self._clock = clock or _t.time
        self._fail = fail

    def _boom(self):
        if self._fail:
            raise RuntimeError("redis exploded")

    def _purge(self, key):
        item = self._store.get(key)
        if item and item[1] is not None and item[1] <= self._clock():
            self._store.pop(key, None)

    def ping(self):
        self._boom(); return True

    def incr(self, key):
        self._boom(); self._purge(key)
        cur = int(self._store.get(key, ["0", None])[0]) + 1
        exp = self._store.get(key, [None, None])[1]
        self._store[key] = [str(cur), exp]
        return cur

    def expire(self, key, secs):
        self._boom(); self._purge(key)
        if key in self._store:
            self._store[key][1] = self._clock() + secs
            return True
        return False

    def set(self, key, val, ex=None, nx=False):
        self._boom(); self._purge(key)
        if nx and key in self._store:
            return None
        self._store[key] = [str(val), (self._clock() + ex) if ex else None]
        return True

    def get(self, key):
        self._boom(); self._purge(key)
        item = self._store.get(key)
        return item[0] if item else None

    def pttl(self, key):
        self._boom(); self._purge(key)
        item = self._store.get(key)
        if item is None:
            return -2
        if item[1] is None:
            return -1
        return int((item[1] - self._clock()) * 1000)

    def delete(self, *keys):
        self._boom()
        n = 0
        for k in keys:
            if self._store.pop(k, None) is not None:
                n += 1
        return n

    def scan_iter(self, match="*", count=500):
        self._boom()
        for k in list(self._store.keys()):
            if fnmatch.fnmatch(k, match):
                yield k


class Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def fake(monkeypatch):
    fr = FakeRedis()
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    return fr


# ══════════════════════════════════════════════════════════════════════════════════
# #1 — MFA second-factor brute-force lockout
# ══════════════════════════════════════════════════════════════════════════════════

def test_mfa_lockout_off_never_locks(fake, monkeypatch):
    monkeypatch.delenv("SAAS_LOGIN_LOCKOUT_ENABLED", raising=False)
    monkeypatch.delenv("SAAS_MFA_LOCKOUT_ENABLED", raising=False)
    for _ in range(50):
        rl.record_mfa_failure("dashboard", "op-1")
    assert rl.mfa_verify_locked("dashboard", "op-1") == (False, 0)
    assert fake._store == {}   # off → never touches Redis


def test_mfa_lockout_follows_login_flag(fake, monkeypatch):
    # No explicit SAAS_MFA_LOCKOUT_ENABLED → follows the login-lockout flag.
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.delenv("SAAS_MFA_LOCKOUT_ENABLED", raising=False)
    monkeypatch.setenv("SAAS_MFA_MAX_FAILURES", "3")
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_WINDOW", "900")
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_SECONDS", "900")

    assert rl.mfa_verify_locked("dashboard", "u1")[0] is False
    rl.record_mfa_failure("dashboard", "u1")
    rl.record_mfa_failure("dashboard", "u1")
    assert rl.mfa_verify_locked("dashboard", "u1")[0] is False   # 2 < 3
    rl.record_mfa_failure("dashboard", "u1")                     # 3rd → locked
    locked, retry = rl.mfa_verify_locked("dashboard", "u1")
    assert locked is True and retry > 0


def test_mfa_lockout_success_clears(fake, monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_MAX_FAILURES", "2")
    rl.record_mfa_failure("individual", "ind-9")
    rl.record_mfa_failure("individual", "ind-9")
    assert rl.mfa_verify_locked("individual", "ind-9")[0] is True
    rl.record_mfa_success("individual", "ind-9")
    assert rl.mfa_verify_locked("individual", "ind-9") == (False, 0)


def test_mfa_lockout_is_per_principal_and_realm(fake, monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_MAX_FAILURES", "1")
    rl.record_mfa_failure("dashboard", "a")
    assert rl.mfa_verify_locked("dashboard", "a")[0] is True
    assert rl.mfa_verify_locked("dashboard", "b")[0] is False       # different principal
    assert rl.mfa_verify_locked("individual", "a")[0] is False      # different realm


def test_mfa_lockout_namespace_separate_from_password(fake, monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_MAX_FAILURES", "1")
    rl.record_mfa_failure("dashboard", "shared-id")
    # MFA lock must not read as a password lock (distinct key namespace).
    assert rl.login_locked("shared-id") == (False, 0)
    assert any(k.startswith("saas:mfalock:") for k in fake._store)


def test_mfa_lockout_fails_open_no_redis(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: None)
    assert rl.mfa_verify_locked("dashboard", "x") == (False, 0)   # never wrongly locks


def test_mfa_lockout_fails_open_on_redis_error(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: FakeRedis(fail=True))
    assert rl.mfa_verify_locked("dashboard", "x") == (False, 0)
    rl.record_mfa_failure("dashboard", "x")   # must not raise


def test_mfa_lockout_missing_identity_is_noop(fake, monkeypatch):
    monkeypatch.setenv("SAAS_MFA_LOCKOUT_ENABLED", "1")
    assert rl.mfa_verify_locked("dashboard", None) == (False, 0)
    assert rl.mfa_verify_locked("", "id") == (False, 0)


# ══════════════════════════════════════════════════════════════════════════════════
# #4 — MFA email-code resend throttle
# ══════════════════════════════════════════════════════════════════════════════════

def test_email_resend_disabled_flag(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_ENABLED", "0")
    fr = FakeRedis()
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    for _ in range(100):
        assert rl.mfa_email_send_allowed("f1") == (True, 0)
    assert fr._store == {}


def test_email_resend_min_interval(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_MIN_INTERVAL", "30")
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_HOURLY_MAX", "8")
    clock = Clock(1000.0)
    fr = FakeRedis(clock=clock)
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    monkeypatch.setattr(rl, "time", clock)

    ok, _ = rl.mfa_email_send_allowed("fac")
    assert ok is True
    ok2, retry = rl.mfa_email_send_allowed("fac")   # immediate resend → blocked
    assert ok2 is False and retry > 0
    clock.advance(31)                                # interval elapsed → allowed again
    assert rl.mfa_email_send_allowed("fac")[0] is True


def test_email_resend_hourly_cap(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_MIN_INTERVAL", "1")
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_HOURLY_MAX", "3")
    clock = Clock(1000.0)
    fr = FakeRedis(clock=clock)
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    monkeypatch.setattr(rl, "time", clock)

    allowed = 0
    for _ in range(10):
        if rl.mfa_email_send_allowed("fac2")[0]:
            allowed += 1
        clock.advance(2)   # step past the min-interval each time
    assert allowed == 3    # hourly cap holds independent of interval


def test_email_resend_per_factor(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_ENABLED", "1")
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_MIN_INTERVAL", "30")
    fr = FakeRedis()
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    assert rl.mfa_email_send_allowed("A")[0] is True
    assert rl.mfa_email_send_allowed("A")[0] is False   # A throttled
    assert rl.mfa_email_send_allowed("B")[0] is True     # B independent


def test_email_resend_fails_open(monkeypatch):
    monkeypatch.setenv("SAAS_MFA_EMAIL_RESEND_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: None)
    assert rl.mfa_email_send_allowed("f") == (True, 0)
    monkeypatch.setattr(rl, "_get_client", lambda: FakeRedis(fail=True))
    assert rl.mfa_email_send_allowed("f") == (True, 0)


# ══════════════════════════════════════════════════════════════════════════════════
# #3 — login MFA gate fails CLOSED on a DB error (require_mfa_decision)
# ══════════════════════════════════════════════════════════════════════════════════

class _FakeCur:
    def __init__(self, exists):
        self._exists = exists

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (self._exists,)


class _FakeConn:
    def __init__(self, exists):
        self._exists = exists

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur(self._exists)


def _patch_db(monkeypatch, *, exists=None, raise_conn=False):
    def _conn():
        if raise_conn:
            raise RuntimeError("pool exhausted")
        return _FakeConn(exists)
    monkeypatch.setattr(M.db, "connection", _conn)


def test_require_mfa_decision_true_false(monkeypatch):
    _patch_db(monkeypatch, exists=True)
    assert M.require_mfa_decision("dashboard", "u") is True
    _patch_db(monkeypatch, exists=False)
    assert M.require_mfa_decision("dashboard", "u") is False


def test_require_mfa_decision_raises_on_db_error(monkeypatch):
    _patch_db(monkeypatch, raise_conn=True)
    with pytest.raises(M.MfaError):
        M.require_mfa_decision("dashboard", "u")   # fail CLOSED — caller denies (503)


def test_require_mfa_decision_bad_realm_raises(monkeypatch):
    with pytest.raises(M.MfaError):
        M.require_mfa_decision("nope", "u")


def test_legacy_gate_still_fails_open(monkeypatch):
    # The fail-SAFE reader is UNCHANGED (returns False on error) — only the login gate is strict.
    _patch_db(monkeypatch, raise_conn=True)
    assert M.principal_has_enabled_factor("dashboard", "u") is False


# ══════════════════════════════════════════════════════════════════════════════════
# #7 — WebAuthn RP/origin server-side allow-list (resolve_webauthn_rp)
# ══════════════════════════════════════════════════════════════════════════════════

def _clear_webauthn_env(monkeypatch):
    for v in ("SAAS_WEBAUTHN_RP_ID", "SAAS_WEBAUTHN_ORIGIN", "SAAS_WEBAUTHN_ALLOWED_ORIGINS",
              "SAAS_MCP_PUBLIC_URL", "SAAS_TENANT_HOSTS", "SAAS_ADMIN_HOSTS"):
        monkeypatch.delenv(v, raising=False)


def test_webauthn_unconfigured_keeps_localhost_dev(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    rp, origin = M.resolve_webauthn_rp("http://localhost:5173")
    assert rp == "localhost" and origin == "http://localhost:5173"
    # any https host also accepted when no allow-list configured
    rp2, _ = M.resolve_webauthn_rp("https://anything.example")
    assert rp2 == "anything.example"


def test_webauthn_allowlist_accepts_listed(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "app.faultline.ca, mcp.faultline.ca:443")
    rp, origin = M.resolve_webauthn_rp("https://app.faultline.ca")
    assert rp == "app.faultline.ca" and origin == "https://app.faultline.ca"


def test_webauthn_allowlist_rejects_unlisted(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "app.faultline.ca")
    assert M.resolve_webauthn_rp("https://evil.example") == (None, None)
    # localhost is NOT a free pass once an allow-list is configured
    assert M.resolve_webauthn_rp("http://localhost:5173") == (None, None)


def test_webauthn_allowlist_from_public_url_and_admin(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    monkeypatch.setenv("SAAS_MCP_PUBLIC_URL", "https://mcp.faultline.ca/base")
    monkeypatch.setenv("SAAS_ADMIN_HOSTS", "ops.faultline.ts.net")
    assert M.resolve_webauthn_rp("https://mcp.faultline.ca")[0] == "mcp.faultline.ca"
    assert M.resolve_webauthn_rp("https://ops.faultline.ts.net")[0] == "ops.faultline.ts.net"
    assert M.resolve_webauthn_rp("https://app.faultline.ca") == (None, None)  # not in this set


def test_webauthn_explicit_allowed_origins_override(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    monkeypatch.setenv("SAAS_WEBAUTHN_ALLOWED_ORIGINS", "https://a.example, b.example")
    assert M.resolve_webauthn_rp("https://a.example")[0] == "a.example"
    assert M.resolve_webauthn_rp("https://b.example")[0] == "b.example"
    assert M.resolve_webauthn_rp("https://c.example") == (None, None)


def test_webauthn_env_pin_still_wins(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    monkeypatch.setenv("SAAS_WEBAUTHN_RP_ID", "pinned.example")
    monkeypatch.setenv("SAAS_WEBAUTHN_ORIGIN", "https://pinned.example")
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "app.faultline.ca")
    # explicit pin short-circuits before any Origin parsing / allow-list check
    assert M.resolve_webauthn_rp("https://whatever.example") == (
        "pinned.example", "https://pinned.example")


def test_webauthn_rejects_http_non_local(monkeypatch):
    _clear_webauthn_env(monkeypatch)
    assert M.resolve_webauthn_rp("http://plain.example") == (None, None)
    assert M.resolve_webauthn_rp("") == (None, None)


# ══════════════════════════════════════════════════════════════════════════════════
# #12 — bench teardown scrubs the copied Fernet brain key (even on delete error path)
# ══════════════════════════════════════════════════════════════════════════════════

def test_teardown_scrubs_brain_key_even_when_delete_fails(monkeypatch):
    from saas import bench_selftest as B

    calls = {"delete_config": []}
    monkeypatch.setattr(B._llm_config, "delete_config",
                        lambda acct: calls["delete_config"].append(acct) or True)
    # Force the account-delete path to blow up — scrub must still have run.
    def _boom(*a, **k):
        raise RuntimeError("delete failed")
    monkeypatch.setattr(B._accounts, "set_account_status", _boom)
    monkeypatch.setattr(B._accounts, "delete_account", _boom)
    # No DB / Qdrant work in this unit test.
    monkeypatch.setattr(B, "psycopg2", None)
    monkeypatch.setattr(B, "httpx", None)

    out = B._teardown_sandbox("acct-x", "slug-x", "00000000-0000-0000-0000-000000000000")
    assert calls["delete_config"] == ["acct-x"]
    assert out["brain_key_scrubbed"] is True


# ══════════════════════════════════════════════════════════════════════════════════
# #9 — bench per-ACCOUNT concurrency + cooldown cap
# ══════════════════════════════════════════════════════════════════════════════════

def test_bench_per_account_concurrency_cap(monkeypatch):
    import asyncio
    from saas import bench_selftest as B
    monkeypatch.setattr(B, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(B._llm_config, "resolve_account_llm_config",
                        lambda acct: {"backend_type": "openai", "model": "m", "base_url": "u"})
    B._RUNS.clear()
    B._ACCOUNT_RUNS.clear()
    # Simulate one seat of the account already running.
    B._ACCOUNT_RUNS["acct"] = {"inflight": 1, "last_finished": 0.0}

    res = asyncio.run(B.start_run("user-2", "acct"))
    assert res["status_code"] == 409   # a run is already in progress FOR THE ACCOUNT
    B._RUNS.clear(); B._ACCOUNT_RUNS.clear()


def test_bench_per_account_cooldown(monkeypatch):
    import asyncio
    import time as _t
    from saas import bench_selftest as B
    monkeypatch.setattr(B, "saas_mode_enabled", lambda: True)
    monkeypatch.setattr(B._llm_config, "resolve_account_llm_config",
                        lambda acct: {"backend_type": "openai", "model": "m", "base_url": "u"})
    monkeypatch.setattr(B, "_ACCOUNT_COOLDOWN_S", 600)
    B._RUNS.clear()
    B._ACCOUNT_RUNS.clear()
    # Account finished a run seconds ago (cooldown still active), no run in flight.
    B._ACCOUNT_RUNS["acct"] = {"inflight": 0, "last_finished": _t.time()}

    res = asyncio.run(B.start_run("user-3", "acct"))
    assert res["status_code"] == 429 and res.get("retry_after", 0) > 0
    B._RUNS.clear(); B._ACCOUNT_RUNS.clear()
