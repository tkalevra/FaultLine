"""Re_embedder background LLM calls: PER-TENANT identity + VALIDATE-BEFORE-SPEND.

THE BUG this pins (measured on production, 2026-08-06)
──────────────────────────────────────────────────────
Seven background LLM call sites in ``src/re_embedder/embedder.py`` passed a hardcoded
``user_id="re_embedder"``. That identity owns no account —
``SELECT count(*) FROM public.accounts WHERE name='re_embedder' OR slug='re_embedder'`` → 0 —
so every one of those calls was ATTRIBUTED to a seat that cannot exist:

    [warning] llm_call.saas_no_brain_failclosed mode=sync_retry operation=NATURAL_LANGUAGE_FILL
    [info]    brain_probe elapsed_s=None endpoint=(unset) lane=NATURAL_LANGUAGE_FILL
              op=NATURAL_LANGUAGE_FILL outcome=fail reason=no_brain seat=re_embed

``seat=re_embed`` is ``uid[:8]`` of the literal. Two distinct defects sit under that line:

  1. ATTRIBUTION. The health sinks resolve a seat's schema through
     ``public.user_provisioning.user_id`` (a UUID column), so the tenant's OWN failures were
     built, buffered and then dropped — their endpoint-health card never saw the background
     lane at all. ``brain_health``'s entry guard (``len(uid) < 8``) let an 11-character
     non-uuid through a filter plainly meant to reject junk.
  2. SPEND. The sweep ledger answers "did this seat's INPUT change?" — it cannot answer "can
     this seat's brain be CALLED?". A seat with no brain is PERMANENTLY dirty (its rel_types
     stay un-phrased), so every LLM-bearing subsystem walked its input and fail-closed once
     per row, every cycle, forever.

What is pinned here:
  1. per-tenant binding — the seat is derived from the bound schema and IS the identity every
     background LLM call is made as (both the module-level and the lazily-imported call sites);
  2. validate-before-spend — the pre-flight closes for a no-brain seat, so the LLM stack is
     never reached, and the ledger is deliberately NOT recorded so the work stays due;
  3. fail-closed is UNCHANGED — a no-brain tenant still returns the safe empty default and
     makes zero network calls, and the gate only ever REFUSES work the stack would refuse;
  4. SAAS off — identity still derived, gate always open, byte-for-byte the OSS path.

Pure unit/seam tests: DB + network seams monkeypatched, no live services.

Run: python3 tools/fltest.py --bug REEMBED-SEAT-SPEND \
         --test tests/test_reembedder_per_tenant_brain_spend.py
"""

import re
from pathlib import Path

import pytest

from saas import brain_health
from saas import llm_config as saas_llm_config
from saas import llm_context as saas_llm_context

from src.api import llm_calls
from src.re_embedder import embedder
from src.re_embedder import sweep_ledger as SL


SEAT_T = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SCHEMA_T = "faultline_" + SEAT_T.replace("-", "_")
ACCT_T = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
TENANT_CFG = {
    "account_id": ACCT_T,
    "backend_type": "openai",
    "base_url": "https://tenant-t.cerebras.example",
    "model": "tenant-t-model",
    "api_key": "tenant-t-secret",
}

# One LLM-bearing subsystem and one deterministic one, taken from the registry rather than
# spelled out, so this file cannot drift from sweep_ledger.SUBSYSTEMS.
LLM_SUBSYSTEM = "orphan_stub_and_nl_fill"
DETERMINISTIC_SUBSYSTEM = "suspect_preferred_names"


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    monkeypatch.delenv("REEMBED_LLM_BRAIN_PREFLIGHT", raising=False)
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    embedder._reembedder_clear_tenant_brain()
    llm_calls._HEALTH_SINK_MISSING = False
    yield
    saas_llm_config.invalidate_config_cache()
    saas_llm_context.invalidate_schema_account_cache()
    saas_llm_context._current_llm_config.set(None)
    embedder._reembedder_clear_tenant_brain()


def _wire_account(monkeypatch, schema_to_account: dict, account_to_cfg: dict):
    """Patch the three live control-plane seams the bind reads (see the same helper in
    tests/test_saas_reembedder_brain_bind.py — patching only ``_account_for_schema`` went dead
    when the seat-brain refactor changed which seam is read)."""
    monkeypatch.setattr(
        saas_llm_context, "_account_for_schema",
        lambda schema: schema_to_account.get(schema))
    monkeypatch.setattr(
        saas_llm_context, "_seat_for_schema",
        lambda schema: (None, schema_to_account.get(schema)))
    monkeypatch.setattr(
        saas_llm_config, "resolve_account_llm_config",
        lambda account_id: account_to_cfg.get(account_id))
    from saas import seat_brain as saas_seat_brain
    monkeypatch.setattr(saas_seat_brain, "seat_scoped", lambda account_id: False)


def _capture_health(monkeypatch):
    """Capture every `_health_note` tee so a test can assert WHO a signal was recorded for."""
    seen = []
    monkeypatch.setattr(
        llm_calls, "_health_note",
        lambda user_id, metric, operation="*", **kw: seen.append(
            (user_id, metric, operation, kw.get("detail"))))
    return seen


def _capture_window(monkeypatch):
    """Capture the ROLLING WINDOW recorder the spend pre-flight writes to (deliberately not the
    last-write-wins alarm stream — see the ⚠️ on _reembedder_llm_lane_open)."""
    seen = []
    monkeypatch.setattr(
        brain_health, "record",
        lambda user_id, metric, operation="*", **kw: seen.append((user_id, metric, operation)))
    monkeypatch.setattr(
        brain_health, "probe_event",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
            "the spend pre-flight must NOT write the last-write-wins alarm stream: a refusal "
            "has no success twin to clear it, so the seat's card would stay red forever")))
    return seen


# ══════════════════════════════════════════════════════════════════════════════════════
# 1 — PER-TENANT: the bound schema IS the identity
# ══════════════════════════════════════════════════════════════════════════════════════

def test_seat_is_derived_from_the_bound_schema(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: TENANT_CFG})

    assert embedder._reembedder_llm_user_id() == "re_embedder"   # nothing bound yet
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    assert embedder._reembedder_llm_user_id() == SEAT_T
    # The clear that prevents a cross-tenant brain leak clears the identity with it — one
    # helper, two facts, so endpoint and attribution can never be bound out of step.
    embedder._reembedder_clear_tenant_brain()
    assert embedder._reembedder_llm_user_id() == "re_embedder"


def test_non_tenant_schema_yields_no_seat_not_a_junk_one():
    """A cortex firewall schema / a legacy name / garbage must resolve to NO seat, never to a
    plausible-looking string that would sail through a health-sink filter."""
    assert embedder._seat_user_id_for_schema(SCHEMA_T) == SEAT_T
    assert embedder._seat_user_id_for_schema("flagent_" + SEAT_T.replace("-", "_")) is None
    assert embedder._seat_user_id_for_schema("faultline_tenant_t") is None
    assert embedder._seat_user_id_for_schema("public") is None
    assert embedder._seat_user_id_for_schema("") is None
    assert embedder._seat_user_id_for_schema(None) is None


def test_module_level_call_site_is_attributed_to_the_tenant(monkeypatch):
    """`_query_llm_for_rel_type_metadata` uses the module-level import of the LLM stack."""
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")   # LLMModels refuses to guess a name
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: TENANT_CFG})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    seen = {}
    monkeypatch.setattr(
        embedder, "call_llm_with_retry_sync",
        lambda **kw: seen.update(kw) or {"natural_language": "X relates to Y"})

    embedder._query_llm_for_rel_type_metadata("marry", "Person", "Person", "a b", "http://x")
    assert seen["user_id"] == SEAT_T


def test_lazily_imported_call_site_is_attributed_to_the_tenant(monkeypatch):
    """`_query_llm_what_is` imports the LLM stack INSIDE the function, so the seam that has to
    be patched is `llm_calls`, not the embedder namespace. Both shapes exist in this file and
    both must carry the seat."""
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: TENANT_CFG})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    seen = {}
    monkeypatch.setattr(
        llm_calls, "call_llm_with_retry_sync",
        lambda **kw: seen.update(kw) or {"entity_type": "Object", "parent": None})

    embedder._query_llm_what_is("router", "http://x")
    assert seen["user_id"] == SEAT_T


def test_no_hardcoded_sentinel_identity_remains_at_a_call_site():
    """STRUCTURAL: the literal must not come back. It is still legal as the no-seat FALLBACK
    constant, so the assertion is on the `user_id=` argument shape specifically."""
    src = Path(embedder.__file__).read_text()
    assert 'user_id="re_embedder"' not in src
    assert "user_id='re_embedder'" not in src
    assert '_NO_SEAT_USER_ID = "re_embedder"' in src


# ══════════════════════════════════════════════════════════════════════════════════════
# 2 — VALIDATE BEFORE SPEND: the gate stops the call that could not have landed
# ══════════════════════════════════════════════════════════════════════════════════════

def test_gate_closes_for_a_seat_with_no_brain(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    # A REAL account that has no brain configured → the no-brain sentinel is bound.
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})
    seen = _capture_window(monkeypatch)

    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    assert llm_calls._saas_brain_unavailable() is True
    assert embedder._reembedder_llm_lane_open(SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is False

    # RECORDED — under the REAL seat, on the engine-sweep operation, in the WINDOW aggregate.
    assert seen == [(SEAT_T, brain_health.M_NO_BRAIN, embedder._BRAIN_PREFLIGHT_OP)]
    assert embedder._BRAIN_PREFLIGHT_OP == brain_health.OP_ENGINE_SWEEP


def test_gate_prevents_the_llm_call_entirely(monkeypatch):
    """The point of the gate: the LLM stack is not merely refused, it is never entered."""
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})

    def _boom(*a, **k):
        raise AssertionError("spend pre-flight breached: the LLM stack was entered")

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", _boom)
    monkeypatch.setattr(embedder, "call_llm_with_retry_sync", _boom)

    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    snap = SL.Snapshot({SEAT_T: {LLM_SUBSYSTEM: 3}})     # ledger says DUE (dirty seat)
    assert SL.claim(snap, SEAT_T, LLM_SUBSYSTEM) is True
    # ...and the combined gate still refuses, so the caller never reaches _boom.
    assert embedder._reembedder_claim(snap, SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is False


def test_gate_opens_when_the_tenant_has_a_brain(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: TENANT_CFG})
    seen = _capture_window(monkeypatch)

    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    assert embedder._reembedder_llm_lane_open(SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is True
    assert seen == []          # nothing to report: the lane is healthy


def test_deterministic_subsystems_are_never_gated_on_a_brain(monkeypatch):
    """The pre-flight guards SPEND. A subsystem with no LLM call has none to guard, and gating
    it would stop free, useful work for a configuration problem it does not depend on."""
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    assert SL.SUBSYSTEMS[DETERMINISTIC_SUBSYSTEM]["llm"] is False
    assert embedder._reembedder_llm_lane_open(
        SEAT_T, DETERMINISTIC_SUBSYSTEM, SCHEMA_T) is True
    # ...and every registry entry marked llm=False behaves the same way.
    for name, spec in SL.SUBSYSTEMS.items():
        if not spec["llm"]:
            assert embedder._reembedder_llm_lane_open(SEAT_T, name, SCHEMA_T) is True, name


def test_ledger_still_skips_a_clean_seat_before_the_brain_is_consulted(monkeypatch):
    """Order of the two gates: a CLEAN seat is skipped by the ledger and never reaches the
    pre-flight, so a clean seat costs nothing extra."""
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("REEMBED_SWEEP_SKIP", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: TENANT_CFG})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    monkeypatch.setattr(
        embedder, "_reembedder_llm_lane_open",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pre-flight ran for a ledger-clean seat")))
    assert embedder._reembedder_claim(
        SL.Snapshot({}), SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is False


def test_gate_can_be_switched_off_per_deployment(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("REEMBED_LLM_BRAIN_PREFLIGHT", "false")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    assert embedder._reembedder_llm_lane_open(SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is True


def test_gate_fails_safe_towards_running(monkeypatch):
    """sweep_ledger invariant 1, extended: skipping needed work is a correctness bug, running
    unneeded work is only waste. A broken pre-flight must RUN."""
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    monkeypatch.setattr(
        llm_calls, "_saas_brain_unavailable",
        lambda: (_ for _ in ()).throw(RuntimeError("control plane down")))
    assert embedder._reembedder_llm_lane_open(SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is True


def test_every_gate_sits_after_its_tenant_brain_bind():
    """STRUCTURAL, and the one ordering that would fail SILENTLY: the pre-flight reads the brain
    the loop just bound. A gate placed BEFORE the bind would judge the PREVIOUS tenant — skipping
    a healthy seat, or admitting one with no brain — and nothing would look wrong."""
    lines = Path(embedder.__file__).read_text().splitlines()
    problems = []
    for i, ln in enumerate(lines):
        if not re.search(r"_reembedder_(claim|llm_lane_open)\(", ln):
            continue
        if ln.lstrip().startswith(("def ", "#")):
            continue
        start = next((j for j in range(i, -1, -1)
                      if re.search(r"for .*in \(?ready_schemas", lines[j])), None)
        if start is None:
            problems.append(f"L{i+1}: no enclosing ready_schemas loop")
            continue
        if "_reembedder_bind_tenant_brain(" not in "\n".join(lines[start:i]):
            problems.append(f"L{i+1}: gate is not preceded by a tenant-brain bind "
                            f"(loop header @L{start+1})")
    assert not problems, "spend gate evaluated against the wrong tenant:\n" + "\n".join(problems)


def test_a_gated_skip_never_records_the_ledger_run():
    """STRUCTURAL: `record_run` marks work DONE. Recording it on a no-brain skip would hide the
    still-owed work for up to REEMBED_SWEEP_MAX_INTERVAL after the tenant adds a brain. Every
    ledger-recording site must therefore sit behind the same flag the gate sets."""
    lines = Path(embedder.__file__).read_text().splitlines()
    problems = []
    for i, ln in enumerate(lines):
        if "_sweep.record_run(" not in ln or ln.lstrip().startswith("#"):
            continue
        # Walk back to the enclosing per-tenant loop header; somewhere between it and the
        # record there must be a gate — either the `_sw_run` / `_nl_lane_open` flag the
        # combined claim sets, or the pre-flight's own `continue` guard (taxonomy discovery,
        # whose ledger claim runs before its bind and so cannot be folded into
        # `_reembedder_claim`). An unrecognised shape is a finding, not a pass.
        # (`record_run_dsn` in the brain_alerts lane carries its own claim and is excluded.)
        start = next((j for j in range(i, -1, -1)
                      if re.search(r"for .*in \(?ready_schemas", lines[j])), max(0, i - 8))
        region = "\n".join(lines[start:i])
        if not re.search(r"if (_sw_run|_nl_lane_open)\b|_reembedder_llm_lane_open\(", region):
            problems.append(f"L{i+1}: {ln.strip()[:90]}")
    assert not problems, (
        "sweep_ledger run recorded without passing the claim/pre-flight gate:\n"
        + "\n".join(problems))


# ══════════════════════════════════════════════════════════════════════════════════════
# 3 — FAIL-CLOSED IS UNCHANGED (the gate may only ever refuse MORE, never less)
# ══════════════════════════════════════════════════════════════════════════════════════

def test_no_brain_tenant_still_fails_closed_with_zero_network(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "true")
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.setenv("LLM_BASE_URL", "http://open-webui:8080")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})

    def _boom(*a, **k):
        raise AssertionError("fail-closed breached: a network path was reached")

    monkeypatch.setattr(llm_calls, "_get_endpoint_list", _boom)
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    # Even if a caller reaches the stack anyway (pre-flight off, or a lane we have not gated),
    # the stack itself still refuses and spends nothing — the gate is defence in depth, not a
    # replacement for the fail-closed contract.
    assert llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "user", "content": "what is a router"}],
        model="global-enrichment-model",
        user_id=embedder._reembedder_llm_user_id(),
        operation="ENRICHMENT") == {}


def test_the_failclosed_refusal_is_now_attributed_to_the_real_seat(monkeypatch):
    """This is the prod log line being fixed: `seat=re_embed` becomes the tenant's own uuid, so
    the signal reaches the seat's endpoint-health card instead of a seat that cannot exist."""
    monkeypatch.setenv("SAAS_MODE", "true")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})
    seen = _capture_health(monkeypatch)
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)

    llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "user", "content": "x"}], model="m",
        user_id=embedder._reembedder_llm_user_id(),
        operation="NATURAL_LANGUAGE_FILL")
    assert (SEAT_T, "no_brain", "NATURAL_LANGUAGE_FILL", None) in seen


def test_brain_health_rejects_a_non_uuid_seat_and_keeps_real_ones(monkeypatch):
    """The `len(uid) < 8` guard let an 11-character non-uuid through. A seat id is a UUID —
    the same shape both flushes already require via public.user_provisioning.user_id."""
    monkeypatch.setenv("SAAS_MODE", "true")
    assert brain_health._is_seat_uid(SEAT_T) is True
    assert brain_health._is_seat_uid("re_embedder") is False
    assert brain_health._is_seat_uid("anonymous") is False
    assert brain_health._is_seat_uid("engine") is False
    assert brain_health._is_seat_uid("") is False

    brain_health._buf.clear()
    brain_health._last_events.clear()
    brain_health.record("re_embedder", brain_health.M_NO_BRAIN, operation="ENRICHMENT")
    brain_health.probe_event("re_embedder", "ENRICHMENT", brain_health.OUTCOME_FAIL,
                             reason=brain_health.REASON_NO_BRAIN)
    assert brain_health._buf == {} and brain_health._last_events == {}

    brain_health.record(SEAT_T, brain_health.M_NO_BRAIN, operation="ENRICHMENT")
    brain_health.probe_event(SEAT_T, "ENRICHMENT", brain_health.OUTCOME_FAIL,
                             reason=brain_health.REASON_NO_BRAIN)
    assert any(k[0] == SEAT_T for k in brain_health._buf)
    assert (SEAT_T, "ENRICHMENT") in brain_health._last_events
    brain_health._buf.clear()
    brain_health._last_events.clear()


# ══════════════════════════════════════════════════════════════════════════════════════
# 4 — SAAS OFF: byte-for-byte the OSS path
# ══════════════════════════════════════════════════════════════════════════════════════

def test_saas_off_gate_is_always_open(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("LLM_BACKEND_TYPE", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    _wire_account(monkeypatch, {SCHEMA_T: ACCT_T}, {ACCT_T: None})

    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    # No tenant brain is bound (the bind is a no-op with SAAS off) and the global env path is
    # the legitimate answer, so the pre-flight must not stand in front of it.
    assert saas_llm_context.current_llm_config() is None
    assert llm_calls._saas_brain_unavailable() is False
    for name in SL.SUBSYSTEMS:
        assert embedder._reembedder_llm_lane_open(SEAT_T, name, SCHEMA_T) is True, name
    assert embedder._reembedder_claim(
        SL.Snapshot({}, always_run=True), SEAT_T, LLM_SUBSYSTEM, SCHEMA_T) is True


def test_saas_off_still_attributes_per_tenant_and_still_reaches_the_llm(monkeypatch):
    """A single-tenant / OSS deployment still iterates per-user schemas, so the seat is still
    the right attribution — and, critically, the call still HAPPENS."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    embedder._reembedder_bind_tenant_brain(SCHEMA_T)
    assert embedder._reembedder_llm_user_id() == SEAT_T

    seen = {}
    monkeypatch.setattr(
        llm_calls, "call_llm_with_retry_sync",
        lambda **kw: seen.update(kw) or {"entity_type": "Object", "parent": None})
    assert embedder._query_llm_what_is("router", "http://x") is not None
    assert seen["user_id"] == SEAT_T


def test_saas_off_health_sink_stays_a_noop(monkeypatch):
    """The tightened guard must not resurrect the SAAS-off no-op contract in reverse."""
    monkeypatch.delenv("SAAS_MODE", raising=False)
    brain_health._buf.clear()
    brain_health.record(SEAT_T, brain_health.M_NO_BRAIN, operation="ENRICHMENT")
    assert brain_health._buf == {}
