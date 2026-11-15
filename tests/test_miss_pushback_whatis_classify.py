"""Unit tests for the MISS-PUSHBACK background "what is X?" concept classifier.

Covers DESIGN-hierarchy-ladder-and-growth.md §"On a MISS: push back to the LLM and
CLASSIFY". The first-fire side (ingest routing to C-raw + queue) is unit-tested via
_retype_mistyped_edge in test_entity_grounding_retype.py; this file covers the
re_embedder SECONDARY strengthen: the bounded "what is <concept>?" LLM call's
deterministic VALIDATION (LLM proposes / deterministic accepts) and the flag/bound.

The LLM call is monkeypatched — no model, no DB. These guard the validation gate
(canonical-type closed set, parent placement via the rung-4 validator) that can
regress silently.
NOTE: tests/ is gitignored; commit with `git add -f`.
"""
import importlib

import pytest

emb = importlib.import_module("src.re_embedder.embedder")


# ── flag + bound defaults (the live-behaviour contract) ──────────────────────
def test_whatis_flag_default_on():
    assert emb._ENGINE_WHATIS_CLASSIFY is True
    assert emb._WHATIS_BATCH_LIMIT >= 1


def test_canonical_types_are_the_pitfall11_six():
    assert set(emb._CANONICAL_ENTITY_TYPES_LC.keys()) == {
        "person", "animal", "organization", "location", "object", "concept"
    }


# ── _query_llm_what_is: LLM proposes / deterministic validates ───────────────
def _patch_llm(monkeypatch, payload):
    """Patch the centralized sync LLM call to return `payload` (dict or None)."""
    import src.api.llm_calls as _lc

    # The whatis lane resolves its model via LLMModels.get("ENRICHMENT") BEFORE the call —
    # and model identity has deliberately NO hardcoded fallback (an unset env var is a LOUD
    # config error, never a silent guess). Stub the env var so resolution succeeds in a bare
    # test environment; the fake below then answers before any network is touched.
    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")
    def _fake(*a, **k):
        return payload
    monkeypatch.setattr(_lc, "call_llm_with_retry_sync", _fake)


def test_whatis_types_into_canonical_and_keeps_valid_parent(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "Animal", "parent": "dog"})
    out = emb._query_llm_what_is("poodle", "http://llm")
    assert out == {"entity_type": "Animal", "parent": "dog"}


def test_whatis_canonicalizes_case(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "animal", "parent": "DOG"})
    out = emb._query_llm_what_is("poodle", "http://llm")
    assert out["entity_type"] == "Animal"
    assert out["parent"] == "dog"


def test_whatis_off_set_type_is_unclassifiable_returns_none(monkeypatch):
    # An off-closed-set type means the LLM could not classify → None → C is last resort.
    _patch_llm(monkeypatch, {"entity_type": "Vehicle", "parent": "car"})
    assert emb._query_llm_what_is("tesla", "http://llm") is None


def test_whatis_unknown_type_returns_none(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "unknown", "parent": "x"})
    assert emb._query_llm_what_is("blah", "http://llm") is None


def test_whatis_drops_bad_parent_keeps_type(monkeypatch):
    # A universal-root parent fails the rung-4 validator → parent dropped, type kept.
    _patch_llm(monkeypatch, {"entity_type": "Object", "parent": "thing"})
    out = emb._query_llm_what_is("router", "http://llm")
    assert out == {"entity_type": "Object", "parent": None}


def test_whatis_drops_scalar_looking_parent(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "Object", "parent": "192.168.1.1"})
    out = emb._query_llm_what_is("router", "http://llm")
    assert out["parent"] is None


def test_whatis_null_parent_string_is_none(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "Concept", "parent": "null"})
    out = emb._query_llm_what_is("bash", "http://llm")
    assert out == {"entity_type": "Concept", "parent": None}


def test_whatis_no_llm_response_raises_rather_than_returning_none(monkeypatch):
    """CONTRACT CHANGED DELIBERATELY (non-answer memo fix), and the old assertion WAS the bug.

    Returning None for "the brain produced no answer" made it byte-identical to "the brain
    answered, and this concept is unclassifiable" — and the caller spends the second on the
    concept's retry budget, bumping toward the terminal 'concept_unplaceable' cap. The helper
    already raised LLMUnavailable for the failure modes that raise; it now raises for the
    shapes that do not (a non-dict, an empty dict, an {"error": ...} envelope), so the
    caller's existing "no verdict cached, no attempt burned" handler covers all of them.
    """
    from src.api.llm_lane import LLMUnavailable
    _patch_llm(monkeypatch, None)
    with pytest.raises(LLMUnavailable):
        emb._query_llm_what_is("poodle", "http://llm")


def test_whatis_answered_but_unclassifiable_still_returns_none(monkeypatch):
    """The other half: an ANSWER whose entity_type is outside the canonical six is a real
    verdict of "cannot classify" and must keep returning None, so the cap/backoff still works."""
    _patch_llm(monkeypatch, {"entity_type": "unknown", "parent": None})
    assert emb._query_llm_what_is("poodle", "http://llm") is None


def test_whatis_empty_concept_returns_none():
    assert emb._query_llm_what_is("", "http://llm") is None
    assert emb._query_llm_what_is("   ", "http://llm") is None


def test_whatis_accepts_feeling_parent(monkeypatch):
    # A feeling/state must ground a real one-rung emotion-family parent (worried -> fear),
    # exactly like a noun (poodle -> dog). The rung-4 validator must NOT reject "fear" as a
    # parent of "worried" (clean snake_case token, not scalar/root, differs from child,
    # types agree as Concept). This is the ladder seed: worried subclass_of fear.
    _patch_llm(monkeypatch, {"entity_type": "Concept", "parent": "fear"})
    out = emb._query_llm_what_is("worried", "http://llm")
    assert out == {"entity_type": "Concept", "parent": "fear"}


def test_whatis_accepts_joy_family_parent(monkeypatch):
    _patch_llm(monkeypatch, {"entity_type": "Concept", "parent": "joy"})
    out = emb._query_llm_what_is("joyful", "http://llm")
    assert out == {"entity_type": "Concept", "parent": "joy"}


def test_whatis_prompt_is_subject_agnostic_few_shot(monkeypatch):
    # PROVE the broadening is few-shot generalization, not a feeling word-list / if-concept zoo.
    # Capture the prompt actually sent to the LLM and assert: it carries a feeling example AND a
    # noun example AND a null example (the model generalizes from examples), and it does NOT
    # enumerate feeling vocabulary as a lookup list.
    captured = {}
    import src.api.llm_calls as _lc

    monkeypatch.setenv("WGM_LLM_MODEL", "test-model")   # model resolves BEFORE the call (no fallback by design)
    def _fake(*a, **k):
        msgs = k.get("messages") or (a[0] if a else None)
        captured["prompt"] = msgs[0]["content"] if msgs else ""
        return {"entity_type": "Concept", "parent": "fear"}

    monkeypatch.setattr(_lc, "call_llm_with_retry_sync", _fake)
    emb._query_llm_what_is("worried", "http://llm")
    p = captured["prompt"].lower()

    # General is-a-ladder framing covers feelings/states explicitly (general, not enumerated).
    assert "feeling" in p or "mental state" in p
    # Few-shot: a feeling example, a noun example, and a "no parent" (null) example coexist.
    assert "worried -> fear" in p
    assert "poodle -> dog" in p
    assert "null" in p
    # Subject-agnostic guard: the prompt must NOT be a hardcoded feeling lexicon. A handful of
    # illustrative examples is fine; a long enumerated emotion list is the word-zoo we reject.
    emotion_zoo = ["anxious", "nervous", "dread", "panic", "terror", "sad", "angry", "happy",
                   "afraid", "scared", "excited", "content"]
    assert sum(1 for w in emotion_zoo if w in p) <= 1, "prompt looks like a feeling word-list"


def test_classify_disabled_by_flag(monkeypatch):
    # When the flag is off the sweep returns zeroed stats without touching the DB.
    monkeypatch.setattr(emb, "_ENGINE_WHATIS_CLASSIFY", False)
    out = emb.classify_unknown_concepts(db_conn=None, qwen_api_url="http://llm")
    assert out == {"classified": 0, "grounded": 0, "deferred": 0, "errors": 0}
