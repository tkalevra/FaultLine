"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.3.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

import json
import os
import re
import time as _time
from collections import defaultdict
from typing import Callable, Optional

import httpx
from pydantic import BaseModel


_REALTIME_SIGNALS: frozenset[str] = frozenset({
    "weather", "forecast", "temperature", "news", "today", "current",
    "right now", "live", "stock", "price", "score",
})

# Identity/preference query detection (dBug-022). Used to gate whether
# preferred-name directives are injected into the memory block. Preferences
# are always used internally for canonical identity; exposing them to the
# LLM is gated on the user directly asking about their identity.
_IDENTITY_QUERIES: frozenset[str] = frozenset({
    "what is my name", "what's my name", "whats my name",
    "who am i", "who i am",
    "how should people call me", "how should you call me",
    "what do you call me", "what should i be called",
    "my preferred name", "my alternate name",
    "what names do i have", "do i have a nickname",
    "what do i go by", "what should i go by",
    "who should i introduce myself as",
    "tell me about my identity", "tell me about my name",
    "what is my preferred identity",
})


_RETRACTION_SIGNALS: frozenset[str] = frozenset({
    "forget", "delete", "remove", "retract", "erase",
    "that's wrong", "thats wrong", "that was wrong", "not true",
    "that's not right", "thats not right", "incorrect", "no longer",
    "remove from memory", "forget that", "don't remember",
    "that information is wrong", "that info is wrong",
})

_IDENTITY_RE = re.compile(
    r"\b(my name is|i am|i'm|call me|people call me)\s+[a-z]+", re.IGNORECASE
)

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

# Semantic intent classification (dprompt-75b / dBug-014): replaces brittle
# _IS_PURE_QUESTION regex. Distinguishes pure factual questions from personal-
# context questions by analyzing grammatical person. If a question uses first-
# person language ("I", "my", "me", etc.), it's personal context — don't skip
# extraction. Pure third-person/general questions can safely skip.

def _should_skip_extraction(text: str) -> bool:
    """Return True if extraction can be skipped (pure factual question).
    Return False if text contains personal context worth extracting.

    Semantic approach: grammatical person is the signal. First-person questions
    are about the user's own history/state/context — must extract. Third-person
    or impersonal questions are about world knowledge — can skip.
    """
    tl = text.lower().strip()
    # Only consider skipping question-form messages
    if not tl.endswith('?'):
        return False
    # First-person pronouns indicate personal context — never skip
    for raw_token in tl.split():
        token = raw_token.strip('.,!?;:\'"()[]{}')
        if token in ('i', 'my', 'me', 'we', 'our', 'us', 'myself', 'ourselves'):
            return False
    # No personal context detected — safe to skip extraction for this question
    return True

# Session memory cache — keyed by user_id, value: (timestamp, facts, preferred_names, canonical_identity, entity_attributes)
_SESSION_MEMORY_CACHE: dict[str, tuple] = {}
_SESSION_MEMORY_TTL: int = 30  # seconds

# Deduplication tracker — prevents the inlet from processing the same text repeatedly.
# OpenWebUI may call the filter multiple times for the same message (streaming chunks,
# system message injection triggers, etc.). Without dedup, each call produces another
# memory injection, which triggers another filter call — a recursive loop.
# Key: user_id, Value: (last_text_hash, last_processed_at_timestamp)
_DEDUP_TRACKER: dict[str, tuple[int, float]] = {}
_DEDUP_WINDOW: float = 5.0  # seconds — ignore duplicate text within this window

# Conversation context — per-user pronoun/entity tracking across turns
_CONVERSATION_CONTEXT: dict[str, dict] = {}
_CONVERSATION_MAX_ENTITIES: int = 10  # prune entity mentions beyond this

_RETRACTION_PROMPT = """\
You are a retraction extractor for a personal knowledge graph.
The user wants to remove or correct a stored fact.
Output ONLY a raw JSON object. No markdown, no explanation.

Fields:
- "subject": the entity the fact is about. If the user means themselves, use "user".
- "rel_type": snake_case relationship type (e.g. lives_at, works_for, also_known_as, has_pet, owns, spouse, occupation). Omit if unknown.
- "old_value": the specific incorrect/outdated value. Omit if unknown or if user wants all facts of that type removed.

Common rel_types: lives_at, lives_in, works_for, occupation, also_known_as, pref_name, has_pet, owns, spouse, likes, dislikes, located_in, age, height, weight.

Examples:
"forget that I live at my old address" → {"subject": "user", "rel_type": "lives_at"}
"delete my work information" → {"subject": "user", "rel_type": "works_for"}
"that info about my family member is wrong" → {"subject": "family_member_name"}
"remove the fact about my pet" → {"subject": "pet_name", "rel_type": "has_pet"}
"forget all that" → {}

Output: {} if nothing specific can be extracted."""


_TRIPLE_SYSTEM_PROMPT = """\
You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array. No markdown, no explanation, no code fences.

ENTITY RULES:
- NEVER use pronouns (i/me/my/we/our/he/she/her/his/they) as subject or object.
- Third-person pronouns refer to the nearest named entity, NEVER to the user.
- Entity names must be proper nouns or named entities only, lowercase.
- First-person (I/my/we) → subject="user" unless a named entity is established.

RELATIONSHIP RULES:
- parent_of: subject IS the parent. child_of: subject IS the child.
- NEVER emit child_of with the speaker as subject. Use parent_of instead.
- Siblings share a parent — emit sibling_of between them, not parent_of/child_of.
- For "X and Y are children of Z": Z parent_of X, Z parent_of Y, X sibling_of Y.
- POSSESSIVE FORMS: "my wife's name is X", "my husband is X", "my son is Y" → ALWAYS emit spouse/child_of FIRST, then separately emit also_known_as for the name. Example: "my wife's name is Marla" → (user, spouse, marla) AND (marla, also_known_as, marla) if needed.
- BIDIRECTIONAL EMISSION: For inverse rel_types (parent_of/child_of, spouse, sibling_of), ALWAYS emit BOTH directions as separate facts. If you emit (user, parent_of, des), you MUST also emit (des, child_of, user). If you emit (user, spouse, mars), you MUST also emit (mars, spouse, user). If you emit (des, sibling_of, cyrus), you MUST also emit (cyrus, sibling_of, des). Example: "I have a son named Des, my husband Mars" → (user, parent_of, des) + (des, child_of, user) + (user, spouse, mars) + (mars, spouse, user). This ensures the graph is complete in both directions.

REL_TYPE REFERENCE:
- also_known_as: nickname or alternate name.
- pref_name: explicitly preferred name ("goes by", "prefers to be called", "preferred name is"). For first-person preferences ("I prefer to be called X", "call me X"), subject IS "user". For third-person ("she goes by X", "his preferred name is Y"), subject is the named person.
- has_pet: person owns an animal (NEVER a person).

HIERARCHY RELATIONSHIPS — extract these whenever you see type/classification/part-of patterns. They appear in every domain and are as important as family relationships:
- instance_of: entity IS a specific type or breed ("Fraggle is a morkie" → fraggle instance_of morkie)
- subclass_of: type IS a subclass of another ("a morkie is a kind of dog" → morkie subclass_of dog)
- member_of: entity belongs to a group or taxonomy ("my pets are family" → pets member_of family)
- part_of: entity is a component of a larger whole ("Engineering dept of TechCorp" → engineering part_of techcorp)
- is_a: type or category (deprecated; prefer instance_of or subclass_of).

Hierarchy chains across domains (extract EVERY link in the chain):
- Taxonomic: "I have a dog named Fraggle, a morkie" → fraggle instance_of morkie, morkie subclass_of dog, dog subclass_of animal
- Organizational: "Alice is an engineer in Engineering at TechCorp" → alice instance_of engineer, engineer member_of engineering, engineering part_of techcorp
- Infrastructure: "Server 192.168.1.1 is in subnet 192.168.1.0/24 on the main network" → 192.168.1.1 part_of subnet_192_168_1, subnet_192_168_1 part_of network_main
- Hardware: "Core 0 is in CPU 1 on motherboard A in server X" → core_0 instance_of cpu_core, cpu_core part_of cpu_1, cpu_1 part_of motherboard_a
- Geographical: "Toronto is in Ontario, Canada" → toronto instance_of city, city part_of ontario, ontario part_of canada
- Software: "The Logger module is in the Monitoring component of the System" → logger part_of monitoring, monitoring part_of system

HIERARCHY CONSTRAINT: When you extract instance_of/subclass_of/member_of/part_of for an entity, the OBJECT of that hierarchy relationship is a TYPE or CATEGORY — NOT a separate entity. Do NOT also extract owns/has_pet/works_for/lives_in for the type entity. Example: "I have a dog named Fraggle, a morkie" → extract fraggle instance_of morkie AND user has_pet fraggle, but NOT user owns morkie. Morkie is a breed, not a separate pet. Same principle applies across all domains — "engineer" is a role, not a person you work with; "Ontario" is a province container, not a separate location you live in.

Common: spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, anniversary_on, met_on, instance_of, subclass_of, member_of, part_of.
- Use snake_case. Other types allowed if none fit.

SELF-ID: Explicit first-person self-identification only ("I am X", "my name is X", "call me X"):
→ {"subject":"user","object":"x","rel_type":"also_known_as","low_confidence":false}
NEVER apply to third-person text. NEVER emit subject="user" from "she/he prefers...".

CORRECTIONS: If text signals a correction ("actually", "not X it's Y", "I meant"):
→ add "is_correction":true to the corrected triple.

UNITS: age→number only. height→feet format (6ft, 5'10"). weight→pounds (230lb).
For age: subject is the PERSON whose age is being stated, NEVER 'user' unless explicitly "I am X years old".
'My son Des is 12' → subject='des', object='12', rel_type='age'.

DATES AND EVENTS:
- NEVER emit spouse, met, married facts as relationship edges when a date is involved.
  Emit the date as a separate event fact FIRST, then emit relationships separately.
- Birthday patterns ("born on X", "my birthday is Y", "X's birthday is Z"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on"}.
  Date formats: "may 3", "june 10, 1990", "15th", "1988", "june" (month only).
- Anniversary patterns ("our anniversary is X", "X anniversary is Y"):
  emit {"subject":"user" or entity,"object":"<date>","rel_type":"anniversary_on"}.
- Meeting dates ("we met on X", "we first met on X"):
  emit {"subject":"user","object":"<date>","rel_type":"met_on"}.
- Marriage/wedding dates ("married on X", "got married on X"):
  emit {"subject":"user","object":"<date>","rel_type":"married_on"}.
- One-time events (appointments, deadlines) with future/past relevance:
  emit {"subject":"<entity>","object":"<date>","rel_type":"appointment_on"}.
- Compound date+age ("I'm 25, born on May 3"):
  emit BOTH (user, age, "25") AND (user, born_on, "may 3").
- Corrections ("Actually born June 3, not May 3"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on","is_correction":true}.
- Fuzzy/partial dates ("sometime in 1990", "around May"):
  emit as-is with "low_confidence":true.
- Day-only patterns ("my birthday is the 3rd"):
  emit "3rd" as the date.
- NEVER emit relative dates ("next week", "last month") — omit entirely.
- Date values must be the date string only — never a name or description.

ENTITY TYPES: If entity types were pre-classified (shown as "GLiNER2 has pre-classified"), include them in output:
- subject_type: Person|Animal|Organization|Location|Object|Concept
- object_type: Person|Animal|Organization|Location|Object|Concept
Preserve the types exactly as classified. Do not invent new types.

OUTPUT: [{"subject":"...","subject_type":"...","object":"...","object_type":"...","rel_type":"...","low_confidence":false}]
subject_type and object_type must be included when available. If no pre-classification, omit or use null.
If nothing to extract: []"""


def _resolve_llm_config(valves, body: dict) -> tuple[str, str]:
    """
    Resolve the LLM model and endpoint to use for extraction calls.
    Valve override takes priority; falls back to user's selected model and
    OpenWebUI's internal endpoint.
    # NO RECURSIVE MATCHING
    """
    model = valves.LLM_MODEL if valves.LLM_MODEL else body.get("model", "default")

    # Standard setup: use OpenWebUI's internal endpoint
    # Custom setup: use explicitly configured LLM_URL
    if valves.LLM_URL:
        url = valves.LLM_URL
    else:
        # Default to OpenWebUI's internal LLM endpoint
        url = "http://host.docker.internal:3000/api/chat/completions"

    return model, url


async def rewrite_to_triples(text: str, valves, model: str, url: str, auth_header: Optional[str] = None, context: list[dict] = None, typed_entities: list[dict] = None, memory_facts: list[dict] = None, user_uuid: Optional[str] = None) -> list[dict]:
    """
    Send text to the Qwen model and parse the returned JSON triple array.
    Context (prior messages) provides conversation history for resolution.
    Memory_facts provides stored facts for pronoun resolution.
    Returns [] on any failure so the caller can handle the empty-edge case.
    """
    try:
        messages = [{"role": "system", "content": _TRIPLE_SYSTEM_PROMPT}]

        if context:
            prior_turns = []
            for msg in context[-valves.MAX_CONTEXT_TURNS:]:
                role = msg.get("role")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") for p in content if isinstance(p, dict)
                        )
                    # Guard against FaultLine self-feedback loop in context
                    # # NO RECURSIVE MATCHING — markers checked against pre-extracted content string only
                    _FEEDBACK_MARKERS = (
                        "⊢ FaultLine Memory",
                        "GLiNER2 has pre-classified",
                    )
                    if content.strip() and not any(marker in content for marker in _FEEDBACK_MARKERS):
                        prior_turns.append({"role": role, "content": content})
            if prior_turns:
                turns_to_add = prior_turns[:-1] if (
                    prior_turns[-1]["role"] == "user"
                ) else prior_turns
                messages.extend(turns_to_add)

        if memory_facts:
            _PREF_RELS = {"spouse", "parent_of", "child_of", "also_known_as", "pref_name", "sibling_of"}
            _priority = [f for f in memory_facts if f.get("rel_type") in _PREF_RELS]
            _other = [f for f in memory_facts if f.get("rel_type") not in _PREF_RELS]
            memory_facts = (_priority + _other)[:10]
            entity_lines = []
            family_members = []
            for f in memory_facts:
                subj = f.get("subject", "")
                obj = f.get("object", "")
                rel = f.get("rel_type", "")
                if subj and obj and rel:
                    # Use USER placeholder for the canonical user identity
                    subj_display = "USER" if subj in ("user",) else subj
                    obj_display = "USER" if obj in ("user",) else obj
                    entity_lines.append(f"- {obj_display} ({rel} of {subj_display})")
                    # Track family relationships for attribute resolution
                    if rel == "parent_of" and subj in ("user",):
                        family_members.append(f"child: {obj}")
                    elif rel == "child_of" and obj in ("user",):
                        family_members.append(f"parent: {subj}")
            if entity_lines:
                hint = (
                    "Known entities (for pronoun resolution only — do not store these as new facts):\n"
                    + "\n".join(entity_lines)
                )
                messages.append({"role": "system", "content": hint})
            # Add family context hint for attribute queries
            if family_members:
                family_hint = f"Known family relationships: {', '.join(family_members)}. When asked about attributes of named family members, extract them with the family member's name as subject."
                messages.append({"role": "system", "content": family_hint})

        user_content = text
        if typed_entities:
            entity_lines = "\n".join(
                f"- {e.get('subject')} (type: {e.get('subject_type', 'unknown')})"
                f" -- {e.get('object')} (type: {e.get('object_type', 'unknown')})"
                for e in typed_entities
                if e.get("subject") and e.get("object")
            )
            user_content = (
                f"{text}\n\n"
                f"Entities detected in text (types pre-classified):\n{entity_lines}\n"
                f"Use these entity TYPES to constrain subject/object type matching. "
                f"Determine the specific relationship from the user's text, not from this list. "
                f"A Person cannot be owned. An Animal cannot be a spouse. "
                f"Respect these type constraints strictly."
            )
        messages.append({"role": "user", "content": user_content})

        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header

        # Use backend LLM if configured, else OpenWebUI with user UUID
        # Special handling: "default" or empty string means use standard OpenWebUI endpoint
        backend_url = valves.BACKEND_LLM_URL if valves.BACKEND_LLM_URL else None
        if backend_url and backend_url.lower() in ("default", "standard", "auto"):
            backend_url = None  # Reset to standard

        final_url = backend_url if backend_url else url

        # Validate URL has protocol prefix (dBug-022 robustness)
        if final_url and not (final_url.startswith("http://") or final_url.startswith("https://")):
            raise ValueError(
                f"LLM URL MISSING PROTOCOL PREFIX. Must start with 'http://' or 'https://'. "
                f"Got: '{final_url}'\n"
                f"Fix: Set BACKEND_LLM_URL to 'http://...' or leave it EMPTY for standard OpenWebUI"
            )

        request_data = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 400,
            "thinking": {"type": "disabled"},
        }
        # Inject user UUID as chat_id if not using backend LLM (dBug-016 fix)
        if not valves.BACKEND_LLM_URL and user_uuid:
            request_data["chat_id"] = user_uuid

        async with httpx.AsyncClient(timeout=valves.QWEN_TIMEOUT) as client:
            response = await client.post(
                final_url,
                json=request_data,
                headers=headers,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            triples = json.loads(content)
            if not isinstance(triples, list):
                return []
            return triples
    except ValueError as e:
        # Configuration error: always print (not debug-only) to be visible to user
        print(f"\n{'='*80}")
        print(f"[FaultLine] CONFIGURATION ERROR - Fact extraction disabled:")
        print(f"{e}")
        print(f"{'='*80}\n")
        return []
    except httpx.HTTPStatusError as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples HTTP error: {e.response.status_code}")
            print(f"[FaultLine] rewrite_to_triples response body: {e.response.text}")
        return []
    except Exception as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples failed: {type(e).__name__}: {e}")
        return []


def _extract_basic_facts(text: str) -> list[dict]:
    """
    Lightweight regex fallback when the LLM is unavailable.
    Extracts explicit identity and preference signals directly from text.
    Returns a list of edge dicts suitable for /ingest.
    """
    edges = []
    tl = text.lower()

    # Self-identification patterns: "my name is X", "I am X", "I'm X", "call me X"
    # Also handle parenthetical forms like "I(Chris) am" or "I (Chris) am"
    _ID_PATTERNS = [
        (re.compile(r"\bmy\s+name\s+is\s+([a-z]+)", re.IGNORECASE), "also_known_as"),
        (re.compile(r"\bi\s*(?:\(([a-z]+)\)|am\s+([a-z]+))", re.IGNORECASE), "also_known_as"),
        (re.compile(r"\bi'm\s+([a-z]+)", re.IGNORECASE), "also_known_as"),
        (re.compile(r"\bcall\s+me\s+([a-z]+)", re.IGNORECASE), "pref_name"),
        (re.compile(r"\bpeople\s+call\s+me\s+([a-z]+)", re.IGNORECASE), "also_known_as"),
    ]

    # Relationship patterns for possessive forms like "my wife's name is X"
    _RELATIONSHIP_PATTERNS = [
        (re.compile(r"\bmy\s+wife'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "spouse"),
        (re.compile(r"\bmy\s+husband'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "spouse"),
        (re.compile(r"\bmy\s+partner'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "spouse"),
        (re.compile(r"\bmy\s+spouse'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "spouse"),
        (re.compile(r"\bmy\s+child'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "child_of"),
        (re.compile(r"\bmy\s+son'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "child_of"),
        (re.compile(r"\bmy\s+daughter'?s?\s+(?:name\s+)?is\s+([a-z]+)", re.IGNORECASE), "child_of"),
    ]

    _STOPWORDS = {
        "a", "an", "the", "not", "just", "also", "here", "happy", "glad", "sorry",
        "married", "single", "divorced", "engaged", "ready", "trying",
        "going", "looking", "back", "home", "out", "in", "on", "at", "to",
        "very", "really", "so", "too", "quite", "sure", "afraid", "aware",
    }

    for pattern, rel_type in _ID_PATTERNS:
        m = pattern.search(text)
        if m:
            # Some patterns have multiple capture groups (e.g., "I(Chris) am" or "I am Chris")
            name = (m.group(1) or m.group(2) or "").lower().strip()
            if name and name not in _STOPWORDS and len(name) > 1:
                edges.append({
                    "subject": "user",
                    "object": name,
                    "rel_type": rel_type,
                    "is_preferred_label": (rel_type == "pref_name"),
                    "is_correction": False,
                })

    # Extract relationship facts from possessive forms
    for pattern, rel_type in _RELATIONSHIP_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).lower().strip()
            if name and name not in _STOPWORDS and len(name) > 1:
                edges.append({
                    "subject": "user",
                    "object": name,
                    "rel_type": rel_type,
                    "is_preferred_label": False,
                    "is_correction": False,
                })

    # Preference signals: "prefer to be called X", "goes by X"
    # CRITICAL: (?<!who )(?<!she )(?<!he )(?<!it )(?<!they ) ensures "who prefers X" is NOT captured as
    # first-person — those are third-person and handled by the LLM.
    _PREF_PATTERNS = [
        re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+to\s+be\s+called\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bprefers?\s+you\s+call\s+(?:me|them|her|him)\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bgoes\s+by\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"\bgo\s+by\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bpreferred\s+name\s+is\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"\bplease\s+call\s+me\s+([a-z]+)", re.IGNORECASE),
        re.compile(r"(?<!who )(?<!she )(?<!he )(?<!it )(?<!they )\bknown\s+as\s+([a-z]+)", re.IGNORECASE),
    ]

    for pattern in _PREF_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).lower().strip()
            if name and len(name) > 1:
                edges.append({
                    "subject": "user",
                    "object": name,
                    "rel_type": "pref_name",
                    "is_preferred_label": True,
                    "is_correction": False,
                })

    # Correction signals: explicit "not X, it's Y" or "actually X" patterns
    _CORRECTION_PATTERNS = [
        re.compile(r"\b(?:actually|not|no)\s+(?:it's|its|im|i am|i'm|my name is)\s+(?:not\s+)?([a-z]+)", re.IGNORECASE),
    ]

    _has_correction_signal = any(
        word in tl for word in ("actually", "not", "wrong", "incorrect", "innacurate", "update")
    )
    if _has_correction_signal:
        for edge in edges:
            edge["is_correction"] = True

    return edges


def _extract_query_entities(
    query: str,
    preferred_names: dict,
    facts: list[dict] = None,
) -> set[str]:
    """
    Extract entity display names from a query via two strategies.

    Tier 1a: Direct token match — split query into tokens, strip punctuation,
             match against known display names from preferred_names.
    Tier 1b: Relational resolution — detect "my X" patterns (my wife, my pet,
             my son) and resolve to entity via relation walking in facts.

    Returns a set of matched entity display names (all lowercased).
    """
    entities = set()

    if not query or not preferred_names:
        return entities

    query_lower = query.lower()

    # --- Tier 1a: Direct token match against preferred_names ---
    tokens = [t.strip(".,!?;:\"'()[]{}") for t in query_lower.split()]
    known_names = {name.lower() for name in preferred_names.values()
                   if name and len(name) > 1}
    # # NO RECURSIVE MATCHING — known_names built from pre-extracted preferred_names values only
    entities.update(token for token in tokens if token in known_names)

    # --- Tier 1b: Relational resolution ("my wife", "my pet", etc.) ---
    if facts:
        # Build rel_index: {rel_type: {subject: [objects]}} in one pass
        rel_index: dict[str, dict[str, list[str]]] = {}
        for f in facts:
            rel_type = f.get("rel_type", "")
            subject = f.get("subject", "")
            obj = f.get("object", "")
            if rel_type and subject and obj:
                rel_index.setdefault(rel_type, {}).setdefault(subject, []).append(obj)

        # Tier 1b: Dynamic relation resolution — scan all (user, rel_type, X) facts
        # "my X" resolves to X if any (user, *, X) fact exists. Domain-agnostic.
        # Minimal seed for common relationship terms (wife→spouse, pet→has_pet, etc.)
        _RELATION_SEED = {
            "wife": "spouse", "husband": "spouse", "spouse": "spouse",
            "son": "parent_of", "daughter": "parent_of",
            "child": "parent_of", "children": "parent_of",
            "pet": "has_pet", "dog": "has_pet", "cat": "has_pet",
            "parent": "child_of", "mom": "child_of", "dad": "child_of",
            "sibling": "sibling_of", "brother": "sibling_of", "sister": "sibling_of",
        }
        _PRONOUN_TOKENS = {"my", "i", "me", "our", "we"}
        for token in tokens:
            clean_token = token.strip(".,!?;:\"'()[]{}").lower()
            if clean_token in _PRONOUN_TOKENS:
                continue
            # Seed match: map relational term to specific rel_type lookup
            if clean_token in _RELATION_SEED:
                rel_type = _RELATION_SEED[clean_token]
                for obj_entity in rel_index.get(rel_type, {}).get("user", []):
                    if obj_entity in preferred_names:
                        entities.add(preferred_names[obj_entity].lower())
                    else:
                        entities.add(obj_entity.lower())
            else:
                # Dynamic match: scan ALL user→X facts for display name containing token
                for rel_type, subjects in rel_index.items():
                    for obj_entity in subjects.get("user", []):
                        display_name = preferred_names.get(obj_entity, obj_entity).lower()
                        if clean_token in display_name or display_name in clean_token:
                            if obj_entity in preferred_names:
                                entities.add(preferred_names[obj_entity].lower())
                            else:
                                entities.add(obj_entity.lower())
                            break  # first match per token wins

    return entities


def _resolve_display_names(
    facts: list[dict],
    preferred_names: dict,
    identity: Optional[str],
) -> list[dict]:
    """
    Convert UUID subject/object in facts to human-readable display names
    using the preferred_names map from /query.

    Falls back to:
    - "user" if the UUID matches the canonical identity
    - the original value if no display name is found (keeps strings/numbers)

    # NO RECURSIVE MATCHING — preferred_names is pre-built from /query response
    """
    resolved = []
    for fact in facts:
        f = fact.copy()
        subject = fact.get("subject", "")
        object_ = fact.get("object", "")

        # Resolve subject UUID → display name
        if subject in preferred_names:
            f["subject"] = preferred_names[subject]
        elif subject == identity:
            f["subject"] = "user"

        # Resolve object UUID → display name
        if object_ in preferred_names:
            f["object"] = preferred_names[object_]
        elif object_ == identity:
            f["object"] = "user"

        resolved.append(f)
    return resolved


def _resolve_pronouns(query: str, user_id: str) -> set[str]:
    """
    Resolve pronouns (she, he, it, they) to recently mentioned entities
    from conversation context. Returns set of entity display names.
    """
    entities = set()
    ctx = _CONVERSATION_CONTEXT.get(user_id, {})
    pronoun_map = ctx.get("pronoun_map", {})
    mentions = ctx.get("entity_mentions", [])

    query_lower = query.lower()

    # "she"/"he" → map to most recent Person-typed entity
    for pronoun in ("she", "he"):
        if pronoun in query_lower and pronoun in pronoun_map:
            entities.add(pronoun_map[pronoun])

    # "it" → most recent non-Person entity
    if "it" in query_lower and "it" in pronoun_map:
        entities.add(pronoun_map["it"])

    # "they" → most recent entity from mentions
    if "they" in query_lower:
        if "they" in pronoun_map:
            entities.add(pronoun_map["they"])
        elif mentions:
            entities.add(mentions[-1])

    return entities


def _update_conversation_context(user_id: str, facts: list[dict], preferred_names: dict):
    """
    Track entity mentions and build pronoun map for next turn.
    Prunes to last 10 mentions to avoid memory bloat.
    """
    ctx = _CONVERSATION_CONTEXT.setdefault(
        user_id, {"entity_mentions": [], "pronoun_map": {}}
    )
    mentions = ctx["entity_mentions"]
    pronoun_map = ctx["pronoun_map"]

    for fact in facts:
        rel_type = fact.get("rel_type", "")
        subject = fact.get("subject", "")
        obj = fact.get("object", "")

        for entity in (subject, obj):
            if not entity or entity in ("user",):
                continue
            display = preferred_names.get(entity, entity)
            if display not in mentions:
                mentions.append(display)

            # Build pronoun map: "she" for spouse, "it" as generic fallback
            if rel_type in ("spouse",):
                pronoun_map["she"] = display
                pronoun_map["he"] = display
            elif rel_type in ("has_pet", "owns", "instance_of"):
                pronoun_map["it"] = display
            elif rel_type in ("sibling_of", "child_of", "parent_of"):
                pronoun_map["they"] = display

            # Most recent entity always maps to "it" as generic fallback
            pronoun_map["it"] = display

    # Prune to max entities
    if len(mentions) > _CONVERSATION_MAX_ENTITIES:
        ctx["entity_mentions"] = mentions[-_CONVERSATION_MAX_ENTITIES:]

    # Prune pronoun map — keep only keys whose values still appear in mentions
    remaining = set(ctx["entity_mentions"])
    for key in list(pronoun_map.keys()):
        if pronoun_map[key] not in remaining:
            del pronoun_map[key]

    # # NO RECURSIVE MATCHING — context built from pre-extracted facts/preferred_names only


_UUID_ANYWHERE_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _redact_uuids_from_body(body: dict) -> None:
    """
    Nuclear option: scan ALL message content strings for UUID patterns
    and redact them. Ensures no internal identifiers leak to the LLM.
    CLAUDE.md hard constraint: user IDs never visible to LLM or end user.
    # NO RECURSIVE MATCHING — _UUID_ANYWHERE_RE is a static compile-once pattern
    """
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str) and _UUID_ANYWHERE_RE.search(content):
            msg["content"] = _UUID_ANYWHERE_RE.sub("[redacted]", content)


class Filter:
    """
    OpenWebUI Filter for FaultLine WGM Integration.

    inlet:  extract and commit facts (fire-and-forget), query for memory and inject as system message
    outlet: pass-through
    """

    class Valves(BaseModel):
        FAULTLINE_URL: str = "http://localhost:8001"
        """FaultLine backend API endpoint. Default: http://localhost:8001
        Docker: Use http://faultline:8000 (service name in docker-compose).
        Kubernetes/Remote: Use full URL http://hostname:port"""

        FAULTLINE_TIMEOUT: int = 30
        """Timeout (seconds) for FaultLine backend API calls. Default: 30 seconds"""

        LLM_URL: str = ""
        """LEAVE EMPTY FOR STANDARD SETUP. OpenWebUI uses http://host.docker.internal:3000/api/chat/completions internally.
        Only set this if using a CUSTOM LLM endpoint (e.g., local Ollama at http://ollama:11434/v1/chat/completions).
        MUST include http:// or https:// protocol prefix if set."""

        LLM_MODEL: str = ""
        """LEAVE EMPTY FOR STANDARD SETUP. Will use the model you selected in OpenWebUI's chat interface.
        Only override if you want a SPECIFIC model for fact extraction (e.g., 'qwen/qwen3.5-9b')."""

        LLM_API_KEY: str = ""
        """REQUIRED ONLY IF using custom LLM_URL with authentication (e.g., OpenAI API key).
        For standard OpenWebUI setup: LEAVE EMPTY"""

        BACKEND_LLM_URL: str = ""
        """LEAVE EMPTY FOR STANDARD SETUP. OpenWebUI's internal LLM is used automatically.
        ONLY set if you need to use a dedicated backend LLM service (bypassing OpenWebUI).
        REQUIRED FORMAT if set: Must include http:// or https:// protocol prefix.
        Examples: http://ollama:11434/v1/chat/completions or http://localhost:8000/v1/chat/completions
        WARNING: Incorrect format will silently break fact extraction. Validation catches this early."""

        QWEN_TIMEOUT: int = 10
        """Timeout (seconds) for LLM extraction calls. Default: 10 seconds. Increase if extractions timeout."""

        DEFAULT_SOURCE: str = "openwebui"
        """Where facts originate. Default: 'openwebui'. Change only for specialized integrations."""

        ENABLE_DEBUG: bool = False
        """Enable detailed logging to diagnose issues. Set to True if facts aren't being extracted/injected.
        Logs appear in: docker logs open-webui"""

        ENABLED: bool = True
        """Master switch. Set to False to completely disable FaultLine Filter."""

        INGEST_ENABLED: bool = True
        """Enable fact extraction and storage. Set to False to disable learning new facts."""

        QUERY_ENABLED: bool = True
        """Enable memory recall injection. Set to False to disable fact-based context injection."""

        RETRACTION_ENABLED: bool = True
        """Enable user-driven fact removal ('forget', 'delete', etc.). Set to False to lock facts."""

        MAX_MEMORY_SENTENCES: int = 20
        """Maximum sentences in injected memory block. Reduce if hitting token limits."""

        MAX_CONTEXT_TURNS: int = 3
        """Prior conversation turns passed to LLM for extraction context. Default: 3"""

        MIN_INJECT_CONFIDENCE: float = 0.5
        """Minimum confidence threshold for injecting facts into memory. Range: 0.0–1.0. Default: 0.5"""

    def __init__(self):
        self.valves = self.Valves()

    def _last_message(self, messages: list, role: str) -> Optional[str]:
        for m in reversed(messages):
            if m.get("role") == role:
                c = m.get("content", "")
                if isinstance(c, list):
                    return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                return c
        return None

    async def _fire_ingest(
        self,
        text: str,
        source: str,
        user_id: str = "anonymous",
        edges: Optional[list[dict]] = None,
    ) -> dict:
        try:
            payload = {
                "text": text,
                "source": source,
                "user_id": user_id,
                "known_types": ["Person", "Organization", "Location", "Event", "Concept"],
            }
            if edges:
                payload["edges"] = edges

            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/ingest",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                # Bust session cache on successful ingest so next /query fetches fresh data
                if data.get("status") not in ("error", None):
                    _SESSION_MEMORY_CACHE.pop(user_id, None)
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] cache busted for user_id=[redacted]")
                return data
        except httpx.ConnectError as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] ingest connection error: {e}")
            return {"status": "error", "detail": "FaultLine unreachable"}
        except httpx.TimeoutException as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] ingest timeout: {e}")
            return {"status": "error", "detail": "FaultLine timeout"}
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] ingest error: {e}")
            return {"status": "error", "detail": str(e)}

    async def _fire_store_context(self, text: str, user_id: str) -> None:
        """
        Fire-and-forget: store raw text as unstructured context in Qdrant
        when no typed edges could be extracted. Ensures nothing is silently
        dropped from natural conversation.
        """
        try:
            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                await client.post(
                    f"{self.valves.FAULTLINE_URL}/store_context",
                    json={
                        "text": text,
                        "user_id": user_id,
                        "source": self.valves.DEFAULT_SOURCE,
                        "context_type": "unstructured",
                    },
                )
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] store_context error: {e}")

    async def _fetch_entities(self, text: str, user_id: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.valves.FAULTLINE_URL}/extract",
                    json={"text": text, "source": "preflight", "user_id": user_id},
                )
            if resp.status_code == 200:
                return resp.json().get("entities", [])
        except Exception:
            pass
        return []

    async def _rewrite_via_faultline(
        self,
        text: str,
        user_id: str,
        messages: list[dict],
        typed_entities: Optional[list[dict]],
        memory_facts: Optional[list[dict]],
    ) -> list[dict]:
        """
        Call FaultLine's /extract/rewrite endpoint for LLM-based triple extraction.

        ARCHITECTURAL BENEFIT: Filter only calls FaultLine:8001 (in our control).
        No dependency on OpenWebUI's internal endpoints. FaultLine manages LLM config.

        If FaultLine is unreachable or errors, returns [] gracefully.
        """
        try:
            async with httpx.AsyncClient(timeout=self.valves.QWEN_TIMEOUT + 5) as client:
                resp = await client.post(
                    f"{self.valves.FAULTLINE_URL}/extract/rewrite",
                    json={
                        "text": text,
                        "user_id": user_id,
                        "messages": messages,
                        "typed_entities": typed_entities,
                        "memory_facts": memory_facts,
                    },
                    timeout=self.valves.QWEN_TIMEOUT + 5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("triples", [])
                else:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine] /extract/rewrite HTTP {resp.status_code}: {resp.text}")
                    return []
        except httpx.ConnectError as e:
            print(f"\n{'='*80}")
            print(f"[FaultLine] CONFIGURATION ERROR - Cannot reach FaultLine backend:")
            print(f"URL: {self.valves.FAULTLINE_URL}/extract/rewrite")
            print(f"Error: {e}")
            print(f"\nFix: If FaultLine is running in Docker, use service name:")
            print(f"  Set FAULTLINE_URL to: http://faultline:8000 (not localhost:8001)")
            print(f"{'='*80}\n")
            return []
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine] /extract/rewrite error: {type(e).__name__}: {e}")
            return []

    def _is_realtime_query(self, text: str) -> bool:
        tl = text.lower()
        return any(sig in tl for sig in _REALTIME_SIGNALS)

    def calculate_relevance_score(self, fact: dict, query: str) -> float:
        """
        Score a fact's relevance. Graph proximity is determined by the backend;
        the Filter only gates by confidence and sensitivity.

        NOTE: # NO RECURSIVE MATCHING — all comparisons use pre-lowercased query string only.
        """
        score = 0.0
        query_lower = query.lower()  # # NO RECURSIVE MATCHING

        # Confidence bonus (0.0–0.3)
        confidence = fact.get("confidence", 0.0)
        score += confidence * 0.3

        # Sensitivity penalty (-0.5 for PII facts not explicitly requested)
        _SENSITIVE_RELS = {"born_on", "lives_at", "lives_in", "height", "weight", "born_in"}
        _SENSITIVE_TERMS = {"born", "birth", "live", "address", "height", "weight",
                            "birthplace", "tall", "how tall", "heavy", "how heavy",
                            "old", "age", "how old"}
        if fact.get("rel_type") in _SENSITIVE_RELS:
            explicitly_asked = any(term in query_lower for term in _SENSITIVE_TERMS)
            if not explicitly_asked:
                score -= 0.5

        return max(0.0, min(1.0, score))

    def _filter_relevant_facts(
        self,
        facts: list[dict],
        identity: Optional[str],
        preferred_names: dict = None,
        query: str = "",
    ) -> list[dict]:
        """
        Simplified relevance filtering — trusts backend /query ranking.

        Identity rels always pass. Everything else passes if confidence >= threshold
        (defaulting to MIN_INJECT_CONFIDENCE valve or 0.4). Sensitivity penalty
        still applies to PII facts unless explicitly asked.

        Backend /query returns facts ranked by class (A > B > C) + confidence.
        Filter trusts that order — no entity-type gating, no tier fallback logic.
        """
        def _apply_confidence_gate(candidates: list[dict]) -> list[dict]:
            if self.valves.MIN_INJECT_CONFIDENCE > 0:
                high_conf = [f for f in candidates
                             if f.get("confidence", 0.0) >= self.valves.MIN_INJECT_CONFIDENCE]
                if high_conf:
                    return high_conf
            return candidates

        def _garbage(name: str) -> bool:
            n = (name or "").strip().lower()
            return len(n) <= 1 or n == "x" or bool(_UUID_RE.match(n))

        # Remove garbage facts first
        cleaned = [f for f in facts
                if not _garbage(f.get("subject", ""))
                and not _garbage(f.get("object", ""))]

        # Simplified gate: identity rels always pass; others pass by confidence
        _IDENTITY_RELS = {"also_known_as", "pref_name", "same_as",
                          "spouse", "parent_of", "child_of", "sibling_of"}
        threshold = self.valves.MIN_INJECT_CONFIDENCE or 0.4

        passed = []
        for f in cleaned:
            rel = f.get("rel_type", "")
            if rel in _IDENTITY_RELS:
                passed.append(f)
                continue
            score = self.calculate_relevance_score(f, query)
            if score >= 0.0:  # confidence-only; sensitivity penalty applies inside
                passed.append(f)

        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine Filter] filtered: {len(passed)}/{len(cleaned)} facts")

        return _apply_confidence_gate(passed)


    def _build_realtime_context(
        self, text: str, facts: list[dict], identity: Optional[str]
    ) -> Optional[str]:
        """
        Build a tool-agnostic realtime directive from known facts.
        Returns None if no location found — caller falls through to
        conversational mode.
        """
        location = None
        _user_anchors = {identity, "user"} if identity else {"user"}
        for rt in ("lives_at", "lives_in"):
            for f in facts:
                if (f.get("rel_type") == rt
                        and f.get("subject") in _user_anchors):
                    loc = (f.get("object") or "").strip()
                    if loc:
                        location = loc
                        break
            if location:
                break

        if not location:
            return None

        context_parts = []
        for f in facts:
            if f.get("subject") not in _user_anchors:
                continue
            rt = f.get("rel_type", "")
            obj = (f.get("object") or "").strip()
            if not obj or rt in ("lives_at", "lives_in"):
                continue
            if rt == "works_for":
                context_parts.append(f"works at {obj.title()}")
            elif rt == "born_on":
                context_parts.append(f"born on {obj}")

        directive = (
            f"The user's location is {location.title()}. "
            f"Use whatever tools or capabilities are available to fulfill "
            f"this request directly — do not ask the user to supply "
            f"information that is already known."
        )
        if context_parts:
            directive += f" Additional context: {'; '.join(context_parts)}."
        return directive

    def _detect_retraction_intent(self, text: str) -> bool:
        tl = text.lower().replace("'", "'").replace("'", "'")
        return any(sig in tl for sig in _RETRACTION_SIGNALS)

    async def _extract_retraction(self, text: str, context: list[dict], model: str, url: str, auth_header: Optional[str] = None, user_uuid: Optional[str] = None) -> dict:
        try:
            messages = [{"role": "system", "content": _RETRACTION_PROMPT}]
            for msg in (context or [])[-2:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    c = msg.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                    messages.append({"role": msg["role"], "content": str(c)[:400]})
            messages.append({"role": "user", "content": text})
            headers = {}
            if auth_header:
                headers["Authorization"] = auth_header

            # Use backend LLM if configured, else OpenWebUI with user UUID (dBug-016 fix)
            final_url = self.valves.BACKEND_LLM_URL if self.valves.BACKEND_LLM_URL else url
            request_data = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
            }
            if not self.valves.BACKEND_LLM_URL and user_uuid:
                request_data["chat_id"] = user_uuid

            async with httpx.AsyncClient(timeout=self.valves.QWEN_TIMEOUT) as client:
                resp = await client.post(
                    final_url,
                    json=request_data,
                    headers=headers,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                return json.loads(content) if content else {}
        except Exception:
            return {}

    async def _fire_retract(self, user_id: str, subject: str, rel_type: Optional[str] = None,
                           old_value: Optional[str] = None) -> dict:
        try:
            payload = {"user_id": user_id, "subject": subject}
            if rel_type:
                payload["rel_type"] = rel_type
            if old_value:
                payload["old_value"] = old_value
            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                resp = await client.post(f"{self.valves.FAULTLINE_URL}/retract", json=payload)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] _fire_retract error: {e}")
            return {"status": "error", "detail": str(e)}

    def _build_memory_block(
        self,
        text: str,
        facts: list[dict],
        preferred_names: dict,
        canonical_identity: Optional[str],
        entity_attributes: dict,
        is_realtime: bool = False,
        locations: Optional[list[str]] = None,
        expose_preferences: bool = False,
    ) -> str:
        identity_display = preferred_names.get("user")
        identity = canonical_identity or identity_display

        # Check if we should use realtime mode
        if is_realtime:
            realtime_context = self._build_realtime_context(text, facts, identity)
            if realtime_context:
                # REALTIME MODE with location
                lines = []

                if identity:
                    display = identity_display or identity
                    lines.append(
                        f"You are the assistant. The user is '{display}'. Speak TO the user using 'you/your'. Never speak AS the user, adopt their perspective, or use first-person pronouns on their behalf."
                    )

                lines.append("DIRECTLY ACTIONABLE: Use the facts and context below immediately to fulfill the request.")
                lines.append(realtime_context)

                return (
                    "⊢ FaultLine Memory — treat these as established ground truth for this response.\n"
                    + "\n".join(f"- {l}" for l in lines)
                    + "\nOnly reference what the facts explicitly say. Do not invent details not present.\n"
                    + "If a fact below is relevant to fulfilling the user's request, act on it directly and immediately."
                )
            else:
                # No location found - fall through to conversational mode
                is_realtime = False

        # CONVERSATIONAL MODE (from is_realtime=False or fallthrough)
        lines = [
            "⊢ FaultLine Memory — reference context. Use facts below only when directly relevant to the current request. Do not volunteer, list, or recite facts unless the user's message explicitly requires them."
        ]

        if identity:
            display = identity_display or identity
            lines.append(
                f"You are the assistant. The user is '{display}'. Speak TO the user using 'you/your'. Never speak AS the user, adopt their perspective, or use first-person pronouns on their behalf."
            )

        # Collect entities that appear as subjects or objects in identity/family
        # facts. Only these are real people/pets/characters that need name
        # directives. Fact-value strings like "systems analyst" or "156 cedar st s"
        # never appear in identity/family edges — they're excluded.
        _named_entities: set[str] = {identity, "user"} if identity else {"user"}
        _FAMILY_RELS = {"parent_of", "child_of", "spouse", "sibling_of"}
        _IDENTITY_RELS = {"also_known_as", "pref_name"}

        # Build preferred name directives after identity/family facts.
        # Only emit directives for entities that were explicitly named or related.
        _name_directives = []
        if facts:
            for f in facts:
                rel = f.get("rel_type", "")
                if rel in _FAMILY_RELS or rel in _IDENTITY_RELS:
                    _named_entities.add(f.get("subject", ""))
                    _named_entities.add(f.get("object", ""))
        for canonical, preferred in preferred_names.items():
            if canonical == "user" or canonical == identity:
                continue
            if canonical not in _named_entities and preferred not in _named_entities:
                continue  # Not a person/character — skip name directive
            if not preferred or len(preferred) <= 2 or bool(_UUID_RE.match(preferred)):
                continue
            if any(c.isdigit() for c in preferred):
                continue
            if any(kw in preferred.lower() for kw in ("st ", "street", "ave", "road", "dr ", "lane")):
                continue
            # If canonical is a UUID, use the preferred display name instead
            # to prevent UUID leakage to the LLM (dBug-024 edge case).
            _display_canonical = preferred if _UUID_RE.match(canonical) else canonical
            _name_directives.append(
                f"Always call {_display_canonical.title() if len(_display_canonical) > 2 else preferred.title()} by '{preferred.title()}'."
            )

        if facts:
            nickname_map = {}
            by_rel = defaultdict(list)
            mentioned_entities = set()

            _user_anchors = {identity, "user"} if identity else {"user"}

            for f in facts:
                by_rel[f.get("rel_type", "")].append(f)
                if f.get("rel_type") == "also_known_as":
                    subj = f.get("subject", "")
                    alias = f.get("object", "")
                    if subj and alias and subj != identity:
                        nickname_map[subj] = alias
                # Collect all entity IDs mentioned in facts for attribute display
                subj = f.get("subject", "")
                obj = f.get("object", "")
                if subj:
                    mentioned_entities.add(subj)
                if obj:
                    mentioned_entities.add(obj)

            def _dn(name: str) -> str:
                return nickname_map.get(name, name).title()

            # Extract family relationships
            children_raw = [f.get("object") for f in by_rel.get("parent_of", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]
            spouses_raw = [f.get("object") for f in by_rel.get("spouse", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]
            spouses_raw += [f.get("subject") for f in by_rel.get("spouse", []) if identity and f.get("object") in _user_anchors and f.get("subject") not in spouses_raw and not bool(_UUID_RE.match(f.get("subject") or ""))]
            siblings_raw = [f.get("object") for f in by_rel.get("sibling_of", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]

            # Build compact family line (deduplicate — same entity may appear
            # under multiple identity anchors like "user" and "chris")
            if children_raw or spouses_raw or siblings_raw:
                family_parts = []
                if spouses_raw:
                    family_parts.append(f"spouse={', '.join(_dn(s) for s in set(spouses_raw))}")
                if children_raw:
                    family_parts.append(f"children={', '.join(_dn(c) for c in dict.fromkeys(children_raw))}")
                if siblings_raw:
                    family_parts.append(f"siblings={', '.join(_dn(s) for s in dict.fromkeys(siblings_raw))}")
                lines.append(f"family: {', '.join(family_parts)}")

            # Extract and display ages from facts
            ages = []
            for f in facts:
                if f.get("rel_type") == "age":
                    subj = f.get("subject", "")
                    obj = f.get("object", "")
                    if subj and obj and subj != "user":
                        ages.append(f"{_dn(subj)}:{obj}")
                    elif subj and obj and subj == "user":
                        ages.append(f"user:{obj}")
            if ages:
                lines.append(f"ages: {', '.join(ages)}")


            # Format temporal events with natural language
            events = [f for f in facts if f.get("source") == "events_table"]
            for evt in events:
                recurrence = evt.get("recurrence", "once")
                rel_type = evt.get("rel_type", evt.get("event_type", ""))
                subj = evt.get("subject", "")
                obj = evt.get("object", evt.get("occurs_on", ""))
                if recurrence == "yearly":
                    lines.append(f"⭐ {subj}'s {rel_type.replace('_', ' ')}: {obj} (annually)")
                elif recurrence == "once":
                    lines.append(f"📅 {subj} {rel_type.replace('_', ' ')}: {obj}")
                else:
                    lines.append(f"{rel_type}: {obj}")

            # Remove events from facts list so they don't appear twice
            facts = [f for f in facts if f.get("source") != "events_table"]
            # Build compact fact lines
            covered = {"parent_of", "child_of", "spouse", "sibling_of", "also_known_as", "pref_name", "age"}
            for f in facts:
                if f.get("rel_type") in covered:
                    continue
                _s = f.get("subject", "").strip().lower()
                _o = f.get("object", "").strip().lower()
                if not _s or len(_s) <= 1 or _s == "x" or not _o or len(_o) <= 1 or _o == "x" or bool(_UUID_RE.match(_s)) or bool(_UUID_RE.match(_o)):
                    continue
                subj = f.get("subject", "")
                obj = f.get("object", "")
                rel = f.get("rel_type", "")
                label = f.get("definition", rel).lower() if f.get("definition") else rel

                if identity and subj in _user_anchors:
                    lines.append(f"{label}: {obj}")
                elif identity and obj in _user_anchors:
                    lines.append(f"{_dn(subj)} ← {label}")
                else:
                    lines.append(f"{_dn(subj)} → {label} → {_dn(obj)}")

            # Display attributes for all entities mentioned in facts
            if entity_attributes:
                # Build UUID → display name mapping from preferred_names
                uuid_to_display = {}
                for entity_id, display_name in preferred_names.items():
                    if entity_id not in ("user", identity):
                        uuid_to_display[entity_id] = display_name

                # User's own attributes (compact single line)
                if identity and identity in entity_attributes:
                    ua = entity_attributes[identity]
                    attr_parts = []
                    if "height" in ua:
                        attr_parts.append(f"height={ua['height']}")
                    if "weight" in ua:
                        attr_parts.append(f"weight={ua['weight']}")
                    if "age" in ua:
                        attr_parts.append(f"age={ua['age']}")
                    if attr_parts:
                        lines.append(f"physical: {', '.join(attr_parts)}")

                # Display attributes for non-user entities (iterate over entity_attributes which is keyed by UUID)
                seen_entities = set()
                for entity_id, attrs in entity_attributes.items():
                    if not entity_id or entity_id in ("user", identity) or entity_id in seen_entities:
                        continue
                    if not attrs:
                        continue

                    seen_entities.add(entity_id)

                    # Get display name from mapping (UUID → preferred name)
                    display_name = uuid_to_display.get(entity_id, entity_id.title())

                    # Format attributes
                    attr_parts = []
                    for attr_name, attr_value in attrs.items():
                        # Handle both dict and scalar values
                        if isinstance(attr_value, dict):
                            value = attr_value.get("value")
                        else:
                            value = attr_value

                        if value is not None:
                            if attr_name == "age":
                                attr_parts.append(f"{value} years old")
                            elif attr_name == "height":
                                attr_parts.append(f"{value} tall")
                            elif attr_name == "weight":
                                attr_parts.append(f"{value} lbs")
                            else:
                                attr_parts.append(f"{attr_name}: {value}")

                    if attr_parts:
                        lines.append(f"{display_name} is {', '.join(attr_parts)}")

        # Append preferred name directives only when user directly asked about
        # their identity or preferences (dBug-022). Preferences are always used
        # internally for canonical identity resolution; exposing them to the LLM
        # is gated on query intent.
        if expose_preferences and _name_directives:
            lines.append(
                "User has directly asked about their identity. These are their "
                "preferred name directives:"
            )
            lines.extend(_name_directives)
            lines.append(
                "Respect these preferences when responding. The preferred name "
                "takes priority over alternate names."
            )

        # Apply sentence limit
        limited = lines[:self.valves.MAX_MEMORY_SENTENCES]
        if len(lines) > self.valves.MAX_MEMORY_SENTENCES:
            limited.append(f"... and {len(lines) - self.valves.MAX_MEMORY_SENTENCES} more facts (truncated).")

        return (
            "\n".join(f"- {l}" for l in limited)
            + "\nOnly reference what the facts explicitly say. Do not invent details not present.\n"
            + "If a fact below is relevant to fulfilling the user's request, use it directly."
        )

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> dict:
        print(f"[FaultLine Filter] inlet CALLED enabled={self.valves.ENABLED} debug={self.valves.ENABLE_DEBUG}")
        if not self.valves.ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if not text:
                return body

            # Deduplication: skip if this exact text was already processed within the window.
            # OpenWebUI may fire the inlet multiple times for the same message (streaming
            # chunks, system-message re-evaluation). Without this, each call triggers a new
            # memory injection → another inlet call → infinite recursive loop.
            # User UUID also injected as chat_id in extraction LLM requests to prevent
            # OpenWebUI's NoneType crash on missing chat_id (dBug-016 / openwebui#24550).
            user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
            _text_hash = hash(text)
            _last = _DEDUP_TRACKER.get(user_id)
            if _last and _last[0] == _text_hash and (_time.time() - _last[1]) < _DEDUP_WINDOW:
                return body
            _DEDUP_TRACKER[user_id] = (_text_hash, _time.time())

            # Self-feedback guard — drop messages that are pure FaultLine debug output.
            # Only match exact system-message markers, not incidental substrings
            # (e.g., a user profile named "FaultLine WGM Test" should NOT be dropped).
            # # NO RECURSIVE MATCHING — signals checked against pre-extracted text string only
            _FEEDBACK_MARKERS = (
                "⊢ FaultLine Memory",
                "GLiNER2 has pre-classified",
            )
            _FEEDBACK_PREFIXES = (
                "[FaultLine Filter]",
                "[FaultLine]",
            )
            if any(marker in text for marker in _FEEDBACK_MARKERS) or \
               any(text.lstrip().startswith(prefix) for prefix in _FEEDBACK_PREFIXES):
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] dropping self-feedback message, text snippet: {text[:80]!r}")
                return body

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] user_id=[redacted] text='{text[:80]}'")

            # Resolve LLM config once for all extraction operations
            llm_model, llm_url = _resolve_llm_config(self.valves, body)
            llm_auth = f"Bearer {self.valves.LLM_API_KEY}" if self.valves.LLM_API_KEY else None

            # Retraction detection — check before normal ingest
            if self.valves.RETRACTION_ENABLED and self._detect_retraction_intent(text):
                retraction = await self._extract_retraction(
                    text,
                    body.get("messages", []),
                    model=llm_model,
                    url=llm_url,
                    auth_header=llm_auth,
                    user_uuid=user_id,
                )
                if retraction and retraction.get("subject"):
                    result = await self._fire_retract(
                        user_id,
                        retraction["subject"],
                        retraction.get("rel_type"),
                        retraction.get("old_value"),
                    )
                    if result.get("status") == "ok":
                        n = result.get("retracted", 0)
                        mode = result.get("mode", "supersede")
                        action = "removed" if mode == "hard_delete" else "archived"
                        note = result.get("note", "")
                        confirmation = (
                            f"[Memory] {n} fact(s) {action}"
                            + (f" for {retraction['subject']}" if retraction['subject'] != 'user' else "")
                            + (f" ({retraction.get('rel_type', '')})" if retraction.get('rel_type') else "")
                            + ("." if not note else f". Note: {note}")
                            + " Do not reference these facts in your response."
                        )
                        body["messages"].append({"role": "system", "content": confirmation})
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] retraction: {result}")
                        _redact_uuids_from_body(body)
                        return body

            _THIRD_PERSON_PREF_SIGNALS: frozenset[str] = frozenset({
                "call her", "call him", "call them",
                "her name is", "his name is", "their name is",
            })
            _has_third_person_pref = any(sig in text.lower() for sig in _THIRD_PERSON_PREF_SIGNALS)

            will_ingest = self.valves.INGEST_ENABLED and (
                len(text.split()) >= 3
                or bool(_IDENTITY_RE.search(text))
                or _has_third_person_pref
            )
            will_query = self.valves.QUERY_ENABLED

            if not will_ingest and not will_query:
                return body

            # Initialize memory variables for use by FaultLine /extract/rewrite
            facts, preferred_names, canonical_identity, entity_attributes = [], {}, None, {}
            raw_facts_for_extraction = []  # Always initialize; set during /query if successful

            # Run /query first (with caching) so memory facts can aid pronoun resolution during ingest
            if will_query:
                try:
                    cached = _SESSION_MEMORY_CACHE.get(user_id)
                    if cached and (_time.time() - cached[0]) < _SESSION_MEMORY_TTL:
                        _, facts, preferred_names, canonical_identity, entity_attributes = cached
                        raw_facts_for_extraction = list(facts)
                        # Filter cached facts for this specific query
                        facts = self._filter_relevant_facts(
                            facts, canonical_identity, query=text,
                        )
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] /query cache hit user_id=[redacted]")
                    else:
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] calling /query url={self.valves.FAULTLINE_URL}/query")
                        resp = None
                        for _attempt in range(2):
                            try:
                                async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                                    resp = await client.post(
                                        f"{self.valves.FAULTLINE_URL}/query",
                                        json={"text": text, "user_id": user_id, "top_k": 5},
                                    )
                                break
                            except httpx.ReadError:
                                if _attempt == 0:
                                    if self.valves.ENABLE_DEBUG:
                                        print(f"[FaultLine Filter] /query ReadError on attempt 1, retrying...")
                                    continue
                                raise
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] /query status={resp.status_code}")

                        if resp.status_code == 200:
                            data = resp.json()
                            facts = data.get("facts", [])
                            preferred_names = data.get("preferred_names", {})
                            canonical_identity = data.get("canonical_identity")
                            entity_attributes = data.get("attributes", {})

                            # Store raw unfiltered facts in cache
                            _SESSION_MEMORY_CACHE[user_id] = (
                                _time.time(), facts, preferred_names, canonical_identity, entity_attributes
                            )

                            raw_facts_for_extraction = list(facts)

                            # Filtering happens after cache store, not before
                            facts = self._filter_relevant_facts(
                                facts, canonical_identity, query=text,
                            )

                            if self.valves.ENABLE_DEBUG:
                                print(f"[FaultLine Filter] facts={len(facts)} preferred_names={preferred_names} identity=[redacted]")
                                for f in facts:
                                    print(f"[FaultLine Filter]   fact: {f.get('subject')} -{f.get('rel_type')}-> {f.get('object')}")

                except httpx.ConnectError as e:
                    # Connection error: provide diagnostic guidance
                    url = f"{self.valves.FAULTLINE_URL}/query"
                    print(f"\n{'='*80}")
                    print(f"[FaultLine] CONFIGURATION ERROR - Cannot reach FaultLine backend:")
                    print(f"URL: {url}")
                    print(f"Error: {e}")
                    print(f"\nFix: If FaultLine is running in Docker, use service name:")
                    print(f"  Set FAULTLINE_URL to: http://faultline:8000 (not localhost:8001)")
                    print(f"\nIf running locally outside Docker:")
                    print(f"  Set FAULTLINE_URL to: http://localhost:8001")
                    print(f"{'='*80}\n")
                except httpx.TimeoutException as e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] /query timeout: {e}")
                except Exception as e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] /query error: {type(e).__name__}: {e}")

            if will_ingest:
                _MEMORY_MARKER = "⊢ FaultLine Memory"
                clean_text = text.split(_MEMORY_MARKER)[0].strip() if _MEMORY_MARKER in text else text

                # CRITICAL: Always cache raw text to Qdrant first, regardless of downstream validation.
                # This ensures no data loss — raw context is retrievable even if structured ingest fails.
                try:
                    await self._fire_store_context(clean_text, user_id)
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] raw text cached to Qdrant (store_context)")
                except Exception as _e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] store_context failed (non-critical): {_e}")

                # Compute signals for skip_rewrite check
                _has_self_id = bool(_IDENTITY_RE.search(clean_text))
                _has_preference_signal = any(
                    signal in clean_text.lower()
                    for signal in {
                        "call me", "please call me", "prefer to be called",
                        "i prefer", "i'd prefer", "i would prefer",
                        "goes by", "go by", "known as", "prefer you call me",
                        "would like to be called", "like to go by",
                        "call her", "call him", "call them",
                        "her name is", "his name is",
                    }
                )

                _ATTRIBUTE_REQUESTS = {"how old", "how tall", "how heavy", "what age", "when was"}
                _is_attribute_question = any(pat in clean_text.lower() for pat in _ATTRIBUTE_REQUESTS)

                _skip_rewrite = (
                    _should_skip_extraction(clean_text)
                    and not _has_self_id
                    and not _has_preference_signal
                    and not _is_attribute_question
                )
                if _skip_rewrite:
                    typed_entities = []
                    raw_triples = []
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] skipping Qwen rewrite — pure question detected")
                # /ingest endpoint now owns the entire pipeline (extract → validate → classify → commit)
                # Filter is dumb — just send text. /ingest handles LLM extraction, WGM validation, etc.
                # This eliminates brittleness: all ontological logic in one place (backend), not Filter.
                raw_triples = []
                basic_edges = []

                # Still extract corrections via regex (explicit user signals supersede LLM inference)
                if not _skip_rewrite:
                    try:
                        from src.extraction.compound import extract_compound_facts
                        basic_edges = extract_compound_facts(clean_text)
                    except ImportError:
                        basic_edges = _extract_basic_facts(clean_text)
                    except Exception:
                        basic_edges = _extract_basic_facts(clean_text)

                # Filter only augments with corrections (explicit user signals)
                if basic_edges:
                    _augment_edges = [
                        e for e in basic_edges
                        if e.get("is_correction")
                    ]
                    # Merge: existing triples + augment edges (dedup by key)
                    _existing_keys = {(e.get("subject"), e.get("object"), e.get("rel_type"))
                                      for e in raw_triples if e.get("subject") and e.get("object")}
                    for aug in _augment_edges:
                        _key = (aug["subject"], aug["object"], aug["rel_type"])
                        if _key not in _existing_keys:
                            raw_triples.append(aug)
                            _existing_keys.add(_key)
                    if self.valves.ENABLE_DEBUG and _augment_edges:
                        print(f"[FaultLine Filter] regex augment added {len(_augment_edges)} correction edge(s) "
                              f"to LLM output")
                    # Full fallback: if LLM returned nothing at all, use ALL basic edges
                    if not raw_triples:
                        raw_triples = basic_edges
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] regex full fallback extracted {len(raw_triples)} basic fact(s)")

                _PRONOUNS = {"i", "me", "my", "we", "us", "our", "he", "she", "it", "they", "them"}
                edges = [
                    {
                        "subject": e["subject"],
                        "object": e["object"],
                        "rel_type": e["rel_type"],
                        "is_preferred_label": e.get("is_preferred_label", False),
                        "is_correction": e.get("is_correction", False),
                    }
                    for e in raw_triples
                    if (e.get("rel_type") == "pref_name" or not e.get("low_confidence", False))
                    and e.get("subject") and e.get("object") and e.get("rel_type")
                    and e.get("subject", "").lower() not in _PRONOUNS
                    and e.get("object", "").lower() not in _PRONOUNS
                ]

                # Guard: "user → also_known_as" edges require first-person self-ID or preference signal
                # to prevent false positives (e.g., "her name is Marla" → "user also_known_as marla").
                # But "user → pref_name" edges are always allowed — extraction itself is the intent signal.
                before = len(edges)
                edges = [
                    e for e in edges
                    if not (
                        e["subject"].lower() == "user"
                        and e["rel_type"].lower() == "also_known_as"
                        and not (_has_self_id or _has_preference_signal)
                    )
                ]
                if self.valves.ENABLE_DEBUG and len(edges) < before:
                    print(
                        f"[FaultLine Filter] dropped {before - len(edges)} "
                        f"user→also_known_as edge(s): no first-person self-ID or preference signal in text"
                    )

                # ALWAYS call ingest when will_ingest=True. /ingest owns the extraction pipeline.
                # Filter is dumb — backend is smart. Don't gate on local edge extraction.
                # Raw text already cached to Qdrant (line 1542). Backend extracts via LLM.
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] firing ingest (local edges={len(edges)})")
                await self._fire_ingest(clean_text, self.valves.DEFAULT_SOURCE, user_id, edges=edges)

            # Build and inject memory block from retrieved facts
            if will_query and (facts or preferred_names or canonical_identity):
                # Filtering already applied in cache hit/miss paths; use filtered facts directly
                _is_realtime = self._is_realtime_query(text)

                # Inject whenever we have anything useful — let the model decide relevance
                if facts or preferred_names or canonical_identity:
                    # Extract locations for temporal reasoning hint
                    locations = set()
                    for f in facts:
                        if f.get("rel_type") in ("lives_at", "lives_in", "works_for"):
                            obj = f.get("object", "").strip()
                            if obj and len(obj) > 1:
                                locations.add(obj)

                    # Score entity attributes against query before injection
                    _ATTR_CATEGORY_MAP = {
                        "height": "physical",
                        "weight": "physical",
                        "age": "temporal",
                        "born_on": "temporal",
                        "born_in": "temporal",
                        "nationality": "identity",
                        "occupation": "work",
                        "has_gender": "identity",
                    }

                    filtered_attributes = {}
                    for entity_id, attrs in entity_attributes.items():
                        filtered_attrs = {}
                        for attr, value in attrs.items():
                            synthetic_fact = {
                                "rel_type": attr,
                                "category": _ATTR_CATEGORY_MAP.get(attr, "identity"),
                                "confidence": 1.0,
                            }
                            score = self.calculate_relevance_score(synthetic_fact, text)
                            if score >= 0.0:
                                filtered_attrs[attr] = value
                        if filtered_attrs:
                            filtered_attributes[entity_id] = filtered_attrs

                    entity_attributes = filtered_attributes

                    # Resolve any remaining UUIDs to display names before building memory
                    facts = _resolve_display_names(facts, preferred_names, canonical_identity)

                    # HARD GUARD: validate no UUIDs leaked into resolved facts
                    # CLAUDE.md constraint: "display names must never be UUIDs in user-facing output"
                    # # NO RECURSIVE MATCHING — _UUID_RE is a static compile-once pattern
                    for _f in facts:
                        for _field in ("subject", "object"):
                            _val = _f.get(_field, "")
                            if _val and _UUID_RE.match(str(_val)):
                                _display = preferred_names.get(_val, _val)
                                if _display != _val:
                                    _f[_field] = _display
                                    if self.valves.ENABLE_DEBUG:
                                        print(f"[FaultLine Filter] uuid_guard: late-resolved {_field}={_val[:12]}→{_display}")

                    # Update conversation context for next turn
                    _update_conversation_context(user_id, facts, preferred_names)

                    # Detect whether user is directly asking about identity/preferences.
                    # Injects name directives only when the question warrants it (dBug-022).
                    _query_asks_about_identity = any(
                        sig in text.lower() for sig in _IDENTITY_QUERIES
                    )
                    memory_block = self._build_memory_block(
                        text, facts, preferred_names, canonical_identity, entity_attributes,
                        is_realtime=_is_realtime,
                        locations=sorted(list(locations)) if locations else None,
                        expose_preferences=_query_asks_about_identity,
                    )
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] injecting system message:\n{memory_block}")

                    # Insert immediately before the last user message for best context proximity
                    msgs = body["messages"]
                    injected = False
                    for i in range(len(msgs) - 1, -1, -1):
                        if msgs[i].get("role") == "user":
                            msgs.insert(i, {"role": "system", "content": memory_block})
                            injected = True
                            break
                    if not injected:
                        msgs.append({"role": "system", "content": memory_block})

                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] INJECTED total_messages={len(body['messages'])}")

                    # Emit visible status indicator when memory is injected
                    if __event_emitter__ and facts:
                        fact_count = len(facts)
                        await __event_emitter__({
                            "type": "status",
                            "data": {
                                "description": f"⊢ FaultLine — {fact_count} fact{('s' if fact_count != 1 else '')} loaded",
                                "done": True
                            }
                        })
                else:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] no facts to inject")

        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet error: {type(e).__name__}: {e}")

        # NUCLEAR OPTION: redact any UUID patterns from all messages before
        # returning to OpenWebUI. Catches UUIDs from ANY source: OpenWebUI
        # system prompts, prior messages, memory block, etc.
        _redact_uuids_from_body(body)

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        return body