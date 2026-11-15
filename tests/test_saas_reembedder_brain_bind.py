"""Re_embedder ASYNC/BACKGROUND per-tenant BYO engine-brain binding (SaaS Gap-B).

THE BUG this pins: the re_embedder runs several per-tenant ``for … in ready_schemas`` growth
loops. Only the PRIMARY sync loop bound the tenant brain (and it CLEARED it afterwards); every
subsequent LLM-calling loop — the ±6 what-is/climb ENRICHMENT grounder, ontology eval, taxonomy
discovery, name-conflict arbitration, natural-language phrasing fill — set the DB search_path but
NOT the LLM ContextVar, and this process never sets the request-path ``rel_type_overlay`` schema
either, so ``current_llm_config()`` returned None → every one of those background LLM calls fell
through to the process-global env brain (the dead ``open-webui:8080`` default on the CPU-only SaaS
host → the "115 ENRICHMENT calls to the global default" prod hang; in a live multi-tenant system a
cross-tenant routing risk).

The fix binds the tenant brain at the top of EACH per-tenant LLM loop via the single-source
``_reembedder_bind_tenant_brain`` helper. These tests prove:
  1. behavioural — after the helper binds tenant T's brain, an ENRICHMENT-style LLM call resolves
     to T's endpoint/model, NOT the global env default; clearing reverts to global.
  2. fail-closed — a tenant with an account but NO brain binds the no-brain sentinel → the LLM call
     returns its safe empty default and makes ZERO network call (never hits the global default).
  3. SAAS off — the helper is a pure no-op; the global env path is byte-for-byte unchanged.
  4. STRUCTURAL — every known background LLM call in the re_embedder lives inside a per-tenant
     ``ready_schemas`` loop that binds the tenant brain first. This guards against the binding
     being dropped again (the gate: structural, not "we remembered to bind").

Pure unit/seam tests: DB + network seams monkeypatched, no live services.

Run: python3 tools/fltest.py --bug BRAIN-BIND-ASYNC --test tests/test_saas_reembedder_brain_bind.py
"""

import re
from pathlib import Path

import pytest

from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_calls
from src.api import llm_client
from src.re_embedder import embedder


ACCT_T = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
TENANT_SCHEMA = "faultline_tenant_t"
TENANT_CFG = {
    "account_id": ACCT_T,
    "backend_type": "openai",
    "base_url": "https://tenant-t.cerebras.example",
    "model": "tenant-t-model",
    "api_key": "tenant-t-secret",
}


@pytest.fixture(autouse=True)
def _clean_context():
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    # Ensure no brain leaks between tests.
    saas_llm_context._current_llm_config.set(None)
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)


def _wire_account(monkeypatch, schema_to_account: dict, account_to_cfg: dict):
    """Monkeypatch the DB seams so the schema→account→config resolution is deterministic.

    ⚠️ DEAD-MOCK REPAIR (2026-08-01). This helper patched ONLY ``_account_for_schema``. The
    seat-brain refactor (``95517d75``, migration 202) changed ``bind_schema_brain_absolute`` to
    call ``_seat_for_schema`` and to route through ``seat_brain.resolve_brain`` — so from that
    commit on, the patched seam was NO LONGER READ. The bind fell through to a real
    ``saas.db.connection()``, which fails with no ``POSTGRES_DSN``, resolved no account, and the
    tests below have been asserting against an unbound context ever since (silently red; this
    file predates that refactor by 17 days).

    This is the "when a mock isn't taking, check the code still READS that seam" corollary in
    CLAUDE.md, so all three live seams are patched now: schema→(seat, account), the account
    resolver, and ``seat_scoped`` (which otherwise does its OWN control-plane read for
    ``accounts.account_kind`` and fails towards seat-scoped).
    """
    monkeypatch.setattr(
        saas_llm_context, "_account_for_schema",
        lambda schema: schema_to_account.get(schema),
    )
    monkeypatch.setattr(
        saas_llm_context, "_seat_for_schema",
        lambda schema: (None, schema_to_account.get(schema)),
    )
    monkeypatch.setattr(
        saas_llm_config, "resolve_account_llm_config",
        lambda account_id: account_to_cfg.get(account_id),
    )
    # These loops iterate SCHEMAS, not seats, so there is no seat identity to resolve with;
    # the corporate (shared-brain) account lane is the one under test here.
    from saas import seat_brain as saas_seat_brain
    monkeypatch.setattr(saas_seat_brain, "seat_scoped", lambda account_id: False)


# ════════════════════════════════════════════════════════════════════════════════════
# 1 — behavioural: the async loop's bind routes ENRICHMENT to the TENANT endpoint
# ════════════════════════════════════════════════════════════════════════════════════

def test_async_loop_bind_routes_enrichment_to_tenant_endpoint(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    # Global env brain — this is the dead default the buggy path fell through to.
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    _wire_account(monkeypatch, {TENANT_SCHEMA: ACCT_T}, {ACCT_T: TENANT_CFG})

    # Before any bind (loop entry): NO endpoint at all.
    #
    # ⚠️ ASSERTION CHANGED 2026-08-01 (Gap A) — it used to assert the global env default here,
    # i.e. it PINNED the fallthrough as expected behaviour. The owner's ruling is that the
    # process-global env brain must not be reachable under SAAS_MODE at all, so the resolvers
    # themselves now fail closed (llm_client.saas_global_brain_blocked). The old expectation
    # and the ruling are mutually exclusive; do NOT "restore" it.
    assert llm_client.get_backend_endpoint() is None

    # This is exactly what each per-tenant growth loop now does before its ENRICHMENT call.
    embedder._reembedder_bind_tenant_brain(TENANT_SCHEMA)

    # An ENRICHMENT-op LLM call in this loop now resolves to the TENANT's own brain.
    assert llm_client.get_backend_endpoint() == "https://tenant-t.cerebras.example/v1/chat/completions"
    _, model, _, _ = llm_calls._apply_tenant_brain("global-enrichment-model", 2048)
    assert model == "tenant-t-model"
    assert llm_calls._saas_brain_blocked() is False

    # Clearing (between-tenant / end-of-cycle) drops the tenant brain. Under SAAS_MODE that
    # leaves NOTHING — not the global env path (Gap A). The point of the clear is that tenant
    # T's brain cannot leak into the next tenant's iteration; it is not a route to a default.
    embedder._reembedder_clear_tenant_brain()
    assert llm_client.get_backend_endpoint() is None
    assert llm_calls._saas_brain_unavailable() is True


# ════════════════════════════════════════════════════════════════════════════════════
# 2 — fail-closed: tenant with NO brain → safe empty default, ZERO network call
# ════════════════════════════════════════════════════════════════════════════════════

def test_async_loop_bind_no_brain_fails_closed_no_global_call(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    # Account resolves, but it has NO brain configured (resolve → None).
    _wire_account(monkeypatch, {TENANT_SCHEMA: ACCT_T}, {ACCT_T: None})

    # Any attempt to build the endpoint list / hit the network must FAIL the test — the
    # fail-closed guard returns {} BEFORE endpoint resolution, so this is never reached.
    def _boom(*a, **k):
        raise AssertionError("fail-closed breached: a network path was reached for a no-brain tenant")

    monkeypatch.setattr(llm_calls, "_get_endpoint_list", _boom)

    embedder._reembedder_bind_tenant_brain(TENANT_SCHEMA)

    # The no-brain sentinel is bound → the LLM stack fail-closes.
    assert llm_calls._saas_brain_blocked() is True
    result = llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "user", "content": "what is a router"}],
        model="global-enrichment-model",
        user_id="re_embedder",
        operation="ENRICHMENT",
    )
    assert result == {}  # safe empty default, no spend, no global endpoint touched


# ════════════════════════════════════════════════════════════════════════════════════
# 3 — SAAS off: the helper is a pure no-op; global env path unchanged
# ════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_bind_is_noop_global_unchanged(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("LLM_BACKEND_TYPE", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")

    # Even if a schema→account mapping existed, SAAS off short-circuits the bind.
    _wire_account(monkeypatch, {TENANT_SCHEMA: ACCT_T}, {ACCT_T: TENANT_CFG})

    embedder._reembedder_bind_tenant_brain(TENANT_SCHEMA)
    assert saas_llm_context.current_llm_config() is None
    assert llm_client.get_backend_endpoint() == "https://env.example.com/v1/chat/completions"
    assert llm_calls._saas_brain_blocked() is False


# ════════════════════════════════════════════════════════════════════════════════════
# 4 — STRUCTURAL: every background LLM call sits inside a tenant-brain-bound loop
# ════════════════════════════════════════════════════════════════════════════════════
#
# The known LLM-calling helpers invoked in the re_embedder's per-tenant growth loops. Each MUST
# be reached only after the enclosing `for … in ready_schemas` iteration has bound the tenant
# brain. This asserts the wiring itself, so a future edit that drops a bind fails loudly here
# rather than silently re-introducing the global-default fallthrough.
_LLM_LOOP_CALLERS = [
    "evaluate_ontology_candidates(",
    "converge_lifted_synonyms(",
    "classify_unknown_concepts(",
    "climb_classification_chains(",
    "_llm_discover_taxonomy_from_facts(",
    "resolve_name_conflicts(",
    "generate_rel_type_phrasing(",
]


def _embedder_lines():
    return Path(embedder.__file__).read_text().splitlines()


def _enclosing_loop_start(lines, call_idx):
    """Return the line index of the nearest preceding `for … in ready_schemas` loop header."""
    for i in range(call_idx, -1, -1):
        if re.search(r"for .*in \(?ready_schemas", lines[i]):
            return i
    return None


def test_structural_every_background_llm_call_is_brain_bound():
    lines = _embedder_lines()
    problems = []
    for caller in _LLM_LOOP_CALLERS:
        # Find every INVOCATION (skip the `def …` definition line).
        call_idxs = [
            i for i, ln in enumerate(lines)
            if caller in ln and not ln.lstrip().startswith("def ")
        ]
        # These helpers are called from within the main() per-tenant loops; require at least one.
        assert call_idxs, f"expected to find a call site for {caller!r}"
        for idx in call_idxs:
            loop_start = _enclosing_loop_start(lines, idx)
            if loop_start is None:
                problems.append(f"{caller} @L{idx+1}: no enclosing ready_schemas loop found")
                continue
            region = "\n".join(lines[loop_start:idx])
            if "_reembedder_bind_tenant_brain(" not in region:
                problems.append(
                    f"{caller} @L{idx+1}: no _reembedder_bind_tenant_brain bind between the "
                    f"loop header @L{loop_start+1} and the call"
                )
    assert not problems, "background LLM call(s) not tenant-brain-bound:\n" + "\n".join(problems)


def test_structural_primary_loop_and_final_clear_present():
    """The primary sync loop binds, and the cycle clears the brain back to global afterward."""
    src = Path(embedder.__file__).read_text()
    assert src.count("_reembedder_bind_tenant_brain(") >= 5, "expected binds in the sync loop + each LLM growth loop"
    assert "_reembedder_clear_tenant_brain()" in src
