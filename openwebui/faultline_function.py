"""
FaultLine WGM Tool for OpenWebUI v0.9.2 Admin → Functions
Store validated facts through the FaultLine WGM pipeline with user-facing status feedback.
"""

import json
import os
from typing import Callable, Optional

import httpx
from pydantic import BaseModel


class Function:
    """OpenWebUI v0.9.2 Function for FaultLine WGM tool."""

    class Valves(BaseModel):
        """Configuration valves for FaultLine integration."""

        FAULTLINE_URL: str = "http://faultline:8001"
        FAULTLINE_TIMEOUT: int = 20
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

        IMPORTANT: You MUST provide edges. This tool does nothing useful without them.
        Always include at least one edge describing the relationship found in the text.

        Args:
            text:   The sentence or passage containing the fact.
            edges:  Required. A list of explicit edges to validate and commit.
                    Each edge is {"subject": str, "object": str, "rel_type": str}.
                    subject is the entity ORIGINATING the relationship.
                    object  is the entity RECEIVING the relationship.
                    All values must be lowercase.
            source: Provenance label recorded in the fact store (defaults to valve config).

        Available rel_types (use exactly as written, lowercase):
            parent_of   subject=parent      object=child
            child_of    subject=child       object=parent
            works_for   subject=employee    object=employer
            created_by  subject=creation    object=creator
            kills       subject=agent       object=target
            part_of     subject=component   object=whole
            is_a        subject=subtype     object=supertype

        Directionality examples:
            "Tom is Jenny's father"       → subject=tom,       object=jenny,     rel_type=parent_of
            "Jenny is Tom's daughter"     → subject=jenny,     object=tom,       rel_type=child_of
            "Alice works for Acme Corp"   → subject=alice,     object=acme corp, rel_type=works_for
            "Bob created the algorithm"   → subject=algorithm, object=bob,       rel_type=created_by
        """

        # Check ENABLED first — scope valve
        if not self.valves.ENABLED:
            return "[FaultLine] Tool is disabled."

        # Validate edges are present
        if not edges:
            await self._emit(
                __event_emitter__,
                "[FaultLine] No edges provided — nothing to commit. "
                "Available rel_types: parent_of, child_of, works_for, "
                "created_by, kills, part_of, is_a"
            )
            return (
                "[FaultLine] No edges provided — nothing to commit. "
                "You must supply edges with subject, object, and rel_type. "
                "Available rel_types: parent_of, child_of, works_for, "
                "created_by, kills, part_of, is_a"
            )

        # Normalize edges to lowercase
        normalized_edges = self._normalize_edges(edges)

        # Check if any edges survived normalization
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

        # Build payload
        payload = {
            "text": text,
            "source": source,
            "edges": normalized_edges,
        }

        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine Debug] POST {self.valves.FAULTLINE_URL}/ingest")
            print(f"[FaultLine Debug] Payload: {json.dumps(payload, indent=2)}")

        # Emit start status
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

        # Emit final status with done flag
        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": result, "done": True}}
            )

        return result
