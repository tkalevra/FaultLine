"""Regression gate for the SPINE deriver's RELOCATION / change-of-residence capture.

Deterministic capture gap in ``derive_sentence_facts`` (src/extraction/linguistics.py), surfaced on
the live spine (SENTENCE_PIPELINE ON): the present-tense "I live in Toronto" captured a residence
edge (the SVO predicate "live_in" folds onto the seeded ``lives_in``), but the two OTHER surface
shapes of the SAME residence fact captured NOTHING:

  • "I moved to Tokyo"        — SVO folded a NOVEL ``move_to`` predicate (no residence semantics) →
                                dropped; only a stray ``tokyo`` entity survived.
  • "I used to live in London" — the past-habitual "used to" makes ``live`` an xcomp of the modal-
                                like ``used`` so the SVO/residence chains never reached it.

Both are the marquee STATE-CHANGE ("moved cities") the temporal model exists for (London→Tokyo →
"where do I live" must return Tokyo current, London past). The new ``_chain_relocation`` recognizes:
  (A) a RELOCATION verb + destination place                     → lives_in @ current, and
  (B) the past-habitual "used to live/move in/to <place>"       → lives_in @ temporal_status='past'.

SUBJECT-AGNOSTIC: the RELOCATION verb vocabulary lives in the ``relocation_verb`` DB cue class
(migration 167), NOT in code; the PERSON-subject + PLACE-destination gates are grammar. lives_in is
a ``state`` rel so the old residence COEXISTS (never superseded); recency/temporal picks current.
All inputs use NON-personal reference data.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)
# The date-extraction layer (dateparser) needs a datetime reference, not a bare date.
_REF_DT = datetime.datetime(2023, 6, 1, tzinfo=datetime.timezone.utc)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _lives_in(facts):
    """(object, temporal_status) for every lives_in edge in the fact set."""
    return [(f.object, f.temporal_status) for f in facts if f.rel_type == "lives_in"]


# ── (A) PRESENT RELOCATION → lives_in (current) ─────────────────────────────────────────────

@pytest.mark.parametrize("text,place", [
    ("I moved to Tokyo.", "tokyo"),
    ("I relocated to Berlin.", "berlin"),
    ("We resettled in Halifax.", "halifax"),
    ("I emigrated to Canada.", "canada"),
])
def test_present_relocation_binds_lives_in(text, place):
    facts = m.derive_sentence_facts(text, _REF)
    li = _lives_in(facts)
    # RESIDENCE-CURRENCY FIX (TEMPORAL_RELOCATION_CURRENT): a present relocation is the CURRENT
    # residence — the chain now emits temporal_status='now' EXPLICITLY (valid-FROM semantics) so a
    # PAST move-date can't demote the residence to 'past' downstream. (Was None → date-driven.)
    assert (place, "now") in li, f"{text!r} → expected current lives_in({place}); got {_triples(facts)}"
    # NEVER the novel ``move_to``/``relocate_to`` junk predicate for the relocation clause.
    assert not any(f.rel_type in ("move_to", "relocate_to", "resettle_in", "emigrate_to")
                   for f in facts), f"{text!r} leaked a novel relocation predicate: {_triples(facts)}"


def test_present_relocation_propn_subject():
    # a named 3rd-person subject resolves to that person (lives_in head_types=Person).
    facts = m.derive_sentence_facts("Sarah relocated to Berlin.", _REF)
    assert ("sarah", "lives_in", "berlin") in _triples(facts), _triples(facts)


def test_present_relocation_preserves_event_date():
    facts = m.derive_sentence_facts("I moved to New York in 2015.", _REF_DT)
    hits = [f for f in facts if f.rel_type == "lives_in" and f.object == "new york"]
    assert hits, _triples(facts)
    assert hits[0].event_date and hits[0].event_date.startswith("2015"), hits[0].event_date
    # RESIDENCE-CURRENCY FIX: a DATED present relocation ("moved to New York in 2015") is the CURRENT
    # residence — event_date is the state's valid-FROM, NOT a valid-at. temporal_status='now' now
    # rides the edge EXPLICITLY so the past move-date can't flip the residence to 'past' at the
    # request-level date-driven derivation (the residence-currency bug). Was None (→ derived 'past').
    assert hits[0].temporal_status == "now"


# ── (B) PAST-HABITUAL "used to …" → lives_in @ temporal_status='past' ────────────────────────

@pytest.mark.parametrize("text,place", [
    ("I used to live in London.", "london"),
    ("I used to reside in Ottawa.", "ottawa"),
    ("I used to move to Tokyo.", "tokyo"),  # relocation verb under "used to"
])
def test_past_habitual_residence_marked_past(text, place):
    facts = m.derive_sentence_facts(text, _REF)
    li = _lives_in(facts)
    assert (place, "past") in li, f"{text!r} → expected PAST lives_in({place}); got {_triples(facts)}"


# ── FALSE-POSITIVE FIREWALL — the PLACE + "used <X> to <V>" gates ────────────────────────────

def test_no_false_relocation_for_transitive_move():
    # "move" with a direct object + a non-place destination is a physical transfer, NOT a residence.
    facts = m.derive_sentence_facts("I moved the box to the shelf.", _REF)
    assert not any(f.rel_type == "lives_in" for f in facts), _triples(facts)


def test_no_false_relocation_for_abstract_destination():
    facts = m.derive_sentence_facts("I moved to the next item.", _REF)
    assert not any(f.rel_type == "lives_in" for f in facts), _triples(facts)


def test_used_x_to_verb_is_not_past_habitual():
    # "used my phone to call Sarah" — ``used`` has a direct object, so it is NOT the periphrastic
    # past-habitual "used to <verb>"; no residence edge, no PAST marking.
    facts = m.derive_sentence_facts("I used my phone to call Sarah.", _REF)
    assert not any(f.rel_type == "lives_in" for f in facts), _triples(facts)


# ── NO REGRESSION to the present-tense residence path ───────────────────────────────────────

def test_present_live_in_still_captures_residence():
    # "I live in Toronto" folds the SVO predicate "live_in" (→ ``lives_in`` downstream via the
    # ontology morphology fold). The relocation chain must not disturb it.
    facts = m.derive_sentence_facts("I live in Toronto.", _REF)
    assert any(f.subject == "user" and f.object == "toronto"
               and m._norm_rel_identity(f.rel_type) in m._residence_predicate_identities()
               for f in facts), _triples(facts)


# ── APPOSITIVE-NAMED THIRD PARTY + PHRASAL-PARTICLE DESTINATION (LME 830ce83f) ────────────────
# Two stacked deriver gaps kept "My friend Rachel actually just moved back to the suburbs again"
# out of the walkable residence lane (it minted a NOVEL, unwalkable ``move_to`` instead of
# ``lives_in``, so a Rachel-anchored recall found nothing):
#   (1) the nsubj is the ROLE noun "friend" with the NAME "Rachel" hung off it as an ``appos`` PROPN
#       — the person-subject gate only accepted a 1st-person pronoun or a bare PROPN nsubj; and
#   (2) "moved BACK to <place>" hangs the directional "to" PP off the adverbial particle "back"
#       (advmod), not the verb, so the destination pobj is a grandchild the direct-child scan missed.
# Both fixes are structural (apposition unification + the SAME ``_verb_particle_prep`` the SVO object
# selection uses) and subject-agnostic. PROPN places here so the parser-only test needs no GLiNER2.

@pytest.mark.parametrize("text,name,place", [
    ("My friend Rachel moved to Tokyo.", "rachel", "tokyo"),
    ("My colleague Sam relocated to Berlin.", "sam", "berlin"),
])
def test_appositive_named_third_party_binds_lives_in(text, name, place):
    facts = m.derive_sentence_facts(text, _REF)
    assert any(f.subject == name and f.rel_type == "lives_in" and f.object == place
               for f in facts), f"{text!r} → expected {name} lives_in {place}; got {_triples(facts)}"


@pytest.mark.parametrize("text,place", [
    ("I moved back to Tokyo.", "tokyo"),
    ("I moved away to Berlin.", "berlin"),
])
def test_phrasal_particle_destination_binds_lives_in(text, place):
    # "moved BACK to X" — the directional PP nests under the advmod particle, not the verb.
    facts = m.derive_sentence_facts(text, _REF)
    # RESIDENCE-CURRENCY FIX: present relocation → temporal_status='now' (was None).
    assert (place, "now") in _lives_in(facts), \
        f"{text!r} → expected current lives_in({place}); got {_triples(facts)}"


def test_appositive_subject_plus_particle_common_noun_place_typed():
    # The exact LME 830ce83f shape: appositive-named subject + "moved BACK to" + a COMMON-NOUN place
    # ("the suburbs") that only GLiNER2 types as a Location. Simulate the production typed Doc by
    # writing the Location span onto the parse (what ``_build_typed_doc`` does with GLiNER2 output),
    # so the place gate admits the common noun. Expect the walkable residence edge on the NAME.
    text = "My friend Rachel actually just moved back to the suburbs again."
    doc = m._parse(text)
    assert doc is not None, "parser unavailable"
    _start = doc.text.lower().find("suburbs")
    _span = doc.char_span(_start, _start + len("suburbs"), label="LOCATION",
                          alignment_mode="contract")
    assert _span is not None
    doc.set_ents([_span])
    facts = m.derive_sentence_facts(doc, _REF)
    assert any(f.subject == "rachel" and f.rel_type == "lives_in" and f.object == "suburbs"
               for f in facts), f"expected rachel lives_in suburbs; got {_triples(facts)}"


def test_no_false_relocation_for_appositive_subject_non_place():
    # Firewall: the appositive-subject acceptance must NOT relax the PLACE gate — a non-place
    # particle destination stays out of the residence lane.
    facts = m.derive_sentence_facts("My friend Rachel moved on to the next topic.", _REF)
    assert not any(f.rel_type == "lives_in" for f in facts), _triples(facts)
