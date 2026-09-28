"""PROMPT BUDGET — bound a metadata-driven prompt preamble so the growth engine cannot
grow the prompt out of the brain's context window.

THE BUG (confirmed on live pre-prod, 2026-08-01)
------------------------------------------------
``_build_extraction_prompt`` (``src/api/main.py``) enumerates the ``rel_types`` table with
NO bound, one line per row::

    base_prompt += f'  - {rel_type} ({behavior}): {label}\\n'

Its filter is a TAUTOLOGY — ``WHERE correction_behavior IS NOT NULL AND
correction_behavior != 'ignore'`` against a column whose CHECK constraint permits only
``hard_delete|supersede|immutable`` — so it selects **every row**. Measured on one tenant
: **541 rel_types** (public seed is 66; **475 engine-minted**), rendering a
**5,925-token** block. Total EXTRACT system prompt **~9,974 tokens against an 8,192-token
slot** → HTTP 400 ``exceed_context_size_error`` → **EXTRACT scored 0 successes / 93
failures**.

WHY THIS IS THE NASTIEST SHAPE OF THE BUG — and why splitting the INPUT cannot fix it.
The overflow is in the PREAMBLE, not the content. No amount of chunking the user's text
helps once the menu alone exceeds the window. And it is PROGRESSIVE: every ingest grows the
ontology, every growth lengthens the prompt. A fresh tenant works; a long-lived, well-trained
one silently loses ALL extraction. **The better the memory, the more completely it dies.**

THE FIX AND WHY THIS ONE
------------------------
The block's purpose is to tell the model which rel_types need special correction handling.
Look at what the 541 rows actually SAY: **475 of them restate the same behavior**, and only
**9** carry a different one. The menu was spending 5,925 tokens to convey ~40 tokens of
information. The original ``!= 'ignore'`` filter shows the intent was always "list the ones
that need saying" — it was simply written against a value the schema forbids.

So: **state the dominant behavior ONCE as a blanket rule, enumerate only the exceptions**,
rank deterministically, and cap the result by a character budget derived from the DISCOVERED
window rather than a magic constant. Subject-agnostic and metadata-driven throughout — the
dominant behavior is computed as the MODAL value of the column, never a hardcoded
``'supersede'``; no rel_type name, domain word, or type list appears anywhere in this file.

WHAT WAS CONSIDERED AND REJECTED (so this is not re-litigated later)
--------------------------------------------------------------------
* **``LIMIT 50`` (or any magic N).** Rejected. Arbitrary, blind to the tenant's actual
  window, and — because the existing query's ``ORDER BY correction_behavior, rel_type``
  is alphabetical — it would silently drop whichever exceptions sort late, changing ingest
  semantics as the ontology grows. It also fixes nothing on a brain with a smaller window.
* **Drop the block entirely.** Rejected. The non-default rows (``immutable`` /
  ``hard_delete``) genuinely change correction behaviour; deleting them changes what the
  engine captures. The exceptions are exactly the part worth keeping.
* **Select rel_types by semantic similarity to the turn (embeddings/cosine).** Rejected on
  two counts: the project's deterministic-not-fuzzy rule, and it would make the system
  prompt vary per turn — new nondeterminism in a codebase already fighting bench flicker.
* **Prune the engine-minted sprawl** (``bring``, ``set``, ``draw_to_to``, …). Rejected HERE —
  it is a real growth-engine quality problem, but it is not a prompt fix, it destroys grown
  metadata, and "we don't forget". Worth its own work item; noted, not smuggled in.
* **Hardcode an allowlist of "important" rel_types.** Forbidden outright by the project's
  subject-agnostic rule.

FAIL-SAFE: every function here degrades to today's exhaustive rendering rather than losing
information it cannot rank, and never raises.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Iterable, Optional, Sequence

try:
    import structlog  # type: ignore
    log = structlog.get_logger()
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("prompt_budget")


def enabled() -> bool:
    """``PROMPT_PREAMBLE_BUDGET`` (default true). OFF ⇒ callers get the legacy rendering."""
    return os.getenv("PROMPT_PREAMBLE_BUDGET", "true").strip().lower() not in (
        "0", "false", "no", "off")


def _default_budget_chars() -> int:
    """Fallback preamble budget when the window is unknown, in CHARACTERS.

    ~2,000 tokens at the conservative 3.5 chars/token estimate. Chosen as a fraction of the
    smallest window we have actually met in the wild (8,192 per slot), leaving room for the
    fixed instruction body and the completion reserve. Configurable; only ever used when
    discovery has nothing to say.
    """
    try:
        return max(200, int(os.getenv("PROMPT_PREAMBLE_BUDGET_CHARS", "7000")))
    except ValueError:
        return 7000


def _default_content_reserve_chars() -> int:
    """Characters kept free for the USER content the caller appends after the preamble.

    Default 4,000 ≈ 1,150 tokens at the conservative estimate — comfortably above both
    measured populations: the p99 LongMemEval turn is 754 tokens and the largest document
    chunk is 532 tokens (``_DOC_CHUNK_MAX_CHARS`` is 1,200). Configurable.
    """
    try:
        return max(0, int(os.getenv("PROMPT_CONTENT_RESERVE_CHARS", "4000")))
    except ValueError:
        return 4000


def preamble_budget_chars(fixed_prompt_chars: int, content_chars: Optional[int] = None,
                          reserve_completion_tokens: int = 2048) -> int:
    """Characters a GROWABLE preamble may spend, given what the rest of the call needs.

    Derived from the discovered context window (``src/api/context_window.py``) so the bound
    tracks the brain the tenant actually runs, not a constant someone picked in 2026. Falls
    back to ``PROMPT_PREAMBLE_BUDGET_CHARS`` when the window is unknown.
    """
    if content_chars is None:
        content_chars = _default_content_reserve_chars()
    try:
        from src.api import context_window
        total = context_window.prompt_budget_chars(reserve_completion_tokens)
    except Exception:  # noqa: BLE001
        total = None
    if not total:
        return _default_budget_chars()
    budget = total - max(0, int(fixed_prompt_chars)) - max(0, int(content_chars))
    return budget if budget > 0 else 0


def _rows_as_tuples(rows: Iterable) -> list[tuple]:
    out: list[tuple] = []
    for r in rows or []:
        try:
            out.append(tuple(r))
        except Exception:  # noqa: BLE001
            continue
    return out


def correction_behavior_block(rows: Sequence,
                              budget_chars: Optional[int] = None,
                              usage: Optional[dict] = None) -> str:
    """Render the CORRECTION-SUPPORTING REL_TYPES preamble, bounded.

    ``rows`` are ``(rel_type, label, behavior)`` — exactly what the existing query in
    ``_build_extraction_prompt`` returns — with an OPTIONAL 4th element ``source`` used only
    for ranking (seeded backbone before engine-grown, the same reservation principle the
    ``LIMIT 15`` menu above it already applies). ``usage`` optionally maps rel_type → fact
    count so the tenant's own evidence of relevance drives truncation.

    Output shape::

        CORRECTION-SUPPORTING REL_TYPES (metadata-driven):
        Mark with is_correction=true when the user corrects a rel_type below.
        Default correction behavior for every rel_type not listed here: supersede.
          - immutable: born_in, born_on, child_of, ...

    The dominant behavior is the MODAL value of the column — computed, never hardcoded — so
    this is correct for any ontology, in any domain, on any tenant.
    """
    tuples = _rows_as_tuples(rows)
    if not tuples:
        return ""
    if not enabled():
        return _legacy_block(tuples)

    behaviors = [str(t[2]) for t in tuples if len(t) >= 3 and t[2] is not None]
    if not behaviors:
        return _legacy_block(tuples)
    dominant, dominant_n = Counter(behaviors).most_common(1)[0]

    exceptions: dict[str, list[tuple]] = {}
    for t in tuples:
        if len(t) < 3 or t[2] is None:
            continue
        b = str(t[2])
        if b == dominant:
            continue
        exceptions.setdefault(b, []).append(t)

    header = ("\nCORRECTION-SUPPORTING REL_TYPES (metadata-driven):\n"
              "Mark with is_correction=true when the user corrects a rel_type below.\n"
              f"Default correction behavior for every rel_type not listed here: {dominant}.\n")

    if not exceptions:
        # Every row shares one behavior — the blanket line says all of it. This is not a
        # degenerate case, it is the common one (475/541 on the measured tenant).
        return header

    budget = budget_chars if budget_chars is not None else _default_budget_chars()
    body = ""
    dropped_total = 0
    for behavior in sorted(exceptions):
        names = [str(t[0]) for t in _rank(exceptions[behavior], usage)]
        line, dropped = _pack_line(f"  - {behavior}: ", names,
                                   max(0, budget - len(header) - len(body)))
        dropped_total += dropped
        if line:
            body += line
    if dropped_total:
        body += f"  (+{dropped_total} further rel_types omitted to fit the context window)\n"
        log.warning("prompt_budget.menu_truncated", dropped=dropped_total,
                    budget_chars=budget, total_rows=len(tuples),
                    note="ranked seed-first then by tenant usage; deterministic")

    log.info("prompt_budget.correction_menu",
             rows=len(tuples), dominant_behavior=dominant, dominant_rows=dominant_n,
             enumerated=sum(len(v) for v in exceptions.values()),
             chars=len(header) + len(body),
             legacy_chars=len(_legacy_block(tuples)))
    return header + body


def _rank(rows: list[tuple], usage: Optional[dict]) -> list[tuple]:
    """Deterministic ranking: seeded backbone first, then tenant usage, then name.

    NOTE the tie-break on ``rel_type``: stable ordering matters as much as the ranking. An
    unordered truncation would make the system prompt vary between runs on the same data,
    which is precisely the bench-flicker failure mode this codebase is already fighting.
    """
    usage = usage or {}

    def key(t: tuple):
        source = str(t[3]).lower() if len(t) >= 4 and t[3] is not None else ""
        seeded = 0 if source in ("wikidata", "builtin", "seed") else 1
        used = -int(usage.get(str(t[0]), 0) or 0)
        return (seeded, used, str(t[0]))

    return sorted(rows, key=key)


def _pack_line(prefix: str, names: list[str], budget: int) -> tuple[str, int]:
    """Comma-join ``names`` behind ``prefix`` within ``budget`` chars. Returns (line, dropped)."""
    if budget <= len(prefix) + 2:
        return "", len(names)
    line = prefix
    kept = 0
    for n in names:
        add = (", " if kept else "") + n
        if len(line) + len(add) + 1 > budget:
            break
        line += add
        kept += 1
    if not kept:
        return "", len(names)
    return line + "\n", len(names) - kept


def _legacy_block(tuples: list[tuple]) -> str:
    """Byte-for-byte the pre-fix rendering — the flag-OFF path and the size baseline."""
    out = "\nCORRECTION-SUPPORTING REL_TYPES (metadata-driven):\n"
    out += "Mark with is_correction=true when user corrects these rel_types:\n"
    for t in tuples:
        rel_type = t[0] if len(t) > 0 else ""
        label = t[1] if len(t) > 1 else ""
        behavior = t[2] if len(t) > 2 else ""
        out += f'  - {rel_type} ({behavior}): {label}\n'
    return out


def bound_menu_lines(lines: Sequence[str], budget_chars: int, menu: str = "menu") -> list[str]:
    """Generic char-budget truncation for a growable, already-ranked menu of prompt lines.

    For the ontology menus in the correction/retraction prompts, whose ``LIMIT`` was removed
    ON PURPOSE ("a truncated menu hid grown rels") — a decision this does not overturn. It
    keeps the full menu whenever it fits and only trims when the alternative is the endpoint
    REFUSING the entire request, which hides every rel rather than the tail. Truncation is
    announced in-band so the model is not silently told a partial ontology is the whole one.
    """
    kept: list[str] = []
    used = 0
    for ln in lines or []:
        if used + len(ln) + 1 > budget_chars:
            break
        kept.append(ln)
        used += len(ln) + 1
    dropped = len(lines or []) - len(kept)
    if dropped > 0:
        kept.append(f"  (+{dropped} further rel_types not shown — context window limit)")
        log.warning("prompt_budget.menu_truncated", menu=menu, dropped=dropped,
                    kept=len(kept) - 1, budget_chars=budget_chars)
    return kept
