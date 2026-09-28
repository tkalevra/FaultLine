"""Recurring day-of-week SCHEDULE scalar capture — LME schedule/recurring cluster.

ROOT CAUSE this pins: a recurring NAMED-TIME schedule adverbial attached to an activity — a
day-of-week the activity recurs on ("I have a cocktail-making class ON FRIDAYS", "I wake up …
ON TUESDAYS AND THURSDAYS") — was DROPPED by the SVO backbone: the weekday PROPN fell through as
uncovered residue, so "what day do I take my class" had no captured value to surface. The sibling
RATE pre-pass (``_rate_binds``) already captured COUNT-over-period recurrence ("three times a
week") but not a PREPOSITIONAL day-of-week recurrence.

The fix adds a SCHEDULE pre-pass in ``derive_sentence_facts``: a spaCy DATE-NER span (the NER's own
temporal-named judgment) governed by a preposition and gated by the dateparser DUAL-CLOCK FIREWALL —
a span dateparser RESOLVES to a concrete calendar date is a one-time WHEN (event_date lane, kept
OUT via ``_date_token_idx``); a span it cannot pin (a bare weekday name) is RECURRING, kept as a
schedule SCALAR bound to the clause subject. rel = the verb lemma QUALIFIED by the surface
preposition ("have_on"/"wake_on") so it never clobbers a same-verb time/measure scalar.

Deterministic (NER + UD dep/prep + dateparser firewall); subject-agnostic (NO weekday/day-name word
list). Covers MULTIPLE exemplars of the cluster (LME ce6d2d27 Friday, gpt4_2c50253f Tue/Thu days)
plus the firewall (a genuine one-time date must NOT become a schedule scalar).

FAILS on the pre-fix code (the weekday was dropped — no schedule scalar emitted).
"""
from datetime import date

import pytest

REF = date(2026, 7, 20)  # a Monday — session reference for the dateparser firewall


def _derive(sentence, ref=REF):
    from src.extraction.linguistics import derive_sentence_facts
    return list(derive_sentence_facts(sentence, reference=ref))


def _schedule_scalars(facts):
    """The recurring-schedule scalar edges: string-datatype, rel qualified by a preposition."""
    return [
        (f.subject, f.rel_type, f.object)
        for f in facts
        if f.scalar_datatype == "string" and "_" in (f.rel_type or "")
    ]


# ── EXEMPLAR: LME ce6d2d27 — day-of-week the class recurs on ──────────────────────────────────────
def test_day_of_week_class_captured_as_schedule_scalar():
    facts = _derive("By the way, I have a cocktail-making class on Fridays, "
                    "so maybe something I can experiment with then.")
    sched = _schedule_scalars(facts)
    # the gold "Friday" must be captured as a scalar value bound to the user (the anchor the lean
    # walk reads directly). Pre-fix: "Fridays" was uncovered residue → this list was empty.
    assert any(s == "user" and "friday" in v for (s, _r, v) in sched), sched


# ── EXEMPLAR: LME gpt4_2c50253f — coordinated day-of-week (the wake schedule) ──────────────────────
def test_coordinated_days_captured_whole():
    facts = _derive("I wake up at 6:45 AM on Tuesdays and Thursdays.")
    sched = _schedule_scalars(facts)
    # the coordination is recovered grammatically (NER tags only "Tuesdays"), NOT a day-name list.
    assert any(s == "user" and "tuesdays and thursdays" in v for (s, _r, v) in sched), sched
    # …and the clock TIME still lands on its own (uncollided) scalar — the "_on" suffix keeps the
    # day off the "wake" (time) attribute key.
    assert any(f.scalar_datatype == "string" and f.rel_type == "wake" and "6:45" in f.object
               for f in facts), [(f.rel_type, f.object) for f in facts]


# ── GENERALIZATION: a DIFFERENT weekday + a different verb — proves NO day-name / verb word list ───
def test_other_weekday_generalizes():
    facts = _derive("I go to therapy on Mondays.")
    sched = _schedule_scalars(facts)
    assert any(s == "user" and "monday" in v for (s, _r, v) in sched), sched


def test_non_first_person_subject_binds_to_that_subject():
    # subject-agnostic: a non-"I" subject binds the schedule to THAT subject, not the user.
    facts = _derive("My daughter has ballet on Saturdays.")
    sched = _schedule_scalars(facts)
    assert any("daughter" in s and "saturday" in v for (s, _r, v) in sched), sched


# ── FIREWALL: a genuine ONE-TIME date must NOT be captured as a schedule scalar ────────────────────
def test_one_time_date_is_event_date_not_schedule():
    facts = _derive("I flew to Paris on May 3rd.")
    # dateparser resolves "May 3rd" → event_date; it must NEVER leak into a schedule scalar.
    assert not any("may" in v for (_s, _r, v) in _schedule_scalars(facts)), facts
    assert any(f.event_date and f.event_date.startswith("2026-05-03") for f in facts), \
        [(f.rel_type, f.object, f.event_date) for f in facts]


def test_numeric_one_time_date_is_event_date_not_schedule():
    facts = _derive("I saw a house on 3/1.")
    assert not _schedule_scalars(facts) or all(
        "3/1" not in v and "march" not in v for (_s, _r, v) in _schedule_scalars(facts)), facts
    assert any(f.event_date and f.event_date.startswith("2026-03-01") for f in facts), \
        [(f.rel_type, f.object, f.event_date) for f in facts]


# ── NEGATION: a negated clause defers absence, emits no schedule scalar ────────────────────────────
def test_negated_schedule_suppressed():
    facts = _derive("I do not have a class on Fridays.")
    assert not any("friday" in v for (_s, _r, v) in _schedule_scalars(facts)), facts


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
