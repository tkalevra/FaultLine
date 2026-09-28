"""[it branch] The install-language signal and the WordNet gate.

FAULTLINE_LANGUAGE unset/en => English behaviour, byte-for-byte; ``it`` => English-only
lexical sources stay inert (Princeton WordNet would mint wrong ladders from Italian lemmas
that are English homographs: cane, camera, burro)."""

import pytest

from src.extraction import install_language as il


@pytest.mark.parametrize("val,expected", [(None, True), ("", True), ("en", True), (" EN ", True),
                                          ("it", False), ("IT", False), ("es", False)])
def test_english_grammar_available(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    else:
        monkeypatch.setenv("FAULTLINE_LANGUAGE", val)
    assert il.english_grammar_available() is expected


def test_wordnet_source_inert_on_italian_install(monkeypatch):
    from src.api import wordnet_ladder as wl
    monkeypatch.setenv("FAULTLINE_LANGUAGE", "it")
    assert wl._wn() is None
    # public entry points degrade to the documented corpus-unavailable answer
    assert not wl.hypernym_rungs("cane")
    assert wl.has_common_noun_sense("cane") is None


def test_wordnet_gate_is_noop_on_english(monkeypatch):
    from src.api import wordnet_ladder as wl
    monkeypatch.delenv("FAULTLINE_LANGUAGE", raising=False)
    sentinel = object()
    monkeypatch.setattr(wl, "_WN", sentinel)
    assert wl._wn() is sentinel
