r"""THE HARD LINE for MEASUREMENT: a unit is never a PLACE, a measure is never SPLIT, and a
FUTURE window never resolves into the PAST. Plus: a CLOCK TIME is not a network PORT.

Two unrelated defects, both reproduced offline before anything was changed, both pinned here.

─────────────────────────────────────────────────────────────────────────────────────────────────
BUG 1 — a TIME UNIT was filed as a PLACE, and the measure was split (session reference 2023-05-22)

    "My flight is in two weeks."
      ->  (user,   owns,       flight)
          (flight, duration,   "two")     # MAGNITUDE alone — the unit destroyed
          (flight, located_in, "weeks")   # a unit of TIME filed as a LOCATION

``located_in`` is a SPATIAL containment relation (Wikidata P131) and a seeded ``is_hierarchy_rel``
the descendant walk DESCENDS, so this made ``weeks`` walkable as a place — an engine-built PLACE
minted straight out of user content, which is exactly what CLAUDE.md's founding rule forbids ("a
name/value/specific instance NEVER becomes a place; a place is NEVER user content").

Traced to two chains. ``_chain_classification_containment`` guards only on
``pobj.ent_type_ in ("DATE","TIME")`` — dead on both callers, because the deriver's parse pipeline
has no NER component (``doc.ents == []``, measured) and the GLiNER2-typed Doc carries only the six
concise zero-shot labels (Pitfall 11 — never DATE). ``_chain_copula_measure`` accepted a unit noun
anywhere under the copula, so a case-marked OBLIQUE adjunct ("is IN two weeks") was read as the
copula's measure predicate and minted ``duration`` from the numeral alone.

THE GUARDS (``SPINE_TEMPORAL_UNIT_NOT_PLACE``, default ON):
  (1) a NUM-quantified MEASURE-UNIT nominal is never the tail of a spatial containment rel. The
      unit inventory is the DB-grown per-tenant ``unit_scalar`` cue class — no in-code unit list —
      and the numeral requirement is what keeps a genuine place safe ("in rack 4", "in the pound").
  (2) a copula MEASURE PREDICATE is never CASE-MARKED. Measured on this parser: the genuine cases
      put the unit noun in the PREDICATE ("62 years old"/"6 feet tall" → npadvmod under the ADJ
      complement; "is 3 hours"/"is 45 minutes" → attr), while "is IN two weeks" puts it in ``pobj``
      under an ADP — an oblique ADJUNCT. A pobj unit is declined rather than half-captured.
  (3) when the date-core recovery narrows a span to a bare ``<magnitude> <unit>``, the discarded
      tokens were the positional marker and the core re-parses under PREFER_DATES_FROM=past — so a
      FUTURE window resolved BACKWARDS ("for the next two weeks" → reference − 2 weeks). The
      direction is now read off dateparser ITSELF (probe the discarded marker against an INERT
      calendar carrier) and the core re-anchored forward ONLY when the probe proves prospectivity.
      Strictly additive: a retrospective or undetermined marker resolves exactly as it does today.

EVIDENCE-GROUND (fetched and verified this session — nothing invented):
  • ISO-TimeML 1.2.1 §2.3 <TIMEX3> declares ``type ::= 'DATE' | 'TIME' | 'DURATION' | 'SET'`` and
    states "beginPoint and endpoint are used to anchor durations to other time expressions in the
    document" — a temporal expression is one of four TEMPORAL types, anchored to other TEMPORAL
    expressions; it is categorically not a spatial container.
    https://timeml.github.io/site/publications/timeMLdocs/timeml_1.2.1.html
  • Universal Dependencies defines ``obl:tmod`` as "a subtype of the obl relation: if the modifier
    is specifying a time, it is labeled as _tmod_" — UD classifies a time-specifying nominal as an
    OBLIQUE MODIFIER of the predicate, which is exactly the pobj-vs-attr/npadvmod split guard (2)
    keys on.  https://universaldependencies.org/en/dep/obl-tmod.html
  (The "Quirk et al. CGEL ch. 8" time-position/duration claim carried elsewhere in this repo could
  NOT be verified against a primary source in this session and is deliberately not restated.)

─────────────────────────────────────────────────────────────────────────────────────────────────
BUG 2 — a CLOCK TIME was read as a network PORT

The seeded scalar_atomic pattern for ``has_port`` (migration 060) was ``(?:port\s+|:)([1-9]\d{0,4})\b``.
Its bare-colon alternative matches ANY colon+digits, so the session-date marker every turn carries —
``[Date: 2023/05/22 (Mon) 22:17]`` — matched ``:17`` and proposed a ``has_port`` scalar on whatever
entity was in scope, on every turn. RFC 3986 §3.2.3 is explicit that the colon is a port marker only
by virtue of what stands to its LEFT ("an optional port number in decimal FOLLOWING THE HOST and
delimited from it by a single colon"), so migration 198 replaces the bare colon with a HOST-QUALIFIED
one and puts the marker in a LOOKBEHIND — which also fixes a second, independent defect: the
detector takes ``m.group(0)``, so every previous hit stored its own marker ("port 8080", ":8080")
and was then REJECTED by the migration-101 ``integer`` datatype gate. The tests below pin the regex
AS SHIPPED IN THE MIGRATION FILE (read off disk, never a copy) so the seed and the contract cannot
drift apart.

─────────────────────────────────────────────────────────────────────────────────────────────────
NO WORD LIST COULD SATISFY THESE TESTS. Every parametrization deliberately spans unrelated domains
(aviation, dentistry, agriculture, music, chemistry, networking, real-estate, veterinary, athletics)
and unrelated unit DIMENSIONS (second/minute/hour/day/week/month/year and the non-temporal
foot/pound), and the place cases are drawn from unrelated place kinds.

fail-on-old: with ``SPINE_TEMPORAL_UNIT_NOT_PLACE=false`` the unit-as-place edge and the split
magnitude both come back (pinned explicitly in section F).
"""
import datetime
import importlib
import os
import pathlib
import re

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

# The session reference both bugs were reproduced against.
_REF = datetime.datetime(2023, 5, 22, tzinfo=datetime.timezone.utc)


def _facts(text, **env):
    # Every flag this lane interacts with is set EXPLICITLY on each call: ``_reload`` mutates the
    # real process environment, so a merely-defaulted flag would inherit whatever a previous
    # parametrized case set. Explicit-always keeps each case independent of execution order.
    env.setdefault("LINGUISTIC_LAYER", "true")
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    env.setdefault("SPINE_TEMPORAL_UNIT_NOT_PLACE", "true")
    env.setdefault("SPINE_DURATION_ADJUNCT", "true")
    env.setdefault("ADJUNCT_MEASURE_SCALAR", "true")
    env.setdefault("EVENTIVE_COUNT_SCALAR", "true")
    m = _reload(**env)
    return list(m.derive_sentence_facts(text, _REF))


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


def _date(text, **env):
    env.setdefault("LINGUISTIC_LAYER", "true")
    env.setdefault("SPACY_MODEL", "en_core_web_sm")
    env.setdefault("SPINE_TEMPORAL_UNIT_NOT_PLACE", "true")
    env.setdefault("SPINE_DURATION_ADJUNCT", "true")
    m = _reload(**env)
    return m.extract_event_date(text, _REF)


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# A. THE HARD LINE — a MEASURE UNIT is NEVER filed as a PLACE
# ═════════════════════════════════════════════════════════════════════════════════════════════════

# (sentence, the unit surface that must never appear as a located_in tail). Unrelated domains,
# SEVEN distinct unit dimensions, digit + spelled-out magnitudes.
_UNIT_AS_PLACE_CASES = [
    ("My flight is in two weeks.",              "weeks"),
    ("My appointment is in three days.",        "days"),
    ("The conference is in two months.",        "months"),
    ("The meeting is in 2 hours.",              "hours"),
    ("My renewal is in three months.",          "months"),
    ("The harvest is in six weeks.",            "weeks"),
    ("Her recital is in four days.",            "days"),
    ("The lease review is in 18 months.",       "months"),
    ("The titration finishes in 90 seconds.",   "seconds"),
    ("The next maintenance window is in 45 minutes.", "minutes"),
    ("The warranty expires in two years.",      "years"),
]


@requires_model
@pytest.mark.parametrize("sentence,unit", _UNIT_AS_PLACE_CASES)
def test_measure_unit_is_never_a_located_in_tail(sentence, unit):
    """THE HARD LINE. ``located_in`` is spatial containment (P131) and a hierarchy rel the walk
    DESCENDS — a unit of time filed there becomes walkable as a place. It must never happen, in
    ANY domain, for ANY unit dimension."""
    for subj, rel, obj in _triples(_facts(sentence)):
        assert not (rel == "located_in" and obj.strip().lower() == unit), (
            f"{sentence!r} filed the measure unit {unit!r} as a PLACE: ({subj}, {rel}, {obj})")


@requires_model
@pytest.mark.parametrize("sentence,unit", _UNIT_AS_PLACE_CASES)
def test_no_measure_unit_lands_on_any_hierarchy_place_rel(sentence, unit):
    """Stronger form: the unit must not be the tail of ANY containment/classification rel either —
    filing it under ``instance_of``/``subclass_of``/``part_of`` would be the same category error
    one rung over (a unit is a measurement primitive, never a type node or a component)."""
    _place_rels = {"located_in", "located_at", "part_of", "instance_of", "subclass_of", "member_of"}
    for subj, rel, obj in _triples(_facts(sentence)):
        assert not (rel in _place_rels and obj.strip().lower() == unit), (
            f"{sentence!r} routed the measure unit {unit!r} onto {rel!r}: ({subj}, {rel}, {obj})")


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# B. NO SPLIT MEASURE — a bare magnitude is never emitted as a scalar on its own
# ═════════════════════════════════════════════════════════════════════════════════════════════════

# (sentence, the bare magnitude the OLD code emitted as the whole "duration" value)
_SPLIT_CASES = [
    ("My flight is in two weeks.",          "two"),
    ("My appointment is in three days.",    "three"),
    ("The conference is in two months.",    "two"),
    ("The meeting is in 2 hours.",          "2"),
    ("My renewal is in three months.",      "three"),
    ("The harvest is in six weeks.",        "six"),
    ("The warranty expires in two years.",  "two"),
    ("I'm free for the next two weeks.",    "two"),
]


@requires_model
@pytest.mark.parametrize("sentence,magnitude", _SPLIT_CASES)
def test_measure_is_never_split_into_a_bare_magnitude(sentence, magnitude):
    """A measure is a magnitude AND a unit. Emitting the numeral alone is not a partial capture, it
    is an uninterpretable one — and the value is not even readable by the ``duration`` consumers
    (``duration_phrase_to_months`` requires digits + a calendar unit; ``_parse_scalar_magnitude``
    requires a leading digit — "two" satisfies neither)."""
    for subj, rel, obj in _triples(_facts(sentence)):
        assert obj.strip().lower() != magnitude, (
            f"{sentence!r} emitted the bare magnitude {magnitude!r} as a value: "
            f"({subj}, {rel}, {obj}) — magnitude and unit must never be split")


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# C. GENUINE PLACES MUST STILL BE CAPTURED (the guard must not eat locations)
# ═════════════════════════════════════════════════════════════════════════════════════════════════

# A NUM-quantified pobj that is NOT a unit ("rack 4") is a place and must survive; so must every
# ordinary place. Unrelated place kinds: datacentre, building, geography, domestic.
_PLACE_CASES = [
    ("The server is in rack 4.",        "rack 4"),
    ("The switch is in cabinet 12.",    "cabinet 12"),
    ("Rack-2 is located in row-a.",     "row-a"),
]


@requires_model
@pytest.mark.parametrize("sentence,place", _PLACE_CASES)
def test_genuine_places_still_capture(sentence, place):
    """A numeral next to a noun does not make it a unit — ``rack``/``cabinet`` are not in the
    ``unit_scalar`` map, so these must still land on ``located_in``."""
    tails = [obj for _s, rel, obj in _triples(_facts(sentence)) if rel == "located_in"]
    assert any(place in t for t in tails), (
        f"{sentence!r} lost its genuine place — located_in tails: {tails}")


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# D. GENUINE COPULA MEASURES MUST STILL BE CAPTURED (the oblique guard must not eat predicates)
# ═════════════════════════════════════════════════════════════════════════════════════════════════

# (sentence, expected scalar rel, expected value). The unit noun here is npadvmod/attr — the
# PREDICATE — never pobj. Unrelated dimensions: age (year), height (foot), duration (hour/minute).
_COPULA_MEASURE_CASES = [
    ("She is 62 years old.",     "age",    "62"),
    # a degree-adjective measure keeps its unit unless the dimension declares a bare count
    # (age is integer-typed, so "62 years old" stays 62; height is a quantity)
    ("He is 6 feet tall.",       "height", "6 feet"),
    ("Sarah is 28.",             "age",    "28"),
    ("My daughter is 10 years old.", "age", "10"),
]


@requires_model
@pytest.mark.parametrize("sentence,rel,value", _COPULA_MEASURE_CASES)
def test_copula_measure_predicate_untouched(sentence, rel, value):
    """The oblique guard keys on ``pobj``. A measure in the copula's PREDICATE (npadvmod under the
    ADJ complement, or the attr nominal) is not case-marked and must be captured exactly as before."""
    got = [(r, o) for _s, r, o in _triples(_facts(sentence))]
    assert (rel, value) in got, f"{sentence!r} lost its copula measure — got {got}"


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# E. DIRECTION — a FUTURE window must not resolve into the PAST; the PAST must not move
# ═════════════════════════════════════════════════════════════════════════════════════════════════

# Unrelated domains, four unit dimensions. Every one of these resolved BEFORE the reference under
# the old code (measured: two weeks → 2023-05-08, three days → 2023-05-19, six months →
# 2022-11-22, two years → 2021-05-22 then year-repinned to 2023-05-22).
_PROSPECTIVE = [
    "I'm free for the next two weeks.",
    "I'm busy for the next three days.",
    "She is away for the next six months.",
    "The offer runs for the next two years.",
]


@requires_model
@pytest.mark.parametrize("sentence", _PROSPECTIVE)
def test_future_window_never_resolves_into_the_past(sentence):
    iso, _gran = _date(sentence)
    assert iso is not None, f"{sentence!r} lost its date entirely"
    assert iso[:10] >= _REF.date().isoformat(), (
        f"{sentence!r} resolved a FORWARD window to the PAST date {iso[:10]} "
        f"(reference {_REF.date().isoformat()})")


# Retrospective / absolute / bare-duration spans must be BYTE-IDENTICAL to today. These are the
# regression wall: the direction fix is only ever allowed to flip a resolution the probe PROVED
# was backwards.
_UNCHANGED_DATES = [
    ("I moved to Toronto three weeks ago.",   "2023-05-01"),
    ("I saw the dentist last Thursday.",      "2023-05-18"),
    ("We adopted the dog on May 3rd.",        "2023-05-03"),
    ("I graduated in 2019.",                  "2019-05-22"),
    ("I renewed the lease yesterday.",        "2023-05-21"),
    ("The furniture arrived three weeks ago.", "2023-05-01"),
    ("I bought it the previous two months.",  "2023-03-22"),
    ("I saw her last Tuesday.",               "2023-05-16"),
    ("It happened in mid-February.",          "2023-02-15"),
]


@requires_model
@pytest.mark.parametrize("sentence,expected", _UNCHANGED_DATES)
def test_retrospective_and_absolute_dates_are_unmoved(sentence, expected):
    iso, _gran = _date(sentence)
    assert iso is not None and iso[:10] == expected, (
        f"{sentence!r} expected {expected}, got {iso and iso[:10]} — the direction fix must never "
        f"touch a retrospective or absolute span")


# A BARE duration is a TIMEX3 DURATION (a span LENGTH), never a time-position — it must stay
# date-free, exactly as the shipped SPINE_DURATION_ADJUNCT lane requires.
@requires_model
@pytest.mark.parametrize("sentence", [
    "I have been learning Spanish for 3.5 weeks.",
    "The class lasted for 90 minutes.",
    "I have owned this bike for two decades.",
])
def test_bare_durations_still_yield_no_date(sentence):
    assert _date(sentence) == (None, None), sentence


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# F. FAIL-ON-OLD — with the flag OFF the defects come back (proves the tests bite)
# ═════════════════════════════════════════════════════════════════════════════════════════════════

@requires_model
def test_flag_off_reproduces_the_unit_as_a_place_defect():
    got = _triples(_facts("My flight is in two weeks.", SPINE_TEMPORAL_UNIT_NOT_PLACE="false"))
    assert ("flight", "located_in", "weeks") in got, (
        "the ORIGINAL defect no longer reproduces with the flag OFF — this test has stopped "
        f"proving anything; got {got}")


@requires_model
def test_flag_off_reproduces_the_split_measure_defect():
    got = _triples(_facts("My flight is in two weeks.", SPINE_TEMPORAL_UNIT_NOT_PLACE="false"))
    assert ("flight", "duration", "two") in got, (
        f"the ORIGINAL split no longer reproduces with the flag OFF; got {got}")


@requires_model
def test_flag_off_reproduces_the_backwards_future_window():
    iso, _gran = _date("I'm free for the next two weeks.", SPINE_TEMPORAL_UNIT_NOT_PLACE="false")
    assert iso is not None and iso[:10] < _REF.date().isoformat(), (
        f"the ORIGINAL backwards resolution no longer reproduces with the flag OFF; got {iso}")


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# G. BUG 2 — a CLOCK TIME is not a network PORT (the SHIPPED seed pattern, read off disk)
# ═════════════════════════════════════════════════════════════════════════════════════════════════

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
_PORT_MIGRATION = _MIGRATIONS / "198_atomic_port_pattern_not_a_clock_time.sql"


def _shipped_port_regex() -> str:
    """The has_port scalar_atomic regex AS SHIPPED in migration 198 — read off the migration file,
    never copied into this test, so the seed and this contract can never drift apart."""
    src = _PORT_MIGRATION.read_text()
    pats = re.findall(r"pattern_regex\s*=\s*'((?:[^']|'')*)'", src)
    # The migration carries the OLD regex only in its guard clauses; the NEW one is the long form.
    new = [p for p in pats if "?<=" in p]
    assert new, "migration 198 no longer contains a lookbehind-form has_port pattern"
    assert len(set(new)) == 1, f"migration 198 has DRIFTED — {len(set(new))} distinct new patterns"
    return new[0]


def test_port_migration_regex_compiles():
    re.compile(_shipped_port_regex())


# Clock times / ratios / colon-bearing non-ports. Every one of these matched the OLD pattern.
@pytest.mark.parametrize("text", [
    "[Date: 2023/05/22 (Mon) 22:17]",          # the session-date marker on EVERY bench turn
    "My meeting is at 14:30.",
    "I woke at 6:45 and left at 7:20.",
    "Standup is 09:15 sharp.",
    "The score was 3:1.",
    "The ratio is 16:9.",
    "Chapter 4:12 of the manual.",
    "MAC is 00:1a:2b:3c:4d:5e",
])
def test_a_clock_time_or_ratio_is_never_a_port(text):
    hits = [m.group(0) for m in re.finditer(_shipped_port_regex(), text)]
    assert hits == [], f"{text!r} still yields a phantom port {hits}"


# GENUINE ports must capture — and the captured value must be the BARE NUMBER, because
# `_detect_atomic_values` stores `m.group(0)` and migration 101 types has_port as `integer`.
@pytest.mark.parametrize("text,expected", [
    ("The server listens on port 8080.",   "8080"),
    ("Set Port 443 on the firewall.",      "443"),
    ("Open ports 80 and 443.",             "80"),
    ("Use port:22 for ssh.",               "22"),
    ("Connect to 192.168.1.50:8080",       "8080"),
    ("https://example.com:8443/api",       "8443"),
    ("db.internal.lan:5432 is the DSN",    "5432"),
])
def test_genuine_ports_capture_as_a_bare_integer(text, expected):
    hits = [m.group(0) for m in re.finditer(_shipped_port_regex(), text)]
    assert expected in hits, f"{text!r} lost its genuine port — got {hits}"
    for h in hits:
        assert h.isdigit(), f"port value {h!r} is not a bare integer (migration 101: has_port=integer)"
        assert 1 <= int(h) <= 65535, f"port {h} outside the RFC 6335 16-bit range"


@pytest.mark.parametrize("text", [
    "port 70000 is invalid",     # above the RFC 6335 ceiling
    "port 0 is reserved",        # the pattern has always excluded a leading zero
    "My IP is 192.168.1.50",     # a dotted quad is not a port
])
def test_out_of_range_and_non_port_numerics_are_refused(text):
    hits = [m.group(0) for m in re.finditer(_shipped_port_regex(), text)]
    assert hits == [], f"{text!r} minted a port it should not: {hits}"


def test_port_migration_only_touches_the_bootstrap_seed():
    """AUTHORITY ORDER (user > seed > growth). The migration must scope its UPDATE to the seeded
    row, so a tenant-grown or user-corrected pattern can never be overwritten by a seed fix."""
    src = _PORT_MIGRATION.read_text()
    assert src.count("source        = 'bootstrap'") + src.count("source = 'bootstrap'") >= 2, (
        "migration 198 must restrict its UPDATE to source='bootstrap' in BOTH the public seed and "
        "the per-tenant fan-out")
    assert "faultline\\_%" in src, "migration 198 must fan out to already-provisioned tenants"
