"""Regression gate for THREE deterministic SPINE-deriver capture bugs (``derive_sentence_facts``,
src/extraction/linguistics.py). All inputs are SUBJECT-AGNOSTIC and use NON-personal reference data;
every case is a CLASS-LEVEL mechanism proven with surface values DIFFERENT from the repro string, so a
point-patch that only fixes the one example would still fail here.

── BUG 1 — MULTI-TOKEN SCALAR VALUE TRUNCATION ─────────────────────────────────────────────────
"My blood type is O negative" captured only the ADJ head ("negative"), TRUNCATING the compound
nominal value. FIX (general): the copula-state value build now rebuilds the CONTIGUOUS left
name-like premodifier run (PROPN/PRON/NUM/SYM/X ``compound``/``advmod`` children) + the complement
head, VERBATIM — so ANY compound scalar value survives whole (blood group, a model code, …), never a
value word-list. A genuine single-adjective state ("down"/"idle") has no such run → unchanged.

── BUG 2 — BARE YEAR MIS-ROUTED / DROPPED ──────────────────────────────────────────────────────
"I was diagnosed with diabetes in 2019" lost the year: the dateparser RELATIVE_BASE was a plain
``date``, which dateparser>=1.2 REJECTS ("must be datetime, not date") → every span parse threw and
the year never reached ``event_date`` (leaving a bare NUM free to be mis-read as ``age``). FIX
(general, temporal-layer): coerce the reference to a ``datetime`` (``_as_datetime_ref``) so the
existing spaCy-DATE-NER + dateparser machinery resolves the year to ``event_date``; the pre-existing
copula-measure YEAR guard keeps a 4-digit calendar year out of ``age``. A genuine small NUM age
("Sarah is 28") is untouched (not a DATE ent, not a plausible year).

── BUG 3 — KINSHIP DIRECTION + MULTI-TOKEN NAME MANGLE ─────────────────────────────────────────
"My son David Chen lives in Boston" bound only the last name token onto the ROLE noun. spaCy excludes
appositional modifiers from noun_chunks, so the name must be rebuilt off the ``appos`` PROPN + its
``compound`` run. FIX (general): the possessive-kinship emit AND the SVO subject resolution both
resolve a role noun renamed by an apposed proper name (``_appositive_proper_name``) to the FULL name
span, apply the kin DIRECTION from the ``kinship_noun`` cue-class rel-map (son→child_of), and collapse
the role noun. Works for any kin role + any multi-token name.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2023, 6, 1)
_REF_DT = datetime.datetime(2023, 6, 1)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


# ── BUG 1: multi-token scalar value captured WHOLE (any compound value, no word-list) ───────

@pytest.mark.parametrize("text,value", [
    ("My blood type is O negative.", "o negative"),
    ("My blood type is O positive.", "o positive"),
    ("My blood type is AB negative.", "ab negative"),   # DIFFERENT surface than the repro
    ("My blood type is AB positive.", "ab positive"),
])
def test_multitoken_scalar_value_not_truncated(text, value):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    # the FULL compound value is captured as an object somewhere (has_state OR a scalar rel) —
    # never truncated to the single ADJ head.
    objs = [o for _s, _r, o in triples]
    assert value in objs, triples
    # the truncated single-token value must NOT be the sole capture
    _head = value.split()[-1]
    assert not any(o == _head for o in objs), triples


def test_single_adjective_state_still_converges_to_lemma():
    # a genuine single-adjective state has no premodifier run → unchanged (lemma-converged node).
    triples = _triples(m.derive_sentence_facts("The printer is idle.", _REF))
    assert any(rel == m._STATE_REL and o == "idle" for _s, rel, o in triples), triples


# ── BUG 2: bare 4-digit year → event_date (temporal lane), NEVER age; small NUM age intact ──

@pytest.mark.parametrize("text,year", [
    ("I was diagnosed with diabetes in 2019.", "2019"),
    ("I was diagnosed 2005.", "2005"),               # DIFFERENT year + phrasing than the repro
    ("I graduated in 1998.", "1998"),                # different verb entirely (no medical literal)
])
def test_bare_year_routes_to_event_date_not_age(text, year):
    facts = m.derive_sentence_facts(text, _REF)
    triples = _triples(facts)
    # NO fact carries the year as an ``age`` scalar
    assert not any(rel == "age" and str(o).startswith(year[:2]) and str(o) == year
                   for _s, rel, o in triples), triples
    assert not any(rel == "age" for _s, rel, o in triples), triples
    # the year reached the temporal lane as an event_date (year granularity ⇒ ISO starts with the year)
    assert any(getattr(f, "event_date", None) and f.event_date.startswith(year) for f in facts), \
        [(t, getattr(f, "event_date", None)) for t, f in zip(triples, facts)]


def test_bare_year_works_with_date_reference_type():
    # the RELATIVE_BASE coercion bug: a plain ``datetime.date`` reference must still resolve the year.
    facts = m.derive_sentence_facts("I was diagnosed with diabetes in 2019.", _REF)
    assert any(getattr(f, "event_date", None) and f.event_date.startswith("2019") for f in facts), \
        [(f.subject, f.rel_type, f.object, getattr(f, "event_date", None)) for f in facts]


def test_datetime_reference_type_also_resolves_year():
    facts = m.derive_sentence_facts("I was diagnosed with diabetes in 2019.", _REF_DT)
    assert any(getattr(f, "event_date", None) and f.event_date.startswith("2019") for f in facts)


def test_small_num_age_is_not_treated_as_a_year():
    # a genuine 2-digit age must STILL be captured as ``age`` (the year guard must not over-reach).
    triples = _triples(m.derive_sentence_facts("Sarah is 28.", _REF))
    assert ("sarah", "age", "28") in triples, triples


# ── BUG 3: kinship apposition — full name span + correct direction + role collapse ──────────

@pytest.mark.parametrize("text,name,kin,verb_rel,place", [
    ("My son David Chen lives in Boston.", "david chen", "child_of", "live_in", "boston"),
    # DIFFERENT kin role + DIFFERENT multi-token name + DIFFERENT verb/place
    ("My daughter Sarah Jones lives in Toronto.", "sarah jones", "child_of", "live_in", "toronto"),
    # THREE-token name — proves the span rebuild is not a two-token point-patch
    ("My brother Michael Andrew Smith works in Chicago.", "michael andrew smith",
     "sibling_of", "work_in", "chicago"),
])
def test_kinship_apposition_full_name_correct_direction_role_collapsed(
        text, name, kin, verb_rel, place):
    triples = _triples(m.derive_sentence_facts(text, _REF))
    # (a) the FULL multi-token proper name binds as the person, correct kin DIRECTION (from cue-map)
    assert (name, kin, "user") in triples, triples
    # (b) the verb predicate lands on the SAME named instance (not the role noun)
    assert (name, verb_rel, place) in triples, triples
    # (c) the role noun is COLLAPSED — never a standalone subject/object entity
    _role = text.split()[1].lower()   # "son"/"daughter"/"brother"
    for _s, _r, _o in triples:
        assert _s != _role and _o != _role, triples
    # (d) the name was NOT truncated to a single token, and NOT classified into L4 (THE HARD LINE)
    assert not any(_r in ("instance_of", "subclass_of") and _s == name for _s, _r, _o in triples), \
        triples


def test_kinship_apposition_direction_matches_inverse_metadata():
    # "my son" ⇒ the named person is the user's CHILD (child_of), NOT parent_of — direction from the
    # kinship_noun rel-map, never inline.
    triples = _triples(m.derive_sentence_facts("My son David Chen lives in Boston.", _REF))
    assert ("david chen", "child_of", "user") in triples, triples
    assert not any(_r == "parent_of" for _s, _r, _o in triples), triples


def test_plain_possessive_without_apposition_unchanged():
    # a bare "my <kin>" with no apposed name keeps the role-surface kin edge (no regression).
    triples = _triples(m.derive_sentence_facts("My mother lives in Ottawa.", _REF))
    assert ("mother", "parent_of", "user") in triples, triples
