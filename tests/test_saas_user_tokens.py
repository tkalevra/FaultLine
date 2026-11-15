"""SaaS Phase 4 — PER-USER TOKEN + ONBOARDING isolation PROOF.

A per-user token (``flu_…``) authenticates as EXACTLY ONE bound UUID (one seat), activating
``bind_tenant`` Option A: the token can act ONLY as its bound user; presenting ANY other
X-OpenWebUI-User-Id is a spoof → 403. This is the crown-jewel per-user scoping.

Coverage layers (mirror test_saas_isolation.py / test_saas_memory_ops.py):
  1. SEAM: _resolve_principal maps flu_ → bound UUID; bind_tenant Option A enforces scoping.
  2. Pure unit: token format/hash/prefix, is_user_token, onboarding blob + OTS fail-graceful.
  3. ROUTE: mint/onboard via TestClient — own seat OK, cross-account 403, SAAS off → 404.

Run: python3 -m pytest tests/test_saas_user_tokens.py -q
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mcp import server as mcp_server
from src.mcp import http_server as http_server

from saas import admin as saas_admin
from saas import seats as saas_seats
from saas import accounts as saas_accounts
from saas import tokens as saas_tokens
from saas import onboarding as saas_onboarding


UA = "11111111-1111-4111-8111-111111111111"   # AcctA's seat
UB = "22222222-2222-4222-8222-222222222222"   # AcctB's seat
UNOWNED = "33333333-3333-4333-8333-333333333333"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

KEY_A = "flk_aaaaaaaa_secretA"
KEY_B = "flk_bbbbbbbb_secretB"


# ════════════════════════════════════════════════════════════════════════════════
# Pure unit — token format / hashing (mirror account-key contract)
# ════════════════════════════════════════════════════════════════════════════════

def test_user_token_format_and_hash():
    full, prefix, token_hash = saas_tokens.generate_user_token()
    assert full.startswith("flu_")
    assert prefix.startswith("flu_")
    assert full.startswith(prefix)          # prefix is a real prefix of the token
    assert prefix != full                   # secret half not in the prefix
    assert len(token_hash) == 64            # sha256 hex
    assert saas_tokens.generate_user_token()[0] != full  # unique each mint


def test_user_token_distinct_from_account_key():
    """flu_ (user token) and flk_ (account key) canaries never collide."""
    full, _, _ = saas_tokens.generate_user_token()
    assert saas_tokens.is_user_token(full) is True
    assert saas_tokens.is_user_token("flk_aaaa_secret") is False
    assert saas_tokens.is_user_token(None) is False


def test_user_token_prefix_never_leaks_secret():
    full, prefix, _ = saas_tokens.generate_user_token()
    assert saas_tokens.prefix_of(full) == prefix
    secret_half = full[len(prefix) + 1:]
    assert secret_half not in saas_tokens.prefix_of(full)


# ════════════════════════════════════════════════════════════════════════════════
# SEAM: _resolve_principal maps a per-user token to its BOUND UUID
# ════════════════════════════════════════════════════════════════════════════════

def test_resolve_principal_user_token_returns_bound_uuid(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_tokens, "resolve_user_token", lambda t: UA)
    # account-key resolver must NOT be consulted for a flu_ token
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: pytest.fail("account resolver reached for a flu_ token"))
    principal = http_server._resolve_principal("flu_deadbeef_secret")
    assert principal == UA


def test_resolve_principal_revoked_user_token_is_none(monkeypatch):
    """A revoked/unknown token → None → 401. Never retried as an account key."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_tokens, "resolve_user_token", lambda t: None)
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account",
                        lambda k: pytest.fail("account resolver reached for a flu_ token"))
    assert http_server._resolve_principal("flu_deadbeef_revoked") is None


def test_resolve_principal_account_key_still_works(monkeypatch):
    """flk_ still resolves to account:<id> (unchanged)."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: ACCT_A)
    assert http_server._resolve_principal("flk_aaaa_secret") == f"account:{ACCT_A}"


def test_resolve_principal_user_token_resolver_error_fails_closed(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")

    def boom(_t):
        raise RuntimeError("db down")
    monkeypatch.setattr(saas_tokens, "resolve_user_token", boom)
    assert http_server._resolve_principal("flu_x_y") is None


# ════════════════════════════════════════════════════════════════════════════════
# SEAM: bind_tenant Option A — the crown-jewel per-user scoping
# ════════════════════════════════════════════════════════════════════════════════

def test_option_a_token_acts_as_bound_uuid(monkeypatch):
    """token_A principal (UUID_A) + claimed UUID_A → allowed."""
    monkeypatch.delenv("SAAS_MODE", raising=False)  # principal is a bare UUID, not account:
    resolved = mcp_server.bind_tenant(UA, UA)
    assert resolved == UA


def test_option_a_token_cannot_act_as_another_user(monkeypatch):
    """token_A principal (UUID_A) + claimed UUID_B → 403 (per-user token spoof-blocked)."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(UA, UB)
    assert exc.value.status_code == 403


def test_option_a_empty_claim_binds_to_token_uuid(monkeypatch):
    """No claimed header + per-user token → acts as its bound UUID (token IS identity)."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    assert mcp_server.bind_tenant(UA, "") == UA


def test_option_a_case_insensitive_match(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    assert mcp_server.bind_tenant(UA.upper(), UA) == UA


# ════════════════════════════════════════════════════════════════════════════════
# Onboarding — instruction blob + OTS fail-graceful
# ════════════════════════════════════════════════════════════════════════════════

def test_instruction_blob_contains_token_uuid_and_both_clients():
    blob = saas_onboarding.build_instruction_blob("https://host:8002", "flu_abc_secret", UA)
    assert "flu_abc_secret" in blob
    assert UA in blob
    assert "https://host:8002" in blob
    assert "Claude Desktop" in blob
    assert "OpenWebUI" in blob
    assert "claude_desktop_config.json" in blob
    # the MCP JSON-RPC url + the user header both appear (paste-ready)
    assert "https://host:8002/mcp" in blob
    assert "X-OpenWebUI-User-Id" in blob


def test_ots_unconfigured_is_graceful(monkeypatch):
    monkeypatch.delenv("OTS_USERNAME", raising=False)
    monkeypatch.delenv("OTS_APITOKEN", raising=False)
    out = saas_onboarding.push_to_ots("some blob")
    assert out["ots_configured"] is False
    assert out["share_url"] is None


def test_ots_configured_returns_url(monkeypatch):
    monkeypatch.setenv("OTS_USERNAME", "op@example.com")
    monkeypatch.setenv("OTS_APITOKEN", "apitok")
    monkeypatch.setenv("OTS_API_URL", "https://ots.example.com")

    class _Resp:
        status_code = 200
        def json(self):
            return {"secret_key": "SECRETKEY123"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    out = saas_onboarding.push_to_ots("blob")
    assert out["ots_configured"] is True
    assert out["share_url"] == "https://ots.example.com/secret/SECRETKEY123"


def test_ots_unreachable_is_graceful(monkeypatch):
    monkeypatch.setenv("OTS_USERNAME", "op@example.com")
    monkeypatch.setenv("OTS_APITOKEN", "apitok")

    import httpx
    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(httpx, "post", boom)
    out = saas_onboarding.push_to_ots("blob")
    # configured but unreachable → still no exception, share_url None
    assert out["ots_configured"] is True
    assert out["share_url"] is None


# ════════════════════════════════════════════════════════════════════════════════
# ROUTE: mint / onboard via the admin API — own seat OK, cross-account 403
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    keymap = {KEY_A: ACCT_A, KEY_B: ACCT_B}
    monkeypatch.setattr(saas_accounts, "resolve_key_to_account", lambda k: keymap.get(k))
    monkeypatch.delenv("SAAS_OPERATOR_KEY", raising=False)
    roster = {(ACCT_A, UA): True, (ACCT_B, UB): True}
    monkeypatch.setattr(saas_seats, "is_active_seat", lambda a, u: roster.get((a, u), False))
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def fake_tokens(monkeypatch):
    minted = {"calls": []}

    def fake_mint(account_id, user_id):
        minted["calls"].append((account_id, user_id))
        return {"token_id": "tok-1", "token_prefix": "flu_abcd1234",
                "user_id": user_id, "token": "flu_abcd1234_secretXYZ"}

    monkeypatch.setattr(saas_tokens, "mint_user_token", fake_mint)
    monkeypatch.setattr(saas_tokens, "revoke_user_token", lambda a, u, t: True)
    # keep OTS unconfigured so onboard is fully offline in the route test
    monkeypatch.delenv("OTS_USERNAME", raising=False)
    monkeypatch.delenv("OTS_APITOKEN", raising=False)
    return minted


def test_mint_token_own_seat_allowed(client, fake_tokens):
    r = client.post(f"/admin/users/{UA}/token", headers=_auth(KEY_A))
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("flu_")
    assert fake_tokens["calls"] == [(ACCT_A, UA)]


def test_mint_token_cross_account_denied(client, fake_tokens):
    r = client.post(f"/admin/users/{UB}/token", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_tokens["calls"] == []  # never minted (authz before mint)


def test_mint_token_unowned_uuid_denied(client, fake_tokens):
    r = client.post(f"/admin/users/{UNOWNED}/token", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_tokens["calls"] == []


def test_mint_token_malformed_uuid_400(client, fake_tokens):
    r = client.post("/admin/users/not-a-uuid/token", headers=_auth(KEY_A))
    assert r.status_code == 400


def test_onboard_own_seat_returns_blob(client, fake_tokens):
    r = client.post(f"/admin/users/{UA}/onboard", headers=_auth(KEY_A))
    assert r.status_code == 200
    body = r.json()
    assert body["ots_configured"] is False       # OTS not configured in this test
    assert body["share_url"] is None
    assert "flu_abcd1234_secretXYZ" in body["instructions"]  # fallback blob carries token
    assert body["token_prefix"] == "flu_abcd1234"


def test_onboard_cross_account_denied(client, fake_tokens):
    r = client.post(f"/admin/users/{UB}/onboard", headers=_auth(KEY_A))
    assert r.status_code == 403
    assert fake_tokens["calls"] == []


def test_revoke_token_cross_account_denied(client, fake_tokens):
    r = client.request("DELETE", f"/admin/users/{UB}/token/tok-1", headers=_auth(KEY_A))
    assert r.status_code == 403


def test_mint_token_no_bearer_401(client, fake_tokens):
    r = client.post(f"/admin/users/{UA}/token")
    assert r.status_code == 401


def test_saas_off_mint_token_404(client, fake_tokens, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.post(f"/admin/users/{UA}/token", headers=_auth(KEY_A))
    assert r.status_code == 404


def test_saas_off_onboard_404(client, fake_tokens, monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    r = client.post(f"/admin/users/{UA}/onboard", headers=_auth(KEY_A))
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════════════════
# LIVE-DB integration — real mint + resolve + revoke (auto-skip if no control plane)
# ════════════════════════════════════════════════════════════════════════════════

def _control_plane_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.account_user_tokens')")
            ok = cur.fetchone()[0] is not None
        conn.close()
        return ok
    except Exception:
        return False


live_db = pytest.mark.skipif(
    not _control_plane_available(),
    reason="no reachable POSTGRES_DSN control plane (migration 132 tables) — live path proven via curl",
)


@live_db
def test_live_mint_resolve_revoke_roundtrip():
    """End-to-end on a real DB: mint a token, resolve it to the bound UUID, revoke → deny."""
    import psycopg2
    dsn = os.environ["POSTGRES_DSN"]
    slug = f"test-tok-{uuid.uuid4().hex[:8]}"
    ua = str(uuid.uuid4())
    conn = psycopg2.connect(dsn)
    acct = None
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO public.accounts (name, slug, seat_limit) "
                        "VALUES (%s,%s,%s) RETURNING account_id::text", ("tok test", slug, 1))
            acct = cur.fetchone()[0]
            cur.execute("INSERT INTO public.account_users (account_id,user_id,status) "
                        "VALUES (%s,%s,'active')", (acct, ua))
        conn.commit()
        saas_tokens.invalidate_token_cache()
        saas_seats.invalidate_seat_cache()

        minted = saas_tokens.mint_user_token(acct, ua)
        assert minted and minted["token"].startswith("flu_")

        # resolve → the bound UUID
        assert saas_tokens.resolve_user_token(minted["token"]) == ua.lower()

        # revoke → resolves to None (cache invalidated by revoke)
        assert saas_tokens.revoke_user_token(acct, ua, minted["token_id"]) is True
        assert saas_tokens.resolve_user_token(minted["token"]) is None
    finally:
        with conn.cursor() as cur:
            if acct:
                cur.execute("DELETE FROM public.account_user_tokens WHERE account_id=%s", (acct,))
                cur.execute("DELETE FROM public.account_users WHERE account_id=%s", (acct,))
                cur.execute("DELETE FROM public.accounts WHERE account_id=%s", (acct,))
        conn.commit()
        conn.close()
        saas_tokens.invalidate_token_cache()
