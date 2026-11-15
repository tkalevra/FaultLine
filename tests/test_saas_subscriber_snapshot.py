"""SaaS operator SUBSCRIBER SNAPSHOT — GET /admin/subscribers.

The operator console needs one page that answers "who signed up, when, and what state are
they in" without a hand-rolled psql session. This suite pins the contract that makes that
page safe to ship:

  * OPERATOR (key or session) can read it; a plain ACCOUNT key is NOT an operator → 403;
    no credential → 401; SaaS off → 404 (router guarded). The principal is derived from the
    credential — the route takes NO account/tenant parameter, so there is nothing to BOLA.
  * NO MONEY anywhere in the payload. Money lives on /admin/billing/overview and behind the
    tenant console's can_view_billing gate (migration 159); this route must not widen that
    permission model by leaking a price/MRR/ledger amount through an ungated door.
  * NO tenant-schema read: the reader only ever issues public.* SQL (a tenant search_path
    bind would put a subscriber's MEMORY one step from an operator page).
  * Fail-SAFE: a control-plane error returns a shaped payload with `error` set — 200, never
    a 500 and never a partial/wrong roster.
  * Summary counters are computed over EVERY row even when the detail tables are truncated
    by `limit` (a headline number that silently counts only the first page is a lie).
  * window_days / limit are clamped, never interpolated into SQL.

Run: python3 tools/fltest.py --bug ADMINSNAP --test tests/test_saas_subscriber_snapshot.py
(tests/ is gitignored → git add -f)
"""

import contextlib
import datetime as _dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import admin as saas_admin
from saas import subscribers as saas_subs


OP_KEY = "op-super-secret-key"

_NOW = _dt.datetime.now(_dt.timezone.utc)
_D = lambda days: _NOW - _dt.timedelta(days=days)  # noqa: E731


# ── a scripted fake control-plane cursor ────────────────────────────────────────────
class _FakeCursor:
    """Returns queued result sets in execute() order; records every SQL string."""

    def __init__(self, results, sql_log):
        self._results = list(results)
        self._sql = sql_log
        self._current = None

    def execute(self, sql, params=None):
        self._sql.append(" ".join(str(sql).split()))
        self._current = self._results.pop(0) if self._results else []

    def fetchall(self):
        return list(self._current or [])

    def fetchone(self):
        cur = self._current
        if isinstance(cur, list):
            return cur[0] if cur else None
        return cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, results, sql_log):
        self._results, self._sql = results, sql_log

    def cursor(self):
        return _FakeCursor(self._results, self._sql)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _script():
    """The nine result sets subscriber_snapshot() consumes, in execute() order."""
    return [
        # 1. accounts rollup: status, kind, n, new7, new30, new90
        [("active", "tenant", 25, 1, 4, 20), ("archived", "tenant", 17, 0, 0, 17),
         ("active", "team", 9, 0, 2, 9), ("active", "individual_house", 1, 0, 0, 1)],
        # 2. seat mix: account_id, seat_type, active, all, new7, new30
        [("A1", "standard", 3, 4, 1, 2), ("A1", "agent", 1, 1, 0, 0),
         ("A2", "standard", 2, 2, 0, 1)],
        # 3. entitlements: account_id, seat_type, quantity, base_quantity
        [("A1", "standard", 5, 5), ("A1", "agent", 2, 0)],
        # 4. last activity: account_id, ts
        [("A1", _D(2)), ("A2", _D(90))],
        # 5. accounts timeline
        [(_NOW.date() - _dt.timedelta(days=1), 2)],
        # 6. individuals timeline
        [(_NOW.date(), 1)],
        # 7. individuals rollup: total, active, trialing, trial_ended
        (5, 5, 2, 3),
        # 8. individuals detail
        [("u-1", "acct-singles", "sam@example.com", "Sam", "individual", "active",
          "none", _D(20), _D(1), _NOW + _dt.timedelta(days=5), None)],
        # 9. accounts detail
        [("A1", "Acme", "acme", "standard", "active", "agent", "tenant", 5, _D(30), 6,
          _NOW + _dt.timedelta(days=150), True),
         ("A2", "Nimbus", "nimbus", "standard", "suspended", "standard", "team", 3, _D(60),
          None, None, False)],
    ]


@pytest.fixture
def fake_db(monkeypatch):
    sql_log: list[str] = []

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(_script(), sql_log)

    monkeypatch.setattr(saas_subs.db, "connection", _conn)
    return sql_log


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "subscriber-snapshot-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


# ── 1. AUTH: operator only, principal from the credential ───────────────────────────

def test_operator_reads_the_snapshot(client, fake_db):
    r = client.get("/admin/subscribers", headers=_op())
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["accounts_total"] == 52
    assert body["error"] is None


def test_account_key_is_not_an_operator(client, monkeypatch):
    called = {"n": 0}

    def _poison(**kw):
        called["n"] += 1
        return {}

    monkeypatch.setattr(saas_admin._subscribers, "subscriber_snapshot", _poison)
    r = client.get("/admin/subscribers", headers={"Authorization": "Bearer flk_some_tenant"})
    assert r.status_code == 403
    assert called["n"] == 0            # the reader is never reached


def test_no_credential_is_denied(client, monkeypatch):
    monkeypatch.setattr(saas_admin._subscribers, "subscriber_snapshot",
                        lambda **kw: pytest.fail("reader must not run unauthenticated"))
    assert client.get("/admin/subscribers").status_code == 403


def test_saas_off_hides_the_route(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    app = FastAPI()
    app.include_router(saas_admin.router)
    c = TestClient(app)
    r = c.get("/admin/subscribers", headers=_op())
    assert r.status_code == 404        # _require_saas() → not found


def test_route_takes_no_account_parameter(client, fake_db):
    """There is no tenant/account selector on this route — nothing to BOLA. A stray
    account_id in the query string is ignored, not honoured."""
    r = client.get("/admin/subscribers?account_id=someone-elses", headers=_op())
    assert r.status_code == 200
    assert "account_id" not in r.json()


# ── 2. NO MONEY (the billing-separation firewall) ────────────────────────────────────

# Matched per underscore-separated SEGMENT, not as a substring: "generated_at" must not trip
# on "rate", and "created_at" must not trip on "rate" either.
_MONEY_TOKENS = frozenset({"price", "prices", "amount", "cost", "costs", "monthly", "mrr",
                           "currency", "cad", "usd", "invoice", "ledger", "bill", "billing",
                           "charge", "rate", "rates", "total_cad", "spend", "revenue"})


def _walk_keys(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k).lower())
            _walk_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, out)


def test_payload_carries_no_monetary_field(client, fake_db):
    r = client.get("/admin/subscribers", headers=_op())
    keys: list[str] = []
    _walk_keys(r.json(), keys)
    offenders = [k for k in keys if _MONEY_TOKENS & set(k.split("_"))]
    assert offenders == [], (
        f"monetary field(s) {offenders} leaked through an ungated operator route — money "
        "belongs on /admin/billing/overview / behind can_view_billing (migration 159)")


# ── 3. NO tenant-schema read (a subscriber's memory is never one step away) ──────────

def test_reader_only_touches_the_public_control_plane(fake_db):
    saas_subs.subscriber_snapshot()
    joined = " ".join(fake_db).lower()
    assert "search_path" not in joined            # never binds a tenant schema
    assert "faultline_" not in joined             # never names a tenant schema
    assert "from facts" not in joined and "staged_facts" not in joined
    assert "entity_" not in joined
    # every table reference is schema-qualified to public.* (no bare, search_path-relative name)
    import re
    for sql in fake_db:
        for tbl in re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", sql, re.I):
            assert tbl.startswith("public.") or tbl == "(", f"unqualified table {tbl!r} in: {sql}"


def test_reader_does_not_scan_the_event_log(fake_db):
    """saas_event_log is ~500k rows on pre-prod — the activity signal must come from the
    small credential tables instead, or this admin page degrades as the platform grows."""
    saas_subs.subscriber_snapshot()
    assert "saas_event_log" not in " ".join(fake_db)


# ── 4. SHAPE: counters are full-population; detail rows are bounded ──────────────────

def test_summary_counts_every_row_even_when_detail_is_truncated(fake_db):
    snap = saas_subs.subscriber_snapshot(limit=2)
    # 25+17+9+1 accounts exist; only 2 detail rows are returned.
    assert snap["summary"]["accounts_total"] == 52
    assert snap["accounts_shown"] == 2
    assert snap["accounts_truncated"] is True
    assert snap["summary"]["by_status"] == {"active": 35, "archived": 17}
    assert snap["summary"]["by_kind"] == {"tenant": 42, "team": 9, "individual_house": 1}
    assert snap["summary"]["new_7d"] == 1 and snap["summary"]["new_30d"] == 6


def test_seat_rollup_and_entitlements(fake_db):
    snap = saas_subs.subscriber_snapshot()
    s = snap["summary"]
    assert s["seats_active"] == 6 and s["seats_total"] == 7
    assert s["seats_by_type"] == {"standard": 5, "agent": 1}
    a1 = next(a for a in snap["accounts"] if a["account_id"] == "A1")
    assert a1["seats_active"] == 4 and a1["seats_by_type"] == {"standard": 3, "agent": 1}
    assert a1["entitlements"]["standard"] == {"quantity": 5, "base_quantity": 5}
    assert a1["account_kind_label"] == "Enterprise"


def test_signup_dates_and_activity_are_surfaced(fake_db):
    snap = saas_subs.subscriber_snapshot()
    a1 = next(a for a in snap["accounts"] if a["account_id"] == "A1")
    assert a1["created_at"] and a1["age_days"] == 30
    assert a1["last_activity_at"] and a1["idle_days"] == 2
    a2 = next(a for a in snap["accounts"] if a["account_id"] == "A2")
    assert a2["idle_days"] == 90                 # gone quiet — visible at a glance
    assert a2["expires_at"] is None and a2["term_months"] is None   # not fabricated


def test_individual_trial_state_is_derived_not_invented(fake_db):
    snap = saas_subs.subscriber_snapshot()
    u = snap["individuals"][0]
    assert u["email"] == "sam@example.com"
    assert u["trialing"] is True                 # trial_ends_at in the future
    assert u["tenant_context"] == "none"
    assert snap["summary"]["individuals_trialing"] == 2
    assert snap["summary"]["individuals_trial_ended"] == 3


def test_timeline_is_densified_over_the_window(fake_db):
    snap = saas_subs.subscriber_snapshot(window_days=14)
    assert snap["window_days"] == 14
    assert len(snap["timeline"]) == 14
    dates = [d["date"] for d in snap["timeline"]]
    assert dates == sorted(dates)                # ascending, gap-free
    assert sum(d["accounts"] for d in snap["timeline"]) == 2
    assert sum(d["individuals"] for d in snap["timeline"]) == 1


# ── 5. INPUT clamping (never interpolated into SQL) ──────────────────────────────────

@pytest.mark.parametrize("given,expected", [(0, 7), (3, 7), (30, 30), (9999, 180)])
def test_window_days_is_clamped(fake_db, given, expected):
    assert saas_subs.subscriber_snapshot(window_days=given)["window_days"] == expected


def test_limit_is_clamped_and_route_rejects_out_of_range(client, fake_db):
    assert client.get("/admin/subscribers?limit=0", headers=_op()).status_code == 422
    assert client.get("/admin/subscribers?limit=99999", headers=_op()).status_code == 422
    assert client.get("/admin/subscribers?window_days=1", headers=_op()).status_code == 422


def test_params_are_bound_not_interpolated(fake_db):
    saas_subs.subscriber_snapshot(window_days=30, limit=10)
    for sql in fake_db:
        assert "make_interval(days => 30)" not in sql   # bound as %s, never formatted in
        assert "LIMIT 10" not in sql


# ── 6. FAIL-SAFE ─────────────────────────────────────────────────────────────────────

def test_control_plane_error_returns_a_shaped_payload(client, monkeypatch):
    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("POSTGRES_DSN not set — cannot reach the control plane")
        yield  # pragma: no cover

    monkeypatch.setattr(saas_subs.db, "connection", _boom)
    r = client.get("/admin/subscribers", headers=_op())
    assert r.status_code == 200                  # never a 500 on an admin page
    body = r.json()
    # 2026-09-14 (gauntlet codeql-info-exposure, IE2): this used to assert the EXCEPTION TEXT
    # ("control plane" from the RuntimeError above) reached the body — it was pinning the leak.
    # The body now carries the authored phrase + a correlation id; the exception text goes to
    # the CRIT log under that id (src/api/errors.py, OWASP ASVS V7.4.1).
    assert body["error"] == "control-plane unavailable"
    assert "POSTGRES_DSN" not in body["error"]
    assert body["correlation_id"]
    assert body["accounts"] == [] and body["individuals"] == []
    assert body["summary"]["accounts_total"] == 0


def test_route_is_mounted_on_the_admin_router():
    paths = {r.path for r in saas_admin.router.routes}
    assert "/admin/subscribers" in paths
