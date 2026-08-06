"""Regression gate for GLiNER2 filing ONE span into SEVERAL scalar slots.

THE DEFECT (measured on the live line, via /ingest — the unit tests could not see it because
it happens above the spine deriver). GLiNER2 scores each candidate rel_type INDEPENDENTLY, so
nothing stopped it returning the same (subject, object) pair under two different scalar labels.
The verbatim raw output for "My son Rowan is 12." was::

    {'relation_extraction': {'age': [('Rowan', '12')], 'has_gender': [('Rowan', '12')],
                             'height': [], 'postal_code': [], ...}}

Both edges converted, both committed, and the store gained ``has_gender = 12`` — a person whose
gender is a number — alongside the correct age. That is the same shape as the "gender named F"
and "quantity is 10 F" / "12 m" rows a real memory accumulated.

THE FIX resolves the collision with metadata the schema ALREADY carries (migration 101's
``scalar_datatype``), not a rel_type word-list: a bare cardinal satisfies ``integer``, is not a
``quantity`` (no unit), and is only vacuously a ``string`` — so ``age`` wins and ``has_gender``
is dropped. Non-numeric spans are left alone, because co-claiming is legitimate there.
"""
import pytest

from src.api import main as m


def _edges(pairs):
    """Build converter-shaped edges: [(rel_type, subject, object), ...]."""
    return [{"subject": s, "object": o, "rel_type": r, "confidence": 0.85,
             "fact_provenance": "gliner2", "subject_type": None, "object_type": None}
            for r, s, o in pairs]


def _rels(edges):
    return sorted(e["rel_type"] for e in edges)


@pytest.fixture
def scalar_meta(monkeypatch):
    """The real declared datatypes for these rels (public seed, migration 101)."""
    monkeypatch.setattr(m, "_rel_meta", lambda *a, **k: {
        "age": {"scalar_datatype": "integer"},
        "has_gender": {"scalar_datatype": "string"},
        "height": {"scalar_datatype": "quantity"},
        "has_reference_id": {"scalar_datatype": "string"},
        "pref_name": {"scalar_datatype": "string"},
        "also_known_as": {"scalar_datatype": "string"},
    })


def test_the_live_collision_keeps_age_and_drops_gender(scalar_meta):
    """The exact production case: age(integer) beats has_gender(string) for a bare cardinal."""
    kept = m._resolve_gliner_scalar_slot_collisions(
        _edges([("age", "rowan", "12"), ("has_gender", "rowan", "12")]))
    assert _rels(kept) == ["age"], kept


def test_integer_also_beats_quantity(scalar_meta):
    """A bare cardinal is not a quantity — a quantity needs a unit."""
    kept = m._resolve_gliner_scalar_slot_collisions(
        _edges([("age", "rowan", "12"), ("height", "rowan", "12"),
                ("has_gender", "rowan", "12")]))
    assert _rels(kept) == ["age"], kept


def test_a_non_numeric_span_is_never_adjudicated(scalar_meta):
    """Co-claiming is legitimate for names — "ro" really is both pref_name and also_known_as."""
    edges = _edges([("pref_name", "rowan", "ro"), ("also_known_as", "rowan", "ro")])
    kept = m._resolve_gliner_scalar_slot_collisions(edges)
    assert _rels(kept) == ["also_known_as", "pref_name"], kept


def test_a_tie_between_equally_specific_slots_is_left_alone(scalar_meta):
    """Never guess: two string slots claiming one cardinal has no principled winner."""
    edges = _edges([("has_gender", "rowan", "12"), ("has_reference_id", "rowan", "12")])
    kept = m._resolve_gliner_scalar_slot_collisions(edges)
    assert _rels(kept) == ["has_gender", "has_reference_id"], kept


def test_a_single_claim_is_never_touched(scalar_meta):
    """No collision, no adjudication — a lone string-slot cardinal still stands."""
    edges = _edges([("has_reference_id", "rowan", "12")])
    assert m._resolve_gliner_scalar_slot_collisions(edges) == edges


def test_different_subjects_do_not_collide(scalar_meta):
    """The group key is (subject, object) — two people can both be 12."""
    edges = _edges([("age", "rowan", "12"), ("age", "quinn", "12")])
    assert _rels(m._resolve_gliner_scalar_slot_collisions(edges)) == ["age", "age"]


def test_missing_metadata_drops_nothing(monkeypatch):
    """Fail-safe: unresolvable metadata must never remove a captured edge."""
    monkeypatch.setattr(m, "_rel_meta", lambda *a, **k: {})
    edges = _edges([("age", "rowan", "12"), ("has_gender", "rowan", "12")])
    assert m._resolve_gliner_scalar_slot_collisions(edges) == edges


def test_metadata_failure_drops_nothing(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "_rel_meta", _boom)
    edges = _edges([("age", "rowan", "12"), ("has_gender", "rowan", "12")])
    assert m._resolve_gliner_scalar_slot_collisions(edges) == edges


def test_converter_applies_the_guard_end_to_end(scalar_meta):
    """The verbatim GLiNER2 payload from the live line, through the real converter."""
    out = m._convert_gliner_relations_to_edges({
        "relation_extraction": {
            "age": [("Rowan", "12")],
            "has_gender": [("Rowan", "12")],
            "height": [],
            "postal_code": [],
        }
    })
    assert _rels(out) == ["age"], out
    assert out[0]["subject"] == "rowan" and out[0]["object"] == "12"
