"""Unit tests for strong-ingest entity grounding (re-type / quarantine / climb).

Covers DESIGN-hierarchy-ladder-and-growth.md: a TYPING problem, not a dropping
problem. The decision is PURELY METADATA-DRIVEN — re-route when the rel's
constrained head_types is inconsistent with the subject's resolved type AND the
object resolves to a KNOWN CLASS (existing canonical-typed entity, or an existing
hierarchy node). NO source-text / copula inspection. "I have a poodle named
Luna" → occupation=poodle re-routes to instance_of (keep the object) no matter
how the sentence was phrased; the ±X subclass_of climb follows; never drop info.

These exercise the PURE helpers in src/api/main.py — no DB, no model. The object's
class signal is resolved by the caller (a DB probe) and passed in as a boolean, so
the decision function stays pure and testable.
NOTE: tests/ is gitignored; commit with `git add -f`.
"""
import os
import types

import pytest

from src.api.main import (
    _retype_mistyped_edge,
    _propose_classification_climb,
    _object_resolves_to_known_class,
    _compute_batch_class_objects,
    _is_scalar_rel_type,
    _flag_on,
    _CLASSIFICATION_RETYPE_REL,
)
import src.api.main as _main


def _edge(subject="luna", rel_type="nationality", object="poodle"):
    return types.SimpleNamespace(subject=subject, rel_type=rel_type, object=object)


# Minimal per-tenant ontology resolver: name -> meta dict.
_ONTOLOGY = {
    "nationality": {"head_types": ["Person"], "tail_types": ["SCALAR"], "is_hierarchy_rel": False},
    "occupation": {"head_types": ["Person"], "tail_types": ["SCALAR"], "is_hierarchy_rel": False},
    "has_ip": {"head_types": [], "tail_types": ["SCALAR"], "is_hierarchy_rel": False},
    "likes": {"head_types": ["ANY"], "tail_types": ["ANY"], "is_hierarchy_rel": False},
    "instance_of": {"head_types": ["ANY"], "tail_types": ["ANY"], "is_hierarchy_rel": True},
    "subclass_of": {"head_types": ["ANY"], "tail_types": ["ANY"], "is_hierarchy_rel": True},
}


def _resolver(name):
    return _ONTOLOGY.get((name or "").lower(), {})


# ── _retype_mistyped_edge: the core metadata-only re-route ───────────────────

def test_retype_animal_into_personscalar_slot():
    """Luna(Animal) + nationality(head=[Person]) + object IS a class → instance_of."""
    d = _retype_mistyped_edge(
        _edge(rel_type="nationality", object="poodle"),
        subject_gliner_type="Animal",
        object_is_known_class=True,
        rel_meta_resolver=_resolver,
    )
    assert d == {"action": "retype", "rel_type": _CLASSIFICATION_RETYPE_REL}


def test_retype_non_copula_have_a_poodle():
    """THE LEAK: "I have a poodle named Luna" — NO copula in the sentence.

    occupation(head=[Person]) + subject Animal + object poodle resolves to an
    Animal-class entity → must STILL re-type to instance_of. Zero dependence on
    the sentence containing "is a".
    """
    d = _retype_mistyped_edge(
        _edge(rel_type="occupation", object="poodle"),
        subject_gliner_type="Animal",
        object_is_known_class=True,
        rel_meta_resolver=_resolver,
    )
    assert d == {"action": "retype", "rel_type": _CLASSIFICATION_RETYPE_REL}


def test_retype_occupation_dog():
    d = _retype_mistyped_edge(
        _edge(rel_type="occupation", object="dog"),
        subject_gliner_type="Animal",
        object_is_known_class=True,
        rel_meta_resolver=_resolver,
    )
    assert d["action"] == "retype"


def test_user_authored_exempt_from_quarantine_lands_as_stated():
    """USER-IS-TRUTH: a user-authored edge whose subject type mismatches the rel's
    constrained head is NOT re-slotted/quarantined — it lands AS STATED (return None),
    the mismatch is a growth signal, not a veto. Same edge that would otherwise
    miss_pushback to Class C (object not a known class)."""
    # Control: WITHOUT the exemption this exact edge miss_pushbacks (see
    # test_inconsistent_object_not_a_class_pushes_back_not_drops).
    inferred = _retype_mistyped_edge(
        _edge(rel_type="occupation", object="zwomble"),
        subject_gliner_type="Object", object_is_known_class=False,
        rel_meta_resolver=_resolver, rel_is_scalar=True,
        user_authored=False,
    )
    assert inferred is not None and inferred["action"] == "miss_pushback"
    # user-authored → exempt → left exactly as stated.
    authored = _retype_mistyped_edge(
        _edge(rel_type="occupation", object="zwomble"),
        subject_gliner_type="Object", object_is_known_class=False,
        rel_meta_resolver=_resolver, rel_is_scalar=True,
        user_authored=True,
    )
    assert authored is None


def test_type_consistent_edge_left_alone():
    """Person subject + Person-only scalar = correct slot → no re-route."""
    d = _retype_mistyped_edge(
        _edge(subject="chris", rel_type="nationality", object="canadian"),
        subject_gliner_type="Person",
        object_is_known_class=True,
        rel_meta_resolver=_resolver,
    )
    assert d is None


def test_unconstrained_head_left_alone():
    """ANY/empty head_types can't be violated → no re-route."""
    assert _retype_mistyped_edge(
        _edge(rel_type="likes", object="poodle"),
        subject_gliner_type="Animal", object_is_known_class=True,
        rel_meta_resolver=_resolver,
    ) is None
    assert _retype_mistyped_edge(
        _edge(rel_type="has_ip", object="x"),
        subject_gliner_type="Animal", object_is_known_class=True,
        rel_meta_resolver=_resolver,
    ) is None


def test_unknown_subject_type_failsafe_when_object_is_class():
    """GLiNER2-miss subject BUT object is a known class → fail-safe leave-as-is.

    The object will re-type on re-ingest once signals A/B fire; no false-B risk here
    because a known-class object means the edge is already a confirmable classification.
    """
    assert _retype_mistyped_edge(
        _edge(rel_type="nationality", object="poodle"),
        subject_gliner_type="unknown", object_is_known_class=True,
        rel_meta_resolver=_resolver, rel_is_scalar=True,
    ) is None
    assert _retype_mistyped_edge(
        _edge(rel_type="nationality", object="poodle"),
        subject_gliner_type=None, object_is_known_class=True,
        rel_meta_resolver=_resolver, rel_is_scalar=True,
    ) is None


def test_gliner_miss_scalar_unconfirmable_pushes_back_not_false_b():
    """MISS-PUSHBACK: GLiNER2-miss subject + head-CONSTRAINED SCALAR + object NOT a class.

    This is the false-confident-B risk (occupation=poodle stored as a hard scalar). With
    no confirmable subject type AND an unconfirmable object, we must route to C-raw +
    queue 'what is poodle?' — NEVER store the false-B, NEVER drop.
    """
    d = _retype_mistyped_edge(
        _edge(rel_type="occupation", object="poodle"),
        subject_gliner_type="unknown",
        object_is_known_class=False,
        rel_meta_resolver=_resolver,
        rel_is_scalar=True,
    )
    assert d is not None
    assert d["action"] == "miss_pushback"
    assert d["object"] == "poodle"


def test_gliner_miss_non_scalar_failsafe():
    """GLiNER2-miss on a NON-scalar (relational) rel → fail-safe leave-as-is.

    A relational edge is not a false-B scalar, so the miss-pushback does not fire here
    (no regression to the existing fail-safe for relational edges)."""
    # 'likes' is ANY-headed (no constraint) so it never reaches the scalar branch; build
    # a head-constrained NON-scalar rel to prove the scalar-gate, not the head-gate.
    def _rel(name):
        m = dict(_ONTOLOGY)
        m["works_for"] = {"head_types": ["Person"], "tail_types": ["Organization"],
                          "is_hierarchy_rel": False}
        return m.get((name or "").lower(), {})
    d = _retype_mistyped_edge(
        _edge(rel_type="works_for", object="acme"),
        subject_gliner_type="unknown", object_is_known_class=False,
        rel_meta_resolver=_rel, rel_is_scalar=False,
    )
    assert d is None


def test_inconsistent_object_not_a_class_pushes_back_not_drops():
    """Type-inconsistent + object NOT a known class → MISS-PUSHBACK (C-raw + classify).

    The unknown concept is the object the user named, so we queue 'what is poodle?'
    rather than only quarantining. Info preserved (never dropped), never a false-B.
    """
    d = _retype_mistyped_edge(
        _edge(rel_type="nationality", object="poodle"),
        subject_gliner_type="Animal",
        object_is_known_class=False,   # object doesn't resolve to a class
        rel_meta_resolver=_resolver,
        rel_is_scalar=True,
    )
    assert d is not None
    assert d["action"] == "miss_pushback"
    assert d["object"] == "poodle"
    assert "object_not_a_class" in d["reason"]


def test_retype_target_missing_quarantines():
    """If instance_of isn't a hierarchy rel in this tenant → quarantine, not drop."""
    def _bad_resolver(name):
        m = dict(_ONTOLOGY)
        m["instance_of"] = {"head_types": ["ANY"], "tail_types": ["ANY"], "is_hierarchy_rel": False}
        return m.get((name or "").lower(), {})
    d = _retype_mistyped_edge(
        _edge(rel_type="nationality", object="poodle"),
        subject_gliner_type="Animal", object_is_known_class=True,
        rel_meta_resolver=_bad_resolver,
    )
    assert d["action"] == "quarantine"


# ── _propose_classification_climb: bounded ±X ────────────────────────────────

def test_climb_emits_connect_to_known_endpoint():
    os.environ["INGEST_CLASSIFICATION_GROWTH"] = "true"
    proposals = _propose_classification_climb("poodle", "Animal", _resolver)
    assert proposals
    assert proposals[0] == {"subject": "poodle", "rel_type": "subclass_of",
                            "object": "animal", "depth": 1}


def test_climb_disabled_by_flag():
    os.environ["INGEST_CLASSIFICATION_GROWTH"] = "false"
    try:
        assert _propose_classification_climb("poodle", "Animal", _resolver) == []
    finally:
        os.environ["INGEST_CLASSIFICATION_GROWTH"] = "true"


def test_climb_noop_when_leaf_is_root():
    os.environ["INGEST_CLASSIFICATION_GROWTH"] = "true"
    assert _propose_classification_climb("animal", "Animal", _resolver) == []


def test_climb_noop_unknown_root():
    os.environ["INGEST_CLASSIFICATION_GROWTH"] = "true"
    assert _propose_classification_climb("poodle", "unknown", _resolver) == []
    assert _propose_classification_climb("poodle", None, _resolver) == []


def test_climb_requires_subclass_of_in_ontology():
    os.environ["INGEST_CLASSIFICATION_GROWTH"] = "true"

    def _no_subclass(name):
        m = dict(_ONTOLOGY)
        m.pop("subclass_of", None)
        return m.get((name or "").lower(), {})
    assert _propose_classification_climb("poodle", "Animal", _no_subclass) == []


# ── _object_resolves_to_known_class signal C (in-batch GLiNER2 type) ─────────
# Signal C closes the re-type baseline: it returns True from the in-memory GLiNER2
# type BEFORE the object is a DB row, so "I have a poodle named Luna" re-types on
# the FIRST sight. The C branch returns early — no DB access — so db_conn=None proves
# the in-batch path needs no DB.

def test_object_known_class_via_gliner_type_no_db():
    """GLiNER2 typed the object to a canonical class in-batch → known class, no DB."""
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="Animal") is True
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="ANIMAL") is True  # case-insensitive


def test_object_gliner_type_unknown_or_noncanonical_not_a_class():
    """'unknown' / empty / non-canonical GLiNER type is NOT a class signal.

    A non-canonical type must NOT short-circuit to True; with db_conn=None the
    DB signals A/B fail-safe to False, so the whole call returns False.
    """
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="unknown") is False
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="") is False
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="KnowledgeGraph") is False
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type=None) is False


def test_have_a_poodle_retype_fires_with_gliner_object_type():
    """END-TO-END (pure layer): the non-copula leak now re-types.

    "I have a poodle named Luna" → occupation=poodle. The object is NOT yet a DB
    entity (it was the STRING object of a scalar rel), but GLiNER2 typed 'poodle' as
    Animal in-batch → _object_resolves_to_known_class returns True via signal C →
    _retype_mistyped_edge re-routes occupation → instance_of. Garbage gone, info kept.
    """
    obj_is_class = _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="Animal")
    assert obj_is_class is True
    d = _retype_mistyped_edge(
        _edge(subject="luna", rel_type="occupation", object="poodle"),
        subject_gliner_type="Animal",
        object_is_known_class=obj_is_class,
        rel_meta_resolver=_resolver,
    )
    assert d == {"action": "retype", "rel_type": _CLASSIFICATION_RETYPE_REL}


# ── _compute_batch_class_objects + signal D (in-batch class assertion) ───────
# Signal D makes the re-type ORDERING-ROBUST: the same batch that scalar-slots
# "occupation=poodle" also asserts "Luna is_a poodle", which PROVES poodle is a
# class — regardless of which edge is processed first. GLiNER2 types luna (the named
# instance), NOT the breed word poodle, so signal C is empty for poodle; signal D is the
# one that closes the leak. The set is computed ONCE over the whole batch before the loop.


def test_batch_class_objects_collects_classification_objects():
    """is_a / instance_of / subclass_of objects are collected; scalar rels are not."""
    edges = [
        _edge(subject="luna", rel_type="occupation", object="poodle"),  # scalar, NOT a source
        _edge(subject="luna", rel_type="is_a", object="poodle"),        # classification → poodle
        _edge(subject="poodle", rel_type="subclass_of", object="dog"),     # → dog
        _edge(subject="rex", rel_type="instance_of", object="Boxer"),      # → boxer (lowercased)
    ]
    got = _compute_batch_class_objects(edges, _resolver)
    assert got == {"poodle", "dog", "boxer"}


def test_batch_class_objects_requires_hierarchy_metadata():
    """A classification-seed rel that is NOT is_hierarchy_rel in metadata contributes nothing."""
    def _no_hier(name):
        m = dict(_ONTOLOGY)
        m["is_a"] = {"head_types": ["ANY"], "tail_types": ["ANY"], "is_hierarchy_rel": False}
        return m.get((name or "").lower(), {})
    edges = [_edge(subject="luna", rel_type="is_a", object="poodle")]
    assert _compute_batch_class_objects(edges, _no_hier) == set()


def test_batch_class_objects_failsafe_empty():
    """A resolver that raises → empty set (signal D disabled, never crashes ingest)."""
    def _boom(name):
        raise RuntimeError("metadata down")
    edges = [_edge(subject="luna", rel_type="is_a", object="poodle")]
    assert _compute_batch_class_objects(edges, _boom) == set()


def test_object_known_class_via_signal_d_no_gliner_no_db():
    """poodle has NO in-batch GLiNER type and NO DB row, but IS an in-batch class object."""
    batch = {"poodle"}
    # signal C empty (gliner_type None), signals A/B fail-safe to False (db_conn=None),
    # signal D fires from the batch set → True.
    assert _object_resolves_to_known_class(
        None, "poodle", None, gliner_type=None, batch_class_objects=batch) is True
    # not in the batch set → False (no other signal available).
    assert _object_resolves_to_known_class(
        None, "spaniel", None, gliner_type=None, batch_class_objects=batch) is False


def test_have_a_poodle_retypes_via_signal_d_scalar_edge_FIRST():
    """THE ORDERING BUG: scalar garbage edge processed FIRST still re-types via signal D.

    Batch = [occupation=poodle (scalar, ordered FIRST), luna is_a poodle]. GLiNER2 did
    NOT type the breed word poodle (it typed luna), so signal C is empty. The DB has no
    poodle row yet (signals A/B fail). Without signal D this stored occupation=poodle as a
    scalar (the leak). Signal D — computed once over the whole batch — proves poodle is a
    class from the sibling is_a edge, so _object_resolves_to_known_class returns True and the
    scalar occupation edge re-types to instance_of EVEN THOUGH it is processed first.
    """
    os.environ.pop("INGEST_EDGE_RETYPE", None)  # default-on
    batch = [
        _edge(subject="luna", rel_type="occupation", object="poodle"),  # FIRST in order
        _edge(subject="luna", rel_type="is_a", object="poodle"),
    ]
    batch_class_objects = _compute_batch_class_objects(batch, _resolver)
    assert "poodle" in batch_class_objects

    # Process the scalar garbage edge FIRST — poodle is not yet a DB row, GLiNER2 typed
    # luna not poodle (gliner_type None for poodle).
    scalar_edge = batch[0]
    obj_is_class = _object_resolves_to_known_class(
        None, scalar_edge.object, None,
        gliner_type=None,                       # GLiNER2 did NOT type the breed word
        batch_class_objects=batch_class_objects,
    )
    assert obj_is_class is True  # signal D
    d = _retype_mistyped_edge(
        scalar_edge,
        subject_gliner_type="Animal",
        object_is_known_class=obj_is_class,
        rel_meta_resolver=_resolver,
    )
    assert d == {"action": "retype", "rel_type": _CLASSIFICATION_RETYPE_REL}


def test_no_signal_d_without_sibling_classification_pushes_back():
    """No in-batch class assertion + no GLiNER type + no DB → MISS-PUSHBACK, NEVER dropped.

    The object (poodle) is the unknown user-derived concept, so instead of only
    quarantining we queue 'what is poodle?' (C-raw + classify). Still never a false-B,
    still never dropped — the directive carries the concept for the background classifier.
    """
    batch = [_edge(subject="luna", rel_type="occupation", object="poodle")]  # scalar only
    batch_class_objects = _compute_batch_class_objects(batch, _resolver)
    assert batch_class_objects == set()
    obj_is_class = _object_resolves_to_known_class(
        None, "poodle", None, gliner_type=None, batch_class_objects=batch_class_objects)
    assert obj_is_class is False
    d = _retype_mistyped_edge(
        batch[0], subject_gliner_type="Animal",
        object_is_known_class=obj_is_class, rel_meta_resolver=_resolver,
        rel_is_scalar=True)
    assert d["action"] == "miss_pushback"
    assert d["object"] == "poodle"
    assert "object_not_a_class" in d["reason"]


# ── POSITION/ORDERING: re-type runs BEFORE the scalar/relational split ───────
#
# The bug this fixes: the re-type evaluation used to run ~600 lines AFTER the
# `if _is_scalar_rel_type(edge.rel_type):` object-routing split, so a scalar-tail
# rel (occupation=poodle) had already had its object kept as a STRING and its
# typing suppressed before the re-type could intercept it. The fix MOVES the
# evaluation BEFORE the split and flips `edge.rel_type` to instance_of there.
#
# This test asserts the load-bearing consequence of the move: the SAME boolean
# gate the loop uses for object routing (`_is_scalar_rel_type`) returns True for
# the ORIGINAL mis-slotted rel but False for the CORRECTED rel — i.e. after the
# pre-split re-type the object takes the RELATIONAL branch (resolved to a UUID),
# not the scalar-string branch. No DB: we seed the in-memory scalar cache.

def test_retype_flips_scalar_rel_before_split(monkeypatch):
    """occupation(SCALAR) mistype → instance_of, and the split gate now sees a
    NON-scalar rel so the object routes relational (the position fix in one line).
    """
    # Seed the cache the loop's split gate reads, so _is_scalar_rel_type is DB-free.
    monkeypatch.setattr(_main, "_SCALAR_REL_TYPES_CACHE",
                        {"occupation", "nationality"}, raising=False)

    edge = _edge(subject="luna", rel_type="occupation", object="poodle")

    # BEFORE the re-type the loop's split gate would keep the object as a scalar string.
    assert _is_scalar_rel_type(edge.rel_type) is True

    # Compute the re-type from exactly the signals available at the EARLY position:
    #   subject GLiNER2 type (Animal) + object class signal (Animal in-batch, signal C).
    obj_is_class = _object_resolves_to_known_class(
        None, "poodle", None, gliner_type="Animal", batch_class_objects=None)
    assert obj_is_class is True
    d = _retype_mistyped_edge(
        edge, subject_gliner_type="Animal",
        object_is_known_class=obj_is_class, rel_meta_resolver=_resolver)
    assert d == {"action": "retype", "rel_type": _CLASSIFICATION_RETYPE_REL}

    # The loop applies the directive by flipping edge.rel_type BEFORE the split.
    edge.rel_type = d["rel_type"]

    # AFTER the flip the split gate sees a NON-scalar (hierarchy) rel → the object
    # would resolve as a RELATIONAL UUID, not be kept as a scalar string. THIS is
    # the behavior the position move buys: the corrected rel_type drives routing.
    assert _is_scalar_rel_type(edge.rel_type) is False
    assert _resolver(edge.rel_type)["is_hierarchy_rel"] is True


# ── flags ────────────────────────────────────────────────────────────────────

def test_flags_default_on_reversible():
    os.environ.pop("INGEST_EDGE_RETYPE", None)
    assert _flag_on("INGEST_EDGE_RETYPE") is True
    for falsey in ("0", "false", "no", "off", "FALSE"):
        os.environ["INGEST_EDGE_RETYPE"] = falsey
        assert _flag_on("INGEST_EDGE_RETYPE") is False
    os.environ.pop("INGEST_EDGE_RETYPE", None)
