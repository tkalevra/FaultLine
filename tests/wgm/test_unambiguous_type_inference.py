"""Contract pins for `WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS` and the async reflexive-hierarchy guard.

THE BUG (item #2). `_check_type_constraints` stamped an unknown-typed entity with
``tail_types[0]`` — the ARBITRARY FIRST element of a disjunction. ``owns
tail={Animal,Object,Organization}`` therefore typed every unknown owned thing ``Animal``, purely
because ``Animal`` sorts first. A disjunctive range entails NO single type (RDFS
https://www.w3.org/TR/rdf-schema/#ch_range; only OWL ``owl:unionOf`` can express the disjunction,
and union membership entails no particular member,
https://www.w3.org/TR/owl2-syntax/#Union_of_Class_Expressions), so the stamp is a FABRICATION that
poisons L4 — and, with the seed value-type constraint now enforced, can make a LATER legitimate
edge fail its own ``sh:class`` check.

THE BUG (item #3). The re_embedder's promotion jobs COPY a staged row into ``facts`` without ever
re-running ``validate_edge``, so a hierarchy SELF-LOOP already staged lands in the authoritative
table regardless of the gate's `WGM_REJECT_REFLEXIVE_HIERARCHY` rejection (RDF 1.1 Semantics rule
**rdfs10**, https://www.w3.org/TR/rdf11-mt/#patterns-of-rdfs-entailment-informative).

Both are FLAG-GATED and DEFAULT OFF; the default-off pins are the byte-identical guarantee.
PURE — no DB, no network, no LLM.

Run: python3 tools/fltest.py --bug OWNS --test tests/wgm/test_unambiguous_type_inference.py
"""
import pytest

from src.wgm.gate import _entailed_constraint_type
from src.re_embedder.embedder import _reflexive_hierarchy_blocked


# ── item #2 — disjunctive constraints entail no type ────────────────────────────────────────────

def test_default_off_is_byte_identical_legacy_first_element(monkeypatch):
    monkeypatch.delenv("WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS", raising=False)
    assert _entailed_constraint_type(["Animal", "Object", "Organization"]) == "Animal"
    assert _entailed_constraint_type(["Location"]) == "Location"


def test_flag_on_rejects_a_disjunction(monkeypatch):
    monkeypatch.setenv("WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS", "true")
    # owns tail={Animal,Object,Organization} — must NOT fabricate "Animal".
    assert _entailed_constraint_type(["Animal", "Object", "Organization"]) is None
    assert _entailed_constraint_type(["Person", "Organization"]) is None


def test_flag_on_keeps_an_unambiguous_singleton(monkeypatch):
    monkeypatch.setenv("WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS", "true")
    # lives_at tail={Location} — one disjunct, so the disjunction IS that type.
    assert _entailed_constraint_type(["Location"]) == "Location"
    assert _entailed_constraint_type([" Person "]) == "Person"


@pytest.mark.parametrize("types", [[], None, [""], ["ANY"], ["SCALAR"], ["any"]])
def test_flag_on_never_entails_a_placeholder(monkeypatch, types):
    monkeypatch.setenv("WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS", "true")
    assert _entailed_constraint_type(types) is None


@pytest.mark.parametrize("types", [[], None, [""]])
def test_empty_is_none_in_both_flag_states(monkeypatch, types):
    monkeypatch.delenv("WGM_INFER_TYPE_ONLY_IF_UNAMBIGUOUS", raising=False)
    assert _entailed_constraint_type(types) is None


# ── item #3 — the async promotion self-loop guard ───────────────────────────────────────────────

def test_reflexive_guard_default_off(monkeypatch):
    monkeypatch.delenv("WGM_REJECT_REFLEXIVE_HIERARCHY", raising=False)
    assert _reflexive_hierarchy_blocked("uuid-a", "uuid-a", True) is False


def test_reflexive_guard_blocks_hierarchy_self_loop(monkeypatch):
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    assert _reflexive_hierarchy_blocked("uuid-a", "uuid-a", True) is True
    assert _reflexive_hierarchy_blocked("UUID-A", "uuid-a", True) is True  # normalized identity


def test_reflexive_guard_leaves_everything_else_alone(monkeypatch):
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    assert _reflexive_hierarchy_blocked("uuid-a", "uuid-b", True) is False   # distinct ends
    assert _reflexive_hierarchy_blocked("uuid-a", "uuid-a", False) is False  # non-hierarchy rel
    assert _reflexive_hierarchy_blocked(None, None, True) is False           # no ids
    assert _reflexive_hierarchy_blocked("", "", True) is False
