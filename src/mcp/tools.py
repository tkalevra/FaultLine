"""MCP tool definitions and schemas for FaultLine endpoints."""

TOOLS = [
    {
        "name": "recall_memory",
        "description": "Call at the START of a turn to look up what you already know when the user "
                       "asks about or references something you may know about them, their people, or "
                       "their world. This only READS memory — it never saves; to SAVE a new fact the "
                       "user states, use remember_facts instead. The results are things you "
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
        "description": "ALWAYS call this whenever the user states ANY fact about themselves, another "
                       "person, their preferences, relationships, possessions, work, or location — "
                       "even said in passing and even if it seems minor. Default to calling it; only "
                       "skip pure questions, greetings, or chitchat with no fact. Examples that MUST "
                       "trigger it: 'My favorite language is Rust', 'I work at Guelph', 'My coworker "
                       "Zelda got promoted', 'My office is on the 3rd floor'. Also call it to correct "
                       "or update a prior fact (e.g. 'actually X is Y not Z'). Pass the user's "
                       "sentence as text. Do NOT ask permission first.",
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
        "description": "Use ONLY when the user explicitly wants something deleted or forgotten — "
                       "signals like 'forget that', 'delete', 'erase', 'remove that'. For corrections "
                       "or updated values (the user giving a NEW value for something), use "
                       "remember_facts instead, NOT this. "
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
