"""Issue #79 — ported spine pins red on saas: the two real deriver defects, pinned on PRODUCTION flags.

1. SOURCE→GOAL leak. "I flew from NYC to LA." folds the GOAL prep onto the predicate (fly_to la) and,
   by the G7 design, leaves the SOURCE on the residue path. Two later lanes re-emitted the source
   anyway: the SVO named-place seam stacked its prep onto the goal-folded predicate
   (user, fly_to_from, nyc), and under SPINE_PA_CORE the PA bridge filed (user, fly_from, nyc).
   Fix: linguistics.derive_sentence_facts — both lanes skip a PP that is not the clause's goal when
   _source_goal_change_goal_prep fires (token identity, no word list).

2. TEMPORAL fold. "She wrote a book in 2019." filed (book, write, "2019"): the verb-measure
   price-of-object branch took the bare year pobj as the book's price; with that gone, the PA bridge
   filed (she, write_in, "2019"). A date is never an object. Fix: a bare-numeral pobj/arg the
   DATE/TIME NER spans is skipped in both; a peeled date token is skipped in the PA bridge.

Pure deriver pins (no DB). Every fix carries a fresh-domain control.
"""
import datetime

import pytest

from src.extraction import linguistics as L

pytestmark = [
    pytest.mark.skipif(not L.linguistics_available(), reason="spaCy model unavailable"),
    pytest.mark.usefixtures("production_flags", "no_tenant_db"),
]

_REF = datetime.date(2023, 6, 1)


def _t(sentence, ref=None):
    return [(f.subject, f.rel_type, (f.object or "").strip().lower())
            for f in L.derive_sentence_facts(sentence, ref)]


@pytest.mark.parametrize("ref", [None, _REF])
@pytest.mark.parametrize("sentence,verb,goal,source", [
    ("I flew from NYC to LA.", "fly", "la", "nyc"),
    ("I switched from Python to Rust.", "switch", "rust", "python"),
    # fresh domain: a transfer between institutions — no fixture shares it
    ("I transferred from Waterloo to McMaster.", "transfer", "mcmaster", "waterloo"),
])
def test_source_goal_files_the_goal_only(ref, sentence, verb, goal, source):
    t = _t(sentence, ref)
    assert ("user", f"{verb}_to", goal) in t, t
    assert not any(o == source for (_s, _r, o) in t), t
    assert not any(r.endswith("_from") or "_to_" in r for (_s, r, _o) in t), t


def test_single_source_pp_still_captured():
    # control: with no goal PP there is no source→goal pairing — the source place still lands.
    t = _t("I bought a laptop from Best Buy.")
    assert any("best buy" in o for (_s, _r, o) in t), t


@pytest.mark.parametrize("ref", [None, _REF])
@pytest.mark.parametrize("sentence", [
    "She wrote a book in 2019.",
    "The team shipped the release in 2021.",   # fresh domain
])
def test_year_pobj_is_never_an_object(ref, sentence):
    t = _t(sentence, ref)
    year = sentence.rstrip(".").split()[-1]
    assert not any(year in o for (_s, _r, o) in t), t


@pytest.mark.parametrize("sentence,value", [
    ("I bought a handbag for 800 dollars.", "800 dollars"),   # a price pobj is still a measure
    ("I waited for 20 minutes.", "20 minutes"),               # a unit-bearing duration too
])
def test_measure_pobj_still_captured(sentence, value):
    t = _t(sentence)
    assert any(o == value for (_s, _r, o) in t), t


def _t5(sentence, ref=None):
    return [(f.subject, f.rel_type, (f.object or "").strip().lower(),
             getattr(f, "event_date", None), getattr(f, "temporal_status", None))
            for f in L.derive_sentence_facts(sentence, ref)]


# ── #89: only the SOURCE "from" PP leaves the clause; every other adjunct keeps its edge ──────────
@pytest.mark.parametrize("ref", [None, _REF])
@pytest.mark.parametrize("sentence,keep", [
    ("I flew from NYC to LA with Delta.", "delta"),
    ("I switched from Python to Rust at Google.", "google"),
    ("I relocated from Boston to Denver for a job at Google.", "job"),
])
def test_source_skip_keeps_every_other_adjunct(ref, sentence, keep):
    t = _t(sentence, ref)
    assert any(o == keep for (_s, _r, o) in t), t
    assert not any(r.endswith("_from") or "_to_from" in r for (_s, r, _o) in t), t


@pytest.mark.parametrize("sentence", [
    "I flew to LA with Delta.",            # control: no source PP → unchanged
])
def test_no_source_clause_unchanged(sentence):
    assert any(o == "delta" for (_s, _r, o) in _t(sentence)), _t(sentence)


@pytest.mark.parametrize("sentence,new,old", [
    ("I moved from Toronto to Ottawa.", "ottawa", "toronto"),
    ("I moved to Ottawa from Toronto.", "ottawa", "toronto"),
    ("I moved from Halifax to Regina.", "regina", "halifax"),     # fresh domain
])
def test_relocation_keeps_the_prior_residence_as_past(sentence, new, old):
    t = _t5(sentence, _REF)
    assert ("user", "lives_in", new, None, "now") in t, t
    assert ("user", "lives_in", old, None, "past") in t, t


# ── #90: a bare year-shaped AMOUNT is an amount, with or without a reference date ─────────────────
@pytest.mark.parametrize("sentence,value", [
    ("I paid 1999.", "1999"),
    ("I sold the car for 2000.", "2000"),
    ("I bought the house for 1999.", "1999"),
    ("I sold it for 1990.", "1990"),
    ("I traded the bike for 2015.", "2015"),       # fresh domain
])
def test_year_shaped_amount_is_kept_and_never_dated(sentence, value):
    for ref in (None, _REF):
        t = _t5(sentence, ref)
        assert any(o == value for (_s, _r, o, _d, _ts) in t), (ref, t)
        assert not any(d for (_s, _r, _o, d, _ts) in t), (ref, t)   # no invented event_date


# M2 (bare-measure verb-PP, no dobj): "retired IN 2019" is a time position — no measure value,
# with or without a reference (with one, the year is the event_date). Intended: before the fix the
# no-reference run filed (she, retire, '2019') while the reference run dated it.
@pytest.mark.parametrize("sentence,year", [
    ("She retired in 2019.", "2019"),
    ("The bridge opened in 1932.", "1932"),        # fresh domain
])
def test_time_position_year_is_never_a_measure(sentence, year):
    for ref in (None, _REF):
        t = _t5(sentence, ref)
        assert not any(o == year for (_s, _r, o, _d, _ts) in t), (ref, t)
    assert any(d and d.startswith(year) for (_s, _r, _o, d, _ts) in _t5(sentence, _REF))


# M3 (PA bridge, peeled date token): a peeled non-numeric date ("in March") is never an argument.
def test_peeled_month_is_never_a_pa_argument():
    t = _t5("She painted the fence in March.", _REF)
    assert not any(o == "march" for (_s, _r, o, _d, _ts) in t), t
