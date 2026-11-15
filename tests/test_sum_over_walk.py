"""SUM-OVER-WALK — the aggregation sibling of COUNT-OVER-WALK.

Deterministic Σ of the numeric ``entity_attributes`` scalars carried by a type's instances:
"how much total money on bikes" / "how many days on camping trips" → Σ, not len. Mirrors the
count lane's design (detect intent → resolve type via alias+subclass_of → direct L4 read → fail
loud on a miss, never a fabricated number). These tests exercise the PURE functions with a fake
cursor that dispatches on the SQL shape (avoids the dead-mock trap), so they need no DB/brain.

Also covers the owner-directed TAXONOMY-OVERLAY member admission added to
``_resolve_count_type_uuids`` (a group's declared member types count under the group hypernym).
"""
import pytest
from src.api import main as m


# ─────────────────────────── _detect_sum_intent ───────────────────────────

def test_sum_intent_money_shape():
    out = m._detect_sum_intent(
        "How much total money have I spent on bike-related expenses since the start of the year?")
    assert out.get("type_phrase") == "bike-related expenses"
    assert "money" in out.get("measure_phrase", "")
    assert out.get("since") and "start of the year" in out["since"]


def test_sum_intent_duration_shape():
    out = m._detect_sum_intent(
        "How many days did I spend on camping trips in the United States this year?")
    # the trailing temporal scope ("this year") is peeled off the TYPE phrase
    assert out.get("type_phrase") == "camping trips in the united states"
    assert out.get("measure_phrase") == "days"


def test_sum_intent_aggregation_cued_variant_fires():
    # AGGREGATION-CUED duration shape: the cue sits between unit↔pronoun, TWO verbs precede the
    # object ("spend driving"), and the object preposition is "to". The default shape cannot reach
    # it; the cue-gated broadened branch must (LME aae3761f). Trailing "combined" is peeled.
    out = m._detect_sum_intent(
        "How many hours in total did I spend driving to my three road trip destinations combined?")
    assert out.get("type_phrase") == "my three road trip destinations"   # trailing "combined" peeled
    assert out.get("measure_phrase") == "hours"


def test_sum_intent_to_object_requires_aggregation_cue():
    # The broadened "to"-object / multi-verb branch is GATED on an explicit aggregation cue, so a
    # plain "how many hours did I drive to work" (no cue, "to" object) NEVER enters it → declines.
    assert m._detect_sum_intent("How many hours did I drive to work?") == {}


def test_sum_intent_declines_took_me_to_complete_frame():
    # "how many weeks did it take me to watch …" is the DURATION-TO-COMPLETE frame (expletive "it",
    # to-infinitive completion), owned by the temporal lane — not a per-instance scalar SUM (e831120c).
    assert m._detect_sum_intent(
        "How many weeks did it take me to watch all the Marvel movies and the Star Wars films?") == {}


def test_sum_intent_declines_plain_recall():
    assert m._detect_sum_intent("Where do I live?") == {}
    assert m._detect_sum_intent("Tell me about my family.") == {}


def test_sum_intent_declines_interval_between():
    # "how long between X and Y" is the temporal-CALC interval lane, NOT a SUM (no on/for TYPE).
    assert m._detect_sum_intent("How long between my wedding and my graduation?") == {}


def test_sum_intent_failsafe_empty():
    assert m._detect_sum_intent("") == {}
    assert m._detect_sum_intent(None) == {}


def test_count_and_sum_are_mutually_exclusive_on_the_two_shapes():
    # The COUNT detector deliberately DECLINES the aggregation shapes the SUM detector claims,
    # so the query router never runs both on one question.
    q_money = "How much money have I spent on bikes in total?"
    q_days = "How many days did I spend on camping trips?"
    assert m._detect_count_intent(q_money) == {}   # "in total" → declined by count
    assert m._detect_count_intent(q_days) == {}    # duration unit → declined by count
    assert m._detect_sum_intent(q_days).get("type_phrase") == "camping trips"


# ─────────────────────────── _parse_scalar_magnitude ───────────────────────────

@pytest.mark.parametrize("vint,vtext,exp_mag,exp_cur", [
    (5, "5", 5.0, False),
    (None, "3-day", 3.0, False),
    (None, "6 hours", 6.0, False),
    (None, "$25", 25.0, True),
    (None, "$120", 120.0, True),
    (None, "1200 dollars", 1200.0, True),
    (None, "1,250", 1250.0, False),
    (40, None, 40.0, False),
])
def test_parse_scalar_magnitude(vint, vtext, exp_mag, exp_cur):
    mag, cur = m._parse_scalar_magnitude(vint, vtext)
    assert mag == exp_mag
    assert cur is exp_cur


def test_parse_scalar_magnitude_non_numeric_is_none():
    assert m._parse_scalar_magnitude(None, "blue") == (None, False)
    assert m._parse_scalar_magnitude(None, "") == (None, False)
    assert m._parse_scalar_magnitude(None, None) == (None, False)


# ─────────────────────────── _l4_direct_scalar_sum ───────────────────────────

CAMP = "camping-trip-type-uuid"
INSTS = ["trip-a", "trip-b"]


class SumCursor:
    """Dispatches the two SQL shapes _l4_direct_scalar_sum issues, in order, on one cursor."""
    def __init__(self, inst_ids, attr_rows):
        self._inst_ids = inst_ids
        self._attr_rows = attr_rows
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM facts" in s and "UNION" in s and "object_id = ANY" in s:
            self._result = [(i,) for i in self._inst_ids]           # instance-set query
        elif "FROM entity_attributes" in s and "entity_id = ANY" in s:
            self._result = list(self._attr_rows)                    # scalar query
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SumDB:
    def __init__(self, inst_ids, attr_rows):
        self._inst_ids = inst_ids
        self._attr_rows = attr_rows

    def cursor(self):
        return SumCursor(self._inst_ids, self._attr_rows)


def test_sum_durations_across_instances():
    # (entity_id, attribute, value_int, value_text, provenance) — the scalar query selects
    # provenance since 2e39a7c5 (worst-of-set authorship of the total).
    rows = [("trip-a", "duration", None, "3-day", "user_stated"), ("trip-b", "duration", 5, "5-day", "user_stated")]
    out = m._l4_direct_scalar_sum(SumDB(INSTS, rows), {CAMP}, {"instance_of", "is_a"})
    assert out["miss"] is False
    assert out["sum"] == 8.0            # 3 + 5 = the gold "8 days"
    assert out["is_currency"] is False
    assert out["n_components"] == 2
    assert out["provenance"] == "user_stated"


def test_sum_money_flags_currency():
    rows = [("x", "cost", None, "$25", "user_stated"), ("y", "buy", None, "$120", "user_stated"), ("z", "amount", None, "$40", "user_stated")]
    out = m._l4_direct_scalar_sum(SumDB(["x", "y", "z"], rows), {"bike-type"}, {"instance_of"})
    assert out["miss"] is False
    assert out["sum"] == 185.0          # 25 + 120 + 40 = the gold "$185"
    assert out["is_currency"] is True
    assert out["n_components"] == 3


def test_sum_empty_type_is_miss():
    out = m._l4_direct_scalar_sum(SumDB(INSTS, []), set(), {"instance_of"})
    assert out["miss"] is True and out["sum"] == 0.0


def test_sum_no_numeric_scalar_is_miss_not_zero():
    # instances exist but carry no parseable numeric scalar → MISS (fail loud), never a false 0.
    rows = [("trip-a", "color", None, "blue", "user_stated")]
    out = m._l4_direct_scalar_sum(SumDB(INSTS, rows), {CAMP}, {"instance_of"})
    assert out["miss"] is True and out["n_instances"] == 2


def test_sum_no_instances_is_miss():
    out = m._l4_direct_scalar_sum(SumDB([], []), {CAMP}, {"instance_of"})
    assert out["miss"] is True


def test_sum_failsafe_on_db_error():
    class Boom:
        def cursor(self):
            raise RuntimeError("db down")
    out = m._l4_direct_scalar_sum(Boom(), {CAMP}, {"instance_of"})
    assert out["miss"] is True and out["sum"] == 0.0  # fail-safe, never fabricated


# ─────────── _resolve_count_type_uuids TAXONOMY member admission (owner steer) ───────────

class ResolveCursor:
    """Fake for _resolve_count_type_uuids: exact-alias lookup, taxonomy member lookup, subclass descent."""
    def __init__(self, alias_map, taxonomy_members):
        self._alias_map = {k.lower(): v for k, v in alias_map.items()}
        self._taxo = taxonomy_members  # phrase(lower) -> [member type names]
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM entity_aliases WHERE alias = ANY" in s:
            cands = params[0]
            ids = []
            for c in cands:
                ids += self._alias_map.get(str(c).lower(), [])
            self._result = [(i,) for i in ids]
        elif "FROM entity_taxonomies" in s:
            cands = params[0]
            members = []
            for c in cands:
                members += self._taxo.get(str(c).lower(), [])
            self._result = [(members,)] if members else []
        elif "FROM entity_aliases WHERE lower(alias) = ANY" in s:
            names = params[0]
            ids = []
            for n in names:
                ids += self._alias_map.get(str(n).lower(), [])
            self._result = [(i,) for i in ids]
        elif "FROM facts" in s and "rel_type = ANY" in s:
            self._result = []  # no subclass descent in these fixtures
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ResolveDB:
    def __init__(self, alias_map, taxonomy_members):
        self._a = alias_map
        self._t = taxonomy_members

    def cursor(self):
        return ResolveCursor(self._a, self._t)


def test_resolve_admits_taxonomy_member_types(monkeypatch):
    # "doctor" names a taxonomy whose declared member types are physician + specialist; those
    # type nodes must be admitted even though no subclass_of(physician, doctor) edge exists.
    monkeypatch.setattr(m, "_get_classification_rels", lambda db: frozenset({"subclass_of", "instance_of"}))
    alias_map = {
        "doctor": ["doctor-node"],
        "physician": ["physician-node"],
        "specialist": ["specialist-node"],
    }
    taxo = {"doctor": ["physician", "specialist"]}
    out = m._resolve_count_type_uuids(ResolveDB(alias_map, taxo), "doctors")
    assert "doctor-node" in out
    assert "physician-node" in out and "specialist-node" in out


def test_resolve_no_taxonomy_is_l4_only(monkeypatch):
    # A phrase that names no taxonomy resolves ONLY its own node (today's L4-only behaviour).
    monkeypatch.setattr(m, "_get_classification_rels", lambda db: frozenset({"subclass_of", "instance_of"}))
    out = m._resolve_count_type_uuids(ResolveDB({"tank": ["tank-node"]}, {}), "tanks")
    assert out == {"tank-node"}


def test_resolve_strips_distinctness_qualifier(monkeypatch):
    # "different doctors" must ground to the "doctor" TYPE — the count-distinctness modifier
    # qualifies the count, not the type (LME a3838d2b / gpt4_f2262a51 shape).
    monkeypatch.setattr(m, "_get_classification_rels", lambda db: frozenset({"subclass_of", "instance_of"}))
    out = m._resolve_count_type_uuids(ResolveDB({"doctor": ["doctor-node"]}, {}), "different doctors")
    assert out == {"doctor-node"}


# ─────────── _build_sum_over_walk_fact — success-path class binding (aggregation crash fix) ───────────
# REGRESSION (aggregation cluster): the inline sum-render bound `_sm_class` ONLY on the miss
# branch, so a SUCCESSFUL total raised `UnboundLocalError: _sm_class` and crashed the whole
# /query handler → empty recall. Verified LIVE on pre-prod: with 3 clean typed instances carrying
# duration scalars, `_l4_direct_scalar_sum` returned {sum:15.0,n:3} yet /query returned
# {"error":"cannot access local variable '_sm_class'…"} and recall said "no information". These
# pin the extracted pure helper so a grounded total ASSERTS (Class A) and actually surfaces.

def test_sum_fact_currency_success_asserts_class_a():
    res = {"type_phrase": "bike-related expenses", "measure_phrase": "total money",
           "miss": False, "sum": 185.0, "is_currency": True, "n_components": 3,
           "provenance": "user_stated"}
    fact = m._build_sum_over_walk_fact(res, "user")
    assert fact is not None
    assert fact["fact_class"] == "A"                 # was UnboundLocalError on the old inline path
    assert "$185" in fact["definition"] and "$185" in fact["object"]
    assert fact["confidence"] == 1.0


def test_sum_fact_duration_success_asserts_class_a():
    res = {"type_phrase": "road trip destinations", "measure_phrase": "hours",
           "miss": False, "sum": 15.0, "is_currency": False, "n_components": 3,
           "provenance": "user_stated"}
    fact = m._build_sum_over_walk_fact(res, "user")
    assert fact is not None
    assert fact["fact_class"] == "A"
    assert "15 hours" in fact["definition"] and "15 hours" in fact["object"]


def test_sum_fact_fractional_success_keeps_decimal():
    res = {"type_phrase": "workouts", "measure_phrase": "hours",
           "miss": False, "sum": 0.5, "is_currency": False, "n_components": 2,
           "provenance": "user_stated"}
    fact = m._build_sum_over_walk_fact(res, "user")
    assert fact["fact_class"] == "A"
    assert "0.5 hours" in fact["definition"]         # gold 0.5 (jog+yoga) renders un-rounded


def test_sum_fact_without_constituent_provenance_is_not_asserted():
    """2e39a7c5 (EP r2): a total whose constituents carry no stored provenance is NOT user truth
    — it resolves through the canonical resolver as llm_inferred and is never Class A. The three
    success pins above carry an all-user-stated set (what _l4_direct_scalar_sum now returns)."""
    res = {"type_phrase": "workouts", "measure_phrase": "hours",
           "miss": False, "sum": 4.0, "is_currency": False, "n_components": 2}
    fact = m._build_sum_over_walk_fact(res, "user")
    assert fact["fact_class"] != "A"
    assert fact["fact_provenance"] == "llm_inferred"


def test_sum_fact_miss_holds_class_c():
    res = {"type_phrase": "bike-related expenses", "measure_phrase": "money", "miss": True,
           "sum": 0.0, "is_currency": False, "n_components": 0, "n_instances": 0}
    fact = m._build_sum_over_walk_fact(res, "user")
    assert fact["fact_class"] == "C"
    assert "don't have any" in fact["definition"] and fact["confidence"] == 0.5


def test_sum_fact_none_when_no_intent():
    assert m._build_sum_over_walk_fact(None, "user") is None
    assert m._build_sum_over_walk_fact({}, "user") is None


# ─────────── _count_type_candidates — possessive + cardinal peel (LME aae3761f scoping) ───────────
# The real question names the TYPE with a possessive determiner + cardinal ("my three road trip
# destinations"). Those qualify the aggregation SCOPE (whose / how-many), never the TYPE noun-group,
# so the type must ground to "road trip destination(s)". Old code left "my three …" in and MISSED.

def test_count_candidates_peel_possessive_and_cardinal():
    cands = m._count_type_candidates("my three road trip destinations")
    assert "road trip destination" in cands       # singular head, peeled
    assert "road trip destinations" in cands       # raw peeled phrase
    # the untouched original is still kept as a fallback candidate
    assert "my three road trip destinations" in cands


def test_count_candidates_peel_possessive_only():
    cands = m._count_type_candidates("my expedition runs")
    assert "expedition run" in cands and "expedition runs" in cands


def test_count_candidates_no_quantifier_unchanged():
    # A bare type phrase is unaffected (no possessive/cardinal to peel).
    cands = m._count_type_candidates("bike-related expenses")
    assert "bike-related expense" in cands and "bike-related expenses" in cands
