"""Unit tests for the POSSESSIVE ALIENABILITY gate (``SPINE_POSSESSIVE_ALIENABILITY``).

Pins the thin-``owns`` capture fix: a first-person possessive ("my X") only reads as OWNERSHIP when
the possessum satisfies the DECLARED RANGE (``tail_types``) of the ownership relation. The
discriminator is the **alienable/inalienable possession** distinction resolved as a SHACL
``sh:class`` value-type check, with two deterministic offline type oracles (GLiNER2 label, then
WordNet supersenses) plus an inalienable dominant-sense veto.

PURE — no DB, no network, no LLM. ``_rel_tail_types`` is monkeypatched with the LIVE seeded values
read from ``public.rel_types`` (owns tail = {Animal,Object,Organization}); WordNet is a local
offline corpus. Requires ``en_core_web_sm`` for the deriver cases (``tools/fltest.py`` sets it).

Run: python3 tools/fltest.py --bug OWNS --test tests/test_spine_possessive_alienability.py
     (tests/ is gitignored → git add -f)
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.api import wordnet_ladder as wl  # noqa: E402
from src.extraction import linguistics as L  # noqa: E402

# LIVE seed (public.rel_types, 2026-07-28).
_LIVE_TAILS = {"owns": ["Animal", "Object", "Organization"], "related_to": ["ANY"]}

# The real LongMemEval recall census (854 lines / 76 questions). LEGITIMATE ownership MUST survive;
# a wrongly-demoted legit object is a FATAL regression, not a tuning miss.
CENSUS_LEGIT = [
    "navy suit", "tent", "phone", "samsung galaxy s22", "laptop", "smartwatch", "travel adapter",
    "smart thermostat", "iphone pro", "anker powercore 20000", "telephone", "devices", "bike",
    "auto",
]
# GLiNER2 labels MEASURED with the live ``_DEFAULT`` label set on "My <X> is important to me."
CENSUS_LEGIT_GLINER = {o: "object" for o in CENSUS_LEGIT}


@pytest.fixture(autouse=True)
def _live_tails(monkeypatch):
    monkeypatch.setattr(
        L, "_rel_tail_types",
        lambda rel: list(_LIVE_TAILS.get((rel or "").strip().lower(), [])))


# ── the WordNet supersense oracles ───────────────────────────────────────────────────────────────

def test_supersense_type_match_tristate():
    tail = ["Animal", "Object", "Organization"]
    assert wl.supersense_type_match("laptop", tail) is True        # noun.artifact
    assert wl.supersense_type_match("hometown", tail) is False     # noun.location only
    assert wl.supersense_type_match("friend", tail) is False       # noun.person only
    assert wl.supersense_type_match("smartwatch", tail) is None    # no synset → undecidable
    assert wl.supersense_type_match("laptop", ["ANY"]) is True     # unconstrained range
    assert wl.supersense_type_match("laptop", []) is None          # nothing testable


def test_supersense_match_is_any_sense_not_mfs():
    """ANY-SENSE, not MFS: these have a non-ownable DOMINANT sense but a real artifact sense.
    An MFS rule would wrongly demote them (ring→noun.attribute, shoe→noun.state,
    printer→noun.person)."""
    tail = ["Animal", "Object", "Organization"]
    for term in ("ring", "shoes", "printer"):
        assert wl.supersense_type_match(term, tail) is True, term


def test_plural_surface_uses_morphy_union():
    """``devices`` has its OWN synset (noun.cognition, "plans or schemes"); the morphy base
    ``device`` is noun.artifact. The union keeps the real possession."""
    assert wl.supersense_type_match("devices", ["Object"]) is True


def test_is_inalienable_dominant():
    assert wl.is_inalienable_dominant("eye") is True        # noun.body
    assert wl.is_inalienable_dominant("laptop") is False    # noun.artifact
    assert wl.is_inalienable_dominant("smartwatch") is None  # no synset → undecidable


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("obj", CENSUS_LEGIT)
def test_every_legitimate_census_object_survives(obj):
    """FATAL if any of these demotes — with GLiNER2 typing AND with it absent."""
    assert L._possessum_admitted_by_rel_tail(obj, CENSUS_LEGIT_GLINER[obj], "owns") is True
    assert L._possessum_admitted_by_rel_tail(obj, "", "owns") is True


@pytest.mark.parametrize("obj,gliner", [
    ("hometown", "location"),   # associative place
    ("friend", "person"),       # social relation
    ("past", "concept"),        # abstract/temporal
    ("way", "concept"),
    ("stay", "location"),
    ("access to power outlet", "concept"),
])
def test_junk_demoted_via_declared_tail(obj, gliner):
    assert L._possessum_admitted_by_rel_tail(obj, gliner, "owns") is False


def test_inalienable_veto_outranks_a_permissive_gliner_label():
    """GLiNER2 types "eye" as Object (which the tail admits) — the body-part veto still demotes."""
    assert L._possessum_admitted_by_rel_tail("eye", "object", "owns") is False


def test_head_reduction_is_never_used_for_a_negative_decision():
    """Right-hand-head reduction is a POSITIVE device only. "iphone pro" must not reduce to "pro"
    (noun.person) and demote a real device; an unclassifiable multiword is UNDECIDABLE → kept."""
    assert L._possessum_admitted_by_rel_tail("iphone pro", "", "owns") is True


# ── fail-safe: every undecidable input keeps today's emit ────────────────────────────────────────

def test_failsafe_paths_keep_todays_emit(monkeypatch):
    assert L._possessum_admitted_by_rel_tail("", "", "owns") is True          # no possessum
    assert L._possessum_admitted_by_rel_tail("hometown", "", "") is True      # no rel
    monkeypatch.setattr(L, "_rel_tail_types", lambda rel: [])
    assert L._possessum_admitted_by_rel_tail("hometown", "location", "owns") is True  # cold overlay
    monkeypatch.setattr(L, "_rel_tail_types", lambda rel: ["ANY"])
    assert L._possessum_admitted_by_rel_tail("hometown", "location", "owns") is True  # ANY range


def test_failsafe_on_oracle_exception(monkeypatch):
    monkeypatch.setattr(wl, "is_inalienable_dominant",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert L._possessum_admitted_by_rel_tail("hometown", "location", "owns") is True


def test_unrecognized_gliner_label_falls_through_to_wordnet():
    """A label outside the fixed GLiNER2 set must not decide by itself."""
    assert L._possessum_admitted_by_rel_tail("laptop", "Widget", "owns") is True
    assert L._possessum_admitted_by_rel_tail("hometown", "Widget", "owns") is False


# ── the deriver: flag OFF is byte-identical, flag ON demotes rather than drops ───────────────────

def _rels(sentence):
    return sorted(f"{f.rel_type}:{f.object}"
                  for f in L.derive_sentence_facts(sentence, None) if f.subject == "user")


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(L, "SPINE_POSSESSIVE_ALIENABILITY", False)
    assert "owns:hometown" in _rels("My hometown is important to me.")
    assert "owns:laptop" in _rels("My laptop is important to me.")


def test_flag_on_demotes_junk_and_keeps_real_ownership(monkeypatch):
    monkeypatch.setattr(L, "SPINE_POSSESSIVE_ALIENABILITY", True)
    junk = _rels("My hometown is important to me.")
    assert "owns:hometown" not in junk
    assert "related_to:hometown" in junk       # DEMOTED, never dropped — "we don't forget"
    assert "owns:laptop" in _rels("My laptop is important to me.")


def test_kinship_still_wins_over_the_gate(monkeypatch):
    """The kinship cue class runs BEFORE the gate — "my mother" keeps its specific kin relation."""
    monkeypatch.setattr(L, "SPINE_POSSESSIVE_ALIENABILITY", True)
    facts = L.derive_sentence_facts("My mother is important to me.", None)
    assert not any(f.rel_type == "owns" for f in facts)
