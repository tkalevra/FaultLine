"""BUG-11 — a verbless SVO mis-parse nominalized the FINITE VERB into the subject entity.

"The zephyrite lattice stores exactly seven calibration tones." parses ``stores`` NOUN/ROOT
(head = itself), the true subject NP (``zephyrite lattice``) as a flat ``compound`` chain left of
it and the true object NP (``tones``, carrying ``nummod`` 'seven') as its ``appos`` child — so
``derive_sentence_facts`` emitted ``stores | tone | seven``: the PREDICATE became the subject
entity and the sentence subject was never minted. ``_repair_nominalized_svo_root`` re-reads
POS/dep on that exact verbless-SVO shape (structural gates only — one self-headed NOUN ROOT with
a 3sg-verb surface, a left ``compound`` chain, exactly one ``appos`` child, NO VERB/AUX in the
sentence) before the chains run. These pins hold the gates SHUT on the non-firing shapes (a
verbless nominal fragment, a plural-noun root, a healthy SVO parse) and OPEN on the confirmed
one, including the Doc-input path the harvest actually wires (a GLiNER2-typed ``Doc``, whose
``ent_type_`` spans the repair must never touch).

Run: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/extraction/test_bug11_stores_verb_recovery.py -q
"""
import pytest

import src.extraction.linguistics as ling


# Skip the model-backed tests if en_core_web_sm is not installed (sandbox without the bake);
# the kill-switch defaults ON and tests/conftest.py setdefaults SPACY_MODEL.
requires_model = pytest.mark.skipif(
    not ling.linguistics_available(), reason="en_core_web_sm not installed in test env"
)

REPRO = "The zephyrite lattice stores exactly seven calibration tones."
CONTROL = "The gold ring holds three small stones."


def _doc(text):
    """Parse through the MODULE pipeline (``_get_nlp``) — the exact parse the deriver sees."""
    nlp = ling._get_nlp()
    assert nlp is not None
    return nlp(text)


# ─────────────────────────── the confirmed mis-parse repairs ───────────────────────────

@requires_model
def test_repro_subject_is_minted_and_verb_is_never_the_subject():
    facts = ling.derive_sentence_facts(REPRO, reference=None)
    assert facts, "the repaired SVO must still derive facts"
    subjects = [f.subject for f in facts]
    # THE bug: the verb lexeme stood in for the subject.
    assert "stores" not in subjects
    # The sentence subject is minted (the SVO chain emits the full compound NP surface).
    assert any(s in ("lattice", "zephyrite lattice") for s in subjects), subjects
    # The object NP head survives with its modifier attached (no mangled bare head).
    assert any("tone" in (f.object or "") or "tone" in (f.rel_type or "") for f in facts), facts


@requires_model
def test_control_sentence_still_derives_its_real_subject():
    """'The gold ring holds three small stones.' parses HEALTHY (VERB ROOT) — the chain's subject
    surface there (bare head 'ring') is what a correct parse yields; the repair must never
    disturb it, and the verb must never become the subject."""
    facts = ling.derive_sentence_facts(CONTROL, reference=None)
    assert facts
    subjects = [f.subject for f in facts]
    assert subjects == ["ring"], subjects
    assert "holds" not in subjects


@requires_model
def test_repair_also_fires_on_the_typed_doc_path_the_harvest_uses():
    """The harvest passes an already-parsed Doc, not a str — the entry repair must run BEFORE the
    chains on that path too (same derivation, same subject)."""
    facts = ling.derive_sentence_facts(_doc(REPRO), reference=None)
    assert facts
    subjects = [f.subject for f in facts]
    assert "stores" not in subjects
    assert any(s in ("lattice", "zephyrite lattice") for s in subjects), subjects


# ─────────────────────────── the gates stay SHUT ───────────────────────────

@requires_model
def test_verbless_nominal_fragment_does_not_fire():
    """'No pain, no gain.' is a genuine verbless nominal fragment: its root carries an ``appos``
    child (so the object gate alone cannot discriminate) but NO left ``compound`` chain. The
    repair must not fire and the pre-existing nominal fact must be unchanged — no spurious
    verb-fact minted from the noun pair."""
    doc = _doc("No pain, no gain.")
    assert ling._repair_nominalized_svo_root(doc) == 0
    facts = ling.derive_sentence_facts("No pain, no gain.", reference=None)
    assert len(facts) == 1, facts
    assert (facts[0].subject, facts[0].rel_type, facts[0].object) == ("pain", "has_role", "gain")


@requires_model
def test_plural_stores_noun_does_not_fire():
    """'The stores on Main Street.' — a REAL plural of 'stores'. Its root has no left
    ``compound`` chain and no ``appos`` child (a PP adjunct, not an object), so the recovery must
    not fire, the derivation must not crash, and no verb-stem subject ('store'/'stor') may be
    minted from it."""
    doc = _doc("The stores on Main Street.")
    assert ling._repair_nominalized_svo_root(doc) == 0
    facts = ling.derive_sentence_facts("The stores on Main Street.", reference=None)
    for f in facts:
        assert f.subject not in ("store", "stor", "stores"), facts
        assert f.rel_type not in ("store", "stor"), facts


@requires_model
def test_comma_appositive_fragment_does_not_fire():
    """Independent re-derivation finding R1: a verbless comma-appositive FRAGMENT shaped
    '<compound chain> <plural -s head>, a <adj> <noun>.' carries the same compound chain + plural
    -s head + ``appos`` child as the mis-parse — but an SVO object is ADJACENT to its verb while
    a supplemented appositive is set off by a comma. The repair must not fire, no predication may
    be fabricated, and the honest nominal reading (has_role off the head) must survive."""
    for sent, head in (("Data storage arrays, a big concern.", "arrays"),
                       ("Nightly server backups, a real chore.", "backups"),
                       ("The seven calibration tones, a fragile thing.", "tones")):
        doc = _doc(sent)
        assert ling._repair_nominalized_svo_root(doc) == 0, sent
        facts = ling.derive_sentence_facts(sent, reference=None)
        # no fabricated SVO predication off the head lemma
        for f in facts:
            assert f.rel_type != head, (sent, facts)
        # the honest appositive reading survives
        assert any(f.subject == head and f.rel_type == "has_role" for f in facts), (sent, facts)


@requires_model
def test_ies_surface_is_refused_not_guessed():
    """Independent re-derivation finding R2: a -ies 3sg verb forced into the bug shape must never
    repair to a non-word lemma ('carries' → 'carrie'). The coin-flip class is refused outright —
    fail toward today's parse, never toward corruption."""
    assert ling._verb3sg_stem("carries") is None
    assert ling._verb3sg_stem("studies") is None
    assert ling._verb3sg_stem("dies") is None
    # ...while the confirmed -s/-es classes keep stripping.
    assert ling._verb3sg_stem("stores") == "store"
    assert ling._verb3sg_stem("watches") == "watch"


@requires_model
def test_healthy_svo_parse_is_not_touched():
    """A healthy SVO parse (VERB ROOT) is refused by the verbless gate — the repair must not fire
    and the derivation must be exactly what it was before the repair existed."""
    doc = _doc("The registry refreshes its anchor table every ninth cycle.")
    assert ling._repair_nominalized_svo_root(doc) == 0
    toks = {t.text: t for t in doc}
    assert toks["refreshes"].pos_ == "VERB" and toks["refreshes"].dep_ == "ROOT"  # untouched
    facts = ling.derive_sentence_facts(
        "The registry refreshes its anchor table every ninth cycle.", reference=None)
    assert facts
    assert any(f.subject == "registry" and f.rel_type == "refresh" for f in facts), facts


# ─────────────────────────── the repair itself, on the raw Doc ───────────────────────────

@requires_model
def test_repair_rewrites_exactly_the_misparse_shape_and_keeps_ents():
    """On the raw mis-parsed Doc the repair re-types the root VERB/VBZ with the stripped lemma,
    promotes the rightmost left compound to ``nsubj`` (the chain re-hangs under it as
    ``compound``) and the appositive child to ``dobj`` — and leaves GLiNER2 ``ent_type_`` spans
    untouched."""
    doc = _doc(REPRO)
    doc.set_ents([doc.char_span(37, 60, label="PRODUCT")])  # "seven calibration tones"
    assert [(e.text, e.label_) for e in doc.ents] == [("seven calibration tones", "PRODUCT")]

    assert ling._repair_nominalized_svo_root(doc) == 1
    toks = {t.text: t for t in doc}
    assert toks["stores"].pos_ == "VERB"
    assert toks["stores"].tag_ == "VBZ"
    assert toks["stores"].lemma_ == "store"
    assert toks["lattice"].dep_ == "nsubj"
    assert toks["zephyrite"].head.i == toks["lattice"].i  # chain re-hung under the NP head
    assert toks["tones"].dep_ == "dobj"
    assert [(e.text, e.label_) for e in doc.ents] == [("seven calibration tones", "PRODUCT")]


@requires_model
def test_repair_is_failsafe_on_an_empty_doc():
    """No ROOT, nothing to repair: the helper returns 0 and never raises (fail-safe contract).""" 
    assert ling._repair_nominalized_svo_root(_doc("")) == 0


# ─────────────────────────── the 3sg stem stripper (orthography only) ───────────────────────────

def test_verb3sg_stem_strips_s_and_es_classes():
    f = ling._verb3sg_stem
    assert f("stores") == "store"      # plain -s
    assert f("makes") == "make"
    assert f("uses") == "use"          # silent-e stem spells -es, lands on the stem
    assert f("changes") == "change"
    assert f("watches") == "watch"     # sibilant digraph takes -es
    assert f("washes") == "wash"
    assert f("passes") == "pass"       # -ss stems take -es
    assert f("boxes") == "box"
    assert f("buzzes") == "buzz"
    assert f("goes") == "go"           # -o stems take -es
    assert f("sizes") == "size"        # single -zes is ALWAYS a silent-e stem (z doubles: buzzes)
    assert f("amazes") == "amaze"
    assert f("uses") == "use"          # silent-e -ses stems land on the stem


@requires_model
def test_stem_declared_residues_are_pinned():
    """The lopsided silent-e minorities reviewer A catalogued are ACCEPTED residues — pinned so a
    future edit cannot change them silently (see _verb3sg_stem's docstring for the class logic)."""
    f = ling._verb3sg_stem
    assert f("focuses") == "focuse"    # ses minority (majority: uses/raises/chooses)
    assert f("buses") == "buse"
    assert f("caches") == "cach"       # ches minority (majority: searches/fetches/touches)
    assert f("axes") == "ax"           # xes minority (majority: taxes/boxes/fixes)
    assert f("shoes") == "sho"         # oes minority (majority: goes/does/echoes)
    assert f("quizzes") == "quizz"     # zzes minority (majority: buzzes/fizzes)


@requires_model
def test_parenthetical_interrupted_svo_stays_todays_parse():
    """Contract boundary (re-derivation finding F1): a parse whose clause carries a real VERB
    (the parenthetical participle 'designed') is OUTSIDE the verbless contract — the gates decline
    it and today's parse stands; owning that shape would be a separate repair class."""
    doc = _doc("The lattice stores, as designed, seven calibration tones.")
    assert ling._repair_nominalized_svo_root(doc) == 0


def test_verb3sg_stem_refuses_non_verb_surfaces():
    f = ling._verb3sg_stem
    assert f("press") is None          # bare -ss: 3sg forms add -es, so this is a noun/base form
    assert f("focus") is None          # Latin singular -us
    assert f("run") is None            # no -s at all
    assert f("is") is None             # stripping must not leave a single letter
    assert f("") is None
    assert f("a1s") is None            # non-alphabetic surface
