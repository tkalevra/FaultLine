"""Brain-scoped budget/cache scope — the owner's scoping rule, proven by construction.

The rule (DEV/DESIGN-brain-budget-event-cache.md §0):
  * corporate/team account, one shared brain  → seats SHARE one scope (one budget, one cache)
  * house/individual account, per-seat brains  → seats ISOLATE (distinct scopes)
  * two customers on the same public endpoint  → NEVER share (api-key digest is part of identity)

These tests exercise ``redis_coord.scope_for_brain`` directly (the primitive) AND the rate-key
derivation that consumes it (``llm_calls._resolve_rate_limit_key``), because the bug this closes
lived in the latter keying by ``account:`` instead of by brain.
"""
import os
import sys
from pathlib import Path

# Make src/ importable when run via fltest / pytest without an installed package.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))


# ── cfg builders matching saas/llm_config._build_config + seat-brain stamps ──────────
def _corp_brain(account_id, base, model, key):
    """A corporate/shared brain cfg: brain_scope='account', owner = account_id."""
    return {
        "account_id": account_id, "brain_scope": "account",
        "backend_type": "openai", "base_url": base, "model": model, "api_key": key,
    }


def _seat_brain(account_id, user_id, base, model, key):
    """A house/individual seat brain cfg: brain_scope='seat', owner = seat_user_id."""
    return {
        "account_id": account_id, "brain_scope": "seat", "seat_user_id": user_id,
        "backend_type": "openai", "base_url": base, "model": model, "api_key": key,
    }


def test_corporate_account_seats_share_one_brain_scope():
    from src.api import redis_coord
    # Two seats on one corporate account resolve the SAME shared brain cfg.
    cfg = _corp_brain("acct-helpdesk", "https://api.cerebras.ai/v1", "gpt-oss-120b", "sk-corp-AAA")
    seat_a = redis_coord.scope_for_brain(cfg)
    seat_b = redis_coord.scope_for_brain(cfg)
    assert seat_a is not None and seat_b is not None
    assert seat_a == seat_b, "two seats on one corporate brain MUST share one budget+cache"


def test_house_account_seats_with_own_brains_isolate():
    from src.api import redis_coord
    # Two individuals on the SAME "Singles" house account, each with their OWN brain.
    a = redis_coord.scope_for_brain(
        _seat_brain("acct-singles", "user-11111111", "https://api.cerebras.ai/v1",
                    "gpt-oss-120b", "sk-alice"))
    b = redis_coord.scope_for_brain(
        _seat_brain("acct-singles", "user-22222222", "https://api.openai.com/v1",
                    "gpt-4o-mini", "sk-bob"))
    assert a is not None and b is not None
    assert a != b, ("house-account seats with their OWN brains MUST NOT share a budget — "
                    "this is the leak the brain-scope exists to close (they share account_id)")


def test_house_account_seats_same_provider_different_keys_isolate():
    """The cross-customer cache-isolation guarantee: same endpoint+model, different keys."""
    from src.api import redis_coord
    a = redis_coord.scope_for_brain(
        _seat_brain("acct-singles", "user-11111111", "https://api.openai.com/v1",
                    "gpt-4o-mini", "sk-alice"))
    b = redis_coord.scope_for_brain(
        _seat_brain("acct-singles", "user-22222222", "https://api.openai.com/v1",
                    "gpt-4o-mini", "sk-bob"))
    assert a is not None and b is not None
    assert a != b, ("same public endpoint+model with DIFFERENT api_keys MUST NOT share a cache — "
                    "that would serve one customer an answer computed for another")


def test_no_brain_sentinel_and_none_yield_no_scope():
    from src.api import redis_coord
    assert redis_coord.scope_for_brain(None) is None
    assert redis_coord.scope_for_brain({"account_id": "x", "no_brain": True}) is None
    # Missing base_url/model → no routable brain → no scope.
    assert redis_coord.scope_for_brain({"account_id": "x", "base_url": "", "model": ""}) is None
    # Missing owner (placeholder) → no scope, never an unscoped key.
    assert redis_coord.scope_for_brain(
        {"brain_scope": "seat", "seat_user_id": "", "base_url": "https://x", "model": "y",
         "api_key": "k"}) is None


def test_rate_limit_key_is_brain_scoped_not_account_scoped():
    """The live fix: _resolve_rate_limit_key() now returns brain:<ident>, not account:<acct>.

    Monkeypatches the cfg reader so two house-account seats (same account_id, different
    brains) resolve to two DIFFERENT keys — proving the budget no longer leaks across them.
    """
    import src.api.llm_calls as lc

    seat_a_cfg = _seat_brain("acct-singles", "user-11111111",
                             "https://api.cerebras.ai/v1", "gpt-oss-120b", "sk-alice")
    seat_b_cfg = _seat_brain("acct-singles", "user-22222222",
                             "https://api.openai.com/v1", "gpt-4o-mini", "sk-bob")

    orig = lc._current_llm_config_or_none
    try:
        lc._current_llm_config_or_none = lambda: seat_a_cfg
        key_a = lc._resolve_rate_limit_key()
        lc._current_llm_config_or_none = lambda: seat_b_cfg
        key_b = lc._resolve_rate_limit_key()
    finally:
        lc._current_llm_config_or_none = orig

    assert key_a and key_a.startswith("brain:"), f"expected brain: key, got {key_a!r}"
    assert key_b and key_b.startswith("brain:")
    assert key_a != key_b, ("two house-account seats with own brains must draw from DIFFERENT "
                            "rate buckets — the bug was that they shared account:<acct>")


def test_foss_path_keys_by_url_when_no_brain():
    """SAAS off / no cfg → byte-for-byte FOSS: key by the env LLM_BASE_URL (url: prefix).."""
    import src.api.llm_calls as lc

    orig_cfg = lc._current_llm_config_or_none
    orig_url = os.environ.get("LLM_BASE_URL")
    try:
        lc._current_llm_config_or_none = lambda: None
        os.environ["LLM_BASE_URL"] = "https://api.example.com"
        key = lc._resolve_rate_limit_key()
    finally:
        lc._current_llm_config_or_none = orig_cfg
        if orig_url is None:
            os.environ.pop("LLM_BASE_URL", None)
        else:
            os.environ["LLM_BASE_URL"] = orig_url
    assert key == "url:https://api.example.com", f"FOSS path broke: got {key!r}"


def test_brain_scope_is_stable_across_calls():
    """The same cfg resolves to the same scope on every call (no flapping)."""
    from src.api import redis_coord
    cfg = _corp_brain("acct-x", "https://api.foo.com/v1", "m1", "k1")
    scopes = {redis_coord.scope_for_brain(cfg) for _ in range(10)}
    assert len(scopes) == 1, "scope resolution must be deterministic"
