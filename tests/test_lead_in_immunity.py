"""LEAD-IN IMMUNITY (bar 1.3) — a conversational lead-in must not cost the fact behind it.

THE DEFECT, measured against the live ``/classify-intent`` before the fix:

    "small heads up for whenever it matters: my sable group meets on tuesdays"  → QUERY 0.99997
    "my sable group meets on tuesdays"                                          → STATEMENT 0.9997
    "random note for the books: my vetch count is eleven right now"             → QUERY 0.9974
    "quick one for the books: my widgeon's shelf number is 318"                 → QUERY 0.99991

The main clause is structurally IDENTICAL with and without the lead-in; only the verbless
fragment is added. So a fact-bearing DECLARATIVE bounced to the recall route, the fact never
landed, and a later correction had nothing to supersede. It was the dominant SILENT failure in
production drives — 5 of ~15 fact turns in one 15-minute window.

WHY NO DB LANE COULD FIX IT: every deterministic override in ``classify_intent`` lives under
``if confidence < user_gate_threshold``, and these misroutes come back at 0.997-0.9999, so the
override lanes are unreachable. The GLiNER2 intent labels are already concise and correct
(Pitfall 11) and are NOT the defect.

THE FIX, in two halves:
  L-A  ``peel_discourse_frame`` — grammar-only peel of a sentence-initial verbless discourse
       frame (dependency/POS/morphology; the delimiter set is PUNCTUATION, never vocabulary).
  L-B  consumed at the EXISTING declarative pre-route slot, admitting the peeled clause at >= 1.
  L-C  ``count_declarative_assertions`` widened to score an INTRANSITIVE finite predication —
       without it "my sable group meets on tuesdays" scores 0 even after peeling.

⚠️ THE ORDERING TRAP, pinned below by ``test_immunity_sits_below_the_correction_bypass_gate``:
a correction-framed lead-in ("small correction: …", "small update: actually, …") ALSO satisfies
the structural test, so the immunity MUST sit below the correction bypass gate and nowhere
earlier. If that test goes red, someone hoisted the block and corrections are being eaten.
"""
import re
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")  # spaCy/torch CUDA warnings are noise in the test env

from src.api.main import _spacy_cue_route  # noqa: E402
from src.extraction.linguistics import (  # noqa: E402
    count_declarative_assertions,
    linguistics_available,
    peel_discourse_frame,
)

spine = pytest.mark.skipif(
    not linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — the peel no-ops",
)

_MAIN_PY = Path(__file__).resolve().parents[1] / "src" / "api" / "main.py"


def _route(text):
    """The shipped decision ladder for the two declarative pre-route slots, verbatim.

    Returns one of: ``"STATEMENT(>=2)"``, ``"STATEMENT(lead-in)"``, ``"fallthrough"``.
    ``fallthrough`` means GLiNER2 decides — i.e. today's behaviour, unchanged.
    """
    n = count_declarative_assertions(text)
    if isinstance(n, int) and n >= 2:
        return "STATEMENT(>=2)"
    peeled = peel_discourse_frame(text)
    if peeled:
        cue, _tier = _spacy_cue_route(peeled)
        if cue is None:
            n_peeled = count_declarative_assertions(peeled)
            if isinstance(n_peeled, int) and n_peeled >= 1:
                return "STATEMENT(lead-in)"
    return "fallthrough"


# ── THE THREE MEASURED MISROUTES ──────────────────────────────────────────────────────
@spine
@pytest.mark.parametrize("text", [
    "small heads up for whenever it matters: my sable group meets on tuesdays",
    "random note for the books: my vetch count is eleven right now",
    "quick one for the books: my widgeon's shelf number is 318",
])
def test_a_lead_in_no_longer_costs_the_fact_behind_it(text):
    assert _route(text) == "STATEMENT(lead-in)", (
        "the lead-in bounced this fact-bearing declarative away from ingest again"
    )


@spine
@pytest.mark.parametrize("text", [
    "my sable group meets on tuesdays",
    "my vetch count is eleven right now",
    "my widgeon's shelf number is 318",
])
def test_the_bare_main_clause_is_unaffected(text):
    """The peel is for FRAMED turns only — a bare clause has no delimiter to peel."""
    assert peel_discourse_frame(text) is None


# ── THE RECALL SURFACE MUST BE UNTOUCHED ──────────────────────────────────────────────
@spine
@pytest.mark.parametrize("text", [
    # a framed QUESTION — the main span is interrogative
    "quick question for you: what is my shelf number",
    "one thing I keep forgetting: what day does my sable group meet",
    "random note for the books: what day does my sable group meet",
    "by the way, what's my shelf number",
    # a framed IMPERATIVE retraction — no overt subject, ROOT is VerbForm=Inf
    "one more thing: forget my dog",
    # a bare recall request — no delimiter at all
    "tell me about my family",
    "what is my shelf number",
    # a framed RETRIEVAL-DESIRE clause — its predicate takes a clausal complement, so
    # count_declarative_assertions' (c) branch vetoes it and it still scores 0
    "quick question: I want to know about my family",
])
def test_a_recall_turn_is_never_immunized(text):
    assert _route(text) == "fallthrough", (
        "a recall turn was routed to STATEMENT — the immunity has widened onto the read surface"
    )


@spine
def test_an_imperative_recall_still_scores_zero_after_the_widening():
    """L-C must not let imperatives in: they have no overt nsubj and are VerbForm=Inf."""
    for text in ("tell me about my family", "show me my pets", "list my krellins"):
        assert count_declarative_assertions(text) == 0


# ── THE ORDERING TRAP ─────────────────────────────────────────────────────────────────
@spine
def test_a_correction_cue_the_frame_was_hiding_declines_the_immunity():
    """The frame can HIDE a clause-initial repair marker from the correction gate.

    Measured: ``_spacy_cue_route`` sees NO cue on the raw turn (the marker is not
    sentence-initial once the frame is in front of it), but sees CORRECTION on the peeled
    clause. The immunity therefore re-runs the cue detector on the peel and declines, handing
    the turn back to the normal ladder instead of swallowing it as a STATEMENT.
    """
    text = "small update: actually, the bosterway moved to murvale"
    assert _spacy_cue_route(text) == (None, None), "raw-cue precondition changed — re-measure"
    peeled = peel_discourse_frame(text)
    assert peeled == "actually, the bosterway moved to murvale"
    assert _spacy_cue_route(peeled)[0] == "CORRECTION"
    assert _route(text) == "fallthrough"


@spine
@pytest.mark.parametrize("text", [
    "heads up: rex is no longer my pet",
    "quick note: forget that I have a dog",
])
def test_a_framed_removal_is_not_immunized(text):
    assert _route(text) == "fallthrough"


def test_immunity_sits_below_the_correction_bypass_gate():
    """SOURCE-ORDER pin — the trap the whole design turns on.

    A correction-framed lead-in satisfies the structural test, so the immunity must be reached
    only AFTER the correction bypass gate has had its say. This asserts the block order inside
    ``classify_intent``: correction gate → … → declarative pre-route → the immunity. If a future
    edit hoists the immunity above the gate, this goes red BEFORE a correction gets eaten.
    """
    src = _MAIN_PY.read_text(encoding="utf-8")
    start = src.index("async def classify_intent(")
    body = src[start:start + 40000]

    gate = body.index("PRE-GLINER2 CORRECTION BYPASS GATE")
    declarative = body.index("PRE-GLINER2 DECLARATIVE-STATEMENT ROUTE")
    immunity = body.index("LEAD-IN IMMUNITY (bar 1.3)")
    gliner = body.index("_build_intent_descriptions_for_gliner2()")

    assert gate < declarative < immunity < gliner, (
        "the lead-in immunity moved: it must run AFTER the correction bypass gate and "
        "inside/after the declarative pre-route, but still BEFORE GLiNER2"
    )


def test_the_immunity_is_wired_to_the_real_helpers():
    """A route disconnected from its helper would leave every behavioural test above green."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    start = src.index("PRE-GLINER2 DECLARATIVE-STATEMENT ROUTE")
    block = src[start:src.index("PRE-GLINER2 SOURCE→GOAL TRANSITION ROUTE", start)]

    assert "peel_discourse_frame as _peel_frame" in block
    assert "_peel_frame(text)" in block
    # first-touch-cold-path round 2: the decision now runs as ONE threaded unit
    # (`_decl_preroute_sync`, off the event loop); the peeled clause is `_mc` inside it.
    assert re.search(r"_spacy_cue_route\(_mc\)", block), (
        "the peeled-clause cue re-check is the belt that keeps a hidden correction out"
    )
    assert re.search(r"_np\s*>=\s*1", block)
    assert "asyncio.to_thread(_decl_preroute_sync)" in block


def test_the_rollback_lever_exists_and_defaults_on():
    """OFF must be byte-for-byte legacy; the codebase's flag convention is env-overridable."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    assert re.search(
        r'LEAD_IN_IMMUNITY = os\.getenv\("LEAD_IN_IMMUNITY", "true"\)', src)
    assert "if LEAD_IN_IMMUNITY:" in src  # inside `_decl_preroute_sync` (round 2 restructure)
