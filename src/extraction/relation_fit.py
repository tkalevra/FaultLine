"""Relation-fit guardrail — the junk-predicate filter for the ingest spine.

PART 1 of DEV/DESIGN-ingest-spine-and-temporal-recall.md (item 3). Every triple candidate,
*regardless of which extractor proposed it* (the GLiNER2 scorer, deterministic verb-lift, OR
the LLM `/extract/rewrite` path), must pass through here before it is allowed to reach the
WGM gate / `/ingest`. A predicate that cannot ground to a classifiable rel_type is dropped
BY CONSTRUCTION — so malformed fragments like ``gets_my`` / ``has_my`` (a light verb whose
"object" is a bare possessive pronoun, i.e. no content at all) can never land as facts.

WHAT "GROUNDS TO A CLASSIFIABLE REL_TYPE" MEANS (first hit wins):
  1. RUNG 2/3 — ``canonical.resolve_canonical`` resolves it to a known rel_type PK or a
     ``rel_type_aliases`` entry. PASS (it is or maps to a curated/grown ontology rel).
  2. RUNG 1 — it normalizes to a CLEAN NOVEL PREDICATE eligible for growth via the existing
     ``source="mcp"``/``user_stated`` /ingest growth path: a non-empty, content-bearing verb
     token whose segments are NOT pure function-word residue (no possessive/personal pronoun,
     no bare copula/auxiliary). A verb-lifted ``fix`` / ``repair`` / ``lives_in`` grows; a
     ``gets_my`` / ``has_my`` / ``be`` does not.
  else — DROP (a miss beats a junk rel; the no-islands invariant forbids un-walkable junk).

HARD CONSTRAINTS:
- PURE, deterministic. Stdlib + the pure ``canonical`` morphology helpers only. No GLiNER2,
  no LLM, no FastAPI. The tenant-scoped DB resolution (RUNG 2/3) happens via
  ``resolve_canonical`` which is the SAME deterministic ladder the verb-lift call sites use.
- Subject-agnostic — NO domain/keyword word-lists. The only closed sets consulted are the
  bounded English-morphology sets already defined in ``canonical`` (determiners, pronouns,
  auxiliaries) — language primitives, not an ontology/world list (same contract as
  ``predicate_span``).
- Fail-OPEN on an unexpected internal error (never silently eat a legitimate edge because the
  guardrail itself crashed) — but fail-CLOSED on a structurally-bad predicate (the whole point).
"""
from __future__ import annotations

import os

from src.ontology.canonical import (
    normalize_rel,
    resolve_canonical,
    _AUXILIARIES,
    _LEADING_DROP,
    _KEEP_PREPOSITIONS,
)

# Single-token FUNCTION WORDS that can NEVER be a standalone relation: conjunctions,
# subordinators, degree/focus adverbs, and bare prepositions. A predicate that normalizes
# to JUST one of these ("but"/"when"/"while"/"near"/"along"/"too"/"especially") is grammar,
# not a relation — extraction noise. This is a bounded ENGLISH-GRAMMAR primitive set (same
# kind as canonical's _LEADING_DROP/_AUXILIARIES), NOT a domain/ontology word-list. Verbs
# (earn/find/fix/move) are deliberately NOT here, so legitimate novel-verb rels still grow.
# Multi-token rels keep their prepositions (lives_in, feature_of) — this only rejects a
# SINGLE bare function word as the WHOLE predicate.
_NON_PREDICATE_WORDS: frozenset[str] = (
    _KEEP_PREPOSITIONS | frozenset({
        "and", "or", "but", "nor", "yet", "so",
        "when", "while", "because", "although", "though", "if", "unless", "since",
        "whereas", "where", "after", "before", "until", "than",
        "too", "very", "just", "also", "especially", "really", "quite", "then",
        "thus", "however", "therefore", "still", "even", "only", "almost",
        "along", "near", "through", "onto", "upon", "over", "under", "across",
        "around", "toward", "towards", "between", "among", "without", "within",
        "during", "against", "off", "out", "up", "down",
    })
)

# Kill-switch (default ON). When false, the guardrail is a pass-through (today's no-filter
# behavior) — a single env flip, no per-extractor wiring change.
RELATION_FIT_GUARDRAIL = os.environ.get(
    "RELATION_FIT_GUARDRAIL", "true"
).strip().lower() not in ("0", "false", "no")

# ── spaCy POS layer (PRIMARY when present, additive) — generalizes the single-token set above ──
# RUNG 1's bespoke ``_NON_PREDICATE_WORDS`` set is a hand-maintained English-grammar primitive
# list. The deterministic linguistic layer (spaCy ``en_core_web_sm``) can generalize it via real
# POS tagging — BUT only safely for MULTI-TOKEN predicates. Isolated single-token tagging by the
# sm model is UNRELIABLE: a bare "the"/"a"/"an" tags as PRON (not DET) and "too"/"especially" tag
# as ADV, so ``is_function_word_predicate("the")`` returns False (would NOT reject) — a regression
# of the bespoke set. In CONTEXT (≥2 tokens) the tagger is reliable ("out of" → ADP+ADP,
# "because of" → SCONJ+ADP), and catches multi-token all-function-word predicates the single-token
# bespoke set structurally MISSES.
#
# THE CONTRACT (UNION, tighten-only): the bespoke checks below are the AUTHORITATIVE FLOOR. spaCy
# may only ADD a rejection for a MULTI-TOKEN predicate that the bespoke set let through; it can
# NEVER cause a bespoke rejection to pass, and is NEVER consulted for a single bare token. When the
# layer is unavailable / killed / returns None, behavior is EXACTLY today's bespoke path.
try:
    from src.extraction.linguistics import (
        linguistics_available as _ling_available,
        is_function_word_predicate as _ling_is_function_word_predicate,
    )
except Exception:  # noqa: BLE001 — a missing linguistics module degrades to bespoke-only, fail-safe
    _ling_available = None  # type: ignore[assignment]
    _ling_is_function_word_predicate = None  # type: ignore[assignment]


def _spacy_rejects_multitoken(segments: list[str]) -> bool:
    """Return True iff spaCy CONFIDENTLY judges a MULTI-TOKEN predicate to be all function-word.

    Additive UNION member only — consulted AFTER the bespoke floor, and ONLY for predicates with
    ≥2 segments (where in-context POS tagging is reliable; isolated single-token tagging mistags
    determiners as PRON and degree-adverbs as ADV, so single tokens are left to the bespoke set).

    Returns False (no added rejection) when: the layer is killed/unavailable, the predicate is a
    single token, or the verdict is None/keep/anything-but-confident-reject. NEVER raises — any
    failure degrades to "no opinion", preserving the bespoke verdict (fail-safe, fail-open).
    """
    try:
        if _ling_available is None or _ling_is_function_word_predicate is None:
            return False
        # Gate on BOTH the linguistic-layer kill-switch (via linguistics_available) and the
        # multi-token reliability boundary. Single bare tokens are NEVER routed to spaCy.
        if len(segments) < 2:
            return False
        if not _ling_available():
            return False
        verdict = _ling_is_function_word_predicate(" ".join(segments))
        # Only a confident True (all content-bearing tokens are function-word POS) ADDS a rejection.
        # None (layer unavailable mid-call) / False (has content) → defer to the bespoke verdict.
        return verdict is True
    except Exception:  # noqa: BLE001 — fail-safe: a guardrail-internal error adds no rejection
        return False


# Pronoun/determiner residue that, if it appears as a SEGMENT of a normalized predicate token,
# marks the predicate as contentless: a possessive/personal pronoun or article can NEVER be the
# real OBJECT noun of a light-verb fold ("gets_my" / "has_my" / "gives_their" — the user-message
# had a possessive the LLM hallucinated into the predicate). This is the SAME bounded morphology
# set canonical uses for leading-strip — one source of truth for "what is a function word", no new
# word-list. NOTE: light-verb 3sg surfaces ("has_issue", "gets_discount") are CONTENT predicates
# (object noun present) and must PASS — so the auxiliary set is NOT included here; a single bare
# auxiliary/copula ("be", "has") is handled by the dedicated single-segment check below.
_RESIDUE_SEGMENTS: frozenset[str] = _LEADING_DROP


def is_relation_fit(
    rel_type: str,
    *,
    dsn: str | None = None,
    schema: str | None = None,
) -> bool:
    """Return True iff ``rel_type`` grounds to a classifiable rel_type (see module docstring).

    ``dsn``/``schema`` are forwarded to ``resolve_canonical`` so RUNG 2/3 reads the CORRECT
    tenant ontology (public seed ∪ tenant overlay) — NEVER another tenant, NEVER public at
    runtime when a schema is supplied. With ``schema=None`` it resolves against the default
    search_path (public seed) — used only outside request scope.

    Fail-OPEN: an unexpected internal error returns True (keep the edge) so the guardrail can
    never silently drop a legitimate fact by crashing. The structural rejection below is the
    intended fail-CLOSED path.
    """
    if not RELATION_FIT_GUARDRAIL:
        return True
    rt = (rel_type or "").strip().lower()
    if not rt:
        return False
    try:
        # RUNG 2/3 — known rel_type PK or a rel_type_aliases entry → classifiable. PASS.
        res = resolve_canonical(rt, dsn=dsn, schema=schema)
        if res.get("canonical"):
            return True

        # RUNG 1 — clean novel predicate eligible for growth?
        normalized = (res.get("normalized") or "").strip()
        if not normalized:
            # normalize_rel dropped it entirely (clitic residue / empty). A miss beats junk.
            return False
        segments = [s for s in normalized.split("_") if s]
        if not segments:
            return False
        # A single bare auxiliary/copula ("be", "has", "is") is not a content predicate.
        if len(segments) == 1 and segments[0] in _AUXILIARIES:
            return False
        # A single bare FUNCTION WORD (conjunction/subordinator/standalone preposition/
        # degree-adverb: "but"/"when"/"while"/"near"/"along"/"too"/"especially") is grammar,
        # not a relation. Reject standalone; multi-token rels keep their prepositions
        # (lives_in/feature_of). Verbs (earn/find/fix) are NOT in the set → still grow.
        if len(segments) == 1 and segments[0] in _NON_PREDICATE_WORDS:
            return False
        # ANY segment that is a possessive/personal pronoun or determiner means the predicate
        # carries no real object content (gets_my / has_my / gives_their). Reject BY CONSTRUCTION.
        # Load-bearing prepositions (in/at/of …) are NOT in _RESIDUE_SEGMENTS, so lives_in passes;
        # light-verb folds with a real object noun (has_issue / gets_discount) also pass.
        if any(seg in _RESIDUE_SEGMENTS for seg in segments):
            return False
        # UNION-TIGHTENING (additive): the bespoke floor above did not reject. Give the spaCy POS
        # layer a chance to ADD a rejection for a MULTI-TOKEN all-function-word predicate the
        # single-token bespoke set structurally misses ("out of"/"because of"/"up to"). Gated on
        # ≥2 segments + LINGUISTIC_LAYER/linguistics_available + a confident True; single tokens and
        # an unavailable/None verdict fall through to the bespoke PASS below. Tighten-only: this can
        # only reject, never rescue a bespoke rejection (those already returned False above).
        if _spacy_rejects_multitoken(segments):
            return False
        return True
    except Exception:
        # Fail-open — never eat a legitimate edge on a guardrail-internal error.
        return True


def filter_edges(
    edges: list[dict],
    *,
    dsn: str | None = None,
    schema: str | None = None,
) -> list[dict]:
    """Return only the edges whose ``rel_type`` passes :func:`is_relation_fit`.

    Convenience for the call sites that hold a list of candidate edges (the LLM
    ``/extract/rewrite`` output, the harvest builder). Order-preserving; never raises.
    """
    if not RELATION_FIT_GUARDRAIL:
        return edges
    out: list[dict] = []
    for e in edges or []:
        try:
            if is_relation_fit(e.get("rel_type", ""), dsn=dsn, schema=schema):
                out.append(e)
        except Exception:
            out.append(e)  # fail-open per-edge
    return out
