"""The endpoint FAMILY — chat · model-discovery · embeddings · health — must agree.

WHY THIS FILE EXISTS (ENDPOINTS, 2026-08-13). ``tests/test_llm_backend_endpoint_resolution.py``
fixed ONE site (chat). The same "we hardcode a version and append it blind" defect lives at
three more, and they are *silently* worse than the chat one:

  * ``saas/llm_config.list_remote_models``  builds ``{base}/v1/models``  → the console's
    "fetch models" picker 404s for a versioned base.
  * ``llm_client.get_embedding_url``        builds ``{base}/v1/embeddings``.
  * ``llm_client.get_health_check_url``     derives by STRING-REPLACING ``/v1/chat/completions``.
    With a versioned base that substring is absent, so the probe GETs the CHAT url — and since
    anything short of an exception reads as "up", the health check silently degrades into a
    reachability check that proves nothing.

PART 1 was written and run FIRST against UNMODIFIED code, so it pins REAL behaviour — including
behaviour that is WRONG (marked ``# ⚠️ WRONG``); those are the pins that flip in Part 2, and each
one names the URL the provider actually serves.

No network, no DB — this resolves strings only.

Run: python3 tools/fltest.py --bug ENDPOINTS --test tests/test_llm_endpoint_family_resolution.py
     (tests/ is gitignored → git add -f)
"""

import pytest

from src.api import llm_client


# ── the three shapes a real tenant pastes ─────────────────────────────────────────────
#
# DASHSCOPE is LIVE ON PRODUCTION RIGHT NOW (account 95aa8183…, backend_type=openai,
# model qwen-turbo) — every assertion about it is a regression pin, not a wish.
# ZAI_FULL is also live (account d0fa70b1…, backend_type=raw) — the owner's z.ai brain,
# stored as the FULL endpoint after the versioned-base attempt 404'd.
DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/"
DASHSCOPE_CHAT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"

ZAI_BASE = "https://api.z.ai/api/paas/v4"                       # version in the BASE
ZAI_FULL = "https://api.z.ai/api/paas/v4/chat/completions"      # the FULL endpoint
ZAI_CHAT = "https://api.z.ai/api/paas/v4/chat/completions"

CEREBRAS_BASE = "https://api.cerebras.ai"
OWUI_BASE = "http://open-webui:8080"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test takes the ENV path unless it explicitly binds a tenant brain."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("EMBEDDING_API_URL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(llm_client, "_tenant_brain", lambda: None)


def _emb(monkeypatch, backend_type, base_url, chat_url):
    monkeypatch.setenv("LLM_BACKEND_TYPE", backend_type)
    if base_url is None:
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LLM_BASE_URL", base_url)
    return llm_client.get_embedding_url(chat_url)


def _health(monkeypatch, backend_type, chat_url):
    monkeypatch.setenv("LLM_BACKEND_TYPE", backend_type)
    return llm_client.get_health_check_url(chat_url)


def _models(base_url):
    """The URL ``saas.llm_config.list_remote_models`` would GET for an OpenAI-compat backend.

    Asserted through the module's own helpers rather than by monkeypatching httpx, so the test
    stays a pure string assertion with NO network anywhere near it.
    """
    from saas import llm_config as lc
    if hasattr(lc, "_models_discovery_url"):          # after the fix
        return lc._models_discovery_url(base_url)
    return f"{lc._normalize_base_for_models(base_url)}/v1/models"   # before the fix


# ══════════════════════════════════════════════════════════════════════════════════════
# PART 1 — PINNED BEFORE THE CHANGE. Every one of these passed on unmodified code.
# ══════════════════════════════════════════════════════════════════════════════════════

# ── model discovery ───────────────────────────────────────────────────────────────────

def test_pin_models_dashscope():
    """LIVE ON PROD. ``compatible-mode`` is not a version → the /v1 is correctly appended."""
    assert _models(DASHSCOPE_BASE) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models")


def test_pin_models_cerebras_bare_host():
    assert _models(CEREBRAS_BASE) == "https://api.cerebras.ai/v1/models"


def test_pin_models_openai_base_with_v1_is_normalized():
    """The ONE case the existing ``_normalize_base_for_models`` already handled: a trailing
    ``/v1`` is stripped before the append, so this does NOT double."""
    assert _models("https://api.openai.com/v1") == "https://api.openai.com/v1/models"


# ── embeddings ────────────────────────────────────────────────────────────────────────

def test_pin_embeddings_openwebui(monkeypatch):
    assert _emb(monkeypatch, "openwebui", OWUI_BASE,
                OWUI_BASE + "/api/chat/completions") == OWUI_BASE + "/api/embeddings"


def test_pin_embeddings_dashscope(monkeypatch):
    """LIVE ON PROD."""
    assert _emb(monkeypatch, "openai", DASHSCOPE_BASE, DASHSCOPE_CHAT) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/embeddings")


def test_pin_embeddings_anthropic_has_none(monkeypatch):
    assert _emb(monkeypatch, "anthropic", "https://api.anthropic.com",
                "https://api.anthropic.com/v1/messages") == ""


def test_pin_embeddings_explicit_env_always_wins(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_URL", "http://nomic:8080/embed")
    assert _emb(monkeypatch, "openai", ZAI_BASE, ZAI_CHAT) == "http://nomic:8080/embed"


def test_pin_embeddings_legacy_derivation_from_chat_url(monkeypatch):
    """No LLM_BASE_URL at all → derived from the chat URL by substring replacement."""
    assert _emb(monkeypatch, "openai", None,
                "https://api.openai.com/v1/chat/completions") == (
        "https://api.openai.com/v1/embeddings")


def test_pin_embeddings_legacy_derivation_groq(monkeypatch):
    assert _emb(monkeypatch, "groq", None,
                "https://api.groq.com/openai/v1/chat/completions") == (
        "https://api.groq.com/openai/v1/embeddings")


# ── health check ──────────────────────────────────────────────────────────────────────

def test_pin_health_openwebui(monkeypatch):
    assert _health(monkeypatch, "openwebui", OWUI_BASE + "/api/chat/completions") == (
        OWUI_BASE + "/api/version")


def test_pin_health_dashscope(monkeypatch):
    """LIVE ON PROD."""
    assert _health(monkeypatch, "openai", DASHSCOPE_CHAT) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models")


def test_pin_health_openai(monkeypatch):
    assert _health(monkeypatch, "openai", "https://api.openai.com/v1/chat/completions") == (
        "https://api.openai.com/v1/models")


def test_pin_health_groq_keeps_its_openai_prefix(monkeypatch):
    assert _health(monkeypatch, "groq",
                   "https://api.groq.com/openai/v1/chat/completions") == (
        "https://api.groq.com/openai/v1/models")


def test_pin_health_anthropic_from_base(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com")
    assert _health(monkeypatch, "anthropic", "https://api.anthropic.com/v1/messages") == (
        "https://api.anthropic.com/v1/models")


def test_pin_health_saas_with_no_brain_probes_nothing(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    assert _health(monkeypatch, "openai", "") == ""


# ══════════════════════════════════════════════════════════════════════════════════════
# PART 2 — NEW BEHAVIOUR (ENDPOINTS). RED on unmodified code.
#
# The family must AGREE. Every URL below derives from the same API root as the chat URL,
# through the one ``llm_client.resolve_endpoint`` join.
# ══════════════════════════════════════════════════════════════════════════════════════

def _chat(monkeypatch, backend_type, base_url):
    monkeypatch.setenv("LLM_BACKEND_TYPE", backend_type)
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    return llm_client.get_backend_endpoint()


# ── shape 3: the tenant pasted the FULL endpoint ──────────────────────────────────────

def test_full_endpoint_pasted_as_base_is_not_extended(monkeypatch):
    """THE ESCALATION. Blocked by ``…/v4/v1/chat/completions``, the natural next move is to
    paste the full endpoint — which used to give
    ``…/v4/chat/completions/v1/chat/completions``. Measured on prod before the fix."""
    assert _chat(monkeypatch, "openai", ZAI_FULL) == ZAI_CHAT


def test_full_endpoint_pasted_as_base_resolution_is_idempotent(monkeypatch):
    """resolve(resolve(x)) == resolve(x) — a resolved URL fed back in as a base is stable,
    which is exactly what happens when a tenant copies what the console showed them."""
    once = _chat(monkeypatch, "openai", ZAI_BASE)
    assert _chat(monkeypatch, "openai", once) == once


def test_raw_full_endpoint_chat_still_verbatim(monkeypatch):
    """``raw`` is the escape hatch: the base IS the URL, never rewritten. This is the
    config LIVE ON PROD for account d0fa70b1…"""
    assert _chat(monkeypatch, "raw", ZAI_FULL) == ZAI_FULL


# ── the family agrees, for all three shapes ───────────────────────────────────────────

@pytest.mark.parametrize("backend_type,base", [
    ("openai", ZAI_BASE),   # shape 1 — version in the base
    ("openai", ZAI_FULL),   # shape 3 — the full endpoint
    ("raw",    ZAI_FULL),   # shape 3 via the escape hatch (LIVE ON PROD)
])
def test_zai_family_agrees(monkeypatch, backend_type, base):
    chat = _chat(monkeypatch, backend_type, base)
    assert chat == ZAI_CHAT
    assert _models(base) == "https://api.z.ai/api/paas/v4/models"
    assert _emb(monkeypatch, backend_type, base, chat) == (
        "https://api.z.ai/api/paas/v4/embeddings")
    health = _health(monkeypatch, backend_type, chat)
    assert health == "https://api.z.ai/api/paas/v4/models"
    # THE SILENT DEGRADATION: before the fix the health probe GET the CHAT url, and any
    # non-exception read as "up".
    assert health != chat


@pytest.mark.parametrize("base", [DASHSCOPE_BASE,
                                  "https://dashscope-intl.aliyuncs.com/compatible-mode"])
def test_dashscope_family_unchanged(monkeypatch, base):
    """LIVE ON PROD — must come out byte-identical to the pre-fix values."""
    chat = _chat(monkeypatch, "openai", base)
    assert chat == DASHSCOPE_CHAT
    assert _models(base) == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models"
    assert _emb(monkeypatch, "openai", base, chat) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/embeddings")
    assert _health(monkeypatch, "openai", chat) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models")


def test_dashscope_full_endpoint_paste_also_resolves(monkeypatch):
    assert _chat(monkeypatch, "openai", DASHSCOPE_CHAT) == DASHSCOPE_CHAT


def test_cerebras_family_unchanged(monkeypatch):
    chat = _chat(monkeypatch, "openai", CEREBRAS_BASE)
    assert chat == "https://api.cerebras.ai/v1/chat/completions"
    assert _models(CEREBRAS_BASE) == "https://api.cerebras.ai/v1/models"
    assert _emb(monkeypatch, "openai", CEREBRAS_BASE, chat) == (
        "https://api.cerebras.ai/v1/embeddings")
    assert _health(monkeypatch, "openai", chat) == "https://api.cerebras.ai/v1/models"


def test_openwebui_family_unchanged(monkeypatch):
    """FOSS-local backend — offered to a self-hoster, no longer offered to a SaaS tenant,
    and byte-for-byte unchanged either way."""
    chat = _chat(monkeypatch, "openwebui", OWUI_BASE)
    assert chat == OWUI_BASE + "/api/chat/completions"
    assert _emb(monkeypatch, "openwebui", OWUI_BASE, chat) == OWUI_BASE + "/api/embeddings"
    assert _health(monkeypatch, "openwebui", chat) == OWUI_BASE + "/api/version"


def test_openwebui_full_endpoint_paste_does_not_double(monkeypatch):
    assert _chat(monkeypatch, "openwebui", OWUI_BASE + "/api/chat/completions") == (
        OWUI_BASE + "/api/chat/completions")


@pytest.mark.parametrize("backend_type,base,expected", [
    ("ollama",    "http://localhost:11434", "http://localhost:11434/v1/chat/completions"),
    ("lm_studio", "http://192.168.1.20:1234",
     "http://192.168.1.20:1234/v1/chat/completions"),
    ("localai",   "http://localai:8080", "http://localai:8080/v1/chat/completions"),
])
def test_foss_local_backends_resolve_exactly_as_today(monkeypatch, backend_type, base,
                                                      expected):
    """The open-core build's PRIMARY backends. Dropping them from the SaaS dropdown must
    not change one byte of how they resolve."""
    assert _chat(monkeypatch, backend_type, base) == expected


def test_scheme_less_host_port_base_round_trips(monkeypatch):
    """A base with no scheme must not be mangled by URL parsing (``localhost`` would
    otherwise be read as a URL scheme)."""
    assert _chat(monkeypatch, "lm_studio", "192.168.1.20:1234") == (
        "192.168.1.20:1234/v1/chat/completions")


def test_groq_openai_prefixed_base_no_longer_doubles(monkeypatch):
    """``https://api.groq.com/openai/v1`` overlaps the suffix's LEADING ``openai/v1``."""
    assert _chat(monkeypatch, "groq", "https://api.groq.com/openai/v1") == (
        "https://api.groq.com/openai/v1/chat/completions")


def test_groq_bare_host_unchanged(monkeypatch):
    assert _chat(monkeypatch, "groq", "https://api.groq.com") == (
        "https://api.groq.com/openai/v1/chat/completions")


def test_query_string_base_keeps_the_query_at_the_end(monkeypatch):
    """Azure-style ``?api-version=…``. Plain concatenation put the path AFTER the query."""
    assert _chat(monkeypatch, "openai",
                 "https://x.openai.azure.com/openai?api-version=2024-02-01") == (
        "https://x.openai.azure.com/openai/v1/chat/completions?api-version=2024-02-01")


def test_health_is_never_the_chat_url_for_a_versioned_base(monkeypatch):
    """The degradation signature, asserted directly."""
    chat = _chat(monkeypatch, "openai", ZAI_BASE)
    assert _health(monkeypatch, "openai", chat) != chat


def test_models_url_for_a_full_endpoint_base():
    assert _models(ZAI_FULL) == "https://api.z.ai/api/paas/v4/models"


def test_models_url_openai_full_endpoint():
    assert _models("https://api.openai.com/v1/chat/completions") == (
        "https://api.openai.com/v1/models")


# ── the SaaS OFFERING (console dropdowns + server-side accept-list) ───────────────────
#
# The owner's ruling: ollama / openwebui / lm_studio / localai are LOCAL endpoints and do not
# belong in a hosted offering. They must stay RESOLVABLE (the open-core build runs on them —
# see test_foss_local_backends_resolve_exactly_as_today) and merely stop being OFFERED.

def test_local_backends_are_still_resolvable_in_the_core():
    from src.api.llm_client import _BACKEND_PATHS
    for local in ("ollama", "openwebui", "lm_studio", "localai"):
        assert local in _BACKEND_PATHS, f"{local} must stay in the core — FOSS runs on it"


def test_saas_offering_excludes_local_only_backends():
    from saas.llm_config import SAAS_OFFERED_BACKENDS
    for local in ("ollama", "openwebui", "lm_studio", "localai"):
        assert local not in SAAS_OFFERED_BACKENDS


def test_saas_offering_includes_the_public_providers_and_the_escape_hatch():
    from saas.llm_config import SAAS_OFFERED_BACKENDS
    for public in ("openai", "anthropic", "groq"):
        assert public in SAAS_OFFERED_BACKENDS
    # ``raw`` is the general escape hatch for a provider whose URL shape we did not
    # anticipate. It existed in code and was invisible in the UI, which is precisely why a
    # working config was unreachable by trial and error.
    assert "raw" in SAAS_OFFERED_BACKENDS


def test_every_offered_backend_is_a_real_backend_type():
    from saas.llm_config import SAAS_OFFERED_BACKENDS
    from src.api.llm_client import _BACKEND_PATHS
    assert SAAS_OFFERED_BACKENDS <= set(_BACKEND_PATHS)


def test_save_door_rejects_a_local_only_backend_type():
    """Server-side, not just the dropdown — the accept-list is the enforcement."""
    from fastapi import HTTPException
    from saas.llm_admin import LLMConfigRequest, build_llm_config_fields
    with pytest.raises(HTTPException) as exc:
        build_llm_config_fields(LLMConfigRequest(backend_type="lm_studio",
                                                 base_url="http://192.168.1.20:1234"))
    assert exc.value.status_code == 400
    detail = str(exc.value.detail).lower()
    assert "lm_studio" in detail
    assert "raw" in detail or "exact url" in detail, "the message must name the way out"


@pytest.mark.parametrize("backend_type", ["openai", "anthropic", "groq", "raw"])
def test_save_door_accepts_every_offered_backend_type(backend_type):
    from saas.llm_admin import LLMConfigRequest, build_llm_config_fields
    fields = build_llm_config_fields(LLMConfigRequest(backend_type=backend_type,
                                                      base_url="https://api.example.com"))
    assert fields["backend_type"] == backend_type


def test_save_door_ignores_backend_type_when_not_being_changed():
    """A patch that touches only the model must not be rejected for a field it did not send."""
    from saas.llm_admin import LLMConfigRequest, build_llm_config_fields
    fields = build_llm_config_fields(LLMConfigRequest(model="glm-4.5-air"))
    assert fields == {"model": "glm-4.5-air"}


# ══════════════════════════════════════════════════════════════════════════════════════
# GEMINI — the one provider whose chat and model-list prefixes DISAGREE
# ══════════════════════════════════════════════════════════════════════════════════════
# Every other backend here obeys one rule: keep the version prefix, swap the operation.
# Google does not. Chat is OpenAI-compatible under /v1beta/openai/, but the model list is
# NOT its sibling — /v1beta/openai/models is a hard 404 and the list lives at the native
# /v1beta/models. Measured against the live API, not inferred:
#     POST /v1beta/openai/chat/completions -> 400 "model is not specified"  (exists)
#     GET  /v1beta/openai/models           -> 404                          (does not)
#     GET  /v1beta/models                  -> 403                          (exists, needs key)
# So _models_suffix_for carries an explicit override. These pins exist because deleting that
# override leaves every test above green while the tenant gets a working brain whose model
# picker 404s — the exact Groq defect the family join was introduced to kill, reintroduced
# for a different vendor.

def test_pin_gemini_chat_path_is_the_openai_compat_prefix():
    assert llm_client._BACKEND_PATHS["gemini"] == "/v1beta/openai/chat/completions"


def test_pin_gemini_model_list_is_NOT_a_sibling_of_its_chat_path():
    """The override, stated as the thing it prevents: a derived suffix would be
    /v1beta/openai/models, which Google 404s."""
    from saas.llm_config import _models_suffix_for
    assert _models_suffix_for("gemini") == "/v1beta/models"
    assert _models_suffix_for("gemini") != "/v1beta/openai/models"


def test_pin_gemini_discovery_url_from_a_bare_google_host():
    from saas.llm_config import _models_discovery_url
    assert _models_discovery_url(
        "https://generativelanguage.googleapis.com", "gemini"
    ) == "https://generativelanguage.googleapis.com/v1beta/models"


def test_pin_gemini_is_offered_in_the_hosted_console():
    """It has to be SELECTABLE, or a tenant lands on 'OpenAI Compatible' and builds a URL
    Google does not serve — which is the reported defect."""
    from saas.llm_config import SAAS_OFFERED_BACKENDS
    assert "gemini" in SAAS_OFFERED_BACKENDS


def test_pin_the_openai_backend_still_does_not_work_for_google():
    """Guards against 'simplifying' the override away by making gemini an alias of openai."""
    from saas.llm_config import _models_suffix_for
    assert _models_suffix_for("openai") == "/v1/models"
    assert llm_client._BACKEND_PATHS["openai"] == "/v1/chat/completions"


def test_the_js_preview_mirror_matches_the_engine():
    """saas/dashboard/endpoint_preview.js hand-copies _BACKEND_PATHS, and it drifted.

    `gemini` shipped in the Python table and the dropdown but not in the JS mirror, whose
    resolveChat() falls back to /v1/chat/completions for an unknown key — so the panel told the
    tenant "FaultLine will call .../v1/chat/completions" while the engine called
    .../v1beta/openai/chat/completions. A WRONG preview is worse than none: it is the surface a
    tenant debugs with, so it sent them to fix a URL that was already correct.
    """
    import re
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1]
          / "saas" / "dashboard" / "endpoint_preview.js").read_text(encoding="utf-8")
    block = js[js.index("var BACKEND_PATHS = {"):]
    block = block[:block.index("};")]
    mirror = dict(re.findall(r"(\w+)\s*:\s*'([^']*)'", block))
    assert mirror, "could not parse the JS mirror — the test must fail loudly, not vacuously"
    for backend, path in llm_client._BACKEND_PATHS.items():
        assert backend in mirror, (
            f"backend {backend!r} exists in the engine but NOT in the console's preview mirror; "
            f"the panel will silently show the /v1 fallback and misdirect the tenant")
        assert mirror[backend] == path, (
            f"preview mirror disagrees with the engine for {backend!r}: "
            f"{mirror[backend]!r} != {path!r}")
