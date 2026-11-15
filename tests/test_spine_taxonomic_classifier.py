"""Taxonomic-classifier copula construction — "X is a <classifier> of Y" → (X, subclass_of, Y).

Deterministic capture gap in ``derive_sentence_facts`` (src/extraction/linguistics.py):
"A golden retriever is a breed of dog" grabbed the SEMANTICALLY-EMPTY classifier noun ("breed") as
the type and DROPPED the real parent "dog" (the of-PP object) — the L4 subclass ladder
golden-retriever -> dog -> mammal never built.

The fix (``_chain_classification_containment`` section A + ``_TAXONOMIC_CLASSIFIERS`` closed class):
the CLASSIFIER noun is COLLAPSED (never an entity/type — THE HARD LINE) and the transitive taxonomic
ladder rung is filed: ``(X, subclass_of, Y)`` — both X and Y are TYPES / common nouns, so it is
``subclass_of`` (the transitive ladder), NOT ``instance_of`` (which files a NAMED instance at its
type). Subject-agnostic: the classifier is a bounded functional class; X and Y stay OPEN nouns.

These run the REAL ``derive_sentence_facts`` path (a raw string → full parse + all chains), NOT a
synthetic edge list — prior passes missed because they asserted synthetic shapes.

THE LIVE-PATH GUARD (``test_classifier_survives_minted_occupation_override``): on the deployed
harvest, ``_build_typed_doc`` runs GLiNER2 ``extract_relations`` and STAMPS the minted rel onto the
Doc (``set_minted_rel``); GLiNER2's prep-blind relation scorer FUZZILY mints ``occupation`` for
"<X> is a breed of <Y>". ``_emit`` then OVERRIDES its own rel with any minted rel for the
(subj_tok, obj_tok) pair — so the taxonomic chain's ``subclass_of`` was silently rewritten to
``occupation`` on the live path even though it fired. The fix omits ``subj_tok`` from the chain's
``_emit`` (mirroring the employment/kinship chains) so the override can't fire. These tests stamp the
minted rel EXACTLY as ``_build_typed_doc`` does, reproducing the live seam a raw-string test misses.

Run: python3 -m pytest tests/test_spine_taxonomic_classifier.py -q   (tests/ is gitignored → git add -f)
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _derive_with_minted_rel(sentence, subj_word, obj_word, minted_rel):
    """Reproduce the live ``_build_typed_doc`` seam: parse, STAMP a GLiNER2-minted rel on the
    (subj head, obj head) token pair via ``set_minted_rel`` (exactly what the harvest does), then run
    the REAL deriver on that Doc. This is the seam a raw-string derive test does NOT exercise."""
    doc = m._parse(sentence)
    _si = _oi = None
    for _t in doc:
        if _t.text == subj_word:
            _si = _t.i
        if _t.text == obj_word:
            _oi = _t.i
    assert _si is not None and _oi is not None, f"tokens not found in {sentence!r}"
    m.set_minted_rel(doc, _si, _oi, minted_rel)
    return m.derive_sentence_facts(doc, _REF)


# ── POSITIVE: the taxonomic-classifier construction → subclass_of, classifier collapsed ─────────

@pytest.mark.parametrize("text,child,parent,classifier", [
    ("A golden retriever is a breed of dog.", "golden retriever", "dog", "breed"),
    ("A dog is a kind of mammal.", "dog", "mammal", "kind"),
    ("A road bike is a type of bicycle.", "road bike", "bicycle", "type"),
])
def test_classifier_construction_builds_subclass_ladder(text, child, parent, classifier):
    facts = m.derive_sentence_facts(text, _REF)
    tr = _triples(facts)
    # the ladder rung is filed on the REAL parent (the of-PP object), as subclass_of
    assert (child, "subclass_of", parent) in tr, f"{text!r} → expected subclass_of({parent}); got {tr}"
    # the classifier noun is COLLAPSED — never filed as a type/entity via ANY rel
    assert not any(
        (f.object or "").strip().lower() == classifier for f in facts
    ), f"{text!r} leaked the collapsed classifier {classifier!r}: {tr}"
    # and specifically NOT the old (X, instance_of, <classifier>) drop-the-parent bug
    assert (child, "instance_of", classifier) not in tr, f"{text!r} regressed to instance_of: {tr}"


# ── NEGATIVE: plain "X is a Y" (no classifier) stays the instance_of / naming path ──────────────

def test_plain_named_instance_stays_instance_of():
    facts = m.derive_sentence_facts("Fraggle is a dog.", _REF)
    tr = _triples(facts)
    assert ("fraggle", "instance_of", "dog") in tr, tr
    assert not any(f.rel_type == "subclass_of" for f in facts), tr


def test_scalar_copula_untouched():
    # "my commute is 45 minutes" is the scalar/attribute lane — no subclass edge introduced.
    facts = m.derive_sentence_facts("My commute is 45 minutes.", _REF)
    assert not any(f.rel_type == "subclass_of" for f in facts), _triples(facts)


def test_classifier_word_as_adjective_no_of_pp_no_fire():
    # "a kind gesture" — "kind" is an ADJ modifier, NO of-PP → the construction must NOT fire.
    facts = m.derive_sentence_facts("It is a kind gesture.", _REF)
    assert not any(f.rel_type == "subclass_of" for f in facts), _triples(facts)


def test_classifier_without_of_pp_no_fire():
    # "a type" with NO of-PP parent → require the of-PP; leave today's behaviour, no subclass edge.
    facts = m.derive_sentence_facts("A poodle is a type.", _REF)
    assert not any(f.rel_type == "subclass_of" for f in facts), _triples(facts)


def test_propn_subject_not_this_construction():
    # X is a NAMED instance (PROPN) → NOT the type-subclass construction; leave it (no subclass edge).
    facts = m.derive_sentence_facts("Rex is a kind of dog.", _REF)
    assert not any(f.rel_type == "subclass_of" for f in facts), _triples(facts)


# ── LIVE-PATH GUARD: subclass_of survives the GLiNER2-minted occupation override ────────────────

@pytest.mark.parametrize("text,subj,obj,child,parent", [
    ("A golden retriever is a breed of dog.", "retriever", "dog", "golden retriever", "dog"),
    ("A dog is a kind of mammal.", "dog", "mammal", "dog", "mammal"),
    ("A road bike is a type of bicycle.", "bike", "bicycle", "road bike", "bicycle"),
])
def test_classifier_survives_minted_occupation_override(text, subj, obj, child, parent):
    """The gap the raw-string test missed: GLiNER2 mints ``occupation`` for the (X, Y) pair and
    ``_emit`` would override the chain's ``subclass_of`` with it. With the minted rel stamped exactly
    as the harvest does, the taxonomic edge MUST still be ``subclass_of``, never ``occupation``."""
    facts = _derive_with_minted_rel(text, subj, obj, "occupation")
    tr = _triples(facts)
    assert (child, "subclass_of", parent) in tr, f"{text!r} (minted occupation) → expected subclass_of; got {tr}"
    assert not any(f.rel_type == "occupation" for f in facts), \
        f"{text!r} the GLiNER2-minted occupation clobbered the taxonomic subclass_of: {tr}"
