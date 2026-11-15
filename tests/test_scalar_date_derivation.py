"""Focused unit tests for the recall EXPOSE + CALC seams (query path, src/api/main.py).

Two reported bugs, two sides of scalar SCOPING:
  1. UNDER-EXPOSE + NO CALC — an interrogative date/scalar question must reach the
     entity's scalar AND derive a value (birthdate → whole-years AGE).
  2. (scope-leak is a DB-backed lane; covered by live-proof, not here.)

These are PURE unit tests (no DB): the date-arith derivation + the interrogative
decomposition are deterministic string/date functions. Subject-agnostic: keyed on the
VALUE being a date and on query SYNTAX, never on an attribute-name / domain word-list.
"""

from datetime import date

import pytest

import src.api.main as main


# --------------------------------------------------------------------------- #
# _scalar_value_as_date — STRICT date parse (no dateparser greediness).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("2015-06-12", date(2015, 6, 12)),
    ("1958-03-05", date(1958, 3, 5)),
    ("2020-04-03T00:00:00+00", date(2020, 4, 3)),
    ("2020-04-03 00:00:00", date(2020, 4, 3)),
    ("2020", date(2020, 1, 1)),          # bare year → Jan 1
    (" 1999 ", date(1999, 1, 1)),
])
def test_scalar_value_as_date_accepts_dates(value, expected):
    assert main._scalar_value_as_date(value) == expected


@pytest.mark.parametrize("value", [
    None,
    "",
    "192.168.1.50",    # an IP must NEVER read as a date
    "40 tb",           # a storage size
    "72 bpm",          # a heart rate
    "metformin",
    "123 maple street in guelph",
    "2015-13-40",      # impossible date
    "42",              # too-short number (not a 4-digit year)
])
def test_scalar_value_as_date_rejects_non_dates(value):
    assert main._scalar_value_as_date(value) is None


# --------------------------------------------------------------------------- #
# _whole_years_since — completed-year age via dateutil.relativedelta.
# --------------------------------------------------------------------------- #

def test_whole_years_completed_year_boundary():
    b = date(2015, 6, 12)
    # birthday NOT yet reached this year → floor to 10
    assert main._whole_years_since(b, ref=date(2026, 6, 11)) == 10
    # birthday reached → 11
    assert main._whole_years_since(b, ref=date(2026, 6, 12)) == 11
    assert main._whole_years_since(b, ref=date(2026, 7, 3)) == 11


def test_whole_years_future_date_returns_none():
    # a future date must NOT fabricate a negative age
    assert main._whole_years_since(date(2030, 1, 1), ref=date(2026, 7, 3)) is None


def test_derive_age_from_value_end_to_end():
    # non-date → None (no fabrication)
    assert main._derive_age_from_value("40 tb") is None
    assert main._derive_age_from_value("192.168.1.50") is None
    # a date → a non-negative int
    y = main._derive_age_from_value("2020-04-03")
    assert isinstance(y, int) and y >= 0


# --------------------------------------------------------------------------- #
# _annotate_date_derivation — render-time "(age N)" append, universal.
# --------------------------------------------------------------------------- #

def test_annotate_appends_age_for_date_scalar():
    fact = {"source": "attributes", "object": "2020-04-03", "rel_type": "born_in"}
    out = main._annotate_date_derivation(fact, "Fraggle was born in 2020-04-03")
    assert out.startswith("Fraggle was born in 2020-04-03 (age ")
    assert out.endswith(")")


def test_annotate_noop_for_non_date_scalar():
    fact = {"source": "attributes", "object": "40 tb", "rel_type": "storage"}
    prose = "Nas has 40 tb of storage"
    assert main._annotate_date_derivation(fact, prose) == prose


def test_annotate_noop_for_non_attributes_source():
    # a relational fact (source db) whose object happens to be a UUID/date string
    fact = {"source": "db", "object": "2020-04-03", "rel_type": "related_to"}
    prose = "X is related to Y"
    assert main._annotate_date_derivation(fact, prose) == prose


def test_annotate_idempotent():
    fact = {"source": "attributes", "object": "2020-04-03", "rel_type": "born_in"}
    once = main._annotate_date_derivation(fact, "born in 2020-04-03")
    twice = main._annotate_date_derivation(fact, once)
    assert once == twice  # already carries "(age …)" → not doubled


# --------------------------------------------------------------------------- #
# _decompose_query_steps — interrogative exposure (when/where stripping).
# --------------------------------------------------------------------------- #

def test_decompose_when_is_named_entity_birthday():
    # BUG 1a: "when is X's birthday" must decompose to [X, birthday], not leave
    # "when"/"is" as junk owner steps (which made the walk defer → recall miss).
    assert main._decompose_query_steps("When is Fraggle's birthday?") == ["fraggle", "birthday"]


def test_decompose_when_is_possessive_chain():
    assert main._decompose_query_steps("When is my mother's birthday?") == ["my", "mother", "birthday"]


def test_decompose_how_old_named_entity():
    assert main._decompose_query_steps("How old is Cyrus?") == ["cyrus", "age"]


def test_decompose_where_is_scalar():
    assert main._decompose_query_steps("Where is my server's ip?") == ["my", "server", "ip"]


def test_decompose_plain_non_chain_defers():
    # a bare non-possessive query is NOT a decomposable chain
    assert main._decompose_query_steps("what is the weather") is None
