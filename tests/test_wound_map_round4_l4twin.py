"""WOUND-MAP ROUND 4, follow-up — the L4 frag-twin OF-PHRASE BOUND (BUG-R4-1, 2026-11-13).

ADJUDICATION: OVERREACH. The measure-verb L4 twin-drop post-pass
(`measure_verb_l4_twins_dropped`) was contracted for ONE containment — the construction's
later-emitted twins re-file "the same measure stripped of its OF-PHRASE"
("my bouillabaisse recipe uses 800 grams of fish" → fragment (fish, quantity, "800
grams") beside the fire row (bouillabaisse recipe, quantity, "800 grams of fish")). A
bare token-subset also matches ADVERBIAL complements the contract never named:
"My commute takes 45 minutes each way" (fire value "45 minutes each way") retired the
classic (commute, take, "45 minutes") row — the row that serves "how long does my
commute take?" — and "My bike has 13 kilometers on it" retired the have-frame capture
(BUG-l4-arm-dead-emit-issue19, measured key 'bike|have|13 kilometers').

THE FIX (deterministic, no LLM): the drop fires only when the fire value contains a
partitive "of" (closed-class function word — the same lexical key the arm's unmapped
admission bound uses), and the FRAGMENT arm additionally requires the twin's tokens ⊆ the
measure HEAD before the "of". No of-phrase → adverbial extension, never a fragment → the
classic twin survives. Gated by the arm's own SPINE_POSSESSED_QUANTITY_L4 (OFF → no
fires → byte-for-byte legacy). Hermetic — no DB, no network.

Run: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_wound_map_round4_l4twin.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("spacy")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.extraction.linguistics as L  # noqa: E402


def _rows(text):
    # No database: pin the in-code cue floor (the established vm9 pattern — POSTGRES_DSN
    # popped so the rel-overlay map never resolves and the arm fires on code defaults).
    os.environ.pop("POSTGRES_DSN", None)
    return [(f.subject, f.rel_type, str(f.object), f.scalar_datatype)
            for f in L.derive_sentence_facts(text, None) or []]


COMMUTE = "My commute takes 45 minutes each way."
BIKE = "My bike has 13 kilometers on it."
RECIPE = "My bouillabaisse recipe uses 800 grams of fish."


def test_adverbial_complement_keeps_the_classic_twin_commute():
    """The discriminating shape (BUG-R4-1): "each way" is an ADVERBIAL complement, not an
    of-phrase subset — BOTH rows must land (vm9 passes AS WRITTEN, 2 scalars)."""
    rows = _rows(COMMUTE)
    scalars = [r for r in rows if r[3]]
    assert len(scalars) == 2, f"want classic + L4 fire row, got: {rows!r}"
    assert ("commute", "take", "45 minutes", "string") in scalars, rows
    assert ("commute", "duration", "45 minutes each way", "quantity") in scalars, rows


def test_adverbial_complement_keeps_the_classic_twin_shower():
    """Second adverbial shape — "every morning" postfix: same ruling, both rows land."""
    rows = _rows("My shower takes 10 minutes every morning.")
    scalars = [r for r in rows if r[3]]
    assert len(scalars) == 2, rows
    assert ("shower", "take", "10 minutes every morning", "string") in scalars, rows


def test_postfix_have_frame_survives_the_odometer_shape():
    """The issue19 cousin wound: the twin-drop destroyed the have-frame capture the count
    lane reads ('bike|have|13 kilometers' dropped). With the of-phrase bound the classic
    row survives."""
    rows = _rows(BIKE)
    assert any(r[0] == "bike" and r[2] == "13 kilometers" for r in rows), rows


def test_of_phrase_subset_still_drops_its_classic_twin():
    """The ORIGINAL contract shape (issue #19 W1) is untouched: the stripped measure
    fragment and the split SVO twin are still dropped beside the full-value fire row."""
    rows = _rows(RECIPE)
    assert ("bouillabaisse recipe", "quantity", "800 grams of fish", "quantity") in rows
    assert not any(r[1] == "use" for r in rows), rows
    assert not any(r[0] == "fish" for r in rows), rows


def test_flag_off_is_byte_parity_legacy(monkeypatch):
    """OFF/legacy leg: with the arm's own flag OFF there are no fires, hence no twin drop
    and no L4 rows — the pre-lane wound shapes return byte-for-byte (classic rows only)."""
    monkeypatch.setattr(L, "SPINE_POSSESSED_QUANTITY_L4", False)
    rows = _rows(COMMUTE)
    assert ("commute", "take", "45 minutes", "string") in rows, rows
    assert not any(r[0] == "commute" and r[2] == "45 minutes each way" for r in rows), rows
    rows = _rows(RECIPE)
    assert any(r[0] == "recipe" and r[1] == "use" and r[2] == "fish" for r in rows), rows
    assert any(r[0] == "fish" and r[2] == "800 grams" for r in rows), rows
