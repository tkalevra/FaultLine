"""Duration-SUM aggregation capture + query grounding (LME aae3761f / 7024f17c).

Gold (aae3761f): "How many hours in total did I spend driving to my three road trip
destinations combined?" → "15 hours".  The haystack states each trip's driving time in a
"my <trip> to <dest> took N hours" measure-verb frame; the answer is the UNIT-NORMALIZED
SUM of the per-trip durations (4 + 5 + 6 = 15).

INGEST ROOT CAUSE (rule 8 — a SUM is not a boundary, it was an ingest+grounding defect):
  1. CAPTURE — a bare "X took N hours" (no completion xcomp) minted an OPAQUE verb-lemma
     STRING scalar (`(trip, take, "about four hours")`), non-numeric + collided across trips,
     while the possessive-attribute twin `(user, last_trip, "about four hours")` polluted
     rel_types.  Nothing summable, nothing typed.  FIX (linguistics `_chain_verb_measure`
     clock-duration lane, flag DURATION_MEASURE_SCALAR): route a CLOCK-duration measure to the
     SEEDED `duration` scalar, UNIT-NORMALIZED to hours ("about four hours"→"4 hours",
     "90 minutes"→"1.5 hours"), on the destination-folded journey instance, and TYPE that
     instance `instance_of <head noun>` so the sum walk finds it; the attr-scalar possessed-
     measure twin DEFERS for the goal-PP trip shape (no rel-type pollution; commute unchanged).
  2. QUERY GROUNDING — the SUM lane (`_l4_direct_scalar_sum`) resolves the type via
     `_resolve_count_type_uuids`, whose head-noun fallback took only the LAST token
     ("destinations"→"destination"), which grounds NOTHING; the measured instances are typed
     `instance_of trip`.  FIX (main `_resolve_count_type_uuids` INTERIOR-noun fallback): when
     the full phrase AND its head both miss, try each interior token — "road trip destinations"
     reaches the registered "trip" the instances carry.

FAIL-ON-OLD:
  * capture — with DURATION_MEASURE_SCALAR off the deriver emits NO `duration` scalar (a generic
    "take" string) and NO `instance_of`; the assertions fail on the pre-fix path.
  * grounding — the old head-noun-only fallback resolves "destination" to nothing → empty type
    set → the sum MISSES.  The interior fallback grounds "trip".

Evidence-ground: ISO-8601-2 / OWL-Time compound duration; dimensional analysis (sum only like-
normalized magnitudes); Williams (1981) right-hand-head rule (the interior noun is a valid type
head of the query-time descriptor).  Deterministic, subject-agnostic, no domain word-list.
"""
import os

import pytest

# Pure deriver pins, written and verified with no database: run them on the in-code cue floors
# explicitly (every whole-suite run used to, by accident of a leaked DSN pop, #63). The
# divergence WITH the seeded cue tables is a product question, #75.
pytestmark = pytest.mark.usefixtures("no_tenant_db")

from src.extraction import linguistics
from src.extraction.linguistics import (
    duration_phrase_to_hours, _spelled_number_to_float, derive_sentence_facts)
from src.api import main


_REF = None  # the deriver tolerates a None reference for these non-dated frames


def _facts(sentence):
    return derive_sentence_facts(sentence, _REF, None)


def _find(facts, rel):
    return [f for f in facts if str(f.rel_type) == rel]


# ── helper arithmetic: clock-duration → unit-normalized hours ────────────────────
def test_duration_phrase_to_hours_normalizes_units():
    assert duration_phrase_to_hours("about four hours") == 4.0
    assert duration_phrase_to_hours("four hours") == 4.0
    assert duration_phrase_to_hours("6 hours") == 6.0
    # MIXED-UNIT normalization: minutes → hours (the load-bearing "sum only like units" case)
    assert duration_phrase_to_hours("90 minutes") == 1.5
    assert duration_phrase_to_hours("30 minutes") == 0.5
    assert duration_phrase_to_hours("half an hour") == 0.5
    assert duration_phrase_to_hours("an hour and a half") == 1.5
    # NEVER fabricate: a vague quantifier / a NON-clock (calendar) unit → None
    assert duration_phrase_to_hours("a few hours") is None
    assert duration_phrase_to_hours("two weeks") is None
    assert duration_phrase_to_hours("") is None


def test_spelled_number_to_float():
    assert _spelled_number_to_float("four") == 4.0
    assert _spelled_number_to_float("twenty five") == 25.0
    assert _spelled_number_to_float("4.5") == 4.5
    assert _spelled_number_to_float("half") == 0.5
    assert _spelled_number_to_float("few") is None


# ── CAPTURE: "my <trip> to <dest> took N hours" → duration scalar + instance_of (FAIL-ON-OLD) ─
@pytest.mark.skipif(not linguistics.linguistics_available(),
                    reason="spaCy model required (set SPACY_MODEL=en_core_web_sm)")
def test_trip_duration_scalar_normalized_and_typed():
    facts = _facts("My last trip to Outer Banks only took about four hours.")
    durs = _find(facts, "duration")
    assert durs, f"no duration scalar (old code → generic 'take' string): {[(f.subject, f.rel_type, f.object) for f in facts]}"
    d = durs[0]
    # destination-folded, DISTINCT-per-trip host
    assert d.subject == "last trip to outer banks", d.subject
    # UNIT-NORMALIZED to hours, with a parseable DIGIT (so the SUM can total it)
    assert d.object == "4 hours", d.object
    assert (d.scalar_datatype or "") == "duration"
    # the instance is TYPED so the sum's instance_of walk reaches it
    assert any(f.rel_type == "instance_of" and f.subject == "last trip to outer banks"
               and f.object == "trip" for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]
    # NO opaque verb-lemma string scalar twin for this construction
    assert not _find(facts, "take"), [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.skipif(not linguistics.linguistics_available(), reason="spaCy model required")
def test_minutes_trip_duration_normalizes_to_hours():
    """A MIXED unit ("90 minutes") normalizes to a common hours magnitude so a cross-unit sum
    is dimensionally correct."""
    facts = _facts("The drive to Tybee Island took 90 minutes.")
    durs = _find(facts, "duration")
    assert durs and durs[0].object == "1.5 hours", [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.mark.skipif(not linguistics.linguistics_available(), reason="spaCy model required")
def test_capture_flag_off_is_byte_identical_old_behaviour():
    """FAIL-ON-OLD proof: with the flag OFF the duration lane never fires — the old generic
    verb-lemma string scalar is emitted and NO `duration` / instance_of typing exists."""
    _prev = linguistics.DURATION_MEASURE_SCALAR
    linguistics.DURATION_MEASURE_SCALAR = False
    try:
        facts = _facts("My last trip to Outer Banks only took about four hours.")
        assert not _find(facts, "duration"), [(f.subject, f.rel_type, f.object) for f in facts]
        # the pre-fix path minted the opaque verb-lemma scalar instead
        assert _find(facts, "take"), [(f.subject, f.rel_type, f.object) for f in facts]
    finally:
        linguistics.DURATION_MEASURE_SCALAR = _prev


@pytest.mark.skipif(not linguistics.linguistics_available(), reason="spaCy model required")
def test_commute_measure_not_regressed():
    """The possessed-measure attr-scalar chain still owns a NON-goal measure ("my commute
    takes 45 minutes") — the deferral is narrow (goal-PP trip shape only)."""
    facts = _facts("My daily commute takes 45 minutes.")
    # attr-scalar user-anchored scalar still present (no goal PP → not deferred)
    assert any(f.rel_type == "daily_commute" and f.subject == "user" for f in facts), \
        [(f.subject, f.rel_type, f.object) for f in facts]


# ── QUERY: SUM-intent routing + render + magnitude parse ─────────────────────────
def test_sum_intent_routes_the_real_question():
    intent = main._detect_sum_intent(
        "how many hours in total did i spend driving to my three road trip destinations combined?")
    assert intent, intent
    assert intent.get("measure_phrase") == "hours", intent
    assert "road trip destinations" in (intent.get("type_phrase") or ""), intent


def test_scalar_magnitude_parses_normalized_hours():
    assert main._parse_scalar_magnitude(None, "4 hours") == (4.0, False)
    assert main._parse_scalar_magnitude(None, "1.5 hours") == (1.5, False)


def test_sum_render_and_miss_fail_loud():
    ok = main._build_sum_over_walk_fact(
        {"type_phrase": "road trip destinations", "measure_phrase": "hours",
         "miss": False, "sum": 15.0, "is_currency": False}, "you")
    assert ok["fact_class"] == "A" and "15 hours" in ok["definition"], ok
    miss = main._build_sum_over_walk_fact(
        {"type_phrase": "road trip destinations", "measure_phrase": "hours", "miss": True}, "you")
    assert miss["fact_class"] == "C" and "amounts on record" in miss["definition"], miss


# ── QUERY: interior-noun grounding + Σ over the real function set (fake cursor) ───
class _FakeCur:
    """Minimal cursor: routes each SELECT to a canned result by SQL fragment + params, mirroring
    the committed shape from the live end-to-end (3 trip instances instance_of trip, each with a
    numeric `duration` scalar). Proves _resolve_count_type_uuids reaches the type ONLY via the
    interior-noun fallback and _l4_direct_scalar_sum totals 4+5+6 = 15."""
    T = "T-uuid"
    INSTS = ["I1", "I2", "I3"]

    def __init__(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        p = params or []
        self._rows = []
        if "from rel_types" in s and "wikidata_pid" in s:
            self._rows = [("instance_of",), ("subclass_of",), ("is_a",)]
        elif "from entity_aliases where alias = any" in s:
            aliases = [a.lower() for a in (p[0] if p else [])]
            # the type node is registered ONLY as "trip" — reachable via the interior token,
            # NEVER via the full phrase or the "destination" head-noun try.
            self._rows = [(self.T,)] if "trip" in aliases else []
        elif "from entity_taxonomies" in s:
            self._rows = []
        elif "select distinct subject_id from facts" in s:
            # instance_of walk (sum) OR subclass_of descent (type resolve). Only the instance_of
            # walk over the trip type returns instances; subclass_of returns nothing.
            rels = [r.lower() for r in (p[1] if len(p) > 1 else [])]
            objs = [o for o in (p[0] if p else [])]
            if self.T in objs and "instance_of" in rels:
                self._rows = [(i,) for i in self.INSTS]
            else:
                self._rows = []
        elif "from entity_attributes where entity_id = any" in s:
            self._rows = [("I1", "duration", None, "4 hours"),
                          ("I2", "duration", None, "5 hours"),
                          ("I3", "duration", None, "6 hours")]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def cursor(self):
        return _FakeCur()


def test_interior_fallback_grounds_trip_and_sums_15():
    db = _FakeDB()
    # FAIL-ON-OLD: the head-noun-only fallback resolves "destination" → nothing. The interior
    # fallback reaches "trip" (the registered type the instances carry).
    types = main._resolve_count_type_uuids(db, "my three road trip destinations")
    assert types == {_FakeCur.T}, types
    res = main._l4_direct_scalar_sum(db, types, {"instance_of", "is_a"})
    assert not res["miss"], res
    assert res["sum"] == 15.0 and res["n_components"] == 3, res


def test_interior_fallback_flag_off_misses():
    """Flag OFF (COUNT_HEAD_NOUN_FALLBACK) → neither head nor interior fires → type MISS → the
    sum fails loud (never a fabricated total)."""
    _prev = os.environ.get("COUNT_HEAD_NOUN_FALLBACK")
    os.environ["COUNT_HEAD_NOUN_FALLBACK"] = "0"
    try:
        db = _FakeDB()
        types = main._resolve_count_type_uuids(db, "my three road trip destinations")
        assert types == set(), types
        res = main._l4_direct_scalar_sum(db, types, {"instance_of", "is_a"})
        assert res["miss"], res
    finally:
        if _prev is None:
            os.environ.pop("COUNT_HEAD_NOUN_FALLBACK", None)
        else:
            os.environ["COUNT_HEAD_NOUN_FALLBACK"] = _prev
