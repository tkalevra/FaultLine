"""Class-C / Qdrant short-term recall strengthening.

This module makes the Class-C (Qdrant) short-term lane a REAL backstop for legit
short-term facts. It is ADDITIVE and confined to the vector lane — it never touches
the deterministic PostgreSQL A/B walk, which remains authoritative and always
overrides/strengthens the C lane (see CLAUDE.md, "Query Path").

Two techniques are borrowed from mnemory (fpytloun):

  1. MULTI-QUERY EXPANSION — instead of a single cosine query, generate a few
     semantic angles of the recall query, embed + search each against Qdrant, and
     UNION the hit sets. This catches a legit C fact a single phrasing would miss.
     • Deterministic by default (no LLM): lightweight, subject-agnostic surface
       variants of the query string (question→declarative stripping, lead-word
       trimming). NO fact is invented — only the QUERY STRING is varied.
     • Optional LLM expansion (QDRANT_MULTIQUERY_LLM, default OFF): the LLM is asked
       ONLY to paraphrase the query into a few search angles. It never sees or emits
       facts — it rephrases the query string and nothing else. Any failure falls back
       to the deterministic variants. (HARD CONSTRAINT: the LLM expands query
       strings, never fabricates content.) The model identity is resolved via
       LLMModels.get() — model names live ONLY in the environment, never as a literal.

  2. RERANK by Reciprocal Rank Fusion (RRF) — the unioned hits from N sub-queries are
     re-scored by RRF over each sub-query's per-query rank, fused with the raw cosine
     score, so a fact that ranks well across MULTIPLE angles floats to the top before
     the held / less-certain render. Fully deterministic.

Subject-agnostic: no domain vocabulary, no hardcoded subjects/rel_types. Fail-safe:
any error returns the single-query result (or empty), recall never breaks.
"""

from __future__ import annotations

import os
import re

import structlog

log = structlog.get_logger()


# ── Flags (env-overridable; conservative defaults) ──────────────────────────────
def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, "true" if default else "false").strip().lower() not in (
        "false", "0", "no",
    )


# Multi-query expansion of the Class-C lane. Default ON — deterministic, cheap.
# When OFF, build_query_angles returns the single original angle → qdrant_semantic_search
# behavior is byte-identical to the legacy single-cosine path.
QDRANT_MULTIQUERY = _flag("QDRANT_MULTIQUERY", True)
# Use the LLM to paraphrase the query into extra angles (query-string only, never
# facts). Default OFF — the deterministic variants are enough and add zero LLM cost.
QDRANT_MULTIQUERY_LLM = _flag("QDRANT_MULTIQUERY_LLM", False)
# Max number of expansion angles (including the original). Kept small — each angle is
# an extra embed + Qdrant round-trip.
QDRANT_MULTIQUERY_MAX = int(os.environ.get("QDRANT_MULTIQUERY_MAX", "4"))
# RRF constant. Standard value; larger → flatter fusion.
QDRANT_RRF_K = int(os.environ.get("QDRANT_RRF_K", "60"))


# Lead words that carry recall intent but not retrieval signal — trimming them yields
# a declarative angle of the same query. Subject-agnostic, grammatical-surface only:
# these are question/command framings, NOT domain vocabulary.
_LEAD_FRAMING = (
    "what is", "what are", "what's", "whats", "who is", "who are", "who's",
    "tell me about", "tell me", "do you know", "do you remember", "remember",
    "recall", "what do you know about", "what do i", "what did i", "where is",
    "where are", "when is", "when did", "how is", "how are", "show me", "list",
    "give me", "can you tell me", "please", "about",
)


def _dedup_preserve(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        key = s.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(s.strip())
    return out


def deterministic_query_variants(query_text: str) -> list[str]:
    """Subject-agnostic surface variants of a recall query — NO LLM, NO fact invention.

    Produces a few angles of the SAME query string by stripping recall/question
    framing and trailing punctuation. The original always comes first.
    """
    base = (query_text or "").strip()
    if not base:
        return []
    variants = [base]

    lowered = base.lower()
    # Strip a leading question/command framing → declarative angle.
    for lead in sorted(_LEAD_FRAMING, key=len, reverse=True):
        if lowered.startswith(lead + " "):
            stripped = base[len(lead):].strip(" ?.!,")
            if stripped:
                variants.append(stripped)
            break

    # Strip trailing question mark / punctuation → a plainer angle.
    no_q = base.rstrip(" ?.!")
    if no_q and no_q != base:
        variants.append(no_q)

    # Content-word angle: drop the leading function word ("my", "the", "your"…) if the
    # remainder still has >=2 tokens. Grammatical surface, not a domain word-list.
    toks = re.findall(r"\w+", base)
    if len(toks) >= 3 and toks[0].lower() in (
        "my", "the", "your", "their", "his", "her", "our", "a", "an",
    ):
        variants.append(" ".join(base.split(" ")[1:]).strip(" ?.!,"))

    return _dedup_preserve(variants)[:QDRANT_MULTIQUERY_MAX]


def llm_query_variants(query_text: str, user_id: str = "anonymous") -> list[str] | None:
    """Ask the LLM to paraphrase the query into search angles — QUERY STRING ONLY.

    The LLM is explicitly constrained to rephrase the query and emit nothing else; it
    never sees facts and never invents content. The model identity is resolved via
    LLMModels.get() (model names are pure .env config — never a literal). Returns None
    on any failure so the caller falls back to the deterministic variants.
    """
    if not (query_text or "").strip():
        return None
    try:
        from .llm_calls import LLMModels, call_llm_with_retry_sync

        n = max(2, QDRANT_MULTIQUERY_MAX - 1)
        messages = [
            {
                "role": "system",
                "content": (
                    "You rewrite a memory-recall query into a few alternative phrasings "
                    "that mean the SAME thing, to improve vector search recall. "
                    "Rules: ONLY rephrase the query string. Do NOT answer it, do NOT add "
                    "facts, names, dates, or any information not present in the query. "
                    "Keep each phrasing short. "
                    'Return STRICT JSON: {"queries": ["...", "..."]}'
                ),
            },
            {"role": "user", "content": f"Query: {query_text}\nGive {n} alternative phrasings."},
        ]
        result = call_llm_with_retry_sync(
            messages=messages,
            model=LLMModels.get("QUERY_EXPANSION"),
            user_id=user_id,
            operation="QUERY_EXPANSION",
        )
        if not isinstance(result, dict):
            return None
        cands = result.get("queries")
        if not isinstance(cands, list):
            return None
        out = [query_text] + [c for c in cands if isinstance(c, str) and c.strip()]
        return _dedup_preserve(out)[:QDRANT_MULTIQUERY_MAX]
    except Exception as e:  # noqa: BLE001 — fail-safe, never break recall
        log.warning("qdrant_recall.llm_expansion_failed non-blocking", error=str(e)[:160])
        return None


def build_query_angles(query_text: str, user_id: str = "anonymous") -> list[str]:
    """Return the list of query angles to search (original first). Always >=1 element.

    Deterministic variants are the baseline; LLM paraphrases (query-string only) are
    layered on when QDRANT_MULTIQUERY_LLM is set and succeed. Union + dedup.

    When QDRANT_MULTIQUERY is OFF, returns just [original] so the caller's path is
    byte-identical to the legacy single-cosine search.
    """
    base = (query_text or "").strip()
    if not base:
        return []
    if not QDRANT_MULTIQUERY:
        return [base]

    angles = deterministic_query_variants(base)
    if QDRANT_MULTIQUERY_LLM:
        llm = llm_query_variants(base, user_id=user_id)
        if llm:
            angles = _dedup_preserve(angles + llm)
    return angles[:QDRANT_MULTIQUERY_MAX] or [base]


def _fact_key(fact: dict) -> tuple:
    """Stable identity for a Qdrant hit across sub-queries, for union + RRF fusion.

    Prefers (source_table, fact_id) — the same collision-safe key the rest of the
    pipeline uses. Falls back to the (subject, rel_type, object) triple for
    store_context rows that have no backing fact_id.
    """
    fid = fact.get("fact_id")
    st = fact.get("source_table")
    if fid is not None:
        return ("id", str(st or "facts"), str(fid))
    return (
        "triple",
        str(fact.get("subject") or "").lower(),
        str(fact.get("rel_type") or "").lower(),
        str(fact.get("object") or "").lower(),
    )


def fuse_multiquery_hits(
    per_query_hits: list[list[dict]],
    rrf_k: int = QDRANT_RRF_K,
) -> list[dict]:
    """Union N sub-query hit-lists and RERANK by Reciprocal Rank Fusion.

    RRF score = Σ over sub-queries of 1/(rrf_k + rank), where rank is the fact's
    0-based position within that sub-query's hit list. Fused with the raw cosine
    score as a tiebreak. A fact ranking well across MULTIPLE angles outranks a fact
    that scored high in only one. Returns hits sorted best-first; each kept fact's
    best cosine score is preserved as ``qdrant_score`` for downstream gates.
    """
    fused: dict[tuple, dict] = {}
    rrf: dict[tuple, float] = {}
    best_cos: dict[tuple, float] = {}

    for hits in per_query_hits:
        for rank, fact in enumerate(hits):
            key = _fact_key(fact)
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (rrf_k + rank)
            cos = float(fact.get("qdrant_score", 0.0) or 0.0)
            if key not in best_cos or cos > best_cos[key]:
                best_cos[key] = cos
                fused[key] = fact  # keep the representative with the best cosine

    ordered_keys = sorted(
        fused.keys(),
        key=lambda k: (rrf[k], best_cos.get(k, 0.0)),
        reverse=True,
    )
    out: list[dict] = []
    for k in ordered_keys:
        f = dict(fused[k])
        # Preserve the strongest cosine seen for this fact (used by the Class-C hit
        # threshold + confidence gate downstream — must remain a real cosine score).
        f["qdrant_score"] = best_cos.get(k, f.get("qdrant_score", 0.0))
        f["_rrf_score"] = rrf[k]
        out.append(f)
    return out
