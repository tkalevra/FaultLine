"""Regression test for the dual-gate correction/retraction cue detector.

Runs the REAL spaCy pipeline (en_core_web_sm) through the deterministic TIER-1 cue router
(`main._spacy_cue_route`, backed by `linguistics.analyze_directive`). This guards the fix for
the `i'm`@0.95 negation-pattern poison: a contraction / plain statement must route to STATEMENT
(no destructive bypass), while genuine corrections/retractions are caught by curated cues +
syntactic scope — not substring matching.

The TIER-2 LLM-corroboration path is NOT exercised here (needs a live model); these assertions
cover the deterministic grammar tier, where the six acceptance cases land.

Skips cleanly when spaCy / the model is unavailable (the layer is fail-safe to STATEMENT).

NOTE: tests/ is gitignored but has tracked files — add with `git add -f` to track this one.
"""
import pytest

from src.extraction.linguistics import linguistics_available, analyze_directive


pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy en_core_web_sm not available — cue detector falls back to STATEMENT (fail-safe).",
)


def _route(text):
    """Import the real router lazily so a missing app env skips rather than errors at collection."""
    from src.api.main import _spacy_cue_route
    return _spacy_cue_route(text)


# (text, expected (route, tier)) — the deterministic tier-1 outcomes.
_CASES = [
    # THE BUG: a contraction / plain statement must NOT route to a destructive bypass.
    ("I'm planning a trip to Hawaii. My favorite food is poke.", (None, None)),
    ("I started the tomato seeds on March 3rd.", (None, None)),
    # Plain predicate negation is a STATEMENT (negated fact), never a retraction.
    ("This is not a problem.", (None, None)),
    ("I am not worried about it.", (None, None)),
    ("My car is not red.", (None, None)),
    # A negated imperative ("don't forget") is NOT a retraction.
    ("Don't forget the milk.", (None, None)),
    # Genuine corrections / retractions — caught deterministically.
    ("Actually, my dog's name is Luna, not Bella.", ("CORRECTION", "confident")),
    ("No wait, I meant the blue one.", ("CORRECTION", "confident")),
    ("Forget what I told you about the meeting.", ("RETRACTION", "confident")),
    ("Please delete my account.", ("RETRACTION", "confident")),
    # Standalone marker / standalone contrast → ambiguous (caller corroborates with the LLM).
    ("Actually I do like pizza.", ("CORRECTION", "ambiguous")),
    ("She is 14 now, not 12.", ("CORRECTION", "ambiguous")),
    ("It's a cat, not a dog.", ("CORRECTION", "ambiguous")),
]


@pytest.mark.parametrize("text,expected", _CASES)
def test_spacy_cue_route(text, expected):
    assert _route(text) == expected


def test_im_is_never_a_cue():
    """The contraction that poisoned the table must never be a curated cue."""
    from src.api import main as M
    assert "i'm" not in M._RETRACTION_CUE_LEMMAS
    assert "i'm" not in M._CORRECTION_VERB_LEMMAS
    assert "i'm" not in M._CORRECTION_MARKER_LEMMAS
    assert "my" not in M._RETRACTION_CUE_LEMMAS
    assert "my" not in M._CORRECTION_VERB_LEMMAS
    assert "my" not in M._CORRECTION_MARKER_LEMMAS


def test_analyze_directive_grammar_facts():
    """Spot-check the underlying grammar primitive (deterministic, no cue sets)."""
    d = analyze_directive("Forget what I told you about the meeting.")
    assert d is not None and d.imperative_root_lemma == "forget" and not d.imperative_root_negated

    d = analyze_directive("Don't forget the milk.")
    assert d is not None and d.imperative_root_negated  # negated imperative

    d = analyze_directive("This is not a problem.")
    assert d is not None and not d.has_contrastive_negation  # plain predicate negation

    d = analyze_directive("It's a cat, not a dog.")
    assert d is not None and d.has_contrastive_negation  # nominal contrast


# ── SOURCE→GOAL TRANSITION PRE-ROUTE (from→to narration is a STATEMENT, not a correction) ──
# GLiNER2 mis-scores "I switched from Python to Rust" as CORRECTION@0.96 → remember_facts
# diverted the STATEMENT to /retract/correct and stored nothing. The deterministic pre-route
# _is_source_goal_transition_statement rescues it BEFORE GLiNER2 (grammar only, subject-agnostic).
_TRANSITION_CASES = [
    # THE BUG: declarative source→goal transitions must route STATEMENT (not correction).
    ("I switched from Python to Rust last year.", True),
    ("I upgraded my laptop from an old model to a MacBook.", True),
    ("She moved from Toronto to Berlin.", True),           # subject-agnostic (not first-person)
    ("We migrated the database from MySQL to Postgres.", True),
    # NOT a transition-statement — these must stay for the correction gate / GLiNER2.
    ("Change my address from 12 Oak St to 5 Pine Rd.", False),   # imperative correction directive
    ("Correct my note from A to B.", False),                     # imperative correction directive
    ("You switched from Python to Rust.", False),                # 2nd-person subject (Person=2 morph)
    ("Actually I switched from red to blue, not green.", False), # contrastive repair cue present
    ("I went to the store.", False),                             # goal-only, no source
    ("I moved to Berlin.", False),                               # goal-only, no source
    ("I gave the book to Sara.", False),                         # dative, not a transition
    ("I like Rust.", False),                                     # plain statement, no from→to
    ("What did I switch from Python to?", False),                # interrogative
]


@pytest.mark.parametrize("text,expected", _TRANSITION_CASES)
def test_source_goal_transition_detector(text, expected):
    from src.api.main import _is_source_goal_transition_statement
    assert _is_source_goal_transition_statement(text) is expected


@pytest.mark.parametrize("text,expected", _TRANSITION_CASES)
def test_transition_never_hits_destructive_cue(text, expected):
    """The transition guard is additive: a from→to narration must ALSO not trip the
    destructive spaCy correction cue (which would send it to retract). A real correction
    among the negatives is allowed to route via the cue — we only assert the transitions
    themselves stay clear of a destructive bypass."""
    if expected:  # a genuine narrated transition
        assert _route(text) == (None, None)


# ── INTERROGATIVE (QUERY) PRE-ROUTE (G7 — a "…to Y" QUESTION is not a correction) ──
# GLiNER2 mis-scores "What programming language did I switch to?" as CORRECTION (the "…to Y"
# shape) EVEN as a question → recall_memory_tool / remember_facts_tool diverted the question to
# /retract/correct and recall returned nothing. The deterministic interrogative pre-route in
# classify_intent routes any grammatically interrogative turn to QUERY BEFORE GLiNER2, detected
# purely by grammar/morphology (linguistics.is_interrogative_clause). A genuine correction is
# DECLARATIVE, so the guard cannot swallow one.
_INTERROGATIVE_CASES = [
    # THE BUG: "…to Y" questions must be interrogative (→ QUERY, never correction/retraction).
    ("What programming language did I switch to?", True),
    ("Which language do I use now?", True),
    ("Where did I move to?", True),
    ("Which database do I use now?", True),
    ("What did I switch to", True),                              # wh + subject-aux inversion, no '?'
    # NOT interrogative — declarative corrections/retractions/statements stay for the cue gate.
    ("Actually my favorite color is red, not blue.", False),     # declarative correction
    ("I meant Luna, not Bella.", False),                         # declarative correction
    ("Forget that I have a dog.", False),                        # imperative retraction (no wh/'?')
    ("Rex is no longer my pet.", False),                         # cessation retraction (declarative)
    ("I switched from Python to Rust.", False),                  # declarative transition statement
    ("My IP changed to 10.0.0.5.", False),                       # declarative statement
]


@pytest.mark.parametrize("text,expected", _INTERROGATIVE_CASES)
def test_interrogative_query_preroute_detector(text, expected):
    """The grammar test that gates the QUERY pre-route: questions True, declaratives False."""
    from src.extraction.linguistics import is_interrogative_clause
    assert is_interrogative_clause(text) is expected


@pytest.mark.parametrize("text,expected", _INTERROGATIVE_CASES)
def test_interrogative_never_hits_destructive_cue(text, expected):
    """A question must never trip the destructive spaCy correction/retraction cue — the
    interrogative pre-route rescues it, but it must ALSO be clear of the cue gate so nothing
    diverts it to retract. (Declaratives are free to route via the cue; we only assert the
    questions themselves stay clear.)"""
    if expected:  # a genuine question
        assert _route(text) == (None, None)
