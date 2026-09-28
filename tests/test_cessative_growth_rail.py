"""Deriver-level pins for the CESSATIVE-VERB GROWTH RAIL (miss -> grow, never hand-seed).

WHY THIS FILE EXISTS. A cue class that does not yet admit a lexeme is not a reason to hand-seed
the lexicon — this engine's answer to a miss is to recognise the CONSTRUCTION SHAPE, propose the
observed cue onto the per-tenant growth rail, and capture it on a later exposure once the
frequency gate has been crossed. These pins hold that behaviour AND, more importantly, hold the
SAFETY GATES that stop the rail proposing something destructive.

THE DESTRUCTIVE FAILURE THIS GUARDS. A wrongly-grown `cessative_verb` member does not merely fail
to capture — it makes every FUTURE affirmative use of that verb emit a NEGATED edge, i.e. it
rewrites stored user truth. So the proposal must never fire on a frame that cannot distinguish
the aspect's polarity.

MEASURED ON en_core_web_sm BEFORE THESE PINS WERE WRITTEN (asserting a guessed parse is the trap
this repo warns about; every dependency claim below was probed first):

    "The morlisk stopped  operating on saturdays."   operating: dep_=xcomp, head=stopped
    "The morlisk gave up  practising on fridays."    practising: dep_=dobj,  head=gave, "up" prt
    "The morlisk enjoys   running   on mondays."     running:    dep_=xcomp, head=enjoys, NO prt

The first cut of this lane proposed on the bare "matrix VERB + progressive gerundive complement"
frame and therefore proposed `enjoy` as a cessative — that frame identifies GERUND-TAKING verbs
(enjoy / avoid / consider / remember), not aspectualizers. The particle requirement is what
removes it, and the two DB-driven class exclusions remove the phase verbs whose polarity the
frame cannot decide.

GROUNDING (authoritative, not "it worked on my sentences"):
  * aspectual complementation / aspectualizers — Freed, Alice F. 1979, *The Semantics of English
    Aspectual Complementation*, Reidel (DOI 10.1007/978-94-009-9475-1).
  * cessative aspect — SIL International, *Glossary of Linguistic Terms*, "Cessative Aspect",
    https://glossary.sil.org/term/cessative-aspect (its declared polar opposite, Inchoative
    Aspect, is the sibling `inchoative_verb` cue class this lane excludes).

SUBJECT-AGNOSTIC BY CONSTRUCTION, and the tests must SHOW it rather than assert it: every case
below drives the SAME construction through a DIFFERENT invented subject and a different activity,
so a fix that hardcoded any one lexeme would fail here.
"""

import os

import pytest

import src.extraction.linguistics as ling

_MODEL = os.environ.get("SPACY_MODEL", "en_core_web_sm")


@pytest.fixture(scope="module", autouse=True)
def _require_linguistics():
    # Mirror the gate the PRODUCTION resolver uses (linguistics_available), not a weaker
    # "is spacy importable" proxy — a box with the model absent SKIPS, never FAILS.
    os.environ.setdefault("SPACY_MODEL", _MODEL)
    if not ling.linguistics_available():
        pytest.skip(f"spaCy model {_MODEL!r} not loadable — linguistics layer unavailable")


def _derive(sentence):
    """(triples, growth candidates) for one sentence."""
    growth: list = []
    edges = ling.derive_sentence_facts(sentence, reference="user", growth_out=growth)
    triples = {(e.subject, e.rel_type, e.object, bool(e.negated)) for e in edges}
    return triples, growth


# ───────────────────────── the rail PROPOSES on a recognised frame ─────────────────────────

@pytest.mark.parametrize("sentence, cue", [
    ("The morlisk gave up practising on fridays.", "give up"),
    ("The vellisk gave up rehearsing on tuesdays.", "give up"),
])
def test_unknown_phrasal_cessative_is_proposed_not_hand_seeded(sentence, cue):
    """The matrix is not in the cue class, but the PHRASAL aspectual frame is recognised, so the
    cue rides out on the growth out-parameter for the request side to record."""
    _triples, growth = _derive(sentence)
    assert (cue, "cessative_verb") in growth, growth


def test_the_proposed_cue_is_particle_qualified_never_the_bare_verb():
    """`give` alone is not cessative ("I gave her the book" is a plain ditransitive), so growing
    the BARE lemma would make every future affirmative use of it emit a negated edge. The cue
    surface must carry the particle."""
    _triples, growth = _derive("The morlisk gave up practising on fridays.")
    cues = [c for c, _cat in growth]
    assert "give up" in cues
    assert "give" not in cues


def test_a_contained_construction_captures_nothing_rather_than_guessing():
    """When the class does not admit the matrix, the turn is CONTAINED — no edge is invented on a
    guess. In particular the junk light-verb reading (`give` taking the activity as its object)
    must not be emitted."""
    triples, _growth = _derive("The vellisk gave up rehearsing on tuesdays.")
    assert not any(rel == "give" for (_s, rel, _o, _n) in triples), triples


# ───────────────────────── the rail DECLINES what it cannot decide ─────────────────────────

@pytest.mark.parametrize("sentence", [
    # An ordinary gerund-taking transitive. Same xcomp shape as "stopped operating", NO particle.
    "The morlisk enjoys running on mondays.",
    "The vellisk enjoys swimming on sundays.",
])
def test_a_plain_gerund_taking_verb_is_never_proposed(sentence):
    """The bare gerundive frame identifies gerund-taking verbs, not aspectualizers. This is the
    exact false positive the first cut of the lane shipped; if it returns, the particle gate was
    weakened — RE-MEASURE before changing the assertion."""
    _triples, growth = _derive(sentence)
    assert growth == [], f"must not propose from a plain gerund complement: {growth}"


@pytest.mark.parametrize("sentence", [
    # Phase verbs whose polarity the frame cannot decide — held by the aspectual_control_verb class.
    "The morlisk started operating on sundays.",
    "The vellisk kept operating on sundays.",
])
def test_a_polarity_ambiguous_phase_verb_is_never_proposed(sentence):
    """`aspectual_control_verb` deliberately MIXES ingressive, continuative and terminative phase
    verbs, because the aspect's polarity is lexical and not recoverable from the frame. Proposing
    one of those into the cessative class would eventually negate every future affirmative use of
    it. Declining is the fail-safe direction: an unproposed cue costs one capture, a wrongly-grown
    cessative corrupts stored user truth."""
    _triples, growth = _derive(sentence)
    assert growth == [], f"polarity-ambiguous phase verb must not be proposed: {growth}"


# ───────────────── a KNOWN member still captures, and proposes nothing ─────────────────

@pytest.mark.parametrize("sentence, subject, rel, obj", [
    ("The morlisk stopped operating on saturdays.", "morlisk", "operate_on", "saturdays"),
    ("The quenderal stopped running on wednesdays.", "quenderal", "run_on", "wednesdays"),
])
def test_an_admitted_cessative_captures_negated_and_proposes_nothing(sentence, subject, rel, obj):
    triples, growth = _derive(sentence)
    assert (subject, rel, obj, True) in triples, triples
    assert (subject, rel, obj, False) not in triples
    assert growth == [], growth


def test_the_ceased_edge_is_never_tagged_as_a_scalar_value():
    """THE REGRESSION THIS ROUND EXISTS TO UNDO. A previous round tagged the ceased schedule
    `scalar_datatype="string"` so it would route to `entity_attributes` and supersede in place.
    That table has NO polarity column, so the negation was discarded at the write and recall then
    asserted the cancelled schedule at confidence 1.0. A store that cannot express negation must
    never receive a negatable assertion — the ceased edge stays PLAIN and `/ingest` routes it
    relationally."""
    edges = ling.derive_sentence_facts(
        "The morlisk stopped operating on saturdays.", reference="user")
    ceased = [e for e in edges if bool(e.negated)]
    assert ceased, "the cessation must be captured"
    for e in ceased:
        assert not getattr(e, "scalar_datatype", None), (
            f"a negated edge must never carry a scalar datatype: {e.rel_type}="
            f"{getattr(e, 'scalar_datatype', None)}")
