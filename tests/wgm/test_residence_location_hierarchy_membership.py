"""Residence / location hierarchy-membership typing (migration 119 + gate guard).

DEFECT 3: a (Person, lives_in, Location) edge is the canonical residence fact and MUST
validate as a normal relationship — it must NOT be flagged a hierarchy-membership violation
and demoted to Class C. The bug: `lives_in`/`lives_at` are ASYMMETRIC CROSS-TYPE rels
(head=Person/Animal, tail=Location), but they had been listed in the `location`/`household`
taxonomies' rel_types_defining_group, so _validate_hierarchy_membership() — which requires
BOTH ends of a defining rel to be members of ONE homogeneous taxonomy — produced a partial
match (Person∈household but Location∉; Location∈location but Person∉) and cratered the fact.

DEFECT 4: `located_in` is geographic CONTAINMENT (Location -> Location) and must be a
homogeneous hierarchy rel that the membership check accepts (e.g. Paris located_in France).

The gate guard (src/wgm/gate.py::_validate_hierarchy_membership) is the durable fix: it skips
the homogeneous-membership check for any rel whose concrete head/tail type sets are DISJOINT
(cross-type asymmetric), reading the typing from the live ontology — subject-agnostic, no
rel_type name literal. The metadata migration cleans the seeded taxonomies in parallel.

Examples use NON-personal public entities (Paris/France) by TYPE only — the gate validates on
types, not names.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from wgm.gate import WGMValidationGate  # noqa: E402


# lives_in: animate resident -> Location (DISJOINT head/tail == cross-type asymmetric).
_LIVES_IN = {"lives_in": {"head_types": ["Person", "Animal"], "tail_types": ["Location"]}}
# located_in: containment Location -> Location (OVERLAPPING head/tail == homogeneous).
_LOCATED_IN = {"located_in": {"head_types": ["Location"], "tail_types": ["Location"]}}
# sibling_of: Person -> Person (homogeneous) — used to prove the membership check still bites.
_SIBLING = {"sibling_of": {"head_types": ["Person"], "tail_types": ["Person"]}}


def _gate(taxonomy_rows):
    """Gate whose cursor returns `taxonomy_rows` for the membership query.

    Rows shaped like the SELECT: (taxonomy_name, member_entity_types, rel_types_defining_group).
    If the cross-type guard short-circuits, this query is never reached.
    """
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = taxonomy_rows
    cur.fetchone.return_value = None
    return WGMValidationGate(conn)


def test_lives_in_person_location_not_a_membership_violation():
    """(Person, lives_in, Location) validates — cross-type guard short-circuits the homogeneous
    membership check even though the offending taxonomies WOULD partial-match."""
    offending = [
        ("household", ["Person", "Animal"], ["lives_in", "lives_at", "member_of"]),
        ("location", ["Location"], ["located_in", "located_at", "lives_in", "lives_at"]),
    ]
    g = _gate(offending)
    with patch.object(g, "get_current_ontology", return_value=_LIVES_IN):
        valid, reason, hierarchies = g._validate_hierarchy_membership(
            "lives_in", "Person", "Location"
        )
    assert valid is True, (valid, reason)
    assert reason == "cross_type_asymmetric_defer_to_type_constraints", reason
    assert hierarchies == []


def test_located_in_location_location_is_homogeneous_hierarchy():
    """(Location, located_in, Location) — e.g. Paris located_in France — is a homogeneous
    hierarchy: the guard does NOT fire (head/tail overlap) and the membership check accepts it."""
    rows = [("location", ["Location"], ["located_in"])]
    g = _gate(rows)
    with patch.object(g, "get_current_ontology", return_value=_LOCATED_IN):
        valid, reason, hierarchies = g._validate_hierarchy_membership(
            "located_in", "Location", "Location"
        )
    assert valid is True, (valid, reason)
    assert "location" in hierarchies
    assert reason.startswith("hierarchy_valid"), reason


def test_homogeneous_rel_still_craters_genuine_mismatch():
    """Guard must NOT disable the membership check for homogeneous rels: a true partial
    mismatch (sibling_of Person->Location) still violates."""
    rows = [("family", ["Person"], ["sibling_of", "parent_of"])]
    g = _gate(rows)
    with patch.object(g, "get_current_ontology", return_value=_SIBLING):
        valid, reason, hierarchies = g._validate_hierarchy_membership(
            "sibling_of", "Person", "Location"
        )
    assert valid is False, (valid, reason)
    assert "partial_hierarchy_match" in reason
