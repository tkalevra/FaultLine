"""Tail/head-type mismatch veto (WGM_TYPE_MISMATCH_VETO).

A high-confidence edge whose OBJECT (or SUBJECT) GLiNER2 type genuinely does NOT satisfy
the rel's tail_types/head_types — after the hierarchy-reachability allowance — must NOT be
enthroned as durable Class A. It craters to Class C (we don't forget). A type-SATISFYING
object (e.g. Event/Concept for participated_in — the reified-event lane) is UNAFFECTED.

Subject-agnostic, metadata-driven (head/tail from the ontology), no rel/type literal in the
production logic. Tests below use participated_in/Object only as a concrete illustration.

OWNER RULING 2026-09-16 (gauntlet l4-type-constraints-enforced): a CONCRETE type mismatch is now
REFUSED by the gate (`status="type_refused"`, WGM_TYPE_MISMATCH_REFUSE default ON) for EVERY
provenance — the crater-to-C lane and the user_stated exemption below are the PRE-RULING
behaviour, kept reachable ONLY through the rollback lever (WGM_TYPE_MISMATCH_REFUSE=false). The
legacy pins therefore run with the lever OFF (`_legacy` fixture); the refusal pins run with it
ON (the default).
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from wgm.gate import WGMValidationGate  # noqa: E402


# participated_in: a CONSTRAINED tail (Concept/Event) — the lane the GPS miscast poisons.
_PARTICIPATED = {
    "participated_in": {
        "head_types": ["Person"],
        "tail_types": ["Concept", "Event"],
        "is_hierarchy_rel": False,
        "is_symmetric": False,
        "inverse_rel_type": None,
        "category": None,
    }
}


@pytest.fixture
def _legacy(monkeypatch):
    """The PRE-RULING gate: refusal lever OFF → crater-to-C / user_stated exemption semantics."""
    monkeypatch.setenv("WGM_TYPE_MISMATCH_REFUSE", "false")
    yield


def _gate():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []   # no existing facts / no conflict
    cur.fetchone.return_value = None
    g = WGMValidationGate(conn)
    return g


# ---------------- _passed_type_is_genuine_mismatch (pure predicate) ----------------

def test_predicate_genuine_object_mismatch():
    g = _gate()
    # Object ∉ {Concept, Event}; no hierarchy facts → genuine mismatch.
    with patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None):
        assert g._passed_type_is_genuine_mismatch("Object", ["Concept", "Event"], "eid") is True


def test_predicate_type_satisfying_event_not_mismatch():
    g = _gate()
    # Event ∈ {Concept, Event} → NOT a mismatch (the reified-event lane).
    assert g._passed_type_is_genuine_mismatch("Event", ["Concept", "Event"], "eid") is False
    assert g._passed_type_is_genuine_mismatch("concept", ["Concept", "Event"], "eid") is False


def test_predicate_unknown_is_failsafe_not_mismatch():
    g = _gate()
    assert g._passed_type_is_genuine_mismatch("unknown", ["Concept", "Event"], "eid") is False
    assert g._passed_type_is_genuine_mismatch("", ["Concept", "Event"], "eid") is False
    assert g._passed_type_is_genuine_mismatch(None, ["Concept", "Event"], "eid") is False


def test_predicate_any_or_empty_constraint_not_mismatch():
    g = _gate()
    assert g._passed_type_is_genuine_mismatch("Object", ["ANY"], "eid") is False
    assert g._passed_type_is_genuine_mismatch("Object", [], "eid") is False
    assert g._passed_type_is_genuine_mismatch("Object", None, "eid") is False


def test_predicate_hierarchy_reachable_subtype_allowed():
    g = _gate()
    # GLiNER2 said "Object" but the entity's hierarchy resolves to an allowed type → NOT a mismatch.
    with patch.object(g, "_resolve_entity_type_via_hierarchy", return_value="Event"):
        assert g._passed_type_is_genuine_mismatch("Object", ["Concept", "Event"], "eid") is False


# ---------------- pregate veto end-to-end (validate_edge) ----------------

def _run_validate(g, object_type, confidence=1.0):
    # Ground-truth resolution returns None (fresh entity 'unknown') so the EXISTING ground-truth
    # pregate cannot fire — this isolates the PASSED-TYPE veto, the order-independent fix.
    with patch.object(g, "get_current_ontology", return_value=_PARTICIPATED), \
         patch.object(g, "_resolve_entity_type", return_value=None), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None), \
         patch.object(g, "_is_user_correction", return_value=False), \
         patch.object(g, "_find_conflicting_relationships", return_value=[]), \
         patch.object(g, "_delete_conflicting_staged_hierarchy_facts", return_value=None):
        return g.validate_edge(
            "user", "obj", "participated_in",
            subject_type="Person", object_type=object_type,
            confidence=confidence,
        )


def test_veto_craters_object_mismatch_to_class_c(_legacy):
    """(user, participated_in, gps[Object]) → craters to the hierarchy_type_mismatch lane (→ Class C)."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)  # default ON
    g = _gate()
    res = _run_validate(g, object_type="Object")
    assert res.get("hierarchy_type_mismatch") is True, res


def test_veto_unaffected_for_event_object(_legacy):
    """(user, participated_in, webinar[Event]) → NOT cratered (object satisfies tail_types)."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate(g, object_type="Event")
    assert not res.get("hierarchy_type_mismatch"), res
    assert res.get("status") == "valid"


def test_veto_unaffected_for_concept_object(_legacy):
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate(g, object_type="Concept")
    assert not res.get("hierarchy_type_mismatch"), res


def test_flag_off_restores_legacy_admit(_legacy):
    """Flag OFF → the Object mismatch is NOT cratered (legacy bypass enthrones it)."""
    os.environ["WGM_TYPE_MISMATCH_VETO"] = "false"
    try:
        g = _gate()
        res = _run_validate(g, object_type="Object")
        assert not res.get("hierarchy_type_mismatch"), res
    finally:
        os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)


def test_head_type_mismatch_also_craters(_legacy):
    """A head_types (subject) mismatch craters identically (subject-agnostic on the head role)."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    with patch.object(g, "get_current_ontology", return_value=_PARTICIPATED), \
         patch.object(g, "_resolve_entity_type", return_value=None), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None), \
         patch.object(g, "_is_user_correction", return_value=False), \
         patch.object(g, "_find_conflicting_relationships", return_value=[]), \
         patch.object(g, "_delete_conflicting_staged_hierarchy_facts", return_value=None):
        res = g.validate_edge(
            "subj", "obj", "participated_in",
            subject_type="Object",   # head requires Person → mismatch
            object_type="Event",     # tail satisfies
            confidence=1.0,
        )
    assert res.get("hierarchy_type_mismatch") is True, res


# ---------------- USER-IS-TRUTH: provenance exemption from the type-veto ----------------
# A type constraint is a validator/growth-guide for INFERRED edges — NOT a veto on what the
# USER STATED. "A product was born" / "the company was founded" genuinely mismatch a
# Person-constrained rel, but they are TRUE and must LAND durable; the veto stays intact for
# llm_inferred / llm_learned. Keyed on PROVENANCE only, subject-agnostic (no rel/type literal).

def _run_validate_prov(g, object_type, subject_type="Person",
                       fact_provenance="user_stated", is_correction=False, confidence=1.0):
    with patch.object(g, "get_current_ontology", return_value=_PARTICIPATED), \
         patch.object(g, "_resolve_entity_type", return_value=None), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None), \
         patch.object(g, "_find_conflicting_relationships", return_value=[]), \
         patch.object(g, "_delete_conflicting_staged_hierarchy_facts", return_value=None):
        return g.validate_edge(
            "user", "obj", "participated_in",
            subject_type=subject_type, object_type=object_type,
            confidence=confidence,
            fact_provenance=fact_provenance,
            is_correction=is_correction,
        )


def test_user_stated_object_mismatch_is_exempt_lands_durable(_legacy):
    """(user, participated_in, gps[Object]) user_stated → NOT cratered; lands durable."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)  # veto ON
    g = _gate()
    res = _run_validate_prov(g, object_type="Object", fact_provenance="user_stated")
    assert not res.get("hierarchy_type_mismatch"), res
    assert res.get("status") == "valid", res


def test_user_stated_head_mismatch_is_exempt(_legacy):
    """(product, participated_in, event) — subject Object violates head=Person, user_stated → exempt."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate_prov(g, object_type="Event", subject_type="Object",
                             fact_provenance="user_stated")
    assert not res.get("hierarchy_type_mismatch"), res
    assert res.get("status") == "valid", res


def test_correction_object_mismatch_is_exempt(_legacy):
    """A correction (is_correction=True) is authoritative → exempt from the veto."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate_prov(g, object_type="Object",
                             fact_provenance="llm_inferred", is_correction=True)
    assert not res.get("hierarchy_type_mismatch"), res


def test_llm_inferred_object_mismatch_still_craters(_legacy):
    """NO REGRESSION: an llm_inferred type-incompatible edge still craters to Class C."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate_prov(g, object_type="Object", fact_provenance="llm_inferred")
    assert res.get("hierarchy_type_mismatch") is True, res


def test_llm_learned_object_mismatch_still_craters(_legacy):
    """NO REGRESSION: llm_learned mismatch also still craters (veto's legit job intact)."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate_prov(g, object_type="Object", fact_provenance="llm_learned")
    assert res.get("hierarchy_type_mismatch") is True, res


def test_user_stated_type_satisfying_still_valid(_legacy):
    """Control: a user_stated edge whose type SATISFIES is untouched (still valid, no crater)."""
    os.environ.pop("WGM_TYPE_MISMATCH_VETO", None)
    g = _gate()
    res = _run_validate_prov(g, object_type="Event", fact_provenance="user_stated")
    assert not res.get("hierarchy_type_mismatch"), res
    assert res.get("status") == "valid", res


# ---------------- OWNER RULING 2026-09-16: a CONCRETE mismatch is REFUSED (lever ON) ----------------
# The refusal runs FIRST in validate_edge — before the correction supersession and before the
# confidence bypass — for every provenance. Absent/'unknown'/ANY/SCALAR keep the admit semantics;
# a hierarchy-reachable subtype is admitted. The verdict carries role / type seen / declared
# types so /ingest can surface it (a loud, user-correctable status — never a silent flag).

def _run_refuse(g, object_type, subject_type="Person", fact_provenance="user_stated",
                is_correction=False, confidence=1.0, stored_object_type=None, hier=None):
    with patch.object(g, "get_current_ontology", return_value=_PARTICIPATED), \
         patch.object(g, "_resolve_entity_type", return_value=stored_object_type), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=hier), \
         patch.object(g, "_find_conflicting_relationships", return_value=[]), \
         patch.object(g, "_supersede_conflicting_facts", return_value=None) as sup, \
         patch.object(g, "_delete_conflicting_staged_hierarchy_facts", return_value=None):
        res = g.validate_edge(
            "user", "obj", "participated_in",
            subject_type=subject_type, object_type=object_type,
            confidence=confidence, fact_provenance=fact_provenance,
            is_correction=is_correction,
        )
        res["_superseded_calls"] = sup.call_count
        return res


def test_refuse_lever_defaults_on():
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    from wgm import gate as _g
    assert _g._type_mismatch_refuse_on() is True


@pytest.mark.parametrize("prov,corr", [("user_stated", False), ("llm_inferred", False),
                                       ("llm_learned", False), ("llm_inferred", True)])
def test_concrete_tail_mismatch_is_refused_for_every_provenance(prov, corr):
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    res = _run_refuse(g, object_type="Object", fact_provenance=prov, is_correction=corr)
    assert res.get("status") == "type_refused", res
    assert res.get("role") == "tail" and res.get("entity_type") == "Object", res
    assert [a.lower() for a in res.get("allowed")] == ["concept", "event"], res
    assert res.get("committed") == 0
    assert "hierarchy_type_mismatch" not in res     # a verdict, not a flag


def test_refused_correction_supersedes_nothing():
    """A refused CORRECTION must change nothing: the refusal runs before supersession."""
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    with patch.object(g, "_is_user_correction", return_value=True):
        res = _run_refuse(g, object_type="Object", fact_provenance="user_stated", is_correction=True)
    assert res.get("status") == "type_refused", res
    assert res["_superseded_calls"] == 0


def test_concrete_head_mismatch_is_refused_too():
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    res = _run_refuse(g, object_type="Event", subject_type="Object")
    assert res.get("status") == "type_refused" and res.get("role") == "head", res


@pytest.mark.parametrize("object_type", [None, "", "unknown", "UNKNOWN"])
def test_absent_or_unknown_type_is_not_refused(object_type):
    """No concrete signal → RDFS entailment applies (admit); never a refusal."""
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    res = _run_refuse(g, object_type=object_type)
    assert res.get("status") == "valid", res


def test_stored_concrete_type_is_consulted_when_no_type_is_passed():
    """The caller passed nothing, but the entity is STORED as Object → refused (the stored type is
    a concrete signal); stored 'unknown' → admitted."""
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    assert _run_refuse(g, object_type=None, stored_object_type="Object").get("status") == "type_refused"
    assert _run_refuse(g, object_type=None, stored_object_type="unknown").get("status") == "valid"


def test_hierarchy_reachable_subtype_is_admitted_not_refused():
    """(obj typed Object) whose own instance_of/subclass_of chain resolves to an allowed type IS
    allowed — this is the user's correction path ("X is an event" → the chain admits X)."""
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    res = _run_refuse(g, object_type="Object", hier="Event")
    assert res.get("status") == "valid", res


def test_satisfying_type_is_untouched():
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    assert _run_refuse(g, object_type="Event").get("status") == "valid"
    assert _run_refuse(g, object_type="concept").get("status") == "valid"   # case-insensitive


@pytest.mark.parametrize("declared", [["ANY"], ["any", "Person"], ["SCALAR"], [], None])
def test_non_concrete_constraint_never_refuses(declared):
    os.environ.pop("WGM_TYPE_MISMATCH_REFUSE", None)
    g = _gate()
    ont = {"participated_in": dict(_PARTICIPATED["participated_in"], tail_types=declared)}
    with patch.object(g, "get_current_ontology", return_value=ont):
        assert g._concrete_role_violation("participated_in", "user", "obj", "Person", "Object") is None


def test_lever_off_is_byte_for_byte_legacy(_legacy):
    """WGM_TYPE_MISMATCH_REFUSE=false → the pre-ruling verdicts come back exactly."""
    g = _gate()
    assert _run_refuse(g, object_type="Object", fact_provenance="user_stated").get("status") == "valid"
    assert _run_refuse(g, object_type="Object", fact_provenance="llm_inferred").get("hierarchy_type_mismatch") is True


def test_refusal_predicate_reads_metadata_only_no_rel_or_type_literal():
    """The refusal code names no rel and no type: every decision comes from the ontology arrays."""
    import inspect
    from wgm import gate as _g
    src = inspect.getsource(_g.WGMValidationGate._concrete_role_violation)
    for literal in ("'person'", "'organization'", "'location'", "'animal'", "'spouse'",
                    "'educated_at'", "'instance_of'", "'has_pet'"):
        assert literal not in src.lower(), literal
