"""DOCCAP — turn-aware document chunking + deterministic context propagation.

Pins the contract of `src/ingest/document_structure.py` and of the two flags that
consume it in `src/mcp/server.py::_chunk_document`:

  DOC_TURN_AWARE_CHUNKING        — a TRANSCRIPT is chunked one-chunk-per-speaker-turn
  DOC_CHUNK_CONTEXT_PROPAGATION  — the legacy chunker's boundaries inherit the running
                                   "[Date: …]" header and "user:"/"assistant:" speaker

Both must be NO-OPS for ordinary prose, and OFF must reproduce the legacy chunker
exactly. Everything here is deterministic: no LLM, no DB, no network.
"""
import importlib
import os

import pytest


def _server(turn_aware, propagate):
    """Import src.mcp.server with the two flags set. Reloaded so module-level flags bind."""
    os.environ["DOC_TURN_AWARE_CHUNKING"] = "true" if turn_aware else "false"
    os.environ["DOC_CHUNK_CONTEXT_PROPAGATION"] = "true" if propagate else "false"
    import src.mcp.server as S
    return importlib.reload(S)


TRANSCRIPT = (
    "[Date: 2023/04/10 (Mon) 17:50]\n"
    "user: I got my car serviced for the first time on March 15th. It went well.\n"
    "\n"
    "assistant: Glad to hear it! Regular service really does keep a car healthy.\n"
    "\n"
    "user: I had an issue with my GPS system on 3/22. They replaced the whole unit.\n"
    "\n"
    "[Date: 2023/05/02 (Tue) 09:12]\n"
    "user: I finally booked the trip to San Francisco. It leaves in June.\n"
)

PROSE = (
    "Photosynthesis converts carbon dioxide and water into glucose and oxygen. "
    "It happens in the chloroplast.\n"
    "\n"
    "The Calvin cycle fixes carbon. It does not require light directly.\n"
)


# ── the shared structural module ────────────────────────────────────────────────
def test_has_transcript_structure_only_for_role_lines():
    from src.ingest import document_structure as D
    assert D.has_transcript_structure(TRANSCRIPT) is True
    assert D.has_transcript_structure(PROSE) is False
    # a bare header is NOT a transcript — no speaker to attribute anything to
    assert D.has_transcript_structure("[Date: 2023-01-01]\nsome text") is False


def test_split_structural_blocks_keeps_headers_and_turns_ordered():
    from src.ingest import document_structure as D
    blocks = D.split_structural_blocks(TRANSCRIPT)
    kinds = [b[0] for b in blocks]
    assert kinds == ["header", "turn", "turn", "turn", "header", "turn"]
    assert blocks[0][2] == "[Date: 2023/04/10 (Mon) 17:50]"
    assert blocks[1][1] == "user"
    assert blocks[2][1] == "assistant"
    # nothing is dropped: every turn body keeps its own role line verbatim
    assert blocks[2][2].startswith("assistant: Glad to hear it!")


def test_propagate_chunk_context_carries_header_and_speaker():
    from src.ingest import document_structure as D
    chunks = [
        "[Date: 2023/04/10 (Mon) 17:50]\nuser: I got my car serviced on March 15th.",
        "It went well and I was happy with the dealership.",   # orphan continuation
        "assistant: Glad to hear it!",
    ]
    out = D.propagate_chunk_context(chunks)
    assert out[0] == chunks[0], "a chunk that already has its context is untouched"
    # the orphan inherits BOTH the running header and the running speaker
    assert out[1].startswith("[Date: 2023/04/10 (Mon) 17:50]\nuser: It went well")
    # a chunk with its own speaker keeps it, and still inherits the header
    assert out[2].startswith("[Date: 2023/04/10 (Mon) 17:50]\nassistant: Glad to hear it!")


def test_propagate_chunk_context_is_a_noop_for_prose():
    from src.ingest import document_structure as D
    chunks = ["Photosynthesis converts carbon dioxide and water.",
              "It happens in the chloroplast."]
    assert D.propagate_chunk_context(chunks) == chunks


# ── the flags, at the product seam ──────────────────────────────────────────────
def test_flags_off_reproduce_the_legacy_chunker_exactly():
    """OFF is the legacy paragraph chunker: no header carried, orphan continuations kept."""
    S = _server(turn_aware=False, propagate=False)
    chunks = S._chunk_document(TRANSCRIPT)
    # the legacy rule merges the lone header line forward into the first turn and leaves
    # every later chunk without one — that is exactly the defect this work measured.
    assert sum(1 for c in chunks if "[Date:" in c) < len(chunks)
    assert any(not c.lstrip().startswith(("user:", "assistant:", "[Date:")) for c in chunks) \
        or len(chunks) == len([c for c in chunks if c])


def test_turn_aware_emits_one_chunk_per_turn_each_carrying_its_context():
    S = _server(turn_aware=True, propagate=True)
    chunks = S._chunk_document(TRANSCRIPT)
    assert len(chunks) == 4, chunks           # 4 turns, 2 sessions
    for c in chunks:
        assert c.startswith("[Date: "), c     # every chunk is date-grounded
        body = c.split("\n", 1)[1]
        assert body.startswith(("user:", "assistant:")), c   # and speaker-attributed
    # the second session's turn carries the SECOND header, not the first
    assert chunks[3].startswith("[Date: 2023/05/02 (Tue) 09:12]")
    assert "San Francisco" in chunks[3]
    # turns are never merged across a speaker change
    assert "assistant:" not in chunks[0]


def test_turn_aware_never_severs_a_turn_under_the_bound():
    S = _server(turn_aware=True, propagate=True)
    long_turn = " ".join(f"Sentence number {i} about the trip." for i in range(20))
    doc = f"[Date: 2023/01/01 (Sun) 10:00]\nuser: {long_turn}\n"
    chunks = S._chunk_document(doc)
    assert len(chunks) == 1, "a turn inside the bound stays whole"
    assert chunks[0].count("Sentence number") == 20


def test_turn_aware_splits_an_oversized_turn_and_re_carries_context_on_every_piece():
    S = _server(turn_aware=True, propagate=True)
    huge = " ".join(f"Sentence number {i} about the very long trip narrative." for i in range(200))
    doc = f"[Date: 2023/01/01 (Sun) 10:00]\nuser: {huge}\n"
    chunks = S._chunk_document(doc)
    assert len(chunks) > 1, "an oversized turn must be split"
    for c in chunks:
        assert c.startswith("[Date: 2023/01/01 (Sun) 10:00]\nuser: "), c
    # no sentence is lost across the split
    assert sum(c.count("Sentence number") for c in chunks) == 200


def test_prose_document_is_identical_with_flags_on_and_off():
    """A document with no speaker turns must never notice either flag."""
    off = _server(turn_aware=False, propagate=False)._chunk_document(PROSE)
    on = _server(turn_aware=True, propagate=True)._chunk_document(PROSE)
    assert on == off


def test_context_propagation_alone_fixes_the_legacy_boundaries():
    """Propagation ON / turn-aware OFF: same boundaries as legacy, context restored."""
    legacy = _server(turn_aware=False, propagate=False)._chunk_document(TRANSCRIPT)
    fixed = _server(turn_aware=False, propagate=True)._chunk_document(TRANSCRIPT)
    assert len(fixed) == len(legacy), "propagation must not change chunk COUNT"
    assert all("[Date:" in c for c in fixed), "every chunk is date-grounded"


# ── the CONSUMER (verify the read, not the write) ───────────────────────────────
def test_every_chunk_resolves_its_own_session_reference_at_the_ingest_door():
    """The point of the header is what `/ingest` DOES with it.

    `/ingest` calls `_compute_ingest_reference_and_text` -> `src.temporal.derive_now`,
    whose marker regex is `.match()` — ANCHORED AT THE START. A chunk without a LEADING
    "[Date: …]" therefore falls back to wall-clock now, and a bare "3/22" in a 2023
    transcript is silently stamped with the CURRENT year. Measured live on one real
    chunk: event_date 2026-03-22 without the header, 2023-03-22 with it.

    This asserts the whole path: chunker -> chunk text -> the reference the ingest door
    actually derives, including the switch to the SECOND session's date.
    """
    derive_now = pytest.importorskip("src.temporal").derive_now
    S = _server(turn_aware=True, propagate=True)
    refs = [derive_now(c)[0].date().isoformat() for c in S._chunk_document(TRANSCRIPT)]
    assert refs == ["2023-04-10", "2023-04-10", "2023-04-10", "2023-05-02"], refs


def test_legacy_chunks_lose_the_session_reference():
    """The defect, pinned: with both flags OFF, later chunks fall back to wall-clock now."""
    import datetime as _dt
    derive_now = pytest.importorskip("src.temporal").derive_now
    S = _server(turn_aware=False, propagate=False)
    refs = [derive_now(c)[0].date() for c in S._chunk_document(TRANSCRIPT)]
    today = _dt.datetime.now(_dt.timezone.utc).date()
    assert any(r == today for r in refs), (
        "legacy chunking is expected to drop the session reference on at least one chunk; "
        f"got {refs}")


# ── drift pin ───────────────────────────────────────────────────────────────────
def test_worker_and_transport_share_one_definition_of_the_line_shapes():
    """embedder.py must not hold a private copy of the role/header regexes.

    Three hand-maintained copies of the MCP tool descriptions once drifted and shipped a
    degraded tool to every OpenWebUI model; the same failure mode applies here, so the
    identity is pinned rather than the pattern text.
    """
    emb = pytest.importorskip("src.re_embedder.embedder")
    from src.ingest import document_structure as D
    assert emb._ROLE_LINE_RE is D.ROLE_LINE_RE
    assert emb._DOC_HEADER_RE is D.DOC_HEADER_RE
