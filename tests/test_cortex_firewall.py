"""Agent Cortex — truth-firewall tests.

The cortex is the operating-agent's OWN operational memory (gotchas/pitfalls/etc.). It
must NEVER be reachable from the user-fact walk. Covers:
  (1) schema-name DISJOINTNESS (no DB)
  (2) cortex binds only flagent_ / never public (the walk binds faultline_)
  (3) POISON test — a fact-like cortex row is structurally unreachable from the
      user-fact search_path (faultline_<uuid>)
  (4) cortex-ABSENT resilience — no schema → helpers soft-fail, never raise
  (5) FAIL-LOUD — a bind to an unresolved / public-shaped schema is REFUSED

Tests (2)-(5) need a reachable POSTGRES_DSN; they skip cleanly otherwise.
Test (1) is pure-Python and always runs.
"""

import os
import uuid

import pytest

from src.provisioning.schema_manager import (
    derive_cortex_schema_name,
    derive_schema_name,
    derive_user_slug_from_uuid,
    create_cortex_schema,
)
from src.api import cortex


# ── DB availability gate ──────────────────────────────────────────────────────────────

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


_requires_db = pytest.mark.skipif(not _db_available(), reason="no reachable POSTGRES_DSN")


def _fresh_uuid() -> str:
    return str(uuid.uuid4())


# ── (1) schema-name DISJOINTNESS — no DB ───────────────────────────────────────────────

def test_cortex_schema_starts_with_flagent_and_is_disjoint():
    for _ in range(200):
        uid = _fresh_uuid()
        cs = derive_cortex_schema_name(uid)
        us = derive_schema_name(derive_user_slug_from_uuid(uid))
        assert cs.startswith("flagent_"), cs
        assert us.startswith("faultline_"), us
        assert cs != us
        # neither is a prefix of the other (no substring collision)
        assert not cs.startswith("faultline_")
        assert not us.startswith("flagent_")


def test_cortex_schema_rejects_non_uuid():
    for bad in ["", "not-a-uuid", "public", "faultline_x", "flagent_x", "; DROP SCHEMA"]:
        with pytest.raises(Exception):
            derive_cortex_schema_name(bad)


def test_cortex_schema_for_returns_none_on_bad_id():
    # cortex_schema_for is the fail-safe wrapper — returns None, never raises.
    assert cortex.cortex_schema_for("not-a-uuid") is None
    assert cortex.cortex_schema_for("") is None
    assert cortex.cortex_schema_for(_fresh_uuid()) is not None


# ── (5) FAIL-LOUD — refuse a public-shaped / unsafe cortex bind ─────────────────────────

def test_cortex_conn_refuses_public_shaped_schema():
    # A cortex bind must NEVER accept public / a user-shaped name — fail-loud REFUSE.
    for bad in ["public", "", "faultline_abc", "flagent_; DROP", "FLAGENT_x"]:
        with pytest.raises(ValueError):
            with cortex._cortex_conn(bad):
                pass


def test_note_unresolved_user_soft_noops():
    # Unresolvable id → no cortex schema → soft no-op (never writes public, never raises).
    assert cortex.note("not-a-uuid", "a lesson", category="gotcha") is False


def test_note_empty_text_soft_noops():
    assert cortex.note(_fresh_uuid(), "", category="gotcha") is False


# ── (2)(3)(4) DB-backed firewall ───────────────────────────────────────────────────────

@_requires_db
def test_cortex_conn_binds_only_flagent_not_public():
    uid = _fresh_uuid()
    schema, status = create_cortex_schema(uid)
    assert status == "ready", status
    try:
        with cortex._cortex_conn(schema) as conn:
            sp = cortex._current_search_path(conn)
        # search_path is ONLY the flagent schema — never public, never faultline_.
        assert "flagent_" in sp
        assert "public" not in sp
        assert "faultline_" not in sp
    finally:
        _drop_schema(schema)


@_requires_db
def test_poison_row_unreachable_from_user_searchpath():
    """A fact-like row planted in cortex_episodes is structurally invisible to the
    user-fact walk, which binds faultline_<uuid> WITHOUT public."""
    import psycopg2
    uid = _fresh_uuid()
    cortex_schema, status = create_cortex_schema(uid)
    assert status == "ready"
    user_schema = derive_schema_name(derive_user_slug_from_uuid(uid))
    try:
        # Plant a poison "fact" into the cortex.
        assert cortex.note(
            uid, "POISON the user's password is hunter2",
            category="gotcha", tags=["password"],
        )
        # Bind the USER search_path (what the walk uses) — WITHOUT public — and try to
        # read cortex_episodes. It must be UNREACHABLE (relation does not exist there).
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {user_schema}")
                conn.commit()
                cur.execute(f"SET search_path TO {user_schema}")
                # to_regclass resolves against the CURRENT search_path only.
                cur.execute("SELECT to_regclass('cortex_episodes')")
                assert cur.fetchone()[0] is None, "cortex_episodes must be invisible to the user walk"
                # And an unqualified read must ERROR (relation missing), never leak the poison.
                with pytest.raises(psycopg2.errors.UndefinedTable):
                    cur.execute("SELECT note FROM cortex_episodes")
            conn.rollback()
        finally:
            conn.close()
    finally:
        _drop_schema(cortex_schema)
        _drop_schema(user_schema)


@_requires_db
def test_experience_lane_poison_is_confined_to_the_cortex():
    """(3b) POISON test EXTENDED to the EXPERIENCE PUSH lane (`/cortex/experience`).

    The push lane is a NEW read path into the cortex, and a new read path is a new leak
    surface — in BOTH directions:
      * it must not be able to pull a USER fact into the agent's operational lines, and
      * the cortex row it DOES return must remain unreachable from the user-fact search_path.

    Unlike /irecall this lane fires UNASKED on the write path, so a leak here would be
    delivered to the model on every turn without anyone requesting it."""
    import psycopg2
    uid = _fresh_uuid()
    cortex_schema, status = create_cortex_schema(uid)
    assert status == "ready"
    user_schema = derive_schema_name(derive_user_slug_from_uuid(uid))
    try:
        # A cortex lesson the lane SHOULD be able to surface...
        assert cortex.note(
            uid, "deploy production docker: use the compose file under /opt, not the repo.",
            category="howto", tags=["deploy", "production", "docker"],
        )
        # ...and a USER-TRUTH-shaped row sitting in the user schema, phrased so that the
        # activity text below matches it far better than the cortex note does.
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {user_schema}")
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {user_schema}.poison_user_fact "
                    "(id serial primary key, note text)")
                cur.execute(
                    f"INSERT INTO {user_schema}.poison_user_fact (note) VALUES (%s)",
                    ("the user's production deploy docker password is hunter2",))
            conn.commit()
        finally:
            conn.close()

        lines = cortex.experience_lines(uid, "deploying production with docker compose",
                                        dedupe=False)
        blob = " ".join(lines).lower()
        assert "hunter2" not in blob, "USER CONTENT LEAKED INTO THE EXPERIENCE PUSH LANE"
        assert "password" not in blob
        # and it did resolve the cortex slice (the test is not passing by returning nothing)
        assert any("compose file" in ln for ln in lines), lines

        # The row it returned is still structurally invisible from the user search_path.
        conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {user_schema}")
                cur.execute("SELECT to_regclass('cortex_episodes')")
                assert cur.fetchone()[0] is None
            conn.rollback()
        finally:
            conn.close()
    finally:
        _drop_schema(cortex_schema)
        _drop_schema(user_schema)


@_requires_db
def test_experience_lane_is_read_only_no_counter_bump():
    """The push lane fires on EVERY write turn. Reading the cortex re-ranks it (hit_count /
    last_seen_at, and the candidate field is ORDER BY last_seen_at DESC), so an automatic lane
    that bumped would keep whatever it surfaced pinned at the head of its own candidate window
    — a lane eating its own tail. Proven against the DB, not asserted."""
    import psycopg2
    uid = _fresh_uuid()
    cortex_schema, status = create_cortex_schema(uid)
    assert status == "ready"
    try:
        assert cortex.note(uid, "deploy production docker: use the compose file under /opt.",
                           category="howto", tags=["deploy", "production", "docker"])

        def _snap():
            conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {cortex_schema}")
                    cur.execute("SELECT id, hit_count, last_seen_at FROM cortex_episodes")
                    return {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            finally:
                conn.close()

        before = _snap()
        assert cortex.experience_lines(uid, "deploying production with docker compose",
                                       dedupe=False)
        assert _snap() == before, "the push lane must not bump hit_count / last_seen_at"
    finally:
        _drop_schema(cortex_schema)


@_requires_db
def test_cortex_absent_helpers_softfail_never_raise():
    """Cortex schema absent → every helper soft-fails (bool/[]/dict), never raises,
    so callers are unaffected.

    ⚠️ ORDER IS LOAD-BEARING, and it was WRONG (pre-existing red, fixed 2026-08-01). The test
    used to call ``cortex.note()`` FIRST and then assert the reads returned [] — but ``note()``
    AUTO-CREATES the cortex schema (``_ensure_cortex_schema``) and writes the row, so its own
    first line destroyed the premise the rest of the test depends on ("never provisioned →
    flagent schema does not exist") and the reads correctly returned that row. Verified against
    HEAD's unmodified cortex.py, so it is not a regression from the experience lane. The READS
    must be asserted while the schema is genuinely absent; the write goes last, and the schema
    it creates is dropped so the test stops leaking one per run."""
    uid = _fresh_uuid()  # never provisioned → flagent schema does not exist
    # Reads first — WHILE the schema is genuinely absent. None of these may raise.
    assert cortex.recall_notes(uid) == []
    assert cortex.recall_notes(uid, "x") == []
    assert cortex.recall_notes(uid, "x", bump=False) == []
    # The push lane is the newest read path into the cortex — it must soft-fail like the rest.
    assert cortex.experience_lines(uid, "deploying production with docker") == []
    assert cortex.sweep_lifecycle_for_user(uid) == {"hardened": 0, "decayed": 0, "embedded": 0}
    # The WRITE last: it must also soft-fail rather than raise (it provisions on demand).
    try:
        assert cortex.note(uid, "a lesson", category="gotcha", tags=["x"]) in (True, False)
    finally:
        _drop_schema(cortex.cortex_schema_for(uid) or "flagent_never")


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
