"""Fog-family R6 (lane C, critic R4-8): an APOSTROPHE-CITY address must stay whole -
the possessive city name is a PLACE NAME, not a possessive binding.

THE WOUND (measured live, bench seat, cooked image 4ddbff092225):
  "We live at 2 Nettlefold Row in Pilgrim's Reach." produced
    (a) junk twins: (2 nettlefold row in pilgrim's reach, instance_of, row),
        (pilgrim's reach, related_to, pilgrim) and a harvest-shredded "reach"
        city entity (the span pass kept only the possessive head);
    (b) phrase-as-residence: the ingest city edge for lives_in carried the WHOLE
        "pilgrim's reach", whose convergence probe bound it to the harvest-
        minted PHRASE entity via modifier_dropped (new tokens are a suffix of the
        phrase across " in ") - lives_in then resolved to the phrase entity and the
        walk voiced the whole address as the residence;
    (c) phantom committed counts: TRACED HONEST - the count reports rows
        written-or-bumped, and a repeated turn BUMPS the live junk rows
        (related_to confirmed_count=2 after two identical turns), so a turn with
        zero NET-new rows still reports the bumped writes; not the false-success
        class (the rows exist and increment).

THREE fixes, all grammar-scoped declines on the LOCATIVE-PP-OBJECT surface:
  1. registry symmetric PP-tail arm (in test_r4_residence_convergence_no_city_weld:
     a city that is a suffix of an address across a locative prep never binds -
     whichever side is shorter).
  2. the countable-instance typing chokepoint declines an object governed by a
     locative containment prep (an address is never instance_of its street head).
  3. the possessive loose link declines a possessed head governed by a locative
     containment prep (a place name never genitive-links to its first token).
"""
import pytest

from src.extraction.linguistics import (derive_sentence_facts, _get_nlp,
                                        _locative_pp_object)


def _edges(sentence):
    return [e if isinstance(e, dict) else dict(getattr(e, "__dict__", {}))
            for e in (derive_sentence_facts(sentence, None) or [])]


def _rels(sentence):
    return [(e.get("rel_type"), e.get("object")) for e in _edges(sentence)
            if e.get("rel_type") not in ("instance_of", "related_to", "located_in")]


# -- the wound family: only the honest full-address edge survives -----------------
def test_possessive_street_in_possessive_city_mints_no_twins():
    s = "We live at 2 Nettlefold Row in Pilgrim's Reach."
    twins = [e for e in _edges(s) if e.get("rel_type") in
             ("instance_of", "related_to", "located_in")]
    assert twins == []


def test_possessive_street_in_possessive_city_keeps_full_value():
    s = "We live at 2 Nettlefold Row in Pilgrim's Reach."
    assert [(r, o) for r, o in _rels(s)] == [("live_at",
        "2 nettlefold row in pilgrim's reach")]


def test_plain_street_in_possessive_city_mints_no_twins():
    s = "We live at 44 Mill Cross in Pilgrim's Reach."
    twins = [e for e in _edges(s) if e.get("rel_type") in
             ("instance_of", "related_to", "located_in")]
    assert twins == []
    assert _rels(s) == [("live_at", "44 mill cross in pilgrim's reach")]


def test_possessive_street_in_plain_city_mints_no_twins():
    s = "We live at 7 Cooper's Lane in Cindermill."
    twins = [e for e in _edges(s) if e.get("rel_type") in
             ("instance_of", "related_to", "located_in")]
    assert twins == []
    assert _rels(s) == [("live_at", "7 cooper's lane in cindermill")]


# NOTE (residual, out of the wound's grammar): a BARE-subject possessive city
# ("Pilgrim's Reach is lovely") still emits the loose link today - there is no
# locative prep to key the decline on, and inventing one would need an entity
# list or NER. The R4-8 wound is the ADDRESS shape, where the grammar signal
# exists; the bare-subject reading keeps today's behavior byte-for-byte.


# -- _locative_pp_object: the grammar discriminator -------------------------------
def _row_tok(sentence, text):
    nlp = _get_nlp()
    doc = nlp(sentence)
    for t in doc:
        if (t.text or "").strip().lower() == text.strip().lower():
            return t
    return None


def test_address_head_in_locative_pp_is_locative_object():
    tok = _row_tok("We live at 2 Nettlefold Row in Pilgrim's Reach.", "Row")
    assert tok is not None and _locative_pp_object(tok) is True


def test_city_head_in_locative_pp_is_locative_object():
    tok = _row_tok("We live at 2 Nettlefold Row in Pilgrim's Reach.", "Reach")
    assert tok is not None and _locative_pp_object(tok) is True


def test_dobj_noun_is_not_locative_object():
    tok = _row_tok("I finished the report yesterday.", "report")
    assert tok is not None and _locative_pp_object(tok) is False


def test_nonlocative_prep_is_not_locative_object():
    tok = _row_tok("I argued with the landlord.", "landlord")
    assert tok is not None and _locative_pp_object(tok) is False


# -- regression: the typing and loose-link arms keep their non-PP behaviour -------
def test_countable_instance_typing_survives_outside_locative_pp():
    edges = _edges("I bought a navy blue blazer last week.")
    typed = [e for e in edges if e.get("rel_type") == "instance_of"]
    assert any(e.get("object") == "blazer" for e in typed)


def test_possessive_loose_link_survives_outside_locative_pp():
    edges = _edges("I visited Tinker's Forge.")
    loose = [e for e in edges if e.get("rel_type") == "related_to"]
    assert any(e.get("object") == "tinker" for e in loose)
