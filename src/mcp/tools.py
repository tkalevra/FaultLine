"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "recall_memory",
        "description": "Query FaultLine knowledge graph to recall facts relevant to the conversation. "
                       "Call this at the start of any turn where you need to remember things about the user. "
                       "Returns prose facts from PostgreSQL (graph traversal + hierarchy) merged with "
                       "Qdrant semantic search results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you want to recall (e.g., 'family', 'tell me about my pets', 'where does the user live')"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — omit if FAULTLINE_USER_ID env var is set"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "remember_facts",
        "description": "Store facts from the current conversation into the FaultLine knowledge graph. "
                       "Call this when the user states something worth remembering: their name, family, "
                       "preferences, relationships, or corrections to prior facts. "
                       "Internally runs extract/rewrite → WGM validation → ingest. "
                       "Returns the number of facts stored and their classification (Class A/B/C).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The sentence or passage containing the fact(s) to remember"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — omit if FAULTLINE_USER_ID env var is set"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "retract_fact",
        "description": "Remove or correct a previously stored fact. Use when the user says something "
                       "was wrong, has changed, or should be forgotten. "
                       "Accepts natural language (e.g., 'forget that Aurora is a computer', "
                       "'Des is 13 now not 12'). Delegates semantic extraction to FaultLine backend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Natural language retraction statement"
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — omit if FAULTLINE_USER_ID env var is set"
                }
            },
            "required": ["text"]
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


def validate_query(query: str) -> str | None:
    """Return error message if query is invalid, None if valid."""
    if not isinstance(query, str):
        return "query must be a string"
    if len(query.strip()) == 0:
        return "query must not be empty"
    return None
