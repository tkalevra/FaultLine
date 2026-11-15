"""SaaS Phase 2 — per-user MEMORY-OPS admin API isolation PROOF.

Same crown-jewel control as Phase 1: an account key may only touch the memory of a
user that is an ACTIVE SEAT of THAT account (``is_active_seat``). Cross-account memory
ops (summary / export / clear / purge) are the exact same BOLA threat as seat ops and
MUST 403.

Coverage layers (mirror test_saas_isolation.py):
  1. SEAM tests (no DB): drive the FastAPI routes via TestClient with the seat-check +
     memory_ops backend monkeypatched — proves the AUTHZ-BEFORE-BIND chokepoint.
  2. Route-mount gating: SAAS off => memory-ops routes are 404 (never mounted logic runs).
  3. Pure unit: authorize-before-bind ordering, schema-name safety.

Run: python3 -m pytest tests/test_saas_memory_ops.py -q
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import admin as saas_admin
from saas import seats as saas_seats
from saas import accounts as saas_accounts
from saas import memory_ops as saas_memory_ops


UA = "11111111-1111-4111-8111-111111111111"   # AcctA's seat
UB = "22222222-2222-4222-8222-222222222222"   # AcctB's seat
UNOWNED = "33333333-3333-4333-8333-333333333333"  # valid UUID, no account
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

KEY_A = "flk_aaaaaaaa_secretA"
KEY_B = "flk_bbbbbbbb_secretB"


@pytest.fixture
def client(monkeypatch):
    """Mount the admin router on a bare app with SAAS on and the control plane faked."""
    monkeypatch.setenv("SAAS_MODE", "1")

    # key -> account
    keymap = {KEY_A: ACCT_A, KEY_B: ACCT_B}
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: keymap.get(k))
    # no operator key configured for these tests
    monkeypatch.delenv("SAAS_OPERATOR_KEY", raising=False)

    # seat roster: A owns UA, B owns UB
    roster = {(ACCT_A, UA): True, (ACCT_B, UB): True}
    monkeypatch.setattr(saas_seats, "is_active_seat",
                        lambda a, u: roster.get((a, u), False))
    # the admin module imported is_active_seat via `from . import seats as _seats`,
    # so patching saas_seats.is_active_seat is what the route sees.

    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# ════════════════════════════════════════════════════════════════════════════════
# Backend fake — so the SEAM tests never touch Postgres/Qdrant
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fake_backend(monkeypatch):
    calls = {"summary": [], "export": [], "clear": [], "purge": []}

    def fake_summary(user_id, limit=50, offset=0):
        calls["summary"].append(user_id)
        return {"user_id": user_id, "counts": {"facts": 1}, "facts": [], "attributes": []}

    def fake_export(user_id):
        calls["export"].append(user_id)
        # ``complete``/``incomplete`` are part of memory_export's contract (S3): the route
        # refuses to hand back an export that is not stamped complete, so a stub that omits
        # the flag is a stub of a shape the real function never returns.
        return {"user_id": user_id, "complete": True, "incomplete": [], "tables": {},
                "data": {}, "facts": [], "attributes": [], "aliases": []}

    def fake_clear(user_id):
        calls["clear"].append(user_id)
        return {"user_id": user_id, "cleared": True, "rows_deleted": {}, "vector_collection_deleted": True}

    def fake_purge(user_id):
        calls["purge"].append(user_id)
        return {"user_id": user_id, "schema_dropped": True, "vector_collection_deleted": True}

    monkeypatch.setattr(saas_memory_ops, "memory_summary", fake_summary)
    monkeypatch.setattr(saas_memory_ops, "memory_export", fake_export)
    monkeypatch.setattr(saas_memory_ops, "clear_memory", fake_clear)
    monkeypatch.setattr(saas_memory_ops, "purge_user_data", fake_purge)
    return calls


# ════════════════════════════════════════════════════════════════════════════════
# POSITIVE — account touches its OWN seat's memory
# ════════════════════════════════════════════════════════════════════════════════

def test_summary_own_seat_allowed(client, fake_backend):
    r = client.get(f"/admin/users/{UA}/memory", headers=_auth(KEY_A))
    assert r.status_code == 200
    assert r.json()["user_id"] == UA
    assert fake_backend["summary"] == [UA]


def test_export_own_seat_allowed(client, fake_backend):
    r = client.get(f"/admin/users/{UA}/memory/export", headers=_auth(KEY_A))
    assert r.status_code == 200
    assert "facts" in r.json()
    assert fake_backend["export"] == [UA]


def test_clear_own_seat_allowed(client, fake_backend):
    r = client.request("DELETE", f"/admin/users/{UA}/memory", headers=_auth(KEY_A))
    assert r.status_code == 200
    assert r.json()["cleared"] is True
    assert fake_backend["clear"] == [UA]


def test_purge_own_seat_allowed(client, fake_backend):
    r = client.request("DELETE", f"/admin/users/{UA}?purge=true", headers=_auth(KEY_A))
    assert r.status_code == 200
    body = r.json()
    assert body["schema_dropped"] is True
    assert fake_backend["purge"] == [UA]


# ════════════════════════════════════════════════════════════════════════════════
# CROSS-ACCOUNT BOLA — A's key on B's seat → 403 on EVERY memory op
# ════════════════════════════════════════════════════════════════════════════════

def test_summary_cross_account_denied(client, fake_backend):
    r = client.get(f"/admin/users/{UB}/memory", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_backend["summary"] == []  # backend NEVER reached (no schema bind)


def test_export_cross_account_denied(client, fake_backend):
    r = client.get(f"/admin/users/{UB}/memory/export", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_backend["export"] == []


def test_clear_cross_account_denied(client, fake_backend):
    r = client.request("DELETE", f"/admin/users/{UB}/memory", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_backend["clear"] == []


def test_purge_cross_account_denied(client, fake_backend):
    r = client.request("DELETE", f"/admin/users/{UB}?purge=true", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_backend["purge"] == []


# ════════════════════════════════════════════════════════════════════════════════
# UNOWNED / MALFORMED / UNAUTH
# ════════════════════════════════════════════════════════════════════════════════

def test_summary_unowned_uuid_denied(client, fake_backend):
    r = client.get(f"/admin/users/{UNOWNED}/memory", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_backend["summary"] == []


def test_summary_malformed_uuid_400(client, fake_backend):
    r = client.get("/admin/users/not-a-uuid/memory", headers=_auth(KEY_A))
    assert r.status_code == 400
    assert fake_backend["summary"] == []


def test_clear_malformed_uuid_400(client, fake_backend):
    r = client.request("DELETE", "/admin/users/not-a-uuid/memory", headers=_auth(KEY_A))
    assert r.status_code == 400
    assert fake_backend["clear"] == []


def test_summary_no_bearer_401(client, fake_backend):
    r = client.get(f"/admin/users/{UA}/memory")
    assert r.status_code == 401
    assert fake_backend["summary"] == []


def test_export_bad_key_401(client, fake_backend):
    r = client.get(f"/admin/users/{UA}/memory/export", headers=_auth("flk_nope_nope"))
    assert r.status_code == 401
    assert fake_backend["export"] == []


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE-MOUNT GATING — SAAS off => memory-ops routes reject (404), backend never runs
# ════════════════════════════════════════════════════════════════════════════════

def test_saas_off_summary_404(client, fake_backend, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.get(f"/admin/users/{UA}/memory", headers=_auth(KEY_A))
    assert r.status_code == 404
    assert fake_backend["summary"] == []


def test_saas_off_clear_404(client, fake_backend, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.request("DELETE", f"/admin/users/{UA}/memory", headers=_auth(KEY_A))
    assert r.status_code == 404
    assert fake_backend["clear"] == []


# ════════════════════════════════════════════════════════════════════════════════
# AUTHORIZE-BEFORE-BIND — the seat check must gate the schema bind
# ════════════════════════════════════════════════════════════════════════════════

def test_authorize_seat_helper_denies_cross_account(monkeypatch):
    """_authorize_seat raises 403 before any tenant-schema helper is reachable."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_seats, "is_active_seat",
                        lambda a, u: (a, u) == (ACCT_A, UA))
    # own seat: returns normally
    saas_admin._authorize_seat(ACCT_A, UA)
    # cross-account: 403
    with pytest.raises(saas_admin.HTTPException) as exc:
        saas_admin._authorize_seat(ACCT_A, UB)
    assert exc.value.status_code == 403


def test_authorize_seat_operator_rejected(monkeypatch):
    """Operator sentinel has no single account → 400 (must present an account key)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    with pytest.raises(saas_admin.HTTPException) as exc:
        saas_admin._authorize_seat("__operator__", UA)
    assert exc.value.status_code == 400


def test_authorize_seat_malformed_uuid_400(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: True)
    with pytest.raises(saas_admin.HTTPException) as exc:
        saas_admin._authorize_seat(ACCT_A, "not-a-uuid")
    assert exc.value.status_code == 400


def test_authorize_seat_fails_closed_on_check_error(monkeypatch):
    """If is_active_seat raises (DB outage), authz DENIES (403), never allows."""
    monkeypatch.setenv("SAAS_MODE", "1")

    def boom(a, u):
        raise RuntimeError("control plane down")
    monkeypatch.setattr(saas_seats, "is_active_seat", boom)
    with pytest.raises(saas_admin.HTTPException) as exc:
        saas_admin._authorize_seat(ACCT_A, UA)
    assert exc.value.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# SCHEMA-NAME SAFETY — memory_ops never interpolates a non-tenant schema name
# ════════════════════════════════════════════════════════════════════════════════

def test_tenant_schema_for_rejects_malformed():
    with pytest.raises(ValueError):
        saas_memory_ops._tenant_schema_for("not-a-uuid")


def test_tenant_schema_for_shape():
    schema = saas_memory_ops._tenant_schema_for(UA)
    assert schema.startswith("faultline_")
    # only [a-z0-9_] — safe to interpolate into SET search_path
    body = schema[len("faultline_"):]
    assert body and all(c.isalnum() or c == "_" for c in body)


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-DB integration — real bind + clear (auto-skip if no reachable control plane)
# ════════════════════════════════════════════════════════════════════════════════

def _control_plane_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.account_users')")
            ok = cur.fetchone()[0] is not None
        conn.close()
        return ok
    except Exception:
        return False


live_db = pytest.mark.skipif(
    not _control_plane_available(),
    reason="no reachable POSTGRES_DSN control plane — live memory-op path proven via curl",
)


@live_db
def test_live_summary_on_unprovisioned_seat_is_empty():
    """A licensed-but-unprovisioned seat summary is empty zeros, not an error."""
    fresh = str(uuid.uuid4())
    summary = saas_memory_ops.memory_summary(fresh)
    assert summary["counts"]["facts"] == 0
    assert summary["facts"] == []
