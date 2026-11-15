"""Unit tests — the FIRST-CLASS value/place node property (migration 192, src/api/node_role.py).

PURE tests — no live DB, no network, no GLiNER2, no LLM. A tiny in-memory fake connection answers the
three queries node_role.py issues (rel_types.tail_types, entities.node_role read/write, naming-edge
membership) so the derivation / monotonic-transition / read-guard logic is exercised deterministically.

Pins THE HARD LINE contract (DEV/DESIGN-value-place-first-class.md):
  * a user VALUE node (user_stated @ unconstrained/scalar tail) is stamped VALUE and is_protected;
  * a genuine TYPE node (instance_of/subclass_of object, or constrained-tail rel object) is PLACE and
    is NOT protected → still ladders;
  * the HOMONYM/punning case (same surface, two bindings) resolves to BOTH and STAYS protected;
  * user authority: a value binding on a prior place node → BOTH (user wins), growth never demotes;
  * flag OFF (VALUE_PLACE_FIRST_CLASS unset) → every read is 'not protected', every write a no-op
    (byte-identical to today).

Run: python3 tools/fltest.py --bug VALUEPLACE --test tests/test_node_role_first_class.py
"""
import os

import pytest

# Ensure a clean flag baseline before import; individual tests set it explicitly.
os.environ.pop("VALUE_PLACE_FIRST_CLASS", None)

from src.api import node_role as nr  # noqa: E402


# ── in-memory fake connection ───────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("savepoint") or s.startswith("release savepoint") or s.startswith("rollback to savepoint"):
            self._result = None
            return
        if "select tail_types from rel_types" in s:
            rt = (params[0] or "").lower()
            self._result = [(self.conn.tail_types.get(rt),)] if rt in self.conn.tail_types else []
            return
        if "select node_role from entities" in s:
            nid = str(params[0])
            self._result = [(self.conn.roles.get(nid),)] if nid in self.conn.roles else []
            return
        if s.startswith("update entities set node_role"):
            role, nid = params[0], str(params[1])
            self.conn.roles[nid] = role
            self._result = None
            return
        # naming-edge membership: rel names are bound as ANY(%s) params (NOT in the SQL text), so
        # detect the query by its distinctive shape + the naming-rel list in params.
        if "subject_id = %s" in s and "rel_type = any(%s)" in s and "staged_facts" in s:
            nid = str(params[0])
            self._result = [(1,)] if nid in self.conn.naming_subjects else []
            return
        self._result = None

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, tail_types=None, roles=None, naming_subjects=None):
        # rel_type -> tail_types array
        self.tail_types = tail_types or {}
        # entity_id -> persisted node_role
        self.roles = roles or {}
        # set of entity_ids that are naming-edge subjects (names)
        self.naming_subjects = set(naming_subjects or ())

    def cursor(self):
        return _FakeCursor(self)


# Canonical tenant-like ontology fixture: value-filing rels (unconstrained/scalar) vs typed kinds.
_ONTOLOGY = {
    "favorite_colour": ["ANY"],       # unconstrained → files a VALUE
    "likes": [],                       # empty → unconstrained → VALUE
    "lives_at": ["SCALAR"],           # scalar literal → VALUE
    "owns": ["Object"],               # constrained typed kind → PLACE
    "has_pet": ["Animal"],            # constrained → PLACE
    "feels": ["Concept", "emotion"],  # constrained → PLACE (worried→…→emotion still ladders)
    "instance_of": ["ANY"],           # structural hierarchy rel → PLACE by rel name (not tail)
    "subclass_of": ["ANY"],
}


def _on():
    os.environ["VALUE_PLACE_FIRST_CLASS"] = "true"


def _off():
    os.environ.pop("VALUE_PLACE_FIRST_CLASS", None)


# ── flag OFF = byte-identical (not protected, no-op write) ───────────────────────────────────────

def test_flag_off_never_protected_and_write_noop():
    _off()
    conn = _FakeConn(tail_types=_ONTOLOGY, roles={"blue": "value"})
    # even a persisted value stamp is ignored while OFF
    assert nr.is_protected_value(conn, "blue") is False
    assert nr.effective_role(conn, "blue") is None
    # stamp is a no-op while OFF (does not mutate the fake store)
    assert nr.stamp_edge_object(conn, "blue", "favorite_colour", "user_stated") is None
    assert conn.roles == {"blue": "value"}


# ── derivation (subject-agnostic, metadata-driven) ────────────────────────────────────────────────

def test_derive_user_value_at_unconstrained_slot_is_value():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    assert nr.derive_object_role(conn, "favorite_colour", "user_stated") == nr.VALUE
    assert nr.derive_object_role(conn, "likes", "user_stated") == nr.VALUE
    assert nr.derive_object_role(conn, "lives_at", "user_stated") == nr.VALUE  # SCALAR literal


def test_derive_typed_kind_is_place_and_ladders():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    # constrained tails → PLACE (owns→car→vehicle, feels→worried→emotion still ladder)
    assert nr.derive_object_role(conn, "owns", "user_stated") == nr.PLACE
    assert nr.derive_object_role(conn, "has_pet", "user_stated") == nr.PLACE
    assert nr.derive_object_role(conn, "feels", "user_stated") == nr.PLACE
    # structural hierarchy rels → PLACE regardless of provenance
    assert nr.derive_object_role(conn, "instance_of", "llm_inferred") == nr.PLACE
    assert nr.derive_object_role(conn, "subclass_of", "llm_inferred") == nr.PLACE


def test_inferred_value_slot_not_stamped_value():
    # a VALUE requires user_stated authority; an llm_inferred edge at an unconstrained slot is
    # AMBIGUOUS (could be engine growth) → None, never a stamped value (and never a bare place either,
    # so it can't bury a later user value).
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    assert nr.derive_object_role(conn, "favorite_colour", "llm_inferred") is None


# ── stamp + monotonic authority-ordered transition ───────────────────────────────────────────────

def test_stamp_value_then_protected_and_not_laddered():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    role = nr.stamp_edge_object(conn, "blue", "favorite_colour", "user_stated")
    assert role == nr.VALUE
    assert conn.roles["blue"] == "value"
    assert nr.is_protected_value(conn, "blue") is True


def test_genuine_type_node_not_protected():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    role = nr.stamp_edge_object(conn, "dog", "instance_of", "llm_inferred")
    assert role == nr.PLACE
    assert nr.is_protected_value(conn, "dog") is False  # a real type still ladders


def test_homonym_punning_value_then_type_is_both_and_stays_protected():
    # THE HARD LINE is binding, not string. Same surface: first bound as a user VALUE, later a type
    # binding arrives → PUNNING cell BOTH — value protection STAYS (never silently a bare place).
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    nr.stamp_edge_object(conn, "mercury", "favorite_colour", "user_stated")   # the colour value
    assert conn.roles["mercury"] == "value"
    nr.stamp_node_role(conn, "mercury", nr.PLACE)                               # the planet type
    assert conn.roles["mercury"] == "both"
    assert nr.is_protected_value(conn, "mercury") is True


def test_user_value_on_prior_place_wins_to_both():
    # authority order user > growth: a prior engine PLACE node, then a user_stated VALUE binding →
    # BOTH (user wins; the memory is now authoritative), still protected.
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY, roles={"amber": "place"})
    nr.stamp_edge_object(conn, "amber", "favorite_colour", "user_stated")
    assert conn.roles["amber"] == "both"
    assert nr.is_protected_value(conn, "amber") is True


def test_growth_never_demotes_value_to_bare_place():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY, roles={"blue": "value"})
    # an engine place binding on a value node reaches at most BOTH, never bare place
    nr.stamp_node_role(conn, "blue", nr.PLACE)
    assert conn.roles["blue"] == "both"
    assert conn.roles["blue"] != "place"


# ── name coverage via naming-edge membership (no new write required) ──────────────────────────────

def test_name_protected_via_naming_edge_even_when_unstamped():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY, naming_subjects={"rex"})
    # 'rex' has no persisted node_role but IS a naming-edge subject → protected as a NAME
    assert "rex" not in conn.roles
    assert nr.is_protected_value(conn, "rex") is True
    assert nr.effective_role(conn, "rex") == nr.NAME


# ── in-flight hint preserves ordering-independence at ingest ──────────────────────────────────────

def test_inflight_hint_protects_unstamped_value_object():
    # at ingest the row may not be stamped yet; the in-flight edge hint still protects it.
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    assert nr.is_protected_value(
        conn, "teal", inflight_rel_type="favorite_colour", inflight_provenance="user_stated"
    ) is True
    # the same node with a typed-kind in-flight edge is NOT protected
    assert nr.is_protected_value(
        conn, "car", inflight_rel_type="owns", inflight_provenance="user_stated"
    ) is False


# ── fail-safe: unknown rel / missing column → never over-suppress laddering ───────────────────────

def test_unknown_rel_is_not_value():
    _on()
    conn = _FakeConn(tail_types=_ONTOLOGY)
    # a rel absent from the ontology → tail read misses → AMBIGUOUS (None), never a stamped value →
    # not protected (fail-safe: never over-suppress laddering on an unknown rel).
    assert nr.derive_object_role(conn, "some_novel_rel", "user_stated") is None
    assert nr.is_protected_value(conn, "x", inflight_rel_type="some_novel_rel",
                                 inflight_provenance="user_stated") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
