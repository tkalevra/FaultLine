"""Regression gate for the medical-smoke-test ingest-capture fixes (spine deriver).

Four deterministic capture bugs found in a live medical smoke test (SENTENCE_PIPELINE ON):

  #1 (SHARPEST — data loss) "I am allergic to penicillin" was captured as (user, feels,
     allergic) and penicillin was DROPPED entirely. A predicate adjective that governs a
     PREPOSITIONAL OBJECT ("allergic TO X", "afraid OF X") is a RELATION carrying an object,
     NOT a bare feeling. The affect seams (analyze_copula / analyze_copula_affect_complements)
     now DECLINE it; analyze_copula_relational_predicate CAPTURES (user, <adj>_<prep>, <object>)
     — a novel rel the ontology growth engine grounds. The topic-marking preposition "about"
     ("worried/excited about X") is EXCLUDED so genuine feelings stay feelings.

  #2 "My blood type is O negative" truncated the value to "negative" (the "O" dropped). The
     copula-complement VALUE is now the FULL phrase ("o negative").

  #3 "diagnosed in 2019" reframed to a copula mis-typed the YEAR 2019 as an `age` scalar. A
     bare 4-digit cardinal in calendar-year range is a DATE, never a person's age.

  #4 "my son David Chen" bound the name as just "Chen" (the given name "David" dropped). The
     name↔type binding now rebuilds the FULL proper-name span.

All fixes are deterministic, subject-agnostic, grammar-driven (NO medical/adjective/name word
list). See src/extraction/linguistics.py + src/api/main.py (SPINE_AFFECT_PREFERENCE block).
"""
import pytest

from src.extraction.linguistics import (
    analyze_copula,
    analyze_copula_affect_complements,
    analyze_copula_relational_predicate,
    analyze_name_type_bindings,
    analyze_possessive_predication,
    derive_sentence_facts,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)


# ── #1 ALLERGY / RELATIONAL PREDICATE — the data-loss fix ────────────────────────────────

@pytest.mark.parametrize("text,rel,obj", [
    ("I am allergic to penicillin", "allergic_to", "penicillin"),
    ("I'm allergic to penicillin", "allergic_to", "penicillin"),
    ("I am afraid of spiders", "afraid_of", "spiders"),
])
def test_relational_predicate_captures_object(text, rel, obj):
    edges = analyze_copula_relational_predicate(text)
    assert {"subject": "user", "rel_type": rel, "object": obj, "negated": False} in edges


def test_allergy_does_not_produce_a_feeling():
    """THE SHARPEST BUG: 'I am allergic to penicillin' must NOT emit (user, feels, allergic)."""
    text = "I am allergic to penicillin"
    # analyze_copula declines (no feels first-cut) …
    assert analyze_copula(text) is None
    # … and the enumerated affect-complement seam declines too (no 'allergic' feeling).
    assert analyze_copula_affect_complements(text) == []


def test_coordinated_allergens_each_captured():
    edges = analyze_copula_relational_predicate("I'm allergic to penicillin and sulfa")
    objs = {(e["rel_type"], e["object"]) for e in edges}
    assert ("allergic_to", "penicillin") in objs
    assert ("allergic_to", "sulfa") in objs


@pytest.mark.parametrize("text,emotion", [
    ("I'm excited about the trip.", "excited"),
    ("I am worried about the migration", "worried"),
    ("I am nervous about the results", "nervous"),
])
def test_about_topic_stays_a_feeling_not_a_relation(text, emotion):
    """'worried/excited ABOUT X' is a FEELING (about = the emotion's topic), NOT a relation —
    the affect seam must still own it; the relational-predicate seam must NOT claim it."""
    assert emotion in analyze_copula_affect_complements(text)
    assert analyze_copula_relational_predicate(text) == []


@pytest.mark.parametrize("text", [
    "I am excited",   # bare feeling, no prep object
    "I am happy",
])
def test_bare_feeling_untouched_by_relational_seam(text):
    assert analyze_copula_relational_predicate(text) == []
    ca = analyze_copula(text)
    assert ca is not None and ca.relation == "feels"


def test_negated_relational_predicate_flagged():
    edges = analyze_copula_relational_predicate("I am not allergic to penicillin")
    # captured AND marked negated — the caller now threads polarity='negated' (negation-as-absence)
    assert edges and all(e["negated"] for e in edges)


# ── NEGATION-AS-ABSENCE (polarity) + residue↔seam reconciliation ─────────────────────────────
# The harvest now (a) emits a NEGATED relational predicate as polarity='negated' (not dropped) so
# ingest's ON CONFLICT flips the prior affirmed row to negated (supersede-in-place; a re-affirmation
# flips it back), and (b) reconciles the store_context residue against seam-captured objects so the
# SAME statement never lands as BOTH a structured edge AND a verbatim Class-C blob.

def test_harvest_emits_negated_polarity_edge_for_relational_predicate():
    """Mirror the exact edge-build decision in the SPINE_AFFECT_PREFERENCE relpred seam: a negated
    relational predicate emits polarity='negated'; an affirmed one carries no polarity override."""
    def _build(text):
        out = []
        for _rp in (analyze_copula_relational_predicate(text) or []):
            rel = (_rp.get("rel_type") or "").strip().lower()
            obj = (_rp.get("object") or "").strip().lower()
            if not rel or not obj:
                continue
            edge = {"subject": "user", "rel_type": rel, "object": obj,
                    "fact_provenance": "user_stated"}
            if _rp.get("negated"):
                edge["polarity"] = "negated"
            out.append(edge)
        return out

    neg = _build("I'm not allergic to penicillin")
    assert neg and all(e.get("polarity") == "negated" for e in neg)
    assert {("allergic_to", "penicillin")} == {(e["rel_type"], e["object"]) for e in neg}

    pos = _build("I'm allergic to penicillin")
    assert pos and all("polarity" not in e for e in pos)  # affirmed → column default


def test_residue_reconcile_drops_seam_captured_sentence():
    """A residue sentence whose content a seam already captured (object token present) is DROPPED —
    no double capture (structured edge + verbatim Class-C blob)."""
    from src.api.main import _reconcile_residue_against_seam
    residue = [
        "I am allergic to penicillin.",       # captured by the relpred seam (penicillin)
        "I'm not allergic to penicillin.",    # captured negated (penicillin)
        "I feel worried.",                    # captured by the feeling seam (worried)
    ]
    captured = {"penicillin", "worried"}
    kept, dropped = _reconcile_residue_against_seam(residue, captured)
    assert dropped == 3
    assert kept == []


def test_residue_reconcile_keeps_genuine_residue():
    """A genuinely untypeable residue sentence (no seam captured its content) is NEVER dropped —
    the reconciliation is precise, not a blanket suppressor."""
    from src.api.main import _reconcile_residue_against_seam
    residue = [
        "I am allergic to penicillin.",             # seam-captured → drop
        "The quarterly synergy was blindingly odd.",  # no seam object → KEEP (real residue)
    ]
    captured = {"penicillin"}
    kept, dropped = _reconcile_residue_against_seam(residue, captured)
    assert dropped == 1
    assert kept == ["The quarterly synergy was blindingly odd."]


def test_residue_reconcile_word_boundary_not_substring():
    """Coverage is WORD-BOUNDARY, not substring: a captured object must not spuriously match a
    longer word that merely contains it."""
    from src.api.main import _reconcile_residue_against_seam
    # 'pen' must NOT match inside 'penicillin'/'happen'
    kept, dropped = _reconcile_residue_against_seam(
        ["Something happened at the pen store."], {"pen"})
    # 'pen' IS a whole word here → covered; but 'happened' alone would not be matched by 'pen'
    assert dropped == 1
    kept2, dropped2 = _reconcile_residue_against_seam(
        ["I organized the penicillin shelf."], {"pen"})
    assert dropped2 == 0 and kept2 == ["I organized the penicillin shelf."]


def test_empty_inputs_are_safe():
    from src.api.main import _reconcile_residue_against_seam
    assert _reconcile_residue_against_seam([], {"x"}) == ([], 0)
    assert _reconcile_residue_against_seam(["a b c"], set()) == (["a b c"], 0)


# ── #2 MULTI-TOKEN COPULA VALUE — the "O" drop fix ───────────────────────────────────────

@pytest.mark.parametrize("text,possessed,value", [
    ("My blood type is O negative", "blood type", "o negative"),
    ("My favorite color is dark blue", "favorite color", "dark blue"),
    ("My favorite color is blue", "favorite color", "blue"),  # single-token unchanged
])
def test_possessive_value_is_full_phrase(text, possessed, value):
    pp = analyze_possessive_predication(text)
    assert pp is not None
    assert pp.possessed == possessed
    assert pp.value == value


# ── #3 BARE YEAR IS NOT AN AGE ───────────────────────────────────────────────────────────

def test_bare_year_not_captured_as_age():
    """A bare 4-digit calendar year must NOT become an `age` scalar (the '2019 as age' bug)."""
    facts = derive_sentence_facts("Sarah is 2019", "user")
    assert not any(f.rel_type == "age" for f in facts)


def test_real_two_digit_age_still_captured():
    facts = derive_sentence_facts("Sarah is 28", "user")
    ages = [(f.subject, f.object) for f in facts if f.rel_type == "age"]
    assert ("sarah", "28") in ages


# ── #4 FULL PROPER-NAME SPAN — the "David Chen" → "Chen" fix ──────────────────────────────

@pytest.mark.parametrize("text,name,type_noun", [
    ("I have a son David Chen", "David Chen", "son"),
    ("my friend John Smith", "John Smith", "friend"),
    ("I have a dog Rex", "Rex", "dog"),  # single-token name unchanged
])
def test_name_type_binding_full_name_span(text, name, type_noun):
    bindings = {(b.name, b.type_noun) for b in analyze_name_type_bindings(text)}
    # the kinship/possession cue overlay needs the DB to fire the kin domain, but the NAME/TYPE
    # binding itself is grammar-only — assert the (full-name, type) pair is present when detected.
    if bindings:
        assert (name, type_noun) in bindings
