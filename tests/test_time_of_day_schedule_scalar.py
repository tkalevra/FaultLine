"""Time-of-day SCHEDULE scalar — multi-schedule collision fix (LME gpt4_2c50253f).

ROOT CAUSE this pins: a recurring CLOCK-TIME-OF-DAY bound to a day/period FRAME ("I wake up at
6:45 AM on Tuesdays and Thursdays") was captured as TWO divorced scalars — the clock time on the
BARE verb-lemma attribute ("wake" = "6:45 am") and the day span on the prep-qualified attribute
("wake_on" = "tuesdays and thursdays"). Fine for ONE schedule, but a person states SEVERAL wake
times ("… 8:30 on weekdays … 6:45 on Tuesdays and Thursdays"): every clock time lands on the SAME
"wake" key, and because entity_attributes is UNIQUE(entity_id, attribute) each OVERWRITES the last
— only one wake time survives and the day-scoped answer ("what time on Tuesdays and Thursdays?")
is lost. The real haystack recall returned OTHER wake times (8:30 / 7:00) but never the 6:45.

THE FIX (SPINE_TIME_OF_DAY_SCHEDULE, default ON, ADDITIVE): when a verb carries BOTH a clock-time
measure AND a recurring day/period frame, ALSO emit ONE frame-KEYED schedule-time scalar —
rel = f"{verb}_{prep}_{frame_slug}" (frame from the parsed day span, NOT a literal), value = the
clock time. Distinct schedules then occupy DISTINCT keys → all coexist → the walk surfaces the
day-matching one. The two legacy scalars are UNTOUCHED (byte-identical), so this only ADDS a key.

Evidence-ground: the clock time is TIMEX3's TIME (a time-of-day point, ISO-8601 hh:mm), NOT a DATE
(a calendar day) — Pustejovsky et al. 2003 (TimeML/TIMEX3). It is therefore NEVER an event_date
(the temporal peel already returns None) and NEVER an age (the copula-measure age gate already
refuses a ':'-bearing structured literal). The recurring day/period is the UD nmod:tmod temporal
frame (Universal Dependencies). Deterministic (NER TIME clock span + the schedule pre-pass's
dateparser-firewalled recurring DATE frame); subject-agnostic (NO day-name / verb / domain list).

FAILS on the pre-fix code (no frame-keyed schedule-time scalar existed → the two schedules'
clock times shared one key and could not coexist).
"""
from datetime import date

import pytest

REF = date(2026, 7, 20)  # a Monday — session reference for the dateparser firewall


def _derive(sentence, ref=REF):
    from src.extraction.linguistics import derive_sentence_facts
    return list(derive_sentence_facts(sentence, reference=ref))


def _scalars(facts):
    return [(f.rel_type, f.object) for f in facts if f.scalar_datatype == "string"]


# ── CORE: the gold — a frame-keyed schedule-time scalar carries the clock time (uncollided) ────────
def test_frame_keyed_schedule_time_scalar_emitted():
    facts = _derive("I wake up at 6:45 AM on Tuesdays and Thursdays.")
    sc = _scalars(facts)
    # NEW (fail-on-old): a scalar whose ATTRIBUTE is qualified by the day FRAME carries the clock
    # time — this is the uncollided key that survives a multi-schedule haystack.
    assert any("tuesdays" in r and "thursdays" in r and "6:45" in v for (r, v) in sc), sc
    # the two LEGACY scalars remain (byte-identical): bare "wake" = time, "wake_on" = days.
    assert ("wake", "6:45 am") in sc, sc
    assert any(r == "wake_on" and "tuesdays and thursdays" in v for (r, v) in sc), sc


# ── COLLISION: two schedules on the same verb occupy DISTINCT frame-keyed attributes ───────────────
def test_two_schedules_do_not_share_a_key():
    a = dict(_scalars(_derive("I usually wake up at 8:30 am on weekdays.")))
    b = dict(_scalars(_derive("I wake up at 6:45 AM on Tuesdays and Thursdays.")))
    # the frame-keyed attribute names differ → both survive entity_attributes' UNIQUE(entity,attr).
    keys_a = {k for k in a if k.startswith("wake_on_")}
    keys_b = {k for k in b if k.startswith("wake_on_")}
    assert keys_a and keys_b and keys_a.isdisjoint(keys_b), (keys_a, keys_b)


# ── GENERALIZATION: a different verb + different day proves NO verb/day-name literal ────────────────
def test_generalizes_across_verb_and_day():
    facts = _derive("I go to the gym at 6:00 AM on Mondays.")
    sc = _scalars(facts)
    assert any(r.startswith("go_") and "monday" in r and "6:00" in v for (r, v) in sc), sc


# ── NEGATIVE (clock-time-not-date): a clock time is NEVER an event_date ────────────────────────────
def test_clock_time_is_not_an_event_date():
    from src.extraction.linguistics import extract_event_date
    for clock in ("6:45 AM", "at 6:45 AM", "8:30 am", "9:00 PM"):
        d, _ = extract_event_date(clock, REF)
        assert d is None, (clock, d)
    # …and no fact in the wake sentence carries an event_date derived from the clock time.
    facts = _derive("I wake up at 6:45 AM on Tuesdays and Thursdays.")
    assert all(f.event_date is None for f in facts), \
        [(f.rel_type, f.object, f.event_date) for f in facts]


# ── NEGATIVE (clock-time-not-age): a clock time is NEVER read as an age scalar ─────────────────────
def test_clock_time_is_not_an_age():
    facts = _derive("I wake up at 6:45 AM on Tuesdays and Thursdays.")
    # no scalar should be the person-age rel, and no scalar value should be a bare "6" / "45".
    assert not any(r == "age" for (r, _v) in _scalars(facts)), _scalars(facts)
    assert not any(v.strip() in ("6", "45", "6:45") and r == "age"
                   for (r, v) in _scalars(facts)), _scalars(facts)


# ── DURATION FIREWALL: a plain measure verb ("takes 45 minutes") gets NO schedule-time scalar ──────
def test_duration_measure_untouched():
    facts = _derive("My commute takes 45 minutes.")
    sc = _scalars(facts)
    # the additive schedule-time scalar requires a CLOCK time (':') + a frame — neither is present.
    assert not any("_on_" in r or "_at_" in r for (r, _v) in sc), sc
    assert any("45 minutes" in v for (_r, v) in sc), sc


# ── FLAG OFF: byte-identical to the legacy two-scalar behavior ─────────────────────────────────────
def test_flag_off_is_byte_identical(monkeypatch):
    import importlib
    import src.extraction.linguistics as L
    monkeypatch.setenv("SPINE_TIME_OF_DAY_SCHEDULE", "false")
    importlib.reload(L)
    try:
        facts = list(L.derive_sentence_facts("I wake up at 6:45 AM on Tuesdays and Thursdays.",
                                             reference=REF))
        sc = [(f.rel_type, f.object) for f in facts if f.scalar_datatype == "string"]
        # OFF → NO frame-keyed schedule-time scalar; only the two legacy scalars.
        assert not any(r.startswith("wake_on_") for (r, _v) in sc), sc
        assert ("wake", "6:45 am") in sc, sc
    finally:
        monkeypatch.setenv("SPINE_TIME_OF_DAY_SCHEDULE", "true")
        importlib.reload(L)


# ── QUERY-SIDE SURFACING (verify-the-read): the day-scoped question surfaces the frame-keyed value ─
_ANCHOR = "11111111-1111-1111-1111-111111111111"


class _FakeCur:
    """Minimal SQL-pattern cursor: returns the anchor's entity_attributes for the ATTRSCOPE read."""

    def __init__(self, attr_rows):
        self._attr_rows = attr_rows
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        import re as _re
        s = _re.sub(r"\s+", " ", sql).strip().lower()
        if "from entity_attributes ea" in s and "left join rel_types" in s:
            self._rows = list(self._attr_rows)
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, attr_rows):
        self._attr_rows = attr_rows

    def cursor(self):
        return _FakeCur(self._attr_rows)


def test_day_scoped_query_grounds_frame_keyed_scalar():
    from src.api.main import determine_path
    # the anchor holds the uncollided frame-keyed scalar the fix emits (no natural_language template).
    path = determine_path("what time do I wake up on Tuesdays and Thursdays",
                          _FakeDB([("wake_on_tuesdays_and_thursdays", None), ("wake", "x is y")]),
                          user_id=None, anchor_resolved_uuid=_ANCHOR)
    admitted = [r.lower() for r in path.scalar_rels]
    # ATTRSCOPE tokenises "wake_on_tuesdays_and_thursdays" → {wake, tuesdays, thursdays}; the query
    # words ground it (aspect-precision, not firehose).
    assert "wake_on_tuesdays_and_thursdays" in admitted, admitted
    assert path.scope_active is True


def test_prose_renders_frame_keyed_clock_value():
    from src.api.main import convert_to_prose

    class _NullDB:
        def cursor(self):
            return _FakeCur([])

    fact = {
        "subject": _ANCHOR, "_subject_id": _ANCHOR,
        "rel_type": "wake_on_tuesdays_and_thursdays", "object": "6:45 am",
        "fact_class": "A", "source": "attributes", "confidence": 1.0, "category": None,
    }
    prose = convert_to_prose([fact], _NullDB(), anchor=_ANCHOR, user_id=_ANCHOR)
    assert any("6:45" in p for p in prose), prose


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
