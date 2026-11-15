"""SaaS IP allow/deny rules (migration 303, BUG-05) — behavior + safety PROOF (no live DB).

Mirrors tests/test_saas_origin_acl.py's harness shape (fake cursors + a primed cache):
  1. normalize_cidr canonicalizes + rejects junk (garbage strings, bare prefix ints, zones,
     impossible masks); accepts single IPs (/32, /128) and ranges; canonicalizes host bits.
  2. decide(): deny rule blocks · global ∪ account merge · most-specific (longest-prefix)
     wins · deny beats allow on an exact tie · NO rule / no account / unparseable IP /
     control-plane error / SAAS off → ALLOW (the documented fail-open posture, matching the
     origin ACL) · an IPv6 rule never decides an IPv4 client (version guard).
  3. explain(): the operator dry-run names the winning rule (cidr/action/scope).
  4. CRUD round-trip via a fake control plane: add (canonicalized, idempotent) · list
     (tenant + global scopes) · remove (scoped delete) · 400 on malformed cidr/action.
  5. The enforcement seam src/mcp/http_server.py::_saas_ip_denied: SAAS off → False;
     denied → True; module/lookup error → False (fail-open, never wrongly blocks).

Run: python3 -m pytest tests/test_saas_ip_acl.py -q   (tests/ is gitignored → git add -f)
"""

import time
from datetime import datetime, timezone

import pytest

from saas import ip_acl as m


def _ts():
    return datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, responses=None, fail=False):
        self._responses = responses or []
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
    def __init__(self, responses=None, fail=False):
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
    """Prime the rule cache: one global deny /24, one global allow /16, one tenant rule."""
    m._cache["global"] = [("203.0.113.0/24", "deny"), ("192.168.0.0/16", "allow")]
    m._cache["accounts"] = {
        "acct-on": [("192.168.40.0/24", "deny")],
    }
    m._cache["at"] = time.monotonic()
    yield
    m.invalidate()


# ── 1. normalize_cidr ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("203.0.113.7", "203.0.113.7/32"),           # single IPv4 → /32
    ("2001:db8::1", "2001:db8::1/128"),          # single IPv6 → /128
    ("203.0.113.0/24", "203.0.113.0/24"),
    ("203.0.113.9/24", "203.0.113.0/24"),        # host bits canonicalized away
    ("  10.0.0.0/8 ", "10.0.0.0/8"),            # whitespace trimmed
    ("0.0.0.0/0", "0.0.0.0/0"),                 # the allow-only floor composes
    # rejects:
    ("office", None),
    ("24", None),                                # a bare prefix is not a rule
    ("10.0.0.0/99", None),                      # impossible mask
    ("::1/129", None),
    ("fe80::1%eth0", None),                      # a zone id is not a rule
    ("10.0.0.0.5", None),
    ("", None),
    (None, None),
])
def test_normalize_cidr(raw, expect):
    assert m.normalize_cidr(raw) == expect


# ── 2. decide ────────────────────────────────────────────────────────────────────────

def test_deny_rule_blocks(saas_on, primed):
    assert m.decide("acct-on", "203.0.113.7") == "deny"      # global /24 deny


def test_no_rule_matches_allows(saas_on, primed):
    assert m.decide("acct-on", "198.51.100.7") == "allow"    # default allow


def test_global_account_merge_and_specificity(saas_on, primed):
    # global allow 192.168.0.0/16 vs TENANT deny 192.168.40.0/24 → the /24 wins → deny.
    assert m.decide("acct-on", "192.168.40.20") == "deny"
    # same global allow, outside the tenant hole → allow.
    assert m.decide("acct-on", "192.168.41.20") == "allow"
    # an account with NO own rules still inherits global rules.
    assert m.decide("acct-other", "192.168.40.20") == "allow"
    assert m.decide("acct-other", "203.0.113.9") == "deny"


def test_deny_beats_allow_on_tie(saas_on, monkeypatch):
    m._cache["global"] = [("10.1.2.0/24", "allow"), ("10.1.2.0/24", "deny")]
    m._cache["accounts"] = {}
    m._cache["at"] = time.monotonic()
    try:
        assert m.decide("acct-x", "10.1.2.3") == "deny"
    finally:
        m.invalidate()


def test_ipv6_rule_never_decides_ipv4(saas_on, monkeypatch):
    m._cache["global"] = [("2001:db8::/32", "deny"), ("0.0.0.0/0", "deny"),
                          ("203.0.113.0/24", "allow")]
    m._cache["accounts"] = {}
    m._cache["at"] = time.monotonic()
    try:
        # version guard: the v6 deny cannot touch a v4 client; the /0 deny floor does.
        assert m.decide("acct-x", "192.0.2.9") == "deny"
        # a v6 client inside the v6 deny is denied; the v4 floor cannot touch it.
        assert m.decide("acct-x", "2001:db8::1") == "deny"
        # most-specific beats the floor: /24 allow over /0 deny.
        assert m.decide("acct-x", "203.0.113.1") == "allow"
        assert m.decide("acct-x", "203.0.114.1") == "deny"
    finally:
        m.invalidate()


def test_failopen_postures(saas_on, primed, monkeypatch):
    assert m.decide(None, "203.0.113.7") == "deny"            # global rules apply w/o account
    assert m.decide("acct-on", "not-an-ip") == "allow"        # unparseable client IP
    assert m.decide("acct-on", "") == "allow"
    assert m.decide("acct-on", None) == "allow"


def test_control_plane_error_fails_open(saas_on, monkeypatch):
    """A DB error during the (uncached) load contributes NO rules → allow."""
    m.invalidate()
    import saas.db as db
    monkeypatch.setattr(db, "connection", lambda: _CM(_Conn(fail=True)))
    assert m.decide("acct-on", "203.0.113.7") == "allow"


def test_saas_off_is_a_noop(saas_off):
    assert m.decide("acct-on", "203.0.113.7") == "allow"
    assert m.ip_denied("acct-on", "203.0.113.7") is False
    assert m.list_ip_rules(None) == []
    assert m.add_ip_rule(None, "10.0.0.0/8", "deny")["status_code"] == 409


# ── 3. explain (operator dry-run) ────────────────────────────────────────────────────

def test_mapped_ipv6_cannot_dodge_v4_rules(saas_on, monkeypatch):
    """Critic round 1 B1: ::ffff:a.b.c.d (dual-stack peer / XFF shape) must evaluate as its
    IPv4 form — before the unwrap it matched NO v4 rule, not even a 0.0.0.0/0 deny floor."""
    m._cache["global"] = [("0.0.0.0/0", "deny"), ("203.0.113.0/24", "allow")]
    m._cache["accounts"] = {}
    m._cache["at"] = time.monotonic()
    try:
        for form in ("203.0.113.7", "::ffff:203.0.113.7"):
            assert m.decide("acct-x", form) == "allow", form      # the /24 hole
        for form in ("192.0.2.9", "::ffff:192.0.2.9"):
            assert m.decide("acct-x", form) == "deny", form       # the floor
        # the write side canonicalizes a mapped RULE to v4 too
        assert m.normalize_cidr("::ffff:203.0.113.7") == "203.0.113.7/32"
        assert m.normalize_cidr("::ffff:203.0.113.0/120") == "203.0.113.0/24"
        # and explain agrees with decide on the mapped form
        assert m.explain("acct-x", "::ffff:192.0.2.9")["decision"] == "deny"
    finally:
        m.invalidate()


def test_explain_names_the_winning_rule(saas_on, primed):
    r = m.explain("acct-on", "192.168.40.20")
    assert r["decision"] == "deny"
    assert r["matched"]["cidr"] == "192.168.40.0/24"
    assert r["matched"]["action"] == "deny"
    assert r["matched"]["scope"] == "account"
    r2 = m.explain("acct-on", "198.51.100.1")
    assert r2["decision"] == "allow" and r2["matched"] is None


# ── 4. CRUD round-trip (fake control plane) ──────────────────────────────────────────

@pytest.fixture
def fake_db(monkeypatch):
    import saas.db as db
    # two query shapes hit the same table: _load (3 cols) and list_ip_rules (6 cols,
    # recognizable by its ORDER BY). The matcher takes the FIRST needle that fits, so the
    # list-shaped needle is declared first.
    load_rows = [(None, "203.0.113.0/24", "deny"), ("acct-on", "192.168.40.0/24", "allow")]
    list_rows = [(1, "203.0.113.0/24", "deny", None, "operator:global", _ts()),
                 (2, "192.168.40.0/24", "allow", None, "operator", _ts())]
    global_rows = [(1, "203.0.113.0/24", "deny", None, "operator:global", _ts())]
    tenant_rows = [(2, "192.168.40.0/24", "allow", None, "operator", _ts())]

    class _ScopedCur(_Cur):
        """Honor the WHERE scope the real SQL carries (plain _Cur ignores it): the list
        query answers by its account predicate, not by table."""
        def execute(self, sql, params=None):
            if "ORDER BY created_at DESC" in sql and "saas_ip_rules" in sql:
                self.executed.append((sql, params))
                self._last = global_rows if "account_id IS NULL" in sql else tenant_rows
                self.rowcount = len(self._last)
                return
            super().execute(sql, params)

    class _ScopedConn(_Conn):
        def __init__(self):
            super().__init__(responses=[("FROM public.saas_ip_rules", load_rows)])
            self.cur = _ScopedCur([("FROM public.saas_ip_rules", load_rows)])

    conn = _ScopedConn()
    monkeypatch.setattr(db, "connection", lambda: _CM(conn))
    m.invalidate()
    yield conn
    m.invalidate()


def test_add_rejects_malformed(saas_on, fake_db):
    r = m.add_ip_rule("acct-on", "not-a-network", "deny")
    assert not r["ok"] and r["status_code"] == 400
    r = m.add_ip_rule("acct-on", "10.0.0.0/8", "block")
    assert not r["ok"] and r["status_code"] == 400


def test_add_canonicalizes(saas_on, fake_db):
    r = m.add_ip_rule("acct-on", "10.9.9.9/8", "deny")
    assert r["ok"] and r["cidr"] == "10.0.0.0/8"
    sql, params = fake_db.cur.executed[-1]
    assert "INSERT INTO public.saas_ip_rules" in sql
    assert params[1] == "10.0.0.0/8" and params[2] == "deny"


def test_list_scopes_and_shapes(saas_on, fake_db):
    glob = m.list_ip_rules(None)
    own = m.list_ip_rules("acct-on")
    assert [g["cidr"] for g in glob] == ["203.0.113.0/24"]
    assert [o["cidr"] for o in own] == ["192.168.40.0/24"]
    sql, params = fake_db.cur.executed[-1]
    assert "account_id = %s" in sql and params == ("acct-on",)
    gsql, gparams = fake_db.cur.executed[-2]
    assert "account_id IS NULL" in gsql and gparams == tuple()


def test_effective_payload(saas_on, fake_db):
    eff = m.effective_ip_rules("acct-on")
    assert [r["cidr"] for r in eff["rules"]] == ["192.168.40.0/24"]
    assert [r["cidr"] for r in eff["global"]] == ["203.0.113.0/24"]


def test_remove_is_scoped(saas_on, fake_db):
    assert m.remove_ip_rule("acct-on", 5) is True
    sql, params = fake_db.cur.executed[-1]
    assert "DELETE FROM public.saas_ip_rules" in sql
    assert "account_id = %s" in sql and params == (5, "acct-on")
    gsql, gparams = m, m.remove_ip_rule(None, 7)
    # global delete has no account predicate
    sql2, params2 = fake_db.cur.executed[-1]
    assert "account_id IS NULL" in sql2 and params2 == (7,)


def test_crud_never_raises_on_db_error(saas_on, monkeypatch):
    import saas.db as db
    monkeypatch.setattr(db, "connection", lambda: _CM(_Conn(fail=True)))
    m.invalidate()
    assert m.list_ip_rules(None) == []
    r = m.add_ip_rule(None, "10.0.0.0/8", "deny")
    assert not r["ok"] and r["status_code"] == 500
    assert m.remove_ip_rule(None, 1) is False


# ── 5. the enforcement seam in src/mcp/http_server.py ────────────────────────────────

class _Req:
    def __init__(self, ip):
        from types import SimpleNamespace
        headers = {}
        if ip:
            headers["x-forwarded-for"] = ip
        self.headers = _H(headers)
        self.client = SimpleNamespace(host=ip or "9.9.9.9")


class _H:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, k, default=None):
        return self._d.get(k.lower(), default)


def test_seam_saas_off_and_denied(saas_off):
    from src.mcp import http_server as h
    assert h._saas_ip_denied("account:acct-on", _Req("203.0.113.7")) is False


def test_seam_denies_via_module(saas_on, monkeypatch):
    from src.mcp import http_server as h
    import saas.agent_mode as am
    import saas.ip_acl as ipm
    monkeypatch.setattr(am, "account_id_for_principal", lambda p, x: "acct-on")
    monkeypatch.setattr(ipm, "ip_denied", lambda acct, ip: ip == "203.0.113.7")
    assert h._saas_ip_denied("account:acct-on", _Req("203.0.113.7")) is True
    assert h._saas_ip_denied("account:acct-on", _Req("192.0.2.1")) is False


def test_seam_fails_open_on_error(saas_on, monkeypatch):
    from src.mcp import http_server as h
    import saas.agent_mode as am
    def boom(p, x):
        raise RuntimeError("no db")
    monkeypatch.setattr(am, "account_id_for_principal", boom)
    assert h._saas_ip_denied("account:acct-on", _Req("203.0.113.7")) is False


# ── 6. rendered console half (real index.html + app.js in jsdom) ─────────────────────
#
# Same three-way split convention as tests/test_operator_console_ux.py: node+jsdom missing
# → SKIP with the remedy (npm install --include=dev --prefix tests/dashboard — NOTE the
# --include=dev: NODE_ENV=production in this container makes a bare `npm install` omit the
# devDependency jsdom entirely and report "up to date" while node_modules stays EMPTY);
# harness genuinely broken → FAIL loudly; else assert the observations.

import json
import shutil
import subprocess


def test_console_ip_rules_panel_dom_drive():
    harness = __file__.rsplit("/", 1)[0] + "/dashboard/render_operator_console.js"
    if not shutil.which("node"):
        pytest.skip("node missing — run: npm install --include=dev --prefix tests/dashboard")
    try:
        out = subprocess.run(["node", harness], capture_output=True, text=True,
                             timeout=120, cwd=__file__.rsplit("/", 1)[0] + "/../..")
    except subprocess.TimeoutExpired:
        pytest.fail("render harness timed out")
    if out.returncode != 0:
        pytest.fail(f"render harness failed: {out.stderr[-500:]}")
    try:
        report = json.loads(out.stdout)
    except json.JSONDecodeError:
        pytest.skip("jsdom missing — run: npm install --include=dev --prefix tests/dashboard")
    ip = report.get("iprules") or {}
    assert ip.get("body_exists"), "Access control panel has no ad-iprules-body table"
    assert ip.get("row_count") == 2
    assert ip.get("tenant_row_shows_cidr_and_badges")
    assert ip.get("global_row_shows_cidr")
    assert ip.get("add_posts_tenant_route"), "add must POST exactly once to the tenant ip-rules route"
    assert ip.get("remove_button_found")
    assert ip.get("remove_deletes_by_id"), "remove must DELETE /admin/accounts/<id>/ip-rules/<rule_id>"
    assert ip.get("check_names_winner"), "the check dry-run must name the winning rule"
    assert ip.get("sec_body_exists"), "Security view has no sec-iprules-body table"
    assert ip.get("sec_row_renders")
    assert ip.get("sec_remove_targets_global_route"), "global remove must DELETE /admin/ip-rules/<id>"
