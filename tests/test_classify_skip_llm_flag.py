"""API SKIP-LLM TIER FLAG — /classify-intent's deterministic answer, on demand.

The API half of the MCP brain-independence lane (2026-10-25; FOSS port 2026-10-26 —
the neg-ensemble pins are private-lineage and dropped, see the note below). The MCP bounds its classify
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
    # FOSS rig note: LLMModels.get refuses to guess without a model env var — the
    # recorder intercepts the actual LLM call, so a dummy name is enough.
    monkeypatch.setenv("WGM_LLM_MODEL", "foss-port-test-model")
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)


async def _ready(user_id, endpoint):
    return None


class _GlinerStub:
    """Returns a fixed GLiNER2-shaped answer: {"intent": {"label": ..., "confidence": ...}}.

    FOSS-port note: this endpoint passes DESCRIPTION labels to classify_text and maps the
    returned description back to the intent name — the stub translates intent names to
    their descriptions so a stub built as ("QUERY", 0.93) scores as QUERY."""

    _DESC = {
        "QUERY": "asking a question",
        "STATEMENT": "stating a fact, preference, or habit",
        "RETRACTION": "deleting or forgetting information",
        "CORRECTION": "replacing a previously stated value with a different one",
    }

    def __init__(self, label, confidence):
        self.label = self._DESC.get(label, label)
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
    def _cue(text):                   # SYNC — the FOSS endpoint calls _spacy_cue_route(text)
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
    resp = await M.classify_intent(
        {"text": "she moved to Halifax"}, user_id=_UID,
        model=_GlinerStub("CORRECTION", 0.55))

    assert len(llm_recorder) == 1, llm_recorder
    assert resp["intent"] == "CORRECTION"         # the escalated verdict routes


# ── NEG-ENSEMBLE: NOT PORTED — the negation-band ensemble adjudicator is private-
#    lineage machinery (no _neg_scope_ensemble_decide / _neg_ensemble_grow on FOSS);
#    its skip_llm hunk has no target here. Filed DEV/BUGS/foss-port-gauntlets.md.

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
