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
        # This prevents "Cy" merging into "Cyrus".
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

# (Other classification functions like classify() and invoke_oracle() can be added as needed)