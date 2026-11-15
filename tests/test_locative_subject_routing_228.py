"""#228 — SUBJECT-TYPE-ROUTED LOCATIVE REL at the spine's locative frame (ROUTE, never widen).

THE WOUND (measured on prod + reproduced through the MCP front door pre-fix, 2026-10-08):
"My old dobsonian telescope lives in the garage." → the spine's locative verb frame keyed the
relation on the verb — ``(old dobsonian telescope, lives_in, garage)`` — and the WGM gate
``type_refused`` it (``lives_in.head_types={Person, Animal}`` vs an Object subject) with NOTHING
filed. Natural dialogue says "the X lives in the Y" about tools, instruments, servers, trailers.

THE GROUNDING (Wikidata property scopes, fetched 2026-10-08):
  • P551 "residence" — "the place where the person is or has been, resident";
    ``instance of`` *Wikidata property for items about people*; subject-type constraint classes
    human / character / domesticated animal (…animal) — mirrored by this repo's
    ``lives_in.head_types={Person, Animal}``.
  • P276 "location" — "location of the object, structure or event; use P131 to indicate the
    containing administrative entity" (canonical example: Mona Lisa → Louvre Museum). P551 is a
    ``subproperty of`` P276: RESIDENCE is the person-scoped specialization of the generic
    locative relation. The repo's ``located_in`` carries P131 semantics with
    ``head_types={ANY}``, ``tail_types={Location}``.
    https://www.wikidata.org/wiki/Property:P551 · https://www.wikidata.org/wiki/Property:P276
    Also DEV/DESIGN-locative-subject-routing-228.md.

THE FIX: ``_locative_route_rel`` at the single ``_emit`` chokepoint — when the emitted rel is a
MUTABLE location-category rel whose declared ``head_types`` do not admit the subject's GLiNER2
type, and the object is GLiNER2-typed LOCATION, select the fitting locative rel FROM THE REL_TYPE
OVERLAY (category='location', mutable, head admits the subject's type, tail admits LOCATION;
ranked by metadata specificity) — NEVER a hardcoded lives_in→located_in pair. No fitting rel →
the rel stands and the gate's loud ``type_refused`` is unchanged (fail loud). ``born_in``
(immutable) never routes.

These pins run on FRESH subjects (a banjo, a metal lathe, a camper trailer — an instrument, a
machine, a vehicle; NOT the bench's telescope/networking/family), on a stand-in overlay that
carries the REAL seeded shapes, and on a Doc typed the way ``main._build_typed_doc`` types it.

Run: SPACY_MODEL=en_core_web_sm python3 tools/fltest.py --bug 228-locative-routing \
         --test tests/test_locative_subject_routing_228.py
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    try:
        import spacy  # noqa: F401
        spacy.load(os.environ.get("SPACY_MODEL", "en_core_web_sm"), disable=["ner"])
        return True
    except Exception:
        return False


requires_model = pytest.mark.skipif(
    not _model_available(), reason="en_core_web_sm not installed in test env")

REF = datetime.datetime(2023, 6, 1)

# The REAL seeded shapes (public template, measured 2026-10-08) — lives_in was widened to
# {Person, Animal} (mirroring Wikidata P551's own subject-type constraint list), located_in is
# the P131-semantics containment rel with an unconstrained head, located_at is SCALAR-tailed
# (an address string — never a candidate for a LOCATION object), born_in is IMMUTABLE.
SEED_META = {
    "lives_in":   {"category": "location", "head_types": ["Person", "Animal"],
                   "tail_types": ["Location"], "correction_behavior": "supersede"},
    "lives_at":   {"category": "location", "head_types": ["Person"],
                   "tail_types": ["Location", "SCALAR"], "correction_behavior": "supersede"},
    "located_in": {"category": "location", "head_types": ["ANY"],
                   "tail_types": ["Location"], "correction_behavior": "supersede"},
    "located_at": {"category": "location", "head_types": ["ANY"],
                   "tail_types": ["SCALAR"], "correction_behavior": "supersede"},
    "born_in":    {"category": "location", "head_types": ["Person"],
                   "tail_types": ["Location"], "correction_behavior": "immutable"},
}


def _stub(m, meta=None):
    m.__dict__["_rel_overlay_meta_map"] = lambda: dict(meta if meta is not None else SEED_META)


def _typed(m, sentence, spans):
    """Parse ``sentence`` and seed ``spans`` = [(surface, LABEL)] as doc ents — the SAME contract
    ``main._build_typed_doc`` establishes when it writes GLiNER2's spans onto the Doc via
    ``set_ents`` (uppercase label, token-aligned ``char_span``). No GLiNER2 needed here."""
    from spacy.tokens import Span
    doc = m._parse(sentence)
    assert doc is not None, sentence
    ents = []
    for surface, label in spans:
        start = sentence.find(surface)
        assert start >= 0, (sentence, surface)
        span = doc.char_span(start, start + len(surface), label=label.upper(),
                             alignment_mode="contract")
        assert span is not None, (sentence, surface, label)
        ents.append(Span(doc, span.start, span.end, label=label.upper()))
    doc.set_ents(ents)
    return doc


def _facts(m, doc):
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(doc, REF)}


# ── THE WOUND, FIXED: fresh non-Person subjects route to the overlay-resolved locative rel ───────

@requires_model
@pytest.mark.parametrize("sentence,subj,place", [
    ("My banjo lives in the hallway closet.", "banjo", "hallway closet"),          # instrument
    ("My metal lathe lives in the workshop.", "metal lathe", "workshop"),              # machine
    ("My camper trailer lives in the driveway.", "camper trailer", "driveway"),    # vehicle
])
def test_object_subject_routes_to_located_in(sentence, subj, place):
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    facts = _facts(m, _typed(m, sentence, [(subj, "OBJECT"), (place, "LOCATION")]))
    assert (subj, "located_in", place) in facts, facts
    assert not any(r in ("lives_in", "live_in") and s == subj for (s, r, _o) in facts), facts


@requires_model
def test_raw_svo_predicate_identity_folds_to_the_seeded_row():
    # The SVO verb frame emits the MORPHOLOGY surface ("live_in"), the overlay key is "lives_in" —
    # the identity fold must bridge them (this is the shape the live front door emitted).
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    doc = _typed(m, "My metal lathe lives in the workshop.",
                 [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    saw = next(t for t in doc if t.text.lower() == "lathe")
    shop = next(t for t in doc if t.text.lower() == "workshop")
    assert m._locative_route_rel("live_in", saw, shop) == "located_in"


# ── PERSON (AND THE SEEDED ANIMAL) KEEP lives_in — the P551 scope is untouched ────────────────────

@requires_model
def test_person_subject_keeps_lives_in():
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    facts = _facts(m, _typed(m, "Sarah lives in Bondi.",
                             [("Sarah", "PERSON"), ("Bondi", "LOCATION")]))
    assert ("sarah", "live_in", "bondi") in facts, facts
    assert not any(s == "sarah" and r == "located_in" for (s, r, _o) in facts), facts


@requires_model
def test_animal_subject_keeps_lives_in():
    # lives_in.head_types={Person, Animal} ADMITS an Animal subject — nothing to route (the seed
    # mirrors Wikidata P551's own subject-type constraint list: domesticated animal).
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    facts = _facts(m, _typed(m, "My pony lives in the paddock.",
                             [("pony", "ANIMAL"), ("paddock", "LOCATION")]))
    assert ("pony", "live_in", "paddock") in facts, facts


@requires_model
def test_first_person_untyped_subject_unchanged():
    # "I live in …" — a first-person pronoun carries no GLiNER2 type; the routing stands down
    # (fail-safe) and the user's residence keeps today's person path.
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    facts = _facts(m, _typed(m, "I live in Kitchener.", [("Kitchener", "LOCATION")]))
    assert ("user", "live_in", "kitchener") in facts, facts


# ── NO FITTING REL → THE LOUD REFUSAL STANDS (fail loud, never guess, never drop) ────────────────

@requires_model
def test_no_fitting_rel_keeps_the_mismatched_rel_for_the_gate():
    # Overlay WITHOUT any admitting locative candidate (an operator narrowed located_in's head to
    # Person, or it is absent): the deriver must NOT invent, drop, or guess — the mismatched
    # lives_in flows on exactly as today so the WGM gate refuses it loudly.
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m, {k: (dict(v, head_types=["Person"]) if k == "located_in" else dict(v))
              for k, v in SEED_META.items()})
    facts = _facts(m, _typed(m, "My metal lathe lives in the workshop.",
                             [("metal lathe", "OBJECT"), ("workshop", "LOCATION")]))
    assert ("metal lathe", "live_in", "workshop") in facts, facts
    assert not any(r == "located_in" for (_s, r, _o) in facts), facts


@requires_model
def test_immutable_source_rel_never_routes():
    # born_in (category 'location', immutable) — a birthplace is not a "where it lives" state; the
    # routing must not touch it even on a mismatched subject type.
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    doc = _typed(m, "My metal lathe lives in the workshop.",
                 [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    saw = next(t for t in doc if t.text.lower() == "lathe")
    shop = next(t for t in doc if t.text.lower() == "workshop")
    assert m._locative_route_rel("born_in", saw, shop) == "born_in"


@requires_model
def test_non_location_object_never_routes():
    # The object is not GLiNER2-typed a place → no locative routing decision at all.
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m)
    doc = _typed(m, "My metal lathe lives in the workshop.",
                 [("metal lathe", "OBJECT"), ("workshop", "ORGANIZATION")])
    saw = next(t for t in doc if t.text.lower() == "lathe")
    shop = next(t for t in doc if t.text.lower() == "workshop")
    assert m._locative_route_rel("lives_in", saw, shop) == "lives_in"


# ── METADATA-SPECIFICITY: the candidate set is the tenant's OWN ontology ──────────────────────────

@requires_model
def test_head_specific_candidate_beats_the_any_catch_all():
    # A tenant-grown locative rel whose head ADMITS the subject's concrete type outranks the
    # unconstrained {ANY} seed — selection-by-metadata-specificity, no code change needed for a
    # grown rel to win.
    meta = dict(SEED_META)
    meta["stored_in"] = {"category": "location", "head_types": ["Object"],
                         "tail_types": ["Location"], "correction_behavior": "supersede"}
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m, meta)
    doc = _typed(m, "My metal lathe lives in the workshop.",
                 [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    saw = next(t for t in doc if t.text.lower() == "lathe")
    shop = next(t for t in doc if t.text.lower() == "workshop")
    assert m._locative_route_rel("lives_in", saw, shop) == "stored_in"


@requires_model
def test_scalar_tailed_locative_rel_is_not_a_candidate():
    # located_at (tail {SCALAR} — an address string) must never receive a LOCATION object.
    meta = {k: v for k, v in SEED_META.items() if k != "located_in"}
    m = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(m, meta)
    doc = _typed(m, "My metal lathe lives in the workshop.",
                 [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    saw = next(t for t in doc if t.text.lower() == "lathe")
    shop = next(t for t in doc if t.text.lower() == "workshop")
    assert m._locative_route_rel("lives_in", saw, shop) == "lives_in"


# ── FLAG OFF = BYTE-IDENTICAL (the rollback lever) ───────────────────────────────────────────────

@requires_model
def test_flag_off_is_byte_identical():
    on = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="true")
    _stub(on)
    doc_on = _typed(on, "My metal lathe lives in the workshop.",
                    [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    on_facts = _facts(on, doc_on)
    off = _reload(LINGUISTIC_LAYER="true", SPINE_LOCATIVE_SUBJECT_ROUTING="false")
    _stub(off)
    doc_off = _typed(off, "My metal lathe lives in the workshop.",
                     [("metal lathe", "OBJECT"), ("workshop", "LOCATION")])
    off_facts = _facts(off, doc_off)
    # OFF loses exactly the routed edge and gains today's (wound's) emission — byte-identical
    # to the pre-fix behavior for this frame: the ONLY delta between ON and OFF is the routed rel.
    assert ("metal lathe", "live_in", "workshop") in off_facts, off_facts
    assert ("metal lathe", "located_in", "workshop") not in off_facts, off_facts
    assert on_facts == {("metal lathe", "located_in", "workshop"),
                        ("user", "owns", "metal lathe")}, on_facts
    assert off_facts == {("metal lathe", "live_in", "workshop"),
                         ("user", "owns", "metal lathe")}, off_facts
    _reload(SPINE_LOCATIVE_SUBJECT_ROUTING="true")
