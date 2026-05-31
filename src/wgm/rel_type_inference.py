"""
Rel-Type Inference: Call LLM to infer metadata for novel relationship types.
Enables database-driven growth of the ontology.
"""

import json
import httpx
import logging
import asyncio
from typing import Dict, Any, Optional

log = logging.getLogger(__name__)


async def infer_rel_type_metadata(
    rel_type: str,
    context: str,
    llm_url: str,
    llm_model: str,
    timeout: float = 30.0
) -> Dict[str, Any]:
    """
    Call LLM to infer metadata for a novel rel_type.

    Returns dict with:
    {
        "is_symmetric": bool,
        "inverse_rel_type": str or None,
        "head_types": list[str],
        "tail_types": list[str],
        "is_hierarchy_rel": bool,
        "category": str,
        "confidence": float
    }
    """

    prompt = f"""TASK: Infer relationship type metadata for a novel relationship.

RELATIONSHIP TYPE: {rel_type}
CONTEXT: {context}

INFER METADATA:
1. is_symmetric: Is this relationship bidirectional? (e.g., spouse=true, parent_of=false)
2. inverse_rel_type: If asymmetric, what's the inverse? (e.g., parent_of↔child_of)
3. head_types: What entity types can be the subject? (e.g., ["Person"] or ["Person", "Organization"] or ["Any"])
4. tail_types: What entity types can be the object? (e.g., ["Person"] or ["Scalar"] or ["Any"])
5. is_hierarchy_rel: Does this define composition/classification? (instance_of=true, works_for=false)
6. category: Family/Identity/Location/Work/Physical/Temporal/Pets/Other

CONSTRAINTS:
- is_symmetric: Must be boolean
- inverse_rel_type: If is_symmetric=false, must provide. If true, can be null.
- head_types: Must be list of entity types (["Person"], ["Organization"], ["Person","Organization"], ["Any"])
- tail_types: Must be ["Scalar"] or list of entity types
- is_hierarchy_rel: Must be boolean
- category: Must be one of: Family, Identity, Location, Work, Physical, Temporal, Pets, Other
- confidence: Your confidence in this metadata (0.0-1.0)

Output JSON (no markdown, raw JSON only):
{{
  "is_symmetric": <bool>,
  "inverse_rel_type": <string or null>,
  "head_types": <list>,
  "tail_types": <list>,
  "is_hierarchy_rel": <bool>,
  "category": "<string>",
  "confidence": <float>
}}"""

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{llm_url}",
                json={
                    "model": llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,  # Low temperature for consistency
                },
            )
            response.raise_for_status()

            result = response.json()
            content = result["choices"][0]["message"]["content"]

            # Extract JSON from response
            try:
                metadata = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract from markdown code block
                if "```json" in content:
                    json_text = content.split("```json")[1].split("```")[0].strip()
                    metadata = json.loads(json_text)
                elif "```" in content:
                    json_text = content.split("```")[1].split("```")[0].strip()
                    metadata = json.loads(json_text)
                else:
                    log.error(f"Failed to parse LLM response for {rel_type}: {content[:200]}")
                    return _get_fallback_metadata()

            # Validate metadata
            if not _validate_metadata(metadata):
                log.warning(f"Invalid metadata from LLM for {rel_type}, using fallback")
                return _get_fallback_metadata()

            return metadata

    except asyncio.TimeoutError:
        log.error(f"rel_type inference timeout for {rel_type}")
        return _get_fallback_metadata()
    except Exception as e:
        log.error(f"rel_type inference failed for {rel_type}: {e}")
        return _get_fallback_metadata()


def _validate_metadata(metadata: Dict[str, Any]) -> bool:
    """Validate inferred metadata structure and constraints."""
    required = [
        "is_symmetric",
        "head_types",
        "tail_types",
        "is_hierarchy_rel",
        "category",
        "confidence",
    ]

    # Check required fields
    if not all(k in metadata for k in required):
        log.debug(f"Missing required fields: {required}")
        return False

    # Validate types
    if not isinstance(metadata["is_symmetric"], bool):
        return False
    if not isinstance(metadata["is_hierarchy_rel"], bool):
        return False
    if not isinstance(metadata["head_types"], list):
        return False
    if not isinstance(metadata["tail_types"], list):
        return False
    if not isinstance(metadata["confidence"], (int, float)):
        return False

    # Validate ranges
    if not (0.0 <= metadata["confidence"] <= 1.0):
        return False

    # Validate inverse_rel_type requirement
    if not metadata["is_symmetric"] and not metadata.get("inverse_rel_type"):
        log.debug("Asymmetric rel_type missing inverse_rel_type")
        return False

    # Validate category
    valid_categories = {
        "family",
        "identity",
        "location",
        "work",
        "physical",
        "temporal",
        "pets",
        "other",
    }
    if metadata["category"].lower() not in valid_categories:
        log.debug(f"Invalid category: {metadata['category']}")
        return False

    return True


def _get_fallback_metadata() -> Dict[str, Any]:
    """Safe fallback metadata for novel rel_types."""
    return {
        "is_symmetric": False,
        "inverse_rel_type": None,
        "head_types": ["Any"],
        "tail_types": ["Any"],
        "is_hierarchy_rel": False,
        "category": "Other",
        "confidence": 0.4,  # Low confidence for fallback
    }
