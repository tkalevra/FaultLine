"""Unit tests for the broad-recall relevance gate (Task 3) and the affective-statement
pre-route signal (Task 4). Deterministic, grammar/morphology only — no phrase lists.

Model-backed: skip cleanly if en_core_web_sm is not installed.

Run: SPACY_MODEL=en_core_web_sm LINGUISTIC_LAYER=true python -m pytest tests/test_query_relevance_gate.py -q
"""
import os

import pytest

os.environ.setdefault("LINGUISTIC_LAYER", "true")

import src.extraction.linguistics as ling

_HAS_MODEL = ling.linguistics_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")


@requires_model
def test_broad_recall_reference_present_for_self_and_listener():
    from src.api.main import _has_first_or_second_person_reference as ref
    # genuine recall ABOUT the user / listener → reference present (gate exempt)
    assert ref("tell me about me") is True
    assert ref("what do you know about me") is True
    assert ref("tell me about my family") is True
    assert ref("tell me everything") is True   # dative "me" still a 1st-person reference


@requires_model
def test_broad_recall_reference_absent_for_greeting_and_external():
    from src.api.main import _has_first_or_second_person_reference as ref
    # pure greeting / external question → no 1st/2nd-person reference (gate fires)
    assert ref("how is it going") is False        # "it" is Person=3
    assert ref("what is the weather tomorrow") is False
    assert ref("how are things") is False


@requires_model
def test_affective_preroute_signal_matches_first_person_feeling():
    # The Task-4 pre-route fires when a declarative first-person affective construction is detected.
    from src.extraction.linguistics import (
        is_interrogative_clause, analyze_copula, analyze_copula_affect_complements,
    )
    from src.api.main import _detect_feeling_states

    def is_affective_statement(text: str) -> bool:
        if is_interrogative_clause(text) is True:
            return False
        if _detect_feeling_states(text):
            return True
        if analyze_copula_affect_complements(text):
            return True
        c = analyze_copula(text)
        return bool(c and c.subject_is_self and not c.negated
                    and (c.relation or "") == "feels"
                    and (c.complement_pos or "").upper() == "ADJ")

    assert is_affective_statement("I feel overwhelmed") is True
    assert is_affective_statement("I am anxious") is True
    assert is_affective_statement("I was busy and exhausted") is True
    # questions and non-affective statements never pre-route
    assert is_affective_statement("Do I feel overwhelmed?") is False
    assert is_affective_statement("Why am I so anxious?") is False
    assert is_affective_statement("I am a teacher") is False
    assert is_affective_statement("The server is down") is False
