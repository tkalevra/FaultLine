"""``context_window._coord_scope()`` — the brain-identity resolver, pinned tier by tier.

WHY THIS FILE EXISTS
────────────────────
`_coord_scope()` returned None for every context without a bound tenant brain, which silently
disabled the shared learned-window cache. The two tests that caught it live in
`test_redis_coord_consumers.py` and require a LIVE Redis — without one they SKIP, so the
defect sat unnoticed. Nothing here touches Redis: `_coord_scope()` only *names* a key, it
never issues a command, so every tier is assertable in-process and cannot silently skip.

THE ROOT CAUSE THESE PIN
────────────────────────
Identity was taken from ``llm_client.get_backend_endpoint()`` — the CALL resolver. Under
``SAAS_MODE`` that is deliberately fail-CLOSED (returns None so a call can never go out on the
operator's env brain), and it returns ``base_url`` + the backend's chat path. Naming a key is
not making a call, and a backend-path suffix is not part of a brain's identity.
"""

import os

import pytest

from src.api import context_window as CW
from src.api import llm_calls as L
from src.api import redis_coord as RC


@pytest.fixture
def window_cache_on(monkeypatch):
    """The capability's own flags. `_coord_scope` short-circuits to None without them, and
    that short-circuit is already pinned by
    `test_window_cache_off_by_default_leaves_resolution_untouched`."""
    monkeypatch.setenv("REDIS_COORD", "true")
    monkeypatch.setenv("REDIS_COORD_WINDOW", "true")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("WGM_LLM_MODEL", raising=False)
    yield


def _stub_bound_brain(monkeypatch, cfg):
    """Bind (or unbind) a per-tenant BYO brain the way a SaaS request does."""
    import saas.llm_context as LC
    monkeypatch.setattr(LC, "current_llm_config", lambda: cfg, raising=True)


# ── the regression itself ─────────────────────────────────────────────────────

def test_saas_mode_does_not_blank_the_scope_of_a_configured_brain(window_cache_on, monkeypatch):
    """THE BUG. `SAAS_MODE` is on in every deployment that matters, and the identity resolver
    used a call resolver that fail-CLOSES under it — so a perfectly well-identified brain got
    no scope, and the shared window cache was inert."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-w:8080/v1")
    monkeypatch.setenv("WGM_LLM_MODEL", "qwen-test")
    _stub_bound_brain(monkeypatch, None)

    scope = CW._coord_scope()
    assert scope is not None, (
        "a brain that IS identified must get a scope; refusing one here is what silently "
        "disabled the shared learned-window cache"
    )
    assert RC.key("ctxwin", scope, "n_ctx").startswith("fl:ctxwin:brain:")


def test_the_window_consumer_agrees_with_the_breaker_on_what_a_brain_IS(window_cache_on,
                                                                       monkeypatch):
    """Same brain, same identity. The breaker/semaphore resolver (`_breaker_scope`) is the
    reference; a second, divergent notion of "which brain is this" is the bug class."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-shared:8080/v1")
    monkeypatch.setenv("WGM_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("REDIS_COORD_BREAKER", "true")
    _stub_bound_brain(monkeypatch, None)

    assert CW._coord_scope() == L._breaker_scope()


def test_identity_is_the_base_url_not_the_backends_chat_path(window_cache_on, monkeypatch):
    """`get_backend_endpoint()` appends a per-backend path, so flipping `LLM_BACKEND_TYPE`
    re-hashed the SAME brain to a new key and threw the learned window away."""
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-w:8080/v1")
    _stub_bound_brain(monkeypatch, None)

    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    a = CW._coord_scope()
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openai")
    b = CW._coord_scope()
    assert a is not None and a == b, "the brain did not change; only the request path did"


# ── tier order ────────────────────────────────────────────────────────────────

def test_a_bound_tenant_brain_outranks_the_operator_env_brain(window_cache_on, monkeypatch):
    """SaaS: the tenant's BYO brain is the one being called, so it is the one being scoped.
    Falling through to the operator env brain would file a tenant's window under a scope
    every OTHER tenant also resolves to."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://operator-brain:8080/v1")
    monkeypatch.setenv("WGM_LLM_MODEL", "operator-model")
    _stub_bound_brain(monkeypatch, {"base_url": "http://tenant-brain:9090/v1",
                                    "model": "tenant-model"})

    scope = CW._coord_scope()
    assert scope == RC.scope_for_endpoint("http://tenant-brain:9090/v1", "tenant-model")
    assert scope != RC.scope_for_endpoint("http://operator-brain:8080/v1", "operator-model")


def test_two_tenant_brains_never_collide(window_cache_on, monkeypatch):
    """The isolation property, asserted at the resolver instead of only through Redis."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _stub_bound_brain(monkeypatch, {"base_url": "http://tenant-a:8080/v1", "model": "m"})
    a = CW._coord_scope()
    _stub_bound_brain(monkeypatch, {"base_url": "http://tenant-b:8080/v1", "model": "m"})
    b = CW._coord_scope()
    assert a is not None and b is not None and a != b


def test_the_same_model_on_two_endpoints_is_two_brains(window_cache_on, monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    _stub_bound_brain(monkeypatch, {"base_url": "http://a:8080/v1", "model": "qwen"})
    a = CW._coord_scope()
    _stub_bound_brain(monkeypatch, {"base_url": "http://b:8080/v1", "model": "qwen"})
    assert a != CW._coord_scope()


def test_two_models_on_one_endpoint_are_two_brains(window_cache_on, monkeypatch):
    """`n_ctx` is a property of the LOADED MODEL, not of the host, so the model is part of
    the identity — a 4k model must not inherit a 128k model's learned window."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _stub_bound_brain(monkeypatch, {"base_url": "http://a:8080/v1", "model": "small"})
    a = CW._coord_scope()
    _stub_bound_brain(monkeypatch, {"base_url": "http://a:8080/v1", "model": "big"})
    assert a != CW._coord_scope()


# ── the unidentified-brain tier ───────────────────────────────────────────────

def test_open_core_with_no_configured_brain_gets_an_EXPLICIT_single_tenant_scope(
        window_cache_on, monkeypatch):
    """`redis_coord`'s rule 2: a key is scoped or it is not built. `scope_single_tenant()`
    exists precisely so the non-SaaS path has an explicit scope instead of an empty one, and
    `_breaker_scope` already lands there."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    _stub_bound_brain(monkeypatch, None)

    scope = CW._coord_scope()
    assert scope == RC.scope_single_tenant()
    assert RC.key("ctxwin", scope, "n_ctx").startswith("fl:ctxwin:solo:")


def test_saas_with_no_identifiable_brain_refuses_the_shared_solo_key(window_cache_on,
                                                                    monkeypatch):
    """`solo` is ONE key every tenant resolves to. The breaker can afford that — its shared
    verdict is one-way and costs at most `timeout_seconds` of fast-fail. A WINDOW cannot: a
    wrong number silently truncates prompts. None = no shared opinion = in-process
    behaviour, which is never worse than today."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _stub_bound_brain(monkeypatch, None)

    assert CW._coord_scope() is None


def test_a_bound_brain_with_no_base_url_is_not_an_identity(window_cache_on, monkeypatch):
    """A cfg row exists but carries no endpoint — that is an unresolved brain, not a brain
    named ''. It must fall through, never build a key from the empty string."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://operator-brain:8080/v1")
    _stub_bound_brain(monkeypatch, {"base_url": "", "model": "m"})

    assert CW._coord_scope() == RC.scope_for_endpoint("http://operator-brain:8080/v1", "")


def test_scope_resolution_never_raises_outward(window_cache_on, monkeypatch):
    """Coordination must never break a call: a blown resolver degrades to "no shared
    opinion", it does not propagate."""
    monkeypatch.setenv("SAAS_MODE", "1")
    import saas.llm_context as LC

    def _boom():
        raise RuntimeError("brain resolution exploded")

    monkeypatch.setattr(LC, "current_llm_config", _boom, raising=True)
    monkeypatch.setenv("LLM_BASE_URL", "http://brain-w:8080/v1")
    assert CW._coord_scope() is not None, "a SaaS-side blowup must not lose the env brain"


def test_saas_mode_helper_fails_SAFE_when_it_cannot_resolve(monkeypatch):
    """`_saas_mode_on()` guards whether an unidentified brain may take the shared key.
    Unresolvable → assume multi-tenant → refuse. The conservative direction."""
    import builtins
    real_import = builtins.__import__

    def _no_llm_client(name, *a, **kw):
        if name == "src.api.llm_client":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_llm_client)
    assert CW._saas_mode_on() is True


def test_os_environ_is_read_live_not_captured_at_import(window_cache_on, monkeypatch):
    """A tenant/test toggle must take effect without a module reload."""
    monkeypatch.setenv("SAAS_MODE", "1")
    _stub_bound_brain(monkeypatch, None)
    assert CW._coord_scope() is None
    monkeypatch.setenv("LLM_BASE_URL", "http://late-brain:8080/v1")
    assert CW._coord_scope() == RC.scope_for_endpoint("http://late-brain:8080/v1", "")


def test_no_hardcoded_brain_or_pronoun_literals_in_the_resolver():
    """Subject-agnostic guard: identity comes from config/env, never from a name list."""
    import inspect
    src = inspect.getsource(CW._coord_scope) + inspect.getsource(CW._saas_mode_on)
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith(("#", "*"))
                     and '"""' not in ln)
    for banned in ("localhost", "127.0.0.1", "openai.com", "qwen", "faultline-redis"):
        assert banned not in body.lower(), f"hardcoded brain literal {banned!r} in the resolver"
    assert os.environ is not None  # module-level `os` is the live mapping, not a snapshot
