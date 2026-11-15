"""AUDIT7 — entity-centric STAGED fetch must be symmetric with the committed fetch.

Gap (audit site #7): `_fetch_entity_facts` fetched committed (`facts`) rows in BOTH
directions (subject_id OR object_id) but fetched STAGED (`staged_facts`) rows subject-side
ONLY. So a Class-B/C staged edge with the recall anchor as the OBJECT — e.g.
`(X, related_to, <entity>)` — was invisible on the entity-centric lane, even though the
committed lane already returns object-side rows.

Fix (query-side, deterministic, in `src/api/main.py`): mirror the committed lane's
both-direction loop + scope projection + anchor-inverse skip for `staged_facts`.

This is a REAL DB-level test: it provisions a disposable schema in the live local
Postgres, seeds committed + staged rows in both directions, and calls the actual
`_fetch_entity_facts`. It does NOT mock cursors (mock-drift has repeatedly given false
greens on this path). Requires POSTGRES_DSN reachable; skips cleanly otherwise.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.api.main import _fetch_entity_facts  # noqa: E402


DSN = os.environ.get("POSTGRES_DSN")


def _dsn_reachable(dsn):
    try:
        c = psycopg2.connect(dsn, connect_timeout=3)
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not DSN or not _dsn_reachable(DSN),
    reason="POSTGRES_DSN not set or not reachable — DB-level test",
)


_DDL = """
CREATE TABLE facts (
    id BIGSERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id  TEXT NOT NULL,
    rel_type   TEXT NOT NULL,
    confidence NUMERIC,
    fact_class TEXT,
    superseded_at TIMESTAMPTZ,
    archived_at   TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ,
    valid_until   TIMESTAMPTZ,
    temporal_status TEXT,
    event_date DATE,
    polarity   TEXT
);
CREATE TABLE staged_facts (
    id BIGSERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    object_id  TEXT NOT NULL,
    rel_type   TEXT NOT NULL,
    confidence NUMERIC,
    fact_class TEXT,
    expires_at TIMESTAMPTZ,
    promoted_at TIMESTAMPTZ,
    deleted_at  TIMESTAMPTZ,
    temporal_status TEXT,
    event_date DATE,
    polarity   TEXT
);
CREATE TABLE entity_attributes (
    entity_id TEXT,
    attribute TEXT,
    value_text TEXT,
    value_int INTEGER,
    value_float DOUBLE PRECISION,
    value_date DATE
);
"""


@pytest.fixture()
def schema_conn():
    """Disposable schema bound via search_path; dropped on teardown."""
    schema = "test_audit7_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(DSN, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute(_DDL)
    try:
        yield conn, schema
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()


def _rels(facts):
    return {(f["subject"], f["rel_type"], f["object"]) for f in facts}


def test_object_side_staged_fact_now_returned(schema_conn):
    """The core AUDIT7 case: (X, related_to, entity) staged with entity as OBJECT."""
    conn, _ = schema_conn
    entity = str(uuid.uuid4())
    x_subj = str(uuid.uuid4())
    with conn.cursor() as cur:
        # Staged fact with the anchor entity as the OBJECT — the previously-dropped case.
        cur.execute(
            "INSERT INTO staged_facts (subject_id, object_id, rel_type, confidence, fact_class,"
            " expires_at, promoted_at) VALUES (%s,%s,'related_to',0.6,'B', now()+interval '30 days', NULL)",
            (x_subj, entity),
        )

    facts = _fetch_entity_facts(conn, "u", {entity})
    rels = _rels(facts)
    assert (x_subj, "related_to", entity) in rels, (
        "object-side staged fact was dropped — AUDIT7 not fixed: " + repr(rels)
    )


def test_subject_side_staged_unchanged(schema_conn):
    """Subject-side staged fetch must still work (no regression)."""
    conn, _ = schema_conn
    entity = str(uuid.uuid4())
    obj = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO staged_facts (subject_id, object_id, rel_type, confidence, fact_class,"
            " expires_at, promoted_at) VALUES (%s,%s,'likes',0.6,'B', now()+interval '30 days', NULL)",
            (entity, obj),
        )
    facts = _fetch_entity_facts(conn, "u", {entity})
    assert (entity, "likes", obj) in _rels(facts)


def test_committed_both_directions_unchanged(schema_conn):
    """Committed (facts) rows still return in both directions."""
    conn, _ = schema_conn
    entity = str(uuid.uuid4())
    subj = str(uuid.uuid4())
    obj = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class)"
            " VALUES (%s,%s,'related_to',1.0,'A')",
            (subj, entity),  # entity as object
        )
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, confidence, fact_class)"
            " VALUES (%s,%s,'owns',1.0,'A')",
            (entity, obj),  # entity as subject
        )
    rels = _rels(_fetch_entity_facts(conn, "u", {entity}))
    assert (subj, "related_to", entity) in rels
    assert (entity, "owns", obj) in rels


def test_scope_projection_no_object_side_leak(schema_conn):
    """Under an ACTIVE scope (allowed_rels), an OUT-OF-SCOPE object-side staged fact
    must NOT leak — the fix mirrors the committed lane's rel projection exactly."""
    conn, _ = schema_conn
    entity = str(uuid.uuid4())
    x_in = str(uuid.uuid4())
    x_out = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO staged_facts (subject_id, object_id, rel_type, confidence, fact_class,"
            " expires_at) VALUES (%s,%s,'related_to',0.6,'B', now()+interval '30 days')",
            (x_in, entity),
        )
        cur.execute(
            "INSERT INTO staged_facts (subject_id, object_id, rel_type, confidence, fact_class,"
            " expires_at) VALUES (%s,%s,'has_pet',0.6,'B', now()+interval '30 days')",
            (x_out, entity),
        )
    rels = _rels(_fetch_entity_facts(conn, "u", {entity}, allowed_rels={"related_to"}))
    assert (x_in, "related_to", entity) in rels, "in-scope object-side staged dropped"
    assert (x_out, "has_pet", entity) not in rels, "OUT-OF-SCOPE object-side staged LEAKED"


def test_anchor_inverse_skip_mirrors_committed(schema_conn):
    """Mirror the committed lane's anchor-inverse skip: a subject-side staged row whose
    OBJECT is the anchor is skipped (already rendered by the inverse anchor walk); the
    same row reached via the entity-as-object pass is NOT anchor-skipped."""
    conn, _ = schema_conn
    anchor = str(uuid.uuid4())
    entity = str(uuid.uuid4())  # some connected entity being expanded
    with conn.cursor() as cur:
        # (entity)-related_to->(anchor): subject-side pass points back at anchor → skip.
        cur.execute(
            "INSERT INTO staged_facts (subject_id, object_id, rel_type, confidence, fact_class,"
            " expires_at) VALUES (%s,%s,'related_to',0.6,'B', now()+interval '30 days')",
            (entity, anchor),
        )
    # Expanding `entity` with anchor set: the subject-side hit has object==anchor → skipped,
    # exactly like the committed lane. (entity is not the object here, so no object-side re-add.)
    rels = _rels(_fetch_entity_facts(conn, "u", {entity}, anchor_uuid=anchor))
    assert (entity, "related_to", anchor) not in rels
