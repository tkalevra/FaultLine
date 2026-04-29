import json
import os
import httpx

class EntityRegistry:
    def __init__(self, registry_data=None):
        self.registry = {} if registry_data is None else registry_data 

    def resolve(self, entity_name: str, entity_type: str) -> dict:
        target_name = entity_name.lower().strip()
        target_type = entity_type.lower().strip()

        # Strict Matching: Only merge if text is IDENTICAL.
        # This prevents "Theo" merging into "Theodore".
        for cid, data in self.registry.items():
            if data["name"].lower().strip() == target_name and data["type"].lower().strip() == target_type:
                return {"canonical_id": cid, "is_duplicate": True}

        new_id = f"{target_type}-{len(self.registry)}"
        self.registry[new_id] = {"name": entity_name.strip(), "type": entity_type.strip()}
        return {"canonical_id": new_id, "is_duplicate": False}

def resolve_entities(query_input, model=None, context=None):
    if context is None: context = {"known_types": [], "registry": {}}
    registry = EntityRegistry(registry_data=context.get("registry"))
    known_types = set(context.get("known_types", []))
    results = []
    
    for entity in query_input.get("entities", []):
        name, etype = entity.get("entity", ""), entity.get("type", "")
        if etype not in known_types:
            raise ValueError(f"Novel Type Error: {etype}")
        res = registry.resolve(name, etype)
        results.append({"entity": name, "type": etype, "canonical_id": res["canonical_id"], "confidence": 1.0})

    return {
        "resolution": {
            "resolved": results, 
            "duplicates_found": sum(1 for r in results if r.get("is_duplicate")),
            "canonical_registry": registry.registry
        }
    }

# Human-readable labels for relationship types
LABEL_MAP = {
    "is_a": "is a type of",
    "part_of": "is a part of",
    "created_by": "was created by",
    "works_for": "works for",
    "parent_of": "is the parent of",
    "child_of": "is the child of",
    "spouse": "is married to",
    "sibling_of": "is a sibling of",
    "also_known_as": "is also known as",
    "related_to": "is related to",
    "likes": "likes",
    "dislikes": "dislikes",
    "prefers": "prefers",
    "owns": "owns",
    "located_in": "is located in",
    "educated_at": "was educated at",
    "nationality": "has nationality",
    "occupation": "has occupation",
    "born_on": "was born on",
    "age": "has age",
    "knows": "knows",
    "friend_of": "is friends with",
    "met": "has met",
    "lives_in": "lives in",
    "born_in": "was born in",
    "has_gender": "has gender",
}

# GLiNER2 label constraints for relationship extraction
GLIREL_LABELS = {
    "is_a": {"allowed_head": ["PERSON", "ORG", "MISC", "LOC", "ANIMAL"], "allowed_tail": ["MISC", "ORG", "ANIMAL"]},
    "part_of": {"allowed_head": ["PERSON", "ORG", "LOC", "MISC"], "allowed_tail": ["ORG", "LOC", "MISC"]},
    "created_by": {"allowed_head": ["PERSON", "ORG", "LOC", "MISC"], "allowed_tail": ["PERSON", "ORG"]},
    "works_for": {"allowed_head": ["PERSON"], "allowed_tail": ["ORG"]},
    "parent_of": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "child_of": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "spouse": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "sibling_of": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "also_known_as": {"allowed_head": ["PERSON", "ORG", "LOC"], "allowed_tail": ["PERSON", "ORG", "LOC"]},
    "related_to": {"allowed_head": ["PERSON", "ORG", "LOC", "MISC"], "allowed_tail": ["PERSON", "ORG", "LOC", "MISC"]},
    "likes": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON", "ORG", "LOC", "MISC"]},
    "dislikes": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON", "ORG", "LOC", "MISC"]},
    "prefers": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON", "ORG", "LOC", "MISC"]},
    "owns": {"allowed_head": ["PERSON", "ORG"], "allowed_tail": ["MISC", "ORG", "LOC"]},
    "located_in": {"allowed_head": ["ORG", "LOC", "GPE"], "allowed_tail": ["LOC", "GPE"]},
    "educated_at": {"allowed_head": ["PERSON"], "allowed_tail": ["ORG"]},
    "nationality": {"allowed_head": ["PERSON"], "allowed_tail": ["GPE"]},
    "occupation": {"allowed_head": ["PERSON"], "allowed_tail": ["MISC"]},
    "born_on": {"allowed_head": ["PERSON"], "allowed_tail": ["DATE"]},
    "age": {"allowed_head": ["PERSON"], "allowed_tail": ["MISC"]},
    "knows": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "friend_of": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "met": {"allowed_head": ["PERSON"], "allowed_tail": ["PERSON"]},
    "lives_in": {"allowed_head": ["PERSON"], "allowed_tail": ["LOC", "GPE"]},
    "born_in": {"allowed_head": ["PERSON"], "allowed_tail": ["LOC", "GPE"]},
    "has_gender": {"allowed_head": ["PERSON"], "allowed_tail": ["MISC"]},
}