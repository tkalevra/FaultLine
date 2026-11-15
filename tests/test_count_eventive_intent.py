"""COUNT-INTENT Shape C — the EVENTIVE / PASSIVE THIRD-PERSON cardinality (LME qid 2e6d26dc).

"How many babies were born to friends and family members in the last few months?" (gold=5) is a
cardinality of a TYPE that is the clause SUBJECT of a passive/eventive predicate ("babies WERE BORN"),
NOT a 1st/2nd-person possession ("how many X do I have"). ``_detect_count_intent`` only had Shape A
(possession: aux + i/you/we) and Shape B (membership: "on/in my/your/the <list>"), so it returned {}
for this question → no count path → the product abstained despite 5 clean instance_of baby entities.

The fix adds Shape C ("how many <TYPE> (were|was|are|is|have|had|got) <predicate>"), ORDERED BEFORE
Shape B so a trailing temporal adjunct ("… in the last few months") does not fire Shape B's "in the
<LIST>" pattern and swallow the whole predicate into the type phrase. This test FAILS on the old code
(returns {} / a Shape-B mis-parse) and PASSES on the fix (type_phrase == "babies"), and pins the
non-regression of Shapes A/B and the duration decline.
"""

import pytest

from src.api.main import _detect_count_intent


def test_eventive_passive_cardinality_yields_bare_type():
    # THE FIX (fail-on-old): the actual gold-bearing question resolves to the bare TYPE "babies".
    q = "How many babies were born to friends and family members in the last few months?"
    res = _detect_count_intent(q)
    assert res.get("type_phrase") == "babies", (
        f"eventive cardinality mis-parsed (old code returns {{}} or a Shape-B swallow): {res!r}"
    )
    # "in the last few months" is a FUZZY boundary, NOT an explicit 'since <date>' filter.
    assert not res.get("since"), f"temporal adjunct wrongly captured as a since-bound: {res!r}"


@pytest.mark.parametrize("q,expected", [
    ("How many babies were born?", "babies"),
    ("how many houses were sold last year", "houses"),
    ("how many players are injured", "players"),
])
def test_other_eventive_cardinalities(q, expected):
    assert _detect_count_intent(q).get("type_phrase") == expected, _detect_count_intent(q)


# ── NON-REGRESSION: Shapes A/B and the duration decline are untouched ───────────────────────────
@pytest.mark.parametrize("q,expected", [
    ("how many tanks do I have", "tanks"),                 # Shape A (possession)
    ("how many stories have I written", "stories"),        # Shape A
    ("how many songs on my playlist", "songs"),            # Shape B (membership, no aux)
])
def test_possession_and_membership_shapes_unchanged(q, expected):
    assert _detect_count_intent(q).get("type_phrase") == expected, _detect_count_intent(q)


def test_duration_still_declined():
    # temporal-duration units stay the temporal-calc layer's, never a cardinality
    assert _detect_count_intent("how many days between the trips") == {}
