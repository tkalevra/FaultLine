"""R11 - move-side destination whole-value (critic RG-G-M4 move PARTIAL, Lane C).

The critic's live turn: "We moved from Flether's Lane in Webber's Down to Lorer's Croft
in Plomer's Rise." committed ONLY (user, move_from, <old>) - the destination VANISHED:
  1. the destination goal PP hangs off an ADP-tagged adverbial particle ("Webber's DOWN
     to ..." - 'down' parses ADP/advmod on the verb), so the direct-child prep scan and
     the ADV-only _verb_particle_prep both missed it and the relocation pre-pass filed
     no destination bind at all; and
  2. a digit-less destination carrying a nested locative PP was filed lives_in (the
     digit was the only street marker), welding the whole street phrase in as a
     city-shaped entity the Location type gate silently dropped.

Contract (the pin): plant -> moved-from correction -> WHOLE-VALUE live_at @now present,
scalar_committed>=1, single-current. The deriver half below pins the @now edge the attr
gate consumes (the attr-lane gate trio - tombstone probe, routed provenance, F6 supersede
- is pinned in test_r9/test_r10 and composes with this edge unchanged).

Also pinned: the poss-inclusive place phrase (a possessed noun-headed district -
"Plomer's Rise" - truncated to its bare head 'rise' in three value builders) and the
possessor-never-doubled guard ("cwenburh's field", never "cwenburh's cwenburh's field").
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _edges(sentence):
    out = derive_sentence_facts(sentence, datetime.now()) or []
    rows = []
    for e in out:
        d = e.__dict__ if hasattr(e, "__dict__") else {}
        rows.append((d.get("rel_type"), d.get("object"), d.get("temporal_status")))
    return rows


CRITIC_MOVE = "We moved from Flether's Lane in Webber's Down to Lorer's Croft in Plomer's Rise."


def test_r11_move_destination_files_whole_live_at_now():
    """THE CONTRACT: the moved-to destination lands as whole-value live_at stamped NOW -
    the edge the attr gate consumes to write the whole-value lives_at attr @now."""
    rows = _edges(CRITIC_MOVE)
    dest = [o for r, o, t in rows if r == "live_at" and t == "now"]
    assert dest == ["lorer's croft in plomer's rise"], rows


def test_r11_move_destination_is_never_lost_again():
    """The R10 wound: the move turn committed only move_from - zero destination edges.
    The destination must exist in EVERY reading of the critic sentence."""
    rows = _edges(CRITIC_MOVE)
    assert any(r == "live_at" for r, _, _ in rows), rows


def test_r11_move_destination_city_files_whole():
    rows = _edges(CRITIC_MOVE)
    cities = [o for r, o, t in rows if r == "lives_in" and t == "now"]
    assert cities == ["plomer's rise"], rows  # never the bare head 'rise'


def test_r11_adp_particle_shape_is_the_recovery_marker():
    """Mechanism pin: 'down' parses ADP/advmod hosting the goal prep - the particle-
    hosting shape the direct-child scan misses. Pin it so a parser bump that changes the
    shape re-runs this file's expectations honestly."""
    import spacy
    doc = spacy.load("en_core_web_sm")(CRITIC_MOVE)
    downs = [t for t in doc if t.text == "Down"]
    assert downs and downs[0].dep_ == "advmod", [(t.text, t.pos_, t.dep_) for t in doc]
    toks = [t for t in doc if t.text == "to"]
    assert toks and toks[0].head.i == downs[0].i, (
        toks[0].text, toks[0].head.text)  # spaCy Tokens are proxies: compare .i, never identity


def test_r11_bare_city_destination_still_lives_in():
    """No nested locative PP, no digits -> a city destination stays lives_in unchanged."""
    for s in ("I moved to Berlin.", "We moved to Portland."):
        rows = _edges(s)
        assert any(r == "lives_in" and o in ("berlin", "portland")
                   for r, o, _ in rows), (s, rows)
        assert not any(r == "live_at" for r, _, _ in rows), (s, rows)


def test_r11_digit_destination_shape_byte_identical():
    """The R8 numbered street-move shape is untouched."""
    rows = _edges("I moved from 22 Cordwainer's Stair in Weftmoor "
                  "to 31 Scrimgeour's Lane in Weftmoor.")
    assert any(r == "live_at" and o == "31 scrimgeour's lane in weftmoor" and t == "now"
               for r, o, t in rows), rows
    assert any(r == "lives_in" and o == "weftmoor" and t == "now"
               for r, o, t in rows), rows
    assert any(r == "live_at" and t == "past" for r, _, t in rows), rows


def test_r11_possessed_noun_city_whole_in_address():
    """The plant twin: a digit-less house-name address composes the possessed
    noun-headed district whole ('in plomer's rise', never 'in rise')."""
    rows = _edges("We live at Lorer's Croft in Plomer's Rise.")
    assert any(r == "live_at" and o == "lorer's croft in plomer's rise"
               for r, o, _ in rows), rows


def test_r11_possessor_never_doubled():
    """The poss-inclusive build must never render 'cwenburh's cwenburh's field': a named
    (PROPN) head already carries its possessive, so the prepender's containment check
    compares possessive-stripped words."""
    rows = _edges("We live at 14 Marble Row in Cwenburh's Field.")
    assert any(r == "live_at" and o == "14 marble row in cwenburh's field"
               for r, o, _ in rows), rows
    assert not any(o and "cwenburh's cwenburh's" in o for _, o, _ in rows), rows


def test_r11_non_residence_battery_shapes_unchanged():
    """The poss-inclusive swap covers three shared value builders - the non-residence
    battery stays byte-identical (bare common nouns never gain a possessor)."""
    rows = _edges("I bought a laptop from Amazon.")
    assert any(r == "buy" and o == "laptop from amazon" for r, o, _ in rows), rows
    rows = _edges("I got a new Samsung Galaxy S22 from the Best Buy store.")
    assert any(r == "get_from" and o == "best buy" for r, o, _ in rows), rows
    rows = _edges("I work at Google in Mountain View.")
    assert any(r == "works_for" and o == "google" for r, o, _ in rows), rows
    assert not any(r == "lives_in" for r, _, _ in rows), rows
