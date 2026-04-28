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
You are a fact extraction engine. Your only job is to convert natural language into structured relationship triples.

RULES — follow every rule exactly, no exceptions:
1. Output ONLY a JSON array. No preamble, no explanation, no markdown, no code fences.
2. Every entity must be a real named entity (a proper noun). Never use pronouns (I, my, he, she, they, we), never use "the user", never use generic nouns.
3. All subject and object values must be lowercase.
4. rel_type must be one of exactly: parent_of, child_of, spouse, sibling_of, also_known_as, works_for
5. For every nickname or alias mentioned, emit a separate also_known_as triple.
6. If you cannot confidently extract a relation, still include it with "low_confidence": true. Never silently drop ambiguous relations.
7. Do not infer relations that are not explicitly stated in the input.

OUTPUT SCHEMA (each item):
{"subject": string, "object": string, "rel_type": string, "low_confidence": boolean}"""


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
                    "messages": [
                        {"role": "system", "content": _TRIPLE_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 500,
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

        payload = {
            "text": text,
            "source": source,
            "edges": normalized_edges,
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
