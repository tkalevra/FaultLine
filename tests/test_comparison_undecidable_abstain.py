"""ABSTENTION via undecidable multi-operand comparison (LME _abs cluster).

Cluster (2 exemplars, same structural recall-precision defect):
  • gpt4_c27434e8_abs — "Which project did I start first, the Ferrari model or the Porsche
    991 Turbo S model?"  GOLD: not enough info — the Porsche was never mentioned.
  • gpt4_fe651585_abs  — "Who became a parent first, Tom or Alex?"  GOLD: not enough info —
    only Alex was mentioned, not Tom.

Defect: a "which/who … first, A or B" comparison resolves the ONE named operand that IS in
memory (Ferrari / Alex), orders its dated edge, and renders it as the confident winner —
FABRICATING a comparison the data cannot support. Correct behaviour = ABSTAIN (surface no
ordered comparison) so the reader answers "not enough information".

Root cause / fix seam: operand resolution in the /query temporal-comparison block. The
existing dated-slice guard (`TEMPORAL_UNDECIDABLE_COMPARISON_GUARD`) stays OFF because it
UNDERCOUNTS reified operands (a "tomatoes"/"bike" operand's date rides a sibling edge), which
would false-abstain LEGITIMATE comparisons. This fix keys instead on whether each NAMED
operand PHRASE resolved to ANY entity: a reified-but-real operand DID resolve; only a
never-mentioned operand resolves to nothing — so the gate fires ONLY on the true "operand
absent" case (`_comparison_operands_undecidable`).

These tests exercise the two pure seams directly (no DB): `_extract_comparison_operands`
(the operand parse) and `_comparison_operands_undecidable` (the abstention decision). The
fail-on-old / pass-on-new contract: `_comparison_operands_undecidable` does not exist on the
pre-fix code, so importing it fails collection on old and passes on the fix.
"""
import os

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

import pytest

from src.api.main import (
    _extract_comparison_operands,
    _comparison_operands_undecidable,
)


# ── OPERAND PARSE — both exemplars name TWO operands via the "A or B" alternation ──────────
@pytest.mark.parametrize("query,expected_n", [
    ("Which project did I start first, the Ferrari model or the Porsche 991 Turbo S model?", 2),
    ("Who became a parent first, Tom or Alex?", 2),
])
def test_comparison_queries_name_two_operands(query, expected_n):
    ops = _extract_comparison_operands(query)
    assert len(ops) >= expected_n, (query, ops)


def test_single_anchor_locative_query_names_no_operands():
    # Ex1 (Airbnb-in-Sacramento) is NOT a comparison — the operand parse must stay empty so
    # the comparison abstention gate never fires on a plain single-anchor query.
    assert _extract_comparison_operands("When did I book the Airbnb in Sacramento?") == []


# ── ABSTENTION DECISION — the load-bearing precision gate ──────────────────────────────────
def test_undecidable_when_one_named_operand_missing():
    # Ferrari resolved, Porsche never mentioned → 1 of 2 → undecidable → ABSTAIN.
    assert _comparison_operands_undecidable(
        ["ferrari model", "porsche 991 turbo s model"], 1, True) is True
    # Alex resolved, Tom never mentioned → 1 of 2 → undecidable → ABSTAIN.
    assert _comparison_operands_undecidable(["tom", "alex"], 1, True) is True


def test_undecidable_when_no_named_operand_resolves():
    assert _comparison_operands_undecidable(["a b", "c d"], 0, True) is True


def test_decidable_when_both_operands_resolve():
    # The no-regression case: a real comparison where BOTH operands exist must NOT abstain
    # (this is what keeps the reified-operand comparisons — tomatoes/bike — answerable).
    assert _comparison_operands_undecidable(["tomatoes", "marigolds"], 2, True) is False


def test_no_abstain_without_comparison_intent():
    # A non-ordinal / non-comparison query never triggers the gate even if two phrases got
    # parsed off a stray comma-list.
    assert _comparison_operands_undecidable(["a", "b"], 1, False) is False


def test_no_abstain_for_single_operand():
    assert _comparison_operands_undecidable(["airbnb"], 0, True) is False
    assert _comparison_operands_undecidable([], 0, True) is False


def test_failsafe_on_bad_input():
    # Non-int resolved-count must fail safe to "decidable" (never a spurious abstention).
    assert _comparison_operands_undecidable(["a", "b"], None, True) is False
