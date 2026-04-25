"""
Entity Registry - Manages canonical entity IDs and variant mappings.
"""
import json
import os
import httpx


class EntityRegistry:
    """Registry that manages canonical entity IDs and variant mappings."""

    def __init__(self, registry_data=None):
        self.registry = {} if registry_data is None else registry_data  # canonical_id -> {name: str, type: str}
        self.variants = {}  # (type, name_pattern) -> set of canonical_ids

    def resolve(self, entity_name: str, entity_type: str) -> dict:
        """
        Resolve an entity to a canonical form.

        Args:
            entity_name: Name of the entity being classified
            entity_type: Type classification from Qwen model

        Returns:
            dict with {
                "canonical_id": str (new or existing),
                "is_duplicate": bool,
                "duplicates": list of canonical_ids found
            }
        """
        key = f"{entity_type}:{entity_name.lower()}"

        # Check for existing matches using substring matching
        existing_ids = self._find_matches(key, entity_type)

        if existing_ids:
            return {
                "canonical_id": existing_ids[0],
                "is_duplicate": True,
                "duplicates": [existing_ids[0]]
            }

        # Create new canonical ID
        canonical_id = f"{entity_type.lower()}-{len(self.registry)}"

        # Register the entity
        self.registry[canonical_id] = {
            "name": entity_name,
            "type": entity_type
        }

        return {
            "canonical_id": canonical_id,
            "is_duplicate": False,
            "duplicates": []
        }

    def _find_matches(self, key: str, entity_type: str) -> list:
        """Find matching canonical IDs using case-insensitive substring matching."""
        matched_ids = []
        name_part = key.split(":")[1] if ":" in key else ""
        
        for canonical_id, entity_data in self.registry.items():
            entity_name = entity_data["name"]
            if entity_type.lower() == entity_data["type"].lower():
                # Case-insensitive substring match
                if name_part in entity_name.lower() or entity_name.lower() in name_part:
                    matched_ids.append(canonical_id)
        
        return matched_ids


"""
Schema Oracle Service - Classification-Only Mode
Classifies incoming entities against known schema patterns
and raises appropriate errors for novel/conflicting types.
"""


class ClassificationService:
    """Service that classifies entities using Qwen2.5 Coder model."""

    def __init__(self, model=None):
        self.model = model or None

    def query_classification(self, query_input: dict, context: dict) -> dict:
        """
        Query the classification prompt with no generation mode.
        
        Args:
            query_input: Dictionary with entities list and entity info
            context: Dictionary with known_types and other context
            
        Returns:
            Dictionary with entity_type and confidence score
        """
        if self.model is None:
            raise ValueError("Model not initialized")

        # Build classification prompt (no generation)
        entities = query_input.get("entities", [])
        entity_descriptions = []
        
        for entity in entities:
            entity_name = entity.get("entity", "")
            entity_type = entity.get("type", "")
            entity_descriptions.append(f"Entity: {entity_name}, Type: {entity_type}")

        known_types = context.get("known_types", [])
        
        prompt_lines = [
            "Classify the following entities using ONLY classification mode (no text generation):",
            "",
            f"Known types in schema: {', '.join(known_types)}",
            "",
            "Entities to classify:"
        ]
        
        if entity_descriptions:
            prompt_lines.extend(entity_descriptions)
        
        prompt = "\n".join(prompt_lines) + """

Respond with JSON containing ONLY {"classification": {"entity_type": str, "confidence": float 0.0-1.0}}"""

        # Call model for classification only
        response = self.model.query_classification(query_input, context)
        
        return {
            "entity_type": response.get("entity_type", ""),
            "confidence": float(response.get("confidence", 0.0))
        }


def resolve_entities(query_input: dict, model=None, context: dict = None) -> dict:
    """
    Classify entities and resolve duplicates.

    Args:
        query_input: Dictionary with "entities" list
        model: Qwen2.5 Coder model instance
        context: Dictionary with known_types and optional registry

    Returns:
        dict with {
            "classification": {...},
            "resolution": {
                "resolved": [...],
                "duplicates_found": int,
                "canonical_registry": {...}
            }
        }
    """
    if context is None:
        context = {"known_types": [], "registry": {}}

    # Use existing registry from context or create new one
    if context.get("registry"):
        registry = EntityRegistry(registry_data=context["registry"])
    else:
        registry = EntityRegistry()
    
    known_types = set(context.get("known_types", []))
    results = []
    duplicates_found = 0

    for entity in query_input.get("entities", []):
        entity_name = entity.get("entity", "")
        entity_type = entity.get("type", "")

        # Validate type is known
        if entity_type not in known_types:
            raise ValueError(
                f"Novel/conflicting entity type detected: {entity_type}. "
                f"Known types: {known_types}"
            )

        # Resolve to canonical form using registry directly
        resolution = registry.resolve(entity_name, entity_type)

        results.append({
            "entity": entity_name,
            "type": entity_type,
            "canonical_id": resolution["canonical_id"],
            "confidence": 1.0  # Use passed type confidence since we're not actually classifying here
        })

        if resolution["is_duplicate"]:
            duplicates_found += 1

    return {
        "classification": {},  # Empty per spec - classification-only mode
        "resolution": {
            "resolved": results,
            "duplicates_found": duplicates_found,
            "canonical_registry": registry.registry
        }
    }


def classify(
    query_input: dict,
    model=None,
    context: dict = None,
    enable_resolution: bool = False
) -> dict:
    """
    Classify entities against known schema types.
    
    Args:
        query_input: Dictionary with "entities" list where each entity is
                     {"entity": str, "type": str}
        model: Qwen2.5 Coder model instance for classification queries
        context: Dictionary containing {"known_types": [list of allowed types]}
        enable_resolution: If True, also perform duplicate detection and canonicalization

    Returns:
        If enable_resolution=False (default):
            {"classification": {"entity_type": str, "confidence": float}}

        If enable_resolution=True:
            {
                "classification": {},  # Empty per spec - classification-only mode
                "resolution": {
                    "resolved": [...],
                    "duplicates_found": int,
                    "canonical_registry": {...}
                }
            }

    Raises:
        ValueError: If novel/conflicting entity type detected not in schema
    """
    service = ClassificationService(model=model)
    
    if context is None:
        context = {"known_types": []}
    
    known_types = set(context.get("known_types", []))
    
    # Validate each entity has a type that's not novel/conflicting
    for entity in query_input.get("entities", []):
        entity_name = entity.get("entity", "")
        entity_type = entity.get("type", "")
        
        # Check if type is novel or conflicting
        if entity_type not in known_types:
            raise ValueError(
                f"Novel/conflicting entity type detected: {entity_type}. "
                f"Known types: {known_types}"
            )
    
    # Call classification prompt (no generation)
    response = service.model.query_classification(query_input, context)
    
    if enable_resolution:
        return resolve_entities(query_input, model, context)
    
    return {
        "classification": {
            "entity_type": response.get("entity_type", ""),
            "confidence": float(response.get("confidence", 0.0))
        }
    }


def invoke_oracle(prompt: str) -> dict:
    """
    Calls Qwen2.5 Coder via httpx with fixed params.
    Params: temperature=0.0, max_tokens=50, model=qwen2.5-coder.
    Raises RuntimeError on non-200 response.
    """
    endpoint = os.getenv("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
    payload = {
        "model": "qwen2.5-coder",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 50,
    }
    response = httpx.post(endpoint, json=payload, timeout=30.0)
    if response.status_code != 200:
        raise RuntimeError(f"Oracle request failed: HTTP {response.status_code}")
    return parse_oracle_response(response.json())


def parse_oracle_response(content) -> dict:
    """Parse Qwen2.5 chat completion response to a classification dict."""
    if isinstance(content, dict):
        choices = content.get("choices", [])
        if choices:
            message_content = choices[0].get("message", {}).get("content", "")
            return json.loads(message_content)
    raise ValueError(f"Cannot parse oracle response: {content!r}")
