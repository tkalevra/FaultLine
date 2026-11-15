"""LME cluster `event-ordering` — enumerated multi-event "order these N events" recall.

A "which N events happened in the order from first to last: A, B, and C" ask names its
operands as a COLON-led, comma-separated LIST joined by a trailing "and"/"or" — NOT the
two-way "A or B" alternation the operand extractor originally handled. Before the fix the
first-comma split + " or " split recovered ZERO operands for this shape, so the walk fell
back to the unscoped anchor set and recall returned unrelated dated events (the observed
miss for gpt4_f49edff3).

These tests pin the subject-agnostic ENUMERATION parse of `_extract_comparison_operands`
across MULTIPLE cluster-shaped cases, and guard that the existing "A or B" alternation and
the non-comparison (empty) behaviour are byte-for-byte unchanged.
"""
import src.api.main as m


# ── ENUMERATION shape (the cluster) — ≥2 operands must be recovered ───────────────
def test_enumeration_colon_led_three_events_with_marker():
    # The gpt4_f49edff3 exemplar shape (harness [Date: …] marker + colon-led 3-item list).
    q = ("[Date: 2023/04/10 (mon) 23:07] Which three events happened in the order from "
         "first to last: the day I helped my friend prepare the nursery, the day I "
         "helped my cousin pick out stuff for her baby shower, and the day I ordered a crib?")
    ops = m._extract_comparison_operands(q)
    assert len(ops) == 3
    assert any("nursery" in o for o in ops)
    assert any("baby shower" in o for o in ops)
    assert any("ordered a crib" in o for o in ops)
    # The harness marker time-colon / date tokens must NOT leak into an operand.
    assert not any("2023" in o or "23:07" in o for o in ops)


def test_enumeration_list_in_order_three_items():
    # A DIFFERENT subject (domain-agnostic): the same enumeration grammar.
    q = "List these events in order: the marketing workshop, the sales webinar, and the design review"
    ops = m._extract_comparison_operands(q)
    assert ops == ["marketing workshop", "sales webinar", "design review"]


def test_enumeration_chronological_order_and_joined():
    q = "Put in chronological order: my trip to Paris, my move to Berlin and my job in Rome"
    ops = m._extract_comparison_operands(q)
    assert ops == ["trip to paris", "move to berlin", "job in rome"]


# ── REGRESSION GUARDS — existing shapes byte-for-byte unchanged ───────────────────
def test_two_operand_or_alternation_unchanged():
    ops = m._extract_comparison_operands("which seeds started first, tomatoes or marigolds?")
    assert ops == ["tomatoes", "marigolds"]


def test_quoted_titles_still_win():
    q = ("which was earlier, the 'data analysis using python' workshop or the "
         "'intro to sql' webinar?")
    ops = m._extract_comparison_operands(q)
    assert ops == ["data analysis using python", "intro to sql"]


def test_non_comparison_query_yields_no_operands():
    assert m._extract_comparison_operands("who is my spouse") == []


def test_single_item_list_is_not_an_enumeration():
    # A lone comma-less / single-item "list" must NOT fabricate operands (needs ≥2).
    assert m._extract_comparison_operands("what happened first") == []
