"""DURATION — a stated DURATION is a MEASURE, not a time-position, and its NUMBER must survive.

THE DEFECT (LongMemEval "missing NUMBER" cluster — matched=['week'] missing=['5.5'],
matched=['month'] missing=['two'], matched=[] missing=['3.5']). A `for`-marked DURATION adjunct
under a present-perfect durative had its quantity ANNIHILATED:

    "I have been learning Spanish for 3.5 weeks."   (session reference 2023-05-22)
      ->  (user, learn, spanish) @2023-04-27          # 3.5 and `weeks` appear NOWHERE

THE MECHANISM (measured, not assumed). The DATE LAYER IS NOT THE CULPRIT — ``extract_event_date`` /
``_resolve_first_valid_date`` already REJECT a bare duration adjunct ("for 3.5 weeks", "for two
months", "for three years", "for 90 minutes" all resolve to ``(None, None)``; dateparser's
relative-time parser resolves POSITIONS — "3 weeks ago", "in 2 weeks" — not `for`-marked spans).
The quantity is destroyed one layer later: ``_durative_for_inception`` legitimately derives the
state's INCEPTION (reference − duration) and PEELS the "for <N> <unit>" tokens into
``_date_token_idx`` so the duration cannot fold into a junk relationship object. But
``_date_token_idx`` is ALSO the measure-verb pre-pass's "this span is a WHEN, never a measure"
firewall — so the lane that already captures "I waited FOR 20 minutes" refused the peeled span and
nothing was left to capture the number.

THE FIX (``SPINE_DURATION_ADJUNCT``, default ON). Durative-peeled tokens are recorded separately
(``_durative_measure_idx``) and are exempt from the measure lane's WHEN firewall ONLY — every other
``_date_token_idx`` consumer keeps the full peel. The duration then lands through the EXISTING,
consumer-verified measure path as a scalar carrying the FULL span, so the MAGNITUDE and the UNIT
survive together ("3.5 weeks"), never split and never dropped. Because a `for`-marked duration is an
oblique ADJUNCT (UD ``obl`` + ``case``), not a complement, the lane's SVO suppression is disabled for
it — the relational twin is independent content (regression-guarded below).

EVIDENCE-GROUND. ISO-TimeML 1.2.1 types a temporal expression as ``TIMEX3 @type ::= DATE | TIME |
DURATION | SET``: a DURATION is a span LENGTH (ISO-8601 PnW/PnM) distinct from a DATE calendar point,
and it is ANCHORED by beginPoint/endPoint — so a duration legitimately yields an anchored point, but
must never REPLACE the measure. UD marks the two structurally: ``obl`` covers temporal nominal
modifiers, and ``obl:tmod`` is reserved for a modifier "specifying a TIME" (the bare time-when
nominal), while a `for`-marked duration is a CASE-MARKED oblique. Quirk et al., *A Comprehensive
Grammar of the English Language* ch. 8, treat time-position and duration as distinct adverbial
subclasses.

Deterministic throughout (spaCy dep/morph + the shared measure-NER discriminator + dateparser's rule
engine) — NO LLM, NO cosine, NO unit word zoo, NO verb list. The parametrizations below deliberately
span unrelated domains, unrelated verbs and unrelated unit DIMENSIONS (day/week/month/year/decade) so
no lexicon could satisfy them.

BOTH DIRECTIONS are pinned: durations must yield a magnitude+unit scalar, and genuine TIME-POSITION
adjuncts ("3 weeks ago", "last Thursday", "on May 3rd", "in 2019") must STILL resolve to an
event_date and must NOT mint a duration scalar.

fail-on-old: with ``SPINE_DURATION_ADJUNCT=false`` every duration case below loses the number.
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

# The session reference the bug was reproduced against.
_REF = datetime.datetime(2023, 5, 22, tzinfo=datetime.timezone.utc)


def _facts(text, **env):
    # Every flag this lane interacts with is set EXPLICITLY on each call: ``_reload`` mutates the
    # real process environment, so a flag merely defaulted here would inherit whatever a previous
    # parametrized case set. Explicit-always keeps each case independent of execution order.
    env.setdefault("LINGUISTIC_LAYER", "true")
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    env.setdefault("SPINE_DURATION_ADJUNCT", "true")
    env.setdefault("ADJUNCT_MEASURE_SCALAR", "true")
    env.setdefault("EVENTIVE_COUNT_SCALAR", "true")
    m = _reload(**env)
    return list(m.derive_sentence_facts(text, _REF))


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _scalars(facts):
    """(subject, rel_type, object) for facts carrying a scalar_datatype — the SCALAR lane only."""
    return [(f.subject, f.rel_type, f.object) for f in facts if f.scalar_datatype]


def _scalar_values(facts):
    return [f.object for f in facts if f.scalar_datatype]


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# A. THE DATE LAYER MUST NOT EAT A DURATION (the firewall that already held — pinned so it stays)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# Unrelated verbs, unrelated unit DIMENSIONS, digit + spelled-out + fractional magnitudes.
_DURATION_ONLY_TEXTS = [
    "I have been learning Spanish for 3.5 weeks.",
    "I have been playing the violin for five years.",
    "We have been renovating the kitchen for 11 days.",
    "The class lasted for 90 minutes.",
    "I have owned this bike for two decades.",
    "She has been studying chemistry for six weeks.",
]


@requires_model
@pytest.mark.parametrize("sentence", _DURATION_ONLY_TEXTS)
def test_duration_adjunct_is_never_a_resolved_date_span(sentence):
    """``extract_event_date`` must not resolve a `for`-marked DURATION to a calendar point.

    A DURATION is TIMEX3 @type=DURATION (a span LENGTH), never a DATE. If this ever goes green->red
    the date layer has started swallowing durations again and the magnitude will be destroyed."""
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    assert m.extract_event_date(sentence, _REF) == (None, None), sentence


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# B. THE DURATION MUST BE CAPTURED — magnitude AND unit, together, on the scalar lane
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# (sentence, expected scalar subject, expected attribute, expected value)
# Deliberately unrelated domains (language study, music, pets, wearables, residence, cycling,
# chemistry, home renovation, athletics), unrelated verbs, and FIVE distinct unit dimensions
# (day / week / month / year / decade) with digit, fractional and spelled-out magnitudes.
_DURATIONS = [
    ("I have been learning Spanish for 3.5 weeks.",      "spanish",   "learn",    "3.5 weeks"),
    ("I have been learning Spanish for two months.",     "spanish",   "learn",    "two months"),
    ("I have been playing the violin for five years.",   "violin",    "play",     "five years"),
    ("I've had my cat for 9 months now.",                "cat",       "have",     "9 months"),
    ("I've been using my Fitbit for 9 months now.",      "fitbit",    "use",      "9 months"),
    ("I have owned this bike for two decades.",          "bike",      "own",      "two decades"),
    ("She has been studying chemistry for six weeks.",   "chemistry", "study",    "six weeks"),
    ("We have been renovating the kitchen for 11 days.", "kitchen",   "renovate", "11 days"),
    ("I've been living in Harajuku for 3 months now.",   "user",      "live",     "3 months"),
    ("He has been training for the marathon for 14 weeks.", "he",     "train",    "14 weeks"),
]


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _DURATIONS)
def test_duration_lands_as_scalar_with_unit(sentence, subject, attribute, value):
    """The duration is captured as a scalar whose value carries the magnitude AND the unit."""
    assert (subject, attribute, value) in _scalars(_facts(sentence)), sentence


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _DURATIONS)
def test_duration_magnitude_and_unit_are_never_split(sentence, subject, attribute, value):
    """The number and its unit survive in ONE value — a bare number or a bare unit is still a miss."""
    vals = _scalar_values(_facts(sentence))
    assert value in vals, (sentence, vals)
    magnitude, unit = value.split(" ", 1)
    assert magnitude not in vals, (sentence, "bare magnitude emitted without its unit", vals)
    assert unit not in vals, (sentence, "bare unit emitted without its magnitude", vals)


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _DURATIONS)
def test_duration_scalar_is_absent_when_flag_off(sentence, subject, attribute, value):
    """fail-on-old: flag OFF reproduces the defect exactly (the number is gone)."""
    off = _facts(sentence, SPINE_DURATION_ADJUNCT="false")
    assert (subject, attribute, value) not in _scalars(off), sentence


@requires_model
@pytest.mark.parametrize("sentence,subject,attribute,value", _DURATIONS)
def test_duration_scalar_routes_to_entity_attributes(sentence, subject, attribute, value):
    """The duration carries ``scalar_datatype`` so /ingest routes the VERBATIM span to
    ``entity_attributes`` (``main.py::_edge_is_scalar`` signal 2) and never resolves it to a UUID
    entity — THE HARD LINE: a measured value is never filed into L4 as a place."""
    hit = [f for f in _facts(sentence)
           if (f.subject, f.rel_type, f.object) == (subject, attribute, value)]
    assert hit, sentence
    assert all(f.scalar_datatype for f in hit), sentence


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# C. A DURATION ADJUNCT NEVER COSTS A RELATIONAL FACT (measured regressions — keep them pinned)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# A `for`-marked duration is an oblique ADJUNCT, so it coexists with the clause's real arguments.
# Both cases below were LOST by the first cut of this fix (the measure lane's bare-PP branch
# suppressed the SVO twin) and are pinned so the suppression can never come back.
_COEXISTING_RELATIONS = [
    ("I've been living in Harajuku for 3 months now.",       "user",    "live_in",  "harajuku"),
    ("He has been training for the marathon for 14 weeks.",  "he",      "train_for", "marathon"),
    ("I have been learning Spanish for 3.5 weeks.",          "user",    "learn",    "spanish"),
    ("I have been playing the violin for five years.",       "user",    "play",     "violin"),
    ("She has been studying chemistry for six weeks.",       "she",     "study",    "chemistry"),
]


@requires_model
@pytest.mark.parametrize("sentence,subject,rel,obj", _COEXISTING_RELATIONS)
def test_relational_twin_survives_the_duration_capture(sentence, subject, rel, obj):
    """The relational edge is independent content — capturing the duration must never suppress it."""
    assert (subject, rel, obj) in _triples(_facts(sentence)), (sentence, _triples(_facts(sentence)))


@requires_model
@pytest.mark.parametrize("sentence,subject,rel,obj", _COEXISTING_RELATIONS)
def test_flag_on_never_loses_a_fact_the_flag_off_path_produced(sentence, subject, rel, obj):
    """ADDITIVE-ONLY contract: every fact derived with the flag OFF is still derived with it ON.

    A duration capture that costs a fact elsewhere is a regression, not a win."""
    off = {(f.subject, f.rel_type, f.object, f.event_date)
           for f in _facts(sentence, SPINE_DURATION_ADJUNCT="false")}
    on = {(f.subject, f.rel_type, f.object, f.event_date)
          for f in _facts(sentence, SPINE_DURATION_ADJUNCT="true")}
    assert off <= on, (sentence, "LOST: " + repr(sorted(off - on)))


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# D. THE OTHER DIRECTION — genuine TIME-POSITION adjuncts must STILL date
# ─────────────────────────────────────────────────────────────────────────────────────────────────

# (sentence, expected ISO event_date) — relative, deictic, absolute and year-only positions across
# unrelated domains, so the discriminator cannot be a word list.
_TIME_POSITIONS = [
    ("I went to Paris 3 weeks ago.",       "2023-05-01"),
    ("I saw the dentist last Thursday.",   "2023-05-18"),
    ("I bought the car on May 3rd.",       "2023-05-03"),
    ("I moved to Berlin in 2019.",         "2019-05-22"),
    ("I adopted the dog two months ago.",  "2023-03-22"),
]


@requires_model
@pytest.mark.parametrize("sentence,iso", _TIME_POSITIONS)
def test_time_position_still_resolves_to_a_date(sentence, iso):
    """A time-WHEN is a TIMEX3 DATE and must still resolve — the fix must not over-reject."""
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                SPINE_DURATION_ADJUNCT="true")
    got, _gran = m.extract_event_date(sentence, _REF)
    assert got is not None and got.startswith(iso), (sentence, got)


@requires_model
@pytest.mark.parametrize("sentence,iso", _TIME_POSITIONS)
def test_time_position_binds_the_date_onto_a_derived_fact(sentence, iso):
    """The resolved position lands on an emitted fact (it is not merely resolvable in isolation)."""
    facts = _facts(sentence)
    dated = [f for f in facts if f.event_date and str(f.event_date).startswith(iso)]
    assert dated, (sentence, [(f.rel_type, f.object, f.event_date) for f in facts])


@requires_model
@pytest.mark.parametrize("sentence,iso", _TIME_POSITIONS)
def test_time_position_mints_no_duration_scalar(sentence, iso):
    """A WHEN must never leak into the measure lane as a duration value."""
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm",
                SPINE_DURATION_ADJUNCT="true")
    facts = list(m.derive_sentence_facts(sentence, _REF))
    for val in _scalar_values(facts):
        assert "ago" not in str(val).lower(), (sentence, val)


@requires_model
@pytest.mark.parametrize("sentence,iso", _TIME_POSITIONS)
def test_time_position_output_is_identical_with_the_flag_off(sentence, iso):
    """Byte-for-byte containment: the flag touches DURATION adjuncts only, never a time-position."""
    off = sorted((f.subject, f.rel_type, f.object, f.event_date, f.scalar_datatype)
                 for f in _facts(sentence, SPINE_DURATION_ADJUNCT="false"))
    on = sorted((f.subject, f.rel_type, f.object, f.event_date, f.scalar_datatype)
                for f in _facts(sentence, SPINE_DURATION_ADJUNCT="true"))
    assert off == on, sentence


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# E. ADJACENT LANES ARE UNTOUCHED (counts, measure adjuncts, duration-to-complete, controls)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

_UNTOUCHED = [
    "I ran 5 kilometers this morning.",
    "My commute takes 45 minutes each way.",
    "I bought a handbag for $800.",
    "I scored 95 on the exam.",
    "I have 600 followers.",
    "I own three bikes.",
    "I bought four movies at the festival.",
    "It took me three weeks to finish the book.",
    "My trip to Outer Banks took about four hours.",
    "I have been working at NovaTech for 4 years and 3 months.",
    "My mother's name is Carol.",
    "My favorite color is blue.",
    "Sarah is 28.",
]


@requires_model
@pytest.mark.parametrize("sentence", _UNTOUCHED)
def test_adjacent_lanes_are_byte_identical_across_the_flag(sentence):
    """Every non-durative shape derives identically with the flag ON and OFF."""
    off = sorted((f.subject, f.rel_type, f.object, f.event_date, f.scalar_datatype)
                 for f in _facts(sentence, SPINE_DURATION_ADJUNCT="false"))
    on = sorted((f.subject, f.rel_type, f.object, f.event_date, f.scalar_datatype)
                for f in _facts(sentence, SPINE_DURATION_ADJUNCT="true"))
    assert off == on, sentence


@requires_model
def test_employment_duration_lane_still_owns_its_construction():
    """The seeded ``duration`` scalar for an employment tenure is unchanged (it peels through its
    OWN pre-pass, not the durative-inception exemption)."""
    facts = _facts("I have been working at NovaTech for 4 years and 3 months.")
    assert ("novatech", "duration", "4 years and 3 months") in _scalars(facts), _triples(facts)
