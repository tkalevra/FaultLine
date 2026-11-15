"""Unit tests for _collect_descendant_layered_facts — the hierarchy-descent completion
that closes "the walk reaches a node and stops instead of descending to the layered fact".

The DB cursor is FAKED (the helper is pure dispatch over cur.execute/fetchall): the fake
returns rows by matching the SQL the helper issues. This exercises the deterministic descent
logic, the depth bound, the scope projection, and the membership-axis filter WITHOUT a live
DB. DB-bound integration (real /query → MCP recall) is validated live, not here.

Covers:
  • descent collects a reached descendant's OWN layered fact (gps → has_state issue),
  • descent collects the descendant's scalar (firmware_version),
  • the OWNER-seed reaches the subtree (user → owns → car → part_of → gps),
  • depth bound stops the descent (deep chain truncated at depth_cap),
  • scope projection drops an off-scope descended relationship (state still admitted),
  • membership axis drops the descendant's classification chain,
  • every returned fact carries _hierarchy_admitted (survives the downstream projection),
  • determinism: same inputs → identical output across repeats.
"""
import datetime

import pytest

from src.api import main as m


class _FakeDate:
    """Mimics a DB datetime column: carries .isoformat() like psycopg2 returns."""
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


# ── Fake DB cursor ───────────────────────────────────────────────────────────
class FakeCursor:
    """Dispatches the helper's three SQL shapes off a small in-memory graph.

    graph_edges: list of (subject, rel_type, object, event_date) — both 'facts' and
        'staged_facts' are unified here (the helper UNIONs them; we don't distinguish).
    scalars: dict entity_id -> list of (attribute, value_text).
    """

    def __init__(self, graph_edges, scalars):
        self._edges = graph_edges
        self._scalars = scalars
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM facts WHERE object_id = ANY(%s)" in s:
            # descendant frontier walk: subjects whose object is in frontier via descent rels
            frontier, descent, *_ = params
            fset, dset = set(frontier), {d.lower() for d in descent}
            self._result = sorted({
                (e[0],) for e in self._edges
                if e[2] in fset and e[1].lower() in dset
            })
        elif "FROM facts WHERE subject_id = %s" in s:
            # the descended entity's own outbound facts
            did = params[0]
            self._result = [
                (e[0], e[2], e[1], 0.8, "B", (_FakeDate(e[3]) if e[3] else None))
                for e in self._edges if e[0] == did
            ]
        elif "FROM entity_attributes WHERE entity_id = %s" in s:
            did = params[0]
            self._result = [
                (did, attr, val, None, None, None)
                for (attr, val) in self._scalars.get(did, [])
            ]
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)


@pytest.fixture(autouse=True)
def _stub_metadata(monkeypatch):
    # has_state / feels are the event/state tier; category lookup is irrelevant to assertions.
    monkeypatch.setattr(m, "_get_event_state_rels", lambda: frozenset({"has_state", "feels"}))
    monkeypatch.setattr(m, "_get_rel_type_category", lambda r: None)


# ── The canonical GPS graph ──────────────────────────────────────────────────
# user --owns--> car ; gps --part_of--> car ; gps --has_state--> issue@2026-03-22
USER, CAR, GPS, ISSUE = "user-uuid", "car-uuid", "gps-uuid", "issue-uuid"
GPS_GRAPH = [
    (USER, "owns", CAR, None),
    (GPS, "part_of", CAR, None),
    (GPS, "has_state", ISSUE, "2026-03-22"),
]
CLASSIF = frozenset({"instance_of", "subclass_of", "is_a"})
DESCENT = ["part_of", "member_of"]


def _states(facts):
    return [f for f in facts if f["rel_type"] == "has_state"]


def test_descent_from_whole_surfaces_component_state():
    """anchor = car → descend part_of → gps → collect (gps, has_state, issue)."""
    cur = FakeCursor(GPS_GRAPH, {})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
    )
    st = _states(out)
    assert len(st) == 1
    assert st[0]["subject"] == GPS and st[0]["object"] == ISSUE
    assert st[0]["event_date"] == "2026-03-22"
    assert st[0]["_hierarchy_admitted"] is True


def test_owner_seed_reaches_component_state():
    """anchor = user (the OWNER) → seed with car (owns) → descend → gps has_state.

    This is the "regardless of how it was asked" case: a user-anchored question reaches
    the same layered fact via the owns-seed, not only the car-anchored question.
    """
    cur = FakeCursor(GPS_GRAPH, {})
    out = m._collect_descendant_layered_facts(
        cur, USER, DESCENT, CLASSIF, query_axis="classification", depth_cap=4,
        scope_rels=None, seed_entities={CAR},
    )
    st = _states(out)
    assert len(st) == 1 and st[0]["subject"] == GPS and st[0]["object"] == ISSUE


def test_seed_that_is_also_descendant_surfaces_its_state():
    """REGRESSION (seed/visited collision — the live GPS-litmus bug).

    When the component is BOTH a direct seed of the anchor (user owns car AND owns gps — a
    redundant direct edge the extractor captured at ingest) AND the genuine part_of descendant
    of another seed (gps part_of car), the pre-visited seed must STILL have its own has_state
    collected. The old code pre-marked every seed ``_visited`` and only collected facts for
    freshly-discovered ``_next`` entities, so ``gps`` (already visited as a seed) was subtracted
    from ``_next`` when the descent legitimately re-found it under ``car`` → its
    ``(gps, has_state, issue)`` was silently dropped. The up-front seed pre-collection fixes it.
    Also guards against DOUBLE-emit (exactly one state row, not two)."""
    graph = GPS_GRAPH + [(USER, "owns", GPS, None)]  # the spurious direct owns edge → gps is a seed
    cur = FakeCursor(graph, {})
    out = m._collect_descendant_layered_facts(
        cur, USER, DESCENT, CLASSIF, query_axis="classification", depth_cap=4,
        scope_rels=None, seed_entities={CAR, GPS},   # gps is ALSO a direct seed
    )
    st = _states(out)
    assert len(st) == 1, f"expected exactly one has_state (no drop, no double), got {len(st)}"
    assert st[0]["subject"] == GPS and st[0]["object"] == ISSUE


def test_seed_collision_component_scalar_surfaces():
    """Same collision, scalar variant: a seed-that-is-also-a-descendant's scalar surfaces once."""
    graph = GPS_GRAPH + [(USER, "owns", GPS, None)]
    cur = FakeCursor(graph, {GPS: [("firmware_version", "4.2")]})
    out = m._collect_descendant_layered_facts(
        cur, USER, DESCENT, CLASSIF, query_axis="classification", depth_cap=4,
        scope_rels=None, seed_entities={CAR, GPS},
    )
    scal = [f for f in out if f.get("source") == "attributes" and f["rel_type"] == "firmware_version"]
    assert len(scal) == 1 and scal[0]["object"] == "4.2"


def test_owner_without_seed_does_not_reach_component():
    """No seed → from the user alone, no part_of descendant exists → no state surfaced.

    Proves the seed is what bridges the owns hop (the descent itself only walks composition).
    """
    cur = FakeCursor(GPS_GRAPH, {})
    out = m._collect_descendant_layered_facts(
        cur, USER, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
    )
    assert _states(out) == []


def test_descent_collects_component_scalar():
    """A component's scalar (firmware_version) surfaces via the same descent (subject-agnostic)."""
    cur = FakeCursor(GPS_GRAPH, {GPS: [("firmware_version", "4.2")]})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
    )
    scal = [f for f in out if f.get("source") == "attributes"]
    assert any(f["rel_type"] == "firmware_version" and f["object"] == "4.2" for f in scal)
    assert all(f["_hierarchy_admitted"] for f in scal)


def test_depth_bound_truncates_deep_chain():
    """A 3-deep part_of chain under car is truncated by depth_cap=1 (only the first hop)."""
    A, B, C = "a-uuid", "b-uuid", "c-uuid"
    graph = [
        (A, "part_of", CAR, None),  # depth 1
        (B, "part_of", A, None),    # depth 2
        (C, "part_of", B, None),    # depth 3
        (A, "has_state", "sa", None),
        (B, "has_state", "sb", None),
        (C, "has_state", "sc", None),
    ]
    cur = FakeCursor(graph, {})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=1, scope_rels=None,
    )
    subs = {f["subject"] for f in _states(out)}
    assert subs == {A}                 # only the depth-1 descendant collected
    assert B not in subs and C not in subs


def test_scope_projection_drops_offscope_rel_but_keeps_state():
    """Under an ACTIVE concept scope, an off-scope descended relationship is dropped, but the
    descended EVENT/STATE edge (has_state) is still admitted on the temporal tier."""
    graph = GPS_GRAPH + [(GPS, "lives_in", "somewhere", None)]  # off-scope relationship
    cur = FakeCursor(graph, {})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4,
        scope_rels={"part_of", "owns"},  # has_state NOT in scope; lives_in NOT in scope
    )
    rels = {f["rel_type"] for f in out}
    assert "has_state" in rels          # state tier admits it despite being off-scope
    assert "lives_in" not in rels       # off-scope plain relationship dropped (no bleed)


def test_membership_axis_drops_classification_chain():
    """A descendant's classification chain (gps instance_of device) is dropped on a
    membership query — a different question than the descendant's state."""
    graph = GPS_GRAPH + [(GPS, "instance_of", "device", None)]
    cur = FakeCursor(graph, {})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
    )
    rels = {f["rel_type"] for f in out}
    assert "instance_of" not in rels
    assert "has_state" in rels


def test_all_returned_facts_are_hierarchy_admitted():
    cur = FakeCursor(GPS_GRAPH, {GPS: [("firmware_version", "4.2")]})
    out = m._collect_descendant_layered_facts(
        cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
    )
    assert out and all(f.get("_hierarchy_admitted") for f in out)


def test_descent_is_deterministic_across_repeats():
    def run():
        cur = FakeCursor(GPS_GRAPH, {GPS: [("firmware_version", "4.2")]})
        out = m._collect_descendant_layered_facts(
            cur, CAR, DESCENT, CLASSIF, query_axis="membership", depth_cap=4, scope_rels=None,
        )
        return sorted((f["rel_type"], str(f.get("object"))) for f in out)
    first = run()
    for _ in range(5):
        assert run() == first


def test_empty_inputs_safe():
    cur = FakeCursor(GPS_GRAPH, {})
    assert m._collect_descendant_layered_facts(cur, "", DESCENT, CLASSIF, None, 4) == []
    assert m._collect_descendant_layered_facts(cur, CAR, [], CLASSIF, None, 4) == []
    assert m._collect_descendant_layered_facts(cur, CAR, DESCENT, CLASSIF, None, 0) == []
