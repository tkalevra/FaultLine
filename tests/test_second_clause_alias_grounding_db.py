"""ENTITY-ID GROUNDING of the second-clause alias — the literal bar, against a real
PostgreSQL through the REAL EntityRegistry (the seam /ingest grounds edges with).

The deriver suite (test_spine_second_clause_alias_bind.py) pins the EDGE SHAPE: the
second clause of "My wifes name is Dana, she prefers to be called Mars" must emit
(dana, also_known_as, mars) — the FIRST clause's person surface, never the alias
surface in subject position. This suite grounds that edge stream exactly as ingest does
and asserts the literal production bar:

  * ENTITY ID EQUALITY — resolve('mars') == resolve('dana'): the alias points at the
    SAME canonical entity the first clause established;
  * NO NEW ENTITY — the alias surfaces dana/wife/mars all resolve to ONE entity_id.

Also demonstrates WHY the deriver shape is load-bearing: a novel surface grounded in
subject position MINTS a fresh uuid5 entity (the phantom-mint mechanism measured live —
the GLiNER2-relation fallback's (mars, related_to, dana) proposal grounds 'mars' as a
subject when the deriver drops the clause).

Skips cleanly when no throwaway database is offered via ALIAS_BIND_TEST_DSN (the
WELD_GUARD_TEST_DSN pattern). Schema mirrors the production column types that matter
(TEXT ids).
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.entity_registry.registry import EntityRegistry  # noqa: E402

DSN = os.environ.get("ALIAS_BIND_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set ALIAS_BIND_TEST_DSN to a THROWAWAY postgres to run the grounding tests",
)

_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE entities (id text PRIMARY KEY, entity_type text DEFAULT 'unknown');
CREATE TABLE entity_aliases (
    entity_id text, alias text, display_form text,
    is_preferred boolean DEFAULT false, preference_source text DEFAULT 'unspecified',
    PRIMARY KEY (entity_id, alias));
CREATE TABLE entity_name_conflicts (
    id serial PRIMARY KEY, alias text, status text DEFAULT 'pending');
CREATE TABLE rel_types (rel_type text PRIMARY KEY, wikidata_pid text);
CREATE TABLE facts (
    subject_id text, object_id text, rel_type text,
    fact_provenance text, fact_class text,
    superseded_at timestamptz, archived_at timestamptz);
CREATE TABLE staged_facts (
    subject_id text, object_id text, rel_type text,
    provenance text, fact_class text,
    promoted_at timestamptz, deleted_at timestamptz);
CREATE TABLE entity_types (entity_type text PRIMARY KEY, is_learnable boolean);
INSERT INTO entity_types VALUES ('person', true), ('location', true), ('animal', true);
"""


@pytest.fixture()
def reg():
    sch = "ab_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
    conn.commit()
    yield EntityRegistry(conn, auto_commit=True, schema_name=sch), conn
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def _edges_of_turn():
    """The deriver's edge stream for the exact production turn, two atoms.

    Atom 1: (dana, spouse, user) + (dana, also_known_as, wife).
    Atom 2 (the fix): (dana, also_known_as, mars) — the FIRST clause's person surface.
    Read LIVE from the deriver when the linguistic layer is available so this suite
    cannot drift from the capture it grounds; the literals are the fallback shape.
    """
    try:
        from src.extraction import linguistics as L
        if L.linguistics_available():
            import datetime
            ref = datetime.date(2026, 8, 20)
            trn = L.build_turn_role_name_map("My wifes name is Dana") or {}
            f1 = L.derive_sentence_facts("My wifes name is Dana", ref,
                                         turn_role_names=trn)
            f2 = L.derive_sentence_facts("she prefers to be called Mars", ref,
                                         prior_nps=["user", "wife"],
                                         turn_persons=["dana", "mars"],
                                         turn_role_names=trn)
            return [(f.subject, f.rel_type, f.object) for f in (f1 + f2)]
    except Exception:  # noqa: BLE001 — fall through to the pinned shape
        pass
    return [("dana", "spouse", "user"), ("dana", "also_known_as", "wife"),
            ("dana", "also_known_as", "mars")]


def test_second_clause_alias_grounds_on_the_first_clause_entity(reg):
    registry, conn = reg
    user = str(uuid.uuid4())
    user_eid = registry.resolve(user, "user")

    edges = _edges_of_turn()
    person_surface = next(s for s, r, o in edges if r == "spouse")
    person_eid = registry.resolve(user, person_surface)
    registry.register_alias(person_eid, person_surface, True, "Person")
    for s, r, o in edges:
        if r == "also_known_as":
            registry.register_alias(registry.resolve(user, s), o, False, "Person")

    # ENTITY ID EQUALITY: the alias resolves back to the SAME canonical entity
    assert registry.resolve(user, "mars") == person_eid
    assert registry.resolve(user, person_surface) == person_eid

    # NO NEW ENTITY: one canonical entity carries dana/wife/mars
    with conn.cursor() as cur:
        cur.execute("SET search_path TO " + registry.schema_name)
        cur.execute(
            "SELECT count(DISTINCT entity_id) FROM entity_aliases "
            "WHERE alias IN (%s, 'wife', 'mars')", (person_surface,))
        assert cur.fetchone()[0] == 1


def test_subject_position_grounding_mints_a_new_entity(reg):
    """The phantom mechanism, demonstrated: a NOVEL surface grounded as a SUBJECT mints
    a fresh uuid5 entity. This is why the deriver must never emit the alias surface in
    subject position (the GLiNER2 fallback's (mars, related_to, dana) shape)."""
    registry, _ = reg
    user = str(uuid.uuid4())
    person_eid = registry.resolve(user, "dana")
    phantom_eid = registry.resolve(user, "mars")  # novel surface -> mint
    assert phantom_eid != person_eid
    with reg[1].cursor() as cur:
        cur.execute("SET search_path TO " + registry.schema_name)
        cur.execute("SELECT count(*) FROM entities")
        assert cur.fetchone()[0] == 2  # two DISTINCT canonical entities minted
