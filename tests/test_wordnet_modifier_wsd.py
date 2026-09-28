"""Unit tests for the COMPOUND-MODIFIER sense VETO on the WordNet L4 ladder.

PURE tests — no DB, no network, no GLiNER2, no LLM. WordNet (offline corpus) only.

THE BUG PINNED HERE. Ingesting "My kiln firing is 9 hours." head-reduced the compound to the bare
noun ``firing`` and took its Most-Frequent-Sense ``fire.n.02`` — *the act of firing weapons at an
enemy* — minting ``kiln firing → firing → fire → attack → operation`` into L4, which recall then
rendered to the user as "Firing is a subclass of attack". The disambiguating signal (the compound
MODIFIER ``kiln``) was in the input and was being discarded by ``_head_noun``.

THE CONTRACT. With ``L4_WORDNET_MODIFIER_WSD`` ON, a modifier that CONTRADICTS the head's picked
sense makes the ladder ABSTAIN from the hypernym CHAIN while KEEPING the sense-independent
``(compound, head)`` bridge. It is a VETO, never a re-selection — we never guess a different sense.
With the flag OFF every ladder is byte-identical to today.

Run: python3 tools/fltest.py --bug WSD --test tests/test_wordnet_modifier_wsd.py
     (tests/ is gitignored → git add -f)
"""
import os

import pytest

from src.api import wordnet_ladder as W

pytestmark = pytest.mark.skipif(W._wn() is None, reason="offline WordNet corpus unavailable")

# REAL compounds mined from the archived bench corpus whose BEFORE placement is WRONG.
VETO_CASES = [
    ("kiln firing", "firing"),        # fire.n.02 "firing weapons at an enemy" (;c military)
    ("freshwater tank", "tank"),      # tank.n.01 "armored military vehicle"   (;c military)
    ("gallon tank", "tank"),          # same — 26 occurrences in the real corpus
    ("bike light", "light"),          # light.n.01 "electromagnetic radiation" (;c physics)
    ("dog sitting", "sitting"),       # sitting.n.01 "(photography) assuming a position"
]

# REAL compounds whose BEFORE placement is CORRECT — the regression guard. Zero of these may change.
HOLD_CASES = [
    "rifle firing",            # the military sense IS right here (modifier BACKS it)
    "music store", "dog bed", "baby girl", "star sneaker", "taxi ride", "birding trip",
    "bike rack", "data analysis", "cycling event", "guitar lesson", "leather boot",
    "breakfast idea", "photo album", "music festival", "hiking boot", "gift basket",
    "basil plant", "dress shoe", "camping trip", "business trip", "engine compartment",
    "photo frame", "coffee maker", "hindu festival", "movie festival",
    "charity golf tournament",
]


@pytest.fixture
def wsd_on(monkeypatch):
    monkeypatch.setenv("L4_WORDNET_MODIFIER_WSD", "true")


@pytest.fixture
def wsd_off(monkeypatch):
    monkeypatch.delenv("L4_WORDNET_MODIFIER_WSD", raising=False)


# ── the reported bug ────────────────────────────────────────────────────────────────────────────

def test_kiln_firing_ladders_into_attack_when_flag_off(wsd_off):
    """BEFORE (and with the flag OFF, forever): the wrong-sense chain is what ships."""
    parents = [p for _, p in W.hypernym_rungs("kiln firing")]
    assert "attack" in parents, parents


def test_kiln_firing_abstains_when_flag_on(wsd_on):
    """AFTER: no hypernym chain is minted — only the sense-independent compound→head bridge."""
    assert W.hypernym_rungs("kiln firing") == [("kiln firing", "firing")]


def test_kiln_firing_mints_no_wrong_sense_nodes(wsd_on):
    """The six poisoned type nodes (fire/attack/onrush/onset/onslaught/operation) never appear."""
    nodes = {n for rung in W.hypernym_rungs("kiln firing") for n in rung}
    assert nodes == {"kiln firing", "firing"}
    for poisoned in ("fire", "attack", "operation"):
        assert poisoned not in nodes


# ── the veto set (real corpus) ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("compound,head", VETO_CASES)
def test_veto_keeps_bridge_and_drops_chain(compound, head, wsd_on):
    """ABSTENTION granularity: the user's term stays walkable, the wrong-domain rungs do not."""
    rungs = W.hypernym_rungs(compound)
    assert rungs == [(compound, head)], rungs


@pytest.mark.parametrize("compound,head", VETO_CASES)
def test_veto_cases_are_unchanged_when_flag_off(compound, head, wsd_off):
    """Flag OFF is byte-identical: every veto case still ladders its full (wrong) chain."""
    assert len(W.hypernym_rungs(compound)) > 1


# ── the regression guard: correct placements must not move ──────────────────────────────────────

@pytest.mark.parametrize("compound", HOLD_CASES)
def test_correct_placements_are_untouched(compound, monkeypatch):
    monkeypatch.delenv("L4_WORDNET_MODIFIER_WSD", raising=False)
    before = W.hypernym_rungs(compound)
    monkeypatch.setenv("L4_WORDNET_MODIFIER_WSD", "true")
    assert W.hypernym_rungs(compound) == before, compound
    # sanity: these are real ladders, not empty misses ("cycling event" legitimately yields only
    # the bridge because `event` terminates immediately at a generic upper root).
    assert before, f"{compound} should ladder something"


def test_rifle_firing_keeps_the_military_sense(wsd_on):
    """Signal B (Lesk) BACKS the default for 'rifle firing' → no veto. The veto is not a blanket
    ban on domain-tagged senses; it fires only when the modifier disagrees."""
    parents = [p for _, p in W.hypernym_rungs("rifle firing")]
    assert "attack" in parents, parents


# ── helper contracts / fail-safety ──────────────────────────────────────────────────────────────

def test_modifier_tokens_mirrors_head_noun_guards():
    assert W._modifier_tokens("kiln firing") == ["kiln"]
    assert W._modifier_tokens("charity golf tournament") == ["charity", "golf"]
    assert W._modifier_tokens("firing") == []            # bare noun — no modifier
    assert W._modifier_tokens("run for the cure") == []  # connector → not a compound
    assert W._modifier_tokens("model 3 tank") == []      # non-alphabetic → not a clean compound


def test_bare_noun_never_vetoes(wsd_on):
    """No modifier ⇒ no evidence ⇒ today's MFS cascade stands (we never abstain blind)."""
    assert [p for _, p in W.hypernym_rungs("firing")] == ["fire", "attack", "operation"]


def test_veto_helper_is_fail_safe_on_garbage(wsd_on):
    """Never raises; a missing/odd input yields NO veto (today's behaviour)."""
    assert W._modifier_contradicts_sense("firing", (), None) is False
    assert W._modifier_contradicts_sense("", ("kiln",), None) is False
    assert W._modifier_contradicts_sense("zzzqqxnotaword", ("kiln",), None) is False


def test_content_word_test_is_wordnet_native_not_a_stoplist():
    """Open-class membership comes from WordNet itself (it excludes function words by design),
    so the Lesk bag needs no hand-curated stopword list."""
    assert W._is_content_word("kiln") is True
    assert W._is_content_word("burning") is True
    for function_word in ("and", "for", "with", "than"):
        assert W._is_content_word(function_word) is False, function_word
