"""SaaS per-tenant origin ACL — behavior + safety PROOF (no live DB required).

Coverage:
  1. normalize_origin canonicalizes + rejects junk (path/scheme/blank).
  2. cors_allows: SAAS off → env allowlist only; SAAS on → the cors_allowlist() UNION only
     (env base ∪ control-plane global ∪ configured per-account origins ∪ own faces) — an
     arbitrary origin is NOT reflected (audit MED #6).
  3. origin_allowed_for_account (the post-auth gate): SAAS off → allow; ACL disabled → allow;
     no Origin (server-to-server) → allow; enabled + good/global/env → allow; enabled + bad → BLOCK.
  4. add_origin rejects malformed (400) and UPSERTs a good one; remove/list/toggle hit the CP.
  5. Everything is a NO-OP when SAAS is off (list/add/toggle) and never raises.

Run: python3 -m pytest tests/test_saas_origin_acl.py -q   (tests/ is gitignored → git add -f)
"""

import time

import pytest

from saas import acl as a


class _Cur:
    def __init__(self, responses, fail=False):
        self._responses = responses
        self._last = []
        self._fail = fail
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._fail:
            raise RuntimeError("forced failure")
        self._last = []
        self.rowcount = 1
        for needle, rows in self._responses:
            if needle in sql:
                self._last = rows
                self.rowcount = len(rows) if rows else 1
                break

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)

    def __enter__(self):
        return self

    def __exit__(self, *x):
        return False


class _Conn:
    def __init__(self, responses, fail=False):
        self.cur = _Cur(responses, fail=fail)

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass


class _CM:
    def __init__(self, conn):
        self._c = conn

    def __enter__(self):
        return self._c

    def __exit__(self, *x):
        return False


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")


@pytest.fixture
def saas_off(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)


@pytest.fixture
def primed(monkeypatch):
    """Prime the ACL cache so the resolver runs without a DB."""
    a._cache["global"] = {"https://global.example"}
    a._cache["accounts"] = {
        "acct-on": {"enabled": True, "origins": {"https://chat.acme.com"}},
        "acct-off": {"enabled": False, "origins": {"https://ignored.com"}},
    }
    a._cache["at"] = time.monotonic()
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://env.example")
    yield
    a.invalidate()


# ── 1. normalize ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("HTTP://Example.com:3000/", "http://example.com:3000"),
    ("https://chat.acme.com", "https://chat.acme.com"),
    ("https://a.b/", "https://a.b"),
    ("ftp://x", None),
    ("no-scheme", None),
    ("https://a/path", None),
    ("https://", None),
    ("", None),
    (None, None),
])
def test_normalize_origin(raw, expect):
    assert a.normalize_origin(raw) == expect


# ── 2. cors_allows ──────────────────────────────────────────────────────────────────

def test_cors_off_uses_env(saas_off, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://ok.example")
    assert a.cors_allows("https://ok.example") is True
    assert a.cors_allows("https://nope.example") is False
    assert a.cors_allows("garbage") is False


def test_cors_on_allowlists_configured_and_faces(saas_on, primed, monkeypatch):
    """SAAS on → an origin is echoed ONLY if it is in the allow-list union (env base ∪
    control-plane global ∪ every configured per-account origin ∪ the deployment's own faces)."""
    monkeypatch.setenv("SAAS_MCP_PUBLIC_URL", "https://app.faultline.ca")
    assert a.cors_allows("https://env.example") is True       # operator env base
    assert a.cors_allows("https://global.example") is True    # control-plane global
    assert a.cors_allows("https://chat.acme.com") is True     # a configured per-account origin
    assert a.cors_allows("https://app.faultline.ca") is True  # the deployment's own face


def test_cors_on_rejects_arbitrary_origin(saas_on, primed):
    """THE fix (audit MED #6): an arbitrary origin is NO LONGER reflected."""
    assert a.cors_allows("https://evil.example.com") is False
    assert a.cors_allows("http://192.0.2.10:3000") is False
    assert a.cors_allows("https://anything.at.all") is False
    assert a.cors_allows("garbage") is False   # still must be a valid origin


def test_cors_on_faces_derived_from_host_envs(saas_on, monkeypatch):
    """The app's tenant/admin host envs seed portless https face origins into the allow-list."""
    monkeypatch.setenv("SAAS_TENANT_HOSTS", "app.faultline.ca, mcp.faultline.ca:8002")
    monkeypatch.setenv("SAAS_ADMIN_HOSTS", "admin.example.ts.net")
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("SAAS_MCP_PUBLIC_URL", raising=False)
    # No live DB → force the control-plane load to fail-safe empty (env + faces only remain).
    monkeypatch.setattr(a.db, "connection",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    a.invalidate()
    assert a.cors_allows("https://app.faultline.ca") is True
    assert a.cors_allows("https://mcp.faultline.ca") is True   # :8002 stripped for the origin
    assert a.cors_allows("https://admin.example.ts.net") is True
    assert a.cors_allows("https://evil.example.com") is False


# ── 3. the post-auth per-tenant gate ────────────────────────────────────────────────

def test_gate_off_allows_all(saas_off, primed):
    assert a.origin_allowed_for_account("acct-on", "https://evil.com") is True


def test_gate_disabled_account_allows_any(saas_on, primed):
    assert a.origin_allowed_for_account("acct-off", "https://evil.com") is True


def test_gate_no_origin_is_server_to_server(saas_on, primed):
    assert a.origin_allowed_for_account("acct-on", None) is True
    assert a.origin_allowed_for_account("acct-on", "") is True


def test_gate_enabled_allows_listed(saas_on, primed):
    assert a.origin_allowed_for_account("acct-on", "https://chat.acme.com") is True     # own
    assert a.origin_allowed_for_account("acct-on", "https://global.example") is True    # global
    assert a.origin_allowed_for_account("acct-on", "https://env.example") is True       # env base


def test_gate_enabled_blocks_unlisted(saas_on, primed):
    assert a.origin_allowed_for_account("acct-on", "https://evil.com") is False


def test_gate_unknown_account_allows(saas_on, primed):
    assert a.origin_allowed_for_account("no-such-acct", "https://evil.com") is True


# ── 4. CRUD ─────────────────────────────────────────────────────────────────────────

def test_add_origin_rejects_malformed(saas_on):
    r = a.add_origin("acct1", "not-an-origin", created_by="tenant:acct1")
    assert r["ok"] is False and r["status_code"] == 400


def test_add_origin_upserts(saas_on, monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(a.db, "connection", lambda: _CM(conn))
    r = a.add_origin("acct1", "HTTPS://Chat.Acme.com/", created_by="tenant:acct1")
    assert r["ok"] is True and r["origin"] == "https://chat.acme.com"
    sql, params = conn.cur.executed[-1]
    assert "INSERT INTO public.allowed_origins" in sql and "ON CONFLICT DO NOTHING" in sql


def test_add_origin_global_uses_null(saas_on, monkeypatch):
    conn = _Conn([])
    monkeypatch.setattr(a.db, "connection", lambda: _CM(conn))
    a.add_origin(None, "https://g.example", created_by="operator:global")
    sql, _ = conn.cur.executed[-1]
    assert "VALUES (NULL," in sql


def test_toggle_and_list_noop_when_off(saas_off):
    assert a.set_acl_enabled("acct1", True) is False
    assert a.get_acl_enabled("acct1") is False
    assert a.list_origins("acct1") == []
    assert a.add_origin("acct1", "https://x.y")["ok"] is False


def test_remove_origin_scoped(saas_on, monkeypatch):
    conn = _Conn([("DELETE FROM public.allowed_origins", [(1,)])])
    monkeypatch.setattr(a.db, "connection", lambda: _CM(conn))
    assert a.remove_origin("acct1", 7) is True
    sql, params = conn.cur.executed[-1]
    assert "account_id = %s" in sql and params == (7, "acct1")


def test_load_swallows_error(saas_on, monkeypatch):
    conn = _Conn([], fail=True)
    monkeypatch.setattr(a.db, "connection", lambda: _CM(conn))
    a.invalidate()
    st = a._state()   # must not raise
    assert st == {"global": set(), "accounts": {}}
