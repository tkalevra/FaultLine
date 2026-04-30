"""
FaultLine WGM Tool for OpenWebUI v0.9.2 Admin → Functions
Store validated facts through the FaultLine WGM pipeline with user-facing status feedback.
"""

import json
import os
from typing import Callable, Optional

import httpx
from pydantic import BaseModel


_TRIPLE_SYSTEM_PROMPT = """\
You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array. No markdown, no explanation, no code fences.

RULES:

ENTITY NAMING RULES (strictly enforced):
- NEVER use "i", "me", "my", "we", "our", "myself" as subject or object in ANY triple regardless of rel_type. This is an absolute rule with zero exceptions.
- If the subject of a fact is ambiguous due to pronouns, resolve it to the nearest named entity in the sentence. For "Marla, who prefers to be called Mars", the subject is "marla" not "i".
- For preference patterns ("X prefers Y", "X goes by Y", "X is called Y"), the subject is always the person being described, never the speaker.
- Entity names must be proper nouns or named entities only. Never common nouns, pronouns, or role labels (e.g. not "user", "person", "speaker").

RELATIONSHIP RULES (strictly enforced):
- "child_of" means the subject IS the child, the object IS the parent. Example: "henry child_of thomas" means henry is thomas's child.
- "parent_of" means the subject IS the parent, the object IS the child. Example: "thomas parent_of henry" means thomas is henry's parent.
- Never emit child_of or parent_of between two siblings. If two entities share the same parent, emit sibling_of between them instead.
- For "X and Y are children of Z", emit three triples: Z parent_of X, Z parent_of Y, X sibling_of Y.
- Directionality is absolute. When in doubt, prefer parent_of with the parent as subject.
- NEVER emit child_of where the subject is the person speaking or the established user identity. If the user says "I have children" or "we have children", emit parent_of with the user as subject, never child_of with the user as subject.

1. Extract ALL factual assertions — people, animals, objects, places, preferences.
2. Entities must be specific names or nouns. Lowercase all values.
3. rel_type must be snake_case. Use the most precise label that fits.
   Common types: is_a, has_pet, parent_of, child_of, spouse, sibling_of,
   also_known_as, works_for, likes, dislikes, prefers, lives_at, owns,
   age, height, weight.
   You may use other snake_case types if none of the above fit.
4. also_known_as = nickname or alias ONLY (e.g. "Theo" for "Theodore").
5. is_a = type or category (e.g. "morkie is_a dog breed").
6. has_pet = person owns an animal (e.g. "we have a dog named Biskit").
7. For "X is a Y" patterns use is_a. For "named X" patterns use also_known_as
   between the descriptor and the name.
8. Pronoun resolution: replace he/she/it with the named entity if clear.
9. If unsure, set "low_confidence": true. Never return empty if facts exist.
10. First-person "my/our/we/I/me" refers to the named user entity if established,
    otherwise use subject "user".
11. If the message contains a self-identification pattern ("I am X", "I'm X",
    "my name is X", "call me X"), emit an also_known_as triple:
    {"subject":"user","object":"x","rel_type":"also_known_as","low_confidence":false}
    where X is the proper name, lowercased.
    The subject is ALWAYS "user", the object is ALWAYS the name. Never reverse this.
    NEVER emit {"subject":"name","object":"user"} — that direction is always wrong.
12. For age patterns ("X age 12", "X, age 12", "X who is 12"):
    emit {"subject":"x","object":"12","rel_type":"age"} where object is the NUMBER only.
    NEVER use a nickname or name as the age value.
    If the sentence contains both an age AND a nickname (e.g. "Desmonde age 12, goes by Des"),
    emit TWO separate triples:
    {"subject":"desmonde","object":"12","rel_type":"age"}
    {"subject":"desmonde","object":"des","rel_type":"also_known_as"}
13. For height patterns ("X is 6ft tall", "X height 6’", "X stands 6 feet", "X is 6’ tall"):
    emit {"subject":"x","object":"6ft","rel_type":"height"} where object is the height in feet (e.g. "6ft", "5'10\"").
    Normalize units to feet/inches format. Use ' for feet, \" for inches.
    For self-statements ("I am 6’ tall", "I'm 6ft", "my height is 6 feet"), emit {"subject":"user","object":"6ft","rel_type":"height"}.
14. For weight patterns ("X weighs 230lbs", "X weight 230 pounds", "X is 230lb"):
    emit {"subject":"x","object":"230lb","rel_type":"weight"} where object is the weight in pounds (e.g. "230lb").
    Normalize units to pounds.
    For self-statements ("I weigh 230lbs", "I'm 230lb", "my weight is 230 pounds"), emit {"subject":"user","object":"230lb","rel_type":"weight"}.
15. CORRECTION DETECTION: If the message indicates a prior fact was wrong
    ("actually", "his name is", "it's supposed to be", "I meant",
    "not X, it's Y", "correct that to"), extract the correction as a new
    triple with the corrected value. Use prior context to resolve the
    subject. Mark corrected triples with "is_correction": true.
    e.g. "oh his name is actually Biscuit" (context: dog named Biskit) →
    {"subject":"biskit","object":"biscuit","rel_type":"also_known_as",
     "is_correction":true,"low_confidence":false}

OUTPUT: [{"subject":"...","object":"...","rel_type":"...","low_confidence":false}]
If nothing to extract: []"""


async def rewrite_to_triples(text: str, valves) -> list[dict]:
    """
    Send text to the Qwen model and parse the returned JSON triple array.
    Returns [] on any failure so the caller can handle the empty-edge case.
    """
    try:
        async with httpx.AsyncClient(timeout=valves.QWEN_TIMEOUT) as client:
            response = await client.post(
                valves.QWEN_URL,
                json={
                    "model": valves.QWEN_MODEL,
                    "messages": [
                        {"role": "system", "content": _TRIPLE_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "repeat_penalty": 1.0,
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
    except Exception as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples failed: {e}")
        return []


class Function:
    """OpenWebUI v0.9.2 Function for FaultLine WGM tool."""

    class Valves(BaseModel):
        """Configuration valves for FaultLine integration."""

        FAULTLINE_URL: str = "http://faultline:8001"
        FAULTLINE_TIMEOUT: int = 20
        QWEN_URL: str = os.getenv("QWEN_URL", "http://192.168.40.20:1234/v1/chat/completions")
        QWEN_MODEL: str = "qwen/qwen3.5-9b"
        QWEN_TIMEOUT: int = 10
        DEFAULT_SOURCE: str = "openwebui"
        ENABLE_DEBUG: bool = False
        ENABLED: bool = True

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(
        self, __event_emitter__: Optional[Callable], status: str
    ) -> None:
        """Emit a status event to the user via EventEmitter."""
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": status, "done": False}}
            )

    def _normalize_edges(self, edges: list[dict]) -> list[dict]:
        """
        Normalize edge values to lowercase and strip whitespace.
        Silently drop edges missing any required field.
        """
        return [
            {
                "subject": e.get("subject", "").lower().strip(),
                "object": e.get("object", "").lower().strip(),
                "rel_type": e.get("rel_type", "").lower().strip(),
            }
            for e in edges
            if e.get("subject") and e.get("object") and e.get("rel_type")
        ]

    async def store_fact(
        self,
        text: str,
        edges: Optional[list[dict]] = None,
        source: Optional[str] = None,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """
        Store a validated fact via the FaultLine WGM pipeline.

        The model should call this with the sentence or passage containing the fact.
        Edges are rewritten by Qwen before commit — explicit edges passed by the model
        are overridden by the Qwen extraction.

        Args:
            text:   The sentence or passage containing the fact.
            edges:  Ignored — edges are derived from text via Qwen rewrite.
            source: Provenance label recorded in the fact store (defaults to valve config).

        Available rel_types:
            parent_of, child_of, spouse, sibling_of, also_known_as, works_for
        """

        if not self.valves.ENABLED:
            return "[FaultLine] Tool is disabled."

        await self._emit(__event_emitter__, "Extracting triples...")

        # Rewrite text to clean triples via Qwen — overrides any model-supplied edges
        raw_triples = await rewrite_to_triples(text, self.valves)

        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine] raw_triples: {json.dumps(raw_triples, indent=2)}")

        # Strip low-confidence edges
        confident = [e for e in raw_triples if not e.get("low_confidence", False)]

        # Remove the low_confidence key — EdgeInput does not accept it
        edges = [
            {"subject": e["subject"], "object": e["object"], "rel_type": e["rel_type"]}
            for e in confident
            if e.get("subject") and e.get("object") and e.get("rel_type")
        ]

        # Validate edges are present
        if not edges:
            await self._emit(
                __event_emitter__,
                "[FaultLine] No confident triples extracted — nothing to commit."
            )
            return (
                "[FaultLine] No confident triples extracted — nothing to commit. "
                "Available rel_types: parent_of, child_of, spouse, sibling_of, "
                "also_known_as, works_for"
            )

        # Normalize edges to lowercase
        normalized_edges = self._normalize_edges(edges)

        if not normalized_edges:
            await self._emit(
                __event_emitter__,
                "[FaultLine] All edges were invalid or incomplete. "
                "Each edge must have subject, object, and rel_type."
            )
            return (
                "[FaultLine] All edges were invalid or incomplete. "
                "Each edge must have subject, object, and rel_type."
            )

        source = source or self.valves.DEFAULT_SOURCE
        user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"

        payload = {
            "text": text,
            "source": source,
            "edges": normalized_edges,
            "user_id": user_id,
        }

        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine Debug] POST {self.valves.FAULTLINE_URL}/ingest")
            print(f"[FaultLine Debug] Payload: {json.dumps(payload, indent=2)}")

        await self._emit(__event_emitter__, "Validating facts...")

        try:
            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/ingest", json=payload
                )
            response.raise_for_status()
        except httpx.ConnectError as e:
            error_msg = (
                f"[FaultLine] Connection failed to {self.valves.FAULTLINE_URL}. "
                f"Error: {str(e)}"
            )
            await self._emit(__event_emitter__, error_msg)
            return error_msg
        except httpx.TimeoutException:
            error_msg = (
                f"[FaultLine] Timeout connecting to {self.valves.FAULTLINE_URL}. "
                f"Increase FAULTLINE_TIMEOUT if needed."
            )
            await self._emit(__event_emitter__, error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"[FaultLine] Request failed: {str(e)}"
            await self._emit(__event_emitter__, error_msg)
            return error_msg

        try:
            data = response.json()
        except json.JSONDecodeError:
            error_msg = f"[FaultLine] Invalid JSON response: {response.text[:200]}"
            await self._emit(__event_emitter__, error_msg)
            return error_msg

        status = data.get("status", "unknown")
        committed = data.get("committed", 0)
        entity_names = [e["entity"] for e in data.get("entities", [])]

        if status == "extracted":
            result = (
                f"[FaultLine] Extracted {len(entity_names)} entities "
                f"({', '.join(entity_names)}). No edges committed."
            )
        elif status == "valid":
            result = f"[FaultLine] Committed {committed} fact(s). Entities: {', '.join(entity_names)}."
        elif status == "novel":
            result = (
                f"[FaultLine] Edge type not in ontology — queued for review. "
                f"Committed {committed} valid fact(s)."
            )
        elif status == "conflict":
            result = (
                f"[FaultLine] Conflict detected — contradicting edge exists. "
                f"Committed {committed} non-conflicting fact(s)."
            )
        else:
            result = f"[FaultLine] status={status} committed={committed}"

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": result, "done": True}}
            )

        return result
