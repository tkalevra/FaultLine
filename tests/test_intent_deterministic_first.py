# -*- coding: utf-8 -*-
"""THE DETERMINISTIC-FIRST INTENT ROUTER (LATENCY-INVESTIGATION-20261113, STREAM 1).

THE MEASURED WOUND: the intent pre-classification pays a z.ai round-trip on turns whose
surface already carries the route — INTENT_PRECLASSIFY on every ambiguous-cue correction
(the cue+LLM corroboration arm), INTENT_ADJUDICATION on every negation-band / sub-gate
turn — the measured 4-sequential-call chain per correction remember (1.95s + 1.43s of pure
intent latency on a 23s wall). The cue machinery already exists: CORRECTION_MARKER_GATE's
closed repair-marker inventory, the interrogative grammar (wh / inversion / "?"), the
declarative-assertion counter.

THE ROUTER (INTENT_DETERMINISTIC_FIRST, default ON; OFF → the always-LLM ladder
byte-for-byte): TIER 1 — an ambiguous spaCy cue on a turn carrying a STRUCTURAL repair
marker ("changed to" / "correction:" / "not X but Y" / "no longer" / "instead of" /
"now use" / the R4-J end-anchored ", not <Name>" appositive) routes CORRECTION with NO
LLM. TIER 2 — a declarative, non-negated, cue-free,
marker-free, SPEAKER-anchored assertion routes STATEMENT with NO GLiNER2 consult (the
below-gate INTENT_ADJUDICATION escalation it used to pay is gone). RESIDUAL — bare
"actually" without a structural member and contrastive negation without a marker keep the
LLM corroboration (this file's own ambiguous-tier example: "Actually I do like pizza"
could be a NEW statement); the negation bands keep the ensemble.

LITMUS: the measured turn shapes cost ZERO LLM intent calls and the verdicts match the
LLM's on the clear cases.

No database: POSTGRES_DSN is cleared (every DB-touching layer fail-opens by design) —
the same harness as tests/test_correction_marker_gate.py.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("WGM_LLM_MODEL", "qwen/qwen3.5-9b")

import src.api.main as M  # noqa: E402
import src.api.llm_calls as LC  # noqa: E402

os.environ.pop("POSTGRES_DSN", None)

_UID = "5d1c2b3a-2222-4000-8000-abcdefabcdef"

# THE MEASURED TURN SHAPES (the smokes + LATENCY-INVESTIGATION-20261113).
CORRECTION_MARKER_STRONG = "Actually we now use postgres 16 instead of 15"
CORRECTION_CHANGED_TO = "the port changed to 8010, not 9000"
CORRECTION_BARE_ACTUALLY = "Actually I do like pizza"
CORRECTION_CONTRAST_ONLY = "she is 14, not 12"
STATEMENT_POSSESSIVE = "My wife's name is Seraphine"
STATEMENT_PLAIN_FIRST = "I live at 12 fog lane"
STATEMENT_NEGATED = "My dog is not a poodle"
STATEMENT_THIRD_PARTY = "Her name is Mia"
QUERY_TERMINAL_Q = "where do I live?"
STATEMENT_MARKER_CUEBLIND = "My address changed to 99 Fake Street"
# R4-J (autopsy tools/regression_results/r4j_autopsy.md): the wife-rename correction shape —
# the end-anchored ", not <Name>" appositive is now a tier-1 structural member → ZERO LLM.
CORRECTION_APPOSITIVE_NAME = "Actually my wife's name is Odile, not Rosalind."
CORRECTION_APPOSITIVE_NAME_2 = "Actually my wife is Magnus, not Seren."


@pytest.fixture(autouse=True)
def _db_free(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setattr(M, "_ensure_tenant_ready", _ready)
    monkeypatch.setattr(M, "CORRECTION_MARKER_GATE", True)
    monkeypatch.setattr(M, "INTENT_DETERMINISTIC_FIRST", True)


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


class _GlinerBoom:
    """A GLiNER2 that MUST never be consulted — any consult fails the test."""

    def classify_text(self, text, labels, include_confidence=True):
        raise AssertionError("GLiNER2 consulted — the deterministic tier did not own the turn")


@pytest.fixture()
def corroboration_llm(monkeypatch):
    """The INTENT_PRECLASSIFY corroboration answers CORRECTION@0.9 (recorded)."""
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs.get("operation"))
        return {"route": "CORRECTION", "confidence": 0.9}

    monkeypatch.setattr(LC, "call_llm_with_retry_async", _record)
    return calls


@pytest.fixture()
def llm_boom(monkeypatch):
    """The LLM stack that MUST never be called — any intent round-trip fails the test."""

    async def _boom(**kwargs):
        raise AssertionError(f"LLM called with operation={kwargs.get('operation')!r} — the "
                             "deterministic tier did not own the turn")

    monkeypatch.setattr(LC, "call_llm_with_retry_async", _boom)
    return None


# ── A: the tier-1 marker evidence (the closed inventory, minus bare "actually") ──────────


@pytest.mark.parametrize("text", [
    CORRECTION_MARKER_STRONG,          # "now use" + "instead of"
    CORRECTION_CHANGED_TO,             # "changed to" + "not X but Y"
    "Correction: the port changed to 8010",   # "correction:" + "changed to"
    "Rex is no longer my pet",         # "no longer"
    "Oops, the address changed to 99 Fake Street",  # "changed to" behind a cue marker
    CORRECTION_APPOSITIVE_NAME,        # R4-J: end-anchored ", not <Name>" appositive
    CORRECTION_APPOSITIVE_NAME_2,      # R4-J: the Magnus/Seren kin-rename shape
    "my mother is named Imogen, not Rosalind.",  # R4-J: bare (cue-free) appositive tail
])
def test_structural_markers_are_strong(text):
    assert M._turn_strong_correction_marker(text) is True


@pytest.mark.parametrize("text", [
    CORRECTION_BARE_ACTUALLY,          # bare "actually" — the ambiguous residual
    "Actually we moved to 12 Birch Lane",     # bare "actually", no structural member
    CORRECTION_CONTRAST_ONLY,          # contrastive negation, no inventory member (NUMERIC tail — an appositive <Name> WOULD match, the residual is the digit class)
    "My wife's name is Seraphine",     # a plain statement
    "My favorite editor is vim",
])
def test_weak_or_absent_markers_are_not_strong(text):
    assert M._turn_strong_correction_marker(text) is False


# ── B: the tier-2 speaker-anchoring gate (morphology, no word list) ──────────────────────


@pytest.mark.parametrize("text", [
    STATEMENT_POSSESSIVE,              # "My" — Poss=Yes ∧ Person=1
    STATEMENT_PLAIN_FIRST,             # "I" — Person=1 ∧ PronType=Prs ∧ no Poss
    "We adopted our dog Biscuit in May 2023",  # "we" + "our"
])
def test_speaker_anchored_turns_anchor(text):
    assert M._turn_first_person_anchored(text) is True


@pytest.mark.parametrize("text", [
    STATEMENT_THIRD_PARTY,             # "Her" — 3rd person
    "The homelab hypervisor runs Proxmox",    # no first-person token at all
])
def test_non_speaker_turns_do_not_anchor(text):
    assert M._turn_first_person_anchored(text) is False


# ── C: TIER 1 — the ambiguous-cue marker route (ZERO LLM) ────────────────────────────────


async def test_ambiguous_cue_with_strong_marker_routes_without_llm(llm_boom):
    """THE tier-1 litmus: an ambiguous cue ('oops' opener) on a 'changed to' turn routes
    CORRECTION deterministically — INTENT_PRECLASSIFY never fires."""
    resp = await M.classify_intent(
        {"text": "Oops, the address changed to 99 Fake Street"}, user_id=_UID,
        model=_GlinerBoom())
    assert resp["intent"] == "CORRECTION"
    assert resp["confidence"] == pytest.approx(0.85)


async def test_r4j_wife_appositive_routes_correction_without_llm(llm_boom):
    """THE R4-J litmus (autopsy tools/regression_results/r4j_autopsy.md §2 fix 2): the
    s2 wife-leg correction "Actually my wife's name is Odile, not Rosalind." carries the
    bare-ambiguous 'actually' cue + the END-ANCHORED ', not <Name>' appositive → tier-1
    owns it → CORRECTION with ZERO LLM (it previously escaped the LLM only by brain-scope
    cache luck)."""
    resp = await M.classify_intent({"text": CORRECTION_APPOSITIVE_NAME}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "CORRECTION"
    assert resp["confidence"] == pytest.approx(0.85)


async def test_ambiguous_cue_bare_actually_keeps_the_llm(corroboration_llm):
    """THE ambiguous residual: a bare 'actually' (no structural member) keeps the LLM
    corroboration — the agreement path routes exactly as before."""
    resp = await M.classify_intent({"text": CORRECTION_BARE_ACTUALLY}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert corroboration_llm == ["INTENT_PRECLASSIFY"], "the residual must corroborate"
    assert resp["intent"] == "CORRECTION"


async def test_contrast_only_cue_keeps_the_llm(corroboration_llm):
    """A contrastive negation with no inventory member is the other residual — the LLM
    corroboration keeps it (never a lone deterministic route to the destructive lane)."""
    resp = await M.classify_intent({"text": CORRECTION_CONTRAST_ONLY}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert corroboration_llm == ["INTENT_PRECLASSIFY"]
    assert resp["intent"] == "CORRECTION"


async def test_tier1_flag_off_restores_the_corroboration(monkeypatch, corroboration_llm):
    """OFF = byte-parity: the strong-marker turn rides the LLM corroboration again."""
    monkeypatch.setattr(M, "INTENT_DETERMINISTIC_FIRST", False)
    resp = await M.classify_intent(
        {"text": "Oops, the address changed to 99 Fake Street"}, user_id=_UID,
        model=_GlinerStub("STATEMENT", 0.99))
    assert corroboration_llm == ["INTENT_PRECLASSIFY"]
    assert resp["intent"] == "CORRECTION"


# ── D: TIER 2 — the possessive-statement route (ZERO LLM, ZERO GLiNER2) ──────────────────


async def test_possessive_statement_routes_without_gliner_or_llm(llm_boom):
    """THE tier-2 litmus: a clear speaker-anchored statement routes STATEMENT — GLiNER2 and
    the sub-gate escalation never run."""
    resp = await M.classify_intent({"text": STATEMENT_POSSESSIVE}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"
    assert resp["confidence"] == pytest.approx(0.85)


async def test_plain_first_person_statement_routes(llm_boom):
    resp = await M.classify_intent({"text": STATEMENT_PLAIN_FIRST}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "STATEMENT"


async def test_negated_statement_declines_to_gliner():
    """THE negation scan-back contract: a negated turn is NEVER swallowed — GLiNER2 keeps it
    (the neg-scope ensemble owns the band)."""
    resp = await M.classify_intent({"text": STATEMENT_NEGATED}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert resp["intent"] == "STATEMENT"  # GLiNER2's verdict, not the det-first lane


async def test_third_party_statement_declines_to_gliner():
    """'Her name is Mia' is not speaker-anchored — today's path keeps it."""
    resp = await M.classify_intent({"text": STATEMENT_THIRD_PARTY}, user_id=_UID,
                                   model=_GlinerStub("STATEMENT", 0.99))
    assert resp["intent"] == "STATEMENT"


async def test_marker_carrying_statement_is_never_stolen(llm_boom):
    """THE load-bearing exclusion: 'My address changed to 99 Fake Street.' carries the
    'changed to' repair marker but fires NO clause-initial spaCy cue — the tier-2 lane must
    NOT steal it from the correction ladder. It routes GLiNER2's verdict exactly as today."""
    resp = await M.classify_intent({"text": STATEMENT_MARKER_CUEBLIND}, user_id=_UID,
                                   model=_GlinerStub("CORRECTION", 0.95))
    assert resp["intent"] == "CORRECTION"


async def test_correction_cued_turn_is_never_stolen(corroboration_llm):
    """A correction-cued turn ('Actually we moved to 12 Birch Lane') keeps the dual-gate —
    the tier-2 lane never fires behind a cue."""
    resp = await M.classify_intent({"text": "Actually we moved to 12 Birch Lane"},
                                   user_id=_UID, model=_GlinerStub("STATEMENT", 0.99))
    assert corroboration_llm == ["INTENT_PRECLASSIFY"]
    assert resp["intent"] == "CORRECTION"


async def test_tier2_flag_off_restores_gliner(monkeypatch):
    """OFF = byte-parity: the possessive statement rides GLiNER2 again."""
    monkeypatch.setattr(M, "INTENT_DETERMINISTIC_FIRST", False)
    consulted = []

    class _Stub:
        def classify_text(self, text, labels, include_confidence=True):
            consulted.append(text)
            return {"intent": {"label": M._build_intent_descriptions_for_gliner2()["STATEMENT"],
                               "confidence": 0.99}}

    resp = await M.classify_intent({"text": STATEMENT_POSSESSIVE}, user_id=_UID, model=_Stub())
    assert consulted, "OFF must consult GLiNER2 (byte-parity)"
    assert resp["intent"] == "STATEMENT"


# ── E: the QUERY tier (the "?" terminal — owned above, unchanged, still zero-LLM) ────────


async def test_terminal_question_routes_query_without_llm(llm_boom):
    resp = await M.classify_intent({"text": QUERY_TERMINAL_Q}, user_id=_UID,
                                   model=_GlinerBoom())
    assert resp["intent"] == "QUERY"


# ── F: the verdict-parity battery (the measured shapes → the LLM's own verdicts) ─────────


@pytest.mark.parametrize("text,expected", [
    ("My wife's name is Seraphine", "STATEMENT"),
    ("Our dog Biscuit is a corgi", "STATEMENT"),
    ("I live at 12 fog lane", "STATEMENT"),
    ("where do I live?", "QUERY"),
    ("what's my wife's name?", "QUERY"),
    ("Oops, the address changed to 99 Fake Street", "CORRECTION"),
])
async def test_clear_shape_verdicts(text, expected, llm_boom):
    """The routing verdicts on the clear cases — each fires ZERO LLM intent calls
    (llm_boom fails the test on any round-trip) and lands the shape's intended route."""
    resp = await M.classify_intent({"text": text}, user_id=_UID, model=_GlinerBoom())
    assert resp["intent"] == expected


# ── G: observability — the deciding layer is recorded ────────────────────────────────────


async def test_layer_stats_record_the_det_first_layers(llm_boom):
    # intent_layer_stats() returns the LIVE dict — snapshot a COPY or the delta reads zero.
    _before = dict(M.intent_layer_stats()["per_user"].get(_UID[:8], {}))
    await M.classify_intent({"text": "Oops, the address changed to 99 Fake Street"},
                            user_id=_UID, model=_GlinerBoom())
    await M.classify_intent({"text": STATEMENT_POSSESSIVE}, user_id=_UID, model=_GlinerBoom())
    stats = M.intent_layer_stats()["per_user"][_UID[:8]]
    # The counters are process-local and accumulate across the session — assert the DELTA.
    assert stats.get("det_first_marker", 0) - _before.get("det_first_marker", 0) == 1
    assert stats.get("det_first_possessive", 0) - _before.get("det_first_possessive", 0) == 1
