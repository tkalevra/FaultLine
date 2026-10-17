"""FOSS-009 es pin: a pet name files as the PREFERRED label and renders in recall.

On es 761cf914, "Mi gata se llama Fraggle y tiene 3 anos." filed the name as a plain
also_known_as (altLabel): the alias registered NON-preferred and the recall display
resolver (preferred-alias-first, dead-name prevention) kept rendering the type noun
("Gata tiene 3 anos ... You have a pet that is Gata") - "Como se llama mi gata?" was
unanswerable from a store that held the answer. en files the same construction as
pref_name (preferred_label) and renders "your cat preferred name is fraggle".

The fix aligns the es naming chain: the non-kin leg of _chain_es_pronominal_naming
files (named-thing, pref_name, name, preferred_label=True) - ingest then registers the
alias PREFERRED (pref_name => is_pref=True, user_stated source, registry rank) and
every recall lane names the entity Fraggle.

Pins run WITHOUT an es model: the naming-chain pins drive the REAL chain through
derive_sentence_facts on a hand-built UD es Doc (the test_es_inc1 technique - no
model download needed, skips only if spaCy itself is missing); the render pins drive
the ONE display seam (resolve_entity_display / resolve_display_name) on a fake cursor.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.extraction import linguistics as m

_spaCy = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("spacy") is None,
    reason="spaCy not installed - the deriver cannot run at all",
)

_REF = None  # derive_sentence_facts accepts a date; the naming leg ignores it


# ---- 1. static contract: the es naming leg files the prefLabel (always runs) ----


def _es_naming_chain_source() -> str:
    text = (_ROOT / "src" / "extraction" / "linguistics.py").read_text(encoding="utf-8")
    start = text.index("def _chain_es_pronominal_naming")
    end = text.index("def _chain_es_dative_experiencer")
    return text[start:end]


def test_es_naming_leg_files_pref_name_as_preferred_label():
    """The non-kin leg of the es pronominal naming chain must file pref_name with
    preferred_label=True - a plain also_known_as renders as the type noun (FOSS-009)."""
    src = _es_naming_chain_source()
    leg = src[src.index("if not _kin_bound:"):]
    assert '_emit(subject, "pref_name", proper' in leg, (
        "the es naming leg no longer files the name as pref_name - recall will render "
        "the type noun again (FOSS-009 regression)")
    assert "preferred_label=True" in leg, "the name must carry the prefLabel flag"


def test_kin_role_alias_leg_stays_an_also_known_as_slot():
    """The kin leg's ROLE alias ("mi hermana se llama Ana" -> (ana, also_known_as,
    hermana)) is a SLOT on the named person, not a display name - it must stay an
    also_known_as so ingest's role-alias non-preference rule keeps applying."""
    src = _es_naming_chain_source()
    assert 'also_known_as", _hs' in src, (
        "the kin role-alias leg changed rel - the role slot must stay a non-preferred alias")


# ---- 2. behavioral: the REAL chain on a hand-built UD es Doc (no model) ----


def _hand_es_doc(words, pos, deps, heads, lemmas=None):
    """A hand-built UD Doc with lang_ == "es" (the test_es_inc1 technique): constructed
    UNPARSED, sentence start set, then deps/heads/lemma assigned per token (spaCy
    refuses sent_start writes on a Doc constructed already-parsed, E043). No model file
    needed - the es naming chain gates on doc.lang_, the expl:pv clitic, POS and the
    naming-verb lemma. (MorphAnalysis on a BLANK es vocab warns W100 and stores nothing
    - a blank vocab carries no feature inventory - so morphology-bearing reads like the
    1sg-possessive arm are simply not exercised here; the naming leg needs none.)"""
    import spacy
    from spacy.tokens import Doc
    vocab = spacy.blank("es").vocab
    doc = Doc(vocab, words=words, pos=pos)
    doc[0].is_sent_start = True
    for i, (d, h) in enumerate(zip(deps, heads)):
        doc[i].dep_ = d
        doc[i].head = doc[h]
    for i, lm in (lemmas or {}).items():
        doc[i].lemma = vocab.strings.add(lm)
    return doc


def _build_es_fraggle_doc():
    """Hand-built UD parse of "Mi gata se llama Fraggle y tiene 3 anos." (lang_ == es).
    The same construction the gauntlet battery drives through es_core_news_md."""
    return _hand_es_doc(
        ["Mi", "gata", "se", "llama", "Fraggle", "y", "tiene", "3", "anos", "."],
        ["DET", "NOUN", "PRON", "VERB", "PROPN", "CCONJ", "VERB", "NUM", "NOUN", "PUNCT"],
        ["det", "nsubj", "expl:pv", "ROOT", "obj", "cc", "conj", "nummod", "obj", "punct"],
        [1, 3, 3, 3, 3, 3, 3, 8, 6, 3],
        lemmas={3: "llamar", 6: "tener"},
    )


@_spaCy
def test_es_naming_chain_files_the_pet_name_as_pref_name(monkeypatch):
    """THE fixture-equivalent capture pin: "Mi gata se llama Fraggle ..." derives
    (gata, pref_name, fraggle) with preferred_label=True and NO also_known_as twin."""
    monkeypatch.setattr(m, "_SPACY_MODEL", "es_core_news_md")
    monkeypatch.setattr(m, "_nlp", lambda text: _build_es_fraggle_doc())
    monkeypatch.setattr(m, "_naming_verbs",
                        lambda: frozenset({"llamar", "llamarse", "llama"}))
    facts = m.derive_sentence_facts(
        "Mi gata se llama Fraggle y tiene 3 anos.", _REF, None)
    naming = [(f.subject, f.rel_type, f.object) for f in facts
              if f.rel_type in ("pref_name", "also_known_as")]
    assert ("gata", "pref_name", "fraggle") in naming, f"facts={facts}"
    assert ("gata", "also_known_as", "fraggle") not in naming, f"facts={facts}"
    pref = [f for f in facts if f.rel_type == "pref_name"][0]
    assert getattr(pref, "preferred_label", False), f"name not the prefLabel: {facts}"


@_spaCy
def test_es_naming_chain_leg_holds_for_the_perro_fixture_too(monkeypatch):
    """The battery's other surface: "Mi perro se llama Rex." -> (perro, pref_name, rex)."""
    monkeypatch.setattr(m, "_SPACY_MODEL", "es_core_news_md")
    monkeypatch.setattr(m, "_nlp", lambda text: _build_es_doc_noun("perro", "Rex"))
    monkeypatch.setattr(m, "_naming_verbs",
                        lambda: frozenset({"llamar", "llamarse", "llama"}))
    facts = m.derive_sentence_facts("Mi perro se llama Rex.", _REF, None)
    naming = [(f.subject, f.rel_type, f.object) for f in facts
              if f.rel_type in ("pref_name", "also_known_as")]
    assert ("perro", "pref_name", "rex") in naming, f"facts={facts}"


def _build_es_doc_noun(noun, name):
    return _hand_es_doc(
        ["Mi", noun, "se", "llama", name, "."],
        ["DET", "NOUN", "PRON", "VERB", "PROPN", "PUNCT"],
        ["det", "nsubj", "expl:pv", "ROOT", "obj", "punct"],
        [1, 3, 3, 3, 3, 3],
        lemmas={3: "llamar"},
    )


# ---- 3. the render seam: a preferred alias IS the recall name (no DB) ----


class _FakeCursor:
    """Answers the alias lookups in registration order (query -> rows).

    ``rows`` is SHARED with the owning _FakeDB so successive cursor() calls consume
    one FIFO across the resolver's sequential queries."""
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def execute(self, q, *a):
        self.queries.append(" ".join(q.split()))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out = list(self._rows)
        self._rows.clear()
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


_UUID = "48d885f7-1e2b-4c3a-9d4e-5f6a7b8c9d0e"


def test_preferred_alias_is_the_display_name():
    """The recall display resolver names the entity by its PREFERRED alias - the
    registry row the pref_name/preferred_label write produces."""
    from src.api.main import resolve_display_name, resolve_entity_display
    db = _FakeDB([("fraggle", None)])
    assert resolve_display_name(_UUID, db) == "fraggle"
    assert resolve_entity_display(_UUID, set(), {_UUID: "fraggle"}, db) == "fraggle"


def test_rendered_recall_answer_contains_the_name_fraggle():
    """THE work-order pin, at the render seam: the entity the walk reaches for
    "Como se llama mi gata?" resolves to Fraggle once the name is the preferred
    alias (the type noun "Gata" only renders when NO preferred alias exists)."""
    from src.api.main import resolve_display_name
    # Preferred present (post-fix registry state): the name renders.
    db_pref = _FakeDB([("fraggle", "Fraggle"), (False,)])
    assert resolve_display_name(_UUID, db_pref) == "Fraggle"
    # Regression mirror (pre-fix registry state): a NON-preferred-only entity still
    # falls back to the alias (documents the old hole was display, not storage).
    db_nonpref = _FakeDB([None, ("gata", None)])
    assert resolve_display_name(_UUID, db_nonpref) == "gata"
