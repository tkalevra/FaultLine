"""Teeth for ``DEV/tools/collapse_repair.py`` — against a REAL PostgreSQL, not a mocked cursor.

WHY A REAL DATABASE. The arrival guard this tool shares its rule with was once "covered" by a
mocked cursor that answered the class-side probe from a boolean, so the SQL never executed and
inverting the P31 subject/object direction — which makes every named instance look like a class,
the exact HARD-LINE violation the module exists to prevent — left the suite fully green. A mock
that short-circuits the statement under test is not coverage. Everything here runs the real SQL
against production column TYPES (ids are TEXT) and the real UNIQUE constraints.

WHY A SYNTHETIC FIXTURE ON TOP OF THE PRODUCTION REPLICA RUN. The end-to-end run against the
production replica repaired 3 entities and every one of them had ZERO relational edges and ZERO
scalars, so it exercised only the alias-retraction path. ``REPOINT_EDGE`` and ``REPOINT_ATTR`` —
and, more importantly, their RESTORE halves — were never executed. That is precisely how the
attribute restore shipped as a silent no-op (``SET entity_id = X WHERE entity_id = X``). These
fixtures force both paths, then assert byte-identity after restore.

Gated on ``COLLAPSE_REPAIR_TEST_DSN`` so the suite stays runnable without a database.
"""
from __future__ import annotations

import importlib.util
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("COLLAPSE_REPAIR_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLAPSE_REPAIR_TEST_DSN not set")

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOL = os.path.join(os.path.dirname(_HERE), "DEV", "tools", "collapse_repair.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("collapse_repair", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load_tool()

# A fixed but deliberately SYNTHETIC seat UUID, so surrogates are reproducible run to run AND the
# derived schema name can never collide with a real tenant.
#
# ⚠️ THIS IS NOT COSMETIC. The fixture does `DROP SCHEMA ... CASCADE`, and the natural throwaway
# database to point this at is the same one holding a loaded production REPLICA. An earlier
# version used a real production seat UUID here; the derived schema name matched the replica's
# own tenant schema exactly, and the teardown silently DESTROYED the tenant under measurement.
# A test that drops a schema must derive that schema from a value no real tenant can have.
SEAT = "00000000-0000-4000-8000-0000000000ff"
SCHEMA = f"faultline_{SEAT.replace('-', '_')}"

DDL = f"""
DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE;
CREATE SCHEMA "{SCHEMA}";
CREATE TABLE "{SCHEMA}".entity_aliases (
    id SERIAL PRIMARY KEY, entity_id TEXT NOT NULL, alias TEXT NOT NULL,
    is_preferred BOOLEAN DEFAULT false, preference_source TEXT DEFAULT 'unspecified',
    created_at TIMESTAMPTZ DEFAULT now(), display_form TEXT, UNIQUE (entity_id, alias));
CREATE UNIQUE INDEX ON "{SCHEMA}".entity_aliases (entity_id) WHERE is_preferred = true;
CREATE TABLE "{SCHEMA}".facts (
    id SERIAL PRIMARY KEY, subject_id TEXT, object_id TEXT, rel_type TEXT,
    fact_provenance TEXT, provenance TEXT, fact_class TEXT, created_at TIMESTAMPTZ DEFAULT now(),
    superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ, source_ref TEXT,
    UNIQUE (subject_id, object_id, rel_type));
CREATE TABLE "{SCHEMA}".staged_facts (
    id SERIAL PRIMARY KEY, subject_id TEXT, object_id TEXT, rel_type TEXT,
    provenance TEXT, fact_provenance TEXT, fact_class TEXT,
    first_seen_at TIMESTAMPTZ DEFAULT now(), promoted_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ,
    source_ref TEXT, UNIQUE (subject_id, object_id, rel_type));
CREATE TABLE "{SCHEMA}".rel_types (rel_type TEXT PRIMARY KEY, wikidata_pid TEXT,
    is_single_valued BOOLEAN, is_hierarchy_rel BOOLEAN, category TEXT);
CREATE TABLE "{SCHEMA}".entities (id TEXT PRIMARY KEY, entity_type TEXT, node_role TEXT);
CREATE TABLE "{SCHEMA}".entity_attributes (
    user_id TEXT, entity_id TEXT, attribute TEXT, provenance TEXT, superseded_at TIMESTAMPTZ,
    PRIMARY KEY (entity_id, attribute));
"""


def sur(name):
    return str(uuid.uuid5(uuid.UUID(SEAT), name.lower().strip())).lower()


@pytest.fixture()
def conn():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(DDL)
        # rel_type NAMES are arbitrary here on purpose: the tool must resolve RDFS roles through
        # wikidata_pid, never through a rel name. If it ever hardcodes a name, these fixtures
        # (which use deliberately unusual names) go red.
        cur.execute(f'INSERT INTO "{SCHEMA}".rel_types (rel_type, wikidata_pid) VALUES '
                    f"('zz_is_a','P31'),('zz_kind_of','P279'),('zz_keeps',NULL)")
    c.autocommit = False
    yield c
    c.rollback()
    with c.cursor() as cur:
        c.autocommit = True
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    c.close()


def _alias(cur, entity, alias, source="unspecified", pref=False):
    cur.execute(f'INSERT INTO "{SCHEMA}".entity_aliases (entity_id, alias, preference_source,'
                f' is_preferred) VALUES (%s,%s,%s,%s) RETURNING id',
                (entity, alias, source, pref))
    cur.execute(f'INSERT INTO "{SCHEMA}".entities (id) VALUES (%s) ON CONFLICT DO NOTHING',
                (entity,))


def _fact(cur, sub, rel, obj, prov="user_stated"):
    cur.execute(f'INSERT INTO "{SCHEMA}".facts (subject_id, object_id, rel_type, fact_provenance,'
                f' fact_class) VALUES (%s,%s,%s,%s,%s) RETURNING id', (sub, obj, rel, prov, "A"))
    return cur.fetchone()[0]


def build_collapse(cur, *, with_edge=True, with_attr=True, second_name=False):
    """A type node the USER declared a class, with a pet's NAME welded onto it.

    ``place``  = the collapsed node, minted by the TYPE surface (so uuid5('sometype') == place)
    ``twin``   = the name's own entity, which already exists and already holds the name
    """
    place = sur("sometype")
    twin = sur("somename")
    _alias(cur, place, "sometype", "inferred")
    _alias(cur, place, "somename", "user_stated")          # ← the weld
    _alias(cur, twin, "somename", "user_stated", pref=True)
    # USER-STATED class-side evidence: something else is an instance OF this node.
    _fact(cur, twin, "zz_is_a", place)
    if second_name:
        other = sur("othername")
        _alias(cur, place, "othername", "user_stated")
        _alias(cur, other, "othername", "user_stated", pref=True)
    if with_edge:
        _alias(cur, "speaker", "speaker", "provisioned", pref=True)
        _fact(cur, "speaker", "zz_keeps", place)           # a MEMORY edge on the class
    if with_attr:
        cur.execute(f'INSERT INTO "{SCHEMA}".entity_attributes (entity_id, attribute, provenance)'
                    f" VALUES (%s,'colour','user_stated')", (place,))
    return place, twin


def survey_one(conn, entity):
    return [f for f in cr.survey(conn, SCHEMA) if f["entity_id"] == entity][0]


def checksums(conn):
    return cr.checksum(conn, SCHEMA)


# ── classification ───────────────────────────────────────────────────────────────────────────
def test_a_name_welded_onto_a_user_declared_class_is_repairable(conn):
    with conn.cursor() as cur:
        place, twin = build_collapse(cur, with_edge=False, with_attr=False)
    conn.commit()
    f = survey_one(conn, place)
    assert f["shape"] == cr.NAME_ON_USER_PLACE
    assert f["decision"] == cr.REPAIR
    assert [o["op"] for o in f["ops"]] == ["RETRACT_ALIAS"]
    assert f["ops"][0]["resolves_instead_to"] == twin


def test_the_memory_edge_and_the_scalar_both_follow_the_single_named_referent(conn):
    """The path the production replica never exercised: one referent, so attribution is forced."""
    with conn.cursor() as cur:
        place, twin = build_collapse(cur)
    conn.commit()
    f = survey_one(conn, place)
    assert f["decision"] == cr.REPAIR
    ops = {o["op"] for o in f["ops"]}
    assert ops == {"RETRACT_ALIAS", "REPOINT_EDGE", "REPOINT_ATTR"}
    for o in f["ops"]:
        if o["op"] in ("REPOINT_EDGE", "REPOINT_ATTR"):
            assert o["to_entity"] == twin


def test_the_class_side_edge_is_never_moved(conn):
    """``twin zz_is_a place`` CONSTITUTES the place (RDF Schema §3.3). It must stay put."""
    with conn.cursor() as cur:
        place, twin = build_collapse(cur, with_attr=False)
    conn.commit()
    f = survey_one(conn, place)
    classside = [e for e in f["edges"] if e["kind"] == "CLASS_SIDE"]
    assert classside, "the fixture's user-stated instance_of edge must be seen as class-side"
    moved = {o.get("row_id") for o in f["ops"] if o["op"] == "REPOINT_EDGE"}
    assert not (moved & {e["id"] for e in classside})


def test_two_names_plus_a_memory_edge_is_refused_as_undecidable(conn):
    """The ``{morkie dog, fraggle, sophia}`` shape — nothing records which referent owns the edge."""
    with conn.cursor() as cur:
        place, _ = build_collapse(cur, second_name=True, with_attr=False)
    conn.commit()
    f = survey_one(conn, place)
    assert f["decision"] == cr.UNDECIDABLE
    assert f["ops"] == []
    assert "candidate referents" in f["reason"]


def test_two_names_with_no_memory_edge_is_still_repairable(conn):
    """With nothing to attribute, multiple names are not an obstacle — only edges are."""
    with conn.cursor() as cur:
        place, _ = build_collapse(cur, second_name=True, with_edge=False, with_attr=False)
    conn.commit()
    f = survey_one(conn, place)
    assert f["decision"] == cr.REPAIR
    assert sorted(o["alias"] for o in f["ops"]) == ["othername", "somename"]


def test_a_weld_with_no_independent_surrogate_is_refused(conn):
    """Retracting it would delete the surface's ONLY route to its referent."""
    with conn.cursor() as cur:
        place = sur("sometype")
        _alias(cur, place, "sometype", "inferred")
        _alias(cur, place, "orphan surface", "user_stated")
        _alias(cur, sur("inst"), "inst", "user_stated", pref=True)
        _fact(cur, sur("inst"), "zz_is_a", place)
    conn.commit()
    f = survey_one(conn, place)
    assert f["decision"] == cr.UNDECIDABLE
    assert "NO independent surrogate" in f["reason"]


def test_engine_only_class_evidence_never_licenses_retracting_a_user_surface(conn):
    """Authority order: growth may add and widen, never override. Evidence must be user-stated."""
    with conn.cursor() as cur:
        place = sur("sometype")
        _alias(cur, place, "sometype", "unspecified")
        _alias(cur, place, "somename", "user_stated")
        _alias(cur, sur("somename"), "somename", "user_stated", pref=True)
        _fact(cur, sur("somename"), "zz_is_a", place, prov="llm_inferred")  # ENGINE evidence only
    conn.commit()
    f = survey_one(conn, place)
    assert f["decision"] != cr.REPAIR


def test_the_seat_anchor_is_never_repaired(conn):
    with conn.cursor() as cur:
        _alias(cur, SEAT, "user", "provisioned", pref=True)
        _alias(cur, SEAT, "chris", "user_stated")
        _alias(cur, sur("chris"), "chris", "user_stated", pref=True)
        _fact(cur, sur("chris"), "zz_is_a", SEAT)
    conn.commit()
    f = survey_one(conn, SEAT)
    assert f["shape"] == cr.SEAT
    assert f["decision"] == cr.LEAVE


# ── apply / restore ──────────────────────────────────────────────────────────────────────────
def test_apply_then_restore_is_byte_identical_including_edges_and_scalars(conn):
    with conn.cursor() as cur:
        place, twin = build_collapse(cur)
    conn.commit()
    before = checksums(conn)

    findings = [f for f in cr.survey(conn, SCHEMA) if f["decision"] == cr.REPAIR]
    assert findings
    with conn.cursor() as cur:
        cr.ensure_backup_tables(cur, SCHEMA, "tt")
    conn.commit()
    for f in findings:
        assert cr.apply_entity(conn, SCHEMA, "tt", f) == "applied"

    after = checksums(conn)
    changed = {k for k in before if before[k] != after[k]}
    assert f"{SCHEMA}.entity_aliases" in changed
    assert f"{SCHEMA}.facts" in changed, "the memory edge should have been re-pointed"
    assert f"{SCHEMA}.entity_attributes" in changed, "the scalar should have been re-pointed"

    cr.restore(conn, "tt", SCHEMA)
    assert checksums(conn) == before, "restore must return every table byte-identical"


def test_apply_is_idempotent_and_resumable(conn):
    with conn.cursor() as cur:
        build_collapse(cur)
    conn.commit()
    findings = [f for f in cr.survey(conn, SCHEMA) if f["decision"] == cr.REPAIR]
    with conn.cursor() as cur:
        cr.ensure_backup_tables(cur, SCHEMA, "tt")
    conn.commit()
    for f in findings:
        cr.apply_entity(conn, SCHEMA, "tt", f)
    after = checksums(conn)
    # Re-running the SAME finding must be a no-op via the ledger, not a second set of writes.
    for f in findings:
        assert cr.apply_entity(conn, SCHEMA, "tt", f) == "skipped-already-applied"
    assert checksums(conn) == after


def test_a_dry_run_writes_nothing(conn):
    with conn.cursor() as cur:
        build_collapse(cur)
    conn.commit()
    before = checksums(conn)
    cr.survey(conn, SCHEMA)
    assert checksums(conn) == before


def test_a_bad_run_tag_is_refused_before_it_reaches_an_identifier(conn):
    for bad in ('t"1', "t;drop", "", "A" * 40, "t 1"):
        with pytest.raises(SystemExit):
            cr._tag(bad)
