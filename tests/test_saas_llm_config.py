"""Per-account BYO engine-brain LLM config — routing + crypto + SSRF + breaker tests.

The #1 requirement proven here: SAAS-off / unconfigured == today's behavior EXACTLY
(the ContextVar is unset → the LLM stack uses the global env path, byte-for-byte).

These are pure unit/seam tests (no DB, no network): they drive the resolver functions,
crypto, the SSRF guard, the per-tenant breaker, the extra_params floor, GET redaction, and
the extraction-probe path directly, with the DB/LLM seams monkeypatched.

Run: python3 -m pytest tests/test_saas_llm_config.py -q   (tests/ is gitignored → git add -f)
"""

import base64
import hashlib

import pytest

from saas import crypto as saas_crypto
from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_calls
from src.api import llm_client


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clean_context():
    """Ensure the per-tenant ContextVar + caches are clean around every test."""
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()


def _bind_cfg(cfg: dict):
    """Set the explicit ContextVar directly (bypasses DB) and return the reset token."""
    return saas_llm_context._current_llm_config.set(cfg)


# ════════════════════════════════════════════════════════════════════════════════════
# #1 — SAAS-off / unconfigured == today's behavior EXACTLY (byte-for-byte)
# ════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_context_unset_uses_global(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    # No ContextVar bound, SAAS off → resolver returns None.
    assert saas_llm_context.current_llm_config() is None

    # _apply_tenant_brain returns the GLOBAL breaker (same object), unchanged model/mt,
    # empty extra params — the exact pre-existing behavior.
    breaker, model, mt, extra = llm_calls._apply_tenant_brain("global-model", 2048)
    assert breaker is llm_calls._llm_circuit_breaker
    assert model == "global-model"
    assert mt == 2048
    assert extra == {}


def test_saas_off_backend_resolution_is_env(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("LLM_BACKEND_TYPE", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("LLM_API_KEY", "env-key")

    assert llm_client.get_backend_type() == "ollama"
    assert llm_client.get_backend_endpoint() == "https://env.example.com/v1/chat/completions"
    assert llm_client.get_llm_headers() == {"Authorization": "Bearer env-key"}


# ════════════════════════════════════════════════════════════════════════════════════
# #2 — config set → tenant endpoint / model / key used in the built payload
# ════════════════════════════════════════════════════════════════════════════════════

def test_tenant_config_drives_endpoint_headers_model(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND_TYPE", "ollama")            # global — must be OVERRIDDEN
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("LLM_API_KEY", "env-key")

    cfg = {
        "account_id": ACCT_A,
        "backend_type": "openai",
        "base_url": "https://tenant.example.com",
        "model": "tenant-model",
        "api_key": "tenant-secret",
        "extra_headers": {"X-Org": "acme"},
        "extra_params": {"temperature": 0.3},
    }
    token = _bind_cfg(cfg)
    try:
        assert llm_client.get_backend_type() == "openai"
        assert (llm_client.get_backend_endpoint()
                == "https://tenant.example.com/v1/chat/completions")
        headers = llm_client.get_llm_headers()
        assert headers["Authorization"] == "Bearer tenant-secret"
        assert headers["X-Org"] == "acme"

        breaker, model, mt, extra = llm_calls._apply_tenant_brain("global-model", 2048)
        assert model == "tenant-model"
        assert breaker is not llm_calls._llm_circuit_breaker  # per-tenant breaker
        payload = llm_client.build_llm_payload(
            messages=[{"role": "user", "content": "hi"}],
            model=model, user_id="u", max_tokens=mt, **{"temperature": 0.0, **extra})
        assert payload["model"] == "tenant-model"
        assert payload["temperature"] == 0.3
        assert payload["stream"] is False
    finally:
        saas_llm_context.reset_llm_config(token)


# ════════════════════════════════════════════════════════════════════════════════════
# #3 — crypto round-trip + never-plaintext-at-rest
# ════════════════════════════════════════════════════════════════════════════════════

def test_crypto_roundtrip_and_never_plaintext(monkeypatch):
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", "a-test-master-passphrase")
    plaintext = "sk-super-secret-key-123"
    ct = saas_crypto.encrypt(plaintext)
    assert ct and ct != plaintext
    assert plaintext not in ct                       # ciphertext must not leak plaintext
    assert saas_crypto.decrypt(ct) == plaintext      # reversible


def test_crypto_fernet_key_used_directly(monkeypatch):
    real_fernet = base64.urlsafe_b64encode(b"\x01" * 32).decode()
    assert len(real_fernet) == 44
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", real_fernet)
    assert saas_crypto.decrypt(saas_crypto.encrypt("x")) == "x"


def test_crypto_fail_closed_without_key(monkeypatch):
    monkeypatch.delenv("SAAS_LLM_CRED_KEY", raising=False)
    with pytest.raises(saas_crypto.CryptoUnavailable):
        saas_crypto.encrypt("secret")
    # Decrypt of a real blob without the key → None (deny), never a raise/plaintext.
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", "keyA")
    blob = saas_crypto.encrypt("secret")
    monkeypatch.setenv("SAAS_LLM_CRED_KEY", "keyB-different")
    assert saas_crypto.decrypt(blob) is None


# ════════════════════════════════════════════════════════════════════════════════════
# #4 — SSRF egress guard
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1",           # loopback
    "https://169.254.169.254/latest/meta", # cloud metadata (link-local)
    "https://10.0.0.5",                    # RFC1918
    "https://192.168.1.10",                # RFC1918
    "https://172.16.5.5",                  # RFC1918
    "ftp://example.com",                   # scheme
])
def test_ssrf_blocks(url, monkeypatch):
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    with pytest.raises(saas_llm_config.EgressBlocked):
        saas_llm_config.validate_egress_url(url)


def test_ssrf_allows_public_https(monkeypatch):
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    # Public IP literals — deterministic, no DNS needed.
    saas_llm_config.validate_egress_url("https://1.1.1.1/v1/chat/completions")
    saas_llm_config.validate_egress_url("https://8.8.8.8")


def test_ssrf_allowlist_overrides_and_permits_http(monkeypatch):
    # ⚠️ 2026-08-01: the allowlist is about REACHING AN INTERNAL ADDRESS, and nothing else.
    # It used to ALSO be the only way to use plain http at all; that half was split off —
    # http:// to a PUBLIC address now needs no allowlist entry. See
    # tests/test_saas_egress_http_policy.py. What this test still pins is the part that did
    # NOT change: an in-CIDR internal host is reachable only because the OPERATOR listed it.
    monkeypatch.setenv("SAAS_LLM_ALLOWED_EGRESS", "10.0.0.0/8")
    saas_llm_config.validate_egress_url("http://10.0.0.5:11434/v1")
    # Still blocks a private host OUTSIDE the allowlist.
    with pytest.raises(saas_llm_config.EgressBlocked):
        saas_llm_config.validate_egress_url("http://192.168.0.9")


# ════════════════════════════════════════════════════════════════════════════════════
# SSRF hardening (audit HIGH #2): multi-A-record rejection + inference-path egress re-check
# ════════════════════════════════════════════════════════════════════════════════════

def _fake_getaddrinfo(*ips):
    """Return a getaddrinfo-shaped result list for the given IPv4 strings."""
    return lambda host, *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in ips]


def test_ssrf_rejects_multi_record_host_with_one_private(monkeypatch):
    """A host resolving to BOTH a public AND a private IP must be REJECTED — every A/AAAA
    record is vetted, not just the first. Closes the multi-record private-IP smuggle."""
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    # public first, private second — order must not matter.
    monkeypatch.setattr(saas_llm_config.socket, "getaddrinfo",
                        _fake_getaddrinfo("8.8.8.8", "169.254.169.254"))
    with pytest.raises(saas_llm_config.EgressBlocked):
        saas_llm_config.validate_egress_url("https://rebind.example.com/v1")
    # private first, public second — still rejected.
    monkeypatch.setattr(saas_llm_config.socket, "getaddrinfo",
                        _fake_getaddrinfo("10.1.2.3", "8.8.8.8"))
    with pytest.raises(saas_llm_config.EgressBlocked):
        saas_llm_config.validate_egress_url("https://rebind.example.com/v1")


def test_ssrf_allows_multi_record_all_public(monkeypatch):
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    monkeypatch.setattr(saas_llm_config.socket, "getaddrinfo",
                        _fake_getaddrinfo("8.8.8.8", "1.1.1.1"))
    saas_llm_config.validate_egress_url("https://cdn.example.com")  # no raise


def test_inference_egress_recheck_blocks_rebound_tenant_endpoint(monkeypatch):
    """The inference-path re-check: a tenant endpoint that NOW resolves to a private IP is
    blocked before any outbound call (closes the 'no egress check at inference' gap)."""
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    monkeypatch.setattr(saas_llm_config.socket, "getaddrinfo",
                        _fake_getaddrinfo("10.1.2.3"))  # rebound to RFC1918
    cfg = {"account_id": ACCT_A, "backend_type": "openai",
           "base_url": "https://tenant-brain.example.com/v1", "model": "m"}
    token = _bind_cfg(cfg)
    try:
        assert llm_calls._tenant_egress_blocked() is True
    finally:
        saas_llm_context.reset_llm_config(token)


def test_inference_egress_recheck_allows_public_tenant_endpoint(monkeypatch):
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    monkeypatch.setattr(saas_llm_config.socket, "getaddrinfo",
                        _fake_getaddrinfo("8.8.8.8"))
    cfg = {"account_id": ACCT_A, "backend_type": "openai",
           "base_url": "https://tenant-brain.example.com/v1", "model": "m"}
    token = _bind_cfg(cfg)
    try:
        assert llm_calls._tenant_egress_blocked() is False
    finally:
        saas_llm_context.reset_llm_config(token)


def test_inference_egress_recheck_noop_without_tenant_brain():
    """No tenant cfg bound (global env path) → never blocks (byte-for-byte unchanged)."""
    assert llm_calls._tenant_egress_blocked() is False


def test_inference_egress_recheck_noop_for_no_brain_sentinel():
    """The fail-closed no-brain sentinel is not a routable endpoint → never egress-checked."""
    token = _bind_cfg({"account_id": ACCT_A, "no_brain": True})
    try:
        assert llm_calls._tenant_egress_blocked() is False
    finally:
        saas_llm_context.reset_llm_config(token)


# ════════════════════════════════════════════════════════════════════════════════════
# #5 — extra_params merge respects the EXTRACT max_tokens floor
# ════════════════════════════════════════════════════════════════════════════════════

def test_extra_params_never_regress_max_tokens_floor():
    cfg = {"account_id": ACCT_A, "backend_type": "openai",
           "base_url": "https://t", "model": "m",
           "extra_params": {"max_tokens": 100, "temperature": 0.7}}
    token = _bind_cfg(cfg)
    try:
        _b, _m, mt, extra = llm_calls._apply_tenant_brain("m", 2048)
        assert mt == 2048               # clamp UP: 100 must NOT lower the 2048 floor
        assert "max_tokens" not in extra
        assert extra["temperature"] == 0.7
        assert "stream" not in extra    # stream is never allowed through
    finally:
        saas_llm_context.reset_llm_config(token)


def test_extra_params_can_raise_max_tokens():
    cfg = {"account_id": ACCT_A, "backend_type": "openai",
           "base_url": "https://t", "model": "m",
           "extra_params": {"max_tokens": 4096}}
    token = _bind_cfg(cfg)
    try:
        _b, _m, mt, _extra = llm_calls._apply_tenant_brain("m", 2048)
        assert mt == 4096               # clamp UP is allowed
    finally:
        saas_llm_context.reset_llm_config(token)


# ════════════════════════════════════════════════════════════════════════════════════
# #6 — per-tenant circuit breaker isolation
# ════════════════════════════════════════════════════════════════════════════════════

def test_per_tenant_breaker_isolation():
    b_none = llm_calls._breaker_for(None)
    assert b_none is llm_calls._llm_circuit_breaker

    b_a = llm_calls._breaker_for(ACCT_A)
    b_b = llm_calls._breaker_for(ACCT_B)
    assert b_a is not b_b
    assert b_a is not b_none

    # Trip A's breaker to threshold — B and the global must be unaffected.
    for _ in range(b_a.failure_threshold):
        b_a.record_failure()
    assert b_a.is_open() is True
    assert b_b.is_open() is False
    assert b_none.is_open() is False
    b_a.reset()


# ════════════════════════════════════════════════════════════════════════════════════
# #7 — GET redacts the key
# ════════════════════════════════════════════════════════════════════════════════════

def test_get_llm_config_redacts_key(monkeypatch):
    from saas import llm_admin

    def fake_redacted(account_id):
        return {"account_id": account_id, "configured": True, "backend_type": "openai",
                "base_url": "https://t", "model": "m", "api_key_set": True,
                "extra_headers": {}, "extra_params": {}, "embedding_key_set": False,
                "last_tested_at": None}

    monkeypatch.setattr(llm_admin._llm_config, "get_config_redacted", fake_redacted)
    out = llm_admin.get_llm_config(resolved=ACCT_A)
    assert out["api_key_set"] is True
    assert "api_key" not in out            # the key itself is NEVER echoed
    assert "api_key_ciphertext" not in out


def test_get_llm_config_operator_rejected():
    from saas import llm_admin
    with pytest.raises(Exception):
        llm_admin.get_llm_config(resolved="__operator__")


# ════════════════════════════════════════════════════════════════════════════════════
# #8 — extraction-probe path
# ════════════════════════════════════════════════════════════════════════════════════

def test_probe_reports_clean_triples(monkeypatch):
    from saas import llm_admin

    cfg = {"account_id": ACCT_A, "model": "tenant-model",
           "backend_type": "openai", "base_url": "https://t", "api_key": None,
           "extra_headers": {}, "extra_params": {}}

    monkeypatch.setattr(saas_llm_context, "bind_account_llm_config", lambda a: None)
    monkeypatch.setattr(saas_llm_context, "reset_llm_config", lambda t: None)

    def fake_call(**kw):
        return {"triples": [{"subject": "Alice", "relation": "age", "object": "30"}]}

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", fake_call)
    out = llm_admin._probe_extraction(ACCT_A, cfg)
    assert out["ok"] is True
    assert out["triples_found"] == 1


def test_probe_reports_failure_on_junk(monkeypatch):
    from saas import llm_admin

    cfg = {"account_id": ACCT_A, "model": "tenant-model",
           "backend_type": "openai", "base_url": "https://t", "api_key": None,
           "extra_headers": {}, "extra_params": {}}

    monkeypatch.setattr(saas_llm_context, "bind_account_llm_config", lambda a: None)
    monkeypatch.setattr(saas_llm_context, "reset_llm_config", lambda t: None)
    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", lambda **kw: {})

    out = llm_admin._probe_extraction(ACCT_A, cfg)
    assert out["ok"] is False


# ════════════════════════════════════════════════════════════════════════════════════
# #9 — list_remote_models: per-backend shape parsing, SSRF-before-fetch, fail-graceful
# ════════════════════════════════════════════════════════════════════════════════════

def _capture_get(monkeypatch, payload):
    """Stub _http_get_json → record (url, headers) and return a fixed payload."""
    seen = {}

    def _fake(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        seen["timeout"] = timeout
        return payload

    monkeypatch.setattr(saas_llm_config, "_http_get_json", _fake)
    return seen


# THE INVARIANT: the model-list URL is the SIBLING of that backend's CHAT path — same
# prefix, "models" swapped for the operation. This assertion used to read "/v1/models" for
# every backend, which pinned the OLD HARDCODE rather than a verified contract (the stub
# never talks to a real server, so nothing about it was measured). It hid a live defect:
# groq's chat path is "/openai/v1/chat/completions", so its picker asked "/v1/models" and
# 404'd against a brain whose chat worked — precisely the divergence resolve_endpoint was
# introduced to make impossible. openwebui likewise serves "/api/models" beside its
# "/api/chat/completions"; a bare "/v1/models" there is a 404.
@pytest.mark.parametrize("backend,expected_url", [
    ("openai",    "https://1.1.1.1/v1/models"),
    ("groq",      "https://1.1.1.1/openai/v1/models"),
    ("openwebui", "https://1.1.1.1/api/models"),
    ("lm_studio", "https://1.1.1.1/v1/models"),
])
def test_list_models_openai_compatible_shape(backend, expected_url, monkeypatch):
    seen = _capture_get(monkeypatch, {"data": [{"id": "m-a"}, {"id": "m-b"}, {"nope": 1}]})
    out = saas_llm_config.list_remote_models(backend, "https://1.1.1.1", api_key="sekret")
    assert out == {"models": ["m-a", "m-b"]}
    assert seen["url"] == expected_url
    assert seen["headers"]["Authorization"] == "Bearer sekret"


def test_list_models_openai_no_key_no_auth_header(monkeypatch):
    seen = _capture_get(monkeypatch, {"data": [{"id": "m-a"}]})
    out = saas_llm_config.list_remote_models("openai", "https://1.1.1.1", api_key=None)
    assert out == {"models": ["m-a"]}
    assert "Authorization" not in seen["headers"]


def test_list_models_strips_trailing_v1(monkeypatch):
    seen = _capture_get(monkeypatch, {"data": []})
    saas_llm_config.list_remote_models("openai", "https://1.1.1.1/v1/", api_key=None)
    # /v1 (and the trailing slash) are stripped before we re-append /v1/models.
    assert seen["url"] == "https://1.1.1.1/v1/models"


def test_list_models_ollama_shape(monkeypatch):
    seen = _capture_get(monkeypatch, {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]})
    out = saas_llm_config.list_remote_models("ollama", "https://1.1.1.1")
    assert out == {"models": ["qwen2.5:7b", "llama3.1:8b"]}
    assert seen["url"] == "https://1.1.1.1/api/tags"
    assert "Authorization" not in seen["headers"]  # ollama is keyless


def test_list_models_anthropic_shape_and_headers(monkeypatch):
    seen = _capture_get(monkeypatch, {"data": [{"id": "claude-3-5-sonnet"}]})
    out = saas_llm_config.list_remote_models("anthropic", "https://1.1.1.1", api_key="sk-ant")
    assert out == {"models": ["claude-3-5-sonnet"]}
    assert seen["url"] == "https://1.1.1.1/v1/models"
    assert seen["headers"]["x-api-key"] == "sk-ant"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"


def test_list_models_dedupes_and_drops_blanks(monkeypatch):
    _capture_get(monkeypatch, {"data": [{"id": "m"}, {"id": "m"}, {"id": " "}, {"id": "n"}]})
    out = saas_llm_config.list_remote_models("openai", "https://1.1.1.1")
    assert out == {"models": ["m", "n"]}


def test_list_models_ssrf_rejects_internal_before_any_fetch(monkeypatch):
    called = {"hit": False}

    def _fake(url, headers, timeout):
        called["hit"] = True
        return {"data": [{"id": "x"}]}

    monkeypatch.setattr(saas_llm_config, "_http_get_json", _fake)
    monkeypatch.delenv("SAAS_LLM_ALLOWED_EGRESS", raising=False)
    out = saas_llm_config.list_remote_models("openai", "http://127.0.0.1:11434")
    assert "error" in out and "models" not in out
    assert called["hit"] is False  # SSRF guard fires BEFORE the network call


def test_list_models_unsupported_backend(monkeypatch):
    _capture_get(monkeypatch, {"data": []})
    out = saas_llm_config.list_remote_models("mystery", "https://1.1.1.1")
    assert "error" in out and "models" not in out


def test_list_models_fetch_failure_returns_error_never_raises(monkeypatch):
    def _boom(url, headers, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(saas_llm_config, "_http_get_json", _boom)
    out = saas_llm_config.list_remote_models("openai", "https://1.1.1.1")
    assert "error" in out and "models" not in out


def test_list_models_never_logs_the_key(monkeypatch):
    logged = []
    monkeypatch.setattr(saas_llm_config, "_log_crit",
                        lambda event, **kw: logged.append((event, kw)))

    def _boom(url, headers, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(saas_llm_config, "_http_get_json", _boom)
    saas_llm_config.list_remote_models("openai", "https://1.1.1.1", api_key="TOPSECRETKEY")
    blob = repr(logged)
    assert "TOPSECRETKEY" not in blob
