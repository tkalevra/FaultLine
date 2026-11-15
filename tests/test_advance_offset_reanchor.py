"""Regression — CHAINED-RELATIVE "N units in advance" event-date re-anchoring (LME 982b5123).

THE BUG: "How many months ago did I book the Airbnb in San Francisco?" (gold "Five months ago",
asked 2023-05-27). The user stated the SF trip was "two months ago" (→ 2023-03-27) and that the
Airbnb was booked "three months in advance" [of that trip]. So booking = 2023-03-27 − 3mo =
2022-12-27 → FIVE months before the question. But spaCy DATE-NER tags the bare "three months" and
dateparser resolves it as "three months AGO" → (reference − 3mo) = 2023-02-27, so the deterministic
elapsed walk (which is CORRECT — it just reads event_date) rendered "about 3 months ago". The defect
is at INGEST: "in advance" is a PRE-EVENT offset (the booking precedes an anchor event), not "ago".

THE FIX (deterministic, subject-agnostic, strong-ingest/lean-query): at ingest, when the clause
carries the grammatical "N units in advance/beforehand/ahead of" construction, re-anchor the poisoned
event_date to (nearest-prior-dated-event − N). TimeML/TIMEX3 RI-TIMEX anchoring to the nearest
discourse event (Pustejovsky et al. 2003; PMC4986666). Query is UNCHANGED — the elapsed op reads the
now-correct event_date and renders "5 months ago".

These tests FAIL on the pre-fix code (no ``_reanchor_advance_offset_dates`` / ``parse_advance_offset``)
and PASS on the fix. Pin #2 (query render) is the required months-ago-from-reference case.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest


# ── Pin 1: the grammatical parser (pure, no DB) ──────────────────────────────────────────────
def test_parse_advance_offset_grammar():
    from src.temporal import apply_advance_offset, parse_advance_offset

    assert parse_advance_offset("had to book three months in advance") == (3, "months")
    assert parse_advance_offset("I booked my flight two weeks in advance") == (2, "weeks")
    assert parse_advance_offset("I paid a month beforehand") == (1, "months") \
        or parse_advance_offset("I paid a month beforehand") == (1, "month")
    assert parse_advance_offset("reserved 6 days ahead of the concert") == (6, "days")
    assert parse_advance_offset("I planned it a year in advance of the wedding") == (1, "year")
    # MUST NOT match a plain "ago" (past-from-now) or a bare/ambiguous "ahead".
    assert parse_advance_offset("I saw him three months ago") is None
    assert parse_advance_offset("we are three years ahead") is None
    assert parse_advance_offset("I like pizza") is None

    # apply = anchor − N (calendar-exact).
    assert apply_advance_offset(date(2023, 3, 27), 3, "months") == date(2022, 12, 27)
    assert apply_advance_offset(date(2023, 5, 27), 3, "months") == date(2023, 2, 27)  # the poison


# ── Pin 2: end-to-end INGEST re-anchor over the REAL deriver ─────────────────────────────────
def test_reanchor_corrects_in_advance_booking(monkeypatch):
    import src.api.main as m

    ref = datetime(2023, 5, 27, tzinfo=timezone.utc)
    booking = ("I've had a great experience with Airbnb in the past, like when I stayed in "
               "Haight-Ashbury for my best friend's wedding and had to book three months in advance.")

    # The REAL deterministic deriver mis-dates the booking to the NAIVE "3 months ago" poison.
    edges = m._detect_event_states_reified(booking, ref)
    dated = [e for e in edges if e.get("event_date")]
    assert dated, "deriver produced no dated occurrence for the booking clause"
    assert any(str(e["event_date"])[:10] == "2023-02-27" for e in dated), \
        "expected the naive (reference − 3mo) poison date on the occurrence"

    # The user's nearest prior dated event is the SF trip @ 2023-03-27 (stated "two months ago").
    monkeypatch.setattr(m, "_nearest_prior_dated_event_db", lambda uid, rd: date(2023, 3, 27))

    out = m._reanchor_advance_offset_dates(edges, booking, ref, "seat-test")
    out_dated = [str(e["event_date"])[:10] for e in out if e.get("event_date")]
    assert out_dated, "re-anchor dropped the dated edges"
    # 2023-03-27 − 3 months = 2022-12-27  (→ FIVE months before the 2023-05-27 question).
    assert all(d == "2022-12-27" for d in out_dated), out_dated
    assert not any(d == "2023-02-27" for d in out_dated), "poison date survived the re-anchor"


def test_reanchor_is_noop_without_advance_construction(monkeypatch):
    import src.api.main as m

    ref = datetime(2023, 5, 27, tzinfo=timezone.utc)
    # A plain dated occurrence with NO "in advance" construction must be left untouched.
    edges = [{"subject": "user", "rel_type": "participated_in", "object": "airbnb",
              "event_date": "2023-02-27"}]
    called = {"db": False}

    def _spy(uid, rd):
        called["db"] = True
        return date(2023, 3, 27)

    monkeypatch.setattr(m, "_nearest_prior_dated_event_db", _spy)
    out = m._reanchor_advance_offset_dates(edges, "I went to the airbnb on Feb 27", ref, "seat-test")
    assert out[0]["event_date"] == "2023-02-27"
    assert called["db"] is False, "must not even hit the DB without the advance construction"


def test_reanchor_flag_off_is_byte_identical(monkeypatch):
    import src.api.main as m

    ref = datetime(2023, 5, 27, tzinfo=timezone.utc)
    booking = "had to book three months in advance"
    edges = [{"subject": "user", "rel_type": "participated_in", "object": "airbnb",
              "event_date": "2023-02-27"}]
    monkeypatch.setattr(m, "_nearest_prior_dated_event_db", lambda uid, rd: date(2023, 3, 27))
    monkeypatch.setenv("TEMPORAL_ADVANCE_OFFSET", "false")
    out = m._reanchor_advance_offset_dates(edges, booking, ref, "seat-test")
    assert out[0]["event_date"] == "2023-02-27", "flag OFF must leave the legacy poison date"


# ── Pin 3: the (UNCHANGED) query elapsed walk renders the right MONTHS-AGO-FROM-REFERENCE ─────
def test_query_renders_months_ago_from_reference():
    import src.api.main as m

    q = "[Date: 2023/05/27 (Sat) 01:55] How many months ago did I book the Airbnb in San Francisco?"
    intent = m._detect_temporal_calc_intent(q.split("]", 1)[1].strip().lower())
    assert intent.get("op") == "elapsed"

    def render(event_date_iso: str):
        facts = [{"subject": "user", "object": "airbnb", "rel_type": "participated_in",
                  "event_date": event_date_iso, "event_date_granularity": "day"}]
        res = m._apply_temporal_calc(facts, {"calc": intent}, "seat-test", query_text=q)
        return (res or {}).get("answer")

    # Fixed capture (2022-12-27) → the GOLD "five months ago".
    assert "5 months" in render("2022-12-27")
    # The pre-fix poison (2023-02-27) mis-renders as "3 months ago" — the observed miss.
    assert "3 months" in render("2023-02-27")


# ── Pin 4: the DB anchor lookup executes real SQL and picks the nearest PRIOR dated event ─────
@pytest.mark.skipif(not __import__("os").environ.get("ADV_TEST_DSN"),
                    reason="ADV_TEST_DSN not set (throwaway Postgres for the DB-real anchor test)")
def test_nearest_prior_dated_event_db_real(monkeypatch):
    import os

    import psycopg2

    import src.api.main as m

    dsn = os.environ["ADV_TEST_DSN"]
    schema = "faultline_advtest"
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}; "
                        f"SET search_path TO {schema};")
            cur.execute("CREATE TABLE facts (event_date timestamptz, superseded_at timestamptz, "
                        "archived_at timestamptz, deleted_at timestamptz);")
            cur.execute("CREATE TABLE staged_facts (event_date timestamptz, deleted_at timestamptz);")
            # a distractor past event, the real anchor, and a FUTURE event (must be excluded by <= ref)
            cur.execute("INSERT INTO facts(event_date) VALUES "
                        "('2022-01-01'),('2023-03-27'),('2023-08-01');")
            cur.execute("INSERT INTO staged_facts(event_date) VALUES ('2021-06-06');")
        conn.commit()

    monkeypatch.setenv("POSTGRES_DSN", dsn)
    monkeypatch.setattr(m, "get_user_schema_context", lambda uid, conn: {"schema_name": schema})
    got = m._nearest_prior_dated_event_db("seat-test", date(2023, 5, 27))
    assert got == date(2023, 3, 27), got
