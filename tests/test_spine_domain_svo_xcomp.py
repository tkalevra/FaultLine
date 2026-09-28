"""DOCEXTRACT — domain-knowledge (non-personal) SVO capture in the spine deriver.

The `ingest_document` lane under-extracted dense domain prose: a 3-sentence biology
paragraph yielded ONE fact. Root cause was spaCy parse defects on OOV/domain subjects,
NOT a deriver gate and NOT first-person gating.

  1. "Chlorophyll absorbs light most strongly ..." — the sm/md tagger mislabels the
     direct object "light" as an ``xcomp`` (clausal complement) instead of ``dobj``,
     so the SVO object was dropped and NO edge emitted. FIXED here: ``_svo_object_head``
     now recovers a bare NOUN/PROPN ``xcomp`` as the mis-parsed direct object.

  2. "Photosynthesis converts carbon dioxide and water into glucose and oxygen ..." —
     the tagger mis-POS-tags the finite verb "converts" as a plural NOUN (NNS), which
     COLLAPSES the whole main clause into a flat noun phrase (ROOT = "dioxide"). This is
     a parser-MODEL limitation (reproduces on en_core_web_sm AND en_core_web_md); it is
     NOT safely recoverable in the deriver without a verb lexicon (hardcoding, forbidden,
     and would not generalise to OOV domain verbs). Documented as a known limitation.

These are subject-agnostic: the fixes are grammar/dependency driven, no domain word list.
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.extraction.linguistics import derive_sentence_facts  # noqa: E402


def _edges(sentence):
    return [
        (f.subject, f.rel_type, f.object)
        for f in derive_sentence_facts(sentence, reference=None)
    ]


def test_transitive_xcomp_object_recovered():
    # "light" is mis-tagged xcomp (not dobj) under the OOV subject "Chlorophyll".
    # The base SVO relation MUST still emit (was dropped entirely pre-fix).
    edges = _edges("Chlorophyll absorbs light most strongly in the blue and red wavelengths.")
    assert any(
        s == "chlorophyll" and r == "absorb" and o.startswith("light")
        for (s, r, o) in edges
    ), edges


def test_domain_coordination_split_when_parse_is_clean():
    # Proves the deriver's SVO + object-coordination machinery is sound for a NON-personal
    # subject when the tagger parses the verb correctly ("produces" is tagged correctly).
    edges = _edges("Photosynthesis produces glucose and oxygen.")
    assert ("photosynthesis", "produce", "glucose") in edges, edges
    assert ("photosynthesis", "produce", "oxygen") in edges, edges


def test_domain_subjectside_coordination_split():
    edges = _edges("Plants convert carbon dioxide and water into glucose and oxygen.")
    assert ("plants", "convert", "carbon dioxide") in edges, edges
    assert ("plants", "convert", "water") in edges, edges


def test_no_regression_personal_statement():
    # First-person capture unaffected by the xcomp recovery.
    edges = _edges("I work at Google.")
    assert any(s == "user" and o == "google" for (s, r, o) in edges), edges


def test_intransitive_locative_still_works():
    edges = _edges("The Calvin cycle occurs in the stroma of the chloroplast.")
    assert any(r == "occur_in" for (_, r, _) in edges), edges


@pytest.mark.xfail(
    reason="spaCy sm/md mis-POS-tags the finite verb 'converts' as a plural NOUN, "
    "collapsing the main clause into a flat NP. Parser-MODEL limitation (needs "
    "en_core_web_trf); not safely deriver-recoverable without a verb lexicon.",
    strict=False,
)
def test_converts_misposed_collapse_known_limitation():
    edges = _edges(
        "Photosynthesis converts carbon dioxide and water into glucose and oxygen using light energy."
    )
    assert any(s == "photosynthesis" and o in ("glucose", "oxygen") for (s, _, o) in edges), edges
