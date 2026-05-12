"""
Compound fact extraction — robust regex-based extraction for chained/compound text.
Runs as fallback when GLiNER2 produces sparse results and as augment for the filter.

Design principles:
- No external dependencies, no LLM calls, <1ms overhead
- Pattern-matching against known sentence structures
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


# ── Self-identification ────────────────────────────────────────────────────
_IDENTITY_PATTERNS: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"\bmy\s+name\s+is\s+([A-Z][a-z]+)", re.IGNORECASE), "also_known_as", True),
    (re.compile(r"\bi\s+am\s+([A-Z][a-z]+)", re.IGNORECASE), "also_known_as", True),
    (re.compile(r"\bi'm\s+([A-Z][a-z]+)", re.IGNORECASE), "also_known_as", True),
    (re.compile(r"\bcall\s+me\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name", True),
    (re.compile(r"\bpeople\s+call\s+me\s+([A-Z][a-z]+)", re.IGNORECASE), "also_known_as", True),
]

# ── First-person preference ────────────────────────────────────────────────
_FIRST_PERSON_PREF_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Must NOT be preceded by "who" — those are third-person and handled below.
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefer\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bgoes\s+by\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"\bgo\s+by\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bpreferred\s+name\s+is\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"\bplease\s+call\s+me\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bknown\s+as\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    (re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\blike\s+to\s+(?:be|go)\s+(?:by|called)\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
    # NOTE: "wants to be called" is THIRD-person pattern — handled below.
    # First-person only: "I want to be called X"
    (re.compile(r"\bi\s+wants?\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE), "pref_name"),
]

# ── Third-person preference ────────────────────────────────────────────────
# "X prefers to be called Y" / "X, who prefers Y" / "X goes by Y"
_THIRD_PERSON_PREF_PATTERNS: list[re.Pattern] = [
    # "Marla, who prefers to be called Mars" / "Gabriella, age 10, who prefers Gabby"
    # Middle clause allows: ", age N, " or ", our son, " etc. between name and "who prefers"
    re.compile(r"([A-Z][a-z]+)(?:(?:,\s*age\s+\d+|,\s*our\s+(?:son|daughter|child)|,\s*a\s+(?:son|daughter|child))\s*,?\s*)?,?\s*who\s+prefers?\s+(?:to\s+be\s+called\s+)?([A-Z][a-z]+)", re.IGNORECASE),
    # "Marla prefers to be called Mars"
    re.compile(r"([A-Z][a-z]+)\s+prefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "Marla, who goes by Mars" / "Desmonde, age 12, who goes by Des"
    re.compile(r"([A-Z][a-z]+)(?:(?:,\s*age\s+\d+|,\s*our\s+(?:son|daughter|child)|,\s*a\s+(?:son|daughter|child))\s*,?\s*)?,?\s*who\s+goes\s+by\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "Marla goes by Mars" (must NOT match when preceded by "who " — caught above)
    re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )([A-Z][a-z]+)\s+goes\s+by\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "Marla, known as Mars"
    re.compile(r"([A-Z][a-z]+)\s*,\s*known\s+as\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "who prefers Gabby" (bare preference, no "to be called")
    re.compile(r"who\s+prefers?\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "she wants to be called Thumbelina" / "he wants to be called X"
    re.compile(r"(?:she|he|it)\s+wants?\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "she prefers to be called Mars" / "he prefers to be called X"
    re.compile(r"(?:she|he|it)\s+prefers?\s+to\s+be\s+called\s+([A-Z][a-z]+)", re.IGNORECASE),
]

# ── Marriage ────────────────────────────────────────────────────────────────
_MARRIAGE_PATTERNS: list[re.Pattern] = [
    # "I am married to Marla"
    re.compile(r"\b(?:i\s+am|i'm)\s+married\s+to\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "married to Marla"
    re.compile(r"\bmarried\s+to\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "my wife Marla" / "my husband X"
    re.compile(r"\bmy\s+(wife|husband|spouse|partner)\s+([A-Z][a-z]+)", re.IGNORECASE),
    # "Marla is my wife"
    re.compile(r"([A-Z][a-z]+)\s+is\s+my\s+(wife|husband|spouse|partner)", re.IGNORECASE),
]

# ── Children ────────────────────────────────────────────────────────────────
# "We have 3 children, a daughter Gabriella, ... Cyrus, our son is 19, and a son named Desmonde"
# Strategy: detect the "children" clause, then scan for named entities that follow
_CHILDREN_CLAUSE: re.Pattern = re.compile(
    r"(?:we\s+have|have)\s+(?:\d+\s+)?(?:children|kids)",
    re.IGNORECASE
)

# Individual child patterns (run on text after children clause)
_CHILD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "a daughter Gabriella" / "a son Cyrus"
    (re.compile(r"a\s+(daughter|son|child)\s+([A-Z][a-z]+)", re.IGNORECASE), "parent_of"),
    # "our son is 19" — already handled by age patterns, but extract name
    (re.compile(r"our\s+(daughter|son|child)\s+(?:is\s+)?(?:named\s+)?([A-Z][a-z]+)", re.IGNORECASE), "parent_of"),
    # "a son named Desmonde"
    (re.compile(r"a\s+(daughter|son|child)\s+named\s+([A-Z][a-z]+)", re.IGNORECASE), "parent_of"),
    # "daughter Gabriella"
    (re.compile(r"(daughter|son|child)\s+([A-Z][a-z]+)", re.IGNORECASE), "parent_of"),
    # bare capitalized name after commas in children list
    (re.compile(r",\s+(?:and\s+)?(?:a\s+)?(?:daughter|son|child)\s+(?:named\s+)?([A-Z][a-z]+)", re.IGNORECASE), "parent_of"),
]

# ── Age ─────────────────────────────────────────────────────────────────────
_AGE_PATTERNS: list = [
    # "Gabriella, age 10" / "Desmonde, age 12"
    re.compile(r"([A-Z][a-z]+)\s*,\s*age\s+(\d+)", re.IGNORECASE),
    # "Gabriella age 10"
    re.compile(r"([A-Z][a-z]+)\s+age\s+(\d+)", re.IGNORECASE),
    # "X is N" / "X, our son, is N" — greedy, stopword-filtered post-match
    re.compile(r"([A-Z][a-z]+)(?:[\s,]+(?:our|a)\s+(?:son|daughter|child))?\s+is\s+(\d+)", re.IGNORECASE),
    # "I am 35" → special: subject is "user"
    re.compile(r"\bi\s+am\s+(\d+)\s*(?:years?\s*old)?", re.IGNORECASE),
]

# ── Correction signals ─────────────────────────────────────────────────────
_CORRECTION_SIGNALS: frozenset[str] = frozenset({
    "actually", "not", "wrong", "incorrect", "innacurate", "update",
    "sorry", "i meant", "correction", "mistake", "error",
})


def _has_correction_signal(text: str) -> bool:
    return any(sig in text.lower() for sig in _CORRECTION_SIGNALS)


# ── Generic entity-property extraction ─────────────────────────────────────
# Domain-agnostic patterns for "the X is Y", "running X", "X expires on Y", etc.
# These catch technical, scientific, mechanical, and infrastructure facts
# that the family-specific patterns below don't handle.

# "the hostname is Aurora" / "the system is a Ryzen 7" / "the ip is 192.168.40.20"
_GENERIC_IS_PATTERN: re.Pattern = re.compile(
    r"the\s+([\w\s]+?)\s+is\s+(?:a\s+)?([\w.]+(?:\s+[\w.]+)*?)(?=\s*(?:,|\.(?:\s+|$)|$|\s+with\s|\s+running\s|\s+and\s+the|\s+and\s+a|\s+the\s+|\s*$))",
    re.IGNORECASE
)
# "running Windows 11"
_GENERIC_RUNNING_PATTERN: re.Pattern = re.compile(
    r"running\s+(.+?)(?=\s*(?:,|\.|$|\s+the\s+))",
    re.IGNORECASE
)
# "the certificate expires on November 27th 2026"
_GENERIC_EXPIRES_PATTERN: re.Pattern = re.compile(
    r"certificate\s+expires?\s+on\s+(.+?)(?=\s*(?:,|\.|$))",
    re.IGNORECASE
)
# "fqdn of aurora.helpdeskpro.ca"
_GENERIC_FQDN_PATTERN: re.Pattern = re.compile(
    r"fqdn\s+of\s+(\S+(?:\.\S+)*)",
    re.IGNORECASE
)
# "with 64Gb of ram" / "a 2TB M.2 Hard drive"
_GENERIC_WITH_PATTERN: re.Pattern = re.compile(
    r"(?:with|has)\s+(.+?)\s+of\s+(.+?)(?=\s*(?:,|\.|$|\s+and|\s+the\s+))",
    re.IGNORECASE
)
_GENERIC_A_PATTERN: re.Pattern = re.compile(
    r",?\s*a\s+(.+?)(?=\s*(?:,|\.(?:\s+|$)|$|\s+and\s+the|\s+the\s+|\s+running))",
    re.IGNORECASE
)

# Property-to-rel_type mapping for common technical properties
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
    Extract facts from compound/chained text using regex patterns.
    Returns list of edge dicts: {subject, object, rel_type, is_preferred_label, is_correction}
    """
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    is_correction = _has_correction_signal(text)
    text_lower = text.lower()

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

    # ── 0. Generic entity-property extraction ───────────────────────────
    # Domain-agnostic patterns: "the X is Y", "running X", "X expires on Y", etc.
    # These run FIRST so domain-specific patterns can override if needed.
    # Determine the primary subject (e.g., "the system") from context.
    primary_subject = "system"  # default
    sys_match = re.search(r"the\s+(\w[\w\s]*?)\s+is\s+(?:a\s+)?", text, re.IGNORECASE)
    if sys_match:
        raw = sys_match.group(1).strip().lower()
        if raw and raw not in ("hostname","ip","fqdn","certificate","internal ip","ip address"):
            primary_subject = raw

    # "the hostname is Aurora" / "the system is a Ryzen 7" / "the ip is 192.168.40.20"
    for m in _GENERIC_IS_PATTERN.finditer(text):
        prop = m.group(1).strip().lower()
        val = m.group(2).strip().lower()
        if prop and val and len(val) > 1:
            # Skip if it looks like an age or family pattern (already handled below)
            if prop in ("name",) or val.isdigit():
                continue
            rel = _classify_property_rel(prop)
            _add(primary_subject, val, rel)

    # "running Windows 11"
    for m in _GENERIC_RUNNING_PATTERN.finditer(text):
        val = m.group(1).strip().lower()
        if val and len(val) > 1:
            _add(primary_subject, val, "instance_of")

    # "the certificate expires on November 27th 2026"
    for m in _GENERIC_EXPIRES_PATTERN.finditer(text):
        val = m.group(1).strip().lower()
        if val and len(val) > 1:
            _add(primary_subject, val, "expires_on")

    # "fqdn of aurora.helpdeskpro.ca"
    for m in _GENERIC_FQDN_PATTERN.finditer(text):
        val = m.group(1).strip().lower().rstrip(',.')
        if val and len(val) > 1:
            _add(primary_subject, val, "fqdn")

    # "with 64Gb of ram"
    for m in _GENERIC_WITH_PATTERN.finditer(text):
        val_spec = m.group(1).strip().lower()
        prop = m.group(2).strip().lower()
        if val_spec and prop and len(val_spec) > 1 and not val_spec.isdigit():
            rel = _classify_property_rel(prop)
            _add(primary_subject, val_spec, rel)

    # "a 2TB M.2 Hard drive" (standalone spec after comma)
    for m in _GENERIC_A_PATTERN.finditer(text):
        val = m.group(1).strip().lower()
        # Only match if it looks like a tech spec (contains numbers or known terms)
        if val and len(val) > 1 and not val.startswith("daughter") and not val.startswith("son"):
            if re.search(r'\d', val) or any(t in val for t in ("tb","gb","mb","m.2","ssd","hdd","drive","ram","cpu","ryzen","intel","amd")):
                _add(primary_subject, val, "has_spec")

    # ── 1. Self-identification ──────────────────────────────────────────
    for pat, rel, is_pref in _IDENTITY_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1)
            if not _is_stopword(name) and len(name) > 1:
                _add("user", name, rel, is_pref=is_pref)
                break  # first match per pattern wins

    # ── 2. First-person preference ──────────────────────────────────────
    for pat, rel in _FIRST_PERSON_PREF_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1)
            if not _is_stopword(name) and len(name) > 1:
                _add("user", name, rel, is_pref=True)
                break

    # ── 3. Marriage ─────────────────────────────────────────────────────
    for pat in _MARRIAGE_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            # Find the name (group that's a capitalized word)
            for g in groups:
                if g and g[0].isupper() and not _is_stopword(g) and len(g) > 1:
                    _add("user", g, "spouse")
                    break
            break  # one spouse

    # ── 4. Third-person preferences ─────────────────────────────────────
    for pat in _THIRD_PERSON_PREF_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            if len(groups) == 2:
                subject, pref_name = groups[0], groups[1]
                if subject and pref_name and not _is_stopword(subject) and not _is_stopword(pref_name):
                    if len(subject) > 1 and len(pref_name) > 1:
                        _add(subject, pref_name, "pref_name", is_pref=True)
            elif len(groups) == 1:
                # "who prefers Gabby" — only captured the preferred name.
                # Scan backward from match position to find the nearest
                # capitalized word (the entity this preference belongs to).
                pref_name = groups[0]
                if pref_name and not _is_stopword(pref_name) and len(pref_name) > 1:
                    before = text[:m.start()]
                    # Find last capitalized word before the match
                    prev_names = re.findall(r'\b([A-Z][a-z]+)\b', before)
                    if prev_names:
                        subject = prev_names[-1]  # nearest preceding name
                        if subject and not _is_stopword(subject) and len(subject) > 1:
                            _add(subject, pref_name, "pref_name", is_pref=True)

    # ── 5. Ages ─────────────────────────────────────────────────────────
    for pat in _AGE_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            if len(groups) == 1:
                # "I am N" pattern — subject is "user"
                age_val = groups[0]
                _add("user", age_val, "age")
            elif len(groups) >= 2:
                name = groups[0]
                age_val = groups[1]
                # Only accept if name looks like a proper noun (first letter uppercase
                # in the actual text). Prevents "ip is 192" false matches.
                if name and age_val and not _is_stopword(name) and len(name) > 1:
                    if name[0].isupper():
                        _add(name, age_val, "age")

    # ── 6. Children — parent_of ─────────────────────────────────────────
    # Build set of preference names first — these are NOT separate children.
    _pref_names: set[str] = {e["object"].lower() for e in edges if e["rel_type"] == "pref_name"}
    _spouse_names: set[str] = {e["object"].lower() for e in edges if e["rel_type"] == "spouse"}

    # Find all named entities that appear to be children.
    # Strategy: detect the children clause, then scan the rest for names.
    children_clause_match = _CHILDREN_CLAUSE.search(text)
    if children_clause_match:
        # Scan text from the children clause onward for child patterns
        tail = text[children_clause_match.end():]
        for pat, rel in _CHILD_PATTERNS:
            for m in pat.finditer(tail):
                # The captured name might be in group 2 (if group 1 is daughter/son)
                groups = m.groups()
                name = None
                for g in groups:
                    if g and g[0].isupper() and not _is_stopword(g) and len(g) > 1:
                        name = g
                        break
                if name and name.lower() not in _pref_names and name.lower() not in _spouse_names:
                    _add("user", name, "parent_of")

        # Fallback: extract all capitalized words after the children clause.
        # Exclude: stopwords, preference names, spouse names, known age values.
        raw_names = re.findall(r'\b([A-Z][a-z]+)\b', tail)
        for name in raw_names:
            nl = name.lower()
            if len(name) > 1 and not _is_stopword(name) \
               and nl not in _pref_names \
               and nl not in _spouse_names:
                key_check = ("user", nl, "parent_of")
                if key_check not in seen:
                    _add("user", name, "parent_of")

    # ── 7. Sibling relationships ────────────────────────────────────────
    # If we have multiple children, add sibling_of between them
    children = [e["object"] for e in edges if e["rel_type"] == "parent_of" and e["subject"] == "user"]
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            _add(children[i], children[j], "sibling_of")

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
