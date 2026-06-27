"""Turn decomposition — the swappable clause/sentence seam for the ingest spine.

PART 1 of DEV/DESIGN-ingest-spine-and-temporal-recall.md (item 1). Decompose a turn into the
fact-bearing clauses that the one guardrailed builder will route. The DEFAULT decomposer is the
union of:

  * ``trigger_span.find_factbearing_spans`` — the cheap, deterministic regex segmenter that
    returns sentences matching a public-seeded ``category='trigger'`` SIGNAL, AND
  * a plain sentence-split fallback (``_split_sentences``) — so a TRIGGERLESS aside (the
    LongMemEval Q1 clause "by the way, I had an issue with my car's GPS system", which carries
    no trigger signal) still surfaces as its own clause and reaches the builder.

The union (order-preserved, deduped) means: trigger-matched sentences are guaranteed in; every
OTHER sentence is also handed through so a buried, triggerless fact is never dropped before the
builder ever sees it. GLiNER then gets clause-level input (under its window, low label-dilution).

SWAPPABLE SEAM (per spec): ``register_decomposer(name, fn)`` + the ``RESIDUE_DECOMPOSER`` flag
(default ``"sentence"``) select the active strategy. This is the seam; the DEFERRED detection
optimizations (GLiNER2-anchor chain-back, anchor-grouping/sort, sliding-window overlap) are NOT
built — they register here later, measured first, with no call-site change.

HARD CONSTRAINTS: PURE (regex + the existing pure splitters), deterministic, subject-agnostic
(NO keyword/word-list), no LLM, no GLiNER2. Fail-safe: any decomposer error falls back to the
whole turn as a single clause so the builder always gets something.
"""
from __future__ import annotations

import os
from typing import Callable

from src.extraction import trigger_span

# A decomposer takes the raw turn text and returns an ordered list of clause strings.
Decomposer = Callable[[str], list[str]]

# Active-strategy selector. Kill-switch-style: an unknown / empty value falls back to "sentence".
RESIDUE_DECOMPOSER = os.environ.get("RESIDUE_DECOMPOSER", "sentence").strip().lower() or "sentence"

# Conservative sentence splitter — kept local so this module is import-light and matches the
# intent of main._split_sentences / trigger_span._SENT_SPLIT (one source of splitter behavior is
# not load-bearing here; the union below tolerates either granularity).
_SENT_SPLIT = trigger_span._SENT_SPLIT


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into trimmed, non-empty sentences (deduped by lowercase, order-preserved)."""
    out: list[str] = []
    seen: set[str] = set()
    for sent in _SENT_SPLIT.split((text or "").strip()):
        s = sent.strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def sentence_decomposer(text: str) -> list[str]:
    """DEFAULT decomposer: trigger-bearing spans ∪ all sentences (order-preserved, deduped).

    The union guarantees (a) every trigger-matched fact-bearing span is present, and (b) every
    triggerless sentence (the buried aside) is ALSO present — so a fact with no trigger signal
    still reaches the builder. We iterate the sentence split (which is a superset of the trigger
    spans for any reasonable trigger) and keep order; the trigger spans are folded in as a safety
    net in case the trigger segmenter splits differently from the plain splitter.
    """
    if not text or not text.strip():
        return []
    clauses: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = (s or "").strip()
        if not s:
            return
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        clauses.append(s)

    for s in _split_sentences(text):
        _add(s)
    # Fold in trigger spans too (belt-and-suspenders: different splitter granularity). Fail-safe.
    try:
        for s in trigger_span.find_factbearing_spans(text):
            _add(s)
    except Exception:
        pass
    return clauses if clauses else [text.strip()]


# ── Strategy registry (swappable seam) ─────────────────────────────────────────
_DECOMPOSERS: dict[str, Decomposer] = {
    "sentence": sentence_decomposer,
}


def register_decomposer(name: str, fn: Decomposer) -> None:
    """Register a decomposer strategy under ``name`` (idempotent overwrite).

    The DEFERRED detection optimizations (chain-back / anchor-sort / sliding-overlap) register
    here when built; nothing else changes — ``decompose()`` picks them up via RESIDUE_DECOMPOSER.
    """
    if not name or not callable(fn):
        return
    _DECOMPOSERS[name.strip().lower()] = fn


def decompose(text: str, strategy: str | None = None) -> list[str]:
    """Decompose ``text`` into clauses using the active strategy (``RESIDUE_DECOMPOSER``).

    ``strategy`` overrides the flag for a single call (tests). Fail-safe: an unknown strategy or
    any decomposer error degrades to the whole turn as one clause (the builder always gets input).
    """
    name = (strategy or RESIDUE_DECOMPOSER or "sentence").strip().lower()
    fn = _DECOMPOSERS.get(name) or _DECOMPOSERS["sentence"]
    try:
        clauses = fn(text)
    except Exception:
        clauses = [text.strip()] if text and text.strip() else []
    return clauses
