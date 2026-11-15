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
import os
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


# ---------------------------------------------------------------------------
# REFERENCE / FRONT-MATTER DENSITY GATE (litmus fix L4, 2026-11-03)
# ---------------------------------------------------------------------------
# THE CORPUS EVIDENCE (prod seat faultline_1a2b3c4d..., docs 10-14 + 6 controls,
# 671 chunks, benchmarks/corpus_gate_eval8.py + the CSV it reads): arXiv-style
# documents carry CONTIGUOUS reference runs (doc10 lines 106-155, doc12 98-129,
# doc13 65-94, doc14 107-146) whose chunks are citation-line DENSE - every non-empty
# line is an entry carrying a bracket number, a venue token, an arXiv id, or a
# year/page-range - plus affiliation-block front matter ("... is with the",
# emails, Department/School/Institute of ...). The extraction ladder converts
# these chunks into citation JUNK rels/entitles (the 37% zero-yield class of the
# ticket) while burning the full spine+rewrite+GLiNER2 ladder on text that has NO
# assertions in it.
#
# MEASURED DECISION (final): ref_line_ratio >= DOC_CITE_DENSITY_RATIO (0.5) AND
# marks >= DOC_CITE_MIN_MARKS (6), OR an affiliation-dense block (>= 3 affil lines
# and >= 0.6 of non-empty lines). On the real corpus: 175/534 arXiv chunks flagged
# (33%, matching the ticket's 12-37% zero-yield band), 0/137 control chunks. The
# threshold is deliberately CONSERVATIVE - CLAUDE.md precedent: a type gate that
# scored 60% false vetoes was DELETED; this one measured 0 false vetoes on the
# controls before it shipped. Tables and citation-cluttered prose that share the
# lane are a DOCUMENTED TRADEOFF: their text is retained verbatim in episodic_log
# (searchable via stub_tsv) and re-mineable by the R2 lane - only the LADDER is
# skipped, nothing is deleted.
#
# STRUCTURAL, not lexical-domain: every regex is a citation MACHINERY shape
# (bracket numbers, venue boilerplate, arXiv ids, year/page ranges, affiliation
# boilerplate) - no domain noun, no subject vocabulary. The same gate works on a
# law review, a biology paper, or a CS proceedings.

_CITE_NUMS_RE = re.compile(r"\[(\d{1,3})\]")
_CITE_VENUE_RE = re.compile(
    r"\b(?:Proceedings|arXiv preprint|Journal of|IEEE Transactions|ACM Symposium|"
    r"Conference on|Annual Meeting of|Advances in Neural|vol\.|\bpp\.|"
    r"Association for Computational Linguistics|International Conference|Symposium on)\b",
    re.IGNORECASE)
_CITE_ARXID_RE = re.compile(r"arXiv:\d{4}\.\d{4,5}", re.IGNORECASE)
_CITE_YEAR_PAGES_RE = re.compile(r"(?:\b(?:19|20)\d{2}\b|\b\d{1,4}\s*[–—-]\s*\d{1,4}\b)")
# the CONTACT-INFORMATION signature (R4 F4, R5 F-4): an email token — affil_run
# needs >=2 lines carrying one, so org-roster PROSE without contact info never
# skips. TLD whitelist shared by BOTH consumers (the email⊆affil invariant holds
# by construction); widened beyond edu/org/com/ac/cn for international contact
# blocks (R5 measured miss: x@y.io).
# R6 F7: the group ends with a NEGATIVE SUFFIX lookahead, not \b — a whitelisted
# label must not END the match mid-host (user@y.edu.foo.bar does NOT match: the
# only whitelisted label, edu, is followed by .foo and the lookahead rejects it;
# trailing-dot prose "...write to j@x.edu." still matches). RESIDUAL, inherent to
# any TLD whitelist (R6): a hostile/multi-label host whose REAL final label is
# whitelisted (user@y.edu.evil.com ends in .com; user@y.ca.uk ends in .uk)
# matches — no regex knows DNS truth. Harmless to the counting purpose: the
# token IS email-shaped, affil_run still needs >=3 affil lines @ >=0.6, and the
# seam position gate bounds the skip; documented, not fixable at this layer.
_CITE_EMAIL_TLDS = r"(?:edu|org|com|ac|cn|net|io|de|fr|uk|jp|kr|au|ca|ch|nl|eu|info)(?!\.[A-Za-z0-9])"
_CITE_AFFIL_RE = re.compile(
    r"(?:is with the|@[\w.-]+\." + _CITE_EMAIL_TLDS + r"\b|Key Laboratory|Department of|"
    r"School of|University of|Institute of|Academy of|Corresponding author)",
    re.IGNORECASE)
_CITE_EMAIL_RE = re.compile(r"@[\w.-]+\." + _CITE_EMAIL_TLDS + r"\b", re.IGNORECASE)
# LOG-SHAPE veto (gauntlet R1 finding 2): a line-LEADING bracket id followed by a
# timestamp or a level token is a worker-log entry ("[402] 10:31:02 worker:") —
# never a reference line. Reference lists and PDF-wrapped survey sentences also
# open lines with [n], but they continue natural language, not a clock.
_CITE_LOG_SHAPE_RE = re.compile(
    r"^\s*\[\d{1,3}\]\s*(?:"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?"                       # HH:MM[:SS]
    r"|\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"  # ISO date[-time]
    r"|\d{1,2}/\d{1,2}/\d{2,4}"                                   # slash date
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\b"  # syslog (R4 F2)
    r"|\d{9,13}(?:\.\d{1,3})?"                                    # epoch s/ms (R4 F2)
    r"|(?:INFO|ERROR|WARN|WARNING|DEBUG|CRITICAL|FATAL|TRACE|NOTICE)\b"  # level
    r")",
    re.IGNORECASE)
# GAUNTLET R2 FINDING 1 (+ R3 F2, R4 F1/F3/F5): canonical LOG lines carry a
# TIMESTAMP signature — leading OR near-leading — plus a bracket id:
#   "2026-11-03 10:31:02,123 [402] worker: …"          ISO datetime lead
#   "Nov  3 10:31:02 web-01 sshd[402]: …"              syslog RFC 3164
#   "Nov  3 10:31 web-01 sshd[402]: …"                 syslog, no seconds (R4 F5)
#   "2026/11/03 10:31:02 [402] …"                      slash date + time
#   "1762165862 [402] …" / "1762165862123 [402] …"     epoch s/ms/fractional (R4 F1)
#   "2026-11-03[402] worker: …"                        date-only, no space (R4 F2)
#   "INFO [402] worker: …"                             level lead (anchored only)
# The same signature after a small PREFIX (bullet, "1. ", quote, "user: my log
# shows …") is still a log line — R4 F3, so _cite_line_is_log also runs a
# DE-ANCHORED search over the first 48 chars, gated on a numeric bracket mark.
# FN GUARDS (measured): a bare leading YEAR survives (ISO needs the month part);
# epoch needs the bracket qualifier and 9-13 digits (numeric-led prose, ISBN13
# bibliography anchors demote — throughput only); the clock form needs THREE
# colon groups (verse refs "10:12" survive); date-only ISO needs the bracket.
# The LEVEL token is ANCHORED-ONLY: de-anchored it would veto real prose
# ("Error rates in [12] were significant"), so the de-anchored set carries
# TIMESTAMP alternatives exclusively.
_CITE_LOG_ALTS_TS = (
    r"\d{4}-\d{2}(?:-\d{2})?[T ]\d{2}:\d{2}"                      # ISO date + time
    r"|\d{1,2}:\d{2}:\d{2}\b"                                      # HH:MM:SS (3 groups)
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\b"  # syslog, seconds optional (R4 F5)
    r"|\d{1,4}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}"                 # slash date + time
    r"|\d{4}-\d{2}-\d{2}\s*\["                                    # date-only + bracket id (R4 F2: gap optional)
    r"|\d{9,13}(?:\.\d{1,3})?\s*\["                               # epoch s/ms/fractional + bracket (R4 F1)
)
_CITE_LOG_ALTS_LEVEL = r"(?:INFO|ERROR|WARN|WARNING|DEBUG|CRITICAL|FATAL|TRACE|NOTICE)\b"
_CITE_LOG_LEAD_RE = re.compile(
    r"^\s*(?:" + _CITE_LOG_ALTS_TS + r"|" + _CITE_LOG_ALTS_LEVEL + r")",
    re.IGNORECASE)
_CITE_LOG_DEANCHORED_RE = re.compile(
    r"(?:" + _CITE_LOG_ALTS_TS + r")",
    re.IGNORECASE)


def _cite_line_is_log(l: str) -> bool:
    """GAUNTLET R4 FINDING 3: the log signature wherever it sits — the anchored
    lead fast path, or (when the line carries a numeric bracket mark) the same
    TIMESTAMP signature within the first 48 chars (bullet / numbered / quote /
    transcript prefixes: "- 2026-11-03 …", "1. …", "> Nov  3 …", "user: my log
    shows 2026-11-03 …"). A veto can only REDUCE ref/affil counts, so the
    de-anchoring is direction-safe; its cost is throughput-only FNs, bounded by
    the 48-char window. ACCEPTED residuals (documented, throughput-only):
    (a) prose lines carrying BOTH a full timestamp signature and a numeric
        bracket within 48 chars ("resumed at 10:31:02 [5] with…");
    (b) reference lines whose first word is a level token ("NOTICE of Appeal
        filed 2019…") — anchored level veto;
    (c) bracket-id logs with NO timestamp and NO level token."""
    if _CITE_LOG_LEAD_RE.match(l):
        return True
    return bool(_CITE_NUMS_RE.search(l) and _CITE_LOG_DEANCHORED_RE.search(l[:48]))

# Residual, ACCEPTED (gauntlet R3 finding 3, measured, throughput-cost only):
#   (a) a reference line whose FIRST word is a level token — "NOTICE of Appeal
#       filed 2019…", "Trace element analysis of…" — is vetoed by the level
#       alternative; a chunk still escapes the gate unless >50% of its lines are
#       such, and a false NEGATIVE only costs ladder throughput, never a loss.
#   (b) bracket-id logs with NO timestamp and NO level token still read as
#       reference lines.
#   (c) bare HH:MM (two colon groups) leads are NOT vetoed — verse refs like
#       "10:12 A reference about…" share the shape.
#   (d) R5 F-3, measured, throughput-only: the timestamp alternatives also veto
#       REAL reference lines that embed clocked stamps — "Accessed 2026-11-03
#       10:31 [7]: Smith v. Jones…" (legal citations), "PMID 123456789 [12]" and
#       bare "9780132350884 [12]" (ISBN13) — demoting such blocks below the run
#       threshold so the gate escapes and the ladder burns on a genuine reference
#       block (the ticket's junk class). Direction-safe: a skipped skip costs
#       throughput, never a loss. REMINE SEMANTICS (R7 F-4, traced): a zero-edge
#       ladder result is a DONE/c=0 no-cls ledger entry, which the remine prepass
#       RE-OWES (kept requires c>0) — on a remine-eligible document the burn
#       REPEATS up to DOC_REMINE_MAX_ATTEMPTS; junk-edge results (c>0) are kept
#       untouched. Remine recovers NEITHER outcome — it re-reads, it does not
#       repair.
#   (e) R6 F2, measured: a FACT-BEARING staff roster WITH emails at chunk idx <=
#       DOC_CITE_AFFIL_FRONT_MATTER_MAX is still permanently skipped (no
#       chunk-granularity discriminator separates it from an authors-contact
#       block); the seam position gate covers the deeper-document case only.#
# ALTERNATIVE-SET FREEZE (owner ruling context, gauntlet R5 Q5): the measured
# 175/534 gate effect is carried ENTIRELY by the R1-era core (NUMS/venue/year
# marks + LEVEL + shape veto); the timestamp signature alternatives have ZERO
# corpus prevalence and exist as critic-shape insurance. NO further alternative
# may be added without corpus prevalence or a reproducible FP against the
# CURRENT gate — each addition widens the prose-FN surface this file documents.


def _cite_env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _cite_env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def citation_density_verdict(text: str) -> dict:
    """Is this chunk citation-line DENSE enough that the ladder would only mint junk?

    Pure function, no I/O, deterministic: the same text always yields the same
    verdict. Returns {"cite_dense": bool, "ref_line_ratio": float, "marks": int,
    "affil_lines": int, "affil_ratio": float, "nonempty_lines": int, "reason": str}.

    The verdict decides a SKIP of the extraction ladder (fix L4), never a deletion:
    callers retain the chunk verbatim and mark an HONEST zero-yield class
    (`cite_dense_reference_block`), distinct from read-and-empty and from failure.
    """
    r = _cite_env_float("DOC_CITE_DENSITY_RATIO", "0.5")
    m = _cite_env_int("DOC_CITE_MIN_MARKS", "6")
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return {"cite_dense": False, "ref_line_ratio": 0.0, "marks": 0,
                "affil_lines": 0, "affil_ratio": 0.0, "nonempty_lines": 0,
                "reason": "empty"}

    def _is_ref_line(l: str) -> bool:
        # GAUNTLET R1 FINDING 2 — two populations share bracketed numbers, and the
        # discriminator is WHAT FOLLOWS the bracket, not where it sits:
        #   * MID-LINE brackets are inline CITATIONS ("HyKGE [73] first defines…") —
        #     the measured 175/534 prod population is exactly this survey-prose
        #     clutter; every such line counts.
        #   * LINE-LEADING brackets are AMBIGUOUS: reference lists open lines with
        #     [n], and so do PDF-wrapped survey sentences (the wrap lands the
        #     citation at line start) — but so do worker-log line ids, and an
        #     8-line log chunk ("[402] 10:31:02 worker: connection refused", zero
        #     citations) reproduced a false cite_dense=True that would be
        #     PERMANENT (the R2 remine re-evaluates this same deterministic
        #     verdict). A leading bracket followed by a timestamp/level/ISO-date
        #     (the LOG-SHAPE signature) is a log entry, never a reference line;
        #     any other leading bracket stays a reference line (the measured
        #     population). Residual, accepted: bracket-id logs with NO timestamp
        #     and NO level token still read as reference lines.
        if _cite_line_is_log(l):
            return False  # R2 F1 + R4 F3: the log signature wherever it sits —
                          # leading, or near-leading after a prefix with a bracket
        if _CITE_NUMS_RE.search(l):
            if _CITE_LOG_SHAPE_RE.match(l):
                return False  # a log entry, not a reference
            return True
        return bool(_CITE_VENUE_RE.search(l) or _CITE_ARXID_RE.search(l)
                    or _CITE_YEAR_PAGES_RE.search(l))

    ref_lines = sum(1 for l in lines if _is_ref_line(l))
    ref_ratio = ref_lines / len(lines)
    # GAUNTLET R5 F-2 + R6 F3 (measured parity-safe): marks count ONLY lines the
    # REF LANE does not veto — the same veto family _is_ref_line honours (the
    # timestamp signature AND the R1 bracket-first log shape). A bracketed PID is
    # not a citation mark; measured compound R5: 4 log + 4 fact lines -> ratio .5
    # marks 8 -> permanent skip of the FACT lines. R6 widened the exclusion to the
    # bracket-first family after the critic reproduced 2 bracket+LEVEL log lines
    # donating marks (8 such lines -> marks 8) on top of 5 inline-citation lines
    # + 3 fact sentences. Measured parity holds (175->175, 0 flips both classes;
    # flagged-chunk marks min 0 / p10 8 / median 11).
    _kept_text = "\n".join(
        l for l in lines
        if not (_cite_line_is_log(l)
                or (_CITE_NUMS_RE.search(l) and _CITE_LOG_SHAPE_RE.match(l))))
    marks = (len(_CITE_NUMS_RE.findall(_kept_text)) + len(_CITE_ARXID_RE.findall(_kept_text))
             + len(_CITE_VENUE_RE.findall(_kept_text)))
    # GAUNTLET R3 F1 + R4 F3: the affil lane honours the SAME log predicate (an
    # ISO-timestamp-led auth log carries an affiliation token PER LINE and would
    # otherwise pass affil_run, re-landing the R2 population through this side
    # door; prefix-defeated leads are covered by the shared predicate too).
    affil_lines = sum(1 for l in lines
                      if not _cite_line_is_log(l)
                      and _CITE_AFFIL_RE.search(l))
    affil_ratio = affil_lines / len(lines)

    # GAUNTLET R4 FINDING 4 + R5 F-1 + R6 F2 (the honest split): affil_run needs
    # the CONTACT-INFORMATION signature (>=2 email-bearing lines) AND, at the
    # embedder seam, the FRONT-MATTER position gate (idx <=
    # DOC_CITE_AFFIL_FRONT_MATTER_MAX, default 5). What the email signature alone
    # fixes: email-less roster/directory prose (measured FP population, zero
    # emails) demotes to body-prose. What it CANNOT fix (critic-measured): a
    # roster WITH emails is byte-shaped like an authors-contact block (both are
    # name + dept + email) — no chunk-granularity discriminator exists; the
    # position gate saves idx>5, an idx<=5 roster-with-emails IS still
    # permanently skipped. That residual FP is ACCEPTED and DOCUMENTED (seam
    # comment + residual (e) below); a real fix needs document-level context
    # (owner call), not more regex.
    email_lines = sum(1 for l in lines
                      if not _cite_line_is_log(l) and _CITE_EMAIL_RE.search(l))
    ref_run = ref_ratio >= r and marks >= m
    affil_run = (affil_lines >= 3 and affil_ratio >= 0.6 and email_lines >= 2)
    dense = bool(ref_run or affil_run)
    reason = ("ref_run" if ref_run else ("affil_block" if affil_run else "body_prose"))
    return {"cite_dense": dense, "ref_line_ratio": ref_ratio, "marks": marks,
            "affil_lines": affil_lines, "affil_ratio": affil_ratio,
            "nonempty_lines": len(lines), "reason": reason}
