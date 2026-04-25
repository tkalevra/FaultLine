"""
Tool name:   FaultLine WGM
Description: Validate and persist facts through the FaultLine WGM pipeline.
             Call this when you want to store a validated relationship as a
             long-term memory fact rather than relying on context window alone.

Usage (model-facing):
  store_fact(text="Alice works for Acme Corp.",
             edges=[{"subject": "Alice", "object": "Acme Corp", "rel_type": "WORKS_FOR"}])

Returns a human-readable string describing the validation outcome.
"""

import json
import os
from typing import Optional

import httpx


class Valves:
    """Configuration valves for FaultLine integration."""

    FAULTLINE_URL: str = os.getenv("FAULTLINE_URL", "http://faultline:8001")
    FAULTLINE_TIMEOUT: int = 20
    DEFAULT_SOURCE: str = "openwebui"
    ENABLE_DEBUG: bool = False


class Tools:
    """OpenWebUI Tools class for FaultLine WGM integration."""

    def __init__(self):
        self.valves = Valves()

    async def store_fact(
        self,
        text: str,
        edges: Optional[list[dict]] = None,
        source: Optional[str] = None,
    ) -> str:
        """
        Store a validated fact via the FaultLine WGM pipeline.

        Args:
            text:   The sentence or passage containing the fact.
            edges:  Optional list of explicit edges to validate and commit.
                    Each edge is {"subject": str, "object": str, "rel_type": str}.
                    If omitted, entities are extracted but nothing is committed.
            source: Provenance label recorded in the fact store (defaults to valve config).

        Returns:
            A short status string the model can relay to the user.
        """
        source = source or self.valves.DEFAULT_SOURCE
        payload = {"text": text, "source": source}
        if edges:
            payload["edges"] = edges

        if self.valves.ENABLE_DEBUG:
            print(f"[FaultLine Debug] POST {self.valves.FAULTLINE_URL}/ingest")
            print(f"[FaultLine Debug] Payload: {json.dumps(payload, indent=2)}")

        try:
            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/ingest", json=payload
                )
            response.raise_for_status()
        except httpx.ConnectError as e:
            return (
                f"[FaultLine] Connection failed to {self.valves.FAULTLINE_URL}. "
                f"Error: {str(e)}"
            )
        except httpx.TimeoutException:
            return (
                f"[FaultLine] Timeout connecting to {self.valves.FAULTLINE_URL}. "
                f"Increase FAULTLINE_TIMEOUT if needed."
            )
        except Exception as e:
            return f"[FaultLine] Request failed: {str(e)}"

        try:
            data = response.json()
        except json.JSONDecodeError:
            return f"[FaultLine] Invalid JSON response: {response.text[:200]}"

        status = data.get("status", "unknown")
        committed = data.get("committed", 0)
        entity_names = [e["entity"] for e in data.get("entities", [])]

        if status == "extracted":
            return (
                f"[FaultLine] Extracted {len(entity_names)} entities "
                f"({', '.join(entity_names)}). No edges committed."
            )
        if status == "valid":
            return f"[FaultLine] Committed {committed} fact(s). Entities: {', '.join(entity_names)}."
        if status == "novel":
            return (
                f"[FaultLine] Edge type not in ontology — queued for review. "
                f"Committed {committed} valid fact(s)."
            )
        if status == "conflict":
            return (
                f"[FaultLine] Conflict detected — contradicting edge exists. "
                f"Committed {committed} non-conflicting fact(s)."
            )
        return f"[FaultLine] status={status} committed={committed}"
