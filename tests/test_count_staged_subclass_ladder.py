"""COUNT/SUM over a CLASS-C-ONLY place-index ladder (bug COUNT-STAGED-LADDER).

Boundary being pinned: the hypernym TYPE-resolution descent in `_resolve_count_type_uuids`
must read the `subclass_of` place-index ladder from `facts` ∪ `staged_facts`, not `facts`
alone. The WordNet / Hearst ladders stage their `subclass_of` rungs at Class C in
`staged_facts` and, in a single-query context (LME: reset → ingest → one query, no C→B
promotion), they NEVER reach `facts`. A facts-only descent then resolves the hypernym to
ITSELF only, so "how many doctors" / "how much on bags" descends nothing and no-ops the
whole aggregation lever.

L4 (the subclass_of hierarchy) is the ENGINE-GROWN place index — a valid place edge
REGARDLESS of tier — so the TYPE descent SHOULD walk the Class-C rungs. This does NOT
loosen instance counting: the INSTANCES counted are still the real `instance_of` facts;
only the TYPE/hypernym RESOLUTION unions the staged place-ladder.

FAKE-CURSOR CONTRACT (avoids the dead-mock trap by dispatching on real SQL shape): the
subclass_of ladder rungs are returned ONLY when the descent SQL actually reads
`staged_facts`. So this test is RED on the pre-fix facts-only descent (the rungs are
invisible → subtree stays {root} → count 0 / sum miss) and GREEN once the descent unions
`staged_facts`. No network, no live services.
"""
import pytest

from src.api import main as m

# ── the graph (all UUID-shaped strings) ─────────────────────────────────────────
# subclass_of ladder — CLASS-C, exists ONLY in staged_facts (never promoted to facts):
#     dermatologist ─┐
#                    ├─ specialist ─┐
#     (spec directly)│              ├─ doctor
#     physician ─────────────────────┘
#     handbag ─ bag
DOC = "doctor-type-uuid"
SPEC = "specialist-type-uuid"
DERM = "dermatologist-type-uuid"
PHYS = "physician-type-uuid"
BAG = "bag-type-uuid"
HANDBAG = "handbag-type-uuid"

# subclass_of edges (child -> parent), the Class-C place-index ladder
_SUBCLASS = {
    DERM: SPEC,
    SPEC: DOC,
    PHYS: DOC,
    HANDBAG: BAG,
}

# instance_of edges (real captured facts): three distinct doctor instances, two handbags
D1, D2, D3 = "visit-1-uuid", "visit-2-uuid", "visit-3-uuid"
HB1, HB2 = "handbag-1-uuid", "handbag-2-uuid"
_INSTANCE_OF = {
    D1: DERM,   # instance_of dermatologist
    D2: PHYS,   # instance_of physician
    D3: SPEC,   # instance_of specialist
    HB1: HANDBAG,
    HB2: HANDBAG,
}
# numeric scalars carried by the handbag instances (for the SUM lane)
_SCALARS = {
    HB1: ("buy", None, "1200 dollars"),
    HB2: ("buy", None, "500 dollars"),
}

_ALIASES = {"doctor": DOC, "doctors": DOC, "bag": BAG, "bags": BAG}


class _FakeCursor:
    def __init__(self):
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = list(params) if params else []

        # 1) classification-rel lookup
        if "FROM rel_types" in s and "wikidata_pid = ANY" in s:
            self._result = [("instance_of",), ("subclass_of",), ("is_a",)]
            return
        # 2) alias grounding of the type phrase
        if "FROM entity_aliases" in s and "alias = ANY" in s and "entity_id" in s:
            cands = [str(c).lower() for c in (p[0] or [])]
            self._result = [(_ALIASES[c],) for c in cands if c in _ALIASES]
            return
        # 3) taxonomy overlay member admission (none in this fixture)
        if "FROM entity_taxonomies" in s:
            self._result = []
            return
        # The descent and the instance-set query share the SAME SQL SHAPE
        # (subject_id / object_id = ANY / rel_type = ANY) — the rel names live in the
        # PARAMS, not the SQL — so dispatch on the rel-param content, not SQL text.
        _rels = set()
        if "object_id = ANY" in s and "subject_id" in s and len(p) >= 2:
            _rels = {str(r).lower() for r in (p[1] or [])}
        # 4) subclass_of DESCENT — the ladder rungs are visible ONLY when staged_facts is read
        if _rels and "subclass_of" in _rels and "instance_of" not in _rels:
            frontier = set(p[0] or [])       # params[0] is the frontier object set
            if "staged_facts" not in s:
                # PRE-FIX facts-only descent: the Class-C rungs are invisible → no descent
                self._result = []
                return
            children = [child for child, parent in _SUBCLASS.items() if parent in frontier]
            self._result = [(c,) for c in children]
            return
        # 5) direct instance-set query (facts ∪ staged): subjects instance_of a type in ANY(types)
        if _rels and "instance_of" in _rels and "entity_attributes" not in s:
            types = set(p[0] or [])
            subs = [sub for sub, t in _INSTANCE_OF.items() if t in types]
            self._result = [(x,) for x in subs]
            return
        # 6) scalar fetch for the SUM lane
        if "FROM entity_attributes" in s and "entity_id = ANY" in s:
            ids = set(p[0] or [])
            self._result = [(eid, a, vi, vt) for eid, (a, vi, vt) in _SCALARS.items() if eid in ids]
            return
        self._result = []

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDB:
    def cursor(self):
        return _FakeCursor()


# ── the resolver descends the Class-C-only ladder ───────────────────────────────
def test_resolver_descends_class_c_only_subclass_ladder():
    """"doctor" grounds to {doctor} then descends the STAGED subclass_of rungs to
    include specialist / dermatologist / physician (all Class-C-only)."""
    out = m._resolve_count_type_uuids(_FakeDB(), "doctors")
    assert DOC in out, "root type must ground"
    # the fix: staged rungs are descended
    assert {SPEC, DERM, PHYS} <= out, (
        "expected the Class-C subclass_of ladder to be descended; got %r" % (out,))


# ── end-to-end: direct L4 count over the Class-C ladder → 3 (was 0) ─────────────
def test_direct_count_over_class_c_ladder_is_three():
    types = m._resolve_count_type_uuids(_FakeDB(), "doctors")
    res = m._l4_direct_instance_count(_FakeDB(), types, {"instance_of", "is_a"})
    assert res["miss"] is False
    assert res["count"] == 3, "three real instance_of facts sit under the ladder"
    assert set(res["instance_ids"]) == {D1, D2, D3}


# ── end-to-end: SUM over the Class-C ladder → 1700 (was miss) ────────────────────
def test_direct_sum_over_class_c_ladder_is_seventeen_hundred():
    types = m._resolve_count_type_uuids(_FakeDB(), "bags")
    assert HANDBAG in types, "handbag must be reached via the staged handbag->bag rung"
    res = m._l4_direct_scalar_sum(_FakeDB(), types, {"instance_of", "is_a"})
    assert res["miss"] is False
    assert res["sum"] == pytest.approx(1700.0)
    assert res["is_currency"] is True
    assert res["n_components"] == 2


# ── instances counted are REAL instance_of facts, not the place-ladder rungs ────
def test_no_loosening_place_rungs_are_not_counted_as_instances():
    """The subclass_of rung subjects (dermatologist / specialist / handbag TYPE nodes)
    must NOT be counted as instances — only instance_of subjects are."""
    types = m._resolve_count_type_uuids(_FakeDB(), "doctors")
    res = m._l4_direct_instance_count(_FakeDB(), types, {"instance_of", "is_a"})
    # the TYPE nodes themselves (DERM/SPEC/PHYS) are never in the counted instance set
    assert not ({DERM, SPEC, PHYS} & set(res["instance_ids"]))
