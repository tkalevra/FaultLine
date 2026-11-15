"""Verb-lift RELABEL post-pass — the LLM /extract/rewrite lane coerces a novel domain
predicate ("CVE affects Exchange") onto the nearest seeded rel (located_in/related_to)
instead of GROWING it. These tests pin the deterministic fix:

  * predicate_span.lift_predicate(content_verb_only=True) lifts a genuine content verb but
    REFUSES a copula / auxiliary / light verb (so the seeded copula/possessive/scalar seams
    keep their edges).
  * main._relabel_coerced_rels replaces a coerced generic with the lifted USER VERB only when
    the verb is genuinely novel, and KILLS the verb-as-entity leak.

Subject-agnostic: proven across security (CVE), biology (enzyme) and law (case) predicates.
No DB is needed — schema_name=None skips ontology resolution (everything reads as novel), so
these unit-test the coercion path directly.
"""
from src.extraction import predicate_span as ps
from src.api.main import _relabel_coerced_rels


# ---------------------------------------------------------------- lift_predicate content-verb-only
def test_lift_content_verb_fires_multidomain():
    assert ps.lift_predicate(
        "CVE-2026-1234 affects Microsoft Exchange.",
        "cve-2026-1234", "microsoft exchange", content_verb_only=True) == "affect"
    assert ps.lift_predicate(
        "The enzyme phosphorylates the substrate.",
        "enzyme", "substrate", content_verb_only=True) == "phosphorylate"
    assert ps.lift_predicate(
        "Case A overrules Case B.",
        "case a", "case b", content_verb_only=True) == "overrule"


def test_lift_copula_and_light_verbs_refused_under_content_only():
    # copula "is" — owned by the seeded copula seam
    assert ps.lift_predicate(
        "My mother is Diane.", "mother", "diane", content_verb_only=True) is None
    # light verb "have" — owned by the seeded possessive/has_pet seam
    assert ps.lift_predicate(
        "I have a dog Rex.", "i", "dog", content_verb_only=True) is None


def test_lift_light_verb_still_folds_when_flag_off():
    # additive verb-lift (lift_edges_from_entities) keeps its light-verb fold — flag default False
    assert ps.lift_predicate("I have a dog Rex.", "i", "dog") == "has"


# ---------------------------------------------------------------- _relabel_coerced_rels
def test_relabel_replaces_coerced_generic_with_user_verb():
    triples = [
        {"subject": "cve-2026-1234", "rel_type": "located_in",
         "object": "microsoft exchange"},
    ]
    out = _relabel_coerced_rels(
        triples, "CVE-2026-1234 affects Microsoft Exchange.", schema_name=None)
    assert len(out) == 1
    assert out[0]["rel_type"] == "affect"          # coerced located_in → user's verb
    assert out[0]["subject"] == "cve-2026-1234"
    assert out[0]["object"] == "microsoft exchange"


def test_relabel_kills_verb_as_entity_leak():
    triples = [
        {"subject": "cve-2026-1234", "rel_type": "located_in",
         "object": "microsoft exchange"},
        # LLM nominalized the verb into a junk entity node
        {"subject": "affect", "rel_type": "instance_of", "object": "concept"},
    ]
    out = _relabel_coerced_rels(
        triples, "CVE-2026-1234 affects Microsoft Exchange.", schema_name=None)
    rels = {(t["subject"], t["rel_type"], t["object"]) for t in out}
    assert ("cve-2026-1234", "affect", "microsoft exchange") in rels
    assert ("affect", "instance_of", "concept") not in rels   # junk entity dropped
    assert len(out) == 1


def test_relabel_leaves_agreeing_edge_untouched():
    # LLM already used the user's verb → no change, no drop.
    triples = [
        {"subject": "case a", "rel_type": "overrule", "object": "case b"},
    ]
    out = _relabel_coerced_rels(triples, "Case A overrules Case B.", schema_name=None)
    assert out[0]["rel_type"] == "overrule"
    assert len(out) == 1


def test_relabel_skips_first_person_user_subject():
    # subject renders as "user" (not in the source surface) → lift bails → seeded edge kept.
    triples = [
        {"subject": "user", "rel_type": "lives_in", "object": "kitchener"},
    ]
    out = _relabel_coerced_rels(triples, "I live in Kitchener.", schema_name=None)
    assert out[0]["rel_type"] == "lives_in"
    assert len(out) == 1


def test_relabel_skips_scalar_object_edges():
    triples = [
        {"subject": "diane", "rel_type": "age", "object": "62", "object_type": "SCALAR"},
    ]
    out = _relabel_coerced_rels(triples, "Diane is 62 years old.", schema_name=None)
    assert out[0]["rel_type"] == "age"
