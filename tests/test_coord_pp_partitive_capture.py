"""Regression: coordinated prepositional-object / pseudo-partitive capture (SPINE_COORD_PP_DESCENT).

Cluster: COORDINATED-OBJECT / coordinated-PP capture. LongMemEval c4a1ceb8 ("how many different types
of citrus fruits …", gold=3) missed because the deriver dropped the content of a coordinated partitive
PP-object: "slices **of orange and lemon**" surfaced neither content noun — the measure noun "slices"
swallowed its ``of`` complement and the coordination tail was never expanded — so LEMON (stated ONLY in
that construction) was lost and the count came back 2, not 3.

Fix (``src/extraction/clause_pa.py``): the MinIE nmod factoring is now (a) COORDINATION-complete — one
proposition per ``conj`` conjunct of a PP-complement (UD ``conj``/``cc``; distributive coordination), (b)
extended to OBLIQUE argument heads (a measure/partitive NP inside an oblique), and (c) CHAINED (bounded
depth) so a nested partitive ("… a pitcher with slices of orange and lemon") reaches the deep content
nouns. Purely structural — no domain/measure word list. Default ON; OFF → byte-identical.

Evidence-ground: de Marneffe & Manning (Stanford typed dependencies, conj/cc); Universal Dependencies v2
coordination (first conjunct = head, tail via ``conj``); Del Corro & Gemulla (ClausIE, WWW 2013) +
Gashteovski et al. (MinIE, EMNLP 2017) minimization/factoring.

These assert the READ-path capture (the propositions the PA bridge emits) — deterministic, no LLM/DB.
Fails on the pre-fix code (obl heads never factored, POS gate rejects the ADJ-tagged conjunct head, no
chained descent); passes on the fix.
"""
import os

import pytest

from src.extraction.clause_pa import extract_propositions


def _nmod_objs(sentence):
    """Every nominal-modifier object text produced for ``sentence`` (lowercased set)."""
    return {
        a.text
        for p in extract_propositions(sentence).propositions
        if p.clause_type == "nominal_modifier"
        for a in p.args
        if a.role == "nmod"
    }


# ── POSITIVE: coordinated partitive PP-object — BOTH conjuncts captured ──────────────────────────
def test_coordinated_partitive_pp_object_captures_both_conjuncts():
    # The c4a1ceb8 construction: "slices OF orange AND lemon" — both content nouns must surface.
    objs = _nmod_objs("I served Sangria with slices of orange and lemon.")
    assert "orange" in objs, objs
    assert "lemon" in objs, objs      # the dropped second conjunct — the actual bug


def test_three_way_comma_list_with_mistagged_head_captures_all_three():
    # "of orange, lemon and lime" — spaCy tags the colour-homograph head "orange" as ADJ; the fix
    # admits the mistagged nominal head on the descent path and completes the comma+and conj chain.
    objs = _nmod_objs("I served Sangria with slices of orange, lemon and lime.")
    assert {"orange", "lemon", "lime"} <= objs, objs


def test_nested_partitive_is_descended_to_deep_content_nouns():
    # Two-level nesting: pitcher → with → slices → of → orange/lemon (the live turn-10 shape).
    objs = _nmod_objs("I served the Sangria in a large pitcher with slices of orange and lemon.")
    assert "orange" in objs and "lemon" in objs, objs


# ── NEGATIVE: a NON-coordinate PP-modifier must NOT be split / duplicated ────────────────────────
def test_noncoordinate_of_phrase_not_oversplit():
    # "malfunction OF the stand mixer" — no conj → exactly ONE nominal_modifier, unchanged.
    props = [p for p in extract_propositions("The malfunction of the stand mixer ruined dinner.")
             .propositions if p.clause_type == "nominal_modifier"]
    assert len(props) == 1, props
    assert props[0].predicate == "of"
    assert props[0].args[0].text == "stand mixer"


def test_appositive_not_affected():
    # An appositive ("my brother, a doctor") is a different construction — must stay a single appos.
    props = [p for p in extract_propositions("My brother, a doctor, lives in Ohio.").propositions
             if p.clause_type == "nominal_modifier"]
    assert any(p.predicate == "appos" and p.args[0].text == "doctor" for p in props), props
    # and no spurious nmod split introduced
    assert not any(p.predicate != "appos" for p in props), props


# ── FLAG OFF → byte-identical (the coordinated partitive drops both, as before) ──────────────────
def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setenv("SPINE_COORD_PP_DESCENT", "false")
    objs = _nmod_objs("I served Sangria with slices of orange and lemon.")
    # Pre-fix behaviour: the oblique head "slices" is never factored → no orange/lemon nmod at all.
    assert "orange" not in objs and "lemon" not in objs, objs
