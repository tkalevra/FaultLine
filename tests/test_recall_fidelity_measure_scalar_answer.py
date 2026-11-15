"""P3 RECALL-FIDELITY LANE - recall renders VALUES, not row counts.

Measured wounds (benchmarks/walk_litmus, quantity family 0.00 / temporal 0.00):
  - "How many laps did I say I did at swimming?" rendered the ROW COUNT ("You have 1
    laps on record") while the real value do_laps=19 sat in entity_attributes - a
    verb-prefixed MEASURE filing that the head-noun consult was only allowed to see on
    a 0/miss instance walk, and a phantom instance row had made the walk report 1.
  - "How many kilometers did I say I ran?" answered "I don't have any kilometers on
    record" over the stored value run='13 kilometers' - the value filed under the
    ACTIVITY noun, its unit noun nowhere in the attribute NAME.

Fix (deterministic, in _count_scalar_answer + the count render):
  - UNIT-IN-VALUE admit: when no head-noun match exists, a stored value TEXT carrying a
    numeral AND the queried head noun (word-boundary) IS the answer; more than one
    distinct matching attribute declines; non-cardinal numerals decline.
  - MEASURE-SCALAR OVERRIDE (COUNT_SCALAR_MEASURE_OVERRIDE, default on): on a NON-zero
    instance count, a unique scalar whose attribute is of MEASURE SHAPE (head-matches
    the type phrase but carries MORE tokens - do_laps for laps) outranks the row count.
    Bare-noun attributes (cats for cats) keep the instance-first contract.
  - RENDER: a unit-bearing surface is voiced verbatim ("You have 13 kilometers on
    record.") - never noun-doubled.

fail-on-old: the consult fires only on 0/miss, the unit admit and the shape discriminator
do not exist, and the render doubles the noun for a unit-bearing surface.
"""
import datetime

import pytest


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCur(self._rows)


def _dt(y, mo, d):
    return datetime.datetime(y, mo, d)


def test_measure_shape_discriminator():
    import src.api.main as main
    assert main._count_scalar_attr_is_measure("do_laps", "laps") is True
    assert main._count_scalar_attr_is_measure("instagram followers", "followers") is True
    # bare-noun possession shape keeps the instance-first contract
    assert main._count_scalar_attr_is_measure("cats", "cats") is False
    assert main._count_scalar_attr_is_measure("followers", "followers") is False
    # head mismatch is not a measure of this phrase
    assert main._count_scalar_attr_is_measure("run", "kilometers") is False
    assert main._count_scalar_attr_is_measure("", "laps") is False


def test_unit_in_value_admit():
    import src.api.main as main
    db = _FakeDB([("run", None, "13 kilometers", _dt(2023, 5, 1))])
    r = main._count_scalar_answer(db, "u1", "kilometers")
    assert r and r["count"] == 13 and r["attribute"] == "run", r
    assert r.get("value_bears_unit") is True and r.get("surface") == "13 kilometers", r


def test_unit_in_value_declines_ambiguous():
    import src.api.main as main
    db = _FakeDB([("run", None, "13 kilometers", _dt(2023, 5, 1)),
                  ("swim", None, "2 kilometers", _dt(2023, 5, 2))])
    assert main._count_scalar_answer(db, "u1", "kilometers") is None


def test_unit_in_value_declines_fraction():
    import src.api.main as main
    db = _FakeDB([("run", None, "13.5 kilometers", _dt(2023, 5, 1))])
    assert main._count_scalar_answer(db, "u1", "kilometers") is None


def test_unit_in_value_requires_unit_in_value_text():
    import src.api.main as main
    db = _FakeDB([("run", None, "this morning", _dt(2023, 5, 1))])
    assert main._count_scalar_answer(db, "u1", "kilometers") is None


def test_head_match_contract_unchanged():
    # existing recency-scalar contract still holds beside the new admit
    import src.api.main as main
    db = _FakeDB([("cats", 3, None, _dt(2023, 5, 2))])
    r = main._count_scalar_answer(db, "u1", "cats")
    assert r and r["count"] == 3 and r["attribute"] == "cats", r


def test_zero_miss_consult_branch_propagates_value_bears_unit():
    # CRITIC REGRESSION PIN (2026-11-06): the UNIT-IN-VALUE admit fires in _count_scalar_answer,
    # but the RENDER guard (_cnt_res.get("value_bears_unit")) can only see it if the CALL SITE
    # carries the flag. The admit reaches recall through the 0/miss consult branch of
    # _query_impl (the walk found no instances) - the inline branch is not unit-callable, so
    # this pin asserts its source shape: the consult-branch _cres dict must copy
    # value_bears_unit from _sc, exactly as the elif override branch already did. Measured
    # defect when absent (live, bench seat, deployed HEAD): "How many kilometers did I say I
    # ran?" over run='13 kilometers' rendered "You have 13 kilometers kilometers on record."
    # (noun doubled by the generic render) while the bench scorer's substring match masked it.
    import inspect
    import src.api.main as main
    src = inspect.getsource(main)
    # locate the consult branch and its _cres construction
    branch_at = src.index('if _cres.get("miss") or int(_cres.get("count") or 0) == 0:')
    window = src[branch_at:branch_at + 1400]
    assert 'from_scalar_attr": _sc["attribute"]' in window, window[:400]
    assert '"value_bears_unit": bool(_sc.get("value_bears_unit"))' in window, (
        "the 0/miss consult branch must propagate value_bears_unit - without it the "
        "unit-in-value admit renders the noun doubled (q016/q018 defect)")
    # and the elif override branch keeps its own copy (the original fix)
    assert '"value_bears_unit": bool(_mo.get("value_bears_unit"))' in src, src
    # the admit itself still carries the flag
    db = _FakeDB([("run", None, "13 kilometers", _dt(2023, 5, 1))])
    r = main._count_scalar_answer(db, "u1", "kilometers")
    assert r.get("value_bears_unit") is True and r.get("surface") == "13 kilometers", r
