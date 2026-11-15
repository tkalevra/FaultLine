"""Fog-family R3 (lane C, R2-C-1): the lives_at/attr-scalar VALUE phrase must keep the
FULL possessive street phrase.

TWO seams, both fired on "112 Tinker's Court"-shaped values and both dropping the
proper-name genitive ("Tinker" parses ``poss`` of the type head "Court"):
  1. ``_object_value_phrase`` (the SVO predicate-lift / value-slot phrase builder) walked
     only compound/amod/nummod/quantmod — the PROPN ``poss`` sub-name (and the street
     number hanging under it) fell out: lives_at="court".
  2. ``_attr_scalar_binding``'s VERBATIM value-span capture declined ANY span carrying a
     ``poss`` (the issue-#19 W2 coordination-glue firewall): "My address is 112 Tinker's
     Court" dropped the address entirely.
Both fixes are scoped by GRAMMAR, not word lists: a proper-name genitive (PROPN poss) on
a named value slot with no coordination in the span is the naming construction itself;
first-person/common-noun possessors and coordination-glued spans keep today's behavior
byte-identically (the possessive-owner chains never see a dependency change — only the
phrase STRING widens).
"""
import pytest

from src.extraction.linguistics import derive_sentence_facts, _get_nlp, _object_value_phrase


def _edges(sentence):
    return [e if isinstance(e, dict) else dict(getattr(e, "__dict__", {}))
            for e in (derive_sentence_facts(sentence, None) or [])]


def _objs(sentence, rel):
    return [e.get("object") for e in _edges(sentence) if e.get("rel_type") == rel]


# ── seam 1: the SVO predicate-lift value phrase (the critic's live shape) ────────────
def test_svo_possessive_street_keeps_the_full_phrase():
    objs = _objs("We live at 112 Tinker's Court in Rainhollow.", "live_at")
    assert objs == ["112 tinker's court in rainhollow"]


def test_svo_plain_street_is_unchanged():
    assert _objs("I live at 42 Kestrel Lane.", "live_at") == ["42 kestrel lane"]


def test_svo_same_city_move_keeps_full_new_phrase():
    assert _objs("We live at 9 Kessel Street in Greywater.", "live_at") == ["9 kessel street"]


def test_common_noun_possessor_stays_excluded():
    """A common-noun possessor is an OWNER binding, not part of the name — unchanged."""
    assert _objs("We live at my sister's house.", "live_at") == ["house"]


# ── seam 2: the copula attr-scalar verbatim value span ──────────────────────────────
def test_copula_possessive_address_scalar_keeps_the_full_value():
    e = [x for x in _edges("My address is 112 Tinker's Court in Rainhollow.")
         if x.get("scalar_datatype") == "string"]
    assert e and e[0]["subject"] == "user"
    assert e[0]["object"] == "112 tinker's court in rainhollow"


def test_copula_possessive_address_without_city_keeps_the_street():
    e = [x for x in _edges("My address is 112 Tinker's Court.")
         if x.get("scalar_datatype") == "string"]
    assert e and e[0]["object"] == "112 tinker's court"


def test_copula_plain_address_is_unchanged():
    e = [x for x in _edges("My address is 12 Alder Lane in Rainhollow.")
         if x.get("scalar_datatype") == "string"]
    assert e and e[0]["object"] == "12 alder lane in rainhollow"


def test_w2_coordination_glue_still_declines():
    """The issue-#19 W2 wound keeps its firewall: a determinative genitive inside a
    coordination-glued span never becomes a scalar value."""
    scalars = [x for x in _edges(
        "My router is a Unifi Dream Machine and its IP is 192.168.1.1.")
        if x.get("scalar_datatype") == "string"]
    assert scalars == []


def test_propn_genitive_with_coordination_still_declines():
    """A PROPN genitive is admitted ONLY without coordination in the span: the W2
    mis-segmentation with a glued second span still declines."""
    scalars = [x for x in _edges("My address is 112 Tinker's Court and Rainhollow is the city.")
        if x.get("scalar_datatype") == "string"]
    assert scalars == []


# ── unit: the phrase builder itself, on both parses ─────────────────────────────────
def test_object_value_phrase_unit_possessive_vs_plain():
    nlp = _get_nlp()
    doc = nlp("We live at 112 Tinker's Court in Rainhollow.")
    court = [t for t in doc if t.text == "Court"][0]
    assert _object_value_phrase(court) == "112 tinker's court"
    doc2 = nlp("I live at 42 Kestrel Lane.")
    lane = [t for t in doc2 if t.text == "Lane"][0]
    assert _object_value_phrase(lane) == "42 kestrel lane"


def test_object_value_phrase_count_head_stays_a_count():
    """A bare common noun with a digit nummod is a COUNT, never a named value."""
    nlp = _get_nlp()
    doc = nlp("I have 3 cats.")
    cats = [t for t in doc if t.text == "cats"][0]
    assert _object_value_phrase(cats) == "cats"
