"""WOUND-MAP ROUND 4 — the two deterministic ingest-side extraction fixes (2026-11-13,
the family-onboarding corpus smoke). Every fix: deterministic only (NO LLM in the fix
path), env-flag-gated defaulting ON, OFF = byte-for-byte legacy behavior. Hermetic — no
DB, no network; the pins call the spine derive path directly (the established pattern of
tests/test_sparse_residual_fixes.py).

FIX 1 — SPINE_TURNED_AGE (wound "amara-age"). Input turn: "Our eldest, Amara, turned 14
on January 22; she's a freshman at Northgate High and carries an EpiPen for a bee-sting
allergy." Legacy behavior minted the attr `turn` ((amara, turn, "14") off the
measure-verb bare-cardinal arm, then a future-coerced event_date at ingest — recall served
"Amara's turn is 2027-01-22"). The fix: a "turned/turns N" clause (verb lemma `turn` + a
bare integer direct object ≤ 120) mints the AGE attribute — the EXACT storage the existing
bare-number person scalar frame ("X is 9" → the copula-measure branch (b)) uses: rel
"age", bare numeral string, no scalar_datatype. The optional "on <date>" stays the date
lanes' business (never duplicated).

FIX 2 — SPINE_ORDINAL_GRADE (wound "rowan-grade"). Input turn: "My stepson Rowan is 12,
seventh grade at Crestwood Middle." Legacy: nothing grade-shaped landed for Rowan (the
existing digit-grade capture — the copular at/in-PP bare-count frame, "he's in grade 4" →
(subj, grade, "4", string)) keys on a DIGIT nummod). The fix: the ordinal WORD immediately
left-adjacent to the `grade` head noun ("seventh grade" / "the seventh grade") maps
through the closed _ORDINAL_WORD_DIGIT dict (zero..twelfth) and mints the SAME storage the
digit form uses.
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

pytest.importorskip("spacy")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.extraction.linguistics as L  # noqa: E402

_REF = datetime.datetime(2026, 11, 13)

AMARA = "Our eldest, Amara, turned 14 on January 22."
ROWAN = "My stepson Rowan is 12, seventh grade at Crestwood Middle."
ROWAN_DET = "Rowan is in the seventh grade at Crestwood Middle."
TEO_DIGIT = "My son Teo is 9 and he's in grade 4 at Ridgeline Elementary."


def _facts(text, ref=_REF):
    doc = L._parse(text)
    return list(L.derive_sentence_facts(doc, ref) or [])


def _triples(text, ref=_REF):
    return [(f.subject, f.rel_type, str(f.object)) for f in _facts(text, ref)]


# ══════════════════════════════════════════════════════════════════════════════════
# FIX 1 — SPINE_TURNED_AGE

def test_turned_n_mints_age_not_turn():
    """The litmus wound sentence: 'turned 14' mints (amara, age, '14') in the SAME storage
    the bare-number person scalar frame uses (rel 'age', bare numeral, scalar_datatype
    None) — and the opaque legacy (amara, turn, '14') mint is GONE."""
    facts = _facts(AMARA)
    ages = [f for f in facts if f.rel_type == "age"]
    assert ("amara", "age", "14") in [(f.subject, f.rel_type, str(f.object))
                                      for f in ages], [str(f) for f in facts]
    assert all(f.scalar_datatype is None for f in ages), \
        [f.scalar_datatype for f in ages]  # byte-parity with the 'X is 9' age row
    assert not any(f.rel_type == "turn" for f in facts), [str(f) for f in facts]


def test_turns_n_future_shape_mints_age():
    assert ("saskia", "age", "5") in _triples("Saskia turns 5 in March.")


def test_turned_age_storage_matches_the_is9_age_frame():
    """Same rel/value/datatype triple shape as the existing age frame on the same subject
    style — one storage, not a parallel one."""
    t_turned = [f for f in _facts(AMARA) if f.rel_type == "age"][0]
    t_is9 = [f for f in _facts("Amara is 14.") if f.rel_type == "age"][0]
    assert (t_turned.subject, t_turned.rel_type, str(t_turned.object),
            t_turned.scalar_datatype) == \
           (t_is9.subject, t_is9.rel_type, str(t_is9.object), t_is9.scalar_datatype)


def test_turned_adjective_complement_never_mints_age():
    """Negative guard: 'turned sour' — no bare integer direct object, no age mint (and no
    opaque turn scalar either; the state reading owns it)."""
    for s in ("The meeting turned sour.",
              "The milk turned overnight.",
              "The conversation turned political."):
        trips = _triples(s)
        assert not any(r == "age" for _s, r, _o in trips), (s, trips)


def test_turned_large_number_never_mints_age():
    """A bare integer ABOVE the human-age ceiling (120) is not an age — the legacy
    verb-lemma measure scalar keeps it."""
    trips = _triples("The odometer turned 90000.")
    assert not any(r == "age" for _s, r, _o in trips), trips


def test_turned_age_off_is_byte_parity_legacy(monkeypatch):
    """OFF-state byte-parity: SPINE_TURNED_AGE forced off reproduces the pre-change
    (measured) legacy output exactly — the verb-lemma `turn` string scalar, no age row."""
    monkeypatch.setattr(L, "SPINE_TURNED_AGE", False)
    facts = _facts(AMARA)
    assert [(f.subject, f.rel_type, str(f.object), f.scalar_datatype)
            for f in facts] == [("amara", "turn", "14", "string")], [str(f) for f in facts]


# ══════════════════════════════════════════════════════════════════════════════════
# FIX 2 — SPINE_ORDINAL_GRADE

def test_ordinal_grade_word_mints_the_digit_storage():
    """The litmus wound sentence: 'seventh grade' → (rowan, grade, '7') in the SAME
    storage the digit form uses (scalar_datatype 'string')."""
    facts = _facts(ROWAN)
    got = [(f.subject, f.rel_type, str(f.object), f.scalar_datatype) for f in facts]
    assert ("rowan", "grade", "7", "string") in got, got


def test_ordinal_grade_determiner_shape_mints():
    assert ("rowan", "grade", "7", "string") in [
        (f.subject, f.rel_type, str(f.object), f.scalar_datatype)
        for f in _facts(ROWAN_DET)]


def test_ordinal_grade_map_covers_the_school_range():
    for word, digit in [("first", "1"), ("third", "3"), ("fifth", "5"),
                        ("eighth", "8"), ("ninth", "9"), ("tenth", "10"),
                        ("eleventh", "11"), ("twelfth", "12")]:
        s = f"Wren is in {word} grade at Ridgeline Elementary."
        got = [(f.subject, f.rel_type, str(f.object), f.scalar_datatype)
               for f in _facts(s)]
        assert ("wren", "grade", digit, "string") in got, (s, got)


def test_ordinal_grade_storage_matches_the_digit_grade_frame():
    """One storage: the ordinal row is byte-identical in shape to the existing digit-grade
    row ('in grade 4' → (subj, grade, '4', string))."""
    t_ord = [f for f in _facts(ROWAN_DET) if f.rel_type == "grade"][0]
    t_dig = [f for f in _facts("Rowan is in grade 7 at Crestwood Middle.")
             if f.rel_type == "grade"][0]
    assert (t_ord.subject, t_ord.rel_type, t_ord.scalar_datatype) == \
           (t_dig.subject, t_dig.rel_type, t_dig.scalar_datatype)


def test_digit_grade_capture_is_untouched():
    """The pre-existing digit form still mints through its own chain (no regression, no
    double mint from the ordinal chain)."""
    got = [(f.subject, f.rel_type, str(f.object)) for f in _facts(TEO_DIGIT)]
    assert ("teo", "age", "9") in got, got
    assert ("he", "grade", "4") in got, got
    assert sum(1 for _s, r, _o in got if r == "grade") == 1, got


def test_ordinal_non_grade_noun_never_mints():
    """Negative guard: grade-noun head adjacency is the whole gate — 'seventh street' /
    'the seventh option' never mint a grade row."""
    for s in ("She lives on seventh street.",
              "The seventh option was rejected.",
              "We took the third exit."):
        trips = _triples(s)
        assert not any(r == "grade" for _s, r, _o in trips), (s, trips)


def test_ordinal_grade_off_is_byte_parity_legacy(monkeypatch):
    """OFF-state byte-parity: SPINE_ORDINAL_GRADE forced off reproduces the pre-change
    (measured) legacy output exactly — only the educated_at row, no grade-shaped mint."""
    monkeypatch.setattr(L, "SPINE_ORDINAL_GRADE", False)
    got = [(f.subject, f.rel_type, str(f.object), f.scalar_datatype)
           for f in _facts(ROWAN)]
    assert got == [("rowan", "educated_at", "crestwood middle", None)], got
    got_det = [(f.subject, f.rel_type, str(f.object))
               for f in _facts(ROWAN_DET)]
    assert ("rowan", "grade", "7") not in got_det, got_det


# ══════════════════════════════════════════════════════════════════════════════════
# Flag wiring (structural)

def test_flags_default_on_and_follow_the_house_pattern():
    """Both flags exist, default ON, and OFF honors the full house negation set
    (0/false/no/off) per the established env-flag pattern."""
    assert L.SPINE_TURNED_AGE is True
    assert L.SPINE_ORDINAL_GRADE is True
    import importlib
    for off in ("0", "false", "no", "off"):
        os.environ["SPINE_TURNED_AGE"] = off
        os.environ["SPINE_ORDINAL_GRADE"] = off
        try:
            importlib.reload(L)
            assert L.SPINE_TURNED_AGE is False, off
            assert L.SPINE_ORDINAL_GRADE is False, off
        finally:
            os.environ.pop("SPINE_TURNED_AGE", None)
            os.environ.pop("SPINE_ORDINAL_GRADE", None)
    importlib.reload(L)  # restore the module for the other pins
    assert L.SPINE_TURNED_AGE is True and L.SPINE_ORDINAL_GRADE is True


def test_ordinal_word_map_is_the_closed_school_dict():
    assert L._ORDINAL_WORD_DIGIT["seventh"] == "7"
    assert L._ORDINAL_WORD_DIGIT["twelfth"] == "12"
    assert set(L._ORDINAL_WORD_DIGIT.values()) <= {
        str(n) for n in range(0, 13)}
