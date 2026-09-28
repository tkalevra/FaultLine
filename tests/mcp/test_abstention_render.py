"""Pins for the HONEST ABSTENTION RENDER (recall of a genuinely-empty memory).

GAP: LongMemEval `_abs` questions (Wu et al. 2024, arXiv:2410.10813) ask about events the
user NEVER mentioned — the correct answer is an honest "I don't have that / you haven't
mentioned X", not a bare null and not loosely-related junk. The recall render seam
(`src/mcp/server.py::recall_memory_tool`) used to emit a bare "No relevant facts found."
on a genuinely-empty walk; a downstream reader/judge does not credit that as an intentional
abstention. This renders a clean, subject-referencing refusal instead (the known-unknown
answer; SQuAD 2.0 Rajpurkar et al. 2018 unanswerable shape; "Know Your Limits" abstention
survey, Wen et al. 2024, arXiv:2407.18418).

THE CRITICAL SAFETY pinned here: PRECISION — an ANSWERABLE recall (facts survive the
confidence gate) still renders its facts, NEVER the abstention. The abstention is reached
only when the existing confidence/relevance gate already dropped/held everything, so no new
threshold and no hit-turned-miss. Flag-gated (`ABSTENTION_RENDER`, default ON); OFF ⇒
byte-identical legacy string.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp.server import (
    _query_subject_phrase,
    _render_abstention,
    recall_memory_tool,
)


# ── Subject reduction (grammatical, deterministic) — real _abs exemplars ─────────
def test_subject_phrase_do_support_object_np():
    # "when did I book the Airbnb in Sacramento?" (982b5123_abs) → object NP.
    assert _query_subject_phrase(
        "When did I book the Airbnb in Sacramento?") == "the airbnb in sacramento"


def test_subject_phrase_how_many_do_support():
    # "How many times did I bake egg tarts in the past two weeks?" (88432d0a_abs)
    assert _query_subject_phrase(
        "How many times did I bake egg tarts in the past two weeks?"
    ) == "egg tarts in the past two weeks"


def test_subject_phrase_copula_object():
    # "what is my daily commute" — copula 'is', possessive 'my' consumed → complement.
    assert _query_subject_phrase("what is my daily commute") == "daily commute"


def test_subject_phrase_wh_lexical_verb_declines():
    # "Who became a parent first, Tom or Alex?" (gpt4_fe651585_abs) — NO auxiliary; a
    # verb-led span must NOT be echoed → generic abstention (empty subject).
    assert _query_subject_phrase("Who became a parent first, Tom or Alex?") == ""


def test_subject_phrase_nested_clause_declines():
    # "How long have I been working before I started my current job at Google?"
    # (gpt4_93159ced_abs) — residual carries a nested first-person clause → decline.
    assert _query_subject_phrase(
        "How long have I been working before I started my current job at Google?") == ""


def test_subject_phrase_bare_keyword_declines():
    # A reformulated one-word recall term has no scaffold/aux → generic abstention.
    assert _query_subject_phrase("pets") == ""


# ── Render: flag ON (default) is an abstention; OFF is byte-identical legacy ──────
def test_render_abstention_default_on_references_subject():
    out = _render_abstention("When did I book the Airbnb in Sacramento?")
    assert "don't have any information about the airbnb in sacramento" in out
    assert "haven't mentioned" in out
    # It is unambiguously a refusal, not a null.
    assert out != "No relevant facts found."


def test_render_abstention_generic_when_no_subject():
    out = _render_abstention("pets")
    assert "don't have any information about that" in out
    assert "haven't mentioned" in out


def test_render_abstention_flag_off_byte_identical():
    with patch("src.mcp.server.ABSTENTION_RENDER", False):
        assert _render_abstention(
            "When did I book the Airbnb in Sacramento?") == "No relevant facts found."
        assert _render_abstention("pets") == "No relevant facts found."


def test_render_abstention_fail_safe_on_empty_query():
    # Empty/garbage query never raises; falls to the generic abstention under the flag.
    out = _render_abstention("")
    assert "don't have any information about that" in out


# ── Integration through recall_memory_tool ───────────────────────────────────────
@pytest.fixture
def mock_http_client():
    return AsyncMock()


def _classify_query():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"intent": "QUERY", "confidence": 0.9}
    return r


def _gate():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"threshold": 0.70}
    return r


def _harvest():
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = {"edges": []}
    return r


def _query_resp(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = payload
    return r


@pytest.mark.asyncio
async def test_recall_empty_renders_abstention(mock_http_client):
    """Genuinely-empty walk → honest, subject-referencing abstention (the fix)."""
    mock_http_client.post = AsyncMock(side_effect=[
        _classify_query(), _harvest(),
        _query_resp({"facts": [], "attributes": {}}),
    ])
    mock_http_client.get = AsyncMock(return_value=_gate())
    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool(
            "When did I book the Airbnb in Sacramento?", "user-alice")
    assert "don't have any information about the airbnb in sacramento" in result["memory"]
    assert result["memory"] != "No relevant facts found."


@pytest.mark.asyncio
async def test_recall_answerable_returns_facts_not_abstention(mock_http_client):
    """CRITICAL PRECISION PIN: a recall WITH facts renders the facts, never the
    abstention — the fix must never turn a hit into a miss."""
    mock_http_client.post = AsyncMock(side_effect=[
        _classify_query(), _harvest(),
        _query_resp({
            "facts": [{
                "rel_type": "has_pet",
                "definition": "You have a dog named Fraggle.",
                "fact_class": "A", "fact_provenance": "user_stated", "confidence": 1.0,
            }],
            "attributes": {},
        }),
    ])
    mock_http_client.get = AsyncMock(return_value=_gate())
    with patch("src.mcp.server._http_client", mock_http_client):
        result = await recall_memory_tool("Tell me about my pets", "user-alice")
    assert "Fraggle" in result["memory"]
    assert "don't have any information" not in result["memory"]
    assert "haven't mentioned" not in result["memory"]


@pytest.mark.asyncio
async def test_recall_empty_flag_off_byte_identical(mock_http_client):
    """Flag OFF ⇒ the empty recall is byte-identical to the legacy string."""
    mock_http_client.post = AsyncMock(side_effect=[
        _classify_query(), _harvest(),
        _query_resp({"facts": [], "attributes": {}}),
    ])
    mock_http_client.get = AsyncMock(return_value=_gate())
    with patch("src.mcp.server._http_client", mock_http_client), \
         patch("src.mcp.server.ABSTENTION_RENDER", False):
        result = await recall_memory_tool(
            "When did I book the Airbnb in Sacramento?", "user-alice")
    assert result == {"memory": "No relevant facts found."}
