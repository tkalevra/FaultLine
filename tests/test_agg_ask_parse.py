"""AGG_ASK_PARSE — the three AGGREGATION ASK-PARSE defects (query-side, upstream of any walk).

All three are defects in how a count/sum QUESTION is READ, not in the walk that answers it. Each
produced a WRONG ask rather than an honest miss, so each is observable purely from the detector
(no DB, no LLM, no fixtures) — which is exactly why they survived a green aggregation suite: every
existing count/sum test hands the detector an already-well-formed question.

Live evidence (LongMemEval clean run 2026-07-31, `CLEAN_v4pro_n68.json`):
  * 28dc39ac  "How many hours have I spent playing games in total?"  → type_phrase="total",
              rendered verbatim as "I don't have any TOTAL amounts on record, so I can't total
              that up." The aggregation cue's own preposition was bound as the object PP.
  * 2318644b  "How much more did I spend on accommodations per night in Hawaii compared to
              Tokyo?" → the SUM lane claimed a DIFFERENCE ask, type_phrase="accommodations per
              night in hawaii compared to tokyo", and abstained "I can't total that up".
  * gpt4_a56e767c  "How many movie festivals that I attended?" → NO count shape matched at all;
              the cardinality lane never fired.

NOTE ON SCOPE (deliberate, do not "fix" by widening): correcting the ask does NOT by itself make
those bench questions hit — their member facts were not captured at ingest (hour/money scalars
never scalarized, 3 of 4 festivals never stored). That is an INGEST gap owned elsewhere. These
tests pin the ASK, which is the only part that lives in the query layer.
"""
import re

import pytest

from src.api import main as M


# ── (1) AGGREGATION-CUE TYPE MIS-BINDING ────────────────────────────────────────────────────

def test_agg_cue_is_not_bound_as_the_type_object():
    """THE BUG: "in total" fed the object alternation, so the type resolved to "total"."""
    got = M._detect_sum_intent("How many hours have I spent playing games in total?")
    assert got, "the SUM ask must still be detected"
    assert got["type_phrase"] == "games", (
        f"aggregation cue leaked into the type phrase: {got['type_phrase']!r}")
    assert got["measure_phrase"] == "hours"


def test_agg_cue_never_survives_as_a_type_phrase():
    """No wording of the closed cue class may ever end up as the counted TYPE."""
    for cue in ("in total", "altogether", "combined", "in all", "all together"):
        got = M._detect_sum_intent(f"How many hours have I spent playing games {cue}?")
        assert got.get("type_phrase") != cue.split()[-1], f"cue {cue!r} bound as type"


def test_pp_object_branch_still_wins_over_the_gerund_branch():
    """aae3761f — a genuine PP object must still be read NON-greedily (first preposition wins),
    so the broadened gerund branch can never steal a well-formed PP ask."""
    got = M._detect_sum_intent(
        "How many hours in total did I spend driving to my three road trip "
        "destinations combined?")
    assert got["type_phrase"] == "my three road trip destinations"
    assert got["measure_phrase"] == "hours"


def test_gerund_branch_declines_a_trailing_calendar_noun():
    """The temporal-scope clause is peeled BEFORE the greedy verb span, so "…slept this week"
    can never yield type_phrase="week" (a calendar noun is a scope, never a counted type)."""
    got = M._detect_sum_intent("How many hours in total did I sleep this week?")
    assert got.get("type_phrase") not in ("week", "this week")


def test_gerund_branch_needs_a_verb_span_and_an_object():
    """A cue-marked ask with nothing after the verb declines rather than inventing a type."""
    assert M._detect_sum_intent("How many days in total did I work?") == {}


# ── (2) COMPARATIVE-DIFFERENCE DECLINE ──────────────────────────────────────────────────────

def test_comparative_difference_is_declined_by_the_sum_lane():
    """2318644b — a DIFFERENCE between two operands is not a total; Σ is the wrong operator."""
    assert M._detect_sum_intent(
        "How much more did I spend on accommodations per night in Hawaii "
        "compared to Tokyo?") == {}


@pytest.mark.parametrize("q", [
    "How much more did I spend on hotels in Hawaii than in Tokyo?",
    "How much less did I spend on food in Rome compared with Paris?",
    "How many fewer hours did I work in June versus May?",
])
def test_comparative_markers_all_decline(q):
    assert M._detect_sum_intent(q) == {}


def test_comparative_degree_alone_does_not_decline():
    """BOTH halves are required. A bare comparative with NO standard of comparison is an
    ordinary ask and must stay on today's path (CGEL ch.5 §16 — a comparative construction
    needs an explicit standard to be a comparison)."""
    got = M._detect_sum_intent("How much more money do I need for the trip?")
    assert got.get("type_phrase") == "the trip"


def test_plain_total_ask_is_untouched_by_the_comparative_guard():
    got = M._detect_sum_intent("How much money did I raise for charity in total?")
    assert got["type_phrase"] == "charity"
    assert got["measure_phrase"] == "money"


# ── (3) RELATIVE-CLAUSE CARDINALITY (Shape D) ───────────────────────────────────────────────

def test_relative_clause_cardinality_is_detected():
    """gpt4_a56e767c — no auxiliary precedes the pronoun, so Shapes A/C/B all decline."""
    got = M._detect_count_intent("How many movie festivals that I attended?")
    assert got.get("type_phrase") == "movie festivals"


def test_zero_relative_cardinality_is_detected():
    """English freely omits the relativizer in an object relative (CGEL ch.12, "zero relative")."""
    got = M._detect_count_intent("How many books I read last year?")
    assert got.get("type_phrase") == "books"


def test_shape_d_never_preempts_shape_a():
    """Shape D is ordered LAST: an auxiliary ask must still resolve through Shape A unchanged."""
    got = M._detect_count_intent("How many weddings have I attended in this year?")
    assert got["type_phrase"] == "weddings"
    assert got["window"] == "this year"


def test_shape_d_does_not_claim_a_duration_or_aggregation_ask():
    """Duration units and aggregation cues are declined BEFORE any shape runs, so the new
    shape can never hijack the temporal-calc or SUM lanes."""
    assert M._detect_count_intent("How many hours I spent gaming?") == {}
    assert M._detect_count_intent("How many trips I took in total?") == {}


# ── FLAG DISCIPLINE — OFF must be byte-for-byte legacy ──────────────────────────────────────

@pytest.fixture()
def agg_off(monkeypatch):
    monkeypatch.setattr(M, "AGG_ASK_PARSE", False)


def test_flag_off_restores_every_legacy_verdict(agg_off):
    # (1) the cue is bound as the type again
    assert M._detect_sum_intent(
        "How many hours have I spent playing games in total?")["type_phrase"] == "total"
    # (2) the comparative is claimed by the SUM lane again
    assert M._detect_sum_intent(
        "How much more did I spend on accommodations per night in Hawaii "
        "compared to Tokyo?")["measure_phrase"] == "more"
    # (3) the relative-clause cardinality is not detected at all
    assert M._detect_count_intent("How many movie festivals that I attended?") == {}


def test_flag_off_leaves_the_well_formed_asks_identical(agg_off):
    """The paths that were already correct must not move when the flag flips — that is what
    makes OFF a true legacy restore rather than a different behaviour."""
    assert M._detect_sum_intent(
        "How many hours in total did I spend driving to my three road trip "
        "destinations combined?")["type_phrase"] == "my three road trip destinations"
    assert M._detect_sum_intent(
        "How many days did I spend on camping trips?")["type_phrase"] == "camping trips"
    assert M._detect_count_intent(
        "How many tanks do I have?")["type_phrase"] == "tanks"


# ── FAIL-SAFE ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", None, "   ", "?", "how many"])
def test_detectors_never_raise(bad):
    assert M._detect_sum_intent(bad) == {}
    assert M._detect_count_intent(bad) == {}


def test_shared_cue_alternation_is_a_single_definition():
    """The cue class was previously repeated inline four times; a drifted copy is how (1) hid.
    Pin that both detectors read the SAME alternation."""
    for cue in ("in total", "altogether", "combined", "in all", "all together"):
        assert re.search(rf"\b{M._AGG_CUE_ALT}\b", f"x {cue} y"), cue
