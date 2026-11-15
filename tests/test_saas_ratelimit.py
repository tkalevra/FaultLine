"""Redis-backed per-account rate limit + auth brute-force lockout — behavior + fail-open PROOF.

The counters live in REDIS (shared across processes / replicas), reusing the app's existing
Redis (REDIS_URL). These tests drive a small in-memory FAKE Redis injected via
``ratelimit._get_client`` — no live Redis needed. Coverage:

  1. rate limit is a no-op (always allow) when the flag is OFF (permissive default).
  2. fixed-window counter denies past the max, resets in the next window (Redis INCR).
  3. rate limiter FAILS OPEN when Redis is down (client None) AND on any Redis error.
  4. login lockout is a no-op when its flag is OFF.
  5. N failures arm a Redis-backed lockout; a success clears it; the lock TTL expires.
  6. lockout FAILS OPEN when Redis is down AND on any Redis error.
  7. counters are SHARED via Redis — two logical "processes" see the same counter.
  8. tenant + operator login handlers enforce the lockout (429 before the argon2 verify).
  9. the /mcp auth chokepoint 429s an over-limit account (integration through require_auth).

Run: python3 -m pytest tests/test_saas_ratelimit.py -q
"""

import fnmatch

import pytest

from saas import ratelimit as rl


# ── minimal in-memory FAKE Redis (only the commands ratelimit.py uses) ──────────────

class FakeRedis:
    """A tiny, TTL-aware, process-shared stand-in for redis.Redis (decode_responses=True).

    Shared-ness is the whole point: one FakeRedis instance = one Redis = the shared store
    that every 'process' reads/writes. A controllable ``clock`` drives TTL expiry.
    """

    def __init__(self, clock=None, fail=False):
        import time as _t
        self._store: dict[str, list] = {}   # key -> [value:str, expire_at:float|None]
        self._clock = clock or _t.time
        self._fail = fail                   # force every command to raise (Redis-error path)

    def _boom(self):
        if self._fail:
            raise RuntimeError("redis exploded")

    def _purge(self, key):
        item = self._store.get(key)
        if item and item[1] is not None and item[1] <= self._clock():
            self._store.pop(key, None)

    def ping(self):
        self._boom()
        return True

    def incr(self, key):
        self._boom()
        self._purge(key)
        cur = int(self._store.get(key, ["0", None])[0]) + 1
        exp = self._store.get(key, [None, None])[1]
        self._store[key] = [str(cur), exp]
        return cur

    def expire(self, key, secs):
        self._boom()
        self._purge(key)
        if key in self._store:
            self._store[key][1] = self._clock() + secs
            return True
        return False

    def set(self, key, val, ex=None, nx=False):
        self._boom()
        self._purge(key)
        if nx and key in self._store:
            return None
        self._store[key] = [str(val), (self._clock() + ex) if ex else None]
        return True

    def get(self, key):
        self._boom()
        self._purge(key)
        item = self._store.get(key)
        return item[0] if item else None

    def pttl(self, key):
        self._boom()
        self._purge(key)
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

    def time(self):          # so it can stand in for the `time` module (rl.time)
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def fake(monkeypatch):
    """Inject a fresh FakeRedis as the shared store for the ratelimit module."""
    fr = FakeRedis()
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    return fr


# ── 1: rate limit no-op when off ─────────────────────────────────────────────────────

def test_rate_limit_off_always_allows(fake, monkeypatch):
    monkeypatch.delenv("SAAS_RATE_LIMIT_ENABLED", raising=False)
    for _ in range(1000):
        assert rl.allow_request("account:x") is True
    assert fake._store == {}   # off → never even touches Redis


# ── 2: fixed-window counter denies past max, resets next window ──────────────────────

def test_fixed_window_denies_and_resets(monkeypatch):
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_MAX", "3")
    monkeypatch.setenv("SAAS_RATE_LIMIT_WINDOW", "10")
    clock = Clock(1000.0)
    fr = FakeRedis(clock=clock)
    monkeypatch.setattr(rl, "_get_client", lambda: fr)
    monkeypatch.setattr(rl, "time", clock)   # rl.time.time() -> clock()

    p = "account:w"
    assert [rl.allow_request(p) for _ in range(4)] == [True, True, True, False]
    clock.advance(10)                        # next fixed window → counter key changes
    assert rl.allow_request(p) is True


def test_buckets_are_per_principal(fake, monkeypatch):
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_MAX", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_WINDOW", "100")
    assert rl.allow_request("account:a") is True
    assert rl.allow_request("account:a") is False   # a drained
    assert rl.allow_request("account:b") is True      # b independent


# ── 3: rate limiter fails open — Redis down AND Redis error ──────────────────────────

def test_rate_limit_fails_open_no_redis(monkeypatch):
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: None)   # Redis unreachable
    assert rl.allow_request("account:x") is True


def test_rate_limit_fails_open_on_redis_error(monkeypatch):
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: FakeRedis(fail=True))
    assert rl.allow_request("account:x") is True   # INCR raises → allowed


# ── 4: lockout no-op when off ────────────────────────────────────────────────────────

def test_lockout_off_never_locks(fake, monkeypatch):
    monkeypatch.delenv("SAAS_LOGIN_LOCKOUT_ENABLED", raising=False)
    for _ in range(100):
        rl.record_login_failure("bob")
    assert rl.login_locked("bob") == (False, 0)
    assert fake._store == {}


# ── 5: N failures arm the Redis lock; success clears; TTL expires ────────────────────

def test_lockout_after_threshold_and_clear(fake, monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_WINDOW", "900")
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_SECONDS", "900")

    assert rl.login_locked("bob")[0] is False
    rl.record_login_failure("bob")
    rl.record_login_failure("bob")
    assert rl.login_locked("bob")[0] is False   # under threshold
    rl.record_login_failure("bob")              # 3rd → locked
    locked, retry = rl.login_locked("bob")
    assert locked is True and retry > 0
    rl.record_login_success("bob")              # clears the lock
    assert rl.login_locked("bob") == (False, 0)


def test_lockout_case_insensitive(fake, monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_LOGIN_MAX_FAILURES", "2")
    rl.record_login_failure("Bob")
    rl.record_login_failure("bOb")   # same identifier, different case
    assert rl.login_locked("BOB")[0] is True


def test_lockout_expires_after_cooldown(monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_LOGIN_MAX_FAILURES", "1")
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_SECONDS", "60")
    clock = Clock(500.0)
    fr = FakeRedis(clock=clock)
    monkeypatch.setattr(rl, "_get_client", lambda: fr)

    rl.record_login_failure("bob")          # threshold 1 → locked
    assert rl.login_locked("bob")[0] is True
    clock.advance(61)                        # past the cooldown TTL
    assert rl.login_locked("bob")[0] is False


# ── 6: lockout fails open — Redis down AND Redis error ───────────────────────────────

def test_lockout_fails_open_no_redis(monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: None)
    assert rl.login_locked("bob") == (False, 0)
    rl.record_login_failure("bob")   # must not raise


def test_lockout_fails_open_on_redis_error(monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setattr(rl, "_get_client", lambda: FakeRedis(fail=True))
    assert rl.login_locked("bob") == (False, 0)   # pttl raises → fail-open
    rl.record_login_failure("bob")                # incr raises → swallowed


# ── 7: counters are SHARED via Redis (two logical processes see one counter) ─────────

def test_rate_counter_shared_across_processes(monkeypatch):
    """Two 'processes' pointing at the SAME Redis share the per-account counter."""
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_MAX", "2")
    monkeypatch.setenv("SAAS_RATE_LIMIT_WINDOW", "100")
    shared = FakeRedis()
    # process A and process B both resolve to the SAME shared Redis instance.
    monkeypatch.setattr(rl, "_get_client", lambda: shared)
    assert rl.allow_request("account:shared") is True   # proc A → count 1
    assert rl.allow_request("account:shared") is True   # proc B → count 2 (shared!)
    assert rl.allow_request("account:shared") is False  # proc A again → count 3 > max
    # the counter is in Redis, not any process:
    assert any(k.startswith("saas:rl:account:shared:") for k in shared._store)


def test_lockout_shared_across_processes(monkeypatch):
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_LOGIN_MAX_FAILURES", "2")
    shared = FakeRedis()
    monkeypatch.setattr(rl, "_get_client", lambda: shared)
    rl.record_login_failure("dave")   # process A records failure #1
    rl.record_login_failure("dave")   # process B records failure #2 (same Redis) → locks
    assert rl.login_locked("dave")[0] is True   # any process now sees the lock


# ── 8: login handlers enforce the lockout ────────────────────────────────────────────

def _console_client(monkeypatch, module, shared):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_LOGIN_LOCKOUT_ENABLED", "1")
    monkeypatch.setenv("SAAS_LOGIN_MAX_FAILURES", "2")
    monkeypatch.setattr(rl, "_get_client", lambda: shared)   # shared Redis for the limiter
    monkeypatch.setattr(module._auth, "get_user_by_username", lambda u: None)  # unknown → fail
    app = FastAPI()
    app.include_router(module.router)
    return TestClient(app)


def test_tenant_login_locks_out(monkeypatch):
    from saas import tenant_console as tc
    c = _console_client(monkeypatch, tc, FakeRedis())
    body = {"username": "attacker", "password": "x"}
    assert c.post("/tenant/login", json=body).status_code == 401
    assert c.post("/tenant/login", json=body).status_code == 401
    r = c.post("/tenant/login", json=body)   # now locked
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_operator_login_locks_out(monkeypatch):
    from saas import operator_console as oc
    c = _console_client(monkeypatch, oc, FakeRedis())
    body = {"username": "attacker", "password": "x"}
    assert c.post("/operator/login", json=body).status_code == 401
    assert c.post("/operator/login", json=body).status_code == 401
    assert c.post("/operator/login", json=body).status_code == 429


def test_tenant_and_operator_planes_separate(monkeypatch):
    """A tenant lockout must not lock the operator plane (namespaced identifiers)."""
    from saas import tenant_console as tc
    from saas import operator_console as oc
    shared = FakeRedis()   # one Redis, but the planes use different identifier namespaces
    tclient = _console_client(monkeypatch, tc, shared)
    body = {"username": "shared", "password": "x"}
    tclient.post("/tenant/login", json=body)
    tclient.post("/tenant/login", json=body)
    assert tclient.post("/tenant/login", json=body).status_code == 429  # tenant locked
    oclient = _console_client(monkeypatch, oc, shared)
    assert oclient.post("/operator/login", json=body).status_code == 401  # operator NOT locked


# ── 9: /mcp auth chokepoint 429s an over-limit account ───────────────────────────────

def test_require_auth_rate_limits(monkeypatch, reload_http_server):
    """The reload goes through the ``reload_http_server`` fixture (tests/conftest.py).

    ``importlib.reload`` on http_server is process-wide — it rebuilds ``h.app``. The hand-rolled
    version here restored by reloading inside a ``finally`` AFTER ``monkeypatch.delenv("SAAS_MODE")``,
    so the module every later test module shared was one built with SaaS OFF: no /admin, /operator
    or /tenant routers mounted, and blanket 404s for the rest of the session. The fixture restores
    the environment captured at SETUP before its closing reload, so the reload cannot leak.
    """
    monkeypatch.setenv("MCP_API_KEY", "k")
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("SAAS_RATE_LIMIT_MAX", "2")
    monkeypatch.setenv("SAAS_RATE_LIMIT_WINDOW", "100")   # one window for the whole test
    monkeypatch.delenv("FAULTLINE_USER_ID", raising=False)
    hs = reload_http_server()
    monkeypatch.setattr(hs._mcp, "FAULTLINE_USER_ID", "")   # neutralize C5 start-guard
    _shared = FakeRedis()                                    # ONE shared Redis across calls
    monkeypatch.setattr(rl, "_get_client", lambda: _shared)
    monkeypatch.setattr(hs, "_resolve_principal", lambda c: "account:rl-test")
    from fastapi.testclient import TestClient
    with TestClient(hs.app) as client:
        h = {"Authorization": "Bearer k"}
        codes = [client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                           "method": "ping"}, headers=h).status_code
                 for _ in range(5)]
    assert 429 in codes   # the shared Redis counter drains and later calls throttle
