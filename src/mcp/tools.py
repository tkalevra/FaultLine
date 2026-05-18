"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "extract",
        "description": "Preflight entity extraction using GLiNER2. Returns typed entities from text "
                       "before full ingest. Useful for previewing what entities would be extracted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Input text to extract entities from (e.g., 'My wife Marla and I live in Toronto')"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID for per-user isolation"
                }
            },
            "required": ["text", "user_id"]
        }
    },
    {
        "name": "ingest",
        "description": "Ingest facts into the FaultLine knowledge graph. Accepts pre-extracted edges "
                       "(subject, object, rel_type with optional subject_type/object_type) and stores "
                       "them after WGM validation. Supports Class A/B/C fact classification.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Original text that produced these edges (for provenance)"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID for per-user isolation"
                },
                "edges": {
                    "type": "array",
                    "description": "Array of edge objects to ingest",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "object": {"type": "string"},
                            "rel_type": {"type": "string"},
                            "subject_type": {"type": "string"},
                            "object_type": {"type": "string"}
                        },
                        "required": ["subject", "object", "rel_type"]
                    }
                },
                "source": {
                    "type": "string",
                    "description": "Provenance source label (default: 'mcp')",
                    "default": "mcp"
                }
            },
            "required": ["text", "user_id", "edges"]
        }
    },
    {
        "name": "query",
        "description": "Query the FaultLine knowledge graph for memory recall. Returns facts from "
                       "PostgreSQL (baseline + graph traversal + hierarchy expansion) merged with "
                       "Qdrant vector similarity results. Used to inject relevant memories into "
                       "conversation context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Query text for fact retrieval (e.g., 'tell me about my family')"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID for per-user isolation"
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max Qdrant vector search results (default: 5)",
                    "default": 5
                }
            },
            "required": ["text", "user_id"]
        }
    },
    {
        "name": "retract",
        "description": "Retract (soft-delete or hard-delete) facts from the knowledge graph. "
                       "Behavior controlled by the relationship type's correction_behavior: "
                       "supersede (mark as superseded), hard_delete (DELETE from facts + entity_aliases), "
                       "or immutable (no-op).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "User UUID for per-user isolation"
                },
                "subject": {
                    "type": "string",
                    "description": "Subject entity of the fact to retract"
                },
                "rel_type": {
                    "type": "string",
                    "description": "Relationship type to retract (optional — retracts all if omitted)"
                },
                "old_value": {
                    "type": "string",
                    "description": "Old value/object to retract (for value corrections)"
                },
                "behavior": {
                    "type": "string",
                    "enum": ["supersede", "hard_delete", "immutable"],
                    "description": "Override the default correction behavior for this rel_type"
                }
            },
            "required": ["user_id", "subject"]
        }
    },
    {
        "name": "store_context",
        "description": "Store raw text context directly to Qdrant vector store, bypassing WGM "
                       "validation and PostgreSQL. For unstructured text that doesn't fit the "
                       "fact model. Stored as Class C with 30-day expiry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Raw text to embed and store"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID for per-user isolation"
                }
            },
            "required": ["text", "user_id"]
        }
    }
]


def validate_text(text: str) -> str | None:
    """Return error message if text is invalid, None if valid."""
    if not isinstance(text, str):
        return "text must be a string"
    if len(text.strip()) == 0:
        return "text must not be empty"
    return None


def validate_user_id(user_id: str) -> str | None:
    """Return error message if user_id is invalid, None if valid."""
    if not isinstance(user_id, str):
        return "user_id must be a string"
    if len(user_id.strip()) == 0:
        return "user_id must not be empty"
    return None


def validate_edges(edges: list) -> str | None:
    """Return error message if edges array is invalid, None if valid."""
    if not isinstance(edges, list):
        return "edges must be an array"
    if len(edges) == 0:
        return "edges must not be empty"
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            return f"edges[{i}] must be an object"
        if "subject" not in edge or "object" not in edge or "rel_type" not in edge:
            return f"edges[{i}] missing required field (subject, object, rel_type)"
    return None
