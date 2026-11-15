"""Employment-tenure DURATION capture + tenure-difference query (LME gpt4_93159ced).

Gold: "How long have I been working before I started my current job at NovaTech?" → "4 years
and 9 months".  The haystack states two DURATIONS — "I've been working professionally for 9
years" (total) and "I've been working at NovaTech for about 4 years and 3 months" (current
job) — and the answer is the reference-independent SUBTRACTION 9y − 4y3m = 4y9m.

INGEST ROOT CAUSE (rule 8 — duration is not a boundary, it was an ingest defect): a durative
"for <N years>" mis-routed via ``unit_scalar[year]=age`` to a junk ``age`` scalar on a junk
"work for" entity, and the compound "4 years and 3 months" SPLIT (year→age, month→duration) —
nothing clean for the tenure math to compute on.  The capture fix (``EMPLOYMENT_DURATION_SCALAR``,
linguistics ``_chain_employment_duration``) captures the WHOLE compound as ONE ``duration`` scalar
on the affiliation ORG (job tenure) else the user (total).  The query fix (``TEMPORAL_TENURE_DIFF``,
``_employment_tenure_diff``) subtracts total − current-job and renders years+months.

FAIL-ON-OLD: before the fix the deriver emitted NO ``duration`` scalar (it emitted ``age``), so
the capture assertions fail on the pre-fix code; ``_employment_tenure_diff`` did not exist.

Evidence-ground: ISO-8601-2 compound duration model; OWL-Time hasDurationDescription; Allen
(1983) interval algebra (duration = metric span).  Deterministic, subject-agnostic.
"""
import datetime
import os

import pytest

from src.extraction import linguistics
from src.extraction.linguistics import (
    duration_phrase_to_months, months_to_duration_phrase, derive_sentence_facts)
from src.api import main


_REF = datetime.date(2025, 1, 1)


def _facts(sentence):
    return derive_sentence_facts(sentence, _REF, None)


def _find(facts, rel):
    return [f for f in facts if str(f.rel_type) == rel]


# ── helper arithmetic (ISO-8601-2 compound duration) ─────────────────────────────
def test_duration_phrase_to_months_compound():
    assert duration_phrase_to_months("4 years and 3 months") == 51
    assert duration_phrase_to_months("9 years") == 108
    assert duration_phrase_to_months("18 months") == 18
    # no whole-month component → None (never fabricates)
    assert duration_phrase_to_months("about a while") is None


def test_months_to_duration_phrase_renders_years_and_months():
    assert months_to_duration_phrase(57) == "4 years and 9 months"
    assert months_to_duration_phrase(48) == "4 years"
    assert months_to_duration_phrase(9) == "9 months"
    assert months_to_duration_phrase(13) == "1 year and 1 month"


# ── CAPTURE: durative employment duration → a `duration` scalar (FAIL-ON-OLD) ─────
@pytest.mark.skipif(not linguistics.linguistics_available(),
                    reason="spaCy model required (set SPACY_MODEL=en_core_web_sm)")
def test_current_job_duration_scalar_on_org():
    """MULTI-COMPONENT: 'working at NovaTech for 4 years and 3 months' → the WHOLE compound as
    ONE duration scalar on the NovaTech job (not split year→age / month→duration)."""
    facts = _facts("I have been working at NovaTech for about 4 years and 3 months now.")
    durs = _find(facts, "duration")
    assert durs, f"no duration scalar emitted (old code → age): {[(f.subject, f.rel_type, f.object) for f in facts]}"
    d = durs[0]
    assert d.subject == "novatech", d.subject
    assert duration_phrase_to_months(d.object) == 51, d.object
    assert (d.scalar_datatype or "") == "duration"
    # the employer edge still lands
    assert any(f.rel_type == "works_for" and f.object == "novatech" for f in facts)
    # NO junk age scalar for this construction
    assert not _find(facts, "age"), [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.skipif(not linguistics.linguistics_available(),
                    reason="spaCy model required")
def test_total_work_duration_scalar_on_user():
    """BARE (no employer) → the TOTAL activity duration on the user."""
    facts = _facts("I have been working professionally for 9 years.")
    durs = _find(facts, "duration")
    assert durs, [(f.subject, f.rel_type, f.object) for f in facts]
    d = durs[0]
    assert d.subject == "user", d.subject
    assert duration_phrase_to_months(d.object) == 108, d.object


@pytest.mark.skipif(not linguistics.linguistics_available(),
                    reason="spaCy model required")
def test_single_period_simple_past():
    """SINGLE-PERIOD, simple past (no present-perfect): 'I worked at Globex for 3 years'."""
    facts = _facts("I worked at Globex for 3 years.")
    durs = _find(facts, "duration")
    assert durs, [(f.subject, f.rel_type, f.object) for f in facts]
    assert durs[0].subject == "globex"
    assert duration_phrase_to_months(durs[0].object) == 36


@pytest.mark.skipif(not linguistics.linguistics_available(),
                    reason="spaCy model required")
def test_age_reading_not_regressed():
    """The person-age reading ('62 years old') is UNCHANGED — year→age still fires for the
    predicative copula measure; only a durative 'for N years' routes to duration."""
    facts = _facts("she is 62 years old")
    ages = _find(facts, "age")
    assert any(str(f.object) == "62" for f in ages), [(f.subject, f.rel_type, f.object) for f in facts]
    assert not _find(facts, "duration")


# ── QUERY: tenure-difference renders "4 years and 9 months" ──────────────────────
def test_tenure_diff_routes_and_renders():
    """The full query path: the question routes to op=between + start_fallback, and
    ``_apply_temporal_calc`` computes TOTAL − CURRENT-job over the two duration scalars."""
    os.environ.pop("POSTGRES_DSN", None)  # force the in-memory duration-row fallback
    q = "how long have i been working before i started my current job at novatech?"
    calc = main._detect_temporal_calc_intent(q)
    assert calc.get("op") == "between"
    assert calc.get("start_fallback") == "earliest_before_b"
    facts = [
        {"rel_type": "duration", "subject": "you", "object": "9 years", "_subject_id": "USER"},
        {"rel_type": "duration", "subject": "novatech",
         "object": "4 years and 3 months", "_subject_id": "ORG1"},
    ]
    res = main._apply_temporal_calc(facts, {"calc": calc}, user_id="USER", query_text=q)
    assert res and not res.get("miss"), res
    assert "4 years and 9 months" in (res.get("answer") or ""), res


def test_tenure_diff_missing_operand_is_miss_not_fabrication():
    """Only the current-job duration present, no total → None (never a fabricated number)."""
    os.environ.pop("POSTGRES_DSN", None)
    q = "how long have i been working before i started my current job at novatech?"
    calc = main._detect_temporal_calc_intent(q)
    facts = [
        {"rel_type": "duration", "subject": "novatech",
         "object": "4 years and 3 months", "_subject_id": "ORG1"},
    ]
    res = main._employment_tenure_diff(calc.get("anchor_b"), "USER", facts)
    assert res is None


def test_tenure_diff_flag_off_no_op():
    """Flag OFF → the tenure-diff never fires (date-path behaviour)."""
    _prev = main.TEMPORAL_TENURE_DIFF
    main.TEMPORAL_TENURE_DIFF = False
    try:
        facts = [
            {"rel_type": "duration", "subject": "you", "object": "9 years", "_subject_id": "USER"},
            {"rel_type": "duration", "subject": "novatech",
             "object": "4 years and 3 months", "_subject_id": "ORG1"},
        ]
        assert main._employment_tenure_diff("i started at novatech", "USER", facts) is None
    finally:
        main.TEMPORAL_TENURE_DIFF = _prev
