"""A bare ladder rung is scaffolding, not an answer.

`X is a subclass of Y` / `X is an instance of Y` between type nodes is the structure a
walk CLIMBS to reach the members underneath. It is never something a person said about
their own life, so it cannot answer a question about them.

These tests drive the REAL `_query_impl` against a seeded seat, because the defect they
pin is invisible to a unit test of the predicate: a fresh-context critic previously broke
a fix whose 12 tests were all green, precisely because none of them called the entrypoint.

Gated on LADDER_TEST_DSN + LADDER_TEST_SEAT so CI without a database skips rather than
lying. Point them at a bench that carries real stated facts.
"""
import os
import pytest

DSN = os.environ.get("LADDER_TEST_DSN")
SEAT = os.environ.get("LADDER_TEST_SEAT")
WORK_SEAT = os.environ.get("LADDER_TEST_WORK_SEAT")

_gate = pytest.mark.skipif(
    not (DSN and SEAT),
    reason="set LADDER_TEST_DSN and LADDER_TEST_SEAT to a bench seat to run these",
)

_LADDER = ("subclass_of", "instance_of")


def _ask(uid, text):
    # FORCE, never setdefault: with setdefault an already-set POSTGRES_DSN wins and the
    # test silently runs against a different database than LADDER_TEST_DSN names. Seat
    # 3f9c21ae holds 69 facts locally and 862 on production — same id, different truth.
    os.environ["POSTGRES_DSN"] = DSN or ""
    os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
    from src.api.models import QueryRequest
    from src.api import main as M
    return (M._query_impl(QueryRequest(text=text, user_id=uid, source="test")).facts or [])


@_gate
def test_a_family_answer_carries_no_engine_grown_type_assertions():
    facts = _ask(SEAT, "tell me about my family?")
    assert facts, "the seat has stated family facts; an empty answer is a fail"
    grown = [f for f in facts if f.get("_hierarchy_admitted")]
    assert not grown, f"engine-grown rows reached a family answer: {grown}"


@_gate
def test_a_grouping_word_does_not_answer_a_question_the_seat_cannot_answer():
    """The seat has family facts and no car. The honest answer is nothing."""
    assert _ask(SEAT, "what car does my family drive?") == []


@_gate
@pytest.mark.skipif(not WORK_SEAT, reason="set LADDER_TEST_WORK_SEAT")
def test_a_ladder_rung_is_never_spoken_even_when_it_is_all_that_survived():
    """Before the fix this returned exactly one row: 'Work is a subclass of concept'.

    An answer made only of scaffolding is strictly worse than no answer, because the
    caller cannot tell the two apart.
    """
    facts = _ask(WORK_SEAT, "who am I and where do I work?")
    rungs = [f for f in facts
             if f.get("_hierarchy_admitted") and f.get("rel_type") in _LADDER]
    assert not rungs, f"a bare ladder rung was spoken as an answer: {rungs}"


@_gate
@pytest.mark.skipif(not WORK_SEAT, reason="set LADDER_TEST_WORK_SEAT")
def test_suppressing_rungs_does_not_suppress_the_real_answer():
    """The precision direction: dropping scaffolding must not drop attested facts."""
    facts = _ask(WORK_SEAT, "what do I do for work?")
    assert facts, "real participation facts must survive the rung suppression"
    rungs = [f for f in facts
             if f.get("_hierarchy_admitted") and f.get("rel_type") in _LADDER]
    assert not rungs


@_gate
@pytest.mark.skipif(not os.environ.get("LADDER_TEST_STATED_SEAT"),
                    reason="set LADDER_TEST_STATED_SEAT to a seat whose ladder row is user_stated")
def test_a_user_stated_type_fact_is_never_deleted_as_scaffolding():
    """THE regression a critic found: the gate first keyed on `_hierarchy_admitted`, which
    marks REACHABILITY, not authorship. On a seat whose only rows are a user-stated
    `instance_of` and one unrelated fact, "what is my family?" returned NOTHING.

    Deleting a person's own words is strictly worse than showing one line of scaffolding,
    so this asserts the safe direction explicitly.
    """
    facts = _ask(os.environ["LADDER_TEST_STATED_SEAT"], "what is my family?")
    assert facts, "a user-stated instance_of was deleted as if it were engine scaffolding"
    assert any(f.get("rel_type") in _LADDER for f in facts), \
        f"the stated type fact did not survive: {facts}"
