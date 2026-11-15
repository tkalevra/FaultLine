"""P4 RECALL-FIDELITY LANE - interrogatives store no machine-derived memory edges.

Measured wound (benchmarks/walk_litmus, abstention family 0/3): "What's my passport
number?" DERIVED (user, owns, "passport number") - the question's own nouns became a
stored fact - and the unattested recall harvest then filed it, so recall answered
"You own passport number" over a value the user never stated. Same shape for "Where did
I park my car?" (park/own car), "When is my dentist appointment?" (owns dentist
appointment), "What food do I love?" (love food), "How many laps did I say I did at
swimming?" (do many laps).

Fix, two deterministic layers:
  1. deriver SPINE_INTERROGATIVE_NO_MEM (default on): _emit declines every edge whose
     tokens come from a wh-question sentence (WP/WRB/WDT detected in a pre-pass) -
     questions ASK, they do not STATE.
  2. render _drop_never_stated_values (NEVER_STATED_VALUE_GATE, default on): a NON-user-
     stated row whose whole object surface appears in the query text is the question's
     own residue - dropped so the abstention sentinel fires; user_stated/user_corrected
     rows and engine-computed synthetic verdicts keep today's behavior.

fail-on-old: before the fix every question text derives at least one junk edge, and the
render gate does not exist.
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL,
                                    reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2023, 5, 30)

_QUESTIONS = [
    "What's my passport number?",
    "Where did I park my car?",
    "When is my dentist appointment?",
    "What food do I love?",
    "How many laps did I say I did at swimming?",
]


def _facts(text):
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return list(m.derive_sentence_facts(text, _REF))


@requires_model
@pytest.mark.parametrize("text", _QUESTIONS)
def test_question_derives_no_memory_edges(text):
    assert _facts(text) == [], text


@requires_model
def test_statement_controls_still_derive():
    # positive controls: real statements keep deriving through the same gate
    facts = _facts("I ran 13 kilometers this morning before work.")
    assert any(getattr(f, "object", None) == "13 kilometers" for f in facts), facts
    facts2 = _facts("I work at Quartzline as an electrician.")
    assert any(getattr(f, "rel_type", None) == "works_for" for f in facts2), facts2


@requires_model
def test_relative_clause_wh_word_is_not_suppressed():
    # the measured regression this closes: an embedded RELATIVE clause carries the same
    # WP/WDT tags as a question but asserts genuinely - only a "?"-terminated sentence
    # is an interrogative (tests/test_linguistics.py relative pins went red before this)
    facts = _facts("I have a brother who lives in Boston.")
    assert any(getattr(f, "subject", None) == "brother"
               and getattr(f, "rel_type", None) == "live_in"
               and getattr(f, "object", None) == "boston" for f in facts), facts
    facts2 = _facts("the man who died was my neighbor")
    assert any(getattr(f, "rel_type", None) == "has_state"
               and getattr(f, "object", None) == "die" for f in facts2), facts2


# ── render-side gate (no model needed) ──────────────────────────────────────────────

def test_drop_never_stated_values_drops_question_residue():
    import src.api.main as main
    q = "What's my passport number?"
    facts = [
        {"object": "passport number", "rel_type": "owns",
         "fact_provenance": "llm_inferred"},
        {"object": "framework 13", "rel_type": "owns",
         "fact_provenance": "user_stated"},
        {"object": "42 days", "rel_type": "temporal_computation",
         "fact_provenance": "", "source": "temporal_calc"},
    ]
    kept = main._drop_never_stated_values(q, facts)
    assert [f["object"] for f in kept] == ["framework 13", "42 days"], kept


def test_drop_never_stated_values_keeps_stated_and_off_topic():
    import src.api.main as main
    q = "what food do I love?"
    facts = [
        {"object": "burgundy", "rel_type": "love", "fact_provenance": "llm_inferred"},
        {"object": "food", "rel_type": "love", "fact_provenance": "llm_inferred"},
        {"object": "sushi", "rel_type": "love", "fact_provenance": "user_stated"},
    ]
    kept = main._drop_never_stated_values(q, facts)
    # 'food' IS the query's own noun under non-user provenance -> dropped;
    # burgundy (off-topic surface) and the STATED sushi keep today's behavior
    assert [f["object"] for f in kept] == ["burgundy", "sushi"], kept


def test_drop_never_stated_values_fail_safe_on_empty():
    import src.api.main as main
    assert main._drop_never_stated_values("", [{"object": "x"}]) == [{"object": "x"}]
    assert main._drop_never_stated_values("q", []) == []
