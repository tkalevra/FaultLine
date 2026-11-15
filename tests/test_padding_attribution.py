"""A pad from a FAILED RESOLUTION must be distinguishable in ordinary output from a pad
a provider GENUinely forces — same pad, two different facts.

resolve_reasoning_controls returns can_disable=False for two populations:
  * KNOWN host, recorded spec says reasoning cannot be turned off (z.ai coding surface
    with glm-5.2 — the live pre-prod brain; Cerebras gpt-oss; Grok) — a provider
    CAPABILITY, "unavoidable" is an honest word;
  * UNRECOGNIZED host + a model name that says it reasons — resolution FAILED, no
    suppression field could even be sent, and the headroom pad is a PRECAUTION
    (measured on this deployment: LAN llama.cpp brains at http://192.168.40.20:8080
    serving Qwen/GLM weights resolve unknown).

Before this lane, all three surfaces (the per-call audit log, the Test & calibrate
note, the standing console banner) rendered the SECOND case with the FIRST case's
words — "this model cannot disable reasoning", "UNAVOIDABLE on this provider", "this
provider offers no way to turn them off" — each an assertion about a provider's
capability that nobody established. The distinction is visibility at true strength:
info-level, severity warn, no alarm; the pad itself is the safe direction and stays.

Every test here goes red if the resolution-status split is removed (see the mutation
notes per test).
"""
import src.api.llm_calls as C
import src.api.llm_client as CL


GENUINE = {"base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.2"}
UNRESOLVED = {"base_url": "http://192.168.40.20:8080/v1", "model": "glm-5.2"}


def _brain(cfg):
    return lambda: {"base_url": cfg["base_url"], "backend_type": "openai"}


def _leaked(reasoning=440):
    return {"choices": [{"message": {"content": "[]"}}],
            "usage": {"completion_tokens": 488,
                      "completion_tokens_details": {"reasoning_tokens": reasoning}}}


def _audit_events(monkeypatch, cfg, **kw):
    events = []
    monkeypatch.setattr(C.log, "info", lambda msg, **k: events.append((msg, k)))
    monkeypatch.setattr(CL, "_tenant_brain", _brain(cfg))
    C._audit_reasoning_leak(_leaked(), "EXTRACT", "u1", expected=True,
                            budget_padded=kw.get("padded", 1024),
                            model=cfg["model"])
    assert events, "the expected-case audit must log"
    return events[0]


# ── surface 1: the per-call audit log (ordinary output) ────────────────────────────


def test_audit_genuine_cannot_disable_keeps_the_strong_words(monkeypatch):
    """KNOWN host → the strong claim is honest and stays: UNAVOIDABLE, cannot disable.
    RED if the split is removed by collapsing both cases to the strong wording (the
    unresolved test catches the other direction)."""
    msg, kw = _audit_events(monkeypatch, GENUINE)
    assert msg == "llm_reasoning.expected"
    assert "cannot disable reasoning" in kw["note"]
    assert "UNAVOIDABLE" in kw["note"]
    assert kw["resolution"] == "known"
    assert "UNRESOLVED" not in kw["note"]


def test_audit_resolution_failure_states_the_precaution_not_a_capability(monkeypatch):
    """UNKNOWN host (the live LAN-llama.cpp case) → the note must say resolution FAILED
    and the pad is a PRECAUTION, and must NOT claim the provider cannot disable
    reasoning — that capability was never established. RED if the split is removed
    (this case renders with the genuine-case wording)."""
    msg, kw = _audit_events(monkeypatch, UNRESOLVED)
    assert msg == "llm_reasoning.expected"
    assert "could not be resolved" in kw["note"], (
        "the note must name the resolution failure")
    assert "PRECAUTION" in kw["note"]
    assert "UNRESOLVED" in kw["note"]
    assert "UNAVOIDABLE" not in kw["note"], (
        "asserting the spend is unavoidable claims a provider capability nobody "
        "established — resolution FAILED here, the capability question is open")
    assert kw["resolution"] == "unresolved"
    assert kw["resolution_source"] == "192.168.40.20"


def test_audit_resolution_failure_is_info_never_crit(monkeypatch):
    """True strength, no alarm: the unresolved pad logs at INFO and fires no CRIT —
    the pad is the safe direction; visibility, not a fire alarm. RED if someone
    escalates the unresolved case to log_crit."""
    crits = []
    import src.api.logging_config as lc
    monkeypatch.setattr(lc, "log_crit", lambda logger, msg, **k: crits.append(msg))
    _audit_events(monkeypatch, UNRESOLVED)
    assert not crits


def test_audit_zero_pad_unresolved_case_admits_it(monkeypatch):
    """Headroom 0 + resolution failure → the note must say NOT padded, not claim a
    precautionary pad that did not happen."""
    _, kw = _audit_events(monkeypatch, UNRESOLVED, padded=0)
    assert "NOT padded" in kw["note"]


def test_both_causes_pad_the_wire_budget_equally(monkeypatch):
    """The pad is IDENTICAL for both causes (the safe direction, unchanged) — which is
    exactly why the NOTE is the only place the two facts can be told apart. RED if
    either cause stops padding (the distinction would become cosmetic)."""
    for cfg in (GENUINE, UNRESOLVED):
        assert CL.resolve_reasoning_controls(
            cfg["base_url"], cfg["model"])["inflate_budget"] is True, cfg


# ── surface 2: the Test & calibrate probe note ─────────────────────────────────────


def _probe(monkeypatch, cfg):
    from saas import llm_admin

    class _Ctx:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("saas.llm_context.bind_explicit_llm_config", lambda c: _Ctx())
    monkeypatch.setattr("saas.llm_context.reset_llm_config", lambda t: None)

    def _fake_call(**kw):
        C._last_usage.usage = {"completion_tokens": 500, "reasoning_tokens": 412,
                               "operation": "EXTRACTION"}
        return {"triples": [{"subject": "A", "relation": "r", "object": "B"}]}

    monkeypatch.setattr(C, "call_llm_with_retry_sync", _fake_call)
    return llm_admin._probe_extraction("acct", cfg)["reasoning_cost"]


def test_probe_note_genuine_keeps_capability_claim(monkeypatch):
    rc = _probe(monkeypatch, GENUINE)
    assert rc["resolution"] == "known"
    assert "cannot disable reasoning" in rc["note"]
    assert "cannot be turned off on this provider" in rc["note"]


def test_probe_note_unresolved_names_the_real_cause(monkeypatch):
    """RED if the probe renders the unresolved case with the capability claim."""
    rc = _probe(monkeypatch, UNRESOLVED)
    assert rc["resolution"] == "unresolved"
    assert "UNRESOLVED" in rc["note"]
    assert "192.168.40.20" in rc["note"]
    assert "cannot be turned off on this provider" not in rc["note"], (
        "the probe must not assert a capability nobody established")
    assert rc["reasoning_tokens"] == 412  # the measured spend is still stated


# ── surface 3: the standing console banner ──────────────────────────────────────────


def test_banner_genuine_unchanged():
    from saas import reasoning_ticket as rt
    b = rt.build_reasoning_banner("api.z.ai", "glm-5.2", "reason", admin=False)
    assert b["title"] == "Your model always reasons"
    assert "offers no way to turn them off" in b["body"]


def test_banner_unresolved_states_the_real_fact():
    """The STANDING statement a tenant reads must not claim the provider cannot
    disable reasoning when FaultLine simply could not resolve the field. RED if the
    unresolved variant is dropped (the generic body renders for the LAN brain)."""
    from saas import reasoning_ticket as rt
    forced = rt.reasoning_forced(UNRESOLVED["base_url"], UNRESOLVED["model"])
    assert forced and forced["unresolved"] is True
    assert "UNRESOLVED" in forced["reason"]
    b = rt.build_reasoning_banner(forced["provider"], forced["model"],
                                  forced["reason"], admin=True,
                                  unresolved=forced["unresolved"])
    assert b["title"] == "FaultLine cannot suppress reasoning on this endpoint"
    assert "does not recognize endpoint" in b["body"]
    assert "UNRESOLVED" in b["body"]
    assert "offers no way to turn them off" not in b["body"]
    assert b["severity"] == "warn", "true strength: visibility, not an alarm"


def test_banner_genuine_forced_has_no_unresolved_flag():
    from saas import reasoning_ticket as rt
    forced = rt.reasoning_forced(GENUINE["base_url"], GENUINE["model"])
    assert forced and forced["unresolved"] is False
