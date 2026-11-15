"""MCP transport bind must pass the AUTHORIZED seat id (BYOM audit GAP-2).

THE LATENT WOUND this pins: ``src/mcp/server.py::bind_tenant`` (the SINGLE identity seam both
MCP transports resolve through) called ``bind_account_llm_config(account_id)`` with NO user_id.
For a corporate account that binds the org brain (correct); for a HOUSE/individual account
``resolve_brain(None, house)`` refuses BY DESIGN (``saas/llm_context.py``: "there is no seat
identity to resolve with; reaching for the shared row is the defect migration 202 closed") — so
the MCP process held NO brain for exactly the seats that own personal ones. Only
``remember_facts_tool`` re-bound seat-aware itself; every FUTURE in-MCP LLM op would have run
scope-less for house seats.

THE FIX: pass the already-authorized claimed id — by the time the bind site runs, ``claimed``
has been UUID-gated AND proven an ACTIVE seat of THIS account (or freshly auto-provisioned), so
it is a server-derived identity in the ``resolve_brain`` sense. Contract pinned here:
  1. house seat WITH its own ``seat_llm_config`` -> its OWN brain resolves through the bind;
  2. house seat WITHOUT one -> binds NOTHING (fail-closed; the shared row is never reached);
  3. corporate GOVERNED seat -> the org brain, UNCHANGED (``brain_scope == "account"``);
  4. empty claim -> the same 400 refusal as before, BEFORE any bind;
  5. SAAS off -> the no-op is byte-for-byte (the resolver gate lives inside
     ``bind_account_llm_config``).

Fake-conn style of tests/test_saas_seat_brain_isolation.py (the control-plane SQL router), so
the REAL ``bind_account_llm_config`` -> ``resolve_brain`` chain runs end-to-end. No live DB.

Run: SPACY_MODEL=en_core_web_sm PYTHONPATH=. python3 -m pytest tests/test_mcp_bind_tenant_seat_brain.py -q
"""

import contextlib

import pytest

from saas import crypto as saas_crypto
from saas import db as saas_db
from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context
from saas import seat_brain as saas_seat_brain
from saas import seats as saas_seats

from src.api import llm_client
from src.mcp import server as mcp_server


HOUSE = "aaaaaaaa-0000-4000-8000-000000001001"
CORP = "bbbbbbbb-0000-4000-8000-000000002002"
ALICE = "11111111-1111-4111-8111-111111111111"  # house seat A
CAROL = "33333333-3333-4333-8333-333333333333"  # corporate seat


@pytest.fixture
def cp(monkeypatch):
    """SAAS on + fake control plane + an always-active seat roster. Real resolver chain runs."""
    monkeypatch.setenv("SAAS_MODE", "1")
    store = {"account_kind": {HOUSE: "individual_house", CORP: "tenant"},
             "account_users": {ALICE: HOUSE, CAROL: CORP},
             "seat_cfg": {}, "acct_cfg": {},
             "individual_users": set()}  # user_ids holding a /me row; empty => governed

    class _FakeCur:
        def __init__(self):
            self._one = None

        def execute(self, sql, params=()):
            q = " ".join(str(sql).split())
            self._one = None
            if "FROM public.accounts" in q and "account_kind" in q:
                self._one = (store["account_kind"].get(str(params[0])),)
            elif "FROM public.account_users" in q:
                acct = store["account_users"].get(str(params[0]))
                self._one = (acct,) if acct else None
            elif "FROM public.individual_users" in q:
                if str(params[0]) in store["individual_users"]:
                    self._one = ("self_owned", True)
            elif "FROM public.seat_llm_config" in q:
                data = store["seat_cfg"].get((str(params[0]), str(params[1])))
                if data is not None:
                    spec = saas_llm_config._REDACT_COLS if "updated_at" in q \
                        else saas_llm_config._RESOLVE_COLS
                    self._one = tuple(data.get(c) for c in _cols(spec))
            elif "FROM public.account_llm_config" in q:
                data = store["acct_cfg"].get(str(params[0]))
                if data is not None:
                    spec = saas_llm_config._REDACT_COLS if "updated_at" in q \
                        else saas_llm_config._RESOLVE_COLS
                    self._one = tuple(data.get(c) for c in _cols(spec))
            else:
                raise AssertionError(f"unrouted control-plane query: {q[:160]}")

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
        def cursor(self):
            return _FakeCur()

        def close(self):
            pass

    @contextlib.contextmanager
    def _connection():
        yield _FakeConn()

    monkeypatch.setattr(saas_db, "connection", _connection)
    monkeypatch.setattr(saas_llm_config, "validate_egress_url", lambda url: None)
    monkeypatch.setattr(saas_crypto, "decrypt", lambda ct: ("PLAIN:" + str(ct)) if ct else None)
    # Every configured identity is an ACTIVE seat: this suite tests the BRAIN seam, not the
    # authorization seam (that one owns tests/test_saas_isolation.py).
    monkeypatch.setattr(saas_seats, "is_active_seat",
                        lambda account_id, user_id: user_id in store["account_users"])
    yield store


@pytest.fixture(autouse=True)
def _clean_context():
    saas_llm_config.invalidate_config_cache()
    saas_seat_brain.invalidate_kind_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._user_acct_cache.clear()
    if hasattr(saas_seat_brain, "_grant_cache"):
        saas_seat_brain._grant_cache.clear()
    saas_llm_context._current_llm_config.set(None)
    yield
    saas_llm_config.invalidate_config_cache()
    saas_seat_brain.invalidate_kind_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._user_acct_cache.clear()
    if hasattr(saas_seat_brain, "_grant_cache"):
        saas_seat_brain._grant_cache.clear()
    saas_llm_context._current_llm_config.set(None)



def _cols(spec: str) -> list:
    """Split a llm_config column SPEC STRING (e.g. ``_RESOLVE_COLS``) into column names.

    ⚠️ These constants are comma-joined STRINGS, not tuples — iterating one directly yields
    CHARACTERS, and a fixture that feeds the resolver a mis-aligned row is worse than none.
    Mirrors tests/test_saas_seat_brain_isolation.py::_cols."""
    return [c.strip() for c in spec.replace("\n", " ").split(",") if c.strip()]


def _brain(base_url, model, key_ct):
    return {"backend_type": "openai", "base_url": base_url, "model": model,
            "api_key_ciphertext": key_ct, "embedding_backend": "internal", "configured": True}


# =====================================================================
# 1 — the GAP-2 fix: a house seat resolves ITS OWN brain in the MCP process
# =====================================================================

def test_house_seat_with_own_brain_resolves_it_through_the_mcp_bind_path(cp):
    cp["acct_cfg"][HOUSE] = _brain("https://shared.example", "shared-model", "SHARED_KEY")
    cp["seat_cfg"][(ALICE, HOUSE)] = _brain("https://alice.example", "alice-model", "ALICE_KEY")

    resolved = mcp_server.bind_tenant(f"account:{HOUSE}", ALICE)

    assert resolved == ALICE
    cfg = saas_llm_context.current_llm_config()
    assert cfg is not None, "house seat bound NO brain in the MCP process (the GAP-2 wound)"
    assert cfg["base_url"] == "https://alice.example", \
        "house seat must resolve its OWN seat_llm_config, never the shared row"
    assert cfg.get("brain_scope") == "seat"
    assert "SHARED_KEY" not in str(cfg) and "ALICE_KEY" in str(cfg)


def test_house_seat_without_own_brain_binds_nothing_fail_closed(cp):
    # The legacy shared row EXISTS (the exact state migration 202 made dangerous) ...
    cp["acct_cfg"][HOUSE] = _brain("https://shared.example", "shared-model", "SHARED_KEY")
    # ... but ALICE never saved a seat row.
    resolved = mcp_server.bind_tenant(f"account:{HOUSE}", ALICE)

    assert resolved == ALICE
    # Nothing is bound: under SAAS that is fail-closed (zero spend), never the shared row.
    assert saas_llm_context.current_llm_config() is None
    assert llm_client.saas_global_brain_blocked() is True


# =====================================================================
# 2 — tenant behavior UNCHANGED: a governed corporate seat keeps the org brain
# =====================================================================

def test_tenant_governed_seat_binds_the_org_brain_unchanged(cp):
    cp["acct_cfg"][CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    # No /me row for CAROL -> governed -> account scope (seat_holds_endpoint_control False).
    resolved = mcp_server.bind_tenant(f"account:{CORP}", CAROL)

    assert resolved == CAROL
    cfg = saas_llm_context.current_llm_config()
    assert cfg is not None and cfg["base_url"] == "https://corp.example"
    assert cfg.get("brain_scope") == "account", \
        "a governed corporate seat must keep the governance-derived account scope"


def test_tenant_granted_seat_resolves_its_own_row_via_the_transport_bind(cp):
    """A corporate seat GRANTED endpoint control now gets its governance-derived SEAT scope at
    the transport bind too (previously the bind silently bound nothing for it)."""
    cp["acct_cfg"][CORP] = _brain("https://corp.example", "corp-model", "CORP_KEY")
    cp["seat_cfg"][(CAROL, CORP)] = _brain("https://carol.example", "carol-model", "CAROL_KEY")
    cp["individual_users"].add(CAROL)  # holds a /me row with can_control_endpoint=True

    mcp_server.bind_tenant(f"account:{CORP}", CAROL)

    cfg = saas_llm_context.current_llm_config()
    assert cfg is not None and cfg["base_url"] == "https://carol.example"
    assert cfg.get("brain_scope") == "seat"


# =====================================================================
# 3 — the seam itself: the authorized id reaches the bind, refusals bind nothing
# =====================================================================

def test_bind_site_passes_the_authorized_claimed_id(cp, monkeypatch):
    got = {}

    def _spy(account_id, user_id=None):
        got["account_id"] = account_id
        got["user_id"] = user_id
        return None

    monkeypatch.setattr(saas_llm_context, "bind_account_llm_config", _spy)

    mcp_server.bind_tenant(f"account:{HOUSE}", ALICE)

    assert got == {"account_id": HOUSE, "user_id": ALICE}, \
        "bind_tenant must pass the already-authorized claimed seat id (GAP-2)"


def test_empty_claim_still_refuses_400_before_any_bind(cp, monkeypatch):
    got = []
    monkeypatch.setattr(saas_llm_context, "bind_account_llm_config",
                        lambda a, user_id=None: got.append((a, user_id)) or None)

    with pytest.raises(mcp_server.TenantSpoofError) as exc:
        mcp_server.bind_tenant(f"account:{HOUSE}", "")

    assert exc.value.status_code == 400
    assert got == [], "a refused identity must never reach the brain bind"


def test_saas_off_bind_is_a_noop(cp, monkeypatch):
    """SAAS off: the account-principal branch still runs, but the resolver gate inside
    bind_account_llm_config keeps the bind a no-op — byte-for-byte unchanged."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    saas_llm_context._user_acct_cache.clear()

    resolved = mcp_server.bind_tenant(f"account:{HOUSE}", ALICE)

    assert resolved == ALICE
    assert saas_llm_context.current_llm_config() is None