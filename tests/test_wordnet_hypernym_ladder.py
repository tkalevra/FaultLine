"""Deterministic WordNet hypernym-ladder source — pure-function tests (L4 Option b).

Covers the four load-bearing properties of ``src/api/wordnet_ladder.py``:
  1. COVERAGE  — the hypernyms the aggregation needs land (dermatologist→doctor, physician→doctor,
                 handbag→bag, tournament→contest/event).
  2. HARD LINE — a NAME/instance never gets a hypernym rung (no ``subclass_of`` on a name).
  3. DETERMINISM — same input → identical ladder, every run (no cosine, no LLM).
  4. BOUNDED  — depth-capped, terminates before a generic upper-ontology root; multiword/novel
                types WordNet lacks return [] (→ async LLM climb is the caller's fallback).

Pure (no DB, no network) — the corpus is baked/offline. Skips cleanly if the corpus is absent.
"""

import pytest

from src.api import wordnet_ladder as W


def _has_corpus() -> bool:
    return W._wn() is not None


pytestmark = pytest.mark.skipif(not _has_corpus(), reason="WordNet corpus not available offline")


def _parents(rungs):
    return {p for _, p in rungs}


# ── 1. COVERAGE — the aggregation targets are reachable ──────────────────────────────────────────

def test_dermatologist_ladders_up_to_doctor():
    rungs = W.hypernym_rungs("dermatologist", "Person")
    assert rungs, "dermatologist must produce a ladder"
    assert rungs[0] == ("dermatologist", "specialist")
    assert "doctor" in _parents(rungs), f"expected doctor in {rungs}"


def test_physician_synonym_bridges_to_doctor():
    # physician's WordNet synset IS doctor.n.01 (a synonym) — the bridge rung must still file it
    # UNDER 'doctor' so 'how many doctors' aggregates dermatologist AND physician together.
    rungs = W.hypernym_rungs("physician", "Person")
    assert ("physician", "doctor") in rungs, f"expected physician→doctor bridge in {rungs}"


def test_handbag_ladders_to_bag_and_container():
    rungs = W.hypernym_rungs("handbag", "Object")
    assert ("handbag", "bag") in rungs
    assert "container" in _parents(rungs)


def test_tournament_ladders_to_contest_and_event():
    rungs = W.hypernym_rungs("tournament", "Event")
    assert ("tournament", "contest") in rungs
    assert "event" in _parents(rungs)


def test_poodle_reaches_animal_seeded_root():
    # poodle→dog→domestic_animal→animal — and STOPS at animal (organism is generic).
    rungs = W.hypernym_rungs("poodle", "Animal")
    assert ("poodle", "dog") in rungs
    assert "animal" in _parents(rungs)
    assert "organism" not in _parents(rungs) and "living_thing" not in _parents(rungs)


# ── 2. THE HARD LINE — a NAME/instance never ladders ─────────────────────────────────────────────

def test_proper_name_gets_no_ladder():
    # A restaurant NAME (proper noun) has no common-noun synset → no subclass_of ever emitted.
    assert W.hypernym_rungs("nobu", "Person") == []
    assert W.hypernym_rungs("nobu", "Object") == []


def test_multiword_event_title_gets_no_ladder():
    # A named event title ("Run for the Cure") must never ladder — it is a memory, not a type.
    assert W.hypernym_rungs("Run for the Cure", "Event") == []


def test_scalar_surface_never_ladders():
    # A leaked scalar (IP / bare number / email / date) is a value, never a type.
    for scalar in ("192.168.1.1", "45", "user@example.com", "2026-07-22", "3.5"):
        assert W.hypernym_rungs(scalar, "Object") == [], f"{scalar} must not ladder"


# ── 3. DETERMINISM — identical ladder across runs, and stable synset selection ───────────────────

def test_determinism_same_input_same_ladder():
    for term, typ in [("dermatologist", "Person"), ("handbag", "Object"),
                      ("tournament", "Event"), ("physician", "Person")]:
        a = W.hypernym_rungs(term, typ)
        b = W.hypernym_rungs(term, typ)
        c = W.hypernym_rungs(term, typ)
        assert a == b == c, f"non-deterministic ladder for {term}: {a} vs {b} vs {c}"


def test_gliner_type_steers_synset_deterministically():
    # The GLiNER2 type biases sense selection deterministically; each call is stable.
    p = W.hypernym_rungs("specialist", "Person")
    assert p == W.hypernym_rungs("specialist", "Person")
    assert p and p[0][0] == "specialist"


# ── 4. BOUNDED — depth cap + generic-root termination ────────────────────────────────────────────

def test_depth_cap_respected():
    rungs = W.hypernym_rungs("dermatologist", "Person", max_rungs=2)
    assert len(rungs) == 2


def test_never_emits_universal_root():
    # No ladder may terminate in a WordNet upper-ontology root — those carry no classification signal.
    banned = {"entity", "physical_entity", "abstraction", "thing", "object",
              "whole", "artifact", "organism", "living_thing", "psychological_feature"}
    for term, typ in [("dermatologist", "Person"), ("handbag", "Object"),
                      ("restaurant", "Object"), ("tournament", "Event"), ("poodle", "Animal")]:
        rungs = W.hypernym_rungs(term, typ)
        assert not (_parents(rungs) & banned), f"{term} laddered into a universal root: {rungs}"


def test_wordnet_miss_returns_empty_for_uncompoundable_phrase():
    # A phrase with a closed-class CONNECTOR ("Run FOR THE Cure") is not an endocentric nominal
    # compound → never head-reduced → [] → the caller's async LLM climb is the fallback.
    assert W.hypernym_rungs("run for the cure", "Event") == []
    # A single novel token WordNet lacks also returns [] (no head to reduce to).
    assert W.hypernym_rungs("zorptastic", "Object") == []


# ── 5. GAP A — synset disambiguation lands member + category on the SAME chain ───────────────────

def test_gap_a_lime_context_resolves_to_fruit_not_chemical():
    # MFS picks lime→calcium_hydroxide (the chemical). With the citrus-fruit CONTEXT, the closure-
    # containment rule deterministically resolves the FRUIT sense (lime.n.06 under citrus).
    rungs = W.hypernym_rungs("lime", "Object", context_hypernym="citrus fruit")
    assert ("lime", "citrus") in rungs, f"lime must land under citrus (fruit), got {rungs}"
    assert "calcium hydroxide" not in _parents(rungs)


def test_gap_a_citrus_members_share_one_chain():
    # orange / lemon / lime + the category all pass through `citrus → edible fruit` — same chain.
    cat = W.hypernym_rungs("citrus fruit", "Object")
    assert "edible fruit" in _parents(cat)
    for m in ("orange", "lemon", "lime"):
        rungs = W.hypernym_rungs(m, "Object", context_hypernym="citrus fruit")
        assert ("edible fruit" in _parents(rungs)) and (m, "citrus") in rungs, f"{m}: {rungs}"


def test_gap_a_lime_ranked_lexname_without_context():
    # Even with NO context, an Object-typed edible prefers noun.food over noun.substance (ranked
    # lexname) → the fruit sense, not the chemical. (Untyped stays MFS = honest under-capture.)
    rungs = W.hypernym_rungs("lime", "Object")
    assert ("lime", "citrus") in rungs, f"ranked lexname should pick the fruit sense, got {rungs}"


# ── 6. GAP B — multiword type WordNet lacks → HEAD-noun reduction ─────────────────────────────────

def test_gap_b_movie_festival_reduces_to_head():
    # "movie festival" has no synset; the UD head is the rightmost noun "festival". Emit the
    # compound→head bridge, then ladder the head (Event-typed → the event 'festival' sense).
    rungs = W.hypernym_rungs("movie festival", "Event")
    assert rungs and rungs[0] == ("movie festival", "festival"), rungs


def test_gap_b_charity_golf_tournament_reduces_to_tournament():
    rungs = W.hypernym_rungs("charity golf tournament", "Event")
    assert rungs[0] == ("charity golf tournament", "tournament")
    assert "contest" in _parents(rungs) and "event" in _parents(rungs)


def test_gap_b_head_reduction_is_deterministic():
    a = W.hypernym_rungs("charity golf tournament", "Event")
    b = W.hypernym_rungs("charity golf tournament", "Event")
    assert a == b and a


def test_gap_b_multiword_proper_name_never_head_reduces():
    # THE HARD LINE at the module boundary: a multiword surface with a connector is a title/phrase,
    # never a common-noun compound → no head reduction, no ladder.
    assert W.hypernym_rungs("taste of the danforth", "Event") == []
    assert W.hypernym_rungs("bank of montreal", "Organization") == []


# ── 7. CROSS-SYNONYM node aliases — a laddered node carries its WordNet synset synonyms ────────────
# The ladder emits a hypernym node under its synset's PRIMARY lemma ("citrus"); a member ladders
# UNDER that surface. But a user may query the type by a synset SYNONYM ("how many citrus FRUITS").
# `synset_synonyms` returns the node's co-synset lemmas so ingest can register them as aliases,
# collapsing "citrus fruit" and "citrus" onto ONE node UUID. WordNet synonymy = synset membership.

def test_synset_synonyms_citrus_includes_citrus_fruit():
    # citrus.n.01 lemmas = {citrus, citrus_fruit, citrous_fruit}. The node emits "citrus"; the query
    # surface "citrus fruit" must be recoverable as a synonym so both ground the SAME node.
    syns = W.synset_synonyms("citrus")
    assert "citrus fruit" in syns, f"expected 'citrus fruit' synonym of 'citrus', got {syns}"


def test_synset_synonyms_excludes_the_surface_itself_and_is_bounded():
    syns = W.synset_synonyms("citrus")
    assert "citrus" not in syns  # never alias a node to its own surface
    assert len(syns) <= W._SYNONYM_CAP  # bounded


def test_synset_synonyms_deterministic():
    for term in ("citrus", "doctor", "produce"):
        assert W.synset_synonyms(term) == W.synset_synonyms(term) == W.synset_synonyms(term)


def test_synset_synonyms_filters_non_lexical_forms():
    # doctor.n.01 lemmas include "Dr." (punctuation) — an alias must be a real word surface, never a
    # digit/punctuation abbreviation. Real word synonyms (physician/doc/medico) still come through.
    syns = W.synset_synonyms("doctor")
    assert "dr." not in syns and not any(any(c.isdigit() for c in s) for s in syns)
    assert "physician" in syns


def test_synset_synonyms_hard_line_name_and_scalar_get_nothing():
    # THE HARD LINE: a NAME/instance is never a type node → no synset → no synonyms. A scalar
    # (digits/punctuation) is a value, never a type. Both must return [] (nothing to alias).
    assert W.synset_synonyms("nobu") == []
    for scalar in ("192.168.1.1", "45", "user@example.com", "2026-07-22"):
        assert W.synset_synonyms(scalar) == []


def test_synset_synonyms_no_synonyms_returns_empty():
    # A term whose primary synset has a single lemma has no co-synonyms → [] (no spurious alias).
    assert W.synset_synonyms("zorptastic") == []
