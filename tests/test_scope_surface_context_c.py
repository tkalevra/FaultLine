"""HOLE #2 — surface the short-term Class-C `context` lane under active scope.

Pins `_scope_project_qdrant_lane` (main.py), the pure DB-free helper the /query
scope-projection gate calls. The behaviour under test:

  • The precision firewall is PRESERVED: a STRUCTURED Class-C fact whose rel is outside
    the concept scope is STILL dropped (the fuzzy-taxonomy-widens-scope guard, milestone
    398a4fb0). An in-scope structured fact is always kept. These must NOT regress.
  • Under SCOPE_SURFACE_CONTEXT_C (flag), an unstructured `context` blob — which can never
    satisfy a concept scope and was dropped wholesale — is RETAINED iff its cosine relevance
    clears the floor, and is demoted to the HELD band (confidence < the recall assert floor)
    + pinned Class C so it sorts BELOW A/B. A `context` blob carries no rel and so can never
    widen or pollute the A/B structured answer.
  • Flag OFF (default) → byte-identical to the prior drop-wholesale behaviour.

DB-free, deterministic (rel membership + numeric cosine floor). Companion to
DEV/review/type-or-net-coverage.md (Hole #2) and DEV/DESIGN-type-or-net-coverage-fixes.md.
"""
import pytest

from src.api import main as m


# Concept scope for a "family"-shaped query: the membership/kin rels only.
SCOPE = {"has_pet", "parent_of", "child_of", "spouse", "member_of"}
FLOOR = 0.55
HELD = 0.30


def _ctx(score, conf=0.9, text="I was feeling anxious about the move"):
    """An unstructured store_context Class-C blob as it arrives from the Qdrant lane."""
    return {"rel_type": "context", "object": text, "qdrant_score": score,
            "confidence": conf, "fact_class": "C", "source_table": "staged_facts"}


def _structured(rel, score=0.9, conf=0.8):
    return {"rel_type": rel, "subject": "user", "object": "x", "qdrant_score": score,
            "confidence": conf, "fact_class": "C", "source_table": "staged_facts"}


# ─────────────────────────────────────────────────────────────────────────────
# PRECISION FIREWALL — must hold in BOTH flag states (A/B / structured-scope safety).
# ─────────────────────────────────────────────────────────────────────────────

class TestStructuredScopePrecisionPreserved:

    @pytest.mark.parametrize("surface", [False, True])
    def test_in_scope_structured_fact_always_kept(self, surface):
        kept, surfaced = m._scope_project_qdrant_lane(
            [_structured("has_pet")], SCOPE,
            surface_context=surface, relevance_floor=FLOOR, held_conf=HELD)
        assert len(kept) == 1 and surfaced == 0

    @pytest.mark.parametrize("surface", [False, True])
    def test_out_of_scope_structured_fact_always_dropped(self, surface):
        # A structured C fact with a concrete OUT-OF-SCOPE rel is dropped even with the
        # flag ON — surfacing context blobs must NOT loosen structured aspect precision.
        kept, surfaced = m._scope_project_qdrant_lane(
            [_structured("favorite_color")], SCOPE,
            surface_context=surface, relevance_floor=FLOOR, held_conf=HELD)
        assert kept == [] and surfaced == 0


# ─────────────────────────────────────────────────────────────────────────────
# FLAG OFF (default) — context blobs dropped wholesale under scope (prior behaviour).
# ─────────────────────────────────────────────────────────────────────────────

class TestFlagOffIsPriorBehaviour:

    def test_context_blob_dropped_when_flag_off(self):
        kept, surfaced = m._scope_project_qdrant_lane(
            [_ctx(0.99)], SCOPE,
            surface_context=False, relevance_floor=FLOOR, held_conf=HELD)
        assert kept == [] and surfaced == 0

    def test_mixed_lane_flag_off_only_in_scope_survives(self):
        lane = [_ctx(0.99), _structured("has_pet"), _structured("favorite_color")]
        kept, surfaced = m._scope_project_qdrant_lane(
            lane, SCOPE, surface_context=False, relevance_floor=FLOOR, held_conf=HELD)
        assert [f["rel_type"] for f in kept] == ["has_pet"] and surfaced == 0


# ─────────────────────────────────────────────────────────────────────────────
# FLAG ON — relevance-gated surfacing into the HELD band, ranked below A/B.
# ─────────────────────────────────────────────────────────────────────────────

class TestFlagOnSurfacesHeldRelevant:

    def test_relevant_context_blob_surfaced_and_demoted_to_held(self):
        blob = _ctx(0.80, conf=0.9)
        kept, surfaced = m._scope_project_qdrant_lane(
            [blob], SCOPE, surface_context=True, relevance_floor=FLOOR, held_conf=HELD)
        assert surfaced == 1 and len(kept) == 1
        f = kept[0]
        # Demoted to held band (< the 0.35 recall assert floor) and pinned Class C.
        assert f["confidence"] == HELD
        assert f["fact_class"] == "C"
        assert f["_scope_surfaced_held"] is True

    def test_low_relevance_context_blob_still_dropped(self):
        # Below the stricter scope floor → NOT dragged into a scoped answer.
        kept, surfaced = m._scope_project_qdrant_lane(
            [_ctx(0.40)], SCOPE, surface_context=True, relevance_floor=FLOOR, held_conf=HELD)
        assert kept == [] and surfaced == 0

    def test_demotion_never_raises_a_low_confidence(self):
        # A blob already below held_conf keeps its lower value (min), never bumped up.
        kept, _ = m._scope_project_qdrant_lane(
            [_ctx(0.90, conf=0.10)], SCOPE,
            surface_context=True, relevance_floor=FLOOR, held_conf=HELD)
        assert kept[0]["confidence"] == 0.10

    def test_surfaced_context_sorts_below_ab_in_confidence_gate(self):
        # End-to-end ordering: an A/B db fact must lead; the surfaced held context blob
        # (Class C) sorts LAST via apply_confidence_gate's class-priority key.
        ab = {"rel_type": "has_pet", "fact_class": "A", "confidence": 1.0,
              "definition": "user has a pet dog"}
        blob = _ctx(0.80, conf=0.9)
        kept, _ = m._scope_project_qdrant_lane(
            [blob], SCOPE, surface_context=True, relevance_floor=FLOOR, held_conf=HELD)
        merged = m.apply_confidence_gate([ab], qdrant_facts=kept)
        assert merged[0]["fact_class"] == "A"
        assert merged[-1].get("_scope_surfaced_held") is True

    def test_structured_in_scope_and_context_both_kept_flag_on(self):
        lane = [_ctx(0.80), _structured("has_pet"), _structured("favorite_color", score=0.9)]
        kept, surfaced = m._scope_project_qdrant_lane(
            lane, SCOPE, surface_context=True, relevance_floor=FLOOR, held_conf=HELD)
        rels = sorted(f["rel_type"] for f in kept)
        assert rels == ["context", "has_pet"]        # favorite_color (out-of-scope) dropped
        assert surfaced == 1
