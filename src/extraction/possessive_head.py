"""Possessive-head object resolver — the INGEST mirror of the query-side possessive helpers.

PART of the LongMemEval Q1 unlock (DEV/DESIGN-ingest-spine-and-temporal-recall.md). Both
extractors (`/harvest-spans` GLiNER2/verb-lift AND the LLM `/extract/rewrite`) extract the gold
clause "I had an issue with my car's GPS system" as

    (subject="i", rel_type="has_issue", object="car")          ← WRONG

The object resolves to the POSSESSOR ("car") instead of the POSSESSIVE HEAD ("GPS system").
In "my car's GPS system" the thing with the issue is the GPS system (the rightmost head noun
after the final possessive ``'s``), NOT the car (the possessor). So the gold clause must yield

    (subject="i", rel_type="has_issue", object="gps system")   ← head-anchored

This module is a SINGLE deterministic post-processor applied to extracted edges BEFORE
``/ingest`` in BOTH paths, sitting right alongside the relation-fit guardrail. It mirrors the
query-side ``_resolve_possessive_rel_target`` / ``_strip_possessive_suffix`` for the INGEST
direction.

THE GRAMMATICAL RULE (subject-agnostic — NO domain/keyword word-list):
  When an edge's OBJECT token appears in the source text immediately followed by a possessive
  ``'s`` (the possessor-marking clitic), the possessor is NOT the object — the HEAD NOUN PHRASE
  after the LAST ``'s`` in the chain is. We locate ``<object>'s`` in the text and lift the
  contiguous head noun phrase that follows it (dropping inner determiners/possessives, stopping
  at the first clause boundary / verb / load-bearing connector / punctuation). Nested chains
  ("my car's GPS system's antenna") resolve to the rightmost head ("antenna").

WHAT IS LEFT UNCHANGED (non-regression — a wrong rewrite is worse than a miss):
  - Object NOT followed by ``'s`` in the text → unchanged ("issue with the server", "owns a car").
  - Possessor-only "my car" (no ``'s``) → unchanged.
  - A trailing ``'s`` with NO head noun after it → unchanged (don't strip to empty).
  - Any failure on an edge → that edge is left exactly as-is (fail-safe; never raises, never
    crashes extraction).

HARD CONSTRAINTS:
- PURE, deterministic. Stdlib + the pure ``canonical`` morphology sets only. No GLiNER2, no LLM,
  no FastAPI, no DB. The only closed sets consulted are the bounded English-morphology sets
  already defined in ``canonical`` (determiners/pronouns/possessives, prepositions) — language
  primitives, not an ontology/world list (same contract as ``predicate_span`` / ``relation_fit``).
- Subject-agnostic — the ``'s`` boundary + head-noun rule is grammar; NO dictionary matching.
- Fail-safe: any error leaves the object unchanged. Kill-switch, default ON.
"""
from __future__ import annotations

import os
import re

from src.ontology.canonical import (
    _LEADING_DROP,
    _AUXILIARIES,
    _KEEP_PREPOSITIONS,
)

# Kill-switch (default ON). When false this is a pass-through (today's behavior) — single env flip.
POSSESSIVE_HEAD_RESOLVE = os.environ.get(
    "POSSESSIVE_HEAD_RESOLVE", "true"
).strip().lower() not in ("0", "false", "no")

# Possessive clitic suffix on a token (ASCII + curly apostrophe). Matches "car's"/"car’s"/"workers'".
_POSS_SUFFIX = re.compile(r"(?:'s|’s|'|’)$")

# A head noun phrase TERMINATES at any of these: a load-bearing connector/preposition (the head NP
# ended, a new phrase began), a clause/coordination word, or an auxiliary/verb. These are the SAME
# bounded English-grammar sets canonical already owns (one source of truth for "what is a function
# word"), NOT a new word-list. _LEADING_DROP determiners/possessives that appear INSIDE the head NP
# are dropped as filler, not treated as terminators.
_HEAD_TERMINATORS: frozenset[str] = (
    _KEEP_PREPOSITIONS | _AUXILIARIES | frozenset({
        "and", "or", "but", "nor", "yet", "so",
        "when", "while", "because", "although", "though", "if", "unless", "since",
        "whereas", "where", "after", "before", "until", "than", "that", "which", "who",
        "then", "however", "therefore", "thus",
    })
)


# Defensive cap on head-NP length. A noun compound ("database server", "gps system") is short;
# a longer run means we have over-run into the rest of the clause. Bounds the walk, never a
# semantic claim about content.
_MAX_HEAD_TOKENS = 4

# Verb-form suffixes. A token AFTER the first head noun that looks like a past-tense/participle
# (-ed) or gerund (-ing) is the CLAUSE VERB ("my car's gps system FAILED/CRASHED"), not part of
# the compound noun head — so it terminates the NP. Bounded English morphology (the same -ed/-ing
# shapes canonical's _suffix_lemma keys on), NOT a verb/world word-list. Only applied to a
# NON-FIRST token (the head noun itself may legitimately end in -ing, e.g. "my car's wiring").
_VERB_FORM_SUFFIX = re.compile(r"(?:ed|ing)$")


def _strip_poss(tok: str) -> tuple[str, bool]:
    """Return (token without a trailing possessive clitic, had_clitic). "car's"→("car",True)."""
    bare = _POSS_SUFFIX.sub("", tok)
    return bare, (bare != tok)


def _lift_head_after_possessive(text_lower: str, possessor: str) -> str | None:
    """Locate ``<possessor>'s <head NP>`` in ``text_lower`` and return the rightmost head NP.

    Deterministic + pure. The possessor is matched word-boundary aware (so "car" does not match
    inside "cart"). After the possessive clitic the contiguous head noun phrase is collected:
      • inner determiners/possessive pronouns are dropped (filler: "my car's THE gps" → "gps");
      • a CHAIN of further possessives is followed (nested "car's gps's antenna" → "antenna");
      • collection stops at the first head-terminator (preposition/auxiliary/verb/clause word)
        or punctuation.

    Returns the lowercased head NP string, or None when there is no possessive after the possessor
    OR no head noun survives (→ caller leaves the object unchanged).
    """
    possessor = (possessor or "").strip().lower()
    if not possessor:
        return None
    # Anchor on "<possessor>'s" (word boundary before the possessor; possessive clitic after).
    try:
        anchor = re.compile(r"\b" + re.escape(possessor) + r"\s*(?:'s|’s|'|’)(?=\s|$)")
    except re.error:
        return None
    m = anchor.search(text_lower)
    if not m:
        return None

    # Scan tokens after the clitic. Punctuation ends the noun phrase region; we keep only the
    # leading word run up to the first non-word/punctuation break.
    rest = text_lower[m.end():]
    np_region = re.split(r"[^a-z0-9'’\- ]", rest, maxsplit=1)[0]
    toks = [t for t in re.split(r"\s+", np_region.strip()) if t]

    np_words: list[str] = []
    for tok in toks:
        bare, had_clitic = _strip_poss(tok)
        if not bare:
            break
        if bare in _HEAD_TERMINATORS:
            break  # head NP ended at a connector / verb / clause word
        if bare in _LEADING_DROP:
            if had_clitic:
                break  # degenerate (possessive determiner) — stop
            continue   # inner determiner filler — skip, keep scanning the NP
        # A NON-FIRST -ed/-ing token is the clause verb ("...system FAILED/CRASHED"), not part of
        # the compound noun head → terminate the NP (keep the noun run gathered so far).
        if np_words and _VERB_FORM_SUFFIX.search(bare):
            break
        if len(np_words) >= _MAX_HEAD_TOKENS:
            break  # defensive: over-ran a plausible compound — stop (never claim a clause)
        if had_clitic:
            # Nested possessive: everything up to and INCLUDING this token is a possessor of a
            # FURTHER head NP. Re-anchor the chain on this nested possessor and take its head.
            nested_possessor = np_words[-1] if np_words else bare
            nested = _lift_head_after_possessive(text_lower, nested_possessor)
            if nested:
                return nested
            # nested lift failed → fall back to what we have so far (the possessor), i.e. miss
            break
        np_words.append(bare)

    head = " ".join(np_words).strip()
    return head or None


def resolve_object_heads(edges: list[dict], text: str) -> list[dict]:
    """Rewrite each edge's OBJECT from a POSSESSOR to its POSSESSIVE HEAD, using the source ``text``.

    For every edge whose ``object`` appears in ``text`` immediately followed by a possessive
    ``'s`` (i.e. the extractor grabbed the possessor), replace ``object`` with the head noun
    phrase after the final ``'s`` (see ``_lift_head_after_possessive``). Order-preserving;
    mutates a shallow copy of each affected edge. Edges with no possessive after their object,
    or any edge that errors, pass through UNCHANGED. Never raises.

    HEAD RESOLUTION is layered (additive): the spaCy ``poss``-dependency resolver
    (``src/extraction/linguistics.py``, kill-switch ``LINGUISTIC_LAYER``) is the PRIMARY when
    available; this module's bespoke ``'s``-anchored walk (``_lift_head_after_possessive``) is the
    FALLBACK when spaCy is unavailable OR returns None. Both paths agree on the canonical case
    ("my car's GPS system" → "gps system"); spaCy generalizes the rule grammatically. Regression-
    safe: spaCy only replaces the bespoke walk where it yields a head.

    Kill-switches: ``POSSESSIVE_HEAD_RESOLVE`` (default ON) → whole feature off; ``LINGUISTIC_LAYER``
    (default ON) → spaCy primary off, bespoke walk only (today's behavior before the fold).
    """
    if not POSSESSIVE_HEAD_RESOLVE or not edges or not (text or "").strip():
        return edges
    text_lower = text.lower()
    # LINGUISTIC LAYER (additive, kill-switch LINGUISTIC_LAYER default ON): the spaCy ``poss``
    # dependency → head-noun resolver GENERALIZES this module's bespoke ``'s``-anchored string
    # walk. It is tried FIRST; when it is unavailable (flag OFF / spaCy not baked) OR returns
    # None for an edge, we fall back to ``_lift_head_after_possessive`` — TODAY'S behavior,
    # unchanged. A miss in BOTH leaves the object exactly as the extractor produced it. This is
    # regression-safe by construction: spaCy can only REPLACE the bespoke walk where it returns a
    # head; otherwise the bespoke walk decides. Imported lazily + fail-safe (never crashes ingest).
    _ling_head = None
    try:
        from src.extraction.linguistics import possessive_head as _ling_head_fn, linguistics_available
        if linguistics_available():
            _ling_head = _ling_head_fn
    except Exception:
        _ling_head = None
    out: list[dict] = []
    for e in edges:
        try:
            obj = (e.get("object") or "").strip()
            if not obj:
                out.append(e)
                continue
            # PRIMARY: spaCy poss→head (dependency-driven, casing-robust). FALLBACK: bespoke walk.
            head = None
            if _ling_head is not None:
                try:
                    head = _ling_head(text, obj)
                except Exception:
                    head = None
            if not head:
                head = _lift_head_after_possessive(text_lower, obj)
            # Only rewrite when we lifted a DIFFERENT, non-empty head (don't strip to empty,
            # don't no-op rewrite). A miss leaves the object exactly as the extractor produced it.
            if head and head != obj.lower():
                new_e = dict(e)
                new_e["object"] = head
                # The lifted head ("gps system") is a FRESH entity — its type is NO LONGER the
                # possessor's type ("car"). This pure module CANNOT run GLiNER2 (hard constraint:
                # no GLiNER2/LLM/DB here), so it must NOT fabricate a type and must NOT hand the
                # stale possessor type ("OBJECT" inherited from "car") downstream. We CLEAR it to
                # None so the AUTHORITATIVE ingest-side GLiNER2 typing pass re-detects the head's
                # real type ("gps system" → Object/Device) from the head token rather than
                # inheriting the possessor's. None is falsy → the ingest typing path treats it as
                # "type me" (same as unknown) and consults the GLiNER2 cache; it does NOT lock in a
                # wrong type and does NOT feed A1's instance_of-linker a mistyped entity. The
                # relational edge (e.g. has_issue) is preserved intact — only the object token and
                # its stale type are corrected. (A car↔gps-system structural link, if wanted, is a
                # RELATIONAL part_of/has_component edge — NEVER a hierarchy/instance_of edge.)
                if "object_type" in new_e:
                    new_e["object_type"] = None
                out.append(new_e)
            else:
                out.append(e)
        except Exception:
            out.append(e)  # fail-safe per-edge: never drop or crash on a bad edge
    return out
