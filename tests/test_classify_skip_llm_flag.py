"""API SKIP-LLM TIER FLAG — /classify-intent's deterministic answer, on demand.

The API half of the MCP brain-independence lane (2026-10-25). The MCP bounds its classify
probe (MCP_CLASSIFY_TIMEOUT_S, default 3s) and, when the budget is exceeded on a lane that
still needs a route, re-asks once with ``skip_llm: true``. These pins prove the flag gates
EVERY LLM tier inside /classify-intent while the deterministic layers keep deciding:

  (tier-2)  an ambiguous spaCy cue with skip_llm  → NO LLM corroboration, no destructive
            bypass — falls through to GLiNER2 (the no-agreement path)
  (tier-2') the same cue WITHOUT the flag          → the LLM corroborates and the bypass
            fires (the legacy behavior is untouched)
  (sub-gate) a below-gate GLiNER2 label with skip_llm → NO escalation LLM, the sub-gate
            STATEMENT safety net answers (the documented escalation-failure default)
  (sub-gate') without the flag                     → the escalation LLM's route wins
  (ensemble) the negation-band adjudicator with skip_llm → NO 30s adjudication call, the
            adjudicator-unavailable fail-open answers (lone-signal rule, NO grow)
  (ensemble') without the flag                     → the adjudicator fires

No database: POSTGRES_DSN is cleared (every DB-touching layer fail-opens by design) and the
LLM stack is a recorder. The GLiNER2 model is a stub returning a fixed label/confidence.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.main as M
import src.api.llm_calls as LC


_UID = "5d1c2b3a-1111-4000-8000-abcdefabcdef"


@pytest.fixture(autouse=True)
def _db_free(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)


async def _ready(user_id, endpoint):
    return None


class _GlinerStub:
    """Returns a fixed GLiNER2-shaped answer: {"intent": {"label": ..., "confidence": ...}}."""

    def __init__(self, label, confidence):
        self.label = label
        self.confidence = confidence

    def classify_text(self, text, labels, include_confidence=True):
        return {"intent": {"label": self.label, "confidence": self.confidence}}


@pytest.fixture()
def llm_recorder(monkeypatch):
    """Record + answer every call_llm_with_retry_async the endpoint would make."""
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {"route": "CORRECTION", "confidence": 0.9}

    monkeypatch.setattr(LC, "call_llm_with_retry_async", _record)
    return calls


# ── TIER 2: ambiguous spaCy cue — corroboration skipped, no destructive bypass ──────


async def test_tier2_ambiguous_cue_skip_llm_arm_vs_legacy_arm(llm_recorder, monkeypatch):
    """Both arms, one test (the A/B control): the SAME ambiguous cue — with skip_llm no
    LLM corroboration (no destructive bypass), without it the LLM's verdict wins."""
    def _cue(text, user_id):          # SYNC — the endpoint calls it via asyncio.to_thread
        return ("CORRECTION", "ambiguous")
    monkeypatch.setattr(M, "_spacy_cue_route", _cue)

    resp = await M.classify_intent(
        {"text": "my pets are not part of my family", "skip_llm": True}, user_id=_UID,
        model=_GlinerStub("STATEMENT", 0.9))
    assert llm_recorder == [], "skip_llm must not corroborate an ambiguous cue with the LLM"
    assert resp["intent"] == "STATEMENT"  # no agreement → GLiNER2's gate-passed label stands

    resp_legacy = await M.classify_intent(
        {"text": "my pets are not part of my family"}, user_id=_UID,
        model=_GlinerStub("STATEMENT", 0.9))
    assert len(llm_recorder) == 1, llm_recorder          # exactly the legacy arm called
    assert resp_legacy["intent"] == "CORRECTION"          # the corroborated bypass fired
    assert resp_legacy["confidence"] == pytest.approx(0.9)


# ── SUB-GATE: below-gate GLiNER2 label — escalation skipped, STATEMENT net answers ───


async def test_subgate_skip_llm_takes_the_statement_net_without_escalating(llm_recorder):
    resp = await M.classify_intent(
        {"text": "she moved to Halifax", "skip_llm": True}, user_id=_UID,
        model=_GlinerStub("CORRECTION", 0.55))   # below the 0.70 default gate

    assert llm_recorder == [], "skip_llm must not run the sub-gate escalation LLM"
    assert resp["intent"] == "STATEMENT"          # the sub-gate safety net (fail-open)
    assert resp["confidence"] == pytest.approx(0.55)


async def test_subgate_without_flag_the_escalation_llm_routes(llm_recorder):
    # BUG-RECALL-13 PART A: the escalation routes a CORRECTION only for a turn carrying an
    # explicit old-value/repair marker — "the port changed to 8010" (no spaCy cue, so it
    # reaches the sub-gate; marker admits the escalated verdict). The no-marker companion
    # ("she moved to Halifax" → STATEMENT through the marker gate) is pinned in
    # tests/test_correction_marker_gate.py.
    resp = await M.classify_intent(
        {"text": "the port changed to 8010"}, user_id=_UID,
        model=_GlinerStub("CORRECTION", 0.55))

    assert len(llm_recorder) == 1, llm_recorder
    assert resp["intent"] == "CORRECTION"         # the escalated verdict routes


# ── NEG-ENSEMBLE: the 30s adjudicator — skipped, fail-open, no grow ──────────────────


def _lone_negscope():
    """One structural negation voter, no depwalk: the disagreement band (adjudicator zone)."""
    return [SimpleNamespace(predicates_nominal=True, two_referential_poles=False)]


async def test_ensemble_skip_llm_never_adjudicates_and_never_grows(llm_recorder, monkeypatch):
    grew = []
    monkeypatch.setattr(M, "_neg_ensemble_grow", lambda *a, **k: grew.append(a))

    resp = await M._neg_scope_ensemble_decide(
        _UID, "she is not home tonight", _lone_negscope(),
        gliner2_label="STATEMENT", gliner2_conf=0.9, skip_llm=True)

    assert llm_recorder == [], "skip_llm must not fire the 30s ensemble adjudicator"
    assert grew == [], "an adjudicator outage must not manufacture evidence (no grow)"
    assert resp["intent"] == "STATEMENT"          # fail-open: GLiNER2's label stands
    assert resp["confidence"] == pytest.approx(0.9)


async def test_ensemble_without_flag_the_adjudicator_fires(llm_recorder, monkeypatch):
    grew = []
    monkeypatch.setattr(M, "_neg_ensemble_grow", lambda *a, **k: grew.append(a))

    resp = await M._neg_scope_ensemble_decide(
        _UID, "she is not home tonight", _lone_negscope(),
        gliner2_label="STATEMENT", gliner2_conf=0.9, skip_llm=False)

    assert len(llm_recorder) == 1, llm_recorder
    assert resp["intent"] == "CORRECTION"         # the adjudicated verdict (0.85 convention)
    assert grew, "a successful CORRECTION adjudication grows the engine row"


# ── gate-passed turns: skip_llm changes nothing above the gate ───────────────────────


async def test_gate_passed_turn_is_identical_with_and_without_the_flag(llm_recorder):
    # The text is interrogative — the deterministic spaCy pre-route answers it BEFORE
    # GLiNER2, which is itself the pin: a deterministic-layer turn is flag-invariant.
    a = await M.classify_intent({"text": "what car do I drive"}, user_id=_UID,
                                model=_GlinerStub("QUERY", 0.93))
    b = await M.classify_intent({"text": "what car do I drive", "skip_llm": True},
                                user_id=_UID, model=_GlinerStub("QUERY", 0.93))
    assert a == b and a["intent"] == "QUERY"
    assert llm_recorder == []
