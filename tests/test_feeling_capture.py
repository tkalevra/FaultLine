"""Regression gate for the affective/feeling capture seam (_detect_feeling_states).

The seam GLiNER2 deliberately does NOT cover (Pitfall 11): feelings are not entities. The
ONE deterministic primitive is the grammatical feel-VERB construction (it grabs the complement
because nothing else surfaces it); it makes NO claim the complement is an emotion — the
re_embedder "what is X" grounding discerns that and self-builds the emotion is-a ladder.
There is NO seeded feeling word-list. Spec: DEV/DESIGN-feeling-and-temporal-capture.md.

0-tolerance behaviors:
  * feel-verb fires for ANY non-junk complement (incl. novel feelings → grounded → grow map)
  * bare copula ("I am X") is NOT captured here — it's ambiguous (name/role/feeling) and routed
    through grounding (no word-list), so this function must NOT emit for it
  * negated feelings are skipped (no junk does_not_feel predicates)
  * names/roles/cognition are never emitted as feelings
"""
import pytest

from src.api.main import _detect_feeling_states


def _objs(text):
    return {e["object"] for e in _detect_feeling_states(text)}


def _rels_ok(text):
    return all(e["subject"] == "user" and e["rel_type"] == "feels"
               for e in _detect_feeling_states(text))


@pytest.mark.parametrize("text,expected", [
    ("i feel anxious", {"anxious"}),
    ("i'm feeling worried", {"worried"}),
    ("i am feeling really down", {"down"}),
    ("i felt sad today", {"sad"}),
    ("i feel stressed", {"stressed"}),
])
def test_feelverb_feelings_are_captured(text, expected):
    assert _objs(text) == expected
    assert _rels_ok(text)


def test_feelverb_captures_novel_complement():
    # Unambiguous feel-verb → captures a complement absent from any lexicon (there is none),
    # so novel feelings reach grounding and grow the map. No seeded word-list.
    assert "crap" in _objs("i feel like crap")


@pytest.mark.parametrize("text", [
    "i am worried",              # bare copula — ambiguous, routed through grounding, NOT here
    "i am stressed",             # bare copula
    "i am dan",                # bare copula (a name) — must never be emitted as a feeling
    "my name is dan",          # name
    "i am a teacher",            # role
    "i am thinking about goats",  # cognition, not an emotion
])
def test_bare_copula_and_non_feelings_not_captured_here(text):
    assert _detect_feeling_states(text) == []


@pytest.mark.parametrize("text", [
    "i don't feel good",         # pre-verb negation
    "i feel not great",          # inline negation group
])
def test_negated_feelings_skipped(text):
    assert _detect_feeling_states(text) == []


def test_empty_and_garbage_are_safe():
    assert _detect_feeling_states("") == []
    assert _detect_feeling_states("the goats are in the east field") == []


# ── self-predication grounding router (deterministic gate + retired greedy regex) ──
from src.api.main import _SELF_PREDICATION_COPULA, _SELF_PREDICATION_RELS, _IDENTITY_PATTERNS


@pytest.mark.parametrize("text,fires", [
    ("i am worried", True),       # bare copula → grounded (not regex-named)
    ("i'm dan", True),
    ("i am a teacher", True),
    ("my name is dan", False),  # explicit naming → handled by precise pattern, not the router
    ("i feel anxious", False),    # feel-verb owns it
    ("what do i know about fences", False),
    ("the goats are out", False),
])
def test_self_predication_gate(text, fires):
    assert bool(_SELF_PREDICATION_COPULA.search(text)) is fires


def test_greedy_copula_identity_patterns_retired():
    # The greedy `i am X` / `i'm X` name regexes MUST be gone — they were the junk-name leak.
    pats = [p.pattern for p in _IDENTITY_PATTERNS]
    assert not any("i am (" in p or "i'm (" in p for p in pats), pats
    # Explicit naming constructions remain.
    assert any("my name is" in p for p in pats)


def test_self_predication_routes_to_existing_rels_only():
    # Routing targets are real relations, not a name/feeling word-list.
    assert set(_SELF_PREDICATION_RELS) <= {"feels", "also_known_as", "occupation"}
