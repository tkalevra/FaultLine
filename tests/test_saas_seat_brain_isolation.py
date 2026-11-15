"""PER-SEAT BYO brain — credential isolation on the shared house account (migration 202).

THE DEFECT UNDER TEST
---------------------
Self-serve individuals are SEATS under ONE shared house account ("Singles",
``accounts.account_kind='individual_house'``). The BYO brain config was keyed by ACCOUNT
(migration 135, ``account_llm_config.account_id`` is the PK) and the individual console read and
wrote it with that shared key — so every individual shared ONE row, including its Fernet-encrypted
``api_key_ciphertext``. Individual A sets an endpoint; individual B's ingest runs on A's endpoint,
spends A's key, and B can overwrite it.

WHAT THIS FILE PINS
-------------------
 1. SAAS_MODE OFF is byte-for-byte unchanged — zero DB access on every new path.
 2. The DISCRIMINATOR: house/individual → per-seat; corporate (tenant/team/clinical) → the shared
    account row, UNCHANGED. Both directions, because "fixing" corporate into per-seat would break
    the whole point of a tenant-governed deployment.
 3. CROSS-SEAT ISOLATION: two seats on the SAME house account resolve DIFFERENT brains, and seat
    A's credential is never reachable from seat B.
 4. RESOLVER-SIDE PROOF: ``bind_request_brain`` → ``current_llm_config`` returns the SEAT row. A
    per-seat row that the resolver never consults is worthless, and that exact bug class
    ("VERIFY THE READ, NOT THE WRITE") has bitten this repo repeatedly.
 5. NO FALLBACK to the shared account row for a house seat — even when one exists.
 6. The tenant-admin view exposes HEALTH ONLY, never a credential or an endpoint identity.

Run with:
  python3 tools/fltest.py --bug SEATBRAIN --test tests/test_saas_seat_brain_isolation.py \
      --note "per-seat brain: cross-seat isolation + resolver-side read"
"""

import contextlib

import pytest


# ══════════════════════════════════════════════════════════════════════════════════════
# Fake control plane — a tiny SQL router over an in-memory store.
#
# Column ORDER is derived from the module's own column lists (``_RESOLVE_COLS`` /
# ``_REDACT_COLS``) rather than hardcoded here, so a column added to the table cannot make this
# harness silently feed the code a mis-aligned row (a fixture that lies is worse than no fixture).
# ══════════════════════════════════════════════════════════════════════════════════════

HOUSE = "aaaaaaaa-0000-0000-0000-000000000001"     # accounts.account_kind='individual_house'
CORP = "bbbbbbbb-0000-0000-0000-000000000002"      # accounts.account_kind='tenant'

ALICE = "11111111-1111-1111-1111-111111111111"     # house seat A
BOB = "22222222-2222-2222-2222-222222222222"       # house seat B
CAROL = "33333333-3333-3333-3333-333333333333"     # corporate seat
DAVE = "44444444-4444-4444-4444-444444444444"      # corporate seat


class _Store:
    def __init__(self):
        self.account_kind = {HOUSE: "individual_house", CORP: "tenant"}
        self.account_users = {ALICE: HOUSE, BOB: HOUSE, CAROL: CORP, DAVE: CORP}
        self.seat_cfg: dict[tuple, dict] = {}      # (user_id, account_id) -> col->value
        self.acct_cfg: dict[str, dict] = {}        # account_id -> col->value
        self.queries: list[str] = []


def _cols(spec: str) -> list:
    return [c.strip() for c in spec.replace("\n", " ").split(",") if c.strip()]


def _row(colspec: str, data: dict) -> tuple:
    return tuple(data.get(c) for c in _cols(colspec))


class _FakeCur:
    def __init__(self, store: _Store):
        self.s = store
        self._one = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        from saas import llm_config as lc
        q = " ".join(str(sql).split())
        self.s.queries.append(q)
        self._one = None

        if "FROM public.accounts" in q and "account_kind" in q:
            self._one = (self.s.account_kind.get(params[0]),)
            return
        if "FROM public.account_users" in q:
            acct = self.s.account_users.get(params[0])
            self._one = (acct,) if acct else None
            return
        if "FROM public.seat_llm_config" in q:
            data = self.s.seat_cfg.get((params[0], params[1]))
            if data is None:
                return
            spec = lc._REDACT_COLS if "updated_at" in q else lc._RESOLVE_COLS
            self._one = _row(spec, data)
            return
        if "FROM public.account_llm_config" in q:
            data = self.s.acct_cfg.get(params[0])
            if data is None:
                return
            spec = lc._REDACT_COLS if "updated_at" in q else lc._RESOLVE_COLS
            self._one = _row(spec, data)
            return
        raise AssertionError(f"fake control plane got an unrouted query: {q[:160]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return [self._one] if self._one else []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.s = store

    def cursor(self):
        return _FakeCur(self.s)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def cp(monkeypatch):
    """SAAS on + a fake control plane + no real crypto/DNS. Caches cleared per test."""
    monkeypatch.setenv("SAAS_MODE", "1")

    store = _Store()

    @contextlib.contextmanager
    def _connection():
        yield _FakeConn(store)

    from saas import crypto as _crypto
    from saas import db as _db
    from saas import llm_config as lc
    from saas import llm_context as lx
    from saas import seat_brain as sb

    monkeypatch.setattr(_db, "connection", _connection)
    # No DNS, no network: the SSRF guard is exercised in its own tests; here it must not turn a
    # fixture URL into a resolver failure and hide the thing under test.
    monkeypatch.setattr(lc, "validate_egress_url", lambda url: None)
    # Decryption is identity so a test can assert WHICH credential came back, by value.
    monkeypatch.setattr(_crypto, "decrypt", lambda ct: ("PLAIN:" + str(ct)) if ct else None)

    lc.invalidate_config_cache()
    sb.invalidate_kind_cache()
    lx.invalidate_schema_account_cache()
    lx._user_acct_cache.clear()
    yield store
    lc.invalidate_config_cache()
    sb.invalidate_kind_cache()
    lx.invalidate_schema_account_cache()
    lx._user_acct_cache.clear()


def _brain(base_url, model, key_ct, **extra) -> dict:
    d = {"backend_type": "openai", "base_url": base_url, "model": model,
         "api_key_ciphertext": key_ct, "embedding_backend": "internal", "configured": True}
    d.update(extra)
    return d


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. SAAS_MODE OFF — byte-for-byte unchanged, ZERO control-plane access
# ══════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_seat_resolve_is_a_noop_with_no_db(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)

    def _boom():
        raise AssertionError("SAAS off must not touch the control plane")

    from saas import db as _db
    from saas import llm_config as lc
    monkeypatch.setattr(_db, "connection", lambda: _boom())
    lc.invalidate_config_cache()
    assert lc.resolve_seat_llm_config(ALICE, HOUSE) is None


def test_saas_off_resolve_brain_and_bind_are_noops(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)

    def _boom():
        raise AssertionError("SAAS off must not touch the control plane")

    from saas import db as _db
    from saas import llm_context as lx
    from saas import seat_brain as sb
    monkeypatch.setattr(_db, "connection", lambda: _boom())
    sb.invalidate_kind_cache()
    assert sb.resolve_brain(ALICE, HOUSE) is None
    assert lx.bind_request_brain(ALICE) is None
    assert lx.current_llm_config() is None


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. THE DISCRIMINATOR — house is per-seat; corporate is UNCHANGED
# ══════════════════════════════════════════════════════════════════════════════════════

def test_house_account_is_seat_scoped(cp):
    from saas import seat_brain as sb
    assert sb.seat_scoped(HOUSE) is True


def test_corporate_kinds_are_not_seat_scoped(cp):
    """tenant / team / clinical keep ONE shared brain for all their seats — by design."""
    from saas import seat_brain as sb
    for kind in ("tenant", "team", "clinical"):
        cp.account_kind[CORP] = kind
        sb.invalidate_kind_cache()
        assert sb.seat_scoped(CORP) is False, f"{kind} must keep the shared account brain"


def test_unknown_account_fails_towards_not_sharing_a_credential(cp):
    """An unreadable/unknown account resolves SEAT-scoped: at worst no brain (fail closed),
    never somebody else's key. A security property must not depend on a read succeeding."""
    from saas import seat_brain as sb
    assert sb.seat_scoped("99999999-0000-0000-0000-000000000009") is True


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. CROSS-SEAT ISOLATION — the actual exposure
# ══════════════════════════════════════════════════════════════════════════════════════

def test_two_house_seats_get_different_brains_and_never_each_others_key(cp):
    """THE PROOF. Alice and Bob are seats on the SAME house account. Each resolves her/his OWN
    endpoint and OWN credential; neither can reach the other's."""
    from saas import seat_brain as sb
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    cp.seat_cfg[(BOB, HOUSE)] = _brain("https://bob.example", "bob-model", "BOB_KEY")

    a = sb.resolve_brain(ALICE, HOUSE)
    b = sb.resolve_brain(BOB, HOUSE)

    assert a["base_url"] == "https://alice.example" and a["model"] == "alice-model"
    assert b["base_url"] == "https://bob.example" and b["model"] == "bob-model"
    assert a["api_key"] == "PLAIN:ALICE_KEY"
    assert b["api_key"] == "PLAIN:BOB_KEY"
    # The whole point, stated as an assertion: no crossing, in either direction.
    assert a["api_key"] != b["api_key"]
    assert "ALICE_KEY" not in str(b)
    assert "BOB_KEY" not in str(a)
    assert a["brain_scope"] == b["brain_scope"] == "seat"


def test_a_house_seat_with_no_brain_gets_NOTHING_not_its_neighbours(cp):
    """Bob has configured nothing. He must resolve NO brain — not Alice's."""
    from saas import seat_brain as sb
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    assert sb.resolve_brain(BOB, HOUSE) is None


def test_house_seat_NEVER_falls_back_to_the_shared_account_row(cp):
    """Even with a populated shared row present (legacy / restored dump / operator write), a
    house seat must not resolve it — that row IS the defect."""
    from saas import seat_brain as sb
    cp.acct_cfg[HOUSE] = _brain("https://shared.example", "shared-model", "SHARED_KEY")
    assert sb.resolve_brain(BOB, HOUSE) is None
    assert "SHARED_KEY" not in str(sb.resolve_brain(BOB, HOUSE))


def test_seat_row_is_scoped_to_the_account_it_was_saved_under(cp):
    """The row is matched on (user_id, account_id): after a transfer to another account the
    personal brain stops resolving instead of crossing a governance boundary."""
    from saas import llm_config as lc
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    assert lc.resolve_seat_llm_config(ALICE, HOUSE) is not None
    assert lc.resolve_seat_llm_config(ALICE, CORP) is None


# ══════════════════════════════════════════════════════════════════════════════════════
# 4. CORPORATE IS UNCHANGED — both directions
# ══════════════════════════════════════════════════════════════════════════════════════

def test_corporate_seats_share_the_account_brain(cp):
    """Two seats of ONE corporate tenant resolve the SAME, account-owned brain. This is CORRECT
    and must not become per-seat."""
    from saas import seat_brain as sb
    cp.acct_cfg[CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    c = sb.resolve_brain(CAROL, CORP)
    d = sb.resolve_brain(DAVE, CORP)
    assert c["base_url"] == d["base_url"] == "https://corp.example"
    assert c["api_key"] == d["api_key"] == "PLAIN:CORP_KEY"
    assert c["brain_scope"] == "account"


def test_a_seat_row_is_IGNORED_for_a_corporate_account(cp):
    """The seat lane must not leak into the governed corporate path: a stray seat row does not
    override the organization's brain."""
    from saas import seat_brain as sb
    cp.acct_cfg[CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    cp.seat_cfg[(CAROL, CORP)] = _brain("https://rogue.example", "rogue-model", "ROGUE_KEY")
    c = sb.resolve_brain(CAROL, CORP)
    assert c["base_url"] == "https://corp.example"
    assert "ROGUE_KEY" not in str(c)


def test_corporate_resolution_never_reads_the_seat_table(cp):
    """Structural, not behavioural: prove the corporate path does not even QUERY the seat table,
    so it cannot regress into consulting it."""
    from saas import seat_brain as sb
    cp.acct_cfg[CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    cp.queries.clear()
    sb.resolve_brain(CAROL, CORP)
    assert not any("seat_llm_config" in q for q in cp.queries), cp.queries


# ══════════════════════════════════════════════════════════════════════════════════════
# 5. RESOLVER-SIDE PROOF — the ENGINE's bind actually reads the seat row
# ══════════════════════════════════════════════════════════════════════════════════════

def test_bind_request_brain_binds_the_SEAT_row(cp):
    """VERIFY THE READ, NOT THE WRITE. This is the seam the backend uses on every request
    (src/api/main.py:12634 → bind_request_brain). If it did not consult the seat row, the whole
    per-seat table would be decoration."""
    from saas import llm_context as lx
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    token = lx.bind_request_brain(ALICE)
    try:
        cfg = lx.current_llm_config()
        assert cfg is not None
        assert cfg["base_url"] == "https://alice.example"
        assert cfg["api_key"] == "PLAIN:ALICE_KEY"
        assert cfg["brain_scope"] == "seat"
        assert cfg.get("seat_user_id") == ALICE
    finally:
        lx.reset_llm_config(token)


def test_bind_request_brain_gives_two_house_seats_two_different_brains(cp):
    """The end-to-end statement of the fix at the seam that matters."""
    from saas import llm_context as lx
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    cp.seat_cfg[(BOB, HOUSE)] = _brain("https://bob.example", "bob-model", "BOB_KEY")

    t = lx.bind_request_brain(ALICE)
    got_a = dict(lx.current_llm_config())
    lx.reset_llm_config(t)

    t = lx.bind_request_brain(BOB)
    got_b = dict(lx.current_llm_config())
    lx.reset_llm_config(t)

    assert got_a["base_url"] != got_b["base_url"]
    assert got_a["api_key"] != got_b["api_key"]


def test_bind_request_brain_fails_CLOSED_for_a_house_seat_with_no_brain(cp):
    """No brain resolvable → the no-brain sentinel, NOT the global env brain. Cost-safe: the LLM
    stack returns safe empty defaults and spends nothing (constraint-saas-cpu-cloud-tenant-brain)."""
    from saas import llm_context as lx
    cp.acct_cfg[HOUSE] = _brain("https://shared.example", "shared-model", "SHARED_KEY")
    token = lx.bind_request_brain(BOB)
    try:
        cfg = lx.current_llm_config()
        assert cfg is not None, "must bind the sentinel, not fall through to the global brain"
        assert cfg.get("no_brain") is True
        assert "SHARED_KEY" not in str(cfg)
    finally:
        lx.reset_llm_config(token)


def test_bind_request_brain_still_binds_the_shared_brain_for_a_corporate_seat(cp):
    from saas import llm_context as lx
    cp.acct_cfg[CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    token = lx.bind_request_brain(CAROL)
    try:
        cfg = lx.current_llm_config()
        assert cfg["base_url"] == "https://corp.example"
        assert cfg["brain_scope"] == "account"
    finally:
        lx.reset_llm_config(token)


def test_bind_account_llm_config_without_a_seat_refuses_the_shared_house_row(cp):
    """The MCP seam calls this with an account_id only. For a house account that must resolve
    NOTHING rather than reach for the shared row."""
    from saas import llm_context as lx
    cp.acct_cfg[HOUSE] = _brain("https://shared.example", "shared-model", "SHARED_KEY")
    assert lx.bind_account_llm_config(HOUSE) is None
    # ...and passing the seat identity DOES resolve that seat's own brain.
    cp.seat_cfg[(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")
    token = lx.bind_account_llm_config(HOUSE, user_id=ALICE)
    try:
        assert lx.current_llm_config()["base_url"] == "https://alice.example"
    finally:
        lx.reset_llm_config(token)


# ══════════════════════════════════════════════════════════════════════════════════════
# 6. TENANT-ADMIN VIEW — health only, never a credential
# ══════════════════════════════════════════════════════════════════════════════════════

def test_admin_seat_projection_carries_no_credential_or_endpoint_identity():
    """Structural: the admin projection is an explicit ALLOW-LIST, so a column added to the
    redacted config later cannot silently surface on the admin view."""
    from saas.tenant_console import _SEAT_PROFILE_PUBLIC_KEYS
    forbidden = {"api_key", "api_key_set", "api_key_ciphertext", "base_url", "model",
                 "backend_type", "embedding_url", "embedding_key_set", "extra_headers"}
    assert not (set(_SEAT_PROFILE_PUBLIC_KEYS) & forbidden)
    assert set(_SEAT_PROFILE_PUBLIC_KEYS) == {
        "latency_profile", "latency_profile_source", "latency_p50_ms",
        "latency_p95_ms", "latency_samples", "latency_measured_at"}


def test_admin_seat_config_view_shows_profile_but_not_the_key(cp):
    from saas import tenant_console as tc
    cp.seat_cfg[(ALICE, HOUSE)] = _brain(
        "https://alice.example", "alice-model", "ALICE_KEY",
        latency_profile="local", latency_profile_source="measured", latency_p95_ms=9000)
    out = tc._seat_brain_config_for_admin(HOUSE, ALICE)
    assert out["latency_profile"] == "local"
    assert out["latency_p95_ms"] == 9000
    assert out["configured"] is True
    assert out["brain_scope"] == "seat"
    blob = str(out)
    for secret in ("ALICE_KEY", "alice.example", "alice-model"):
        assert secret not in blob, f"admin view leaked {secret!r}"


def test_admin_seat_config_view_for_a_corporate_seat_reads_the_account_row(cp):
    from saas import tenant_console as tc
    cp.acct_cfg[CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY",
                               latency_profile="hosted-fast")
    out = tc._seat_brain_config_for_admin(CORP, CAROL)
    assert out["brain_scope"] == "account"
    assert out["latency_profile"] == "hosted-fast"
    assert "CORP_KEY" not in str(out)
