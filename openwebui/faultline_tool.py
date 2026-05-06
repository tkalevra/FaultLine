"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.3.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

import asyncio
import json
import os
import re
import time as _time
from collections import defaultdict
from typing import Optional

import httpx
from pydantic import BaseModel


_REALTIME_SIGNALS: frozenset[str] = frozenset({
    "weather", "forecast", "temperature", "news", "today", "current",
    "right now", "live", "stock", "price", "score",
})

_QUERY_INTENT: dict[str, frozenset[str]] = {
    "location": frozenset({"weather", "where", "address", "forecast", "city",
                           "located", "location", "home", "residence", "town"}),
    "family":   frozenset({"family", "children", "kids", "spouse", "wife", "husband",
                           "parent", "parents", "sibling", "brother", "sister",
                           "son", "daughter", "partner"}),
    "work":     frozenset({"work", "job", "career", "employer", "employed",
                           "company", "occupation", "profession", "office"}),
    "physical": frozenset({"height", "weight", "tall", "heavy", "body", "size"}),
    "pets":     frozenset({"pet", "dog", "cat", "animal", "fish", "bird",
                           "hamster", "rabbit", "snake"}),
    "identity": frozenset({"name", "who am i", "call me", "known as", "alias"}),
    "temporal":  frozenset({"birthday", "born", "birth", "anniversary",
                            "age", "when was", "how old", "date of birth"}),
}

_REL_CATEGORY: dict[str, str] = {
    "lives_at": "location", "lives_in": "location", "located_in": "location",
    "address": "location", "born_in": "location",
    "parent_of": "family", "child_of": "family", "spouse": "family", "sibling_of": "family",
    "works_for": "work", "occupation": "work",
    "height": "physical", "weight": "physical", "has_gender": "physical",
    "has_pet": "pets", "instance_of": "pets",
    "also_known_as": "identity", "pref_name": "identity", "same_as": "identity",
    "is_a": "identity",
    "born_on": "temporal", "age": "temporal", "anniversary_on": "temporal", "met_on": "temporal",
}


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

# Session memory cache — keyed by user_id, value: (timestamp, facts, preferred_names, canonical_identity, entity_attributes)
_SESSION_MEMORY_CACHE: dict[str, tuple] = {}
_SESSION_MEMORY_TTL: int = 30  # seconds

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

REL_TYPE REFERENCE:
- also_known_as: nickname or alternate name.
- pref_name: explicitly preferred name ("goes by", "prefers to be called", "preferred name is"). Subject is always the named person, never "user".
- is_a: type or category. has_pet: person owns an animal (NEVER a person).
- Common: spouse, parent_of, child_of, sibling_of, works_for, lives_at, likes, dislikes, owns, age, height, weight, born_on, anniversary_on, met_on.
- Use snake_case. Other types allowed if none fit.

SELF-ID: Explicit first-person self-identification only ("I am X", "my name is X", "call me X"):
→ {"subject":"user","object":"x","rel_type":"also_known_as","low_confidence":false}
NEVER apply to third-person text. NEVER emit subject="user" from "she/he prefers...".

CORRECTIONS: If text signals a correction ("actually", "not X it's Y", "I meant"):
→ add "is_correction":true to the corrected triple.

UNITS: age→number only. height→feet format (6ft, 5'10"). weight→pounds (230lb).
Self-statements for height/weight → subject="user".

DATES AND EVENTS:
- For birthday/birth date patterns ("my birthday is X", "born on X", "I was born on X", "born in X"):
  emit {"subject":"user","object":"<date>","rel_type":"born_on"} where object is the date as stated (e.g. "may 3", "march 15th", "1988-04-02"). Normalize to lowercase.
- For named recurring events or anniversaries ("our anniversary is X", "X's birthday is Y"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on"} or "anniversary_on" as appropriate.
- For meeting/event dates ("we met on X", "we got married on X"):
  emit the appropriate rel_type (met_on, married_on) with the date as object.
- Date values must be the date string only — never a name or description.
- If a month/day pattern appears without a year, emit it as-is ("may 3rd", "december 25").

OUTPUT: [{"subject":"...","object":"...","rel_type":"...","low_confidence":false}]
If nothing to extract: []"""


async def rewrite_to_triples(text: str, valves, context: list[dict] = None, typed_entities: list[dict] = None, memory_facts: list[dict] = None) -> list[dict]:
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
                    if content.strip():
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
            for f in memory_facts:
                subj = f.get("subject", "")
                obj = f.get("object", "")
                rel = f.get("rel_type", "")
                if subj and obj and rel:
                    # Use USER placeholder for the canonical user identity
                    subj_display = "USER" if subj in ("user",) else subj
                    obj_display = "USER" if obj in ("user",) else obj
                    entity_lines.append(f"- {obj_display} ({rel} of {subj_display})")
            if entity_lines:
                hint = (
                    "Known entities (for pronoun resolution only — do not store these as new facts):\n"
                    + "\n".join(entity_lines)
                )
                messages.append({"role": "system", "content": hint})

        user_content = text
        if typed_entities:
            entity_lines = "\n".join(
                f"- {e.get('subject')} (type: {e.get('subject_type', 'unknown')})"
                f" {e.get('rel_type')} {e.get('object')} (type: {e.get('object_type', 'unknown')})"
                for e in typed_entities
                if e.get("subject") and e.get("object")
            )
            user_content = (
                f"{text}\n\n"
                f"GLiNER2 has pre-classified these entities from the text:\n{entity_lines}\n"
                f"Use these entity types to guide relationship selection. "
                f"A Person cannot be owned. An Animal cannot be a spouse. "
                f"Respect these types strictly."
            )
        messages.append({"role": "user", "content": user_content})

        async with httpx.AsyncClient(timeout=valves.QWEN_TIMEOUT) as client:
            response = await client.post(
                valves.QWEN_URL,
                json={
                    "model": valves.QWEN_MODEL,
                    "messages": messages,
                    "temperature": 0.0,
                    "max_tokens": 400,
                    "thinking": {"type": "disabled"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            triples = json.loads(content)
            if not isinstance(triples, list):
                return []
            return triples
    except httpx.HTTPStatusError as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples HTTP error: {e.response.status_code}")
            print(f"[FaultLine] rewrite_to_triples response body: {e.response.text}")
        return []
    except Exception as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples failed: {e}")
        return []


class Filter:
    """
    OpenWebUI Filter for FaultLine WGM Integration.

    inlet:  extract and commit facts (fire-and-forget), query for memory and inject as system message
    outlet: pass-through
    """

    class Valves(BaseModel):
        FAULTLINE_URL: str = "http://192.168.40.10:8001"
        FAULTLINE_TIMEOUT: int = 30
        QWEN_URL: str = os.getenv("QWEN_URL", "http://192.168.40.20:1234/v1/chat/completions")
        QWEN_MODEL: str = "qwen/qwen3.5-9b"
        QWEN_TIMEOUT: int = 10
        DEFAULT_SOURCE: str = "openwebui"
        ENABLE_DEBUG: bool = False
        ENABLED: bool = True
        INGEST_ENABLED: bool = True
        QUERY_ENABLED: bool = True
        RETRACTION_ENABLED: bool = True
        MAX_MEMORY_SENTENCES: int = 10
        MAX_CONTEXT_TURNS: int = 3
        MIN_INJECT_CONFIDENCE: float = 0.5

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
                return response.json()
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

    def _is_realtime_query(self, text: str) -> bool:
        tl = text.lower()
        return any(sig in tl for sig in _REALTIME_SIGNALS)

    def _categorize_query(self, text: str) -> set[str]:
        tl = text.lower()
        return {cat for cat, kws in _QUERY_INTENT.items() if any(kw in tl for kw in kws)}

    def _filter_relevant_facts(self, facts: list[dict], categories: set[str], identity: Optional[str], is_realtime: bool = False) -> list[dict]:
        def _garbage(name: str) -> bool:
            n = (name or "").strip().lower()
            return len(n) <= 1 or n == "x" or bool(_UUID_RE.match(n))

        _IDENTITY_RELS = {"also_known_as", "pref_name", "same_as"}
        clean = [f for f in facts
                 if not _garbage(f.get("subject", "")) and not _garbage(f.get("object", ""))]

        # Categories detected from query text via _QUERY_INTENT
        text_cats = categories  # already computed by _categorize_query

        # Categories present in the actual fact payloads from /query
        fact_cats = {f.get("category") for f in clean if f.get("category")}

        # Allowed rel_types from static map for text-matched categories
        static_allowed = {rt for rt, cat in _REL_CATEGORY.items() if cat in text_cats}

        # Baseline categories always included (location, work, identity, temporal for identity anchor)
        _BASELINE_CATS = {"location", "work", "identity", "temporal"}

        if is_realtime:
            allowed_cats = _BASELINE_CATS | {"location"}
        elif text_cats:
            allowed_cats = _BASELINE_CATS | text_cats
        else:
            allowed_cats = _BASELINE_CATS

        # Build final allowed rel_types from both sources:
        # 1. Static _REL_CATEGORY map (existing types)
        # 2. fact.category field from server payload (DB-driven, catches new types)
        allowed = (
            _IDENTITY_RELS
            | static_allowed
            | {f.get("rel_type") for f in clean
               if f.get("category") in allowed_cats and f.get("rel_type")}
        )

        filtered = [f for f in clean if f.get("rel_type", "") in allowed]

        # Confidence gate: drop facts below threshold
        min_conf = self.valves.MIN_INJECT_CONFIDENCE
        if min_conf > 0:
            high_conf = [f for f in filtered if f.get("confidence", 1.0) >= min_conf]
            if high_conf:
                filtered = high_conf

        return filtered if filtered else clean

    def _build_realtime_context(self, facts: list[dict], identity: Optional[str]) -> Optional[str]:
        """Build realtime directive from facts. Returns None if no location found."""
        # Resolve location from lives_at/lives_in facts (prefer lives_at)
        location = None
        for f in facts:
            if f.get("rel_type") == "lives_at" and identity and f.get("subject") in {identity, "user"}:
                loc = f.get("object", "").strip()
                if loc:
                    location = loc
                    break

        if not location:
            for f in facts:
                if f.get("rel_type") == "lives_in" and identity and f.get("subject") in {identity, "user"}:
                    loc = f.get("object", "").strip()
                    if loc:
                        location = loc
                        break

        if not location:
            return None

        # Build compact facts for context (e.g., work)
        compact_parts = []
        for f in facts:
            if f.get("rel_type") == "works_for" and identity and f.get("subject") in {identity, "user"}:
                obj = f.get("object", "").strip()
                if obj:
                    compact_parts.append(f"works at {obj.title()}")

        directive = f"The user's location is {location.title()}."
        if compact_parts:
            directive += f" Known context for this request: {'; '.join(compact_parts)}."
        directive += " Use whatever tools or capabilities are available to fulfill this request directly. Do not ask the user to supply information that is already known above."

        return directive

    def _detect_retraction_intent(self, text: str) -> bool:
        tl = text.lower().replace("'", "'").replace("'", "'")
        return any(sig in tl for sig in _RETRACTION_SIGNALS)

    async def _extract_retraction(self, text: str, context: list[dict]) -> dict:
        try:
            messages = [{"role": "system", "content": _RETRACTION_PROMPT}]
            for msg in (context or [])[-2:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    c = msg.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
                    messages.append({"role": msg["role"], "content": str(c)[:400]})
            messages.append({"role": "user", "content": text})
            async with httpx.AsyncClient(timeout=self.valves.QWEN_TIMEOUT) as client:
                resp = await client.post(
                    self.valves.QWEN_URL,
                    json={"model": self.valves.QWEN_MODEL, "messages": messages,
                          "temperature": 0.0, "max_tokens": 100,
                          "thinking": {"type": "disabled"}},
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
        facts: list[dict],
        preferred_names: dict,
        canonical_identity: Optional[str],
        entity_attributes: dict,
        is_realtime: bool = False,
        locations: Optional[list[str]] = None,
    ) -> str:
        identity_display = preferred_names.get("user")
        identity = canonical_identity or identity_display

        # Check if we should use realtime mode
        if is_realtime:
            realtime_context = self._build_realtime_context(facts, identity)
            if realtime_context:
                # REALTIME MODE with location
                lines = []

                if identity:
                    display = identity_display or identity
                    lines.append(
                        f"The user is '{display}'. Facts referencing '{identity}' or '{display}' are about the user directly. "
                        f"Address the user as 'you', never as a third party."
                    )

                lines.append("DIRECTLY ACTIONABLE: Use the facts and context below immediately to fulfill the request.")
                lines.append(realtime_context)

                return (
                    "🧠 FaultLine Memory — treat these as established ground truth for this response.\n"
                    + "\n".join(f"- {l}" for l in lines)
                    + "\nOnly reference what the facts explicitly say. Do not invent details not present.\n"
                    + "If a fact below is relevant to fulfilling the user's request, act on it directly and immediately."
                )
            else:
                # No location found - fall through to conversational mode
                is_realtime = False

        # CONVERSATIONAL MODE (from is_realtime=False or fallthrough)
        lines = [
            "🧠 FaultLine Memory — reference context. Use facts below only when directly relevant to the current request. Do not volunteer, list, or recite facts unless the user's message explicitly requires them."
        ]

        if identity:
            display = identity_display or identity
            lines.append(
                f"The user is '{display}'. Facts referencing '{identity}' or '{display}' are about the user directly. "
                f"Address the user as 'you', never as a third party."
            )

        if preferred_names:
            for canonical, preferred in preferred_names.items():
                if canonical == "user" or canonical == identity:
                    continue
                lines.append(f"Always call {canonical.title()} by '{preferred.title()}'.")

        if facts:
            nickname_map = {}
            by_rel = defaultdict(list)

            _user_anchors = {identity, "user"} if identity else {"user"}

            for f in facts:
                by_rel[f.get("rel_type", "")].append(f)
                if f.get("rel_type") == "also_known_as":
                    subj = f.get("subject", "")
                    alias = f.get("object", "")
                    if subj and alias and subj != identity:
                        nickname_map[subj] = alias

            def _dn(name: str) -> str:
                return nickname_map.get(name, name).title()

            # Extract family relationships
            children_raw = [f.get("object") for f in by_rel.get("parent_of", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]
            spouses_raw = [f.get("object") for f in by_rel.get("spouse", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]
            spouses_raw += [f.get("subject") for f in by_rel.get("spouse", []) if identity and f.get("object") in _user_anchors and f.get("subject") not in spouses_raw and not bool(_UUID_RE.match(f.get("subject") or ""))]
            siblings_raw = [f.get("object") for f in by_rel.get("sibling_of", []) if identity and f.get("subject") in _user_anchors and not bool(_UUID_RE.match(f.get("object") or ""))]

            # Build compact family line
            if children_raw or spouses_raw or siblings_raw:
                family_parts = []
                if spouses_raw:
                    family_parts.append(f"spouse={', '.join(_dn(s) for s in set(spouses_raw))}")
                if children_raw:
                    family_parts.append(f"children={', '.join(_dn(c) for c in children_raw)}")
                if siblings_raw:
                    family_parts.append(f"siblings={', '.join(_dn(s) for s in siblings_raw)}")
                lines.append(f"family: {', '.join(family_parts)}")

            # Build compact fact lines
            covered = {"parent_of", "child_of", "spouse", "sibling_of", "also_known_as", "pref_name"}
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

                if identity and subj in _user_anchors:
                    lines.append(f"{rel}: {obj}")
                elif identity and obj in _user_anchors:
                    lines.append(f"{_dn(subj)} ← {rel}")
                else:
                    lines.append(f"{_dn(subj)} → {rel} → {_dn(obj)}")

            # User entity attributes (compact single line)
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

            # Non-user entity attributes
            if entity_attributes:
                for entity, attrs in entity_attributes.items():
                    if entity == identity:
                        continue
                    parts = []
                    if "age" in attrs:
                        parts.append(f"age {attrs['age']}")
                    if parts:
                        lines.append(f"{_dn(entity)}: {', '.join(parts)}.")

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
    ) -> dict:
        print(f"[FaultLine Filter] inlet CALLED enabled={self.valves.ENABLED} debug={self.valves.ENABLE_DEBUG}")
        if not self.valves.ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if not text:
                return body

            user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] user_id={user_id} text='{text[:80]}'")

            # Retraction detection — check before normal ingest
            if self.valves.RETRACTION_ENABLED and self._detect_retraction_intent(text):
                retraction = await self._extract_retraction(text, body.get("messages", []))
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
                        return body

            _THIRD_PERSON_PREF_SIGNALS: frozenset[str] = frozenset({
                "call her", "call him", "call them",
                "her name is", "his name is", "their name is",
            })
            _has_third_person_pref = any(sig in text.lower() for sig in _THIRD_PERSON_PREF_SIGNALS)

            will_ingest = self.valves.INGEST_ENABLED and (
                len(text.split()) >= 5
                or bool(_IDENTITY_RE.search(text))
                or _has_third_person_pref
            )
            will_query = self.valves.QUERY_ENABLED

            if not will_ingest and not will_query:
                return body

            # Initialize memory variables for potential use by rewrite_to_triples
            facts, preferred_names, canonical_identity, entity_attributes = [], {}, None, {}

            # Run /query first (with caching) so memory facts can aid pronoun resolution during ingest
            if will_query:
                try:
                    cached = _SESSION_MEMORY_CACHE.get(user_id)
                    if cached and (_time.time() - cached[0]) < _SESSION_MEMORY_TTL:
                        _, facts, preferred_names, canonical_identity, entity_attributes = cached
                        raw_facts_for_extraction = list(facts)
                        # Filter cached facts for this specific query
                        _categories = self._categorize_query(text)
                        _is_realtime = self._is_realtime_query(text)
                        if _is_realtime:
                            _categories.add("location")
                        facts = self._filter_relevant_facts(
                            facts, _categories, canonical_identity, is_realtime=_is_realtime
                        )
                        if self.valves.ENABLE_DEBUG:
                            print(f"[FaultLine Filter] /query cache hit user_id={user_id}")
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
                            _categories = self._categorize_query(text)
                            _is_realtime = self._is_realtime_query(text)
                            if _is_realtime:
                                _categories.add("location")
                            facts = self._filter_relevant_facts(
                                facts, _categories, canonical_identity, is_realtime=_is_realtime
                            )

                            if self.valves.ENABLE_DEBUG:
                                print(f"[FaultLine Filter] facts={len(facts)} preferred_names={preferred_names} identity={canonical_identity}")
                                for f in facts:
                                    print(f"[FaultLine Filter]   fact: {f.get('subject')} -{f.get('rel_type')}-> {f.get('object')}")

                except httpx.ConnectError as e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] /query connection error: {e}")
                except httpx.TimeoutException as e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] /query timeout: {e}")
                except Exception as e:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] /query error: {type(e).__name__}: {e}")

            if will_ingest:
                _MEMORY_MARKER = "🧠 The following facts were previously stored by the user in their personal knowledge graph (FaultLine)."
                clean_text = text.split(_MEMORY_MARKER)[0].strip() if _MEMORY_MARKER in text else text

                typed_entities = await self._fetch_entities(clean_text, user_id)
                raw_triples = await rewrite_to_triples(
                    clean_text, self.valves, context=body.get("messages", []),
                    typed_entities=typed_entities if typed_entities else None,
                    memory_facts=raw_facts_for_extraction if raw_facts_for_extraction else None,
                )
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] raw_triples={raw_triples}")

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
                _has_self_id = bool(_IDENTITY_RE.search(clean_text))
                _has_preference_signal = any(
                    signal in clean_text.lower()
                    for signal in {
                        "call me", "please call me", "prefer to be called",
                        "i prefer", "i'd prefer", "i would prefer",
                        "goes by", "go by", "known as", "prefer you call me",
                        "would like to be called", "like to go by",
                        "call her", "call him", "call them",  # ← add these
                        "her name is", "his name is",
                    }
                )
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

                # Only call ingest when there are edges to commit or the text contains
                # a self-ID pattern that GLiNER2 should process server-side.
                # Skipping for query-like text (0 edges, no self-ID) prevents an unnecessary
                # GLiNER2 crash on texts with no extractable entities.
                if edges or _has_self_id:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] firing ingest edges={len(edges)}")
                    asyncio.create_task(
                        self._fire_ingest(clean_text, self.valves.DEFAULT_SOURCE, user_id, edges=edges)
                    )
                elif self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] skipping ingest — no edges and no self-ID")

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
                    memory_block = self._build_memory_block(
                        facts, preferred_names, canonical_identity, entity_attributes,
                        is_realtime=_is_realtime, locations=sorted(list(locations)) if locations else None
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
                else:
                    if self.valves.ENABLE_DEBUG:
                        print(f"[FaultLine Filter] no facts to inject")

        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet error: {type(e).__name__}: {e}")

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        return body