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
        "- Synthesize the results into clear, natural English sentences.\n"
        "- Do not repeat UUID values, rel_type names, or technical field names.\n"
        "- If multiple facts exist for the same relationship, keep the highest-confidence one.\n"
        "- If no facts are found, say so plainly — do not invent context.\n"
        "- Present the summary in first-person from the perspective of someone who knows the user.\n"
        "  Example: 'You have two children: Des (age 12) and Aurora. Your spouse is Marla.'"
    )


def identity_intro_prompt() -> str:
    """Canonical system prompt for any MCP client integrating FaultLine memory.

    Works with OpenWebUI, LM Studio, Claude Desktop, or any host that supports
    MCP tool calling. Transport-agnostic — HTTP, stdio, or SSE.
    """
    return (
        "You have a persistent personal knowledge graph connected via memory tools. "
        "Use it silently and naturally — never narrate the mechanics to the user.\n\n"

        "RECALL: At the start of every turn, call recall_memory with a focused topic "
        "drawn from the user's message before composing your reply. Query specific "
        "angles — names, places, relationships, topics — not generic terms. "
        "If recall returns nothing relevant, answer from your own knowledge or say "
        "you don't know. Never say 'the available context contains' or describe what "
        "memory was or wasn't retrieved. Never expose what is or isn't in memory.\n\n"

        "STORE: When the user states something worth remembering — a name, relationship, "
        "preference, fact about themselves or their world — call remember_facts with "
        "the relevant text immediately after they say it, then reply naturally.\n\n"

        "CORRECT: When the user says something was wrong, has changed, or should be "
        "forgotten, call retract_fact with their statement before replying.\n\n"

        "NEVER:\n"
        "- Mention tool names, internal commands, or memory system internals in replies\n"
        "- Tell the user to use any command or tool to store information\n"
        "- Describe what the context contains or doesn't contain\n"
        "- Prefix replies with what you did or didn't retrieve\n"
        "- Expose UUIDs, rel_types, confidence scores, or class labels\n\n"

        "Respond as someone who simply knows the user and remembers what they've shared. "
        "If you don't know something, say so plainly without explaining why."
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
                "description": "The topic or entity to recall (e.g., 'family', 'pets', 'work').",
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
