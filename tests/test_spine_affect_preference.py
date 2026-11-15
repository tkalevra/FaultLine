"""Regression gate for the SPINE affect/preference capture seam.

THE GAP: when SENTENCE_PIPELINE is ON, /harvest-spans returns early from
_harvest_via_sentence_pipeline (the spine) and NEVER reaches the legacy
_detect_feeling_states / _detect_preference_states seams (those run only AFTER the early
return). So on the spine path "My favorite color is blue" was mangled into junk
((user, owns, "favorite color") + (color, has_state, blue)) and "I feel worried" / "I'm
excited" dropped entirely.

THE FIX (src/api/main.py, in _harvest_via_sentence_pipeline, guarded by
SPINE_AFFECT_PREFERENCE, default ON): run the SAME proven-live deterministic seams over
the spine's declarative text, UNION their growth-ready edges (preference rel + the SEEDED
`feels` rel routed Class C), and SUPPRESS the possessive/copula-state junk the deriver
produced for the possessive-predication construction the preference seam now OWNS. The
bare-copula feeling ("I'm excited") is captured deterministically via analyze_copula
(NO LLM, NO emotion word list).

These tests verify:
  * the preference seam captures (user, favorite_<x>, <value>) — subject-agnostic
  * the feel-verb + bare-copula feeling seams capture (user, feels, <emotion>) — Class C
    (NO fact_provenance on the feeling edge → /ingest router defaults llm_inferred → C)
  * the deriver's possessive/copula-state JUNK for the preference construction is dropped
    by the seam's suppression logic (no (user, owns, "favorite color"), no
    (color, has_state, blue))
  * negation + questions are not captured
"""
import datetime

import pytest

from src.api.main import _detect_feeling_states, _detect_preference_states
from src.extraction.linguistics import (
    analyze_copula,
    derive_sentence_facts,
    linguistics_available,
)

pytestmark = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine seams no-op",
)

_REF = datetime.date(2023, 6, 1)


# ── PREFERENCE SEAM ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,rel,value", [
    ("My favorite color is blue.", "favorite_color", "blue"),
    ("My favorite food is sushi.", "favorite_food", "sushi"),
    ("My favorite editor is vim.", "favorite_editor", "vim"),
])
def test_preference_seam_captures_clean_edge(text, rel, value):
    edges = _detect_preference_states(text)
    assert len(edges) == 1
    e = edges[0]
    assert e["subject"] == "user"
    assert e["rel_type"] == rel
    assert e["object"] == value
    # preferences are durable → user_stated provenance (Class A/B, not the transient C lane)
    assert e["fact_provenance"] == "user_stated"


def test_preference_seam_skips_question():
    assert _detect_preference_states("What is my favorite color?") == []


# ── FEELING SEAM (feel-verb) — Class C (NO provenance) ───────────────────────────────────

@pytest.mark.parametrize("text,emotion", [
    ("I feel worried about the deadline.", "worried"),
    ("I'm feeling anxious.", "anxious"),
    ("I felt exhausted yesterday.", "exhausted"),
])
def test_feelverb_seam_captures_class_c_feeling(text, emotion):
    edges = _detect_feeling_states(text)
    objs = {e["object"] for e in edges}
    assert emotion in objs
    for e in edges:
        assert e["subject"] == "user"
        assert e["rel_type"] == "feels"
        # NO fact_provenance → /ingest router defaults to llm_inferred → `feels`(class C).
        assert "fact_provenance" not in e


# ── FEELING SEAM (bare copula via analyze_copula) — the spine's deterministic complement ──

def _spine_bare_copula_feeling(text):
    """Replicate the spine seam's bare-copula feeling rule exactly (main.py)."""
    ca = analyze_copula(text)
    if (ca is not None and ca.subject_is_self and not ca.negated
            and (ca.relation or "") == "feels"
            and (ca.complement_pos or "").upper() == "ADJ"):
        comp = (ca.complement or "").strip().lower()
        if comp and len(comp) >= 3:
            return [{"subject": "user", "rel_type": "feels", "object": comp}]
    return []


@pytest.mark.parametrize("text,emotion", [
    ("I'm excited about the trip.", "excited"),
    ("I am anxious.", "anxious"),
    ("I am happy.", "happy"),
])
def test_bare_copula_feeling_captured_deterministically(text, emotion):
    edges = _spine_bare_copula_feeling(text)
    assert edges == [{"subject": "user", "rel_type": "feels", "object": emotion}]
    # NOT caught by the feel-verb regex seam (no feel-verb) — this lane is the complement.
    assert _detect_feeling_states(text) == []


@pytest.mark.parametrize("text", [
    "I am not worried.",      # negated → deferred, never captured
    "I am a teacher.",        # NOUN complement → occupation, not feels
    "My favorite color is blue.",  # possessive subject → preference, not a self-feeling
])
def test_bare_copula_feeling_does_not_overcapture(text):
    assert _spine_bare_copula_feeling(text) == []


# ── SUPPRESSION — the deriver junk for the preference construction is dropped ─────────────

def _apply_suppression(edges, preference_edges):
    """Replicate the spine seam's possessive/copula-state junk suppression (main.py)."""
    from src.ontology.canonical import normalize_rel as _norm_rel
    pref_values = {(p.get("object") or "").strip().lower() for p in preference_edges}
    pref_rels = {(p.get("rel_type") or "").strip().lower() for p in preference_edges}
    kept = []
    for e in edges:
        er = (e.get("rel_type") or "").strip().lower()
        es = (e.get("subject") or "").strip().lower()
        eo = (e.get("object") or "").strip().lower()
        if er == "owns" and es == "user" and eo:
            eo_rel = (_norm_rel(eo) or "").strip().lower() or eo.replace(" ", "_")
            if eo_rel in pref_rels:
                continue
        if er == "has_state" and eo in pref_values and es != "user":
            continue
        kept.append(e)
    return kept


def test_suppression_drops_deriver_preference_junk():
    text = "My favorite color is blue."
    # what the spine deriver actually emits for this construction (the junk):
    facts = derive_sentence_facts(text, _REF, prior_nps=[])
    deriver_edges = [
        {"subject": f.subject, "rel_type": f.rel_type, "object": f.object}
        for f in facts
    ]
    # ⚠️ CONTRACT CHANGED (SPINE_FUNCTIONAL_SLOT) — this block used to assert that the deriver
    # EMITS (user, owns, "favorite color") so the downstream suppression had something to drop.
    # That pinned the WORKAROUND, not the requirement. The possessed head bears a preference
    # SELECTOR, which makes it a Löbner FUNCTIONAL noun — an attribute SLOT, never a possessum —
    # so the deriver no longer mints it AT ALL. Suppressing at the source also fixes the path
    # this downstream lane could never reach: _detect_preference_states runs on DECLARATIVES only,
    # so on "What is my favourite colour?" it returns nothing, the suppression has no key, and the
    # junk edge survived to commit. Set SPINE_FUNCTIONAL_SLOT=false to see the legacy edge again.
    assert {"subject": "user", "rel_type": "owns", "object": "favorite color"} not in deriver_edges
    # the has_state twin IS still emitted and is still the downstream lane's job to drop
    assert {"subject": "color", "rel_type": "has_state", "object": "blue"} in deriver_edges

    pref = _detect_preference_states(text)
    kept = _apply_suppression(deriver_edges, pref)

    # BOTH junk edges are gone
    assert not any(e["rel_type"] == "owns" and e["subject"] == "user" for e in kept)
    assert not any(e["rel_type"] == "has_state" and e["object"] == "blue" for e in kept)
    # the clean preference edge stands on its own
    assert pref == [{"subject": "user", "rel_type": "favorite_color",
                     "object": "blue", "fact_provenance": "user_stated"}]


def test_suppression_is_precise_not_blanket():
    # a NON-preference owns/has_state edge must survive — suppression keys only on the
    # captured preference rel/value, never a blanket drop.
    pref = [{"subject": "user", "rel_type": "favorite_color", "object": "blue"}]
    edges = [
        {"subject": "user", "rel_type": "owns", "object": "favorite color"},   # junk → drop
        {"subject": "color", "rel_type": "has_state", "object": "blue"},        # junk → drop
        {"subject": "user", "rel_type": "owns", "object": "car"},               # real → keep
        {"subject": "printer", "rel_type": "has_state", "object": "idle"},      # real → keep
    ]
    kept = _apply_suppression(edges, pref)
    kept_set = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("user", "owns", "car") in kept_set
    assert ("printer", "has_state", "idle") in kept_set
    assert ("user", "owns", "favorite color") not in kept_set
    assert ("color", "has_state", "blue") not in kept_set
