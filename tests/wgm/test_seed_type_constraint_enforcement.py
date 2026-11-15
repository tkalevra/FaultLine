"""Cluster-C junk overcapture — contract pins for the two ingest-side guards.

BACKGROUND (the bug these pin). A census of 854 recall lines over 76 LongMemEval bench
questions surfaced ~89 junk lines that are ONTOLOGY TYPE VIOLATIONS the ontology already
forbids: 13x ``met`` on a non-Person ("You have met Bike", "You have met southwest airlines"),
65x thin ``owns`` on a bare/abstract noun, and 3x a self-loop hierarchy edge
("Parent is a subclass of Parent"). ``met`` is SEEDED with ``tail_types={Person}`` and ``owns``
with ``tail_types={Animal,Object,Organization}`` — the declarations exist and were simply never
enforced against these writes.

ROOT CAUSE, in one line: ``head_types``/``tail_types`` were being read the RDFS
``rdfs:range`` way (descriptive — "RDF Schema provides a mechanism for describing this
information, but does not say whether or how an application should use it"; the entailment
reading INFERS the missing type and can never fail, https://www.w3.org/TR/rdf-schema/#ch_range)
rather than the SHACL ``sh:class`` way (a VALUE TYPE CONSTRAINT validated at write time — "for
each value node that is ... not a SHACL instance of $class in the data graph, there is a
validation result", https://www.w3.org/TR/shacl/#ClassConstraintComponent). On top of that, the
USER-IS-TRUTH exemption keyed on ``fact_provenance="user_stated"``, which the ingest provenance
router stamps on EVERY MCP arrival — so on the STATEMENT path the EXTRACTOR's choice of rel_type
inherited the user's authority and the type veto was exempted 100% of the time.

The reflexive guard is grounded in RDF 1.1 Semantics entailment rule **rdfs10** — "If S contains:
xxx rdf:type rdfs:Class . then S RDFS entails: xxx rdfs:subClassOf xxx ."
(https://www.w3.org/TR/rdf11-mt/#patterns-of-rdfs-entailment-informative) — so an asserted
hierarchy self-loop is already entailed and carries zero information.

Both behaviours are FLAG-GATED and DEFAULT OFF; the default-off pins below are the
byte-identical-behaviour guarantee.
"""

import os
from contextlib import contextmanager

import pytest

from src.wgm import gate as gate_mod
from src.wgm.gate import WGMValidationGate


# ── Fakes: no DB, no GLiNER2, no LLM. The seed shape is injected verbatim. ──

class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_kw):
        return None

    def fetchone(self):
        return self._row


class _FakeConn:
    """Stands in for the gate's tenant connection. ``seed`` is the shape a
    ``SELECT head_types, tail_types FROM public.rel_types`` would return —
    supplied as a dict for readability and handed to the code as the positional
    ``(head_types, tail_types)`` tuple psycopg2 actually returns.
    ``None`` = the rel is not seeded."""

    def __init__(self, seed):
        self.row = (
            None if seed is None
            else (seed.get("head_types"), seed.get("tail_types"))
        )

    @contextmanager
    def _cur(self):
        yield _FakeCursor(self.row)

    def cursor(self):
        return self._cur()


def _gate(seed_row):
    g = WGMValidationGate.__new__(WGMValidationGate)
    g.db_conn = _FakeConn(seed_row)
    g._schema_name = None
    return g


def _violation(seed_row, *, subject_type=None, object_type=None):
    g = _gate(seed_row)
    # No entity ids → the hierarchy-reachability escape inside
    # _passed_type_is_genuine_mismatch is skipped, isolating the type-token comparison.
    return g._seed_declared_constraint_violation(
        "any_rel", None, None, subject_type=subject_type, object_type=object_type
    )


# ── FLAG DEFAULTS: both new behaviours are OFF unless explicitly enabled. ──

def test_seed_type_constraint_enforcement_defaults_off(monkeypatch):
    monkeypatch.delenv("WGM_SEED_TYPE_CONSTRAINT_ENFORCE", raising=False)
    assert gate_mod._seed_type_constraint_enforce_on() is False


def test_reflexive_hierarchy_rejection_defaults_off(monkeypatch):
    monkeypatch.delenv("WGM_REJECT_REFLEXIVE_HIERARCHY", raising=False)
    assert gate_mod._reject_reflexive_hierarchy_on() is False


def test_flags_turn_on_explicitly(monkeypatch):
    monkeypatch.setenv("WGM_SEED_TYPE_CONSTRAINT_ENFORCE", "true")
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    assert gate_mod._seed_type_constraint_enforce_on() is True
    assert gate_mod._reject_reflexive_hierarchy_on() is True


# ── THE HEADLINE CLUSTER: `met` (seed tail = {Person}) on a concrete non-Person. ──

@pytest.mark.parametrize("bad_object_type", ["Object", "Organization", "Location", "Concept"])
def test_seeded_person_tail_violated_by_concrete_nonperson(bad_object_type):
    """"You have met Bike" / "...met southwest airlines": the seed declares a Person tail,
    the object carries a concrete incompatible type → tail violation."""
    assert _violation({"head_types": ["Person"], "tail_types": ["Person"]},
                      object_type=bad_object_type) == "tail"


def test_seeded_person_tail_satisfied_is_not_a_violation():
    """A real co-participant still passes — this guard must not cost real `met` edges."""
    assert _violation({"head_types": ["Person"], "tail_types": ["Person"]},
                      object_type="Person") is None


@pytest.mark.parametrize("owns_object_type", ["Animal", "Object", "Organization"])
def test_seeded_multi_type_tail_admits_every_declared_member(owns_object_type):
    """`owns` declares {Animal,Object,Organization}: each declared member is admitted."""
    assert _violation(
        {"head_types": ["Person", "Organization"],
         "tail_types": ["Animal", "Object", "Organization"]},
        object_type=owns_object_type,
    ) is None


def test_seeded_multi_type_tail_rejects_undeclared_member():
    """"You currently own Friend": a Person is not an ownable tail for `owns`."""
    assert _violation(
        {"head_types": ["Person", "Organization"],
         "tail_types": ["Animal", "Object", "Organization"]},
        object_type="Person",
    ) == "tail"


def test_head_role_is_checked_too():
    assert _violation({"head_types": ["Person"], "tail_types": ["ANY"]},
                      subject_type="Object") == "head"


# ── FAIL-SAFE: no concrete signal ⇒ no verdict. Never fabricate a violation. ──

@pytest.mark.parametrize("no_signal", [None, "", "   ", "unknown", "UNKNOWN"])
def test_absent_or_unknown_type_never_violates(no_signal):
    """The single most important pin: an untyped entity must NOT be craterable.
    Blocking on 'unknown' would recreate the chicken-and-egg the gate documents."""
    assert _violation({"head_types": ["Person"], "tail_types": ["Person"]},
                      object_type=no_signal) is None


def test_unconstrained_any_role_never_violates():
    """ANY is the ontology's explicit 'unconstrained' sentinel — honour it, do not invent
    a constraint the metadata does not declare."""
    assert _violation({"head_types": ["ANY"], "tail_types": ["ANY"]},
                      subject_type="Object", object_type="Concept") is None


def test_scalar_tail_sentinel_never_violates():
    """SCALAR is a storage-routing marker, not an entity class."""
    assert _violation({"head_types": ["ANY"], "tail_types": ["SCALAR"]},
                      object_type="Object") is None


@pytest.mark.parametrize("empty", [None, [], ()])
def test_empty_constraint_never_violates(empty):
    assert _violation({"head_types": empty, "tail_types": empty},
                      subject_type="Object", object_type="Concept") is None


def test_rel_absent_from_seed_is_never_enforced():
    """AUTHORITY ORDER (user > seed > engine growth): a rel that is NOT in the public
    template is genuinely novel — engine growth owns its structure, so nothing is enforced."""
    assert _violation(None, subject_type="Object", object_type="Concept") is None


def test_db_error_fails_safe_to_no_violation():
    class _Boom:
        def cursor(self):
            raise RuntimeError("connection lost")

    g = WGMValidationGate.__new__(WGMValidationGate)
    g.db_conn = _Boom()
    g._schema_name = None
    assert g._seed_declared_constraint_violation(
        "any_rel", None, None, subject_type="Object", object_type="Concept"
    ) is None


def test_tail_is_reported_before_head_when_both_violate():
    """Deterministic, stable verdict ordering (the object role is the observed junk driver)."""
    assert _violation({"head_types": ["Person"], "tail_types": ["Person"]},
                      subject_type="Object", object_type="Object") == "tail"


# ── `_constraint_is_concrete`: the "does this actually constrain?" predicate. ──

@pytest.mark.parametrize("concrete,expected", [
    (["Person"], True),
    (["Animal", "Object", "Organization"], True),
    (["ANY"], False),
    (["any"], False),
    (["Person", "ANY"], False),        # ANY anywhere ⇒ unconstrained
    (["SCALAR"], False),
    (["scalar"], False),
    ([], False),
    (None, False),
    (["", "  "], False),
])
def test_constraint_is_concrete(concrete, expected):
    assert gate_mod._constraint_is_concrete(concrete) is expected


# ── REFLEXIVE HIERARCHY TAUTOLOGY (rdfs10). ──

def _reflexive_gate(monkeypatch, *, is_hierarchy_rel):
    g = WGMValidationGate.__new__(WGMValidationGate)
    g.db_conn = _FakeConn(None)
    g._schema_name = None
    monkeypatch.setattr(WGMValidationGate, "_is_user_correction", lambda self, d: False)
    monkeypatch.setattr(
        WGMValidationGate, "get_current_ontology",
        lambda self: {"some_rel": {"is_hierarchy_rel": is_hierarchy_rel,
                                   "head_types": ["ANY"], "tail_types": ["ANY"]}},
    )
    return g


def test_reflexive_hierarchy_edge_rejected_when_flag_on(monkeypatch):
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    g = _reflexive_gate(monkeypatch, is_hierarchy_rel=True)
    out = g.validate_edge("same-uuid", "same-uuid", "some_rel",
                          fact_provenance="user_stated", confidence=1.0)
    assert out["status"] == "reflexive_tautology"
    assert out["committed"] == 0


def test_reflexive_hierarchy_edge_survives_when_flag_off(monkeypatch):
    """Default-OFF byte-identity pin: the guard must not fire unless enabled."""
    monkeypatch.delenv("WGM_REJECT_REFLEXIVE_HIERARCHY", raising=False)
    g = _reflexive_gate(monkeypatch, is_hierarchy_rel=True)
    out = g.validate_edge("same-uuid", "same-uuid", "some_rel",
                          fact_provenance="user_stated", confidence=1.0)
    assert out.get("status") != "reflexive_tautology"


def test_self_loop_on_non_hierarchy_rel_is_not_rejected(monkeypatch):
    """Scope discipline: only HIERARCHY rels are covered by rdfs10. A self-loop on a
    non-hierarchy rel is a different defect and is deliberately out of scope here."""
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    g = _reflexive_gate(monkeypatch, is_hierarchy_rel=False)
    out = g.validate_edge("same-uuid", "same-uuid", "some_rel",
                          fact_provenance="user_stated", confidence=1.0)
    assert out.get("status") != "reflexive_tautology"


def test_distinct_endpoints_on_hierarchy_rel_are_untouched(monkeypatch):
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    g = _reflexive_gate(monkeypatch, is_hierarchy_rel=True)
    out = g.validate_edge("uuid-a", "uuid-b", "some_rel",
                          fact_provenance="user_stated", confidence=1.0)
    assert out.get("status") != "reflexive_tautology"


def test_reflexive_check_is_case_and_whitespace_normalized(monkeypatch):
    monkeypatch.setenv("WGM_REJECT_REFLEXIVE_HIERARCHY", "true")
    g = _reflexive_gate(monkeypatch, is_hierarchy_rel=True)
    out = g.validate_edge("  Same-UUID ", "same-uuid", "some_rel",
                          fact_provenance="user_stated", confidence=1.0)
    assert out["status"] == "reflexive_tautology"


# ── No-hardcoding discipline: the guards must contain no rel-name literals. ──

def test_new_guards_contain_no_rel_name_literals():
    """Metadata-driven pin. The seed reader, the concreteness predicate and the violation
    method must decide purely from the injected metadata — grep-proof against a regression
    that reintroduces `if rel_type == "met"` style special-casing."""
    import ast
    import inspect
    import textwrap

    def _executable_source(fn) -> str:
        """Return the function's source with comments AND docstrings stripped, so the pin
        inspects the CODE and never the prose explaining it."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(ast.fix_missing_locations(tree))

    code = "\n".join(_executable_source(fn) for fn in (
        gate_mod._seed_role_type_constraints,
        gate_mod._constraint_is_concrete,
        WGMValidationGate._seed_declared_constraint_violation,
    ))
    for banned in ("'met'", '"met"', "'owns'", '"owns"', "ILIKE", "LIKE '%"):
        assert banned not in code, f"rel-name/fuzzy literal leaked into the guard: {banned}"
