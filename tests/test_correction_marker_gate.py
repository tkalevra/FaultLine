# -*- coding: utf-8 -*-
"""THE CORRECTION MARKER GATE (BUG-RECALL-13 PART A, critic ctx R2-1 MED 2026-11-11).

THE MEASURED WOUND: the pg_dump REGISTER statement ("For restoreable dumps always pg_dump
with --clean --if-exists, otherwise the restore script fights the existing schema…") routes
into the CORRECTION lane on fresh seats — a deterministic GLiNER2 correction-label misroute
(reproduced on fastino/gliner2-base-v1 with the shipped D1 labels: CORRECTION@0.61, sub-gate)
→ remember_facts hands it to /retract/correct → status corrected_unresolved +
facts_superseded=0 (a FALSE correction story) and the register row never mints at the
statement lane.

THE FIX: a CLASSIFIER-born CORRECTION verdict (the GLiNER2 label, the intent_pattern_cache
override, the sub-gate LLM adjudication — measured live on the R2 seats to ride the SUB-GATE
arm at 0.61) must clear an EXPLICIT old-value/repair-marker floor before it routes: the
closed inventory ("changed to", "actually", "correction:", "not X but Y", "no longer",
"instead of", "now use"). A turn lacking ALL of them routes STATEMENT → ingest, where the
register arm mints and a genuine restate still supersedes via the seeded correction_behavior
shelves (the safe direction). The routes with their OWN explicit evidence are checked BEFORE
the gate and never consult it: the spaCy cue tiers (repair frame / marker lemma /
contrastive negation + LLM agreement), the negated-name preroute (a negated denial of a
stored PROPER-NAME value), and the neg-scope ensemble (structural negation scope).

LITMUS (the R2 critic's): the pg_dump sentence routes STATEMENT; "Correction: the port
changed to 8010" and "Actually we moved to 12 Birch Lane" still route CORRECTION.

Flag CORRECTION_MARKER_GATE (default ON); OFF → the helper admits every turn →
byte-for-byte pre-fix behavior. No database: POSTGRES_DSN is cleared (every DB-touching
layer fail-opens by design) — the same harness as tests/test_classify_skip_llm_flag.py.
"""
import asyncio
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

import src.api.main as M  # noqa: E402
import src.api.llm_calls as LC  # noqa: E402

os.environ.pop("POSTGRES_DSN", None)

_UID = "5d1c2b3a-1111-4000-8000-abcdefabcdef"

# THE MEASURED SENTENCE (BUG-RECALL-13; ctx_corpus.json command_flag class, byte-identical
# to the bench evidence and tests/test_instruction_register_arm.PGDUMP).
PGDUMP = ("For restoreable dumps always pg_dump with --clean --if-exists, otherwise the "
          "restore script fights the existing schema and dies halfway.")
# THE LITMUS TRUE CORRECTIONS (the R2 critic's two, verbatim).
CORRECTION_LABEL = "Correction: the port changed to 8010"
CORRECTION_ACTUALLY = "Actually we moved to 12 Birch Lane"


@pytest.fixture(autouse=True)
def _db_free(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", True)


async def _ready(user_id, endpoint):
    return None


class _GlinerStub:
    """Answers the shipped D1 DESCRIPTION (the mapping is description→intent — the same
    shape the real model returns, not the bare intent name)."""

    def __init__(self, label, confidence):
        self.desc = M._build_intent_descriptions_for_gliner2()[label]
        self.confidence = confidence

    def classify_text(self, text, labels, include_confidence=True):
        return {"intent": {"label": self.desc, "confidence": self.confidence}}


# ── A: the marker floor itself (deterministic, closed inventory) ──────────────────────────


def test_the_measured_register_sentence_carries_no_marker():
    # THE wound: a prescription with NO old-value/repair marker — the exact refusal class.
    assert M._turn_carries_correction_marker(PGDUMP) is False


@pytest.mark.parametrize("text", [
    CORRECTION_LABEL,            # "correction:" + "changed to"
    CORRECTION_ACTUALLY,         # "actually"
    "the port changed to 8010",          # "changed to"
    "she is 14 not 12 but reads 13",     # "not X but Y"
    "Rex is no longer my pet",           # "no longer"
    "we use postgres instead of sqlite",  # "instead of"
    "we now use the staging port 8010",  # "now use"
    "Actually my wife's name is Odile, not Rosalind.",  # R4-J end-anchored ", not <Name>"
])
def test_every_marker_class_is_admitted(text):
    assert M._turn_carries_correction_marker(text) is True


@pytest.mark.parametrize("text", [
    PGDUMP,
    "My favorite editor is vim",                 # the Pitfall-11 editor/usage class
    "I always run the smoke suite before a deploy",
    "My wife is Nora and we live in Cardiff",   # a plain statement (supersede rides the store)
])
def test_plain_statements_carry_no_marker(text):
    assert M._turn_carries_correction_marker(text) is False


def test_flag_off_admits_every_turn(monkeypatch):
    """OFF = byte-parity: no classifier verdict is ever refused."""
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", False)
    for text in (PGDUMP, CORRECTION_LABEL, "My favorite editor is vim"):
        assert M._turn_carries_correction_marker(text) is True


# ── B: the GLiNER2-native route (the measured misroute arm) ───────────────────────────────


async def test_the_measured_wound_routes_statement():
    """THE litmus: GLiNER2's CORRECTION label on the pg_dump prescription is refused — the
    register statement reaches the statement lane (where the register arm mints)."""
    resp = await M.classify_intent({"text": PGDUMP}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "STATEMENT"
    assert resp["confidence"] == pytest.approx(0.95)


async def test_a_marker_carrying_native_label_survives():
    """The same confident label on a turn WITH an explicit marker routes CORRECTION."""
    resp = await M.classify_intent({"text": "the port changed to 8010"}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "CORRECTION"


async def test_flag_off_restores_the_native_label(monkeypatch):
    """OFF = byte-parity: the raw label stands (pre-fix behavior)."""
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", False)
    resp = await M.classify_intent({"text": PGDUMP}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "CORRECTION"


# ── C: the sub-gate adjudication route (the arm the live 3/3 rode at 0.61) ────────────────


@pytest.fixture()
def adjudicator(monkeypatch):
    """The INTENT_ADJUDICATION LLM answers CORRECTION@0.9 (recorded)."""
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs.get("operation"))
        return {"route": "CORRECTION", "confidence": 0.9}

    monkeypatch.setattr(LC, "call_llm_with_retry_async", _record)
    return calls


async def test_subgate_adjudicated_correction_is_refused_without_a_marker(adjudicator):
    """A sub-gate CORRECTION guess corroborated CORRECTION still cannot route a turn that
    carries no old-value/repair marker — the fail-open default (STATEMENT → ingest) takes it."""
    resp = await M.classify_intent({"text": "she moved to Halifax"}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.55))
    assert adjudicator == ["INTENT_ADJUDICATION"], "the escalation must have been consulted"
    assert resp["intent"] == "STATEMENT"


async def test_subgate_adjudicated_correction_survives_with_a_marker(adjudicator):
    """The escalation mechanism itself is untouched: a marker-carrying turn routes on the
    adjudicator's verdict exactly as before (the updated skip-llm pin's shape)."""
    resp = await M.classify_intent({"text": "the port changed to 8010"}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.55))
    assert adjudicator == ["INTENT_ADJUDICATION"]
    assert resp["intent"] == "CORRECTION"


async def test_subgate_flag_off_restores_the_escalation_route(monkeypatch, adjudicator):
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", False)
    resp = await M.classify_intent({"text": "she moved to Halifax"}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.55))
    assert resp["intent"] == "CORRECTION"


# ── D: the evidence-carrying preroutes are ABOVE the gate (never consulted) ───────────────


async def test_the_label_frame_correction_routes_before_the_gate():
    """"Correction: …" is a NOMINAL repair frame — the spaCy cue TIER-1 answers CORRECTION
    deterministically, GLiNER2 and the marker gate never consulted."""
    resp = await M.classify_intent({"text": CORRECTION_LABEL}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert resp["intent"] == "CORRECTION"


async def test_the_actually_correction_routes_through_the_dual_gate(adjudicator):
    """"Actually we moved to 12 Birch Lane" — the clause-initial marker is an AMBIGUOUS cue;
    the LLM corroborates (INTENT_PRECLASSIFY) and the AGREEMENT routes CORRECTION — the
    pre-gate dual-gate, its own explicit evidence, the marker gate never consulted."""
    resp = await M.classify_intent({"text": CORRECTION_ACTUALLY}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert adjudicator == ["INTENT_PRECLASSIFY"], "the ambiguous cue must corroborate first"
    assert resp["intent"] == "CORRECTION"


def test_the_preroute_blocks_never_consult_the_gate():
    """SOURCE PIN: the gate is a floor for CLASSIFIER-born verdicts only. The cue tiers and
    the negated-name preroute sit ABOVE it; the neg-scope ensemble sits below the consult
    positions but carries none itself. None of the three evidence-carrying routes may consult
    the marker helper — their dependency-shape evidence IS the stronger marker.
    (ROUND-4 count: the deterministic-first router's TIER-2 possessive lane adds the FOURTH
    consult — an EXCLUSION, not a floor: a marker-carrying turn declines the lane and keeps
    this ladder, "My address changed to 99 Fake Street" must never be stolen from the
    correction path. The three classifier arms keep their exact gates.)"""
    src = inspect.getsource(M.classify_intent)
    gate_call = "_turn_carries_correction_marker(text)"
    assert src.count(gate_call) == 4, "the gate must guard exactly the three classifier arms"
    i_first_gate = src.find(gate_call)
    for marker in ("_cue_route, _cue_tier = await asyncio.to_thread",
                   "spacy_negated_name"):
        i = src.find(marker)
        assert 0 < i < i_first_gate, f"{marker!r} must sit above the marker gate"
    # The FOURTH consult is the tier-2 lane's exclusion — and the tier-2 lane itself must sit
    # BELOW the correction bypass gate (it re-derives the cue and declines on any cue hit).
    i_det_first = src.find("det_first_possessive")
    i_bypass = src.find("CORRECTION_BYPASS_GATE and user_id")
    assert 0 < i_bypass < i_det_first, "the tier-2 lane must sit below the correction gate"
