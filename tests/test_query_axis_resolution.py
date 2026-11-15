"""Unit tests for AXIS-SCOPED DETERMINISTIC WALK axis resolution
(DESIGN-hierarchy-ladder-and-growth.md §"Query model — axis-scoped deterministic
walk, NOT a post-hoc gate").

Covers the pure, DB-free deterministic surfaces:

  - _member_fact_passes_axis(rel, axis, classification_rels)
        — the per-fact axis filter: a MEMBERSHIP query drops a reached member's
          CLASSIFICATION chain (instance_of/subclass_of); scalars + membership
          edges pass. A CLASSIFICATION query keeps everything.
  - _resolve_query_axis(path, query_text, db)
        — deterministic axis pick from the resolved scope's defining rels
          (is_hierarchy_rel) + minimal "what is / what kind" surface cues.

_get_hierarchy_rels() is DB-backed but falls back to the (empty in test) startup
cache, so axis resolution from surface cues + the membership default is exercised
without a live DB. The hierarchy-rel-set branch is exercised by injecting the
classification set directly into _member_fact_passes_axis.
"""
from src.api import main as m
from src.api.models import QueryPath


# Closed classification set (instance_of/subclass_of/is_a) — the CLASSIFICATION axis.
CLASSIFICATION = frozenset({"instance_of", "subclass_of", "is_a"})


# ── _member_fact_passes_axis ────────────────────────────────────────────────
def test_membership_drops_classification_chain():
    # "Luna instance_of poodle" must NOT survive a membership query.
    assert not m._member_fact_passes_axis("instance_of", "membership", CLASSIFICATION)
    assert not m._member_fact_passes_axis("subclass_of", "membership", CLASSIFICATION)


def test_membership_keeps_membership_edges():
    for rel in ("has_pet", "parent_of", "spouse", "member_of", "part_of"):
        assert m._member_fact_passes_axis(rel, "membership", CLASSIFICATION), rel


def test_membership_keeps_scalars():
    # Scalar attribute rels (age, pref_name) are NOT classification rels → pass.
    assert m._member_fact_passes_axis("age", "membership", CLASSIFICATION)
    assert m._member_fact_passes_axis("pref_name", "membership", CLASSIFICATION)


def test_classification_keeps_classification_chain():
    # On a classification query the chain IS the answer.
    assert m._member_fact_passes_axis("instance_of", "classification", CLASSIFICATION)
    assert m._member_fact_passes_axis("subclass_of", "classification", CLASSIFICATION)


def test_classification_keeps_membership_edges_too():
    # Membership edges connect the walk; not dropped on a classification query.
    assert m._member_fact_passes_axis("has_pet", "classification", CLASSIFICATION)


def test_no_axis_passes_everything():
    # axis None (flag off / unresolved) → legacy: nothing filtered.
    assert m._member_fact_passes_axis("instance_of", None, CLASSIFICATION)
    assert m._member_fact_passes_axis("has_pet", None, CLASSIFICATION)


def test_case_insensitive():
    assert not m._member_fact_passes_axis("INSTANCE_OF", "membership", CLASSIFICATION)


# ── _resolve_query_axis ─────────────────────────────────────────────────────
def test_inactive_scope_returns_none():
    p = QueryPath()  # scope_active defaults False
    assert m._resolve_query_axis(p, "tell me about my family", db=None) is None


def test_membership_default_for_family_query():
    # Family taxonomy's defining rels are membership (non-hierarchy) rels. With no
    # classification cue, axis defaults to membership.
    p = QueryPath(relationship_rels=["has_pet", "parent_of", "spouse"], scope_active=True)
    assert m._resolve_query_axis(p, "tell me about my family", db=None) == "membership"


def test_classification_cue_what_is():
    p = QueryPath(relationship_rels=["has_pet"], scope_active=True)
    assert m._resolve_query_axis(p, "what is Luna", db=None) == "classification"


def test_classification_cue_what_kind():
    p = QueryPath(relationship_rels=["has_pet"], scope_active=True)
    assert m._resolve_query_axis(p, "what kind of animal do I have", db=None) == "classification"


def test_classification_when_defining_rels_are_all_hierarchy(monkeypatch):
    # When the scope's defining rels are PREDOMINANTLY hierarchy rels (is_hierarchy_rel),
    # axis = classification even without a surface cue ("my animals" routes to subclass_of).
    monkeypatch.setattr(m, "_get_hierarchy_rels", lambda: frozenset({"instance_of", "subclass_of"}))
    p = QueryPath(relationship_rels=["subclass_of"], scope_active=True)
    assert m._resolve_query_axis(p, "my animals", db=None) == "classification"


def test_membership_when_defining_rels_mixed(monkeypatch):
    # Mixed scope (a hierarchy rel AND a membership rel) is NOT pure-classification →
    # falls through to the membership default (no cue).
    monkeypatch.setattr(m, "_get_hierarchy_rels", lambda: frozenset({"instance_of", "subclass_of"}))
    p = QueryPath(relationship_rels=["subclass_of", "has_pet"], scope_active=True)
    assert m._resolve_query_axis(p, "tell me about my family", db=None) == "membership"


def test_fail_safe_on_bad_path_returns_none():
    # A bad object that raises on attribute access must not blow up — returns None.
    class Boom:
        @property
        def scope_active(self):
            raise RuntimeError("boom")

    assert m._resolve_query_axis(Boom(), "anything", db=None) is None
