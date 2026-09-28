"""Regression gate for the 1st-person DESIRE / VOLITION preference capture seam.

ROOT CAUSE (LongMemEval 35a27287, single-session-preference): a whole session of soft interest
statements — "I want to learn more informal language", "I'd love to attend the cultural festival",
"I'm interested in attending language exchange events", "I'd like to explore language exchange
opportunities and cultural events" — produced ZERO stored facts, so recall found "No relevant facts".
The occurrence lanes DELIBERATELY reject these volition/desire constructions (an intention is not a
thing the user DID — the _CATENATIVE / _MENTAL_STATE INTENT firewall), but nothing CAPTURED the
preference they express. The fix adds a constructive counterpart:

  • analyze_desire_predication — 1st-person subject + a bounded volition verb (predicate_span
    ._PREFERENCE_VERBS: like/love/want/prefer/enjoy/…) + the matrix's (or its infinitival xcomp's)
    direct object → (user, <verb>, <object>). Coordinated objects each yield an edge; negated
    ("I don't like X") is skipped; indefinite-pronoun objects ("attend something") carry no content.

  • analyze_copula_relational_predicate — extended to descend a GERUND prepositional complement
    ("interested IN attending language exchange events") to the gerund's own object, so the object is
    kept instead of the whole complement being dropped.

All deterministic, subject-agnostic, grammar-driven (NO domain/preference word zoo — the volition
verbs are a bounded language primitive like _CATENATIVE). See src/extraction/linguistics.py +
src/api/main.py (SPINE_AFFECT_PREFERENCE block + /harvest-spans).
"""
import pytest

from src.extraction.linguistics import (
    analyze_copula_relational_predicate,
    analyze_desire_predication,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


# ── DESIRE / VOLITION preference capture ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,rel,obj", [
    ("I want to learn more informal language.", "want", "informal language"),
    ("I like jazz.", "like", "jazz"),
    ("I want a dog.", "want", "dog"),
    ("I would like to explore cultural events.", "like", "cultural events"),
])
def test_desire_captures_object(text, rel, obj):
    edges = analyze_desire_predication(text)
    assert {"subject": "user", "rel_type": rel, "object": obj, "negated": False} in edges


def test_desire_captures_coordinated_objects():
    edges = analyze_desire_predication("I like jazz and classical music.")
    objs = {(e["rel_type"], e["object"]) for e in edges}
    assert ("like", "jazz") in objs
    assert ("like", "classical music") in objs


def test_desire_skips_negated_preference():
    # "I don't like cilantro" is negation-as-absence (deferred) — not a positive preference.
    assert analyze_desire_predication("I don't like cilantro.") == []


def test_desire_skips_indefinite_pronoun_object():
    # "attend something" has no content object → no junk (user, love, something).
    edges = analyze_desire_predication(
        "I would love to attend something like the cultural festival.")
    assert all(e["object"] != "something" for e in edges)


@pytest.mark.parametrize("text", [
    "I fixed the fence three weeks ago.",   # occurrence verb, NOT a volition verb
    "I seem to be lost.",                    # catenative but not a preference verb
    "I tried to call Sarah.",                # try ∉ preference verbs → no false preference
])
def test_desire_does_not_fire_on_non_preference_verbs(text):
    assert analyze_desire_predication(text) == []


# ── GERUND relational-predicate object recovery ──────────────────────────────────────────

def test_interested_in_gerund_keeps_object():
    # Before: pcomp gerund pobj dropped the whole complement → []. Now: descend to the gerund's dobj.
    edges = analyze_copula_relational_predicate(
        "I'm interested in attending language exchange events.")
    assert any(e["rel_type"] == "interested_in" and "events" in e["object"] for e in edges)


def test_allergy_relational_predicate_unregressed():
    edges = analyze_copula_relational_predicate("I am allergic to penicillin")
    assert {"subject": "user", "rel_type": "allergic_to", "object": "penicillin",
            "negated": False} in edges
