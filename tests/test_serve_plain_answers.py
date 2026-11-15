"""THE SERVE_PLAIN_ANSWERS PINS (owner product directive, 2026-11-14: "The
refusal or hedging should likely be removed — users don't need to know how
confident we are in the response; it should just respond with what it has").

THE WOUND (LongMemEval pilot autopsy + live seats): the recall serve
prepended/appended hedging and refusal prose — "Though I don't have a direct
answer to that question" trailing the negative-space rows, the "If their
message touched on other distinct topics…" footer on EVERY successful serve,
and refusal-flavored abstentions — and on questions where the GOLD WAS VERBATIM
IN THE SERVED PAYLOAD this framing flipped the judge to NO.

THE FIX under pin: flag ``SERVE_PLAIN_ANSWERS`` default ON (hermetic legs
below), OFF = byte-parity against the LEGACY RENDERS captured here as literal
fixtures. The degraded-failure contract (a retrieval FAILURE still says
try-again — the R4-E incident contract) is pinned INTACT and untouched.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


# ── LEGACY RENDER FIXTURES (byte-parity targets — flag OFF) ────────────────────────
# Captured verbatim from the pre-flag code paths. SERVE_PLAIN_ANSWERS=OFF must
# reproduce these EXACTLY on every seam the flag gates.

LEGACY_ABSTENTION_SUBJECT = (
    "I don't have any information about the airbnb in sacramento in your "
    "memory — you haven't mentioned it in our previous conversations."
)
LEGACY_ABSTENTION_GENERIC = (
    "I don't have any information about that in your memory — you haven't "
    "mentioned it in our previous conversations."
)
LEGACY_OPERAND_ABSTENTION = (
    "I don't have any information about Ottawa in your memory — you haven't "
    "mentioned it in our previous conversations, so I can't answer that."
)
LEGACY_NEG_SPACE = (
    "Here's what I remember about cats:\n"
    "- user has_pet Miso\n"
    "- user has_pet Juniper\n"
    "\n"
    "Though I don't have a direct answer to that question."
)
LEGACY_MORE_HINT = (
    "\n\nIf their message touched on other distinct topics, "
    "recall each before responding."
)
LEGACY_SUM_MISS = (
    "I don't have any ski trips amounts on record, so I can't total that up."
)
LEGACY_COUNT_MISS = (
    "I don't have any ski trips on record, so I can't give a count."
)

PLAIN_EMPTY = "I don't have anything on that yet."


# ── Flag default ───────────────────────────────────────────────────────────────────


def test_flag_defaults_on():
    import src.mcp.server as S
    assert S.SERVE_PLAIN_ANSWERS is True
    # Honest-empty keeps exactly ONE plain line.
    assert S._PLAIN_EMPTY_LINE == PLAIN_EMPTY


# ── Honest-empty faces: ONE plain line, no refusal prose (ON) ─────────────────────


def test_abstention_renders_the_one_plain_line():
    import src.mcp.server as S
    for q in ("When did I book the Airbnb in Sacramento?", "pets", ""):
        assert S._render_abstention(q) == PLAIN_EMPTY


def test_abstention_plain_line_is_not_the_null_sentinel():
    import src.mcp.server as S
    out = S._render_abstention("When did I book the Airbnb in Sacramento?")
    # Still unambiguously an abstention, never the bare null sentinel.
    assert out != "No relevant facts found."


def test_operand_abstention_renders_the_one_plain_line():
    import src.mcp.server as S
    assert S._render_operand_abstention(["Ottawa"]) == PLAIN_EMPTY
    assert S._render_operand_abstention(["Ottawa", "Toronto"]) == PLAIN_EMPTY


# ── Facts-serve: rows lead, no hedge strings anywhere in the payload (ON) ─────────


def test_negative_space_rows_lead_with_no_disclosure():
    import src.mcp.server as S
    out = S._negative_space_assemble(
        "cats", ["user has_pet Miso", "user has_pet Juniper"])
    assert out.startswith("Here's what I remember about cats:")
    assert "- user has_pet Miso" in out
    assert "- user has_pet Juniper" in out
    # THE WOUND: the hedge clause that flipped judges is GONE.
    assert "Though I don't have a direct answer" not in out
    assert "don't have a direct answer" not in out


def test_more_hint_footer_is_empty_on_serve():
    import src.mcp.server as S
    assert S._recall_more_hint() == ""


def test_facts_render_path_carries_no_hedge_strings():
    """The assembled fact-band render (response_types.render) carries no hedge /
    refusal / distinct-topics strings — facts render first, plain."""
    from src.mcp.response_types import render
    out = render(
        None,
        event_lines=["Event #1 [2026-03-04]: You ran the marathon"],
        assert_lines=["You are married to Mia"],
        hold_lines=["You may play chess"],
    )
    for hedge in ("Though I don't have a direct answer",
                  "distinct topics", "haven't mentioned",
                  "I can't", "so I can't"):
        assert hedge not in out
    assert "You are married to Mia" in out


# ── Degraded-failure contract INTACT (never touched by the flag) ───────────────────


def test_degraded_envelope_still_says_try_again():
    """R4-E incident contract: a retrieval FAILURE is a degraded envelope, not a
    hedged success — SERVE_PLAIN_ANSWERS does not touch it."""
    import src.mcp.server as S
    note = ("⚠ I could not complete the memory lookup within the time budget — "
            "this is a degraded response, not an empty memory; your facts are "
            "still there. Try again.")
    env = S._floor_degraded_envelope([], note, recall_budget_s=12.0)
    assert env["status"] == "degraded"
    assert env["isError"] is True
    assert env["memory"] == note
    assert "Try again." in env["memory"]


def test_degraded_failure_literals_unchanged_in_source():
    """The two live degraded notes stay byte-identical (the honest try-again).
    The literals are split across source lines — assert the load-bearing
    fragments, plus the full budget note (single token in source)."""
    import inspect
    import src.mcp.server as S
    src = inspect.getsource(S)
    assert "⚠ I could not reach memory just now" in src
    assert "this is a retrieval failure" in src
    assert "this is a degraded response, not an empty memory" in src
    assert "Try again." in src


# ── OFF = byte-parity against the legacy renders ───────────────────────────────────


def test_abstention_off_is_byte_parity_legacy(monkeypatch):
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_PLAIN_ANSWERS", False)
    assert S._render_abstention(
        "When did I book the Airbnb in Sacramento?") == LEGACY_ABSTENTION_SUBJECT
    assert S._render_abstention("pets") == LEGACY_ABSTENTION_GENERIC


def test_operand_abstention_off_is_byte_parity_legacy(monkeypatch):
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_PLAIN_ANSWERS", False)
    assert S._render_operand_abstention(["Ottawa"]) == LEGACY_OPERAND_ABSTENTION


def test_negative_space_off_is_byte_parity_legacy(monkeypatch):
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_PLAIN_ANSWERS", False)
    assert S._negative_space_assemble(
        "cats", ["user has_pet Miso", "user has_pet Juniper"]) == LEGACY_NEG_SPACE


def test_more_hint_off_is_byte_parity_legacy(monkeypatch):
    import src.mcp.server as S
    monkeypatch.setattr(S, "SERVE_PLAIN_ANSWERS", False)
    assert S._recall_more_hint() == LEGACY_MORE_HINT


def test_sum_miss_off_is_byte_parity_legacy(monkeypatch):
    import src.api.main as M
    monkeypatch.setattr(M, "_SERVE_PLAIN_ANSWERS", False)
    fact = M._build_sum_over_walk_fact(
        {"miss": True, "sum": 0.0, "is_currency": False,
         "type_phrase": "ski trips"}, "anchor-uuid")
    assert fact is not None
    assert fact["definition"] == LEGACY_SUM_MISS
    assert fact["fact_class"] == "C"


def test_count_miss_off_is_byte_parity_legacy(monkeypatch):
    import src.api.main as M
    monkeypatch.setattr(M, "_SERVE_PLAIN_ANSWERS", False)
    assert M._count_miss_definition("ski trips") == LEGACY_COUNT_MISS


# ── ON: the backend synthetic miss rows lose the refusal tail too ─────────────────


def test_sum_miss_plain_on_has_no_refusal_tail():
    import src.api.main as M
    fact = M._build_sum_over_walk_fact(
        {"miss": True, "sum": 0.0, "is_currency": False,
         "type_phrase": "ski trips"}, "anchor-uuid")
    assert fact["definition"] == "I don't have any ski trips amounts on record."
    assert "can't" not in fact["definition"]
    assert fact["fact_class"] == "C"


def test_count_miss_plain_on_has_no_refusal_tail():
    import src.api.main as M
    assert M._count_miss_definition("ski trips") == \
        "I don't have any ski trips on record."
