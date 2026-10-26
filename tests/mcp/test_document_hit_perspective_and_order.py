"""BUG-09 pins (adversarial bench 2026-10-24): document-hit perspective + band order.

Two renderer defects, both pinned here:

  1. PERSPECTIVE FLIP — on the private lineage the agent (de-self) voice rewrote the
     document-passage lines "From your document …" / "You have a document …" into
     "their"/"They have", because they rode ``assert_lines`` and ``deself_line`` maps
     you→they wholesale. The seat IS the document owner (the row lives in that seat's
     tenant schema), so the second person is ownership-correct in EVERY voice. On this
     FOSS lineage there is no voice-preset/de-self machinery at all — the pin here is
     that the second person survives the FULL recall assembly verbatim (and that the
     document band never routes through any rewriting helper), so the defect cannot
     be reintroduced by porting one.
  2. JUNK TOPLINE — staged/extracted facts (possibly B-tier debris) rendered ABOVE
     verbatim document passages. Passages are the highest-truth band (row-level fact,
     quoted from source) and must LEAD.

FOSS-port adaptation (2026-10-26): the private lane pins ``src.mcp.response_types``
(AGENT/PERSONAL presets, the de-self voice, the YOUR-MEMORY fence) — that module is
not part of the public core, so those preset-level pins are expressed here against
this lineage's inline assembly in ``_recall_memory_tool_impl`` instead: second person
survives, passages lead, doc hits count as content, and no doc-line rewrite exists
on the assembly path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.mcp.server import _render_document_hit_lines, recall_memory_tool
from tests.test_document_stub_tsv import _resp

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


# ── PIN 1: "You have a document" not "They have" — through the full assembly ───

def _wire(monkeypatch, query_payload):
    from src.mcp import server as s

    async def fake_post(url, json=None, timeout=None, headers=None):
        if url.endswith("/classify-intent"):
            return _resp({"intent": "QUERY", "confidence": 0.9})
        if url.endswith("/query"):
            return _resp(query_payload)
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


def test_second_person_survives_the_full_recall_assembly(monkeypatch):
    """THE BUG-09 PIN (FOSS shape): the document-passage lines keep the seat-owner's
    second person through the assembled memory. 'From their document' / 'They have a
    document' is the measured defect — this test FAILS if it ever appears."""
    _wire(monkeypatch, {
        "facts": [{"definition": "Your dog is Rex.", "fact_class": "A",
                   "fact_provenance": "user_stated", "confidence": 1.0}],
        "attributes": {},
        "document_hits": _DOC_HITS,
    })
    out = asyncio.run(recall_memory_tool("compressibility", "u-alice"))
    mem = out["memory"]
    assert "From your document" in mem
    assert "You have a document" in mem
    assert "their document" not in mem
    assert "They have" not in mem


# ── PIN 2: passages LEAD — before events, asserted facts, and held facts ───────

def test_document_passages_lead_every_fact_band(monkeypatch):
    """THE ORDER PIN: a verbatim passage renders ABOVE staged/extracted facts, so junk
    B-tier lines can never own the topline above verified source text."""
    _wire(monkeypatch, {
        "facts": [
            {"definition": "website fixes largest visual gap", "fact_class": "A",
             "fact_provenance": "user_stated", "confidence": 1.0},
            {"definition": "peng et al. created 111", "fact_class": "C",
             "fact_provenance": "llm_inferred", "confidence": 0.1},
            {"definition": "Something dated.", "fact_class": "A",
             "fact_provenance": "user_stated", "confidence": 1.0,
             "event_date": "2024-03-03T00:00:00Z"},
        ],
        "attributes": {},
        "temporal_ordered": True,
        "document_hits": _DOC_HITS,
    })
    out = asyncio.run(recall_memory_tool("everything", "u-alice"))
    mem = out["memory"]
    doc_pos = mem.index("From your document")
    for fact_marker in ("Event #1", "website fixes largest visual gap",
                        "peng et al. created 111"):
        assert doc_pos < mem.index(fact_marker), fact_marker


def test_no_doc_hits_renders_the_legacy_assembly(monkeypatch):
    """Back-compat: the document band is OPTIONAL — absent hits and empty hits render
    byte-identical memories (no empty-section artifact from the new band)."""
    facts = [{"definition": "Your dog is Rex.", "fact_class": "A",
              "fact_provenance": "user_stated", "confidence": 1.0}]

    _wire(monkeypatch, {"facts": facts, "attributes": {}, "document_hits": []})
    mem_empty_hits = asyncio.run(recall_memory_tool("dog", "u-alice"))["memory"]

    _wire(monkeypatch, {"facts": facts, "attributes": {}})  # key entirely absent
    mem_absent_hits = asyncio.run(recall_memory_tool("dog", "u-alice"))["memory"]

    assert mem_empty_hits == mem_absent_hits


# ── PIN: doc hits count as content — never an abstention over a found passage ──

def test_doc_hits_render_when_all_fact_lines_were_filtered(monkeypatch):
    """Regression pin for the band move: a recall whose facts ALL failed the
    confidence render but whose document lane hit MUST render the passage — the
    emptiness check treats doc hits as content exactly as it did when they rode
    assert_lines. An abstention on top of a found document is the failure mode."""
    _wire(monkeypatch, {
        # ONE fact whose provenance/confidence will land it in NO renderable band
        # (llm_inferred + low confidence → held… hold_lines DO render; to make
        # every band empty we need facts that produce nothing — empty definition
        # is skipped by the loop). Use a fact with an EMPTY definition: present
        # in `facts` (so the early facts-empty branch at the top does not fire)
        # but contributing no line.
        "facts": [{"definition": "", "fact_class": "B",
                   "fact_provenance": "llm_inferred",
                   "confidence": 0.1}],
        "attributes": {},
        "document_hits": [
            {"document_id": 18, "status": "ready", "chunk_index": 3,
             "chunk_text": "Quantization reduces model size.",
             "title": "On the Compressibility of Quantized LLMs"}],
    })
    out = asyncio.run(recall_memory_tool("compressibility", "u-alice"))
    mem = out["memory"]
    assert "From your document On the Compressibility of Quantized LLMs" in mem
    assert "Quantization reduces model size." in mem
    # never an abstention / empty-recall sentinel over a found passage
    assert "No relevant facts found" not in mem
    assert "have not told me" not in mem
