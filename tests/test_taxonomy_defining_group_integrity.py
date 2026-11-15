"""WALKABLE — entity_taxonomies.rel_types_defining_group must contain only names that RESOLVE.

WHY THIS EXISTS
---------------
`rel_types_defining_group` is the taxonomy PROJECTION KEY: a scoped query that resolves a
taxonomy admits exactly that array into QueryPath.allowed_rels. Measured on pre-prod
2026-07-31, `public.entity_taxonomies` named 59 distinct rel_types of which 48 DO NOT EXIST
in `public.rel_types` — one tenant's shell/coffee-machine growth vocabulary written INTO the
seed template at runtime. `src/provisioning/schema_manager.py` copies that table verbatim into
every new tenant, so the foreign names propagate to every tenant provisioned since.

That is a PRECISION defect, not a recall one: a name that resolves to nothing matches no fact
today, but the moment any tenant grows a rel sharing one of those very common verb names
(`uses`, `references`, `executes`, …) that tenant silently joins several taxonomies at once.

Migration 199 repairs the state (KEEP a name that is a rel_type in THAT schema, DROP anything
else) and installs a template-only recurrence guard.

These tests run against the LIVE local Postgres — this family has produced false greens from
mock drift before, and the whole contract is SQL.
"""

import os
import pathlib

import pytest

# Applies a FAN-OUT migration (ALTER TABLE on every faultline_% schema) to the shared suite
# database: run with the other migration-appliers on one xdist worker (#44 suite hang).
pytestmark = pytest.mark.xdist_group("shared_migrations")

psycopg2 = pytest.importorskip("psycopg2")

_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql://faultline:faultline@172.20.0.5:5432/faultline",
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "199_taxonomy_defining_group_dangling_repair.sql"
)

# Disposable schema. Named `faultline_%` on purpose: the migration's repair loop selects
# exactly that pattern, so the test exercises the REAL loop, not a re-typed copy of it.
_SCRATCH = "faultline_t198_scratch"


def _connect():
    try:
        return psycopg2.connect(_DSN, connect_timeout=5)
    except Exception as exc:                                    # pragma: no cover
        pytest.skip(f"local Postgres unavailable at {_DSN}: {exc}")


@pytest.fixture()
def db():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCRATCH} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCRATCH}")
        cur.execute(f"SET search_path TO {_SCRATCH}")
        cur.execute("CREATE TABLE rel_types (rel_type TEXT PRIMARY KEY)")
        cur.execute(
            "CREATE TABLE entity_taxonomies ("
            " taxonomy_name TEXT PRIMARY KEY,"
            " rel_types_defining_group TEXT[] DEFAULT '{}')"
        )
        # `member_of` is seeded; `grown_rel` stands in for a rel this tenant legitimately
        # GREW (present in its own rel_types). `belongs_to` has a curated canonical in the
        # alias table but is NOT a rel_type — the laundering trap.
        cur.execute(
            "INSERT INTO rel_types (rel_type) VALUES ('member_of'), ('grown_rel')"
        )
        cur.execute(
            "CREATE TABLE rel_type_aliases (alias TEXT, canonical_rel_type TEXT, source TEXT)"
        )
        cur.execute(
            "INSERT INTO rel_type_aliases VALUES ('belongs_to', 'member_of', 'ontology')"
        )
        cur.execute(
            "INSERT INTO entity_taxonomies (taxonomy_name, rel_types_defining_group) "
            "VALUES ('scratch_group', "
            " ARRAY['member_of','grown_rel','belongs_to','pipes_to','sets_option'])"
        )
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCRATCH} CASCADE")
        conn.close()


def _apply_migration(conn):
    with conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text())
        cur.execute("SET search_path TO public")


def _group(conn, schema, taxonomy):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT rel_types_defining_group FROM {schema}.entity_taxonomies "
            "WHERE taxonomy_name = %s",
            (taxonomy,),
        )
        row = cur.fetchone()
    return sorted(row[0]) if row and row[0] else []


def test_repair_keeps_resolvable_drops_dangling(db):
    """KEEP what resolves in THIS schema; DROP what resolves to nothing."""
    assert _group(db, _SCRATCH, "scratch_group") == [
        "belongs_to", "grown_rel", "member_of", "pipes_to", "sets_option",
    ]

    _apply_migration(db)

    assert _group(db, _SCRATCH, "scratch_group") == ["grown_rel", "member_of"]


def test_repair_respects_authority_order_growth_may_add(db):
    """A rel the tenant legitimately GREW is a real rel_type here, so it SURVIVES.

    Authority order is user > seed > growth: the repair reverts only growth's write into a
    place it had no authority to write (a name resolving to nothing), never a legitimate
    tenant-grown defining rel.
    """
    _apply_migration(db)
    assert "grown_rel" in _group(db, _SCRATCH, "scratch_group")


def test_dangling_alias_is_dropped_not_laundered(db):
    """PINS THE REJECTED ALTERNATIVE — do not canonicalise a dangling name via rel_type_aliases.

    `belongs_to` is not a rel_type but the curated alias table maps it to `member_of`.
    Resolving it would LAUNDER a dead pollution entry into a LIVE defining rel and WIDEN the
    group beyond anything the seed declared. Measured against a pristine seed, `belongs_to`
    is not seed intent anywhere — it arrived with the same growth spill as `pipes_to`.
    A dangling name is junk: delete it, never make it work.
    """
    _apply_migration(db)
    group = _group(db, _SCRATCH, "scratch_group")
    assert "belongs_to" not in group
    # and it must NOT have reappeared under its canonical either
    assert group.count("member_of") == 1


def test_repair_is_idempotent(db):
    _apply_migration(db)
    first = _group(db, _SCRATCH, "scratch_group")
    _apply_migration(db)
    assert _group(db, _SCRATCH, "scratch_group") == first


def test_public_template_guard_refuses_dangling_name(db):
    """The template-only recurrence guard: growth can no longer write a phantom into public."""
    _apply_migration(db)
    with db.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute("SELECT taxonomy_name FROM public.entity_taxonomies LIMIT 1")
        row = cur.fetchone()
        if not row:
            pytest.skip("no public.entity_taxonomies rows in this database")
        tax = row[0]
        cur.execute(
            "SELECT rel_types_defining_group FROM public.entity_taxonomies "
            "WHERE taxonomy_name = %s",
            (tax,),
        )
        before = sorted(cur.fetchone()[0] or [])

        # A name that exists in NO rel_types row — exactly what the growth append leaked.
        cur.execute(
            "UPDATE public.entity_taxonomies "
            "SET rel_types_defining_group = array_append(rel_types_defining_group, %s) "
            "WHERE taxonomy_name = %s",
            ("__t198_phantom__", tax),
        )
        cur.execute(
            "SELECT rel_types_defining_group FROM public.entity_taxonomies "
            "WHERE taxonomy_name = %s",
            (tax,),
        )
        after = sorted(cur.fetchone()[0] or [])

    assert "__t198_phantom__" not in after
    assert after == before


def test_public_template_guard_admits_a_resolvable_name(db):
    """The guard constrains only the ILLEGAL write — a real rel_type still goes in."""
    _apply_migration(db)
    with db.cursor() as cur:
        cur.execute("SET search_path TO public")
        cur.execute("SELECT rel_type FROM public.rel_types ORDER BY rel_type LIMIT 1")
        row = cur.fetchone()
        if not row:
            pytest.skip("no public.rel_types rows in this database")
        real_rel = row[0]
        cur.execute(
            "SELECT taxonomy_name FROM public.entity_taxonomies "
            "WHERE NOT (rel_types_defining_group @> ARRAY[%s]::TEXT[]) LIMIT 1",
            (real_rel,),
        )
        row = cur.fetchone()
        if not row:
            pytest.skip("every public taxonomy already carries this rel")
        tax = row[0]
        cur.execute(
            "UPDATE public.entity_taxonomies "
            "SET rel_types_defining_group = array_append(rel_types_defining_group, %s) "
            "WHERE taxonomy_name = %s",
            (real_rel, tax),
        )
        cur.execute(
            "SELECT rel_types_defining_group FROM public.entity_taxonomies "
            "WHERE taxonomy_name = %s",
            (tax,),
        )
        after = cur.fetchone()[0] or []
        # restore
        cur.execute(
            "UPDATE public.entity_taxonomies "
            "SET rel_types_defining_group = array_remove(rel_types_defining_group, %s) "
            "WHERE taxonomy_name = %s",
            (real_rel, tax),
        )
    assert real_rel in after


def test_every_public_defining_name_resolves_after_repair(db):
    """PRECISION INVARIANT: after the repair, no taxonomy names a rel that does not exist."""
    _apply_migration(db)
    with db.cursor() as cur:
        cur.execute(
            "WITH tx AS (SELECT DISTINCT unnest(rel_types_defining_group) rt "
            "            FROM public.entity_taxonomies) "
            "SELECT count(*) FROM tx "
            "WHERE rt NOT IN (SELECT rel_type FROM public.rel_types)"
        )
        assert cur.fetchone()[0] == 0
