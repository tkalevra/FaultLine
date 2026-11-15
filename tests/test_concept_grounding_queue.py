"""Unit tests for `_queue_concept_for_grounding` (understanding-hierarchy arc, 1b).

A relational edge whose OBJECT is a clean novel concept (e.g. "i feel jealous" ->
(user, feels, jealous)) is NOT a mis-slot, so the existing miss-pushback directive
never fires for it -> no is-a rung ever builds. This helper queues the object for
the background "what is X?" grounding by writing the SAME `ingest_miss_pushback`
ontology_evaluations row the mis-slot path writes.

These tests use a fake DB connection/cursor (no model, no real DB) and assert:
  - flag default OFF (OFF == today),
  - the queue decision is SUBJECT-AGNOSTIC (driven by the object being a novel
    concept, identical for two different rel names — proves no rel-name check),
  - typed objects + already-laddered objects + the user/subject are NOT queued,
  - the row shape (extraction_method='ingest_miss_pushback', concept in
    sample_object, real rel in the NOT-NULL candidate_rel_type) + schema-qualified
    INSERT (per-tenant search_path, never public).
NOTE: tests/ is gitignored; commit with `git add -f`.
"""
import importlib

import pytest

main = importlib.import_module("src.api.main")


class _FakeCursor:
    def __init__(self, entity_type="unknown", has_isa=False, capture=None):
        self._entity_type = entity_type
        self._has_isa = has_isa
        self._last = None
        self.capture = capture if capture is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._last = (s, params)
        self.capture.append((s, params))

    def fetchone(self):
        s = self._last[0] if self._last else ""
        if "SELECT entity_type FROM entities" in s:
            return (self._entity_type,) if self._entity_type is not None else None
        if "rel_type IN ('instance_of', 'subclass_of')" in s:
            return (1,) if self._has_isa else None
        return None


class _FakeConn:
    def __init__(self, entity_type="unknown", has_isa=False):
        self.capture = []
        self._cur = _FakeCursor(entity_type, has_isa, self.capture)

    def cursor(self):
        return self._cur

    def rollback(self):
        pass


SCHEMA = "faultline_testtenant"
USER = "11111111-1111-5111-8111-111111111111"
OBJ = "22222222-2222-5222-8222-222222222222"


def _inserts(conn):
    return [c for c in conn.capture if "INSERT INTO" in c[0] and "ontology_evaluations" in c[0]]


def test_flag_default_off(monkeypatch):
    """Default OFF == today: nothing is queued unless explicitly enabled."""
    monkeypatch.delenv("INGEST_QUEUE_CONCEPT_GROUNDING", raising=False)
    conn = _FakeConn(entity_type="unknown")
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "jealous", "feels", USER) is False
    assert _inserts(conn) == []


def test_novel_concept_queued(monkeypatch):
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="unknown", has_isa=False)
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "Jealous", "feels", USER) is True
    ins = _inserts(conn)
    assert len(ins) == 1
    sql, params = ins[0]
    # schema-qualified INSERT — per-tenant, never public.ontology_evaluations
    assert f"INSERT INTO {SCHEMA}.ontology_evaluations" in sql
    # row shape: candidate_rel_type (NOT NULL) carries the real rel; the concept
    # (lowercased) sits in sample_object; extraction_method is the consumer's key.
    assert params[0] == "feels"               # candidate_rel_type
    assert params[3] == USER                  # sample_subject_id
    assert params[4] == "jealous"             # sample_object (lowercased)
    assert params[5] == "ingest_miss_pushback"
    # idempotent ON CONFLICT upsert
    assert "ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)" in sql


def test_subject_agnostic_no_rel_name_check(monkeypatch):
    """A DIFFERENT rel name with the same novel-concept object queues identically —
    proving the decision is 'novel concept-object', not a 'feels' special-case."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn_feels = _FakeConn(entity_type="unknown")
    conn_likes = _FakeConn(entity_type="unknown")
    assert main._queue_concept_for_grounding(
        conn_feels, SCHEMA, OBJ, "jealous", "feels", USER) is True
    assert main._queue_concept_for_grounding(
        conn_likes, SCHEMA, OBJ, "jealous", "likes", USER) is True
    # both queued; only the carried rel differs (candidate_rel_type)
    assert _inserts(conn_feels)[0][1][0] == "feels"
    assert _inserts(conn_likes)[0][1][0] == "likes"


def test_typed_object_not_queued(monkeypatch):
    """An already-typed object (Person/Animal/...) is NOT a novel concept -> skip."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="Person")
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "alice", "knows", USER) is False
    assert _inserts(conn) == []


def test_concept_typed_object_is_queued(monkeypatch):
    """entity_type == 'Concept' (un-laddered) IS a queue candidate."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="Concept", has_isa=False)
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "melancholy", "feels", USER) is True
    assert len(_inserts(conn)) == 1


def test_already_laddered_object_not_queued(monkeypatch):
    """An object that already has an is-a edge has a rung -> not an island -> skip."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="unknown", has_isa=True)
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "fear", "feels", USER) is False
    assert _inserts(conn) == []


def test_user_or_subject_object_not_queued(monkeypatch):
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="unknown")
    # object == the subject itself
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, USER, "me", "knows", USER) is False
    # object == literal "user"
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, "user", "user", "knows", USER) is False
    assert _inserts(conn) == []


def test_no_schema_refuses_public_fallthrough(monkeypatch):
    """Growth writes must land in a tenant schema; never public."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")
    conn = _FakeConn(entity_type="unknown")
    assert main._queue_concept_for_grounding(
        conn, None, OBJ, "jealous", "feels", USER) is False
    assert _inserts(conn) == []


def test_failsafe_never_raises(monkeypatch):
    """A DB error inside the helper is swallowed (never blocks the commit)."""
    monkeypatch.setenv("INGEST_QUEUE_CONCEPT_GROUNDING", "true")

    class _BoomCursor(_FakeCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("db blew up")

    class _BoomConn(_FakeConn):
        def __init__(self):
            super().__init__()
            self._cur = _BoomCursor()

    conn = _BoomConn()
    # must return False, not raise
    assert main._queue_concept_for_grounding(
        conn, SCHEMA, OBJ, "jealous", "feels", USER) is False
