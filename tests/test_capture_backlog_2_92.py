"""Lane I — #92: bare-number values are kept.

"My PIN is 1234." — spaCy tags the acronym PROPN, so the attribute-scalar frame (NOUN subject)
declined and the value was lost; "I scored 1999 points." — the counted NP parsed npadvmod, outside
the eventive count lane, and the year-shaped cardinal was peeled as a date. Fresh-domain controls:
CVV, steps.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from tests.laneb_e2e_support import (  # noqa: E402
    e2e_fixture, module_flags_fixture, state,
)

_flags = module_flags_fixture()
e2e_92 = e2e_fixture()


def _derive(s):
    import datetime
    import src.extraction.linguistics as L
    return [(f.subject, f.rel_type, f.object)
            for f in (L.derive_sentence_facts(s, reference=datetime.datetime(2026, 10, 1)) or [])]


def _prose(env, q):
    r = env["client"].post("/query", json={"text": q, "user_id": env["user_id"]}).json()
    return " / ".join(str(f.get("definition") or "") for f in (r.get("facts") or [])).lower()


@pytest.mark.parametrize("sent,value", [
    ("My PIN is 1234.", "1234"),
    ("My CVV is 1987.", "1987"),                       # fresh domain
    ("I scored 1999 points.", "1999"),
    ("I walked 2000 steps.", "2000"),                  # fresh domain
])
def test_92_bare_number_value_kept(sent, value):
    fs = _derive(sent)
    assert any(o == value for s, r, o in fs), fs


def test_92_year_under_time_prep_stays_a_date():
    assert not any(o == "2019" for s, r, o in _derive("I moved to Oslo in 2019."))


def test_92_e2e_values_answer(e2e_92):
    state(e2e_92, "My PIN is 1234.")
    assert "1234" in _prose(e2e_92, "What is my PIN?")
    state(e2e_92, "I scored 1999 points.")
    assert "1999" in _prose(e2e_92, "How many points do I have?")
