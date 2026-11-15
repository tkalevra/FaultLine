"""COUNT-OVER-WALK — cardinality "how many <TYPE> …" as a THIN RENDER over the walk.

Architecture under test (owner, non-negotiable): a count is NOT a compute operator. If
L4 typing is strong at ingest, the deterministic walk ALREADY collects the distinct typed
instances under the anchor. "How many tanks do I have" is just the CARDINALITY (len) of
that already-collected distinct instance set. So these tests VALIDATE THE WALK: each builds
a small typed-instance graph (the fact dicts the walk would produce — instance_of edges
hanging leaf instances off their L4 type) and asserts the count renders correctly.

Pure-Python: the detection / singularization / counting functions take no DB; the type
resolver is exercised against a MagicMock cursor. No network, no live services.
"""

import re
from unittest.mock import MagicMock

import pytest

from src.api.main import (
    _detect_count_intent,
    _singularize_noun,
    _singularize_phrase,
    _apply_instance_count,
    _resolve_count_type_uuids,
)

_INSTANCE_RELS = frozenset({"instance_of", "is_a"})


# ── helpers ────────────────────────────────────────────────────────────────────
def _instance_edge(inst_uuid: str, type_uuid: str, name: str = None) -> dict:
    """One (instance, instance_of, type) fact dict, shaped like the walk produces it."""
    return {
        "subject": name or inst_uuid,
        "object": "the-type-display",
        "rel_type": "instance_of",
        "_subject_id": inst_uuid,
        "_object_id": type_uuid,
        "fact_class": "A",
        "confidence": 1.0,
    }


def _typed_instance_graph(type_uuid: str, n: int, prefix: str) -> list[dict]:
    """A small L4 graph: n distinct leaf instances each instance_of the SAME type node,
    PLUS some owns edges (the relationship rung the walk arrives by) as noise the count
    must ignore. This is exactly what a strong ingest lays down under an anchor."""
    facts = []
    for i in range(n):
        iid = f"{prefix}-inst-{i}"
        facts.append(_instance_edge(iid, type_uuid, f"{prefix} {i}"))
        # owns edge (user → instance): the walk reaches the instance THIS way; the count
        # ignores it and keys only on the instance_of classification edge.
        facts.append({
            "subject": "user", "object": iid, "rel_type": "owns",
            "_subject_id": "user-uuid", "_object_id": iid,
            "fact_class": "A", "confidence": 1.0,
        })
    return facts


# ── _detect_count_intent — the shape detector ────────────────────────────────────
class TestDetectCountIntent:
    @pytest.mark.parametrize("q,expected_phrase", [
        ("how many short stories have I written", "short stories"),
        ("how many tanks do I have", "tanks"),
        ("how many projects have I led", "projects"),
        ("How many tanks do I have?", "tanks"),
    ])
    def test_possession_action_shape(self, q, expected_phrase):
        got = _detect_count_intent(q)
        assert got, f"expected a count intent for {q!r}"
        assert got["type_phrase"] == expected_phrase

    def test_membership_list_shape(self):
        got = _detect_count_intent("how many titles on my to-watch list")
        assert got
        assert got["type_phrase"] == "titles"
        assert got["list_phrase"] == "to-watch list"

    # ── OUT OF SCOPE — must NOT be treated as an instance cardinality ──
    def test_sum_of_scalars_declined(self):
        # "how many hours in total" is a SUM of a measure, not a cardinality of instances.
        assert _detect_count_intent("how many hours in total have I spent gaming") == {}

    def test_stated_scalar_threshold_declined(self):
        # "how many stars to reach gold" is a stated scalar THRESHOLD — no possession/
        # action verb, no membership list → never matches the count shapes.
        assert _detect_count_intent("how many stars to reach gold") == {}

    def test_temporal_duration_units_declined(self):
        # "how many days/weeks/months/years" belong to the temporal-calc layer, never count.
        assert _detect_count_intent("how many days between my move and my new job") == {}
        assert _detect_count_intent("how many weeks did I work there") == {}

    def test_non_count_query_declined(self):
        assert _detect_count_intent("tell me about my tanks") == {}
        assert _detect_count_intent("what is my favorite color") == {}

    def test_scope_currently(self):
        got = _detect_count_intent("how many cars do I currently have")
        assert got and got.get("scope") == "current"

    def test_since_predicate_captured(self):
        got = _detect_count_intent("how many projects have I led since 2020")
        assert got and got.get("since") == "2020"


# ── singularization ──────────────────────────────────────────────────────────────
class TestSingularize:
    @pytest.mark.parametrize("plural,singular", [
        ("tanks", "tank"),
        ("stories", "story"),
        ("titles", "title"),
        ("projects", "project"),
        ("boxes", "box"),
        ("glasses", "glass"),
        ("class", "class"),      # -ss guard: not stripped
    ])
    def test_singularize_noun(self, plural, singular):
        assert _singularize_noun(plural) == singular

    def test_singularize_phrase_head_only(self):
        # only the HEAD (last token) inflects; modifiers untouched
        assert _singularize_phrase("short stories") == "short story"
        assert _singularize_phrase("to-watch titles") == "to-watch title"


# ── _apply_instance_count — the THIN RENDER (validate the walk) ──────────────────
class TestApplyInstanceCount:
    def test_seven_short_stories(self):
        tuuid = "type-short-story"
        facts = _typed_instance_graph(tuuid, 7, "story")
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["miss"] is False
        assert res["count"] == 7

    def test_three_tanks(self):
        tuuid = "type-tank"
        facts = _typed_instance_graph(tuuid, 3, "tank")
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["count"] == 3

    def test_two_projects(self):
        tuuid = "type-project"
        facts = _typed_instance_graph(tuuid, 2, "project")
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["count"] == 2

    def test_twentyfive_titles(self):
        tuuid = "type-title"
        facts = _typed_instance_graph(tuuid, 25, "title")
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["count"] == 25

    def test_dedup_on_uuid(self):
        # The SAME instance surfaced twice (e.g. via facts + staged) counts ONCE.
        tuuid = "type-tank"
        facts = [
            _instance_edge("tank-1", tuuid, "Sherman"),
            _instance_edge("tank-1", tuuid, "Sherman"),   # duplicate UUID
            _instance_edge("tank-2", tuuid, "Tiger"),
        ]
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["count"] == 2

    def test_subtype_instances_count(self):
        # an instance of a SUBTYPE counts for the parent type (the type_uuids set carries
        # the subclass_of descendants — resolved upstream). "sedan" instance counts for
        # "how many cars".
        car, sedan = "type-car", "type-sedan"
        facts = [
            _instance_edge("car-1", car, "the truck"),
            _instance_edge("sedan-1", sedan, "the civic"),
        ]
        res = _apply_instance_count(facts, {car, sedan}, _INSTANCE_RELS)
        assert res["count"] == 2

    def test_off_type_instances_ignored(self):
        # instances of an UNRELATED type are not counted
        tank, dog = "type-tank", "type-dog"
        facts = [
            _instance_edge("tank-1", tank),
            _instance_edge("dog-1", dog),   # off-type
        ]
        res = _apply_instance_count(facts, {tank}, _INSTANCE_RELS)
        assert res["count"] == 1

    def test_unresolvable_type_is_miss(self):
        # empty type set → MISS (caller fails loud; never a fabricated 0)
        res = _apply_instance_count([_instance_edge("x", "t")], set(), _INSTANCE_RELS)
        assert res["miss"] is True
        assert res["count"] == 0

    def test_only_instance_of_edges_counted(self):
        # a graph of ONLY owns/relationship edges (no instance_of reached) → 0, honestly.
        # This is the SILENT-UNDERCOUNT surface: if ingest/walk never laid the instance_of
        # edge, the count cannot see the instance. Documented failure mode.
        tuuid = "type-tank"
        facts = [{
            "subject": "user", "object": "tank-1", "rel_type": "owns",
            "_subject_id": "user-uuid", "_object_id": "tank-1",
        }]
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS)
        assert res["count"] == 0

    def test_since_held_when_no_dates(self):
        # a "since" bound over a set with NO dated companion edges cannot be honoured
        # deterministically → HELD (tentative), never a confident fabricated number.
        tuuid = "type-project"
        facts = _typed_instance_graph(tuuid, 2, "project")
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS,
                                    since_iso="2020-01-01")
        assert res["held"] is True

    def test_since_filters_by_dated_companion(self):
        # with dated companion edges present, "since" filters instances by event_date.
        tuuid = "type-project"
        facts = [
            _instance_edge("p1", tuuid, "Alpha"),
            {"subject": "p1", "object": "started", "rel_type": "has_state",
             "_subject_id": "p1", "_object_id": "state-started",
             "event_date": "2021-06-01"},
            _instance_edge("p2", tuuid, "Beta"),
            {"subject": "p2", "object": "started", "rel_type": "has_state",
             "_subject_id": "p2", "_object_id": "state-started",
             "event_date": "2019-01-01"},   # before the bound
        ]
        res = _apply_instance_count(facts, {tuuid}, _INSTANCE_RELS,
                                    since_iso="2020-01-01")
        assert res["held"] is False
        assert res["count"] == 1   # only p1 (2021) is on/after 2020


# ── _resolve_count_type_uuids — grounding via exact alias + subclass_of ──────────
class TestResolveCountTypeUuids:
    def test_resolves_singular_alias_and_expands_subtypes(self):
        db = MagicMock()
        cur = MagicMock()
        db.cursor.return_value.__enter__.return_value = cur
        # fetchall order: (0) _get_classification_rels probe, (1) alias lookup → the type
        # node, (2) subclass_of descendants → one subtype, (3) that subtype's → none.
        cur.fetchall.side_effect = [
            [("instance_of",), ("subclass_of",), ("is_a",)],  # classification rels
            [("type-tank",)],        # alias 'tank' resolves
            [],                      # taxonomy-overlay member probe (no 'tank' taxonomy)
            [("type-light-tank",)],  # subclass_of type-tank
            [],                      # no further descendants
        ]
        got = _resolve_count_type_uuids(db, "tanks")
        assert "type-tank" in got
        assert "type-light-tank" in got
        # the alias lookup (2nd execute) used the SINGULAR head + raw phrase (exact match)
        alias_sql = cur.execute.call_args_list[1][0][0]
        assert "alias = ANY(%s)" in alias_sql
        cands = cur.execute.call_args_list[1][0][1][0]
        assert "tank" in cands   # singularized

    def test_unresolvable_type_returns_empty(self):
        db = MagicMock()
        cur = MagicMock()
        db.cursor.return_value.__enter__.return_value = cur
        cur.fetchall.side_effect = [
            [("instance_of",), ("subclass_of",)],  # classification rels probe
            [],                                     # nothing aliased
        ]
        assert _resolve_count_type_uuids(db, "flibbertigibbets") == set()

    def test_none_db_returns_empty(self):
        assert _resolve_count_type_uuids(None, "tanks") == set()


# ── prose render smoke: the number reaches the reader ────────────────────────────
class TestCountProse:
    def test_assert_line_carries_the_number(self):
        # mirror the synthetic-fact definition the /query injector builds for a grounded
        # count, and assert the cardinality is present and clean (no UUIDs / rel tokens).
        n, tp = 7, "short stories"
        definition = f"You have {n} {tp} on record."
        assert "7" in definition
        assert "short stories" in definition
        assert "instance_of" not in definition
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", definition)  # no UUID leak

    def test_miss_line_names_the_gap_without_a_number(self):
        tp = "tanks"
        definition = f"I don't have any {tp} on record, so I can't give a count."
        assert "tanks" in definition
        assert not re.search(r"\b\d+\b", definition)  # never fabricates a number
