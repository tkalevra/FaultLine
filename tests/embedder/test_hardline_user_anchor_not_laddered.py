"""HARD LINE — the USER ANCHOR is never given a `subclass_of` edge by the what-is climb.

Regression for the category error: ingesting "My manager is Priya Sharma." queued the user
ANCHOR itself as a novel concept-object, and the re_embedder what-is climb classified it and
minted `(user, subclass_of, role)` (+ the role→social_role→function ladder), which then leaked
into unrelated recalls.

The user anchor (`entity_id == user_id`) is the grounded self — a NAMED INSTANCE by identity,
never a TYPE. It uniquely evades both structural HARD-LINE checks: it carries NO `instance_of`
edge (the self is grounded directly), and its "user" alias reads as a bare TYPE-word. Recognizing
the anchor by IDENTITY (subject-agnostic — user_id is the runtime tenant anchor, not a word list)
closes the gap.

REAL cursor (DB state matters). Needs a provisioned disposable tenant:
    HL_DSN     postgresql://faultline:faultline@<pg-host>:5432/faultline
    HL_SCHEMA  faultline_<slug>
    HL_USER    <user uuid> (== the anchor entity id)
Skips cleanly if unset.
"""
import os
from unittest.mock import patch

import psycopg2
import pytest

from src.re_embedder import embedder
from src.re_embedder.embedder import _is_named_instance, classify_unknown_concepts

DSN = os.environ.get("HL_DSN")
SCHEMA = os.environ.get("HL_SCHEMA")
USER = os.environ.get("HL_USER")

pytestmark = pytest.mark.skipif(
    not (DSN and SCHEMA and USER),
    reason="set HL_DSN / HL_SCHEMA / HL_USER to a provisioned disposable tenant",
)


@pytest.fixture()
def conn():
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute(f'SET search_path TO "{SCHEMA}"')
    c.commit()
    yield c
    c.close()


def _user_subclass_rows(cur):
    cur.execute(
        "SELECT rel_type, object_id FROM staged_facts"
        " WHERE subject_id = %s AND rel_type = 'subclass_of'"
        "   AND promoted_at IS NULL AND deleted_at IS NULL"
        " UNION ALL"
        " SELECT rel_type, object_id FROM facts"
        " WHERE subject_id = %s AND rel_type = 'subclass_of'"
        "   AND superseded_at IS NULL AND archived_at IS NULL",
        (USER, USER),
    )
    return cur.fetchall()


def test_is_named_instance_recognizes_the_anchor(conn):
    """The anchor evades the edge/naming-layer checks; identity recognition is what saves it.

    Without user_id (the OLD signature) the real user entity reads as NOT-a-name (the bug);
    WITH user_id it is correctly a named instance → subclass_of climb is skipped.
    """
    assert _is_named_instance(conn, USER) is False  # the gap the bug fell through
    assert _is_named_instance(conn, USER, user_id=USER) is True  # HARD LINE, fixed


def test_whatis_climb_never_ladders_the_user_anchor(conn):
    """Full consumer: a queued concept='user' + a Role-returning LLM must NOT ladder the user."""
    with conn.cursor() as cur:
        # Clean slate for the anchor: drop any polluted subclass_of edges on the user.
        cur.execute(
            "DELETE FROM staged_facts WHERE subject_id = %s AND rel_type = 'subclass_of'",
            (USER,),
        )
        cur.execute(
            "DELETE FROM facts WHERE subject_id = %s AND rel_type = 'subclass_of'",
            (USER,),
        )
        # Queue the ANCHOR as a novel concept-object (undecided) — the exact bad row ingest wrote.
        cur.execute(
            "INSERT INTO ontology_evaluations"
            "  (candidate_rel_type, candidate_subject_type, candidate_object_type,"
            "   sample_subject_id, sample_object, extraction_method,"
            "   first_text_snippet, decision_reason, occurrence_count, last_seen_at,"
            "   re_embedder_decision)"
            "  VALUES ('related_to','unknown','unknown', %s, 'user', 'ingest_miss_pushback',"
            "          'My manager is Priya Sharma.', 'test-hardline', 1, now(), NULL)"
            "  ON CONFLICT (candidate_rel_type, sample_subject_id, sample_object)"
            "  DO UPDATE SET re_embedder_decision = NULL, occurrence_count = 1, last_seen_at = now()",
            (USER,),
        )
    conn.commit()

    # The live LLM classified "user" as a Role under social_role — reproduce that verdict.
    def _fake_whatis(concept, url, context=None):
        return {"entity_type": "Role", "parent": "social_role"}

    with patch.object(embedder, "_query_llm_what_is", _fake_whatis):
        classify_unknown_concepts(conn, "http://fake-brain", user_id=USER, schema_name=SCHEMA)

    with conn.cursor() as cur:
        rows = _user_subclass_rows(cur)
    assert rows == [], f"HARD LINE violated: user was laddered → {rows}"
