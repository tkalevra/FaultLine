"""Regression: spaCy DATE-NER OVER-EXTENSION recovery in the event-date resolver.

ROOT CAUSE (LongMemEval temporal-reasoning elapsed/between cluster — qids a3045048, dcfa8644,
993da5e2): spaCy's CNN DATE-NER intermittently over-extends a date span to swallow a leading
non-temporal noun phrase ("I rearranged the furniture three weeks ago" → DATE span
"the furniture three weeks ago"), and ``dateparser.parse`` returns None on the noun-polluted WHOLE,
so a perfectly datable OPERAND event lands UNDATED. The elapsed/between/duration query MATH already
works on cleanly-dated operands, so a missing operand date = the question misses.

FIX (src/extraction/linguistics.py::_recover_overextended_date_core, wired into
_resolve_first_valid_date): when the whole NER span fails to parse, re-extract the embedded date CORE
with dateparser's own noisy-text finder ``dateparser.search.search_dates`` (the authoritative library
API) and re-resolve that clean core through the SAME year-anchoring / vague-month gates. Deterministic
(no ML), subject-agnostic (no date/word lists — the date-ness judgment is dateparser's), non-fabricating
(returns a core only when it is a PROPER substring the finder identifies as a date; a non-date → None).

FAIL-ON-OLD: on the pre-fix code every ``over_extended`` case below returns None (the operand is
dropped). PASS-ON-FIX: each resolves to its correct calendar date. The negative + regression cases
guard against fabrication / behavior drift on the clean-span path.

Run: SPACY_MODEL=en_core_web_sm python3 -m pytest tests/test_temporal_ner_overextension_recovery.py -q
(tests/ is gitignored → git add -f)
"""
import importlib
import os
from datetime import datetime, timezone

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _layers_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    try:
        import dateparser  # noqa: F401
        from dateparser.search import search_dates  # noqa: F401
    except Exception:
        return False
    return m._get_nlp_ner() is not None


_HAS_LAYERS = _layers_available()
requires_layers = pytest.mark.skipif(
    not _HAS_LAYERS, reason="en_core_web_sm and/or dateparser[search] not installed in test env"
)


def _d(s):
    return s[:10] if s else None


# Session references, one per exemplar conversation (LongMemEval question_date).
REF_EX1 = datetime(2022, 5, 15, tzinfo=timezone.utc)   # a3045048 (best-friend gift)
REF_EX2 = datetime(2023, 2, 3, tzinfo=timezone.utc)    # dcfa8644 (Adidas / Converse)
REF_EX3 = datetime(2023, 5, 30, tzinfo=timezone.utc)   # 993da5e2 (area rug / furniture)


# ── OVER-EXTENSION CASES: spaCy swallows a leading NP → old code drops the date ──
# Each is a real gold-bearing operand clause from the cluster (or the same structural shape).
@requires_layers
@pytest.mark.parametrize("text,ref,expected", [
    # 993da5e2 operand-2: the exact gold turn — "the furniture three weeks ago" is the polluted span.
    ("Since I recently rearranged the furniture three weeks ago, I want some ideas.",
     REF_EX3, "2023-05-09"),
    ("I rearranged the furniture three weeks ago", REF_EX3, "2023-05-09"),
    # dcfa8644-adjacent: "the office party on February 5th" comes back as one over-extended DATE span.
    ("I scuffed my boots at the office party on February 5th", REF_EX2, "2023-02-05"),
    # generic over-extension over a determiner+noun + relative offset (subject-agnostic shape).
    ("I replaced the carpet a month ago", REF_EX3, "2023-04-30"),
])
def test_overextended_date_span_is_recovered(text, ref, expected):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, ref)
    assert _d(iso) == expected, f"{text!r} → {iso!r}, expected {expected} (over-extension not recovered)"
    assert gran in ("day", "month", "year")


# ── END-TO-END ARITHMETIC: with BOTH operands now dated, the elapsed matches gold ──
@requires_layers
def test_elapsed_between_operands_matches_gold_993da5e2():
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    # operand-1: "got a new area rug ... a month ago" (already resolved pre-fix)
    rug, _ = m.extract_event_date(
        "I recently got a new area rug for my living room a month ago.", REF_EX3)
    # operand-2: "rearranged the furniture three weeks ago" (recovered by the fix)
    rearrange, _ = m.extract_event_date(
        "Since I recently rearranged the furniture three weeks ago, I want ideas.", REF_EX3)
    assert rug is not None and rearrange is not None, "both operands must be dated for the elapsed calc"
    days = abs((datetime.fromisoformat(rearrange) - datetime.fromisoformat(rug)).days)
    # GOLD: "One week. Answers ranging from 7 days to 10 days are also acceptable."
    assert 7 <= days <= 10, f"elapsed={days}d not in gold range [7,10]"


# ── NEGATIVE: a non-date span must NOT gain a fabricated date via the recovery path ──
@requires_layers
@pytest.mark.parametrize("text", [
    "I rearranged the living room furniture",   # no temporal token at all
    "I like my new furniture",
    "the GPS system",
])
def test_recovery_never_fabricates_on_non_dates(text):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, gran = m.extract_event_date(text, REF_EX3)
    assert iso is None, f"{text!r} fabricated a date {iso!r}"
    assert gran is None


# ── REGRESSION: clean spans that ALREADY resolved must be byte-for-byte unchanged ──
@requires_layers
@pytest.mark.parametrize("text,ref,expected", [
    ("I ordered it on the 15th of April and it arrived on the 20th.", REF_EX1, "2022-04-15"),
    ("it was on the 22nd of April", REF_EX1, "2022-04-22"),
    ("I recently got a new pair of Adidas running shoes on January 10th.", REF_EX2, "2023-01-10"),
    ("the shoelaces on my old Converse sneakers had broken on January 24th", REF_EX2, "2023-01-24"),
    ("three weeks ago", REF_EX3, "2023-05-09"),   # bare clean relative still fine
])
def test_clean_spans_unchanged(text, ref, expected):
    m = _reload(LINGUISTIC_LAYER="true", TEMPORAL_DATE_LAYER="true")
    iso, _ = m.extract_event_date(text, ref)
    assert _d(iso) == expected, f"{text!r} → {iso!r}, expected {expected} (regressed a clean span)"
