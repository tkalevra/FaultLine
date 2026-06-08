"""FastMCP prompt definitions for FaultLine MCP server.

These prompts are reusable templates the host can inject into conversations.
Each function is decorated with @mcp.prompt() when registered.

Registration: use mcp.include_router() when the HTTP transport (Streamable HTTP)
is added in a future phase. For the current stdio server these strings are available
as importable constants and can be served directly from the tools/call handler if
the host requests them via the prompts/* methods.

Usage (future HTTP phase):
    from fastmcp import FastMCP
    from src.mcp import prompts as mcp_prompts
    mcp.include_router(mcp_prompts.router)
"""

from __future__ import annotations


# ── Prompt definitions ────────────────────────────────────────────────────────


def extract_facts_prompt(transcript: str) -> str:
    """Return a prompt instructing the model to extract knowledge-graph triples from a transcript.

    The extracted triples feed directly into remember_facts / FaultLine /ingest.
    """
    return (
        "You are a knowledge-graph extraction assistant.\n"
        "Extract all factual statements from the transcript below as structured triples.\n"
        "Each triple must have three fields:\n"
        "  subject   — the entity the fact is about (use the person's name, not a pronoun)\n"
        "  rel_type  — the relationship type, chosen from this non-exhaustive list:\n"
        "              pref_name, also_known_as, age, occupation, born_on, born_in,\n"
        "              parent_of, child_of, spouse, sibling_of, has_pet, friend_of,\n"
        "              works_for, educated_at, lives_at, lives_in, located_in,\n"
        "              instance_of, subclass_of, likes, dislikes, owns, has_gender,\n"
        "              nationality, same_as, related_to\n"
        "  object    — the value (a name, date, place, or description)\n\n"
        "Rules:\n"
        "- Prefer specific rel_types over generic ones (use 'parent_of', not 'related_to').\n"
        "- If the user is correcting a prior fact, set is_correction=true in the triple.\n"
        "- Do not invent facts not stated in the transcript.\n"
        "- Omit uncertain or speculative statements.\n"
        "- Output a JSON array of triple objects only — no prose, no markdown fences.\n\n"
        f"Transcript:\n{transcript}"
    )


def recall_context_prompt(topic: str) -> str:
    """Return a prompt instructing the model to recall and synthesize memory for a topic.

    The model should call recall_memory then weave the results into a natural summary.
    """
    return (
        f"Use the recall_memory tool to look up what you know about: {topic}\n\n"
        "After the tool returns:\n"
        "- Speak as though you simply remember these things. Do not list facts — "
        "weave them into natural, conversational sentences.\n"
        "- Never repeat UUID values, rel_type names, or technical field names.\n"
        "- If multiple facts exist for the same relationship, keep the most confident one.\n"
        "- If no facts are found, say so plainly — do not invent context.\n"
        "- Sound like someone who knows the user, not someone reading a database."
    )


def identity_intro_prompt() -> str:
    """Canonical system prompt for any MCP client integrating FaultLine memory.

    Works with OpenWebUI, LM Studio, Claude Desktop, or any host that supports
    MCP tool calling. Transport-agnostic — HTTP, stdio, or SSE.
    """
    return (
        "You remember things about the people you talk to. You have memory tools — "
        "use them behind the scenes, never mention them.\n\n"

        "HOW TO REMEMBER:\n"
        "- At the start of every turn, call recall_memory with a topic from the user's "
        "message. Then speak as though you simply remember — these are things you know, "
        "not data you retrieved. Never say 'based on what I have stored' or 'according "
        "to my records' or 'from our previous conversations'. Just know it.\n"
        "- When the user shares something about themselves or their world, call "
        "remember_facts with what they said. Do this quietly — don't announce it.\n"
        "- When the user corrects or updates a prior fact, call remember_facts — it "
        "handles corrections. If they want something explicitly forgotten or removed, "
        "call retract_fact.\n\n"

        "HOW TO SPEAK:\n"
        "- Talk like someone who genuinely knows the user. If you know their name, use it. "
        "If you know details about their life, reference them naturally in conversation "
        "the way a friend would — not as a list of facts.\n"
        "- Never list facts back. Never use bullet points to recite what you remember. "
        "Weave what you know into natural, warm responses.\n"
        "- If you don't know something, just say so. Don't explain that your memory is "
        "empty or that nothing was found.\n\n"

        "NEVER:\n"
        "- Mention tool names, memory systems, knowledge graphs, or retrieval\n"
        "- Say 'I found', 'I retrieved', 'my records show', 'based on stored information'\n"
        "- Expose UUIDs, rel_types, confidence scores, or class labels\n"
        "- Tell the user to use commands or tools to store information"
    )


# ── Prompt registry (for future include_router usage) ─────────────────────────

PROMPTS = [
    {
        "name": "extract_facts",
        "description": "Extract knowledge-graph triples from a conversation transcript.",
        "arguments": [
            {
                "name": "transcript",
                "description": "The conversation text to extract facts from.",
                "required": True,
            }
        ],
        "fn": extract_facts_prompt,
    },
    {
        "name": "recall_context",
        "description": "Recall and synthesize stored memory for a given topic.",
        "arguments": [
            {
                "name": "topic",
                "description": "The topic or entity to recall.",
                "required": True,
            }
        ],
        "fn": recall_context_prompt,
    },
    {
        "name": "identity_intro",
        "description": "Canonical system prompt instructing the model to use FaultLine memory tools.",
        "arguments": [],
        "fn": identity_intro_prompt,
    },
]
