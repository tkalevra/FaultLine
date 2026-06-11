"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "recall_memory",
        "description": "Recall what you know about the user or a topic from memory. "
                       "Call this at the start of any turn where the user's message touches on "
                       "something you might already know about them. The results are things you "
                       "remember — speak about them naturally as your own knowledge, never as "
                       "retrieved data.\n\n"
                       "To build a concept map for a topic, prefix with /expand:\n"
                       "  /expand networking\n"
                       "  /expand networking online\n"
                       "  /expand networking online https://example.com/networking-guide\n\n"
                       "Note: /expand maps how concepts relate — it does not make you an expert "
                       "on the topic. Use it to help classify facts the user shares about that domain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you want to recall (e.g., 'what do you know about X', 'tell me about Y')"
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
                       "Call this when the user states something worth remembering about themselves, "
                       "their world, or their relationships — OR when correcting/updating a prior fact. "
                       "Correction signals: 'actually X is Y', 'X is now Y not Z', 'I meant X', "
                       "'Correction: ...', any update to a previously stated value. "
                       "Internally runs intent classification → extract → validate → ingest. "
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
        "name": "learn_facts",
        "description": (
            "Ingest structured ontological statements into the knowledge graph as source=llm_learn. "
            "Use this to store concept hierarchies you generate — statements like "
            "'X is a subclass of Y', 'X is an instance of Y', 'X is a part of Y'. "
            "This maps how concepts relate to each other (not general knowledge). "
            "Facts stored as Class B (staged, source=llm_learn), confirmed over time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Your generated ontological statements — one per line, "
                                   "using 'X is a subclass of Y', 'X is an instance of Y', "
                                   "or 'X is a part of Y' forms only"
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
        "description": "Remove a previously stored fact from memory. Use ONLY when the user wants "
                       "something forgotten or deleted — signals like 'forget that', 'remove', "
                       "'erase', 'I don't have', 'that's not true', 'I'm not', 'X is not a Y'. "
                       "Do NOT use for corrections or updates — use remember_facts instead. "
                       "Accepts natural language — delegates extraction to FaultLine backend.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The statement to retract (e.g., 'forget that X is Y', 'X is not Z')"
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



def validate_query(query: str) -> str | None:
    """Return error message if query is invalid, None if valid."""
    if not isinstance(query, str):
        return "query must be a string"
    if len(query.strip()) == 0:
        return "query must not be empty"
    return None
