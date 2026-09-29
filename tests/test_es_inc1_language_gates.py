"""es branch: the English engine increment stays inert on Spanish parses.

Three English-grammar additions are gated on the parse language (``_doc_is_spanish``):
the split noun-compound repair (English compounds are right-headed), the degree-adjective
WordNet dimension (English lexicon, ``npadvmod``), and the passive "is called/named N" arm for
sortal subjects (Spanish pronominal naming belongs to the ``_chain_es_*`` chains).
The compound pins build Docs by hand (no model); the capture pins need a Spanish model.
"""
import datetime

import pytest
import spacy
from spacy.tokens import Doc

from src.extraction import linguistics as m

_REF = datetime.datetime(2023, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _split_doc(lang):
    # verb + two adjacent sibling NOUN objects under one head — the #37 split-compound shape
    return Doc(spacy.blank(lang).vocab, words=["compré", "coche", "bomba"],
               pos=["VERB", "NOUN", "NOUN"], deps=["ROOT", "obj", "obj"], heads=[0, 0, 0])


def test_split_compound_repair_is_inert_on_a_spanish_doc():
    doc = _split_doc("es")
    assert m._repair_split_nominal_compound(doc) == 0
    assert doc[1].dep_ == "obj" and doc[1].head.i == 0  # left noun NOT re-headed


def test_split_compound_repair_still_fires_on_an_english_doc():
    doc = _split_doc("en")
    assert m._repair_split_nominal_compound(doc) == 1
    assert doc[1].dep_ == "compound" and doc[1].head.i == 2


_es = pytest.mark.skipif(
    not m.linguistics_available() or (getattr(m._get_nlp(), "lang", "") != "es"),
    reason="Spanish spaCy model not configured (SPACY_MODEL=es_core_news_*)",
)


@_es
def test_degree_adjective_wordnet_never_consulted_on_spanish(monkeypatch):
    from src.api import wordnet_ladder

    calls = []
    monkeypatch.setattr(wordnet_ladder, "degree_adjective_dimension",
                        lambda *a, **k: calls.append(a) or "length")
    for s in ("Mi barco mide 8 metros de largo.", "Mi hermano mide 2 metros de alto.",
              "La mesa tiene 3 metros de ancho."):
        m.derive_sentence_facts(s, _REF, None)
    assert calls == []


@_es
def test_spanish_pronominal_naming_gets_no_english_pref_name_arm():
    f = [(x.subject, x.rel_type, x.object)
         for x in m.derive_sentence_facts("Mi perro se llama Rex.", _REF, None)]
    assert not any(r == "pref_name" for _s, r, _o in f), f
