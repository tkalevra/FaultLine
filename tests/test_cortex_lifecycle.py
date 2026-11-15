"""Agent Cortex — operational-note lifecycle (dedup / harden) + recall tests.

The cortex is the operating-agent's OWN operational memory. A repeated note (same
category + normalized text) STRENGTHENS one row (amplification suppression) and HARDENS
into a rule at observation_count >= 3. Recall is DETERMINISTIC keyword + tag matching
(no cosine). Manual notes are durable (expires_at NULL) and never decay. Needs a
reachable POSTGRES_DSN; skips cleanly otherwise.
"""

import os
import uuid

import pytest

from src.provisioning.schema_manager import create_cortex_schema
from src.api import cortex


def _db_available() -> bool:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no reachable POSTGRES_DSN")


def _fresh_uuid() -> str:
    return str(uuid.uuid4())


def _drop_schema(schema: str) -> None:
    import psycopg2
    try:
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _row(schema: str, note_like: str) -> dict:
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "SELECT category, source, lifecycle_state, observation_count,"
                "       expires_at, tags"
                " FROM cortex_episodes WHERE note ILIKE %s ORDER BY id DESC LIMIT 1",
                (f"%{note_like}%",),
            )
            r = cur.fetchone()
        return None if not r else {
            "category": r[0], "source": r[1], "lifecycle_state": r[2],
            "observation_count": r[3], "expires_at": r[4], "tags": r[5],
        }
    finally:
        conn.close()


def _count(schema: str) -> int:
    """Manual-note count only. Schema creation seeds the curated usage-pattern set
    (source='seed_usage') into every fresh cortex; these tests exercise the MANUAL write path,
    so seeded rows must not be counted against dedup expectations."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT count(*) FROM cortex_episodes WHERE source = 'agent_manual'")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _set_expired(schema: str, note_like: str) -> None:
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                "UPDATE cortex_episodes SET expires_at = now() - INTERVAL '1 day'"
                " WHERE note ILIKE %s",
                (f"%{note_like}%",),
            )
        conn.commit()
    finally:
        conn.close()


def test_upsert_dedup_strengthens_one_row_and_hardens():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        note = "docker compose up --build never on the truenas deploy path"
        for _ in range(3):
            cortex.note(uid, note, category="gotcha", tags=["docker", "deploy"])
        assert _count(schema) == 1                       # ONE row, not three
        row = _row(schema, "docker compose")
        assert row["observation_count"] == 3             # bumped
        assert row["lifecycle_state"] == "hardened"      # hardens at >= 3
    finally:
        _drop_schema(schema)


def test_same_note_different_category_is_distinct_row():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        text = "watch the deploy path"
        cortex.note(uid, text, category="gotcha")
        cortex.note(uid, text, category="pitfall")
        assert _count(schema) == 2  # dedup key is (category, note) — category differs
    finally:
        _drop_schema(schema)


def test_manual_note_is_durable_agent_manual():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        assert cortex.note(uid, "use git add -f for new files under tests/",
                           category="pitfall", tags=["git", "tests"])
        row = _row(schema, "git add -f")
        assert row["source"] == "agent_manual"
        assert row["lifecycle_state"] == "observed"
        assert row["expires_at"] is None                 # durable
    finally:
        _drop_schema(schema)


def test_bad_category_defaults_to_gotcha():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "some lesson with a bad category", category="not_an_enum")
        assert _row(schema, "some lesson")["category"] == "gotcha"
    finally:
        _drop_schema(schema)


def test_sweep_decays_expired_but_durable_never_decays():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "an ephemeral lesson", category="gotcha")   # durable by default
        cortex.note(uid, "a durable lesson", category="howto")
        # Force one row past expiry (simulating a future decay-eligible source), then sweep.
        _set_expired(schema, "ephemeral")
        res = cortex.sweep_lifecycle_for_user(uid)
        assert res["decayed"] >= 1
        assert _row(schema, "ephemeral")["lifecycle_state"] == "decayed"
        # the durable row (expires_at NULL) is untouched.
        assert _row(schema, "durable")["lifecycle_state"] == "observed"
    finally:
        _drop_schema(schema)


def test_recall_matches_by_keyword_and_tag_topk():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        cortex.note(uid, "LLM endpoint is 192.168.40.20:8080, NOT :1234",
                    category="gotcha", tags=["llm-endpoint", "config", "port", "model"])
        cortex.note(uid, "new files under tests/ are gitignored, use git add -f",
                    category="pitfall", tags=["git", "tests"])
        # FUZZY query — no exact words shared with the note text; tags bridge it.
        hits = cortex.recall_notes(uid, "what port for the model", limit=5)
        assert hits, "keyword/tag recall returned nothing"
        assert "192.168.40.20" in hits[0]["note"]        # right note on top
        # a query with no overlap returns nothing (honest miss, no leak).
        assert cortex.recall_notes(uid, "unrelated aquarium submarine") == []
        # bare recall (no query) returns the most-relevant/recent top-K.
        assert len(cortex.recall_notes(uid)) == 2
    finally:
        _drop_schema(schema)
