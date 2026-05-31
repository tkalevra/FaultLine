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
    """Return the canonical Claude Desktop system prompt for FaultLine memory integration.

    This is the same text recommended in CLAUDE-DESKTOP-SETUP.md §4. Expose it here
    so the host can inject it programmatically via the MCP prompts API when HTTP
    transport is available.
    """
    return (
        "You have access to a personal knowledge graph via FaultLine MCP tools.\n\n"
        "At the start of each turn, call recall_memory with the topic of the user's message "
        "before composing your answer.\n"
        "When the user states a fact worth remembering (name, relationship, preference, "
        "correction), call remember_facts with the relevant text.\n"
        "When the user says something was wrong or should be forgotten, call retract_fact "
        "with their statement.\n"
        "Do not mention the tools by name in your replies — use the recalled facts naturally "
        "in your response.\n"
        "Prefer specificity: query recall_memory with \"family\", \"pets\", \"where I live\", "
        "etc. rather than generic terms."
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
