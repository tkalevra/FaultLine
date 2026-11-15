"""Temporal-tier scope admission (recall "what happened to my X").

Root cause regression guard: a concrete-entity anchor's own dated EVENT/STATE edge
(e.g. ``gps has_state break``) was projected out of every scoped lane because
``has_state`` is in NO taxonomy's ``rel_types_defining_group``. The fix admits such
edges into scope by their TEMPORAL metadata (``temporal_class='event'`` /
``category='state'``) — the deterministic, subject-agnostic analogue of
``_get_structural_rels`` for state/event edges.

These tests run WITHOUT a live DB: ``_get_event_state_rels`` falls back to the
``_REL_TYPE_META`` startup cache when ``POSTGRES_DSN`` is unset, which is the case in
the unit-test env (no Postgres/GLiNER2/torch). The admission is keyed on metadata only,
NEVER on a hardcoded rel name.
"""

import os
import pytest

import src.api.main as main


@pytest.fixture
def fake_rel_meta(monkeypatch):
    """Seed _REL_TYPE_META with a representative mix; ensure no DSN so the helper
    uses the cache fallback (deterministic, infra-free)."""
    meta = {
        # Dated state/event predicates — should be admitted.
        "has_state": {"temporal_class": "event", "category": "state", "tail_types": ["ANY"]},
        "feels":     {"temporal_class": "event", "category": "state", "tail_types": ["ANY"]},
        # Ordinary relationship rel — NOT event/state → not admitted by this tier.
        "owns":      {"temporal_class": "state", "category": "ownership", "tail_types": ["ANY"]},
        "spouse":    {"temporal_class": "state", "category": "social", "tail_types": ["ANY"]},
        # Ordinary scalar — not event/state.
        "age":       {"temporal_class": "state", "category": "attribute", "tail_types": ["SCALAR"]},
    }
    monkeypatch.setattr(main, "_REL_TYPE_META", meta)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    return meta


def test_event_state_rels_admits_by_metadata(fake_rel_meta):
    """Only rels whose metadata marks them temporal_class='event' OR category='state'
    are returned — by metadata, not by name."""
    rels = main._get_event_state_rels()
    assert "has_state" in rels   # temporal_class=event AND category=state
    assert "feels" in rels       # state predicate (mirrors has_state)
    assert "owns" not in rels     # ordinary relationship, not event/state
    assert "spouse" not in rels
    assert "age" not in rels      # ordinary scalar


def test_event_state_rels_subject_agnostic_event_marker(monkeypatch):
    """A domain rel marked temporal_class='event' (e.g. networking "server down",
    biology "leg broken") is admitted purely on the temporal marker — no category
    or name needed. Proves the tier is subject-agnostic."""
    monkeypatch.setattr(main, "_REL_TYPE_META", {
        "went_offline": {"temporal_class": "event", "category": "incident"},
        "located_in":   {"temporal_class": "state", "category": "location"},
    })
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    rels = main._get_event_state_rels()
    assert "went_offline" in rels
    assert "located_in" not in rels


def test_anchor_event_edge_is_admitted_into_scope(fake_rel_meta):
    """The admission step intersects the anchor's OWN rels with the event/state tier
    and routes them by tail metadata — exactly the logic wired into determine_path's
    scope-deference block. An anchor with a state edge gets it admitted; an anchor with
    no such edge is unaffected (additive + safe)."""
    event_state = main._get_event_state_rels()

    # Anchor (e.g. "gps") has these stored rels: a state edge + an ordinary one.
    anchor_rels = {"has_state", "owns"}
    admitted = sorted(anchor_rels & event_state)
    assert admitted == ["has_state"]            # state edge admitted
    assert "owns" not in admitted               # ownership not over-widened in

    # Route by tail metadata (relationship here, since has_state tail is ANY not SCALAR).
    assert not main._is_scalar_rel_type("has_state")

    # Anchor with NO event/state edges → nothing admitted (behavior unchanged).
    plain_anchor_rels = {"owns", "spouse"}
    assert (plain_anchor_rels & event_state) == set()
