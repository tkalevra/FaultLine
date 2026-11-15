"""Regression pin: NATURAL_LANGUAGE_FILL batching (SPINE_NL_FILL_BATCH).

A dense ingest turn mints several grown rel_types and, before this change, phrased each
with a SEPARATE blocking NATURAL_LANGUAGE_FILL call — N serial LLM round-trips serialized
on the one shared LLM slot (the owner's measured cost; NATURAL_LANGUAGE_FILL was the #1 op
in the 4-min pre-prod window). ``generate_rel_type_phrasing_batch`` phrases the WHOLE turn's
novel-rel set in ONE round-trip. These tests pin:

  1. N (>=2) novel rels are phrased in exactly ONE LLM call (the reduction).
  2. Phrasing is validated per rel with the SAME placeholder contract as the single-rel path
     (X-in-3p, Y-in-2p-no-X) — a broken/omitted rel is NOT cached, so the ingest caller falls
     back to the per-rel generator (zero capture/render regression).
  3. A single novel rel delegates to the per-rel path (no batching overhead for the trivial
     case), so behavior for a 0/1-novel-rel turn is unchanged.

Deterministic — the LLM is mocked; no DB, no network.
"""

import src.api.llm_calls as llm_calls


def _install_counting_llm(monkeypatch, response):
    # LLMModels.get() refuses to guess a model name → needs a model env var. Real containers set
    # WGM_LLM_MODEL; the test harness may not. Provide one so the op resolves (the call itself is
    # mocked below, so the value is never used to reach a network).
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    calls = {"n": 0, "ops": []}

    def _fake(**kw):
        calls["n"] += 1
        calls["ops"].append(kw.get("operation"))
        return response

    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", _fake)
    return calls


def test_batch_phrases_many_rels_in_one_call(monkeypatch):
    """5 novel rels → ONE NATURAL_LANGUAGE_FILL_BATCH call, all phrased."""
    resp = {
        "fly_to":     {"natural_language": "X flies to Y",     "natural_language_2p": "You fly to Y"},
        "tour":       {"natural_language": "X tours Y",        "natural_language_2p": "You tour Y"},
        "purchase":   {"natural_language": "X purchases Y",    "natural_language_2p": "You purchase Y"},
        "present":    {"natural_language": "X presents Y",     "natural_language_2p": "You present Y"},
        "present_at": {"natural_language": "X is present at Y", "natural_language_2p": "You are present at Y"},
    }
    calls = _install_counting_llm(monkeypatch, resp)

    out = llm_calls.generate_rel_type_phrasing_batch(
        ["fly_to", "tour", "purchase", "present", "present_at"], user_id="u")

    # THE REDUCTION: five novel rels, ONE LLM round-trip (not five).
    assert calls["n"] == 1
    assert calls["ops"] == ["NATURAL_LANGUAGE_FILL_BATCH"]
    # All five phrased, each carrying both render strings.
    assert set(out) == {"fly_to", "tour", "purchase", "present", "present_at"}
    assert out["fly_to"]["natural_language"] == "X flies to Y"
    assert out["fly_to"]["natural_language_2p"] == "You fly to Y"


def test_batch_drops_invalid_rows_for_per_rel_fallback(monkeypatch):
    """A rel the LLM omits, or phrases against the placeholder contract, is NOT cached — the
    ingest caller then falls back to the per-rel generator (no broken phrasing stored)."""
    resp = {
        "fly_to": {"natural_language": "X flies to Y", "natural_language_2p": "You fly to Y"},
        # missing X placeholder → invalid 3p; 2p also broken (contains X) → dropped entirely.
        "tour":   {"natural_language": "flies to Y",   "natural_language_2p": "You tour X"},
        # 'purchase' omitted from the response entirely → absent from the cache.
    }
    _install_counting_llm(monkeypatch, resp)

    out = llm_calls.generate_rel_type_phrasing_batch(["fly_to", "tour", "purchase"], user_id="u")

    assert "fly_to" in out                       # valid → cached
    assert "tour" not in out                      # invalid contract → not cached (per-rel fallback)
    assert "purchase" not in out                  # omitted → not cached (per-rel fallback)


def test_single_novel_rel_uses_per_rel_path(monkeypatch):
    """One novel rel is left to the single-rel generator (no batching win to chase)."""
    resp = {"natural_language": "X tours Y", "natural_language_2p": "You tour Y"}
    calls = _install_counting_llm(monkeypatch, resp)

    out = llm_calls.generate_rel_type_phrasing_batch(["tour"], user_id="u")

    assert calls["n"] == 1
    assert calls["ops"] == ["NATURAL_LANGUAGE_FILL"]   # per-rel op, NOT the batch op
    assert out == {"tour": {"natural_language": "X tours Y", "natural_language_2p": "You tour Y"}}


def test_empty_input_makes_no_call(monkeypatch):
    calls = _install_counting_llm(monkeypatch, {})
    assert llm_calls.generate_rel_type_phrasing_batch([], user_id="u") == {}
    assert calls["n"] == 0


def test_batch_llm_failure_returns_empty_not_raise(monkeypatch):
    """Any LLM error → {} (caller keeps today's serial per-rel path); never raises."""
    def _boom(**kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(llm_calls, "call_llm_with_retry_sync", _boom)
    assert llm_calls.generate_rel_type_phrasing_batch(["a", "b"], user_id="u") == {}
