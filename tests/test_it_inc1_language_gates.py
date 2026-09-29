"""[it branch] The English engine increment stays inert on the Italian install.

Gated on ``english_grammar_available()`` (FAULTLINE_LANGUAGE) and on the parse scheme:
the split noun-compound repair (English compounds are right-headed; Italian "nave scuola" is
left-headed) and the degree-adjective WordNet dimension (English lexicon). No Italian model
needed: Docs are built by hand with the exact labels of the #37 split-compound shape.
"""
import pytest
import spacy
from spacy.tokens import Doc

from src.extraction import linguistics as L


def _split_doc(lang):
    return Doc(spacy.blank(lang).vocab, words=["vedo", "nave", "scuola"],
               pos=["VERB", "NOUN", "NOUN"], deps=["ROOT", "obj", "obj"], heads=[0, 0, 0])


def test_split_compound_repair_inert_on_an_italian_parse(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    doc = _split_doc("it")
    assert L._repair_split_nominal_compound(doc) == 0
    assert doc[1].dep_ == "obj" and doc[1].head.i == 0


def test_split_compound_repair_inert_on_the_italian_install(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    doc = _split_doc("en")
    assert L._repair_split_nominal_compound(doc) == 0
    assert doc[1].dep_ == "obj"


def test_split_compound_repair_still_fires_on_an_english_install(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    doc = _split_doc("en")
    assert L._repair_split_nominal_compound(doc) == 1
    assert doc[1].dep_ == "compound" and doc[1].head.i == 2


def test_degree_dimension_lexicon_is_inert_on_the_italian_install(monkeypatch):
    from src.api import wordnet_ladder as wl

    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    for word in ("lungo", "alto", "long", "tall"):
        assert wl.degree_adjective_dimension(word, "height") is None
        assert wl.degree_adjective_dimensions(word) == frozenset()
