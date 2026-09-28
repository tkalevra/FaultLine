"""QTY-CAPTURE — a stated NUMBER is a SCALAR and must survive ingest.

Measured on a clean bench run (2026-07-31): 59% of addressable misses (22/37) lost a NUMBER while
the NOUN survived. Two distinct CAPTURE seams were reproduced offline as the cause:

  A. EVENTIVE COUNT (``EVENTIVE_COUNT_SCALAR``). The bare-count pre-pass was gated to the DB-grown
     STATIVE-POSSESSION cue class (have/own/keep/…) plus the copular "at N X" frame, so a cardinal
     stated in an EVENTIVE frame lost its numeral entirely:
         "I bought four movies at the festival."  ->  (user, buy, movies)   ['four' = residue]
     UD ``nummod`` is the cardinal-quantifier relation independent of the governing verb's
     aktionsart (universaldependencies.org/u/dep/nummod.html), so the possession gate was an
     accidental limit, not a grammatical one. The count now lands as a VERB-KEYED scalar
     ``(user, buy_movies, "four")`` — distinct per event, while the HEAD noun stays the attribute's
     LAST ``_``-segment, which is exactly what the query-side reconciler matches on
     (``main.py::_count_scalar_answer``). The SVO relational edge is NOT suppressed for this lane.

  B. MEASURE ADJUNCT (``ADJUNCT_MEASURE_SCALAR``). The measure-verb pre-pass admitted only a DIRECT
     OBJECT (or a prep-PP pobj), so an ADVERBIAL measure NP was dropped whole — number AND unit:
         "I ran 5 kilometers this morning."  ->  (user, has_state, run)  ['5','kilometers' = residue]
     UD models this as an oblique nominal modifier of measure (``obl:npmod``; spaCy/ClearNLP label
     ``npadvmod``); Quirk et al., *A Comprehensive Grammar of the English Language* §8.28 ff. class
     it as a MEASURE ADJUNCT. Admitting it yields ``(user, run, "5 kilometers")`` as a scalar.

Both are deterministic (spaCy dep/morph + the shared measure-NER discriminator), subject-agnostic,
and carry NO number-word list and NO unit word zoo — the parametrizations below deliberately span
unrelated domains and unrelated units so no lexicon could satisfy them.

THE HARD LINE is respected: every quantity is emitted with ``scalar_datatype`` set, so /ingest routes
it to ``entity_attributes`` as a STRING value and never resolves it to a UUID or files it into L4.

fail-on-old: before the fix the numeral is absent from every derived fact for the A cases, and the
whole measure span is absent for the B cases.
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL, reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2024, 6, 15)


def _facts(text, **env):
    # Both flags are set EXPLICITLY on every call, never merely defaulted: ``_reload`` mutates the
    # real process environment, so a flag left unset here would inherit whatever the PREVIOUS
    # parametrized case set (the flag-off cases would silently disable the feature for every test
    # that ran after them). Explicit-always keeps each case independent of execution order.
    env.setdefault("LINGUISTIC_LAYER", "true")
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    env.setdefault("EVENTIVE_COUNT_SCALAR", "true")
    env.setdefault("ADJUNCT_MEASURE_SCALAR", "true")
    m = _reload(**env)
    return list(m.derive_sentence_facts(text, _REF))


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _scalars(facts):
    """(subject, rel_type, object) for facts carrying a scalar_datatype — the SCALAR lane only."""
    return [(f.subject, f.rel_type, f.object) for f in facts if f.scalar_datatype]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# A. EVENTIVE COUNT — the cardinal under a non-possession verb
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Deliberately UNRELATED domains, verbs, nouns and numeral surfaces (spelled-out AND digit), so a
# verb list / noun list / number-word list could not satisfy this table.
_EVENTIVE_COUNTS = [
    # sentence,                                        subject,  attribute,             value
    ("I bought four movies at the festival.",          "user",  "buy_movies",           "four"),
    ("I visited 12 countries in 2019.",                "user",  "visit_countries",      "12"),
    ("I planted six saplings behind the shed.",        "user",  "plant_saplings",       "six"),
    ("She published seven papers this year.",          "she",   "publish_papers",       "seven"),
    ("I repaired 3 servers during the outage.",        "user",  "repair_servers",       "3"),
]


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _EVENTIVE_COUNTS)
def test_eventive_count_lands_as_scalar(sentence, subject, attribute, value):
    """The cardinal under an EVENTIVE verb is captured as a scalar (it used to be dropped)."""
    facts = _facts(sentence)
    assert (subject, attribute, value) in _scalars(facts), (sentence, _triples(facts))


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _EVENTIVE_COUNTS)
def test_eventive_count_value_is_a_scalar_never_an_entity(sentence, subject, attribute, value):
    """THE HARD LINE: a quantity is a VALUE. It must carry scalar_datatype so /ingest routes it to
    entity_attributes verbatim and never resolves it to a UUID nor files it into L4."""
    facts = _facts(sentence)
    hit = [f for f in facts if f.subject == subject and f.rel_type == attribute]
    assert hit, (sentence, _triples(facts))
    assert hit[0].scalar_datatype == "string", (sentence, hit[0].scalar_datatype)


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _EVENTIVE_COUNTS)
def test_eventive_count_keeps_the_relational_event_edge(sentence, subject, attribute, value):
    """The count is ADDITIVE — unlike the stative lane it must NOT suppress the SVO twin, because the
    event edge (user, buy, movies) is independent content the walk needs for reachability."""
    facts = _facts(sentence)
    verb = attribute.split("_")[0]
    noun = attribute.split("_", 1)[1]
    # The object surface may carry NP-completion material to the RIGHT of the head ("saplings behind
    # shed"), so match on the head token rather than the whole surface.
    rels = [(f.subject, f.rel_type, (f.object or "").split()[0] if f.object else "")
            for f in facts if not f.scalar_datatype]
    assert (subject, verb, noun) in rels, (sentence, _triples(facts))


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _EVENTIVE_COUNTS)
def test_eventive_count_head_noun_is_the_last_attribute_segment(sentence, subject, attribute, value):
    """Contract with the query-side reconciler (``main.py::_count_scalar_answer`` matches the LAST
    ``_``-segment of the attribute against the queried head noun). Verb-keying must never break it."""
    facts = _facts(sentence)
    hit = [f for f in facts if f.subject == subject and f.rel_type == attribute]
    assert hit, (sentence, _triples(facts))
    assert hit[0].rel_type.split("_")[-1] == attribute.split("_")[-1]


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _EVENTIVE_COUNTS)
def test_eventive_count_flag_off_is_legacy(sentence, subject, attribute, value):
    """OFF → the pre-pass is possession-only, byte-for-byte today's behaviour (no count scalar)."""
    facts = _facts(sentence, EVENTIVE_COUNT_SCALAR="false")
    assert (subject, attribute, value) not in _scalars(facts), (sentence, _triples(facts))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# B. MEASURE ADJUNCT — the adverbial measure NP (UD obl:npmod / spaCy npadvmod)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Unrelated verbs and unrelated unit DIMENSIONS (distance-metric, distance-imperial, time), so no
# unit lexicon could satisfy this table. The unit is NOT required to be in the ``unit_scalar`` cue
# map — the construction itself (nummod NUM under an npadvmod NOUN) plus the measure-NER span is the
# whole discriminator.
#
# NOTE (why this lane is needed at all): spaCy's choice between ``dobj`` and ``npadvmod`` for a
# measure NP is genuinely unstable across near-identical sentences — "I cycled 40 kilometers on
# Saturday" parses the measure as ``dobj`` while "I ran 5 kilometers this morning" parses it as
# ``npadvmod``. The classic dobj lane therefore covered only half of these constructions; every
# sentence below is verified to parse as a genuine ``npadvmod`` measure adjunct.
_ADJUNCT_MEASURES = [
    # sentence,                               subject, rel,      value
    ("I ran 5 kilometers this morning.",      "user",  "run",    "5 kilometers"),
    ("I walked 27 miles last week.",          "user",  "walk",   "27 miles"),
    ("I slept 8 hours last night.",           "user",  "sleep",  "8 hours"),
    ("I studied 6 hours yesterday.",          "user",  "study",  "6 hours"),
]


@requires_model
@pytest.mark.parametrize("sentence,subject,rel,value", _ADJUNCT_MEASURES)
def test_adjunct_measure_is_captured(sentence, subject, rel, value):
    """The adverbial measure NP survives with BOTH its numeral and its unit (it used to be dropped
    whole — the number, which is the answer, never reached the store)."""
    facts = _facts(sentence)
    assert (subject, rel, value) in _scalars(facts), (sentence, _triples(facts))


@requires_model
@pytest.mark.parametrize("sentence,subject,rel,value", _ADJUNCT_MEASURES)
def test_adjunct_measure_flag_off_is_legacy(sentence, subject, rel, value):
    """OFF → the candidate gate is dobj/PP-only, byte-for-byte today's behaviour."""
    facts = _facts(sentence, ADJUNCT_MEASURE_SCALAR="false")
    assert (subject, rel, value) not in _scalars(facts), (sentence, _triples(facts))


@requires_model
def test_adjunct_measure_coexists_with_a_real_direct_object():
    """An adjunct measure modifies the verb ALONGSIDE a direct object, so the relational edge must
    survive — the adjunct lane is deliberately NON-suppressing (unlike the classic dobj measure)."""
    facts = _facts("I drove the truck 300 miles yesterday.")
    t = _triples(facts)
    assert ("user", "drive", "truck") in t, t          # the relational edge survives
    assert ("user", "drive", "300 miles") in _scalars(facts), t   # and the measure lands


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# FIREWALLS — the things that must NOT become quantities
# ─────────────────────────────────────────────────────────────────────────────────────────────────

@requires_model
@pytest.mark.parametrize("sentence", [
    "I ran 5 kilometers this morning.",
    "I walked 27 miles last week.",
    "I slept 8 hours last night.",
])
def test_temporal_adjunct_never_becomes_a_measure(sentence):
    """A WHEN is not a measure (dual-clock). A temporal npadvmod carries no ``nummod`` NUM child and
    is additionally caught by the resolved-date firewall, so it must never surface as a scalar."""
    for (_s, _r, _o) in _scalars(_facts(sentence)):
        for _when in ("morning", "week", "night", "yesterday"):
            assert _when not in _o, (sentence, _s, _r, _o)


@requires_model
@pytest.mark.parametrize("sentence,frequency_noun", [
    ("I ran three times last week.", "times"),
    ("I called twice yesterday.", "times"),
])
def test_frequency_adjunct_is_not_captured_as_a_measure(sentence, frequency_noun):
    """Safe UNDER-capture: a FREQUENCY adjunct is not a measured magnitude. spaCy's measure-NER spans
    only the cardinal (CARDINAL), never covering the head noun, so no bind is made. We do not invent
    a quantity we cannot type."""
    assert not [s for s in _scalars(_facts(sentence)) if frequency_noun in (s[2] or "")], sentence


@requires_model
@pytest.mark.parametrize("sentence,attribute,value", [
    ("I have 3 cats.", "cats", "3"),
    ("I have 600 followers.", "followers", "600"),
    ("I've been to 15 countries.", "countries", "15"),
])
def test_stative_count_is_unchanged(sentence, attribute, value):
    """The stative-possession count keeps its BARE-noun key — that key IS the recency-overwrite row
    ("current count of the same noun wins"). Verb-keying applies to the eventive lane ONLY."""
    assert ("user", attribute, value) in _scalars(_facts(sentence)), sentence


@requires_model
@pytest.mark.parametrize("sentence,subject,rel,value", [
    ("I spent 70 hours on the project.", "user", "spend", "70 hours"),
    ("I logged 140 hours last quarter.", "user", "log", "140 hours"),
    # parses the measure as a genuine ``dobj`` — the classic lane owns it, with the adjunct flag OFF
    ("I cycled 40 kilometers on Saturday.", "user", "cycle", "40 kilometers"),
])
def test_classic_direct_object_measure_is_unchanged(sentence, subject, rel, value):
    """The classic dobj measure shape must be untouched by the adjunct admission (which fires only
    when no numeral-bearing direct object already claimed the slot)."""
    assert (subject, rel, value) in _scalars(
        _facts(sentence, ADJUNCT_MEASURE_SCALAR="false")), sentence


@requires_model
def test_unit_noun_object_is_not_double_captured_as_a_count():
    """A UNIT noun is owned by the measure/quantity chains; the eventive count lane must not also
    mint a count for it (``_count_noun_ok`` excludes unit_scalar units and measure-NER spans)."""
    facts = _facts("I spent 70 hours on the project.")
    assert not [f for f in facts if f.rel_type.endswith("_hours")], _triples(facts)
