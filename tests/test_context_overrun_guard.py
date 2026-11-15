"""CTXOVERRUN — the context-window guard.

Pins the behaviour confirmed from a live failure on 2026-08-01::

    send_error: task id = 13471, error: request (9575 tokens) exceeds the
    available context size (8192 tokens)

and from llama.cpp master (quoted in ``src/api/context_window.py``): the rejection is an
HTTP 400 ``exceed_context_size_error``, the check is ``n_tokens() >= n_ctx`` on the PROMPT
alone, the error body carries the server's own ``n_ctx``/``n_prompt_tokens``, and ``/props``
advertises the PER-SLOT ``n_ctx`` (already ``n_ctx / n_parallel``).
"""

import json
import os

import httpx
import pytest

from src.api import context_window as cw
from src.api import prompt_budget as pb


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_CONTEXT_GUARD", "LLM_CONTEXT_WINDOW", "LLM_CONTEXT_WINDOW_DEFAULT",
              "LLM_CONTEXT_PROBE", "LLM_CONTEXT_CHARS_PER_TOKEN", "LLM_CONTEXT_SAFETY_MARGIN",
              "LLM_CONTEXT_SLOT_DIVISION", "PROMPT_PREAMBLE_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_CONTEXT_PROBE", "false")   # never touch the network in tests
    cw.reset_state()
    cw.reset_counters()
    yield
    cw.reset_state()
    cw.reset_counters()


# ── FLAG OFF: byte-for-byte legacy ────────────────────────────────────────────────────

def test_flag_off_is_byte_for_byte_legacy(monkeypatch):
    """LLM_CONTEXT_GUARD=false ⇒ nothing is checked, clamped, blocked, or learned."""
    monkeypatch.setenv("LLM_CONTEXT_GUARD", "false")
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "100")          # absurdly small on purpose
    msgs = [{"role": "user", "content": "x" * 100_000}]
    plan = cw.plan_call(msgs, max_tokens=2048, operation="EXTRACT")
    assert plan.fits is True
    assert plan.hard_overrun is False and plan.clamped is False
    assert cw.learn_from_rejection({"n_ctx": 8192}) is None
    assert cw.counters() == {}


def test_prompt_budget_flag_off_is_legacy_rendering(monkeypatch):
    monkeypatch.setenv("PROMPT_PREAMBLE_BUDGET", "false")
    rows = [("born_in", "was born in", "immutable"), ("owns", "owns", "supersede")]
    out = pb.correction_behavior_block(rows)
    assert out == ("\nCORRECTION-SUPPORTING REL_TYPES (metadata-driven):\n"
                   "Mark with is_correction=true when user corrects these rel_types:\n"
                   "  - born_in (immutable): was born in\n"
                   "  - owns (supersede): owns\n")


# ── ESTIMATION: conservative, and honest about being a heuristic ──────────────────────

def test_estimator_over_counts_typical_english(monkeypatch):
    """Measured on 4,000 real LongMemEval turns with the Qwen tokenizer: mean 4.66
    chars/token, p01 3.48. The 3.5 default must therefore OVER-count typical prose."""
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    text = ("The quick brown fox jumps over the lazy dog near the river bank. " * 40)
    est = cw.estimate_tokens([{"role": "user", "content": text}])
    real_ish = len(text) / 4.66          # the measured mean for real English
    assert est > real_ish, "estimator must not under-count typical prose"
    assert est < real_ish * 2.0, "estimator must not be so paranoid it refuses healthy calls"


def test_calibration_only_ratchets_down(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    msgs = [{"role": "user", "content": "y" * 4000}]
    cw.observe_usage(msgs, {"usage": {"prompt_tokens": 2000}})   # ratio 2.0 — denser
    first = cw.estimate_tokens(msgs)
    cw.observe_usage(msgs, {"usage": {"prompt_tokens": 400}})    # ratio 10.0 — looser
    second = cw.estimate_tokens(msgs)
    assert second == first, "a looser observation must NEVER widen the estimate"
    assert first >= 2000, "a denser observation must tighten the estimate"


# ── WINDOW DISCOVERY: /props is per-slot; the -c value is the trap ────────────────────

def test_props_n_ctx_is_taken_as_per_slot_not_divided_again():
    """llama.cpp server-context.cpp get_props: {"n_ctx": meta->slot_n_ctx} — ALREADY the
    per-slot value. The owner's brain reports n_ctx 8192 with total_slots 4 behind
    `-c 32768 --parallel 4`. Dividing again would yield 2048 and refuse healthy calls."""
    body = {"default_generation_settings": {"n_ctx": 8192, "params": {}}, "total_slots": 4}
    window, slots = cw._window_from_props(body)
    assert (window, slots) == (8192, 4)


def test_slot_division_is_opt_in_only(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_SLOT_DIVISION", "4")
    assert cw._apply_slot_division(8192) == 2048
    monkeypatch.setenv("LLM_CONTEXT_SLOT_DIVISION", "auto")
    assert cw._apply_slot_division(8192) == 8192


def test_models_endpoint_prefers_the_loaded_window():
    body = {"data": [{"id": "m", "max_context_length": 32768, "loaded_context_length": 8192,
                      "n_ctx_train": 262144}]}
    assert cw._window_from_models(body) == 8192


def test_unknown_window_never_blocks(monkeypatch):
    """No env, no probe, no rejection ⇒ observe-only. Unknown is not an excuse to refuse."""
    plan = cw.plan_call([{"role": "user", "content": "x" * 500_000}], 2048, "EXTRACT")
    assert plan.fits is True and plan.source == cw.SOURCE_UNKNOWN


# ── THE VERDICT: hard vs soft overrun ─────────────────────────────────────────────────

def test_hard_overrun_is_blocked(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    plan = cw.plan_call([{"role": "user", "content": "x" * 40_000}], 2048, "EXTRACT")
    assert plan.fits is False and plan.hard_overrun is True
    assert plan.window == 8192 and plan.prompt_tokens_est >= 8192


def test_soft_overrun_clamps_the_completion_instead_of_truncating_json(monkeypatch):
    """Prompt fits, completion reserve does not. llama.cpp only refuses on the PROMPT, so
    the call would be ACCEPTED and then generate into whatever room is left — a JSON array
    cut mid-token is unparseable, which lands in the same silent-degrade branch."""
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    msgs = [{"role": "user", "content": "x" * 24_000}]        # ~7.4k est, fits
    plan = cw.plan_call(msgs, max_tokens=2048, operation="EXTRACT")
    assert plan.fits is True and plan.soft_overrun is True and plan.clamped is True
    assert plan.effective_max_tokens < 2048
    assert plan.prompt_tokens_est + plan.effective_max_tokens <= 8192


# ── LEARNING FROM THE SERVER'S OWN REFUSAL ────────────────────────────────────────────

_LLAMACPP_400_BODY = json.dumps({
    "error": {"code": 400, "message": "request (9575 tokens) exceeds the available context "
                                      "size (8192 tokens), try increasing it",
              "type": "exceed_context_size_error"},
    "n_prompt_tokens": 9575,
    "n_ctx": 8192,
})


def test_classify_the_real_llamacpp_rejection():
    info = cw.classify_rejection(400, _LLAMACPP_400_BODY)
    assert info is not None
    assert info["n_prompt_tokens"] == 9575 and info["n_ctx"] == 8192


def test_classify_openai_style_context_length_exceeded():
    body = json.dumps({"error": {"message": "This model's maximum context length is 8192 "
                                            "tokens, however you requested 9575 tokens.",
                                 "type": "invalid_request_error",
                                 "code": "context_length_exceeded"}})
    assert cw.classify_rejection(400, body) is not None


def test_an_unrelated_400_is_not_claimed_as_an_overrun():
    assert cw.classify_rejection(400, json.dumps(
        {"error": {"message": "unknown field 'chat_template_kwargs'",
                   "type": "invalid_request_error"}})) is None


def test_learned_window_becomes_authoritative():
    assert cw.resolve_window(allow_probe=False) == (None, cw.SOURCE_UNKNOWN)
    cw.learn_from_rejection(cw.classify_rejection(400, _LLAMACPP_400_BODY))
    assert cw.resolve_window(allow_probe=False) == (8192, cw.SOURCE_LEARNED)


# ── END TO END through the REAL seam ──────────────────────────────────────────────────

def _prep_endpoint(monkeypatch):
    from src.api import llm_calls
    monkeypatch.setenv("LLM_BACKEND_TYPE", "openwebui")
    monkeypatch.setenv("LLM_BASE_URL", "http://198.51.100.20:8080")
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    monkeypatch.delenv("SAAS_MODE", raising=False)
    llm_calls.reset_circuit_breaker()
    return llm_calls


def test_over_window_request_is_never_sent(monkeypatch):
    """THE POINT OF THE WHOLE CHANGE: the HTTP call must not happen."""
    llm_calls = _prep_endpoint(monkeypatch)
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    sent = []
    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync",
                        lambda *a, **k: sent.append(1))
    out = llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "system", "content": "S" * 34_000},
                  {"role": "user", "content": "the turn"}],
        model="test-model", operation="EXTRACT", max_retries=3)
    assert sent == [], "an over-window request was still put on the wire"
    assert out["error"] == "context_overrun"
    assert out["context_overrun"]["window"] == 8192
    assert cw.counters().get(cw.EV_PREFLIGHT_BLOCKED) == 1


def test_server_rejection_is_not_retried_and_does_not_open_the_breaker(monkeypatch):
    """A 400 overrun is a deterministic verdict. Retrying wastes the slot, and letting it
    reach record_failure() opens the breaker for the calls that WOULD have fitted — one
    oversized turn degrading its healthy neighbours."""
    llm_calls = _prep_endpoint(monkeypatch)
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)   # window unknown → no preflight
    calls = []

    def _fake_post(client, endpoint, **kwargs):
        calls.append(endpoint)
        request = httpx.Request("POST", endpoint)
        return httpx.Response(400, request=request, content=_LLAMACPP_400_BODY.encode())

    monkeypatch.setattr(llm_calls.llm_source_ip, "post_sync", _fake_post)
    out = llm_calls.call_llm_with_retry_sync(
        messages=[{"role": "user", "content": "hello"}],
        model="test-model", operation="EXTRACT", max_retries=3)

    assert out["error"] == "context_overrun"
    assert len(calls) == 1, f"a deterministic refusal was retried {len(calls)} times"
    assert llm_calls.get_circuit_breaker_status()["failures"] == 0
    assert cw.counters().get(cw.EV_SERVER_REJECTED) == 1
    # …and the window it refused with is now known, so the NEXT call is pre-flighted.
    assert cw.resolve_window(allow_probe=False) == (8192, cw.SOURCE_LEARNED)


# ── THE ROOT CAUSE: an unbounded, self-amplifying preamble ────────────────────────────

def _synthetic_ontology(n_default=475, n_exception=9):
    """Shaped like the measured tenant 04658f0e: 541 rows, 475 restating one behavior."""
    rows = [(f"grown_rel_{i:03d}", f"Grown Rel {i}", "supersede", "engine")
            for i in range(n_default)]
    rows += [(f"seeded_{i:02d}", f"Seeded {i}", "immutable", "wikidata")
             for i in range(n_exception)]
    return rows


def test_dominant_behavior_is_stated_once_not_enumerated_541_times():
    rows = _synthetic_ontology()
    legacy = pb._legacy_block(rows)
    bounded = pb.correction_behavior_block(rows, budget_chars=7000)
    assert len(bounded) < len(legacy) / 10, "the collapse must be an order of magnitude"
    assert "Default correction behavior for every rel_type not listed here: supersede." in bounded
    for i in range(9):                       # every EXCEPTION survives — no information lost
        assert f"seeded_{i:02d}" in bounded
    assert "grown_rel_000" not in bounded    # …and no default row is enumerated


def test_dominant_behavior_is_computed_not_hardcoded():
    """Subject-agnostic: flip which behavior dominates and the blanket line follows."""
    rows = ([("a", "A", "immutable")] * 50) + [("b", "B", "supersede")]
    out = pb.correction_behavior_block(rows, budget_chars=7000)
    assert "not listed here: immutable." in out
    assert "  - supersede: b" in out


def test_truncation_is_deterministic_and_announced():
    rows = [(f"rel_{i:03d}", f"L{i}", "immutable", "engine") for i in range(400)]
    rows += [("owns", "owns", "supersede", "builtin")] * 500
    a = pb.correction_behavior_block(rows, budget_chars=900)
    b = pb.correction_behavior_block(rows, budget_chars=900)
    assert a == b, "an unstable prompt preamble is new bench flicker"
    assert "further rel_types omitted" in a


def test_bounded_preamble_brings_the_measured_prompt_back_under_the_window():
    """The measured numbers: 3,353-token fixed body + a 5,925-token menu = ~9,974 against
    an 8,192 slot. With the menu collapsed the same call fits with room to spare."""
    rows = _synthetic_ontology()
    fixed_chars = 12_910                       # measured _build_extraction_prompt base+tail
    legacy_est = cw.estimate_tokens(
        [{"role": "system", "content": "x" * (fixed_chars + len(pb._legacy_block(rows)))}])
    bounded_est = cw.estimate_tokens(
        [{"role": "system", "content": "x" * (fixed_chars + len(
            pb.correction_behavior_block(rows, budget_chars=7000)))}])
    assert legacy_est > 8192, "the legacy prompt must reproduce the overrun"
    assert bounded_est + 2048 < 8192, "the bounded prompt must fit WITH its completion reserve"


def test_preamble_budget_tracks_the_discovered_window(monkeypatch):
    """The bound is derived from the brain the tenant actually runs, not a constant."""
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "8192")
    cw.reset_state()
    small = pb.preamble_budget_chars(fixed_prompt_chars=12_910,
                                     reserve_completion_tokens=2048)
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "32768")
    cw.reset_state()
    large = pb.preamble_budget_chars(fixed_prompt_chars=12_910,
                                     reserve_completion_tokens=2048)
    assert large > small, "a bigger brain must be allowed a bigger menu"
    assert small >= 0


def test_preamble_budget_falls_back_when_the_window_is_unknown(monkeypatch):
    monkeypatch.delenv("LLM_CONTEXT_WINDOW", raising=False)
    cw.reset_state()
    assert pb.preamble_budget_chars(fixed_prompt_chars=12_910) == pb._default_budget_chars()


# The EXACT body the owner's llama.cpp build returned on 2026-08-01 (captured live, not
# reconstructed). Note n_prompt_tokens/n_ctx are NESTED inside "error" here, while the
# upstream source reads as though they are top-level — hence both are parsed.
_LIVE_LLAMACPP_BODY = (
    '{"error":{"code":400,"message":"request (9433 tokens) exceeds the available context '
    'size (8192 tokens), try increasing it","type":"exceed_context_size_error",'
    '"n_prompt_tokens":9433,"n_ctx":8192}}'
)


def test_classify_the_captured_live_rejection_body():
    info = cw.classify_rejection(400, _LIVE_LLAMACPP_BODY)
    assert info == {"n_prompt_tokens": 9433, "n_ctx": 8192,
                    "message": "request (9433 tokens) exceeds the available context size "
                               "(8192 tokens), try increasing it"}
    assert cw.learn_from_rejection(info) == 8192
