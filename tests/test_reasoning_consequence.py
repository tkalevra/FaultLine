"""dprompt-156 consequence tests: a detection must CHANGE BEHAVIOUR, and be reported at its
true strength.

Three gaps, one cause (detection without consequence):

1. BUDGET ON THE WIRE. ``resolve_reasoning_controls`` exposes ``inflate_budget`` — the flag
   whose NAME is "the completion budget must be padded". The headroom consumer must gate on
   THAT flag, and the padding must be observable in the PAYLOAD that goes on the wire: a
   cannot-disable model gets a strictly larger ``max_tokens`` than the same operation on a
   can-disable model. Before this lane, the consumer gated on a different key and the
   live z.ai coding-surface brain (glm-5.2) resolved can-disable on the branch — its budget
   went out UNPADDED while its tokens were being spent (19–711 reasoning tokens/call,
   measured live on pre-prod 2026-08-18).

2. THE AUDIT STATES FACTS, NOT INTENT. ``_audit_reasoning_leak(expected=True)`` used to log
   "the completion budget was padded" unconditionally — even when headroom was 0 or the
   resolver failed and NOTHING was padded. The note now carries the measured delta.

3. THE COST IS A FIRST-CLASS METRIC. A cannot-disable brain's reasoning spend is counted in
   the brain-health window (``reasoning_overhead``) and surfaced in seat metrics — so a quiet
   alarm can never again be read as a solved problem.
"""
import pytest
from unittest import mock

import src.api.llm_calls as C
import src.api.llm_client as CL


# ── 1. the wire budget is genuinely larger for a cannot-disable model ─────────────────────

class _FakeResp:
    status_code = 200
    headers: dict = {}

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "{\"atoms\": []}"}}],
                "usage": {"completion_tokens": 10,
                          "completion_tokens_details": {"reasoning_tokens": 0}}}


def _wire_budget(monkeypatch, base_url: str, model: str, operation: str = "REFRAME") -> int:
    """Drive the REAL sync call path with a mocked transport; return the payload's max_tokens."""
    captured = {}

    def fake_post_sync(client, endpoint, json=None, headers=None, timeout=None):
        captured.update(json or {})
        return _FakeResp()

    monkeypatch.setattr(CL, "_tenant_brain",
                        lambda: {"base_url": base_url, "backend_type": "openai"})
    monkeypatch.setattr(C.llm_source_ip, "post_sync", fake_post_sync)
    monkeypatch.setattr(C, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(C, "_get_endpoint_list", lambda: [base_url + "/chat/completions"])
    C.call_llm_with_retry_sync(
        [{"role": "user", "content": "I got a new router installed and it works."}],
        model, user_id="probe", operation=operation, max_retries=1,
    )
    return int(captured.get("max_tokens", -1))


CANNOT_DISABLE = [  # (base_url, model) — measured/documented cannot-disable brains
    ("https://api.z.ai/api/coding/paas/v4", "glm-5.2"),   # live pre-prod brain (coding surface)
    ("https://api.cerebras.ai", "gpt-oss-120b"),          # dprompt-156 original case
    ("https://api.x.ai", "grok-4.5"),
]
CAN_DISABLE = [
    ("https://api.deepseek.com", "deepseek-v4-flash"),
    ("https://api.z.ai/api/paas/v4", "glm-5.2"),          # same host+model, STANDARD surface
]


@pytest.mark.parametrize("base_url,model", CANNOT_DISABLE)
def test_cannot_disable_model_gets_larger_wire_budget(monkeypatch, base_url, model):
    controls = CL.resolve_reasoning_controls(base_url, model)
    assert controls["can_disable"] is False and controls["inflate_budget"] is True
    headroom = C._reasoning_token_headroom()
    got = _wire_budget(monkeypatch, base_url, model)
    base = C.LLMMaxTokens.get("REFRAME")
    assert got == base + headroom, (
        f"cannot-disable {model} on {base_url}: wire max_tokens {got} != base {base} + "
        f"headroom {headroom} — the detection did not change the budget")


@pytest.mark.parametrize("base_url,model", CAN_DISABLE)
def test_can_disable_model_budget_is_untouched(monkeypatch, base_url, model):
    controls = CL.resolve_reasoning_controls(base_url, model)
    assert controls["can_disable"] is True and controls["inflate_budget"] is False
    got = _wire_budget(monkeypatch, base_url, model)
    assert got == C.LLMMaxTokens.get("REFRAME"), (
        f"can-disable {model} on {base_url}: wire budget {got} was padded — headroom leaked "
        f"onto a model that does not need it")


def test_cannot_disable_budget_strictly_exceeds_can_disable(monkeypatch):
    """THE BAR, in one assertion: same operation, same base budget — the cannot-disable brain
    demonstrably receives a larger completion budget than the can-disable one."""
    cannot = _wire_budget(monkeypatch, "https://api.cerebras.ai", "gpt-oss-120b")
    can = _wire_budget(monkeypatch, "https://api.deepseek.com", "deepseek-v4-flash")
    assert cannot > can, (
        f"cannot-disable budget {cannot} is not larger than can-disable budget {can}")


def test_headroom_consumer_gates_on_inflate_budget_flag(monkeypatch):
    """The consumer reads THE FLAG THAT NAMES THE BEHAVIOUR.

    Regression pin for the original gap: the consumer gated on a different key than the one
    that exists to describe this exact behaviour. If resolve_reasoning_controls ever reports
    inflate_budget=False while can_disable=False (or vice versa), the budget must follow
    inflate_budget — it is the contract.
    """
    monkeypatch.setattr(CL, "_tenant_brain",
                        lambda: {"base_url": "https://api.example.com", "backend_type": "openai"})
    with mock.patch.object(CL, "resolve_reasoning_controls",
                           return_value={"fields": {}, "can_disable": False,
                                         "inflate_budget": False, "known": True}):
        assert C._apply_reasoning_headroom("whatever", 256) == 256, (
            "inflate_budget=False must mean NO padding, even if can_disable is also False")
    with mock.patch.object(CL, "resolve_reasoning_controls",
                           return_value={"fields": {}, "can_disable": True,
                                         "inflate_budget": True, "known": True}):
        assert C._apply_reasoning_headroom("whatever", 256) == 256 + C._reasoning_token_headroom(), (
            "inflate_budget=True must mean padding, whatever can_disable says")


def test_headroom_env_override_roundtrip(monkeypatch):
    monkeypatch.setattr(CL, "_tenant_brain",
                        lambda: {"base_url": "https://api.cerebras.ai", "backend_type": "openai"})
    monkeypatch.setenv("LLM_REASONING_TOKEN_HEADROOM", "0")
    assert C._apply_reasoning_headroom("gpt-oss-120b", 256) == 256, "0 must disable padding"
    monkeypatch.setenv("LLM_REASONING_TOKEN_HEADROOM", "64")
    assert C._apply_reasoning_headroom("gpt-oss-120b", 256) == 320


def test_zai_coding_vs_standard_surface(monkeypatch):
    """The live pre-prod brain's exact case: SAME host, SAME model — the coding-plan path
    cannot disable reasoning (budget padded, no suppression field), the standard path can
    (budget untouched, thinking disabled). The surface distinction is not cosmetic: it is the
    difference between padding and not."""
    coding = _wire_budget(monkeypatch, "https://api.z.ai/api/coding/paas/v4", "glm-5.2")
    standard = _wire_budget(monkeypatch, "https://api.z.ai/api/paas/v4", "glm-5.2")
    assert coding == standard + C._reasoning_token_headroom()


# ── 2. the audit states the measured delta, never an assumption ──────────────────────────

def _leaked_result(reasoning=440):
    return {"choices": [{"message": {"content": "[]"}}],
            "usage": {"completion_tokens": 488,
                      "completion_tokens_details": {"reasoning_tokens": reasoning}}}


def test_audit_expected_note_reports_actual_padding(monkeypatch):
    """budget_padded>0 → the note STATES the number; the health meter counts the call."""
    events = []
    monkeypatch.setattr(C.log, "info", lambda msg, **kw: events.append((msg, kw)))
    C._audit_reasoning_leak(_leaked_result(), "EXTRACT", "u1",
                            expected=True, budget_padded=1024)
    assert events, "expected-case audit must log"
    msg, kw = events[0]
    assert msg == "llm_reasoning.expected"
    assert "padded by 1024 tokens" in kw["note"]
    assert kw["budget_padded"] == 1024
    assert "UNAVOIDABLE" in kw["note"]


def test_audit_expected_note_admits_zero_padding(monkeypatch):
    """Headroom 0 or resolution failure → the note must NOT claim padding happened."""
    events = []
    monkeypatch.setattr(C.log, "info", lambda msg, **kw: events.append((msg, kw)))
    C._audit_reasoning_leak(_leaked_result(), "EXTRACT", "u1",
                            expected=True, budget_padded=0)
    assert events and "NOT padded" in events[0][1]["note"], (
        "the note must say the budget was NOT padded rather than asserting padding that "
        "did not happen — an unstated claim here is the exact overstatement this lane removes")


def test_audit_expected_reasoning_health_note_counted(monkeypatch):
    """Each expected-reasoning call tees ONE reasoning_overhead note to brain health."""
    seen = []
    monkeypatch.setattr(C, "_health_note",
                        lambda uid, metric, op="*", **kw: seen.append((uid, metric, op)))
    C._audit_reasoning_leak(_leaked_result(), "EXTRACT", "seat-uid",
                            expected=True, budget_padded=512)
    assert ("seat-uid", "reasoning_overhead", "EXTRACT") in seen


def test_audit_suppression_ineffective_still_fires_when_unexpected(monkeypatch):
    """A field we SENT being ignored stays a CRIT — the reclassification only quieted the
    cannot-disable case, not the ignored-field case."""
    crits = []
    import src.api.logging_config as lc
    monkeypatch.setattr(lc, "log_crit", lambda logger, msg, **kw: crits.append(msg))
    C._audit_reasoning_leak(_leaked_result(), "EXTRACT", "u1", expected=False)
    assert "llm_reasoning.suppression_ineffective" in crits


# ── 3. the cost is a first-class metric, not an inference from token counts ──────────────

def test_reasoning_overhead_metric_in_window_and_not_alarm():
    """M_REASONING_OVERHEAD is counted (window metric) but never a degradation/alarm: the
    spend is EXPECTED for this provider class, and reading it as a fault would be the same
    category error as reading its absence as a solution."""
    from saas import brain_health as bh
    assert bh.M_REASONING_OVERHEAD in bh.METRICS
    assert bh.M_REASONING_OVERHEAD not in bh.DEGRADATION_METRICS


def test_seat_metrics_surface_reasoning_overhead():
    from saas import brain_health as bh
    out = bh.collect_seat_metrics("nobody")
    assert out["available"] is False           # fail-safe, no schema → nothing measured
    assert "reasoning_overhead_calls" in out and "reasoning_overhead_share" in out


def test_probe_extraction_reports_measured_reasoning_cost(monkeypatch):
    """The Test & calibrate surface reports the ACTUAL reasoning tokens of its own call.

    The call wrapper returns parsed CONTENT only, so the numbers travel via the same-thread
    accounting seam (llm_calls.last_call_usage) — the probe reads back ITS call's usage.
    """
    from saas import llm_admin

    cfg = {"base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.2",
           "backend_type": "openai"}

    class _Ctx:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("saas.llm_context.bind_explicit_llm_config", lambda c: _Ctx())
    monkeypatch.setattr("saas.llm_context.reset_llm_config", lambda t: None)

    content = {"triples": [{"subject": "Alice", "relation": "age", "object": "30"}]}

    def _fake_call(**kw):
        # What the real wrapper does: parsed content OUT, usage recorded same-thread.
        C._last_usage.usage = {"completion_tokens": 500, "reasoning_tokens": 412,
                               "operation": "EXTRACTION"}
        return content

    monkeypatch.setattr(C, "call_llm_with_retry_sync", _fake_call)
    res = llm_admin._probe_extraction("acct", cfg)
    rc = res["reasoning_cost"]
    assert rc["reasoning_tokens"] == 412 and rc["completion_tokens"] == 500
    assert rc["can_disable_reasoning"] is False
    assert "cannot disable reasoning" in rc["note"] and "412" in rc["note"]


def test_last_call_usage_seam_records_within_call_path(monkeypatch):
    """The seam is populated by the REAL call path (not only by fakes): a mocked transport
    returning usage must surface it via last_call_usage() on the same thread."""
    class _Resp2:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{\"atoms\": []}"}}],
                    "usage": {"completion_tokens": 104,
                              "completion_tokens_details": {"reasoning_tokens": 49}}}

    def fake_post(client, endpoint, json=None, headers=None, timeout=None):
        return _Resp2()

    monkeypatch.setattr(CL, "_tenant_brain",
                        lambda: {"base_url": "https://api.cerebras.ai", "backend_type": "openai"})
    monkeypatch.setattr(C.llm_source_ip, "post_sync", fake_post)
    monkeypatch.setattr(C, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(C, "_get_endpoint_list",
                        lambda: ["https://api.cerebras.ai/v1/chat/completions"])
    C.call_llm_with_retry_sync(
        [{"role": "user", "content": "say ok"}], "gpt-oss-120b",
        user_id="usage-probe", operation="REFRAME", max_retries=1)
    usage = C.last_call_usage()
    assert usage and usage["operation"] == "REFRAME", usage
    assert usage["reasoning_tokens"] == 49 and usage["completion_tokens"] == 104


def test_banner_states_headroom_number_and_standing_cost():
    from saas import reasoning_ticket as rt
    b = rt.build_reasoning_banner("api.z.ai", "glm-5.2", "reason", admin=False)
    assert "no\nway" not in b["body"]
    assert "still billed on every call" in b["body"]
    assert "+1024 tokens" in b["body"] or "+the configured headroom" in b["body"]
