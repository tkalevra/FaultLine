"""Unit tests for src/extraction/predicate_span.py — the deterministic verb-lift extractor.

Phase 0 of the internal design record These are PURE-FUNCTION tests: no live
DB, no GLiNER2, no LLM. They verify that the user's own verb is lifted verbatim between a
GLiNER2-found entity pair and normalized deterministically (canonical.normalize_rel), and that
the scalar-tail caveat holds (a date/number is NEVER baked into the rel).

The load-bearing example from the report:
    "I just fixed that broken fence three weeks ago, where my goats graze"
must yield predicate fixed→fix on (i, fence) and must NOT yield (i, manages, goats) nor fold
the temporal scalar "three weeks ago" into any rel.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extraction import predicate_span as P


# ──────────────────────────────────────────────────────────────────────────────
# lift_predicate — single pair → normalized user verb
# ──────────────────────────────────────────────────────────────────────────────

class TestLiftPredicateMorphology:
    def test_fixed_to_fix(self):
        assert P.lift_predicate("I fixed the fence", "i", "fence") == "fix"

    def test_repaired_to_repair(self):
        assert P.lift_predicate("I repaired the car", "i", "car") == "repair"

    def test_installed_to_install(self):
        assert P.lift_predicate("I installed the printer", "i", "printer") == "install"

    def test_light_verb_had_an_issue_with(self):
        # light-verb fold: "had an issue with" → has_issue (canonical.normalize_rel)
        assert P.lift_predicate("I had an issue with the server", "i", "server") == "has_issue"

    def test_load_bearing_preposition_kept(self):
        # lives_in ≠ lives_at — the preposition immediately after the verb survives.
        assert P.lift_predicate("Nora lives in Toronto", "nora", "toronto") == "live_in"

    def test_leading_adverb_dropped(self):
        # "just" is an adverb between subject and verb — the lift starts at the real verb.
        assert P.lift_predicate("I just fixed the fence", "i", "fence") == "fix"


class TestLiftPredicateScalarTailCaveat:
    """RC §5: NEVER bake a date/number/scalar into the verb."""

    SENT = "I just fixed that broken fence three weeks ago, where my goats graze"

    def test_fence_pair_yields_fix_not_scalar(self):
        rel = P.lift_predicate(self.SENT, "i", "fence")
        assert rel == "fix"
        assert "three" not in rel and "week" not in rel and "ago" not in rel

    def test_no_scalar_folded_for_distant_pair(self):
        # The connecting region between fence and goats is "three weeks ago where my" — its head
        # is a NUMBER WORD (scalar tail), so NO rel is lifted (returns None), never "three_*".
        assert P.lift_predicate(self.SENT, "fence", "goats") is None

    def test_numeric_digit_head_rejected(self):
        assert P.lift_predicate("the box 3 days later the shed", "box", "shed") is None


class TestLiftPredicateFailSafe:
    def test_entity_not_in_span_returns_none(self):
        assert P.lift_predicate("I fixed the fence", "i", "rocket") is None

    def test_empty_inputs_return_none(self):
        assert P.lift_predicate("", "i", "fence") is None
        assert P.lift_predicate("I fixed the fence", "", "fence") is None
        assert P.lift_predicate("I fixed the fence", "i", "") is None

    def test_no_verb_between_pair_returns_none(self):
        # adjacency with only a determiner/pronoun → no real predicate to mint.
        assert P.lift_predicate("my dog Fraggle", "dog", "fraggle") is None

    def test_pure_copula_returns_none(self):
        # "the" between the pair — pure connector, no verb.
        assert P.lift_predicate("the fence the gate", "fence", "gate") is None


# ──────────────────────────────────────────────────────────────────────────────
# lift_edges_from_entities — GLiNER2 entity map → growth-ready edges
# ──────────────────────────────────────────────────────────────────────────────

class TestLiftEdgesFromEntities:
    SENT = "I just fixed that broken fence three weeks ago, where my goats graze"
    ENTS = {"Person": ["i"], "Object": ["fence"], "Animal": ["goats"]}

    def _edges(self):
        return P.lift_edges_from_entities(self.SENT, self.ENTS)

    def test_fix_fence_edge_present(self):
        edges = self._edges()
        triples = {(e["subject"], e["rel_type"], e["object"]) for e in edges}
        assert ("i", "fix", "fence") in triples

    def test_no_manages_goats(self):
        # The defining symptom: the closed-set scorer forced "manages goats". The verb-lift
        # must NEVER produce a "manages" rel here (it lifts the USER's verb, not a label).
        edges = self._edges()
        assert all(e["rel_type"] != "manages" for e in edges)

    def test_no_scalar_in_any_rel(self):
        edges = self._edges()
        for e in edges:
            assert "three" not in e["rel_type"]
            assert "week" not in e["rel_type"]
            assert "ago" not in e["rel_type"]

    def test_edges_are_user_stated(self):
        for e in self._edges():
            assert e["fact_provenance"] == "user_stated"
            assert e["confidence"] == 0.8

    def test_entity_types_carried(self):
        edges = self._edges()
        fence_edge = next(e for e in edges
                          if (e["subject"], e["object"]) == ("i", "fence"))
        assert fence_edge["subject_type"] == "PERSON"
        assert fence_edge["object_type"] == "OBJECT"

    def test_empty_inputs(self):
        assert P.lift_edges_from_entities("", self.ENTS) == []
        assert P.lift_edges_from_entities(self.SENT, {}) == []
        assert P.lift_edges_from_entities(self.SENT, {"Person": ["i"]}) == []  # <2 entities

    def test_self_pair_skipped(self):
        # same name twice → no self edge.
        edges = P.lift_edges_from_entities("I fixed the fence", {"Person": ["i", "i"]})
        assert edges == []

    def test_dedup_within_span(self):
        edges = self._edges()
        keys = [(e["subject"], e["rel_type"], e["object"]) for e in edges]
        assert len(keys) == len(set(keys))

    def test_catenative_to_verb_rejected(self):
        # "like to / want to / plan to <verb>" = desire/intent, NOT a stated fact → no rel.
        assert P.lift_predicate("my goats like to graze on the east side", "goats", "east side") is None
        assert P.lift_predicate("i want to buy a car", "i", "car") is None
        assert P.lift_predicate("i plan to visit paris", "i", "paris") is None

    def test_preference_and_motion_kept(self):
        # "likes <noun>" (no "to") is a real preference; "go to" is motion — both kept.
        assert P.lift_predicate("i like pizza", "i", "pizza") == "like"
        assert P.lift_predicate("i went to the store", "i", "store") == "go_to"

    def test_mental_state_of_about_rejected(self):
        # FIX #3: a cognition/desire verb governing a gerund/NP via "of"/"about" is an UNREALIZED
        # intention, NOT a stated fact → no rel (the live "think_of | brown swiss" junk).
        assert P.lift_predicate("I am thinking of getting a cow", "i", "cow") is None
        assert P.lift_predicate("I dreamed of a farm", "i", "farm") is None
        assert P.lift_predicate("I wonder about the goats", "i", "goats") is None

    def test_real_activity_with_of_still_lifts(self):
        # A non-mental-state verb with a trailing "of" is a real activity and is still lifted —
        # the rejection is bounded to the cognition-verb GRAMMATICAL class, not all "of".
        # "took care of" lifts the user's verb (light-verb 'take'); it is NOT rejected.
        assert P.lift_predicate("I took care of the fence", "i", "fence") is not None

    def test_governance_no_leap_over_intervening_entity(self):
        # USER IS TRUTH: a verb may not reach past an intervening entity to fabricate a fact.
        # "I fixed the fence … where my goats graze" → (i, fix, fence) ONLY, never (i, *, goats):
        # the user fixed the FENCE, not the goats.
        span = ("I just fixed that broken fence on the east side three weeks ago, "
                "where my goats like to graze")
        edges = P.lift_edges_from_entities(
            span, {"Person": ["i"], "Object": ["fence"], "Animal": ["goats"]})
        rels = [(e["subject"], e["rel_type"], e["object"]) for e in edges]
        assert ("i", "fix", "fence") in rels
        # fence is adjacent to i; goats is NOT (fence intervenes) → no (i, *, goats) fabrication.
        assert not any(s == "i" and o == "goats" for s, r, o in rels), \
            f"fabricated a fact the user never stated: {rels}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
