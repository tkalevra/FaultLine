"""Unit tests for STAGE 3 — the has_state competing lane for the LVC bland-head + with-PP "problem".

PURE tests — no DB, no network, no GLiNER2, no LLM. They exercise:
  * _arbitrate_spine_candidates — the COMMITTED Stage-2 arbitration now seeing a real competitor:
        a STRONG (affected, has_state, problem) vs a CRATER (user, participated_in, affected) →
        the has_state wins (decisive gap) and the participated_in loser is suppressed; a tie /
        close grade / ungroundable candidate KEEPS BOTH (fail-safe, "we don't forget").
        Grouping is on the AFFECTED-ENTITY span REGARDLESS of role (object for participated_in,
        subject for has_state) so the two genuinely compete.
  * linguistic_cue_overlay.resolve_problem_nouns — DB-down fail-safe → the problem_noun bootstrap
        floor (never empty, never the wrong class).
  * linguistics.analyze_events — the deriver lane: problem_head set on the problem head + with-PP
        ("had an issue with my car's GPS"), NOT set on a neutral head ("had a meeting with Sarah").

Run: python3 -m pytest tests/test_spine_has_state_lane.py -q   (tests/ is gitignored → git add -f)
"""
import os

import pytest

os.environ.setdefault("SPINE_CANDIDATE_ARBITRATION", "true")
os.environ.setdefault("SPINE_CANDIDATE_SCORE", "true")

import src.api.main as m  # noqa: E402
from src.api import linguistic_cue_overlay as lco  # noqa: E402


# ── _arbitrate_spine_candidates — the Stage-2 arbitration with a real has_state competitor ──────

def _pi(affected, grade="crater"):
    return {"subject": "user", "rel_type": "participated_in", "object": affected,
            "subject_type": "Person", "object_type": "Event", "_spine_grade": grade}


def _hs(affected, state="issue", grade="strong"):
    return {"subject": affected, "rel_type": "has_state", "object": state,
            "subject_type": "Object", "object_type": "Concept", "_spine_grade": grade}


def test_has_state_strong_beats_participated_in_crater():
    """STRONG has_state vs CRATER participated_in on the SAME affected span → suppress participated_in."""
    edges = [_pi("gps system", "crater"), _hs("gps system", "issue", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    rels = {(e["rel_type"], e["object"]) for e in kept}
    assert ("has_state", "issue") in rels
    assert ("participated_in", "gps system") not in rels  # cratered loser suppressed


def test_affected_span_groups_subject_for_has_state_object_for_participated():
    """The competing pair lands in ONE group despite the affected entity being in different roles
    (object for participated_in, subject for has_state)."""
    # If grouping were object-only, has_state(object='issue') and participated_in(object='gps system')
    # would NEVER share a group and both survive. The role-aware affected-span keys them together.
    edges = [_pi("gps system", "crater"), _hs("gps system", "issue", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert len(kept) == 1
    assert kept[0]["rel_type"] == "has_state"


def test_tie_keeps_both():
    """Equal grades → ambiguous → keep BOTH (we don't forget)."""
    edges = [_pi("router", "strong"), _hs("router", "problem", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert len(kept) == 2


def test_close_grade_keeps_both():
    """Adjacent grades (gap < decisive=2) → keep BOTH (fail-safe). strong(4) - neutral(3) = 1 < 2."""
    edges = [_pi("printer", "neutral"), _hs("printer", "issue", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert len(kept) == 2


def test_ungroundable_keeps_both():
    """A candidate with no groundable grade → keep ALL (never a silent drop)."""
    pi = _pi("laptop")
    pi.pop("_spine_grade")  # no attached grade, and the scorer needs DB/meta it can't reach here
    edges = [pi, _hs("laptop", "issue", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert len(kept) == 2


def test_single_candidate_inert():
    """No competitor on a span → untouched (today's live behavior)."""
    edges = [_pi("gps system", "crater")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert kept == edges


def test_non_competing_rels_untouched():
    """A scalar / ownership edge on the span is never arbitrated."""
    edges = [{"subject": "user", "rel_type": "owns", "object": "car"},
             {"subject": "car", "rel_type": "part_of", "object": "garage"}]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert kept == edges


def test_arbitration_flag_off_returns_unchanged(monkeypatch):
    monkeypatch.setenv("SPINE_CANDIDATE_ARBITRATION", "false")
    edges = [_pi("gps system", "crater"), _hs("gps system", "issue", "strong")]
    kept = m._arbitrate_spine_candidates(list(edges), is_edgeinput=False)
    assert len(kept) == 2  # legacy: no arbitration


# ── linguistic_cue_overlay.resolve_problem_nouns — DB-down fail-safe (bootstrap floor) ──────────

def test_problem_noun_bootstrap_floor_never_empty(monkeypatch):
    """No schema bound / DB unreadable → the problem_noun bootstrap floor (never empty, never the
    naming-verb wrong class)."""
    monkeypatch.setattr(lco.rel_type_overlay, "get_current_schema", lambda: None)
    cues = lco.resolve_problem_nouns("")  # empty dsn → unscoped fallback path
    assert "issue" in cues and "problem" in cues and "trouble" in cues
    assert cues  # never empty
    # The fallback must NOT be the naming-verb set (wrong class).
    assert "name" not in cues and "call" not in cues


def test_problem_noun_category_registered():
    assert lco.PROBLEM_NOUN_CATEGORY == "problem_noun"
    assert lco.PROBLEM_NOUN_CATEGORY in lco._BOOTSTRAP_BY_CATEGORY
    assert lco._BOOTSTRAP_BY_CATEGORY[lco.PROBLEM_NOUN_CATEGORY] is lco._BOOTSTRAP_PROBLEM_NOUNS


# ── linguistics.analyze_events — the deriver lane (problem_head discriminator) ──────────────────

_ling = pytest.importorskip("src.extraction.linguistics")


@pytest.mark.skipif(not _ling.linguistics_available(), reason="spaCy model not installed")
def test_problem_head_set_on_problem_plus_withpp():
    """'I had an issue with my car's GPS system' → an eventive head in problem_noun + with-PP → the
    EventAnalysis flags problem_head AND carries the affected entity as concerns."""
    evs = _ling.analyze_events("I had an issue with my car's GPS system")
    assert evs, "expected an LVC event analysis"
    # The bland 'issue' head with the with-PP affected entity.
    issue_ev = next((e for e in evs if (e.event or "").strip().lower() in ("issue", "an issue")), evs[0])
    assert issue_ev.concerns and "gps" in issue_ev.concerns.lower()
    assert issue_ev.problem_head is True


@pytest.mark.skipif(not _ling.linguistics_available(), reason="spaCy model not installed")
def test_problem_head_NOT_set_on_meeting_withpp():
    """'I had a meeting with Sarah' → meeting ∉ problem_noun → problem_head stays False (untouched)."""
    evs = _ling.analyze_events("I had a meeting with Sarah on Friday")
    assert evs
    for e in evs:
        assert e.problem_head is False


@pytest.mark.skipif(not _ling.linguistics_available(), reason="spaCy model not installed")
def test_problem_head_NOT_set_without_withpp():
    """A problem head with NO with-PP affected entity → problem_head stays False (discriminator needs
    BOTH the cue class AND the with-PP)."""
    evs = _ling.analyze_events("I had an issue yesterday")
    for e in (evs or []):
        assert e.problem_head is False


@pytest.mark.skipif(not _ling.linguistics_available(), reason="spaCy model not installed")
def test_problem_head_subject_agnostic_router():
    """'I had trouble with my router' → trouble ∈ problem_noun + with-PP → problem_head True, no GPS
    literal involved (subject-agnostic)."""
    evs = _ling.analyze_events("I had trouble with my router yesterday")
    assert evs
    ev = next((e for e in evs if e.problem_head), None)
    assert ev is not None
    assert "router" in (ev.concerns or "").lower()
