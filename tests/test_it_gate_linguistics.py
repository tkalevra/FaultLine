"""[it branch] UD negation + generic-kind article reads, pinned with stub tokens.

No Italian spaCy model needed: the stubs carry the exact labels/morph it_core_news_sm emits
(measured: "Non ho un cane" → non/ADV/advmod PronType=Neg; "Il mio cane" → il det Definite=Def,
mio det:poss Poss=Yes; "un" det Definite=Ind|PronType=Art).
"""

import pytest

from src.extraction import linguistics as L


class _Morph:
    def __init__(self, feats):
        self._f = feats

    def get(self, k):
        v = self._f.get(k)
        return [v] if v else []


class _Tok:
    def __init__(self, text, dep, pos="X", lemma=None, morph=None, children=()):
        self.text, self.dep_, self.pos_ = text, dep, pos
        self.lemma_ = lemma or text.lower()
        self.morph = _Morph(morph or {})
        self.children = list(children)
        self.i = 0
        self.doc = None
        self.head = self
        for c in self.children:
            c.head = self


def _non_ho():
    non = _Tok("Non", "advmod", "ADV", morph={"PronType": "Neg"})
    return _Tok("ho", "ROOT", "VERB", lemma="avere", children=[non])


def test_ud_advmod_negation_reads_negated_on_italian_install(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    assert L._predicate_negated(_non_ho()) is True


def test_ud_polarity_neg_also_reads_negated(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    neg = _Tok("not", "advmod", "ADV", morph={"Polarity": "Neg"})
    assert L._predicate_negated(_Tok("v", "ROOT", "VERB", children=[neg])) is True


def test_plain_advmod_is_not_negation(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    piu = _Tok("più", "advmod", "ADV")
    assert L._predicate_negated(_Tok("abito", "ROOT", "VERB", children=[piu])) is False


def test_english_install_does_not_read_the_ud_advmod_arm(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    monkeypatch.setattr(L, "SPINE_GRANDCHILD_NEG", False)
    # English keeps the ClearNLP `neg` arc only: an advmod+PronType=Neg child is not read.
    assert L._predicate_negated(_non_ho()) is False


def _nsubj_with(dets):
    return _Tok("cane", "nsubj", "NOUN", children=dets)


def test_italian_possessive_after_article_is_not_generic():
    il = _Tok("Il", "det", "DET", morph={"PronType": "Art", "Definite": "Def"})
    mio = _Tok("mio", "det:poss", "DET", morph={"Poss": "Yes", "PronType": "Prs"})
    assert L._generic_kind_subject(_nsubj_with([il, mio])) is False


def test_italian_indefinite_article_is_not_generic():
    un = _Tok("un", "det", "DET", lemma="uno", morph={"PronType": "Art", "Definite": "Ind"})
    assert L._generic_kind_subject(_nsubj_with([un])) is False


def test_definite_article_alone_still_generic():
    il = _Tok("Il", "det", "DET", lemma="il", morph={"PronType": "Art", "Definite": "Def"})
    assert L._generic_kind_subject(_nsubj_with([il])) is True


def _che_clause():
    # "Ho una figlia che si chiama Anna": che PRON PronType=Rel, head chiama acl:relcl -> figlia
    figlia = _Tok("figlia", "obj", "NOUN")
    chiama = _Tok("chiama", "acl:relcl", "VERB")
    che = _Tok("che", "nsubj", "PRON", morph={"PronType": "Rel"})
    che.head, chiama.head = chiama, figlia
    che.tag_ = "PR"
    return che


def test_ud_relative_pronoun_resolves_on_italian_install(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    che = _che_clause()
    assert L._is_relative_pronoun(che) is True
    assert L._relative_pronoun_antecedent(che) == "figlia"


def test_english_relative_pronoun_still_needs_penn_tag(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    assert L._is_relative_pronoun(_che_clause()) is False


@pytest.mark.parametrize("lemma", ["the"])
def test_english_the_unchanged(lemma):
    the = _Tok("The", "det", "DET", lemma=lemma, morph={"PronType": "Art", "Definite": "Def"})
    assert L._generic_kind_subject(_nsubj_with([the])) is True
