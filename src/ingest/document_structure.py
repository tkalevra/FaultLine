"""DETERMINISTIC document/transcript structure — shared by the MCP chunker and the worker.

The document lane already recognises exactly two STRUCTURAL line shapes (they were
introduced by ``_partition_chunk_by_role`` in ``src/re_embedder/embedder.py``):

* a **header** line — a lone bracketed line, e.g. ``[Date: 2023/04/10 (Mon) 17:50]``
* a **role** line  — ``user: …`` / ``assistant: …``

Those are *transport* shapes, not domain vocabulary: no subject, entity, rel_type or
domain word is named here, and nothing in this module looks at what the text is ABOUT.
There is no LLM call, no embedding and no similarity anywhere in it — every decision is
a line-shape test, so the same input always yields the same output.

WHY THIS MODULE EXISTS
----------------------
``_partition_chunk_by_role`` already states the product's own rule — a header line is
"SHARED context (prepended to both buckets) … so temporal (event_date) grounding still
applies". That rule was only ever applied *within* one chunk. Across chunk boundaries
the context was dropped, so a chunk that happened to start after its session header was
extracted with no date and, if it started mid-turn, no speaker either.

Measured consequence (LongMemEval oracle, 500 questions, 42 130 product chunks):

* **97.7 %** of chunks carry no header line, and **43.3 %** of the answer-bearing
  ("gold") turns land in a chunk with no header.
* **79.4 %** of chunks begin with an unprefixed continuation line, so the extractor
  cannot tell who is speaking.
* Live A/B on one real chunk: the identical text extracted ``event_date =
  2026-03-22`` without its header and ``2023-03-22`` with it — a **silent three-year
  temporal corruption**, because a bare "3/22" is resolved against wall-clock now when
  no session reference is in scope.

Both helpers below are NO-OPS on a document that carries neither shape (ordinary
prose), so a non-transcript document is byte-identical with the flags on.
"""
import re

# The two structural line shapes. Kept here as the single definition so the transport
# (src/mcp/server.py) and the worker (src/re_embedder/embedder.py) cannot drift apart.
ROLE_LINE_RE = re.compile(r"^\s*(user|assistant)\s*:\s*(.*)$", re.IGNORECASE)
DOC_HEADER_RE = re.compile(r"^\s*\[[^\]]*\]\s*$")


def has_transcript_structure(text: str) -> bool:
    """True iff `text` carries at least one role line — i.e. it is a transcript.

    Ordinary prose returns False, which is how every caller keeps plain documents on
    the legacy path.
    """
    for line in (text or "").splitlines():
        if ROLE_LINE_RE.match(line):
            return True
    return False


def split_structural_blocks(text: str):
    """Split a transcript into ordered blocks, keeping the structure explicit.

    Yields ``(kind, role, body)`` where `kind` is ``"header"`` or ``"turn"``:

    * ``header`` — a lone bracketed line; `role` is None, `body` is the line verbatim.
    * ``turn``   — one speaker turn: the role line plus every following unprefixed
      line (a continuation of the same turn). `role` is the lowercased speaker.

    Leading unprefixed content before any role line is emitted as a turn with
    ``role=None`` so nothing is dropped.
    """
    blocks = []
    cur_role = None
    cur_lines: list[str] = []

    def flush():
        if cur_lines and "".join(cur_lines).strip():
            blocks.append(("turn", cur_role, "\n".join(cur_lines).strip()))

    for raw in (text or "").splitlines():
        if DOC_HEADER_RE.match(raw):
            flush()
            cur_role, cur_lines = None, []
            blocks.append(("header", None, raw.strip()))
            continue
        m = ROLE_LINE_RE.match(raw)
        if m:
            flush()
            cur_role = m.group(1).lower()
            cur_lines = [raw.strip()]
            continue
        if raw.strip():
            cur_lines.append(raw.rstrip())
        elif cur_lines:
            cur_lines.append("")
    flush()
    return blocks


def propagate_chunk_context(chunks):
    """Carry the transcript's structural context ACROSS chunk boundaries.

    Walks `chunks` in order tracking the running header line and the running speaker.
    For each chunk:

    * if it carries no header line of its own, the running header is prepended;
    * if its first content line is an unprefixed continuation, the running speaker's
      prefix is re-applied to that line so the chunk is never speaker-blind.

    Returns a NEW list; the input is not mutated. A chunk list with no header and no
    role line anywhere is returned unchanged (ordinary prose).

    Evidence for the speaker half: DialogRE (Yu et al., ACL 2020) measures a 50.9 → 61.2
    F1 spread on dialogue relation extraction purely on whether the encoding lets the
    model tell which turn belongs to whom, and reports that 89.9 % of dialogue triples
    are speaker attributes or speaker-to-speaker relations. Evidence for the header
    half is our own A/B above (2026-03-22 vs 2023-03-22).
    """
    chunks = list(chunks or [])
    if not any(has_transcript_structure(c) or DOC_HEADER_RE.search(c or "")
               for c in chunks):
        return chunks

    out = []
    run_header = None
    run_role = None
    for chunk in chunks:
        lines = (chunk or "").splitlines()
        own_header = None
        first_content = None
        for line in lines:
            if not line.strip():
                continue
            if first_content is None:
                first_content = line
            if DOC_HEADER_RE.match(line):
                own_header = line.strip()
                break
        body = chunk

        # (a) speaker: re-prefix a chunk that opens on an unprefixed continuation.
        if (run_role and first_content is not None
                and not ROLE_LINE_RE.match(first_content)
                and not DOC_HEADER_RE.match(first_content)):
            idx = lines.index(first_content)
            lines = list(lines)
            lines[idx] = f"{run_role}: {first_content.lstrip()}"
            body = "\n".join(lines)

        # (b) header: prepend the running header when the chunk has none of its own.
        if own_header is None and run_header:
            body = f"{run_header}\n{body}"

        out.append(body)

        # advance the running context from what this chunk actually contained
        for line in (chunk or "").splitlines():
            if DOC_HEADER_RE.match(line):
                run_header = line.strip()
            else:
                m = ROLE_LINE_RE.match(line)
                if m:
                    run_role = m.group(1).lower()
    return out
