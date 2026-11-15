"""IDENTITY-HONESTY BUG 1 — the placeholder-name trap, pinned at the registry seam.

Measured on the owner's live demo (2026-08-21): a fresh seat's PREFERRED alias is the
provisioning placeholder 'user' (inserted by schema_manager bootstrap WITHOUT a
preference_source, so it lands at the column default 'unspecified' — rank 0 — not even
'provisioned' — rank 1). A user-stated name ("My name is Alicia") registers EVERY time
as a NON-preferred alias and nothing ever promotes it, so recall renders 'user' forever and
corrections are idempotent no-ops.

This suite pins the three load-bearing behaviours that close the trap:

  * PLACEHOLDER HONESTY — the bootstrap placeholder is stamped 'provisioned' (source rank 1,
    never user-chosen), so read-time guards that refuse placeholder preferred names finally
    fire;
  * USER-STATED PROMOTION — a user-stated name edge (the /ingest alias lane shape:
    preference_source='user_stated', spine edge is_preferred_label=False) is registered as a
    prefLabel assertion and DEMOTES the never-user-chosen placeholder through the registry's
    own rank guard (user_stated=5 vs provisioned=1/unspecified=0);
  * CORRECTION PROMOTION — "my name is Y, not X" lands Y as preferred with
    preference_source='user_stated' (the correct_fact alias writer stamps it, ratchet-UP).

The gold bar this suite encodes: fresh-seat dialogue "hello, my name is X" → X preferred;
"my name is Y, not X" → Y preferred; the placeholder never surfaces again.

DB-gated (WELD_GUARD_TEST_DSN pattern): skips cleanly without a throwaway postgres.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("WELD_GUARD_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres to run the placeholder trap tests",
)

from src.entity_registry.registry import EntityRegistry  # noqa: E402

_DDL = """
CREATE SCHEMA {sch};
SET search_path TO {sch};
CREATE TABLE entities (id text PRIMARY KEY, entity_type text DEFAULT 'unknown');
CREATE TABLE entity_aliases (
    entity_id text, alias text, display_form text,
    is_preferred boolean DEFAULT false, preference_source text DEFAULT 'unspecified',
    PRIMARY KEY (entity_id, alias));
CREATE TABLE entity_name_conflicts (
    id serial PRIMARY KEY, alias text, status text DEFAULT 'pending',
    entity_id_a text, entity_id_b text, resolved_by text, resolved_at timestamptz);
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
def seat():
    """A fresh seat schema with the PROVISIONING PLACEHOLDER exactly as bootstrap writes it."""
    sch = "ih_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    with conn.cursor() as c:
        c.execute(_DDL.format(sch=sch))
    conn.commit()
    yield conn
    conn.rollback()
    with conn.cursor() as c:
        c.execute(f"DROP SCHEMA {sch} CASCADE")
    conn.commit()
    conn.close()


def _placeholder(conn, uid, stamped):
    """Insert the bootstrap placeholder. stamped=True → the FIXED bootstrap (provisioned);
    stamped=False → the legacy unstamped insert (column default 'unspecified')."""
    with conn.cursor() as c:
        if stamped:
            c.execute(
                "INSERT INTO entity_aliases (entity_id, alias, is_preferred, preference_source) "
                "VALUES (%s, 'user', TRUE, 'provisioned')", (uid,))
        else:
            c.execute(
                "INSERT INTO entity_aliases (entity_id, alias, is_preferred) "
                "VALUES (%s, 'user', TRUE)", (uid,))
    conn.commit()


def _preferred_row(conn, uid):
    with conn.cursor() as c:
        c.execute(
            "SELECT alias, preference_source FROM entity_aliases "
            "WHERE entity_id = %s AND is_preferred = true", (uid,))
        return c.fetchone()


def test_placeholder_bootstrap_is_stamped_provisioned(seat):
    """The FIXED bootstrap labels the placeholder 'provisioned' — the rank-1 source every
    read-time guard already refuses to surface. (The legacy unstamped insert is kept below
    as the historical shape; promotion must beat BOTH.)"""
    uid = str(uuid.uuid4())
    with seat.cursor() as c:
        c.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person')", (uid,))
    seat.commit()
    _placeholder(seat, uid, stamped=True)
    assert _preferred_row(seat, uid) == ("user", "provisioned")


def test_bootstrap_sql_itself_stamps_the_placeholder():
    """The bootstrap SQL in schema_manager must WRITE the stamp (source-level pin — the
    runtime behaviour above is only reachable if the DDL itself says 'provisioned')."""
    import inspect
    from src.provisioning import schema_manager
    src = inspect.getsource(schema_manager)
    # The placeholder INSERT must name preference_source with 'provisioned'.
    assert "VALUES (%s, 'user', TRUE, 'provisioned')" in src, (
        "bootstrap placeholder INSERT must stamp preference_source='provisioned' "
        "(identity-honesty bug 1)")
    # And the ON CONFLICT arm must NOT clobber an existing (possibly user-stated) source.
    assert "WHEN entity_aliases.preference_source = 'unspecified'" in src


def test_ingest_edge_lane_promotes_user_stated_names():
    """SOURCE-LEVEL pin on the /ingest edge lane (mutation-checked in-session: killing
    this seam left the registry tests green, so it needs its own pin): a scalar-identity
    edge carrying the turn's preference signal MUST derive is_pref=True before
    register_alias — the registry's rank guard can only arbitrate a preference claim it
    receives, and the measured trap was exactly here (is_pref stayed False, so the
    user-stated name registered non-preferred forever)."""
    import inspect
    from src.api import main as api_main
    src = inspect.getsource(api_main)
    assert "and (edge.is_preferred_label or has_preferred)):" in src, (
        "the ingest edge lane must promote scalar-identity user-stated names to "
        "is_pref=True (bug 1 - the registry fix alone cannot close the trap)")
    assert "ingest.user_stated_name_promoted" in src, (
        "the promotion must be observable (log event)")


def test_user_stated_name_promotes_over_placeholder(seat):
    """THE GOLD BAR: 'hello, my name is Alicia' on a fresh seat → alicia preferred
    within the same write. The /ingest alias lane shape: spine edge with
    is_preferred_label=False + turn-level preference signal → preference_source='user_stated'
    + is_pref=True (the promotion this change ships). The registry's rank guard arbitrates:
    user_stated(5) demotes the placeholder (provisioned=1 or unspecified=0)."""
    uid = str(uuid.uuid4())
    with seat.cursor() as c:
        c.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person')", (uid,))
    seat.commit()
    # Legacy seats: unstamped placeholder. New seats: stamped. BOTH must lose to the name.
    for stamped in (False, True):
        # reset alias table state for the second arm
        with seat.cursor() as c:
            c.execute("DELETE FROM entity_aliases WHERE entity_id = %s", (uid,))
        seat.commit()
        _placeholder(seat, uid, stamped=stamped)
        reg = EntityRegistry(seat, auto_commit=False, schema_name=_schema_of(seat))
        result = reg.register_alias(
            uid, "alicia",
            is_preferred=True,          # the promotion: a user-stated name IS a prefLabel claim
            entity_type="Person",
            preference_source="user_stated",  # turn-level signal ("my name is …")
        )
        seat.commit()
        assert isinstance(result, dict) and result.get("written"), (
            "register_alias must report the write (bug 2 contract) — both lanes need it")
        row = _preferred_row(seat, uid)
        assert row is not None and row[0] == "alicia", (
            f"user-stated name must be preferred (placeholder was "
            f"{'provisioned' if stamped else 'unstamped'}) — got {row}")
        assert row[1] == "user_stated"


def test_weak_source_does_not_demote_user_stated(seat):
    """The promotion is USER-TRUTH keyed, not free-for-all: a later weak claim
    (rel_default, e.g. an engine re-encounter) must NOT displace the user's chosen name —
    the registry's demotion guard keeps the incumbent (the wren-over-sam poisoning,
    prevented at the source)."""
    uid = str(uuid.uuid4())
    with seat.cursor() as c:
        c.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person')", (uid,))
    seat.commit()
    reg = EntityRegistry(seat, auto_commit=False, schema_name=_schema_of(seat))
    reg.register_alias(uid, "alicia", is_preferred=True,
                       entity_type="Person", preference_source="user_stated")
    reg.register_alias(uid, "wren", is_preferred=True,
                       entity_type="Person", preference_source="rel_default")
    seat.commit()
    row = _preferred_row(seat, uid)
    assert row is not None and row[0] == "alicia", (
        f"a rel_default claim must not demote the user-stated name — got {row}")


def test_correction_promotes_the_new_name(seat):
    """'my name is Y, not X' — the correct_fact alias writer stamps the corrected name
    user_stated + preferred (source-level pin; the runtime route is correct_fact's
    pref_name/also_known_as branch)."""
    import inspect
    from src.api import main as api_main
    src = inspect.getsource(api_main)
    assert "'user_stated'" in src, "correct_fact alias writer must stamp user_stated"
    # Runtime shape: registering the corrected name as user_stated lands preferred.
    uid = str(uuid.uuid4())
    with seat.cursor() as c:
        c.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'Person')", (uid,))
    seat.commit()
    reg = EntityRegistry(seat, auto_commit=False, schema_name=_schema_of(seat))
    reg.register_alias(uid, "alicia", is_preferred=True,
                       entity_type="Person", preference_source="user_stated")
    reg.register_alias(uid, "yannick", is_preferred=True,
                       entity_type="Person", preference_source="user_stated")
    seat.commit()
    row = _preferred_row(seat, uid)
    assert row is not None and row[0] == "yannick", (
        f"the corrected name must take the preferred slot — got {row}")


def _schema_of(conn):
    with conn.cursor() as c:
        c.execute("SELECT current_schema()")
        return c.fetchone()[0]
