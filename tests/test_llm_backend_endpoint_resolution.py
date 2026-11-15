"""Backend endpoint URL resolution — ``llm_client.get_backend_endpoint``.

WHY THIS FILE EXISTS (ZAIURL, 2026-08-13): a tenant pointed their BYO brain at z.ai
(GLM), whose base URL carries its OWN version segment:

    backend_type=openai   base_url=https://api.z.ai/api/paas/v4

``_BACKEND_PATHS["openai"] == "/v1/chat/completions"`` is appended unconditionally, so
every call went to ``https://api.z.ai/api/paas/v4/v1/chat/completions`` — note BOTH ``v4``
AND ``v1`` — and 404'd (observed on production: ENRICHMENT / CLASSIFY_CHAIN, 3 attempts,
all three HelpDeskPro seats).

Part 1 of this file PINS the two shapes that are LIVE ON PRODUCTION RIGHT NOW plus every
other entry in ``_BACKEND_PATHS``. Part 1 was written and run FIRST, against unmodified
code, so it pins REAL behaviour rather than the behaviour of the fix. Part 2 adds the new
behaviour.

No network, no DB: this resolves strings only.

Run: python3 tools/fltest.py --bug ZAIURL --test tests/test_llm_backend_endpoint_resolution.py
     (tests/ is gitignored → git add -f)
"""

import pytest

from src.api import llm_client


# ── helpers ───────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_tenant_brain(monkeypatch):
    """Default every test to the ENV path (no SaaS ContextVar bound)."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(llm_client, "_tenant_brain", lambda: None)


def _env_endpoint(monkeypatch, backend_type: str, base_url: str) -> str | None:
    monkeypatch.setenv("LLM_BACKEND_TYPE", backend_type)
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    return llm_client.get_backend_endpoint()


def _tenant_endpoint(monkeypatch, backend_type: str, base_url: str) -> str | None:
    """The path production actually uses: the per-tenant brain ContextVar."""
    monkeypatch.setattr(
        llm_client, "_tenant_brain",
        lambda: {"backend_type": backend_type, "base_url": base_url,
                 "account_id": "acct-under-test"},
    )
    return llm_client.get_backend_endpoint()


# ══════════════════════════════════════════════════════════════════════════════════════
# PART 1 — PINNED BEFORE THE CHANGE. These passed on unmodified code.
# ══════════════════════════════════════════════════════════════════════════════════════

# ── the two providers live on production ──────────────────────────────────────────────

DASHSCOPE_EXPECTED = (
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
)


def test_dashscope_trailing_slash_env(monkeypatch):
    """LIVE ON PROD (account 95aa8183…, model qwen-turbo). Base ends in a trailing slash
    and a NON-version segment (``compatible-mode``) — must keep the ``/v1`` suffix."""
    assert _env_endpoint(
        monkeypatch, "openai",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/",
    ) == DASHSCOPE_EXPECTED


def test_dashscope_trailing_slash_tenant(monkeypatch):
    assert _tenant_endpoint(
        monkeypatch, "openai",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/",
    ) == DASHSCOPE_EXPECTED


def test_dashscope_without_trailing_slash(monkeypatch):
    """The resolver rstrip("/")s the base — with or without the slash is identical."""
    assert _env_endpoint(
        monkeypatch, "openai",
        "https://dashscope-intl.aliyuncs.com/compatible-mode",
    ) == DASHSCOPE_EXPECTED


CEREBRAS_EXPECTED = "https://api.cerebras.ai/v1/chat/completions"


def test_cerebras_plain_unversioned_base_env(monkeypatch):
    """Cerebras-shaped: a bare host, no path at all → suffix appended verbatim."""
    assert _env_endpoint(
        monkeypatch, "openai", "https://api.cerebras.ai") == CEREBRAS_EXPECTED


def test_cerebras_plain_unversioned_base_tenant(monkeypatch):
    assert _tenant_endpoint(
        monkeypatch, "openai", "https://api.cerebras.ai") == CEREBRAS_EXPECTED


def test_cerebras_trailing_slash(monkeypatch):
    assert _env_endpoint(
        monkeypatch, "openai", "https://api.cerebras.ai/") == CEREBRAS_EXPECTED


# ── every remaining _BACKEND_PATHS entry, unversioned base ────────────────────────────

@pytest.mark.parametrize("backend_type,expected_suffix", [
    ("openwebui", "/api/chat/completions"),
    ("ollama",    "/v1/chat/completions"),
    ("lm_studio", "/v1/chat/completions"),
    ("openai",    "/v1/chat/completions"),
    ("anthropic", "/v1/messages"),
    ("groq",      "/openai/v1/chat/completions"),
    ("localai",   "/v1/chat/completions"),
    ("raw",       ""),
])
def test_every_backend_type_unversioned_base(monkeypatch, backend_type, expected_suffix):
    assert _env_endpoint(monkeypatch, backend_type, "https://host.example") == (
        "https://host.example" + expected_suffix)


def test_unknown_backend_type_defaults_to_openai_shape(monkeypatch):
    assert _env_endpoint(monkeypatch, "not-a-real-backend", "https://host.example") == (
        "https://host.example/v1/chat/completions")


def test_openwebui_prod_shape(monkeypatch):
    """The pre-prod/prod default path (qwen behind OpenWebUI)."""
    assert _env_endpoint(monkeypatch, "openwebui", "http://open-webui:8080") == (
        "http://open-webui:8080/api/chat/completions")


def test_no_base_url_returns_none(monkeypatch):
    assert _env_endpoint(monkeypatch, "openai", "") is None


# ── shapes that must NOT be treated as "already versioned" ────────────────────────────

def test_versioned_looking_HOSTNAME_is_not_a_version_segment(monkeypatch):
    """``v4`` in the HOST is not a path version — the suffix must still be appended."""
    assert _env_endpoint(monkeypatch, "openai", "https://v4.example.com") == (
        "https://v4.example.com/v1/chat/completions")


def test_segment_starting_with_v_and_digits_but_not_a_version(monkeypatch):
    """``v2ray`` is a word, not an API version qualifier — suffix stays."""
    assert _env_endpoint(monkeypatch, "openai", "https://host.example/v2ray") == (
        "https://host.example/v2ray/v1/chat/completions")


def test_bare_v_segment_is_not_a_version(monkeypatch):
    assert _env_endpoint(monkeypatch, "openai", "https://host.example/v") == (
        "https://host.example/v/v1/chat/completions")


def test_groq_versioned_base_keeps_its_openai_prefix(monkeypatch):
    """GUARD AGAINST A NAIVE "strip one version" RULE.

    Groq's suffix is ``/openai/v1/chat/completions`` — its FIRST segment is ``openai``,
    not a version. A rule that stripped "a version anywhere in the suffix" would eat the
    ``/v1`` out of the middle and break a working provider. Legacy behaviour (double path)
    is preserved verbatim: the base is misconfigured, but we do not silently reshape a
    suffix whose leading segment we did not put there.
    """
    assert _env_endpoint(monkeypatch, "groq", "https://api.groq.com/v1") == (
        "https://api.groq.com/v1/openai/v1/chat/completions")


def test_openwebui_versioned_base_is_untouched(monkeypatch):
    """OpenWebUI's suffix carries NO version — nothing to skip, ever."""
    assert _env_endpoint(monkeypatch, "openwebui", "http://gw.example/v1") == (
        "http://gw.example/v1/api/chat/completions")


def test_raw_backend_versioned_base_is_untouched(monkeypatch):
    """``raw`` means the base IS the full URL — never append, never rewrite."""
    assert _env_endpoint(
        monkeypatch, "raw", "https://host.example/api/paas/v4/chat/completions") == (
        "https://host.example/api/paas/v4/chat/completions")


# ══════════════════════════════════════════════════════════════════════════════════════
# PART 2 — NEW BEHAVIOUR (ZAIURL). These are RED on unmodified code.
#
# Rule: when the BASE URL's PATH already ends in a version segment, and the backend
# suffix's LEADING segment is also a version segment, the suffix's version is skipped —
# the base's version wins (it is what the tenant configured, and it is provider truth).
# Structural, provider-agnostic: no hostname matching, no provider list.
# ══════════════════════════════════════════════════════════════════════════════════════

ZAI_EXPECTED = "https://api.z.ai/api/paas/v4/chat/completions"


def test_zai_versioned_base_does_not_double_the_version_env(monkeypatch):
    """THE DEFECT. Stored on prod as account d0fa70b1… (backend_type=openai,
    base_url=https://api.z.ai/api/paas/v4). Before: …/v4/v1/chat/completions → 404."""
    assert _env_endpoint(monkeypatch, "openai", "https://api.z.ai/api/paas/v4") == ZAI_EXPECTED


def test_zai_versioned_base_does_not_double_the_version_tenant(monkeypatch):
    """Same via the per-tenant brain ContextVar — the path production actually takes."""
    assert _tenant_endpoint(monkeypatch, "openai", "https://api.z.ai/api/paas/v4") == ZAI_EXPECTED


def test_zai_trailing_slash(monkeypatch):
    assert _env_endpoint(monkeypatch, "openai", "https://api.z.ai/api/paas/v4/") == ZAI_EXPECTED


def test_gemini_style_v1beta_base(monkeypatch):
    """``v1beta`` / ``v1alpha`` are real, in-the-wild version qualifiers (Google
    generativelanguage, Kubernetes-style ``v1beta1``) — recognised as versions."""
    assert _env_endpoint(
        monkeypatch, "openai", "https://generativelanguage.googleapis.com/v1beta") == (
        "https://generativelanguage.googleapis.com/v1beta/chat/completions")


def test_v1beta1_style_base(monkeypatch):
    assert _env_endpoint(monkeypatch, "openai", "https://host.example/api/v1beta1") == (
        "https://host.example/api/v1beta1/chat/completions")


def test_openai_base_that_already_carries_v1(monkeypatch):
    """The common hand-config error (paste the full ``/v1`` base) now resolves instead
    of producing ``/v1/v1/chat/completions``."""
    assert _env_endpoint(monkeypatch, "openai", "https://api.openai.com/v1") == (
        "https://api.openai.com/v1/chat/completions")


def test_anthropic_base_that_already_carries_v1(monkeypatch):
    """Anthropic's suffix ``/v1/messages`` also leads with a version → same skip."""
    assert _env_endpoint(monkeypatch, "anthropic", "https://api.anthropic.com/v1") == (
        "https://api.anthropic.com/v1/messages")
