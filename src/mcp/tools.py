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
                    "description": "The user's current message copied VERBATIM and in full — do NOT summarize, shorten, or reduce it to a keyword or topic. Keep every word, especially 'not', 'no', 'now', 'actually', 'instead' and any names/values. The backend extracts the search topic AND decides intent (recall vs correction) from the whole sentence itself. Required — never leave empty."
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
        "description": "Save something the user just told you. Call this whenever the user states a "
                       "fact about themselves, another person, or their world — a name, relationship, "
                       "preference, job, possession, or location — even mentioned in passing, and also "
                       "when they correct a prior fact. Default to calling it; skip only pure questions "
                       "or chitchat. Do not ask permission first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The user's own sentence(s) containing the fact(s), copied verbatim. Required — never leave empty."
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
    },
    {
        "name": "forget_fact",
        "description": "Use ONLY when the user EXPLICITLY and deliberately asks to forget or delete "
                       "ONE specific fact about a NAMED target — e.g. 'forget my email address', "
                       "'delete that I have a dog named Rex', 'forget that Jordan is my spouse'. "
                       "Tombstones exactly the one named fact (recoverable, not a hard wipe). You MUST "
                       "name the target via 'subject' (use 'me' for the user) and, to pin it, "
                       "'rel_type' and/or 'old_value'. NEVER call for a broad/bulk request ('forget "
                       "everything', 'wipe my memory') — there is no bulk forget. For a CORRECTION "
                       "(a NEW value) use remember_facts instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "WHOSE fact to forget — 'me' for the user, or the named person/thing. "
                                   "Required: a forget MUST name exactly one target, never a bulk wipe."
                },
                "rel_type": {
                    "type": "string",
                    "description": "Optional: the relationship of the specific fact (e.g. occupation, "
                                   "has_pet, has_email) to narrow the forget to one fact."
                },
                "old_value": {
                    "type": "string",
                    "description": "Optional: the specific value/object of the fact (e.g. the email "
                                   "address, the pet's name) to pin the forget to exactly one fact."
                },
                "user_id": {
                    "type": "string",
                    "description": "User UUID — omit if FAULTLINE_USER_ID env var is set"
                }
            },
            "required": ["subject"]
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
