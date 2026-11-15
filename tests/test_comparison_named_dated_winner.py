"""ANSWER-SHAPING: NAME the winning operand of a comparison — SUBJECT-AGNOSTIC gate.

Owner flag: the shipped `_comparison_query_is_travel` gate (WordNet trip⊑travel) only
fired the named-winner render for TRAVEL comparisons. Non-travel "which/who <predicate>
first, A or B" comparisons still dumped / mis-ordered even when BOTH operands were dated.

Fix (src/api/main.py, query/render — no ingest change): replace the TYPE gate with a
DATED-OPERAND SYMMETRY gate. A comparison is decidable/nameable iff ≥2 DISTINCT operands
each reach a DATED event/instance (event_date present). Then NAME the earlier/later operand
in the user's own phrase with a NEUTRAL ordering verb ("came first" / "was the most recent")
so it reads for ANY type — event, item, purchase, malfunction, person, trip.

Cluster covered (4 exemplars, ONE structural gate defect):
  • gpt4_0b2f1d21 — "which event first, purchase of the coffee maker or malfunction of the
    stand mixer" — GOLD "The malfunction of the stand mixer".   (NON-travel → old gate MISS)
  • gpt4_6ed717ea — "which item first, the dog bed for Max or the training pads for Luna" —
    GOLD "Training pads for Luna".                              (NON-travel → old gate MISS)
  • gpt4_8c8961ae — "which trip first, Europe with family or the solo trip to Thailand" —
    GOLD "The solo trip to Thailand".                           (travel → still HIT)
  • gpt4_c27434e8 — "which project first, the Ferrari model or the Japanese Zero fighter
    plane model" — GOLD Japanese Zero; only the Ferrari has a stated date → ABSTAIN (kept).

FAIL-ON-OLD / PASS-ON-FIX: the new symbols (`_dated_comparison_winner`,
`_named_comparison_winner_definition`) do not exist on the pre-fix code → import fails
collection on old, passes on the fix. And `_comparison_query_is_travel` (kept, now demoted
from the gate) returns False for the two NON-travel exemplars — proving the OLD gate would
have blocked the named winner there, while the NEW dated-operand gate names it.

Evidence: Allen (1983) *Maintaining Knowledge about Temporal Intervals*, CACM 26(11) —
'precedes' (<) over point events keyed on real event_date values. Deterministic, no cosine.
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

from src.api.main import (
    _comparison_query_is_travel,
    _operand_min_dated_event,
    _dated_comparison_winner,
    _named_comparison_winner_definition,
)


# ── NEW GATE: dated-operand symmetry (subject-agnostic) ────────────────────────────────────
def test_two_dated_operands_names_earliest_for_first():
    # gpt4_6ed717ea: training pads (2023-04-20) BEFORE dog bed (2023-04-29) → training pads.
    win = _dated_comparison_winner(
        [("the dog bed for max", "2023-04-29"),
         ("the training pads for luna", "2023-04-20")],
        "first",
    )
    assert win is not None
    assert win["phrase"] == "the training pads for luna"
    assert win["boundary"] == "first"


def test_two_dated_operands_names_earliest_event_cluster():
    # gpt4_0b2f1d21: malfunction (2023-04) BEFORE coffee-maker purchase (2023-05-01).
    win = _dated_comparison_winner(
        [("the purchase of the coffee maker", "2023-05-01"),
         ("the malfunction of the stand mixer", "2023-04-15")],
        "first",
    )
    assert win is not None and win["phrase"] == "the malfunction of the stand mixer"


def test_two_dated_operands_last_boundary_names_latest():
    win = _dated_comparison_winner(
        [("the solo trip to thailand", "2022-06-01"),
         ("the europe trip with family", "2023-04-01")],
        "last",
    )
    assert win is not None and win["phrase"] == "the europe trip with family"


def test_single_dated_operand_abstains():
    # gpt4_c27434e8: only the Ferrari model has a start date; the Japanese Zero has none →
    # naming a winner would fabricate an ordering over an undated operand → abstain.
    assert _dated_comparison_winner([("the ferrari model", "2023-05-03")], "first") is None


def test_no_dated_operand_abstains():
    assert _dated_comparison_winner([], "first") is None
    assert _dated_comparison_winner(None, "first") is None


def test_two_pairs_same_phrase_is_not_two_distinct_operands():
    # A single operand dated twice (same phrase) is NOT two distinct comparands → abstain.
    assert _dated_comparison_winner(
        [("the ferrari model", "2023-05-03"), ("the ferrari model", "2023-05-10")], "first"
    ) is None


# ── FAIL-ON-OLD PROOF: the retired travel gate would have blocked the NON-travel cluster ───
@pytest.mark.parametrize("query", [
    "Which event happened first, the purchase of the coffee maker or the malfunction of the "
    "stand mixer?",
    "Which item did I purchase first, the dog bed for Max or the training pads for Luna?",
])
def test_old_travel_gate_would_have_blocked_non_travel(query):
    # The OLD gate returns False for these → the named-winner block never fired → the miss.
    # The NEW gate does not consult this at all; it keys on dated-operand symmetry instead.
    assert _comparison_query_is_travel(query) is False, query


# ── DIRECT ANSWER prose — names the winner in the user's own words, ANY type ────────────────
@pytest.mark.parametrize("phrase,tokens", [
    ("the training pads for luna", ("training", "pads", "luna")),   # gpt4_6ed717ea
    ("the malfunction of the stand mixer", ("malfunction", "stand", "mixer")),  # gpt4_0b2f1d21
    ("solo trip to thailand", ("solo", "trip", "thailand")),        # gpt4_8c8961ae (travel)
    ("family road trip across the american southwest",
     ("family", "road", "trip", "american", "southwest")),         # gpt4_d31cdae3 (travel)
])
def test_named_answer_carries_gold_tokens_any_type(phrase, tokens):
    low = _named_comparison_winner_definition(phrase, "first").lower()
    for tok in tokens:
        assert tok in low, (tok, low)
    assert "came first" in low  # neutral, subject-agnostic ordering verb (not "you took")


def test_named_answer_adds_determiner_only_when_missing():
    # No double determiner when the phrase already carries one.
    assert _named_comparison_winner_definition(
        "the training pads for luna", "first").lower().count("the training") == 1
    # A bare phrase gets a natural leading determiner.
    assert "the solo trip" in _named_comparison_winner_definition(
        "solo trip to thailand", "first").lower()


def test_named_answer_last_boundary_wording():
    low = _named_comparison_winner_definition("solo trip to thailand", "last").lower()
    assert "most recent" in low
    assert "thailand" in low


# ── read helper — fail-safe guards (SQL happy-path is verified live on pre-prod) ───────────
def test_operand_min_dated_event_guards():
    assert _operand_min_dated_event(None, ["x"]) is None          # no db
    assert _operand_min_dated_event(object(), None) is None       # no ids
    assert _operand_min_dated_event(object(), []) is None         # empty ids
