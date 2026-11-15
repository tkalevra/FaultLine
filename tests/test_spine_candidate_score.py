"""Unit tests for STAGE 1 — the GROUNDED, NON-COMMITTING per-candidate-edge spine score.

PURE tests — no DB, no network, no GLiNER2, no LLM. They exercise the deterministic scorer that
assembles a grounded confidence breakdown for a spine participation candidate, behind
SPINE_CANDIDATE_SCORE (default ON). The score COMMITS NOTHING — it only computes + logs WHY.

What is asserted (each signal is a PROVENANCE OF A REAL SIGNAL — no fabricated constant):
  * _spine_typefit_grade        — exact-member→strong, concrete-incompatible→crater, any/unknown→neutral.
  * _spine_candidate_score      — the four-signal assembly:
        - GPS (argument-promoted bland head, possessive device-part) → type-fit CRATER +
          construction WEAK → grade LOW (mirrors the Stage-0 veto, commits nothing).
        - clean reified occurrence (webinar / surgery, object_type=Event) → type-fit STRONG +
          construction STRONG → grade HIGH.
        - graceful degrade: with no schema bound the Stage-3 cue probe degrades + FLAGS, never
          hardcodes a noun list.
        - subject-agnostic: a router/printer mismatch behaves identically to GPS (no domain literal).
        - conservative default: an ungroundable component never invents a number.

_rel_meta is monkeypatched to the canonical participated_in tail_types ({Concept}) so the tests are
DB-free and deterministic. No model required.

Run: python3 -m pytest tests/test_spine_candidate_score.py -q   (tests/ is gitignored → git add -f)
"""
import os

import pytest

os.environ.pop("POSTGRES_DSN", None)
os.environ.setdefault("SPINE_CANDIDATE_SCORE", "true")

import src.api.main as m  # noqa: E402


class _FakeEv:
    """Stand-in for linguistics.EventAnalysis (the construction-signal source)."""
    def __init__(self, event=None, title=None, concerns=None, negated=False):
        self.event = event
        self.title = title
        self.concerns = concerns
        self.negated = negated
        self.anchors = ()


@pytest.fixture(autouse=True)
def _stub_rel_meta(monkeypatch):
    """participated_in is constrained tail={Concept}, head={Person} (the canonical seed).
    No schema bound → cue probe degrades (probe_ok False)."""
    def _fake_rel_meta(rel_type=None):
        meta = {
            "participated_in": {"tail_types": ["Concept"], "head_types": ["Person"]},
        }
        if rel_type is None:
            return meta
        return meta.get(rel_type, {})
    monkeypatch.setattr(m, "_rel_meta", _fake_rel_meta)
    # No tenant schema bound → the Stage-3 cue probe returns (absent, probe-failed) → degrade.
    monkeypatch.setattr(m.rel_type_overlay, "get_current_schema", lambda: None)


# ── _spine_typefit_grade — the graded re-expression of the Stage-0 verdict ──────────────────────

def test_typefit_exact_member_strong():
    grade, reason = m._spine_typefit_grade("Concept", ["Concept"], "tail")
    assert grade == "strong"
    assert "exact_member" in reason


def test_typefit_concrete_incompatible_craters():
    grade, reason = m._spine_typefit_grade("Object", ["Concept"], "tail")
    assert grade == "crater"
    assert "concrete_incompatible" in reason


def test_typefit_unconstrained_neutral():
    grade, reason = m._spine_typefit_grade("Object", ["ANY"], "tail")
    assert grade == "neutral"
    assert "unconstrained" in reason


def test_typefit_unknown_type_neutral_failsafe():
    grade, reason = m._spine_typefit_grade(None, ["Concept"], "tail")
    assert grade == "neutral"
    assert "type_unknown" in reason
    grade2, _ = m._spine_typefit_grade("unknown", ["Concept"], "tail")
    assert grade2 == "neutral"


# ── _spine_candidate_score — the four-signal assembly ───────────────────────────────────────────

def test_gps_argument_promoted_logs_low_grade():
    """'I had an issue with my car's GPS system' → flat-promoted bland head, possessive device-part.
    type-fit craters (independent Object type) + construction weak → grade LOW. Commits nothing."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "gps system",
                 "subject_type": "Person", "object_type": "Event"}
    ev = _FakeEv(event="issue", title=None, concerns="my car's gps system")
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=True, lane="flat")
    assert bd is not None
    # type-fit reads the INDEPENDENT structural type (Object), not the lane's self-asserted Event.
    assert bd["type_fit"]["object_type"] == "Object"
    assert bd["type_fit"]["object_type_src"].startswith("independent_structural")
    assert bd["type_fit"]["grade"] == "crater"
    assert bd["construction"] == "weak_lvc_bland_head"
    assert bd["part_reinforced"] is True
    # assembled grade is the most conservative component → crater (a LOW grade).
    assert bd["grade"] == "crater"


def test_webinar_clean_occurrence_logs_high_grade():
    """'I attended a webinar' → clean reified occurrence (object_type=Event) → type-fit STRONG +
    construction STRONG → grade HIGH."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "webinar-handle",
                 "subject_type": "Person", "object_type": "Concept"}
    ev = _FakeEv(event="webinar", title=None, concerns=None)
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=False, lane="reified")
    assert bd is not None
    assert bd["type_fit"]["grade"] == "strong"
    assert bd["type_fit"]["object_type_src"] == "lane_asserted"
    assert bd["construction"] == "clean_eventive_head"
    assert bd["part_reinforced"] is False
    assert bd["grade"] == "strong"


def test_surgery_clean_occurrence_logs_high_grade():
    """'I had surgery last week' → clean reified occurrence, no with-PP argument → STRONG."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "surgery-handle",
                 "subject_type": "Person", "object_type": "Concept"}
    ev = _FakeEv(event="surgery", title=None, concerns=None)
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=False, lane="reified")
    assert bd is not None
    assert bd["construction"] == "clean_eventive_head"
    assert bd["grade"] == "strong"


def test_clean_eventive_head_on_flat_lane_is_strong():
    """A clean eventive head that did NOT mint a stable handle still goes through the FLAT lane
    ('attended a webinar' → bare 'webinar'). With an EventAnalysis eventive head and NO
    argument-promotion, construction is clean → STRONG (the emit SHAPE doesn't down-grade it)."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "webinar",
                 "subject_type": "Person", "object_type": "Concept"}
    ev = _FakeEv(event="webinar", title=None, concerns=None)
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=False, lane="flat")
    assert bd is not None
    assert bd["construction"] == "clean_eventive_head"
    assert bd["type_fit"]["object_type_src"] == "lane_asserted"
    assert bd["grade"] == "strong"


def test_no_eventanalysis_flat_is_neutral_construction():
    """A flat participation edge with NO EventAnalysis behind it (a non-event lane) has no
    construction evidence → neutral construction, never an invented strong/weak."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "thing",
                 "subject_type": "Person", "object_type": "Concept"}
    bd = m._spine_candidate_score(candidate, None, promoted_arg=False, lane="flat")
    assert bd is not None
    assert bd["construction"] == "bare_eventive_head"


def test_cue_class_absent_degrades_and_flags():
    """The Stage-3 problem/state cue class is not seeded yet → the scorer DEGRADES GRACEFULLY and
    FLAGS the dependency (no hardcoded noun list)."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "gps system",
                 "subject_type": "Person", "object_type": "Event"}
    ev = _FakeEv(event="issue", concerns="my car's gps system")
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=True, lane="flat")
    assert bd is not None
    # No schema bound → probe unavailable → degraded + flagged, parse-signal only.
    assert bd["cue"] in ("cue_probe_unavailable_degraded", "cue_class_absent_stage3_pending")


def test_subject_agnostic_router_mismatch_logs_low():
    """A second domain ('trouble with my router') behaves IDENTICALLY to GPS — no domain literal."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "router",
                 "subject_type": "Person", "object_type": "Event"}
    ev = _FakeEv(event="trouble", concerns="my router")
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=True, lane="flat")
    assert bd is not None
    assert bd["type_fit"]["grade"] == "crater"
    assert bd["construction"] == "weak_lvc_bland_head"
    assert bd["grade"] == "crater"


def test_subject_agnostic_second_clean_event_logs_high():
    """A second clean event ('attended a conference') logs HIGH — no domain literal in the diff."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "conference-handle",
                 "subject_type": "Person", "object_type": "Concept"}
    ev = _FakeEv(event="conference", title=None, concerns=None)
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=False, lane="reified")
    assert bd is not None
    assert bd["grade"] == "strong"


def test_non_participation_candidate_skipped():
    """The scorer targets the participation candidate (the Stage-0 veto target) only."""
    candidate = {"subject": "user", "rel_type": "owns", "object": "car",
                 "subject_type": "Person", "object_type": "Object"}
    assert m._spine_candidate_score(candidate, None, lane="flat") is None


def test_flag_off_returns_none(monkeypatch):
    """SPINE_CANDIDATE_SCORE OFF → no compute, no log, returns None (non-committing either way)."""
    monkeypatch.setenv("SPINE_CANDIDATE_SCORE", "false")
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "webinar",
                 "subject_type": "Person", "object_type": "Concept"}
    assert m._spine_candidate_score(candidate, _FakeEv(event="webinar"), lane="reified") is None


def test_promoted_arg_without_possessive_still_overrides_via_with_pp():
    """A bland-head promotion whose argument has NO detectable possessive marker is STILL a with-PP-
    promoted subject-matter (a concrete non-event entity), so the lane's 'Event' is overridden to the
    independent 'Object' type via the with_pp_promotion signal, and construction is WEAK → grade LOW.
    Grounded in the with-PP-promotion PARSE signal, not a fabricated constant."""
    candidate = {"subject": "user", "rel_type": "participated_in", "object": "deadline",
                 "subject_type": "Person", "object_type": "Event"}
    ev = _FakeEv(event="problem", concerns="the deadline")  # no 's / no 'my' → not part-reinforced
    bd = m._spine_candidate_score(candidate, ev, promoted_arg=True, lane="flat")
    assert bd is not None
    assert bd["part_reinforced"] is False
    assert bd["type_fit"]["object_type"] == "Object"
    assert bd["type_fit"]["object_type_src"] == "independent_structural(with_pp_promotion)"
    assert bd["construction"] == "weak_lvc_bland_head"
    # type-fit Object ∉ {Concept} = crater; construction weak. Grade LOW.
    assert bd["type_fit"]["grade"] == "crater"
    assert bd["grade"] == "crater"
