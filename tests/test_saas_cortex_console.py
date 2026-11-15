"""Per-seat AGENT-CORTEX console endpoints — BOLA + gating-order proofs (no DB).

The cortex read/prune surfaces (``saas.cortex_ops`` behind ``saas.tenant_console`` and
``saas.admin``) are the AGENT's OWN operational memory (firewalled ``flagent_<uuid>``
schema), DISTINCT from the user's grounded graph. These prove the SOC discipline the
task requires — authorize → verify-seat-ownership (fail-closed) → read/act — WITHOUT ever
binding a real schema (the data-plane fn is monkeypatched).

Proofs:
  1. no session / no key   → 401 (fail-closed).
  2. cross-account (BOLA)  → session/key A cannot read account B's seat cortex → 403,
                             and the data-plane read is NEVER reached for a foreign seat.
  3. happy path            → the read is made for the SESSION/KEY's own seat, and the
                             access is SOC-audited (audit.record fired).
  4. the cortex schema derivation is FIREWALLED (flagent_, never faultline_/public).

Control-plane + audit are monkeypatched, so this file is green without a DB.
Run: python3 -m pytest tests/test_saas_cortex_console.py -q  (tests/ is gitignored → git add -f).
"""

import pytest

from saas import dashboard_auth as auth
from saas import tenant_console as tc
from saas import cortex_ops as cortex_ops


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-4111-8111-111111111111"   # a seat of ACCT_A
USER_B = "22222222-2222-4222-8222-222222222222"   # a seat of ACCT_B (NOT of A)


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session_a():
    return auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")


def _csrf_a():
    return auth.issue_csrf(dashboard_user_id=USER_A)


@pytest.fixture
def seat_ownership(monkeypatch):
    def _is_active(account_id, user_id):
        return (account_id, user_id) in {(ACCT_A, USER_A), (ACCT_B, USER_B)}
    monkeypatch.setattr(tc._seats, "is_active_seat", _is_active)


@pytest.fixture
def stub_cortex(monkeypatch):
    """Monkeypatch the data-plane read/clear so no schema is ever bound. Records calls."""
    calls = {"stats": [], "clear": []}

    def _stats(user_id, recent_limit=20):
        calls["stats"].append((user_id, recent_limit))
        return {"user_id": user_id, "provisioned": True, "total": 2, "hardened": 1,
                "by_category": {"gotcha": 2}, "by_lifecycle": {"observed": 1, "hardened": 1, "decayed": 0},
                "notes": [], "recent_limit": recent_limit}

    def _clear(user_id):
        calls["clear"].append(user_id)
        return {"user_id": user_id, "cleared": True, "schema_present": True, "rows_deleted": 2}

    monkeypatch.setattr(tc._cortex_ops, "cortex_stats", _stats)
    monkeypatch.setattr(tc._cortex_ops, "clear_cortex", _clear)
    return calls


@pytest.fixture
def spy_audit(monkeypatch):
    events = []
    monkeypatch.setattr(tc._audit, "record", lambda action, **kw: events.append((action, kw)))
    return events


# 1. no session → 401 (fail-closed)
@pytest.mark.parametrize("method,path", [
    ("get", f"/tenant/seats/{USER_A}/cortex"),
    ("delete", f"/tenant/seats/{USER_A}/cortex"),
])
def test_no_session_is_401(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# 2. cross-account BOLA → 403, and the data plane is NEVER reached
def test_cross_account_read_is_403_and_never_reads(client, seat_ownership, stub_cortex):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get(f"/tenant/seats/{USER_B}/cortex")   # B is NOT a seat of A
    assert r.status_code == 403
    assert stub_cortex["stats"] == []                  # authorize-BEFORE-read held


# 3. happy path — reads OWN seat + SOC-audits the access
def test_own_seat_read_ok_and_audited(client, seat_ownership, stub_cortex, spy_audit):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.get(f"/tenant/seats/{USER_A}/cortex?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["hardened"] == 1
    assert stub_cortex["stats"] == [(USER_A, 5)]       # read made for the session's own seat
    assert any(a == "seat.cortex_read" for a, _ in spy_audit)


def test_own_seat_prune_ok_and_audited(client, seat_ownership, stub_cortex, spy_audit):
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete(f"/tenant/seats/{USER_A}/cortex", headers={"x-csrf-token": _csrf_a()})
    assert r.status_code == 200
    assert r.json()["rows_deleted"] == 2
    assert stub_cortex["clear"] == [USER_A]
    assert any(a == "seat.cortex_clear" for a, _ in spy_audit)


def test_prune_requires_csrf(client, seat_ownership, stub_cortex):
    # Unsafe method without the session-bound CSRF token is rejected BEFORE any data touch.
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session_a())
    r = client.delete(f"/tenant/seats/{USER_A}/cortex")   # no x-csrf-token
    assert r.status_code == 403
    assert stub_cortex["clear"] == []


# 4. firewall: derived cortex schema is flagent_, never faultline_/public; bad id refused
def test_cortex_schema_is_firewalled():
    s = cortex_ops._cortex_schema_for(USER_A)
    assert s.startswith("flagent_") and not s.startswith("faultline_")
    for bad in ["", "not-a-uuid", "public", "../etc/passwd"]:
        with pytest.raises(ValueError):
            cortex_ops._cortex_schema_for(bad)
    for unsafe in ["public", "faultline_abc", "flagent_bad; DROP"]:
        with pytest.raises(ValueError):
            with cortex_ops._cortex_conn(unsafe):
                pass
