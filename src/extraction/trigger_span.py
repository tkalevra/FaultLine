"""Trigger-span detection — find fact-bearing spans, hand them to GLiNER2 (the strong extractor).

The detection layer of DEV/DESIGN-trigger-span-gliner2-extraction.md. This module is PURE
(regex only, no LLM, no GLiNER2, <1ms): it loads `category='trigger'` SIGNAL regexes from the
`extraction_patterns` table and returns the sentences of a message that look fact-bearing. The
caller (main.extract_rewrite) hands those spans to `gliner_model.extract_relations()`.

PITFALL 11: triggers are SEGMENTATION regexes, not GLiNER2 labels. They never touch GLiNER2's
label strings. Grow this regex zoo freely — it changes WHERE GLiNER2 looks, never WHAT it scores.
"""
import os
import re

# Cache of compiled trigger regexes (populated at first use).
_TRIGGER_CACHE: list[re.Pattern] | None = None

# Sentence splitter — same intent as main._split_sentences (kept local to stay import-light).
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+')


def _load_trigger_patterns() -> list[re.Pattern]:
    """Load active `category='trigger'` regexes from extraction_patterns (cached).

    Reads with the default search_path (public seed) — triggers are universal segmentation
    signals seeded identically into every tenant, so the public copy is authoritative.
    Fails soft to an empty list (extraction continues via the LLM path).
    """
    global _TRIGGER_CACHE
    if _TRIGGER_CACHE is not None:
        return _TRIGGER_CACHE

    patterns: list[re.Pattern] = []
    try:
        import psycopg2
        dsn = os.environ.get("POSTGRES_DSN", "postgresql://faultline:faultline@localhost:5432/faultline")
        db = psycopg2.connect(dsn)
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT pattern_regex FROM extraction_patterns "
                    "WHERE is_active = true AND category = 'trigger'"
                )
                for (rx,) in cur.fetchall():
                    try:
                        patterns.append(re.compile(rx, re.IGNORECASE))
                    except re.error as e:
                        print(f"[WARNING] invalid trigger regex skipped: {e}")
        finally:
            db.close()
    except Exception as e:
        print(f"[WARNING] failed to load trigger patterns: {e}")

    _TRIGGER_CACHE = patterns
    return patterns


def reset_trigger_cache() -> None:
    """Clear the trigger cache (called by /internal/refresh-intent-pattern-caches)."""
    global _TRIGGER_CACHE
    _TRIGGER_CACHE = None


def find_factbearing_spans(text: str) -> list[str]:
    """Return the sentences of `text` that match ANY trigger signal (deduped, order-preserved).

    These are the focused spans worth handing to GLiNER2's relation extractor. Empty list when
    no signal fires (caller then relies on the LLM extraction alone).
    """
    if not text or not text.strip():
        return []
    triggers = _load_trigger_patterns()
    if not triggers:
        return []
    spans: list[str] = []
    seen: set[str] = set()
    for sent in _SENT_SPLIT.split(text.strip()):
        s = sent.strip()
        if not s or s.lower() in seen:
            continue
        if any(t.search(s) for t in triggers):
            seen.add(s.lower())
            spans.append(s)
    return spans
