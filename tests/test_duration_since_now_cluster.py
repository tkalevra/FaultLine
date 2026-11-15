"""Duration-since-now cluster — "How long have I had/been <X>" = NOW − X's inception.

LongMemEval knowledge-update exemplars e61a7584 (cat Luna → 9 months), 08e075c7 (Fitbit
Charge 3 → 9 months), 2133c1b5 (apartment in Harajuku → 3 months). ONE shared INGEST defect
plus a missing QUERY calc-shape:

INGEST (root cause) — the DURATIVE present-perfect "for <N> <unit> [now]" adjunct
("I've HAD my cat for 9 months now", "I've BEEN using my Fitbit for 9 months now", "I've
BEEN living in Harajuku for 3 months now") was DROPPED or mis-captured as a JUNK relationship
object (`luna have "9 months"`, `user live "3 months"`) and X's start date was NEVER recorded.
The state STARTED <N> <unit> before the session, so its INCEPTION = reference − duration
(d01c6aa8 boundary #12(A) durative-state inception). Fix: ``_durative_for_inception`` in
src/extraction/linguistics.py peels the "for <N> <unit>" span (so it cannot fold into a junk
object) and dates the governing state's inception at reference − duration, at the STATED unit's
granularity. Structural (ADP "for" → NUM-nummod time-unit pobj under a present-perfect verb),
subject-agnostic, self-validated by the proven "<N> <unit> ago" resolver (no unit word-list).

QUERY — "how long have I had/been <X>" matched NO calc shape (rule 5 needs "ago", rule 2d needs
"when"), so the elapsed = now − start math never fired. Fix: rule 5b maps it to the proven
``elapsed`` op (anchor = the possessed/stative phrase); elapsed now measures against the SESSION
date (derive_now) not 2026 wall-clock; and a month/year-granular start renders as an exact
CALENDAR delta ("9 months") via ``_calendar_elapsed_phrase`` instead of a day-count's spurious
exact-week coarsening ("39 weeks").

Both FAIL on pre-fix code:
  • pre-fix ``derive_sentence_facts`` produces NO dated inception (junk `luna have "9 months"`,
    dropped duration, or `user live "3 months"`) — the date/distinctness assertions fail.
  • pre-fix ``_detect_temporal_calc_intent`` returns op="duration" (single-point) for
    "how long have I had X", so no now−start elapsed — the op assertion fails.
Deterministic (spaCy en_core_web_sm + dateparser/dateutil), no DB, no LLM, no network.
"""
from datetime import datetime, timezone, date

import pytest


REF = datetime(2023, 3, 11, tzinfo=timezone.utc)  # session reference


# ── INGEST: the durative "for <N> <unit> now" dates the inception at reference − N ───────────

def _dated_facts(text):
    from src.extraction.linguistics import derive_sentence_facts, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    return [f for f in derive_sentence_facts(text, REF) if getattr(f, "event_date", None)]


@pytest.mark.parametrize("text,obj_needle,expect_iso,expect_gran", [
    # cat Luna → 9 months before 2023-03-11 = 2022-06-11, stated in MONTHS
    ("I have had my cat Luna for about 9 months now.", "cat", "2022-06-11", "month"),
    # Fitbit → 9 months → 2022-06-11
    ("I have been using my Fitbit Charge 3 for 9 months now.", "fitbit", "2022-06-11", "month"),
    # apartment/Harajuku → 3 months → 2022-12-11
    ("I have been living in Harajuku for 3 months now.", "harajuku", "2022-12-11", "month"),
])
def test_durative_inception_is_dated_at_reference_minus_duration(
        text, obj_needle, expect_iso, expect_gran):
    facts = _dated_facts(text)
    assert facts, f"pre-fix: NO dated inception fact for {text!r}"
    hit = [f for f in facts if obj_needle in (f.object or "").lower()
           or obj_needle in (f.subject or "").lower()]
    assert hit, f"no dated fact carrying {obj_needle!r}: {[(f.subject,f.rel_type,f.object) for f in facts]}"
    f = hit[0]
    assert str(f.event_date)[:10] == expect_iso, (f.object, f.event_date)
    assert (f.event_date_granularity or "").lower() == expect_gran, f.event_date_granularity


def test_durative_does_not_emit_duration_as_relationship_object():
    # THE JUNK the fix removes: pre-fix produced `(luna, have, "about 9 months")` /
    # `(user, live, "3 months")` — the duration folded into a relationship object.
    from src.extraction.linguistics import derive_sentence_facts, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    for text in ("I have had my cat Luna for about 9 months now.",
                 "I have been living in Harajuku for 3 months now."):
        for f in derive_sentence_facts(text, REF):
            assert "month" not in (f.object or "").lower(), (text, f.subject, f.rel_type, f.object)


def test_harajuku_captures_the_location_not_the_duration():
    # Pre-fix "3 months" STOLE the object slot of "live" (Harajuku dropped entirely). The
    # peel frees the slot so the locative is captured.
    facts = _dated_facts("I have been living in Harajuku for 3 months now.")
    assert any("harajuku" in (f.object or "").lower() for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.parametrize("text", [
    "I have had my cat Luna for 9 months now.",
    "I have been using my Fitbit for 9 months now.",
    "I have been living in my apartment for 3 months now.",
])
def test_no_future_dated_edge_from_durative(text):
    # POSSESSION/stray-future-date bug: dateparser mis-reads "9 months now" as ref+3 (FUTURE)
    # and stamps it onto secondary edges. Assert the deriver never emits a future-dated edge.
    from src.extraction.linguistics import derive_sentence_facts, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    ref_day = REF.date()
    for f in derive_sentence_facts(text, REF):
        ed = getattr(f, "event_date", None)
        if ed:
            assert date.fromisoformat(str(ed)[:10]) <= ref_day, (f.rel_type, f.object, ed)


@pytest.mark.parametrize("text,expect_iso,expect_gran", [
    ("I have had my cat Luna for 9 months now.", "2022-06-11", "month"),
    ("I have been using my Fitbit for 9 months now.", "2022-06-11", "month"),
    ("I have been living in my apartment for 3 months now.", "2022-12-11", "month"),
])
def test_request_level_durative_inception_is_past(text, expect_iso, expect_gran):
    # The request-level override (feeds _req_event_date → the possession has_pet/owns edge and
    # every other undated edge) resolves the durative to the PAST inception (reference − N),
    # NEVER the dateparser future anomaly. This is what makes has_pet|luna@(D−9mo), not @(D+3mo).
    from src.extraction.linguistics import durative_inception_from_text, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    iso, gran = durative_inception_from_text(text, REF)
    assert iso is not None, text
    assert str(iso)[:10] == expect_iso, (text, iso)
    assert (gran or "").lower() == expect_gran, gran
    assert date.fromisoformat(str(iso)[:10]) < REF.date(), (text, iso)  # strictly PAST


def test_durative_inception_from_text_noops_off_construction():
    # No durative "for N unit now" → (None, None), so the request path keeps _detect_temporal.
    from src.extraction.linguistics import durative_inception_from_text, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    assert durative_inception_from_text("I moved to Boston on March 1st.", REF) == (None, None)
    assert durative_inception_from_text("I lived in Kyoto for 3 months.", REF) == (None, None)


def test_simple_past_for_duration_does_not_fire_inception():
    # Subject-agnostic gate: only PRESENT-PERFECT durative fires. A completed simple-past
    # "for N" ("I lived there for 3 months") is NOT an ongoing-until-now inception.
    from src.extraction.linguistics import _durative_for_inception, _get_nlp, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy model unavailable")
    doc = _get_nlp()("I lived in Kyoto for 3 months.")
    assert _durative_for_inception(doc, REF) == []


# ── QUERY: "how long have I had/been X" → elapsed(now − inception) ───────────────────────────

def _calc(q):
    import src.api.main as m
    return m._detect_temporal_calc_intent(q.lower())


@pytest.mark.parametrize("q,anchor_has", [
    ("How long have I had my cat, Luna?", "luna"),
    ("How long have I been using my Fitbit Charge 3?", "fitbit"),
    ("How long have I been living in my current apartment in Harajuku?", "harajuku"),
])
def test_duration_since_now_maps_to_elapsed(q, anchor_has):
    calc = _calc(q)
    assert calc.get("op") == "elapsed", calc          # pre-fix: op="duration" (single-point)
    assert anchor_has in calc.get("anchor", ""), calc


def test_since_now_does_not_hijack_ago_or_when_or_between():
    # rule 5 (ago), rule 2d (when-pivot), rule 2 (between) still own their shapes.
    assert _calc("How long ago did I move to Boston?").get("op") == "elapsed"
    assert _calc("How long had I been bird watching when I attended the workshop?"
                 ).get("op") == "between"
    assert _calc("How long between my dentist visit and my surgery?").get("op") == "between"
    # a genuine "how long did X last" single-event duration stays duration.
    assert _calc("How long did my trip last?").get("op") == "duration"


def test_elapsed_uses_session_now_and_renders_stated_unit():
    # End-to-end calc: a month-granular inception at session−9mo answers "9 months" against
    # the SESSION date (not 2026 wall-clock, which would read ~4 years / "39 weeks").
    import src.api.main as m
    facts = [{"subject": "user", "rel_type": "have", "object": "cat",
              "definition": "cat luna", "event_date": "2022-06-11",
              "event_date_granularity": "month", "_subject_id": "u", "_object_id": "c"}]
    intent = {"calc": _calc("How long have I had my cat, Luna?")}
    res = m._apply_temporal_calc(
        facts, intent, None,
        query_text="[Date: 2023-03-11] How long have I had my cat, Luna?")
    assert res and not res.get("miss"), res
    assert "9 month" in res["answer"], res           # the gold token; pre-fix: no elapsed at all


def test_calendar_elapsed_phrase_is_exact():
    # relativedelta render: 2022-06-11 → 2023-03-11 = exactly 9 months (not "39 weeks").
    import src.api.main as m
    assert m._calendar_elapsed_phrase("2022-06-11", date(2023, 3, 11), "month") == "9 months"
    assert m._calendar_elapsed_phrase("2022-12-11", date(2023, 3, 11), "month") == "3 months"
    # day-granular is NOT calendar-rendered here (day-count render owns it).
    assert m._calendar_elapsed_phrase("2023-03-01", date(2023, 3, 11), "day") is None
