"""SaaS endpoint/key DIVERGENCE fail-closed (Gap-B, bug BRAIN-KEY-DIVERGE).

THE BUG this pins: in SaaS mode, ``get_backend_endpoint`` and ``get_llm_headers`` each read
the per-tenant brain INDEPENDENTLY via ``current_llm_config()``. When the tenant
ContextVar/schema is LOST (a detached/background task that carries neither the brain ContextVar
nor a ``rel_type_overlay`` schema for the schema-derived fallback), the snapshot is ``None`` and
BOTH resolvers fall through to the process-global env brain (``LLM_BASE_URL`` / ``LLM_API_KEY``).

On the CPU-only SaaS host those env values are a MISMATCHED pair — a plausible base_url with the
wrong/empty key — so the call goes out to a real endpoint with a bad key → a 401 retry-storm that
hangs ingest (the "115 ENRICHMENT calls to the global default" prod symptom), and in multi-tenant
it could spend on the WRONG tenant's brain.

The old fail-closed guard ``_saas_brain_blocked`` only caught the EXPLICIT ``{"no_brain": True}``
sentinel — a ``None`` snapshot slipped past it into the env path. The fix: ``_saas_brain_unavailable``
also fail-closes on ``None`` in SaaS mode (no env-brain fallback exists in SaaS by design), so
endpoint+key can never diverge onto the env pair and the storm cannot start.

Pure unit/seam tests: DB + network seams monkeypatched, no live services.

Run: python3 tools/fltest.py --bug BRAIN-KEY-DIVERGE --test tests/test_saas_brain_divergence_failclosed.py
"""

import pytest

from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_calls
from src.api import llm_client


ACCT_T = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TENANT_SCHEMA = "faultline_tenant_d"
TENANT_CFG = {
    "account_id": ACCT_T,
    "backend_type": "openai",
    "base_url": "https://tenant-d.cerebras.example",
    "model": "tenant-d-model",
    "api_key": "tenant-d-secret",
}


@pytest.fixture(autouse=True)
def _clean_context():
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)


def _wire_account(monkeypatch, schema_to_account: dict, account_to_cfg: dict):
    monkeypatch.setattr(
        saas_llm_context, "_account_for_schema",
        lambda schema: schema_to_account.get(schema),
    )
    monkeypatch.setattr(
        saas_llm_config, "resolve_account_llm_config",
        lambda account_id: account_to_cfg.get(account_id),
    )
    # No request-path schema bound → the schema-derived fallback in current_llm_config
    # cannot resolve → a lost ContextVar genuinely yields None (the detached-task case).
    import src.api.rel_type_overlay as rto
    monkeypatch.setattr(rto, "get_current_schema", lambda: None)


def _env_mismatched_pair(monkeypatch):
    """The prod-shaped trap: env base_url looks valid, env key is the WRONG global key."""
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://global-default.example")
    monkeypatch.setenv("LLM_API_KEY", "WRONG-GLOBAL-KEY")


# ════════════════════════════════════════════════════════════════════════════════════
# 1 — the divergence case: SaaS on, ContextVar LOST (None) → fail-closed, ZERO network
# ════════════════════════════════════════════════════════════════════════════════════

def test_lost_context_in_saas_fails_closed_no_env_call(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    _env_mismatched_pair(monkeypatch)
    _wire_account(monkeypatch, {TENANT_SCHEMA: ACCT_T}, {ACCT_T: TENANT_CFG})

    # No brain bound and no schema → snapshot is None (the detached-task state).
    assert saas_llm_context.current_llm_config() is None

    # The OLD sentinel-only guard MISSED this (proving the hole existed)…
    assert llm_calls._saas_brain_blocked() is False
    # …the new guard catches it.
    assert llm_calls._saas_brain_unavailable() is True

    # Any network path for a lost-context SaaS call must FAIL the test.
    def _boom(*a, **k):
        raise AssertionError("divergence breached: env brain reached on a lost-context SaaS call")

    monkeypatch.setattr(llm_calls, "_get_endpoint_list", _boom)

    result = llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "user", "content": "what is a router"}],
        model="global-enrichment-model",
        user_id="detached-task",
        operation="ENRICHMENT",
    )
    assert result == {}  # safe empty default — no spend, no 401 storm, no env pair


# ════════════════════════════════════════════════════════════════════════════════════
# 2 — bound tenant brain: endpoint AND key resolve from the SAME snapshot (no divergence)
# ════════════════════════════════════════════════════════════════════════════════════

def test_bound_tenant_brain_endpoint_and_key_agree(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    _env_mismatched_pair(monkeypatch)
    _wire_account(monkeypatch, {TENANT_SCHEMA: ACCT_T}, {ACCT_T: TENANT_CFG})

    # Bind the tenant brain explicitly (the request-path state).
    saas_llm_context._current_llm_config.set(TENANT_CFG)

    assert llm_calls._saas_brain_unavailable() is False
    # Endpoint is the tenant's, NOT the env default.
    assert llm_client.get_backend_endpoint() == "https://tenant-d.cerebras.example/v1/chat/completions"
    # Key is the tenant's, NOT the WRONG global key — the atomic pair.
    assert llm_client.get_llm_headers().get("Authorization") == "Bearer tenant-d-secret"


# ════════════════════════════════════════════════════════════════════════════════════
# 3 — SAAS off: None context is the legitimate global env path — unchanged, NOT blocked
# ════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_none_context_uses_env_unchanged(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("LLM_BACKEND_TYPE", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("LLM_API_KEY", "env-key")

    assert saas_llm_context.current_llm_config() is None
    assert llm_calls._saas_brain_unavailable() is False  # off → never blocks
    assert llm_client.get_backend_endpoint() == "https://env.example.com/v1/chat/completions"
    assert llm_client.get_llm_headers().get("Authorization") == "Bearer env-key"
