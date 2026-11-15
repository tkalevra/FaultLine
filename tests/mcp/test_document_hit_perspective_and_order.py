"""BUG-09 pins (adversarial bench 2026-10-24): document-hit perspective + band order.

Two renderer defects, both pinned here:

  1. PERSPECTIVE FLIP — the agent (de-self) voice rewrote the document-passage lines
     "From your document …" / "You have a document …" into "their"/"They have", because
     they rode ``assert_lines`` and ``deself_line`` maps you→they wholesale. The seat IS
     the document owner (the row lives in that seat's tenant schema), so the second
     person is ownership-correct in EVERY voice; the de-self exists to neutralize
     USER-perspective self-anchoring in recalled facts, not to re-assign library
     ownership to a third party.
  2. JUNK TOPLINE — staged/extracted facts (possibly B-tier debris) rendered ABOVE
     verbatim document passages. Passages are the highest-truth band (row-level fact,
     quoted from source) and must LEAD.

Run with:
  python3 tools/fltest.py --bug BUG09 --test tests/mcp/test_document_hit_perspective_and_order.py
"""

from src.mcp.response_types import AGENT, PERSONAL, render
from src.mcp.server import _render_document_hit_lines

_DOC_HITS = [
    {
        "title": "On the Compressibility of Quantized Large Language Models",
        "document_id": 18,
        "chunk_text": "Quantization reduces model size while maintaining performance.",
        "chunk_index": 2,
        "status": "ready",
    },
    {   # title-only discovery row (no chunk matched)
        "title": "Analysis of storage quality of crayfish meat",
        "document_id": 20,
        "status": "ready",
    },
]


# ── the renderer shapes (second person, both line kinds) ───────────────────────

def test_document_hit_lines_are_second_person_shapes():
    lines = _render_document_hit_lines(_DOC_HITS)
    assert lines[0].startswith("From your document On the Compressibility")
    assert "(document id 18, section 3)" in lines[0]
    assert lines[1].startswith("You have a document: Analysis of storage quality")


# ── PIN 1: "You have a document" not "They have" — in EVERY voice ─────────────

def test_agent_voice_never_deselfs_document_lines():
    """THE BUG-09 PIN: the de-self voice keeps the seat-owner's second person on
    document hits. 'From their document' / 'They have a document' is the measured
    defect — this test FAILS if it ever comes back."""
    out = render(
        AGENT,
        event_lines=[],
        assert_lines=[],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC_HITS),
    )
    assert "From your document" in out
    assert "You have a document" in out
    assert "their document" not in out
    assert "They have" not in out


def test_agent_voice_still_deselfs_fact_bands():
    """The firewall behavior is PRESERVED: user-perspective fact lines still lose the
    'you' anchor under the agent voice (facts as standalone facts). The document-line
    exemption must not leak into the fact bands."""
    out = render(
        AGENT,
        event_lines=[],
        assert_lines=["You own a bike and your dog is Rex."],
        hold_lines=[],
        document_lines=[],
    )
    assert "You own" not in out and "your dog" not in out
    assert "They own a bike" in out and "their dog" in out


def test_personal_voice_document_lines_unchanged():
    out = render(
        PERSONAL,
        event_lines=[],
        assert_lines=["Your car is blue."],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC_HITS),
    )
    assert "From your document" in out
    assert "You have a document" in out
    assert "Your car is blue." in out  # facts untouched in the default voice


# ── PIN 2 (MOVED 2026-11-07, WITH EVIDENCE): citations TRAIL the fact bands ──────
# The fog-family lab (DEV/FINDINGS-fog-family-lab-20261107.md, findings F2/F3) measured
# the OLD lead order hijacking personal recall on a poisoned document seat: "who is my
# wife?" LED with the ingested paper's worked example ("Bartholomew's wife Clementine…")
# and "tell me about my family" OPENED with "You have a document: <title>". The house
# doctrine "provenance is a SORT KEY, not decoration" ranks the user's stated fact above
# document provenance, so the document band now trails the fact bands
# (RECALL_DOCUMENT_TRAIL, default ON) and renders alone only when no fact band spoke.
# Grounding: attribution placed ABOVE an answer captures the attention that was the
# answer's (PMC13513764); retrieved chunks are grounding material, not the answer
# (Azure RAG prompt engineering). The OLD pin this replaces certified the lead order.

def test_document_lines_trail_every_fact_band():
    """THE ORDER PIN (fog-family lab F2/F3): with any fact band present, the document
    band renders AFTER events, asserted, and held lines — the answer leads, its
    citations trail, and a user fact is never displaced from the topline."""
    out = render(
        None,  # default voice — ordering is voice-independent
        event_lines=["Event #1 [2024-03-03]: Something dated."],
        assert_lines=["website fixes largest visual gap"],
        hold_lines=["peng et al. created 111"],
        document_lines=_render_document_hit_lines(_DOC_HITS),
    )
    doc_pos = out.index("From your document")
    for fact_marker in ("Event #1", "website fixes largest visual gap",
                        "peng et al. created 111"):
        assert doc_pos > out.index(fact_marker), fact_marker


def test_document_lines_lead_when_no_facts_spoke():
    """THE CERTIFIED SHAPE PRESERVED: with empty fact bands the document band renders
    alone — the migration-299 doc-only answer ("do I have anything about Y"), unchanged
    by the trail flip (there is nothing to trail behind)."""

    out = render(
        None,
        event_lines=[], assert_lines=[], hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC_HITS),
    )
    assert out.index("From your document") < out.index("You have a document")


def test_legacy_lead_order_restorable_by_flag(monkeypatch):
    """RECALL_DOCUMENT_TRAIL=false restores the BUG-09 documents-first assembly
    byte-for-byte — the env-off rollback lever for the trail flip."""
    monkeypatch.setenv("RECALL_DOCUMENT_TRAIL", "false")
    out = render(
        None,
        event_lines=["Event #1 [2024-03-03]: Something dated."],
        assert_lines=["website fixes largest visual gap"],
        hold_lines=["peng et al. created 111"],
        document_lines=_render_document_hit_lines(_DOC_HITS),
    )
    doc_pos = out.index("From your document")
    for fact_marker in ("Event #1", "website fixes largest visual gap"):
        assert doc_pos < out.index(fact_marker), fact_marker


def test_document_lines_inside_your_memory_fence():
    """Document passages ARE memory (the user's own library); with the operational
    block present they must sit inside the YOUR MEMORY fence, not after it."""
    out = render(
        AGENT,
        event_lines=[],
        assert_lines=["A fact."],
        hold_lines=[],
        document_lines=["You have a document: Some Paper (document id 7)."],
        operational_lines=["a cortex note"],
    )
    assert out.index("=== YOUR MEMORY ===") < out.index("You have a document")
    assert out.index("You have a document") < out.index("=== HOW YOU OPERATE ===")


def test_render_without_document_lines_is_byte_identical_legacy():
    """Back-compat: the new band is OPTIONAL — no document lines, byte-for-byte the
    pre-feature assembly (assert then hold)."""
    a = render(None, event_lines=[], assert_lines=["X is Y."],
               hold_lines=["Z might be W."])
    b = render(None, event_lines=[], assert_lines=["X is Y."],
               hold_lines=["Z might be W."], document_lines=[])
    assert a == b


# ── PIN: doc hits count as content — never an abstention over a found passage ──

def test_doc_hits_render_when_all_fact_lines_were_filtered(monkeypatch):
    """Regression pin for the band move: a recall whose facts ALL failed the
    confidence render but whose document lane hit MUST render the passage — the
    emptiness check treats doc hits as content exactly as it did when they rode
    assert_lines. An abstention on top of a found document is the failure mode."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from tests.test_document_stub_tsv import _resp
    from src.mcp import server as s
    from src.mcp.server import recall_memory_tool

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/query"):
            # ONE fact whose provenance/confidence will land it in NO renderable band
            # (llm_inferred + low confidence → held… hold_lines DO render; to make
            # every band empty we need facts that produce nothing — empty definition
            # is skipped by the loop). Use a fact with an EMPTY definition: present
            # in `facts` (so the early facts-empty branch at the top does not fire)
            # but contributing no line.
            return _resp({"facts": [{"definition": "", "fact_class": "B",
                                     "fact_provenance": "llm_inferred",
                                     "confidence": 0.1}],
                          "attributes": {},
                          "document_hits": [
                              {"document_id": 18, "status": "ready", "chunk_index": 3,
                               "chunk_text": "Quantization reduces model size.",
                               "title": "On the Compressibility of Quantized LLMs"}]})
        raise AssertionError(url)

    async def fake_get(url, params=None, timeout=None):
        if "/confidence-gate/" in url:
            return _resp({"threshold": 0.70})
        if url.endswith("/documents/pending"):
            return _resp({"pending": False, "count": 0})
        raise AssertionError(url)

    monkeypatch.setattr(s, "_http_client",
                        MagicMock(post=AsyncMock(side_effect=fake_post),
                                  get=AsyncMock(side_effect=fake_get)))
    monkeypatch.setattr(s, "RECALL_INTENT_ROUTING", False, raising=False)
    out = asyncio.run(recall_memory_tool("compressibility", "u-alice"))
    mem = out["memory"]
    assert "From your document On the Compressibility of Quantized LLMs" in mem
    assert "Quantization reduces model size." in mem
    # never an abstention / empty-recall sentinel over a found passage
    assert "No relevant facts found" not in mem
    assert "have not told me" not in mem
