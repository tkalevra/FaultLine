"""Gap A — the SaaS GLOBAL-BRAIN FIREWALL, pinned at the RESOLVERS.

OWNER RULING (binding, 2026-08-01): "That brain, the Cerebras brain, is moot and shouldn't be
relied on for SaaS at all. That global var shouldn't be used at all."

WHAT WAS ALREADY CORRECT (do not re-litigate): the three centralized entry points in
``src/api/llm_calls`` already fail closed via ``_saas_brain_unavailable`` — see
``tests/test_saas_brain_divergence_failclosed.py``. The hand-rolled sites in ``main.py``
(/learn, its operational seed, taxonomy discovery) already call the same guard.

WHAT WAS NOT: the RESOLVERS in ``src/api/llm_client`` still handed the process-global env
brain to ANYONE who asked, in SaaS mode, with no tenant bound:

    get_backend_endpoint()  → $LLM_BASE_URL
    get_endpoint_list()     → $OPENWEBUI_INTERNAL_URL → $OPENWEBUI_URL → $QWEN_API_URL
                              → HARDCODED fallbacks
    get_llm_chat_url()      → HARDCODED "http://open-webui:8080/api/chat/completions"
    get_llm_headers()       → $LLM_API_KEY
    get_embedding_url()     → $LLM_BASE_URL/…

So the invariant lived in the CALL SITES, and every one of them had to remember. That is the
"guard covers 4 of 5 call sites" shape this project has shipped before; two sites did in fact
NOT remember (``LLMOutputValidator._embed_text`` via the WGM gate, and the re_embedder's
process-start endpoint + ``embed_text`` external fallback).

These tests pin the invariant where it now lives — INSIDE the resolvers. The canary check is
the important one: with SAAS_MODE on and no tenant brain bound, NO env value may appear in ANY
resolver's output, by any route.

SAAS_MODE off must be byte-for-byte unchanged, including the legacy priority chain and the
hardcoded dev fallbacks. That is pinned too.

Pure unit/seam tests: no DB, no network, no live services.

Run: python3 tools/fltest.py --bug GAPA --test tests/test_saas_global_brain_firewall.py
"""

import pytest

from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_calls
from src.api import llm_client


ACCT = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

TENANT_CFG = {
    "account_id": ACCT,
    "backend_type": "openai",
    "base_url": "https://tenant-own-brain.example",
    "model": "tenant-own-model",
    "api_key": "tenant-own-secret",
}

# Every env value the ruling names. If ANY of these strings reaches a resolver's output in
# SaaS mode with no tenant brain bound, the firewall is breached.
ENV_CANARIES = {
    "LLM_BASE_URL": "https://api.cerebras.ai",          # the measured pre-prod hole
    "LLM_API_KEY": "OPERATOR-GLOBAL-KEY",
    "OPENWEBUI_INTERNAL_URL": "http://open-webui-internal.canary:8080",
    "OPENWEBUI_URL": "http://open-webui-external.canary:3000",
    "QWEN_API_URL": "http://qwen.canary:11434/v1/chat/completions",
    "WGM_LLM_MODEL": "operator-global-model",
}


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    """No tenant brain bound, and no schema-derived fallback available."""
    import src.api.rel_type_overlay as rto
    monkeypatch.setattr(rto, "get_current_schema", lambda: None)
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    llm_client._global_brain_crit_last.clear()
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    llm_client._global_brain_crit_last.clear()


def _set_env_canaries(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    for k, v in ENV_CANARIES.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMBEDDING_API_URL", raising=False)


def _saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")


def _saas_off(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)


# ════════════════════════════════════════════════════════════════════════════════════
# 1 — THE CANARY: SaaS on, nothing bound ⇒ no env value escapes ANY resolver
# ════════════════════════════════════════════════════════════════════════════════════

def test_no_env_brain_value_escapes_any_resolver_in_saas(monkeypatch):
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)

    assert llm_client.saas_global_brain_blocked() is True

    chat = llm_client.get_llm_chat_url()
    outputs = [
        repr(llm_client.get_backend_endpoint()),
        repr(llm_client.get_endpoint_list()),
        repr(chat),
        repr(llm_client.get_llm_headers()),
        repr(llm_client.get_embedding_url(chat)),
        repr(llm_client.get_health_check_url(chat)),
        repr(llm_client.get_llm_config()),
        repr(llm_calls._get_endpoint_list()),
    ]
    blob = " || ".join(outputs)
    for name, value in ENV_CANARIES.items():
        assert value not in blob, f"{name} leaked into a resolver output in SaaS mode: {blob}"


def test_resolvers_are_empty_not_merely_non_canary(monkeypatch):
    """Empty is the contract — a caller must be able to detect 'do not call'."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)

    assert llm_client.get_backend_endpoint() is None
    assert llm_client.get_endpoint_list() == []
    assert llm_client.get_llm_chat_url() == ""
    assert llm_client.get_llm_headers() == {}
    assert llm_client.get_embedding_url("") == ""
    assert llm_client.get_health_check_url("") == ""
    assert llm_calls._get_endpoint_list() == []


def test_hardcoded_dev_fallbacks_are_never_served_to_a_saas_tenant(monkeypatch):
    """With NO env brain at all, the open-core path serves hardcoded dev defaults. SaaS must not."""
    _saas_on(monkeypatch)
    for k in list(ENV_CANARIES) + ["LLM_BACKEND_TYPE", "EMBEDDING_API_URL"]:
        monkeypatch.delenv(k, raising=False)

    assert llm_client.get_endpoint_list() == []
    assert llm_client.get_llm_chat_url() == ""
    assert "open-webui" not in repr(llm_client.get_endpoint_list())


def test_no_brain_sentinel_is_blocked_exactly_like_an_unbound_context(monkeypatch):
    """A REAL account with no configured brain — the 9-of-15 prod population."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)
    saas_llm_context._current_llm_config.set(saas_llm_context._no_brain_sentinel(ACCT))

    assert llm_client.saas_global_brain_blocked() is True
    assert llm_client.get_backend_endpoint() is None
    assert llm_client.get_endpoint_list() == []
    assert llm_client.get_llm_headers() == {}
    # and the centralized stack still agrees
    assert llm_calls._saas_brain_unavailable() is True


def test_a_truthy_config_without_a_base_url_is_still_blocked(monkeypatch):
    """Belt-and-braces: saas.llm_config._build_config refuses to emit such a row, but the
    firewall must not DEPEND on that refusal — a partially-populated cfg is not routable."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)
    saas_llm_context._current_llm_config.set(
        {"account_id": ACCT, "model": "m", "api_key": "k"}  # no base_url
    )

    assert llm_client.saas_global_brain_blocked() is True
    assert llm_client.get_backend_endpoint() is None
    assert llm_client.get_llm_headers() == {}


# ════════════════════════════════════════════════════════════════════════════════════
# 2 — a BOUND tenant brain still works, end to end, and beats the env
# ════════════════════════════════════════════════════════════════════════════════════

def test_bound_tenant_brain_wins_and_env_is_ignored(monkeypatch):
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)
    saas_llm_context._current_llm_config.set(TENANT_CFG)

    assert llm_client.saas_global_brain_blocked() is False
    assert llm_client.get_backend_endpoint() == (
        "https://tenant-own-brain.example/v1/chat/completions"
    )
    assert llm_client.get_endpoint_list() == [
        "https://tenant-own-brain.example/v1/chat/completions"
    ]
    assert llm_client.get_llm_headers() == {"Authorization": "Bearer tenant-own-secret"}
    chat = llm_client.get_llm_chat_url()
    assert llm_client.get_embedding_url(chat) == "https://tenant-own-brain.example/v1/embeddings"

    cfg = llm_client.get_llm_config()
    assert cfg["endpoint_source"] == "saas_tenant_brain"
    assert cfg["model"] == "tenant-own-model"
    assert ENV_CANARIES["WGM_LLM_MODEL"] not in repr(cfg)


def test_health_diagnostic_no_longer_reports_the_env_lane_in_saas(monkeypatch):
    """The measured evidence: prod (SAAS_MODE=1) self-reported endpoint_source "fallback" +
    the operator's WGM_LLM_MODEL — describing a lane no tenant call can reach. That report
    sent an investigation at a phantom. /health must describe the lane that is REAL."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)

    cfg = llm_client.get_llm_config()
    assert cfg["endpoint_source"] == "saas_no_tenant_brain_bound"
    assert cfg["saas_mode"] is True
    assert cfg["api_key_set"] is False
    assert ENV_CANARIES["WGM_LLM_MODEL"] not in repr(cfg)
    assert ENV_CANARIES["LLM_BASE_URL"] not in repr(cfg)


# ════════════════════════════════════════════════════════════════════════════════════
# 3 — ZERO NETWORK on the two hand-rolled lanes that never had the guard
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_validator_embed_makes_zero_network_call_in_saas_with_no_brain(monkeypatch):
    """LLMOutputValidator._embed_text is reached from the WGM gate and bypasses the
    centralized stack entirely. Before the fix it POSTed to the operator's endpoint with
    the operator's key."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)

    import httpx
    from src.api.llm_output_validator import LLMOutputValidator

    def _boom(*a, **k):
        raise AssertionError("firewall breached: validator embedded against the global brain")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)

    v = LLMOutputValidator(db_conn=None, llm_endpoint=ENV_CANARIES["QWEN_API_URL"])
    assert await v._embed_text("router") is None


def test_reembedder_embed_text_makes_zero_network_call_without_an_endpoint(monkeypatch):
    """embed_text's external fallback used to POST to a URL derived from the operator's
    process-start endpoint. With no endpoint it must skip the call outright, not POST to ""."""
    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)

    from src.re_embedder import embedder

    monkeypatch.setattr(embedder, "_get_local_embedder", lambda: None)

    def _boom(*a, **k):
        raise AssertionError("firewall breached: embed_text called the global endpoint")

    monkeypatch.setattr(embedder, "_http_client_sync", None)
    monkeypatch.setattr(embedder, "_http_client", type("X", (), {"post": staticmethod(_boom)}))

    assert embedder.embed_text("hello", "", fallback=False) is None
    vec = embedder.embed_text("hello", "", fallback=True)
    assert vec is not None and len(vec) == 768  # hash vector, no network


def test_the_orphan_second_legacy_chain_in_main_is_firewalled_too(monkeypatch):
    """``main._resolve_llm_endpoint`` has NO callers (the comment above its def even claims it
    was deleted) but it still HAND-BUILDS a second copy of the legacy chain plus a hardcoded
    open-webui default, outside llm_client. Wiring it up in SaaS would silently reopen this
    exact hole, so it inherits the firewall. SAAS off it is unchanged."""
    main = pytest.importorskip("src.api.main")

    _saas_on(monkeypatch)
    _set_env_canaries(monkeypatch)
    assert main._resolve_llm_endpoint(with_fallback=True) == []

    _saas_off(monkeypatch)
    chain = main._resolve_llm_endpoint(with_fallback=True)
    assert chain and any(ENV_CANARIES["OPENWEBUI_INTERNAL_URL"] in u for u in chain)


# ════════════════════════════════════════════════════════════════════════════════════
# 4 — SAAS OFF is byte-for-byte the open-core path (the whole legacy chain, in order)
# ════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_typed_backend_unchanged(monkeypatch):
    _saas_off(monkeypatch)
    _set_env_canaries(monkeypatch)

    assert llm_client.saas_global_brain_blocked() is False
    assert llm_client.get_backend_endpoint() == "https://api.cerebras.ai/api/chat/completions"
    assert llm_client.get_llm_headers() == {"Authorization": "Bearer OPERATOR-GLOBAL-KEY"}
    assert llm_client.get_llm_config()["endpoint_source"] == "OPENWEBUI_INTERNAL_URL"
    assert llm_client.get_llm_config()["model"] == "operator-global-model"


def test_saas_off_legacy_chain_priority_unchanged(monkeypatch):
    """OPENWEBUI_INTERNAL_URL → OPENWEBUI_URL → QWEN_API_URL, only when LLM_BASE_URL is unset."""
    _saas_off(monkeypatch)
    _set_env_canaries(monkeypatch)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    assert llm_client.get_endpoint_list() == [
        "http://open-webui-internal.canary:8080/api/chat/completions",
        "http://open-webui-external.canary:3000/api/chat/completions",
        "http://qwen.canary:11434/v1/chat/completions",
    ]


def test_saas_off_hardcoded_dev_fallbacks_unchanged(monkeypatch):
    _saas_off(monkeypatch)
    for k in list(ENV_CANARIES) + ["LLM_BACKEND_TYPE", "EMBEDDING_API_URL"]:
        monkeypatch.delenv(k, raising=False)

    assert llm_client.get_endpoint_list() == [
        "http://open-webui:8080/api/chat/completions",
        "http://localhost:8080/api/chat/completions",
        "http://localhost:11434/v1/chat/completions",
    ]
    assert llm_client.get_llm_chat_url() == "http://open-webui:8080/api/chat/completions"


def test_saas_off_embedding_and_health_derivation_unchanged(monkeypatch):
    _saas_off(monkeypatch)
    _set_env_canaries(monkeypatch)

    chat = llm_client.get_llm_chat_url()
    assert chat == "https://api.cerebras.ai/api/chat/completions"
    assert llm_client.get_embedding_url(chat) == "https://api.cerebras.ai/api/embeddings"
    assert llm_client.get_health_check_url(chat) == "https://api.cerebras.ai/api/version"
