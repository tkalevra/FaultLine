"""
Compound fact extraction — robust regex-based extraction for chained/compound text.
Runs as fallback when GLiNER2 produces sparse results and as augment for the filter.

Design principles:
- No external dependencies, no LLM calls, <1ms overhead
- Pattern-matching against known sentence structures
- Patterns are metadata-driven (queried from extraction_patterns table at startup)
- Produces EdgeInput-compatible dicts
- Never rejects — returns best-effort extraction, WGM gate validates downstream
"""

import re
from typing import Optional

# ── Stopwords ──────────────────────────────────────────────────────────────
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "not", "just", "also", "here", "happy", "glad", "sorry",
    "married", "single", "divorced", "engaged", "ready", "trying",
    "going", "looking", "back", "home", "out", "in", "on", "at", "to",
    "very", "really", "so", "too", "quite", "sure", "afraid", "aware",
    "excited", "glad", "grateful", "proud", "tired", "done",
    "who", "whom", "whose", "which", "with", "and", "our", "my",
    "three", "four", "five", "2", "3", "4", "5",
    "son", "daughter", "child", "children", "kid", "kids",
    "wife", "husband", "spouse", "partner",
    "name", "named", "called", "prefer", "prefers", "preferred",
    "she", "he", "they", "them", "her", "him", "his",
    "goes", "known", "likes", "like", "want", "wants",
    "family", "go", "please",
})


def _is_stopword(word: str) -> bool:
    return word.lower().strip() in _STOPWORDS


# ── Extraction patterns cache (loaded from database) ────────────────────────
# Cache is populated at first use of extract_compound_facts()
_EXTRACTION_PATTERNS_CACHE: list[tuple[re.Pattern, str]] = []


def _load_extraction_patterns() -> list[tuple[re.Pattern, str]]:
    """
    Load all active extraction patterns from database, sorted by confidence.
    Returns list of (compiled_pattern, rel_type) tuples.

    This replaces the hardcoded pattern dictionaries from pre-Migration 058.
    Patterns are evaluated at ingest time and scored by re_embedder Job 6.
    """
    global _EXTRACTION_PATTERNS_CACHE

    if _EXTRACTION_PATTERNS_CACHE:
        return _EXTRACTION_PATTERNS_CACHE

    # Import here to avoid circular dependency at module load time
    try:
        import psycopg2
        from os import environ

        dsn = environ.get('POSTGRES_DSN', 'postgresql://faultline:faultline@localhost:5432/faultline')
        db = psycopg2.connect(dsn)
        cur = db.cursor()

        # Query active compound patterns only — scalar_atomic category is handled
        # exclusively by _detect_atomic_values() in main.py at ingest time.
        # The category column is the metadata-driven boundary between the two
        # pattern populations; do not collapse them into one load path.
        cur.execute("""
            SELECT pattern_regex, rel_type
            FROM extraction_patterns
            WHERE is_active = true
              AND category != 'scalar_atomic'
            ORDER BY global_confidence DESC
        """)

        patterns = []
        for pattern_regex, rel_type in cur.fetchall():
            try:
                compiled = re.compile(pattern_regex, re.IGNORECASE)
                patterns.append((compiled, rel_type))
            except re.error as e:
                # Log but continue — invalid regex should not crash extraction
                print(f"[WARNING] Invalid regex in extraction_patterns (rel_type={rel_type}): {e}")

        cur.close()
        db.close()

        _EXTRACTION_PATTERNS_CACHE = patterns
        return patterns

    except Exception as e:
        # Fallback: if database unavailable, use empty cache
        # This allows extraction to continue with other methods
        print(f"[WARNING] Failed to load extraction_patterns from database: {e}")
        return []


def reset_extraction_patterns_cache() -> None:
    """
    Clear the extraction patterns cache to force reload on next use.
    Called by /internal/refresh-intent-pattern-caches endpoint after Job 6 updates.
    """
    global _EXTRACTION_PATTERNS_CACHE
    _EXTRACTION_PATTERNS_CACHE = []


def _has_correction_signal(text: str) -> bool:
    """Check if text contains correction signals (hardcoded for speed)."""
    correction_signals = {"actually", "not", "wrong", "incorrect", "innacurate", "update",
                         "sorry", "i meant", "correction", "mistake", "error"}
    return any(sig in text.lower() for sig in correction_signals)


# ── Property-to-rel_type mapping ────────────────────────────────────────────
# Kept for backward compatibility and fast property classification
# (not used by metadata-driven patterns, but may be needed for other code paths)
_PROPERTY_REL_MAP: dict[str, str] = {
    "hostname": "hostname",
    "fqdn": "fqdn",
    "ip": "ip_address",
    "ip address": "ip_address",
    "internal ip": "ip_address",
    "os": "instance_of",
    "operating system": "instance_of",
    "processor": "instance_of",
    "cpu": "instance_of",
    "ram": "has_ram",
    "memory": "has_ram",
    "storage": "has_storage",
    "hard drive": "has_storage",
    "disk": "has_storage",
    "certificate": "expires_on",
}

def _classify_property_rel(property_name: str) -> str:
    """Map a property name to a rel_type, falling back to 'related_to'."""
    key = property_name.lower().strip()
    if key in _PROPERTY_REL_MAP:
        return _PROPERTY_REL_MAP[key]
    # Partial matches
    for known, rel in _PROPERTY_REL_MAP.items():
        if known in key:
            return rel
    return "related_to"


# ── Main extraction ─────────────────────────────────────────────────────────

def extract_compound_facts(text: str) -> list[dict]:
    """
    Extract facts from compound/chained text using metadata-driven regex patterns.

    Patterns are loaded from extraction_patterns table at first call.
    Each pattern is matched against the text; matches produce EdgeInput dicts.

    Returns list of edge dicts: {subject, object, rel_type, is_preferred_label, is_correction}
    """
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    is_correction = _has_correction_signal(text)
    text_lower = text.lower()

    # Load patterns from database if not cached
    patterns = _load_extraction_patterns()

    def _add(subject: str, obj: str, rel_type: str, *, is_pref: bool = False) -> None:
        key = (subject.lower(), obj.lower(), rel_type.lower())
        if key in seen:
            return
        seen.add(key)
        edges.append({
            "subject": subject.lower(),
            "object": obj.lower(),
            "rel_type": rel_type.lower(),
            "is_preferred_label": is_pref,
            "is_correction": is_correction,
        })

    # ── Metadata-Driven Pattern Extraction ─────────────────────────────────
    # All patterns loaded from database, sorted by confidence
    # Each pattern is matched; groups are interpreted based on rel_type semantics

    for compiled_pattern, rel_type in patterns:
        for m in compiled_pattern.finditer(text):
            groups = m.groups()
            if not groups:
                # Safety net: scalar_atomic patterns are filtered at load time via
                # category != 'scalar_atomic'. Any remaining bare-match pattern here
                # is an authoring error in extraction_patterns — skip it.
                continue

            # Pattern-specific interpretation of captured groups
            # This logic mirrors the original hardcoded patterns but is generic
            try:
                if rel_type in ("also_known_as", "pref_name"):
                    # Naming/preference patterns. The SUBJECT is taken from the pattern's
                    # capture groups — never hardcoded — EXCEPT the first-person self-identity
                    # single-group patterns ("my name is X", "I am X"), where the only captured
                    # token IS the name and the subject is the first-person speaker.
                    #   1 group  → first-person self-identity: group 0 = name, subject = "user".
                    #   2 groups → subject AND object both captured:
                    #              e.g. "Jordan goes by emma"        → (jordan, emma)
                    #              e.g. "a dog named Rex"       → (dog,   rex)   [RC2]
                    #     group 0 is the HEAD NOUN being named (subject-agnostic: any common
                    #     noun — dog/cat/server/boat — captured by the naming construction in
                    #     migration 099), group 1 is the proper name. The subject is the captured
                    #     head noun, NOT "user". No capitalization gate on the subject here, so a
                    #     lowercase common-noun head ("dog") is honored.
                    if len(groups) == 1:
                        # Single-group pattern (first-person self-identity → subject is the speaker)
                        obj = groups[0]
                        if obj and not _is_stopword(obj) and len(obj) > 1:
                            is_pref = rel_type == "pref_name"
                            _add("user", obj.lower(), rel_type, is_pref=is_pref)
                    elif len(groups) == 2:
                        # Two-group pattern: honor the captured subject (head noun), never "user".
                        subj, obj = groups[0], groups[1]
                        if subj and obj and not _is_stopword(subj) and not _is_stopword(obj):
                            if len(subj) > 1 and len(obj) > 1:
                                is_pref = rel_type == "pref_name"
                                _add(subj.lower(), obj.lower(), rel_type, is_pref=is_pref)
                        elif not subj and obj:
                            # Bare object (e.g., "who prefers bob") — backtrack to find subject
                            if obj and not _is_stopword(obj) and len(obj) > 1:
                                before = text[:m.start()]
                                prev_names = re.findall(r'\b([A-Z][a-z]+)\b', before)
                                if prev_names:
                                    subj = prev_names[-1]
                                    if subj and not _is_stopword(subj) and len(subj) > 1:
                                        _add(subj.lower(), obj.lower(), rel_type, is_pref=True)

                elif rel_type == "spouse":
                    # Spouse patterns: find capitalized name in groups
                    for g in groups:
                        if g and g[0].isupper() and not _is_stopword(g) and len(g) > 1:
                            _add("user", g.lower(), rel_type)
                            break

                elif rel_type in ("age",):
                    # Age patterns: subject in group 0, age in group 1 (or just age if 1 group)
                    if len(groups) == 1:
                        # "I am N" — subject is "user"
                        _add("user", groups[0], rel_type)
                    elif len(groups) >= 2:
                        name, age_val = groups[0], groups[1]
                        if name and age_val and not _is_stopword(name) and len(name) > 1:
                            if name[0].isupper():
                                _add(name.lower(), age_val, rel_type)

                elif rel_type == "parent_of":
                    # Child patterns: capture subject or rely on "We have children" context
                    if len(groups) >= 1:
                        # Find capitalized name in groups
                        name = None
                        for g in groups:
                            if g and g[0].isupper() and not _is_stopword(g) and len(g) > 1:
                                name = g
                                break
                        if name:
                            _add("user", name.lower(), rel_type)

                else:
                    # Generic rel_types: first 1-2 groups are subject and object
                    if len(groups) == 1:
                        obj = groups[0]
                        if obj and len(obj) > 1:
                            _add("system", obj.lower(), rel_type)
                    elif len(groups) >= 2:
                        subj, obj = groups[0], groups[1]
                        if subj and obj and len(subj) > 1 and len(obj) > 1:
                            _add(subj.lower(), obj.lower(), rel_type)

            except (IndexError, AttributeError) as e:
                # Log but continue — pattern matched but group interpretation failed
                print(f"[WARNING] Pattern extraction failed for rel_type={rel_type}: {e}")

    # ── Post-processing: deduplicate same (subject, object) across different rel_types ──
    # e.g., "has_spec: ryzen 7" and "related_to: ryzen 7" — keep the more specific one.
    _REL_PRIORITY = {"related_to": 0, "has_spec": 0, "hostname": 1, "fqdn": 1,
                     "ip_address": 1, "instance_of": 1, "has_ram": 1,
                     "has_storage": 1, "expires_on": 1}
    _seen_pairs: dict[tuple[str, str], tuple[int, dict]] = {}
    for e in edges:
        pair = (e["subject"], e["object"])
        prio = _REL_PRIORITY.get(e["rel_type"], 0)
        if pair in _seen_pairs:
            if prio > _seen_pairs[pair][0]:
                _seen_pairs[pair] = (prio, e)
        else:
            _seen_pairs[pair] = (prio, e)
    deduped = [e for _, e in _seen_pairs.values()]

    return deduped
