"""LOCATIVE_PP_DECOMPOSE — the place inside a folded nominal locative PP becomes its own entity.

THE BUG (reproduced on the deployed image, clean seat): ingesting

    "I am staying at an Airbnb in Chicago."

produced entities ``airbnb`` and ``airbnb in chicago`` and NO ``located_in`` edge at all — the whole
prepositional phrase collapsed into ONE object surface, so the genuine locative (``chicago``, a
Location) never became an entity, nothing anchored on it, and the containment hierarchy had no rung.

ROOT CAUSE: ``_nominal_pp_complement`` COMPOSES an object head noun's PP-complement into the object
phrase (shape A) so the value rides the clause's own walk-reachable relation. Correct for a VALUE
complement ("a degree in business administration"); for a LOCATIVE it silently discards a real,
separately-anchorable fact. The decomposition was already in the parse and simply thrown away.

THE MECHANISM: a locative NOMINAL MODIFIER — Universal Dependencies ``nmod`` bearing a ``case`` child
(de Marneffe, Manning, Nivre & Zeman, "Universal Dependencies", Computational Linguistics 47(2),
2021; UD v2 guidelines) — surfaced by the pinned ``en_core_web_sm`` in spaCy's ClearNLP/OntoNotes
scheme as ``prep`` → ``pobj`` (Choi & Palmer, ClearNLP dependency labels, 2012). The parser's NOUN
attachment is taken as given and never re-attached (PP-attachment: Hindle & Rooth, "Structural
Ambiguity and Lexical Relations", CL 19(1), 1993).

THE FIX IS ADDITIVE: the composed object phrase is UNCHANGED (the user's own wording is the memory);
alongside it the deriver emits ``(<composed phrase>, located_in, <place>)``. ``located_in`` is the
EXISTING seeded containment hierarchy rel — nothing is minted — and the VENUE is its SUBJECT, never
its tail, so the range narrowed by migration 194 (``tail_types={Location}``) is satisfied and a brand
is never filed AS a place.

These tests pin: (1) the place lands as its own entity via ``located_in``; (2) the composed phrase
survives verbatim; (3) the flag OFF is byte-identical; (4) the non-locative value fold ("a degree in
Business Administration"), the ablative ``from``, the allative ``to`` and an UNTYPED Doc are all
untouched — the chain fires ONLY on a PLACE-TYPED pobj under a containment adposition.

Run: SPACY_MODEL=en_core_web_sm python3 tools/fltest.py --bug LOC \
         --test tests/test_spine_locative_pp_decompose.py
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


def _typed(m, sentence, place_surface):
    """Parse ``sentence`` and seed ``place_surface`` as a LOCATION ent — the SAME contract
    ``main._build_typed_doc`` establishes when it writes GLiNER2's spans onto the Doc via
    ``set_ents`` (uppercase label, token-aligned ``char_span``). No GLiNER2 needed in the test env."""
    from spacy.tokens import Span
    doc = m._parse(sentence)
    assert doc is not None
    start = sentence.find(place_surface)
    assert start >= 0, sentence
    span = doc.char_span(start, start + len(place_surface), label="LOCATION",
                         alignment_mode="contract")
    assert span is not None, (sentence, place_surface)
    doc.set_ents([Span(doc, span.start, span.end, label="LOCATION")])
    return doc


def _facts(m, doc):
    return {(f.subject, f.rel_type, f.object) for f in m.derive_sentence_facts(doc, REF)}


# ── THE REPRODUCER ────────────────────────────────────────────────────────────────────────────────

@requires_model
def test_airbnb_in_chicago_place_becomes_its_own_entity():
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, "I am staying at an Airbnb in Chicago.", "Chicago"))
    # (1) the PLACE lands as its own entity on the seeded containment hierarchy rel.
    assert ("airbnb in chicago", "located_in", "chicago") in facts, facts
    # (2) the user's own phrasing survives verbatim on the clause's main relation — ADDITIVE, the
    #     composed surface is NOT destroyed (walk: user →stay_at→ "airbnb in chicago" →located_in→ chicago).
    assert any(s == "user" and o == "airbnb in chicago" for (s, _r, o) in facts), facts
    # (3) the VENUE is never the TAIL of located_in — a brand is never filed AS a place
    #     (migration 194 narrowed located_in.tail_types to {Location}).
    assert not any(r == "located_in" and o.startswith("airbnb") for (_s, r, o) in facts), facts


@requires_model
def test_flag_off_is_byte_identical():
    on = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    on_facts = _facts(on, _typed(on, "I am staying at an Airbnb in Chicago.", "Chicago"))
    off = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="false")
    off_facts = _facts(off, _typed(off, "I am staying at an Airbnb in Chicago.", "Chicago"))
    # OFF loses exactly the additive containment edge and NOTHING else.
    assert off_facts == on_facts - {("airbnb in chicago", "located_in", "chicago")}, (
        off_facts, on_facts)
    _reload(LOCATIVE_PP_DECOMPOSE="true")


# ── REAL CORPUS UTTERANCES (LongMemEval oracle haystack, user turns) ──────────────────────────────

@requires_model
@pytest.mark.parametrize("sentence,place,host", [
    ("I stayed in a hostel in Tokyo that cost around $30 per night.", "Tokyo", "hostel in tokyo"),
    ("I'm staying at a luxurious resort in Maui.", "Maui", "luxurious resort in maui"),
    ("I am planning a 10-day trek in New Zealand.", "New Zealand", "10-day trek in new zealand"),
])
def test_corpus_locatives_reach_the_place(sentence, place, host):
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, sentence, place))
    assert (host, "located_in", place.lower()) in facts, facts


@requires_model
def test_possessive_host_also_decomposes():
    # The fold is wired to the possessive head too — "my new studio apartment IN HARAJUKU".
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, "I have been enjoying my new studio apartment in Harajuku.",
                             "Harajuku"))
    assert ("new studio apartment in harajuku", "located_in", "harajuku") in facts, facts


# ── THE FIREWALLS (already-correct placements that must NOT change) ───────────────────────────────

@requires_model
def test_non_place_value_complement_untouched():
    """"a degree IN Business Administration" is a VALUE complement, not a locative. The pobj is not
    place-typed → the chain NO-OPs; the value fold stands exactly as before."""
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    doc = m._parse("I graduated with a degree in Business Administration.")
    facts = _facts(m, doc)
    assert ("user", "graduate_with", "degree in business administration") in facts, facts
    assert not any(r == "located_in" for (_s, r, _o) in facts), facts


@requires_model
def test_untyped_doc_noops_honestly():
    """An UNTYPED Doc (the raw-str deriver path, whose pipeline is loaded ``disable=["ner"]``) carries
    no place type. The chain must NO-OP rather than guess a place from the surface."""
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, m._parse("I am staying at an Airbnb in Chicago."))
    assert not any(r == "located_in" for (_s, r, _o) in facts), facts


@requires_model
def test_ablative_from_is_not_a_containment():
    """"a baby girl FROM China" is an ORIGIN, not a location. ``from`` is outside the containment
    adposition primitive → no containment edge."""
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, "My cousin Alex just adopted a baby girl from China.", "China"))
    assert not any(r == "located_in" and o == "china" for (_s, r, o) in facts), facts


@requires_model
def test_allative_goal_to_is_not_a_containment():
    """"a trip TO Germany" is a GOAL. ``to``/``into``/``onto`` are outside the containment primitive
    (they have their own goal seams) → no containment edge."""
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, "I am planning a trip to Germany.", "Germany"))
    assert not any(r == "located_in" and o == "germany" for (_s, r, o) in facts), facts


@requires_model
def test_sibling_geo_chains_unchanged():
    """The classification-containment and residence lanes own their own constructions and must be
    byte-identical — this chain fires only on a FOLDED nominal PP."""
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    facts = _facts(m, _typed(m, "Hamilton is a city in Ontario.", "Ontario"))
    assert ("hamilton", "instance_of", "city") in facts, facts
    assert ("hamilton", "located_in", "ontario") in facts, facts
    # no spurious (city, located_in, ontario) — a TYPE is never filed at a place.
    assert not any(s == "city" and r == "located_in" for (s, r, _o) in facts), facts

    facts = _facts(m, _typed(m, "I live in Toronto.", "Toronto"))
    assert any(s == "user" and o == "toronto" for (s, _r, o) in facts), facts
    assert not any(r == "located_in" for (_s, r, _o) in facts), facts


# ── THE HELPER, DIRECTLY (no clause needed) ───────────────────────────────────────────────────────

@requires_model
def test_place_probe_requires_containment_prep_and_place_type():
    m = _reload(LINGUISTIC_LAYER="true", LOCATIVE_PP_DECOMPOSE="true")
    doc = _typed(m, "I am staying at an Airbnb in Chicago.", "Chicago")
    head = next(t for t in doc if t.text == "Airbnb")
    # containment adposition + place-typed pobj → the place token
    assert m._nominal_locative_place(head, ("in", "at", "on", "within", "inside")) is not None
    # the SAME structure under a non-containment adposition set → None (never guesses)
    assert m._nominal_locative_place(head, ("from",)) is None
    # an empty adposition set → None (fail-safe, never fires blind)
    assert m._nominal_locative_place(head, ()) is None
