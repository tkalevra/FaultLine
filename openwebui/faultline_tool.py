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

import httpx

FAULTLINE_URL = os.getenv("FAULTLINE_URL", "http://faultline:8000")


class Tools:
    """OpenWebUI Tools class for FaultLine WGM integration."""

    @staticmethod
    async def store_fact(
        text: str,
        edges: list[dict] | None = None,
        source: str = "openwebui",
    ) -> str:
        """
        Store a validated fact via the FaultLine WGM pipeline.

        Args:
            text:   The sentence or passage containing the fact.
            edges:  Optional list of explicit edges to validate and commit.
                    Each edge is {"subject": str, "object": str, "rel_type": str}.
                    If omitted, entities are extracted but nothing is committed.
            source: Provenance label recorded in the fact store.

        Returns:
            A short status string the model can relay to the user.
        """
        payload = {"text": text, "source": source}
        if edges:
            payload["edges"] = edges

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{FAULTLINE_URL}/ingest", json=payload)

        response.raise_for_status()
        data = response.json()

        status = data["status"]
        committed = data["committed"]
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
