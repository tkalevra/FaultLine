"""Age-at-event temporal calc — "How old was I when I moved to the United States?"

LongMemEval temporal-reasoning exemplar d01c6aa8 (gold 27). The user stated a CURRENT age
("I'm a 32-year-old male", session date 2023/05/29) and, across the session, that they had
"been living in the United States for the past five years" — a relocation whose START reifies
as (user, lives_in, united states) @ 2018 (verified: the deriver + extract_event_date resolve
"five years ago"/"the past five years" against the session reference cleanly). Both anchors are
captured; the MISS was purely QUERY-MATH: "how old was I when <event>" matched NO temporal-calc
shape, so the year-difference age was never computed even with clean anchors.

age_at_event = event_year − birth_year, where the birth reference is the subject's OWN captured
scalar:
  • a CURRENT-age scalar anchored to the session reference: birth_year ≈ ref_year − age
    → 2018 − (2023 − 32) = 27  (this exemplar);
  • else a birth/creation DATE scalar (whole completed years birth→event).

Two defects on the pre-fix code, both asserted here (fail-on-old):
  • ``_detect_temporal_calc_intent`` returned ``{}`` for the "how old … when …" shape.
  • ``_apply_temporal_calc`` had no ``age_at_event`` op (returns None even given clean anchors).

Deterministic (pure regex + in-memory date math over the fact dicts + the [Date:] marker parse);
NO DB, NO LLM, NO network, NO spaCy. Subject-agnostic (subject/event are whatever the user named;
the "old"→age binding is the SAME single query-NLU bridge "how old is X" already uses).
"""
import datetime

import pytest

import src.api.main as m


# ── DETECTION: the "how old … when …" shape maps to the age_at_event op ────────────────────────

@pytest.mark.parametrize("q,ev_has,subj_has", [
    ("how old was i when i moved to the united states?", "moved", "i"),
    ("How old was I when I moved to the United States?".lower(), "united states", "i"),
    ("how old were you when you started the job", "started", "you"),
    ("how old was my mother when she moved to canada", "canada", "mother"),
])
def test_age_at_event_shape_detected(q, ev_has, subj_has):
    calc = m._detect_temporal_calc_intent(q)
    assert calc.get("op") == "age_at_event", calc       # pre-fix: {} (no shape matched)
    assert ev_has in calc.get("anchor", ""), calc        # event phrase after "when"
    assert subj_has in calc.get("subject", ""), calc


def test_age_at_event_does_not_hijack_durative_when_pivot():
    # "how long had I been X when Y" is a DURATION ask (rule 2d), NOT age-at-event — the
    # "how old" cue is absent, so the between op must still own it.
    calc = m._detect_temporal_calc_intent(
        "how long had i been bird watching when i attended the workshop?")
    assert calc.get("op") == "between", calc


def test_age_at_event_does_not_hijack_plain_how_long():
    calc = m._detect_temporal_calc_intent("how long between my dentist visit and my surgery?")
    assert calc.get("op") == "between", calc


# ── COMPUTATION: the op computes the year-difference age from clean anchors ─────────────────────

_QUERY = ("[Date: 2023/05/29 (Mon) 21:08]\n"
          "How old was I when I moved to the United States?")
_INTENT = {"calc": {"op": "age_at_event",
                    "anchor": "i moved to the united states", "subject": "i"}}


def test_current_age_scalar_path_hits_gold_27():
    # age=32 @ session ref 2023, move reified @ 2018 → 2018 − (2023 − 32) = 27 (GOLD).
    facts = [
        {"subject": "user", "rel_type": "age", "object": "32",
         "source": "attributes", "fact_class": "A"},
        {"subject": "user", "rel_type": "lives_in", "object": "united states",
         "event_date": "2018-05-29", "event_date_granularity": "day", "source": "facts"},
    ]
    res = m._apply_temporal_calc(facts, _INTENT, "u", query_text=_QUERY)
    assert res is not None                                # pre-fix: None (no age op)
    assert res.get("miss") is False, res
    assert res.get("value") == 27, res
    assert "27" in res.get("answer", ""), res            # gold token surfaces in prose
    assert "2018-05-29" in (res.get("cited_dates") or []), res


def test_birth_date_scalar_fallback_path():
    # No age scalar; a real DOB → whole completed years birth→event (event-relative, no now).
    facts = [
        {"subject": "user", "rel_type": "born_on", "object": "1990-03-01",
         "source": "attributes", "fact_class": "A"},
        {"subject": "user", "rel_type": "lives_in", "object": "united states",
         "event_date": "2018-05-29", "event_date_granularity": "day", "source": "facts"},
    ]
    res = m._apply_temporal_calc(facts, _INTENT, "u", query_text="")
    assert res is not None
    assert res.get("value") == 28, res                   # 1990-03-01 → 2018-05-29 = 28y
    assert "28" in res.get("answer", ""), res


def test_undated_event_fails_loud_no_fabrication():
    # The event has no date → miss-loud, never a fabricated age.
    facts = [
        {"subject": "user", "rel_type": "age", "object": "32",
         "source": "attributes", "fact_class": "A"},
        {"subject": "user", "rel_type": "lives_in", "object": "united states",
         "source": "facts"},   # no event_date
    ]
    res = m._apply_temporal_calc(facts, _INTENT, "u", query_text=_QUERY)
    assert res is None or res.get("miss") is True, res
    if res:
        assert res.get("value") is None, res


def test_no_birth_or_age_anchor_fails_loud():
    # Dated event but the subject has NO captured age/birth → miss, never a guess.
    facts = [
        {"subject": "user", "rel_type": "lives_in", "object": "united states",
         "event_date": "2018-05-29", "event_date_granularity": "day", "source": "facts"},
    ]
    res = m._apply_temporal_calc(facts, _INTENT, "u", query_text=_QUERY)
    assert res is None or res.get("miss") is True, res


def test_compute_helper_year_difference_direct():
    # Unit-level: the current-age helper is pure year-difference vs the reference.
    facts = [{"subject": "user", "rel_type": "age", "object": "40",
              "source": "attributes", "fact_class": "A"}]
    ref = datetime.date(2024, 1, 1)
    ev = datetime.date(2010, 6, 15)
    assert m._compute_age_at_event(facts, ev, ref) == 2010 - (2024 - 40)  # == 26
