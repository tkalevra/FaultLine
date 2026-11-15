"""SaaS multi-tenancy isolation PROOF — the negative-test matrix (Phase 1).

This is the spine of the security deliverable: isolation must be PROVABLE, not asserted.
The matrix maps to DEV/SECURITY-saas-multitenancy.md §7.1 (T1–T13).

Two layers of coverage:
  1. SEAM tests (always green, no DB): drive bind_tenant() and _resolve_principal()
     directly with the account seat-check monkeypatched — this proves the AUTHORIZATION
     LOGIC at the single chokepoint (the crown-jewel control C1).
  2. Pure unit tests (always green): key hashing/format, config fail-closed guards,
     register-seat cap logic surfaces.
  3. LIVE-DB integration (auto-skips if POSTGRES_DSN is unreachable / tables absent):
     creates two real accounts + keys + seats and proves cross-account 403 end to end.

Run: python3 -m pytest tests/test_saas_isolation.py -q   (tests/ is gitignored → git add -f)
"""

import os
import uuid

import pytest

# ── import the seams under test ────────────────────────────────────────────────
from src.mcp import server as mcp_server
from src.mcp import http_server as http_server

from saas import config as saas_config
from saas import keys as saas_keys
from saas import seats as saas_seats
from saas import accounts as saas_accounts


UA = "11111111-1111-4111-8111-111111111111"   # AcctA's seat
UB = "22222222-2222-4222-8222-222222222222"   # AcctB's seat
UNOWNED = "33333333-3333-4333-8333-333333333333"  # valid UUID, no account
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


# ════════════════════════════════════════════════════════════════════════════════
# SEAM: bind_tenant() account seat-check (C1 — cross-account BOLA)
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def seat_roster(monkeypatch):
    """Simulate the seat roster: AcctA owns UA, AcctB owns UB. No DB."""
    roster = {(ACCT_A, UA): True, (ACCT_B, UB): True}

    def fake_is_active_seat(account_id, user_id):
        return roster.get((account_id, user_id), False)

    monkeypatch.setattr(saas_seats, "is_active_seat", fake_is_active_seat)
    return roster


def test_T1_positive_own_seat_allowed(seat_roster):
    """A's key + A's user → allowed, resolves to that user's tenant."""
    resolved = mcp_server.bind_tenant(f"account:{ACCT_A}", UA)
    assert resolved == UA


def test_T2_cross_account_bola_denied(seat_roster):
    """A's key + B's user → 403 (the crown jewel: cross-account BOLA blocked)."""
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UB)
    assert exc.value.status_code == 403


def test_T3_valid_but_unowned_uuid_denied(seat_roster):
    """A well-formed UUID that is not a seat → 403 (validity != ownership)."""
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UNOWNED)
    assert exc.value.status_code == 403


def test_T4_malformed_uuid_400(seat_roster):
    """Malformed claimed id → 400 (UUID gate before any schema interpolation)."""
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", "not-a-uuid")
    assert exc.value.status_code == 400


def test_empty_claim_400(seat_roster):
    """No claimed user id in account mode → 400 (never a silent default tenant)."""
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", "")
    assert exc.value.status_code == 400


def test_revoked_seat_denied(monkeypatch):
    """Revoked seat → is_active_seat False → 403."""
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: False)
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UA)
    assert exc.value.status_code == 403


def test_seat_check_failure_fails_closed(monkeypatch):
    """If the seat check raises (DB outage), bind_tenant DENIES (fail-closed), not allow."""
    def boom(account_id, user_id):
        raise RuntimeError("control plane unreachable")
    monkeypatch.setattr(saas_seats, "is_active_seat", boom)
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{ACCT_A}", UA)
    assert exc.value.status_code == 403


# ════════════════════════════════════════════════════════════════════════════════
# SEAM: _resolve_principal() — key → account principal (revocation / 401)
# ════════════════════════════════════════════════════════════════════════════════

def test_resolve_principal_saas_maps_key_to_account(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    principal = http_server._resolve_principal("KA-plaintext")
    assert principal == f"account:{ACCT_A}"


def test_resolve_principal_saas_revoked_key_is_none(monkeypatch):
    """T6: a revoked/unknown key resolves to None → require_auth returns 401."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: None)
    assert http_server._resolve_principal("revoked-key") is None


def test_resolve_principal_saas_no_credentials_is_none(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    assert http_server._resolve_principal(None) is None


def test_resolve_principal_saas_off_is_unchanged(monkeypatch):
    """SAAS_MODE off => today's behavior EXACTLY: valid shared key → 'shared'."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(http_server, "MCP_API_KEY", "sharedsecret")
    assert http_server._resolve_principal("sharedsecret") == "shared"
    assert http_server._resolve_principal("wrong") is None


def test_resolve_principal_saas_resolver_error_fails_closed(monkeypatch):
    """A resolver exception in saas mode denies (None → 401), never routes to 'shared'."""
    monkeypatch.setenv("SAAS_MODE", "1")

    def boom(_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", boom)
    assert http_server._resolve_principal("anything") is None


# ════════════════════════════════════════════════════════════════════════════════
# Fail-closed startup guards (C3 / C5)
# ════════════════════════════════════════════════════════════════════════════════

def test_T9_saas_on_no_mcp_key_refuses(monkeypatch):
    """SAAS_MODE on + MCP_API_KEY unset → refuse to start (no OPEN server)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    with pytest.raises(saas_config.SaasConfigError):
        saas_config.enforce_startup_guards(mcp_api_key="", faultline_user_id="")


def test_T10_saas_on_faultline_user_id_set_refuses(monkeypatch):
    """SAAS_MODE on + FAULTLINE_USER_ID set → refuse to start (foot-gun)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    with pytest.raises(saas_config.SaasConfigError):
        saas_config.enforce_startup_guards(mcp_api_key="k", faultline_user_id=UA)


def test_saas_on_clean_posture_starts(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    # No raise = OK.
    saas_config.enforce_startup_guards(mcp_api_key="k", faultline_user_id="")


def test_saas_off_guards_are_noop(monkeypatch):
    """SAAS_MODE off => guards never fire even with an unsafe posture (unchanged)."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    saas_config.enforce_startup_guards(mcp_api_key="", faultline_user_id=UA)  # no raise


# ════════════════════════════════════════════════════════════════════════════════
# Pure unit — key hashing / format (C9)
# ════════════════════════════════════════════════════════════════════════════════

def test_key_generation_format_and_hash():
    full, prefix, key_hash = saas_keys.generate_key()
    assert full.startswith("flk_")
    assert prefix.startswith("flk_")
    assert full.startswith(prefix)          # prefix is a real prefix of the key
    assert prefix != full                   # secret half not in the prefix
    assert len(key_hash) == 64              # sha256 hex
    assert saas_keys.hash_key(full) == key_hash          # deterministic
    assert saas_keys.hash_key("other") != key_hash


def test_key_prefix_never_leaks_secret():
    full, prefix, _ = saas_keys.generate_key()
    assert saas_keys.prefix_of(full) == prefix
    # the redacted prefix must not contain the secret half
    secret_half = full[len(prefix) + 1:]
    assert secret_half not in saas_keys.prefix_of(full)


def test_keys_equal_timing_safe():
    assert saas_keys.keys_equal("abc", "abc")
    assert not saas_keys.keys_equal("abc", "abd")


# ════════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL-PRESERVATION: SAAS_MODE off, bind_tenant is unchanged Option-B behavior
# ════════════════════════════════════════════════════════════════════════════════

def test_saas_off_bind_tenant_shared_path_unchanged(monkeypatch):
    """SAAS_MODE off: a 'shared' principal + claimed UUID behaves exactly as today."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(mcp_server, "FAULTLINE_USER_ID", "")
    resolved = mcp_server.bind_tenant("shared", UA)
    assert resolved == UA


def test_saas_off_bind_tenant_malformed_still_400(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(mcp_server, "FAULTLINE_USER_ID", "")
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant("shared", "not-a-uuid")
    assert exc.value.status_code == 400


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-DB integration — real two-account proof (auto-skip if no reachable control plane)
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
    reason="no reachable POSTGRES_DSN control plane (migration 131 tables) — live path proven via curl",
)


@live_db
def test_live_two_account_cross_account_denied():
    """End-to-end on a real DB: build 2 accounts + seats, prove cross-account is denied."""
    import psycopg2
    dsn = os.environ["POSTGRES_DSN"]
    a_slug = f"test-acct-a-{uuid.uuid4().hex[:8]}"
    b_slug = f"test-acct-b-{uuid.uuid4().hex[:8]}"
    ua = str(uuid.uuid4())
    ub = str(uuid.uuid4())
    conn = psycopg2.connect(dsn)
    acct_a = acct_b = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.accounts (name, slug, seat_limit) VALUES (%s,%s,%s) "
                "RETURNING account_id::text", ("test A", a_slug, 1))
            acct_a = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO public.accounts (name, slug, seat_limit) VALUES (%s,%s,%s) "
                "RETURNING account_id::text", ("test B", b_slug, 1))
            acct_b = cur.fetchone()[0]
            cur.execute("INSERT INTO public.account_users (account_id,user_id,status) "
                        "VALUES (%s,%s,'active')", (acct_a, ua))
            cur.execute("INSERT INTO public.account_users (account_id,user_id,status) "
                        "VALUES (%s,%s,'active')", (acct_b, ub))
        conn.commit()
        saas_seats.invalidate_seat_cache()

        # positive: A owns UA
        assert saas_seats.is_active_seat(acct_a, ua) is True
        # cross-account: A does NOT own UB
        assert saas_seats.is_active_seat(acct_a, ub) is False
        # unowned uuid
        assert saas_seats.is_active_seat(acct_a, str(uuid.uuid4())) is False

        # over-cap: AcctA seat_limit=1, already 1 active → second seat rejected
        res = saas_seats.register_seat(acct_a, str(uuid.uuid4()))
        assert res["ok"] is False and res["status_code"] in (402, 409)

        # UNIQUE(user_id): UA already belongs to AcctA → registering under B is 409
        res2 = saas_seats.register_seat(acct_b, ua)
        assert res2["ok"] is False and res2["status_code"] == 409
    finally:
        with conn.cursor() as cur:
            for acct in (acct_a, acct_b):
                if acct:
                    cur.execute("DELETE FROM public.account_users WHERE account_id=%s", (acct,))
                    cur.execute("DELETE FROM public.accounts WHERE account_id=%s", (acct,))
        conn.commit()
        conn.close()
        saas_seats.invalidate_seat_cache()
