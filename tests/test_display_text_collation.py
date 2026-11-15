"""COLLATE — human-readable name text must sort LINGUISTICALLY, not by byte value.

THE DEFECT THIS PINS
--------------------
Production runs `postgres:16-alpine`. The database DECLARES `datcollate = en_US.utf8` with
`datlocprovider = c`, but musl ships no locale definitions, so PostgreSQL silently degrades
that collation to BYTE ordering. Measured on production:

    actual:   avion < ecole < ile < zebra < école < île      <- accents sort AFTER z
    correct:  avion < ecole < école < ile < île < zebra

It is an ENGLISH defect — `résumé`, `naïve`, `café`, `Zoë` and every accented user-entered
name sorts wrong — and it is invisible because the DB reports a locale it is not honouring.
Migration 216 fixes it with a COLUMN-level `en-US-x-icu` collation on `entity_aliases.alias`
and `.display_form`, so that every unqualified `ORDER BY alias` resolves correctly with no
query change anywhere.

WHAT THIS FILE PINS, in the order it matters
--------------------------------------------
 1. THE READ, NOT THE WRITE. Ordering is proven by SELECTing rows back out of a REAL tenant
    schema built from the REAL template — never by asserting the DDL ran. A migration that
    "applied" but left the read wrong is the failure mode.
 2. THE SAFETY PROPERTY. A DETERMINISTIC collation changes ORDERING ONLY. `'ete' = 'été'`
    must stay FALSE and `lower()` must be unchanged, because this codebase dedups on
    lowercased values, derives UUID v5 surrogates from `name.lower().strip()`, and uses
    `UNIQUE (entity_id, alias)` as an `ON CONFLICT` target. A NON-deterministic collation
    would make two distinct people collide on one row. If test_equality_is_unchanged ever
    goes red, someone swapped in a non-deterministic collation — that is a DATA-CORRUPTION
    bug, not a failing assertion to relax.
 3. THE MIGRATION ITSELF, run as the real file: it flips a pre-migration tenant, is
    idempotent, tolerates a pre-214 tenant with no `display_form`, skips a partially
    provisioned schema, and leaves non-tenant schemas alone.
 4. THE TEMPLATE. A tenant provisioned AFTER the migration must get the collation from the
    template, or it silently regresses (the documented migration-197 failure).

Run with:
  POSTGRES_DSN=postgresql://faultline:probe@172.17.0.6:5432/faultline \
  python3 tools/fltest.py --bug COLLATE --test tests/test_display_text_collation.py \
      --note "ICU collation for display text"

The DB half SKIPS without POSTGRES_DSN. A PASS line with 0 passed / N skipped is NOT
evidence — run it with a DSN.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# Applies a FAN-OUT migration (ALTER TABLE on every faultline_% schema) to the shared suite
# database: run with the other migration-appliers on one xdist worker (#44 suite hang).
pytestmark = pytest.mark.xdist_group("shared_migrations")

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "src/provisioning/templates/user_schema.sql"
MIGRATION = REPO / "migrations/216_icu_collation_for_display_text.sql"

TARGET = "en-US-x-icu"

# The production-measured sample. `zebra` is the discriminator: under byte ordering the
# accented forms fall AFTER it; under the Unicode Collation Algorithm (UTS #10) a diacritic
# is a SECONDARY difference, so `école` belongs beside `ecole`.
SAMPLE = ["avion", "ecole", "ile", "zebra", "école", "île"]
BYTE_ORDER = ["avion", "ecole", "ile", "zebra", "école", "île"]
ICU_ORDER = ["avion", "ecole", "école", "ile", "île", "zebra"]

# Accented English that users actually type. All of these sort after `zebra` today.
ENGLISH_SAMPLE = ["cafe", "café", "naive", "naïve", "resume", "résumé", "zoe", "zoë", "zulu"]
ENGLISH_ICU_ORDER = ["cafe", "café", "naive", "naïve", "resume", "résumé", "zoe", "zoë", "zulu"]


# ══════════════════════════════════════════════════════════════════════════════════════
# 0. static — the template and the migration must both declare it, or new tenants regress
# ══════════════════════════════════════════════════════════════════════════════════════

def test_template_declares_the_collation_on_both_name_columns():
    """A migration WITHOUT the template edit silently omits the collation for every tenant
    provisioned afterwards. This repo has a documented case of exactly that (migration 197),
    so the template half is pinned separately from the migration half."""
    sql = TEMPLATE.read_text()
    start = sql.index("CREATE TABLE IF NOT EXISTS entity_aliases")
    end = sql.index("CREATE TABLE IF NOT EXISTS entity_attributes")
    ddl = sql[start:end]

    assert f'alias TEXT NOT NULL COLLATE "{TARGET}"' in ddl, (
        "entity_aliases.alias lost its COLLATE clause — every tenant provisioned from this "
        "template will sort accented names after 'z'"
    )
    assert f'display_form TEXT DEFAULT NULL COLLATE "{TARGET}"' in ddl, (
        "entity_aliases.display_form lost its COLLATE clause — the RENDERED name column "
        "would be the one left on byte ordering"
    )


def test_migration_refuses_a_nondeterministic_collation():
    """The whole safety argument is that the collation is DETERMINISTIC. The migration must
    guard that rather than trust it — a non-deterministic collation would change equality and
    silently collapse distinct entities."""
    sql = MIGRATION.read_text()
    assert "collisdeterministic" in sql, "migration dropped its determinism guard"
    assert "RAISE EXCEPTION" in sql, "migration must fail loud, never silently no-op"


# ══════════════════════════════════════════════════════════════════════════════════════
# DB fixtures — real schemas, dropped unconditionally, connection closed in the same
# finally so no idle-in-transaction backend is stranded (that has wedged a bench before).
# ══════════════════════════════════════════════════════════════════════════════════════

def _dsn() -> str | None:
    return os.getenv("POSTGRES_DSN") or os.getenv("COLLATE_TEST_DSN")


def _alias_ddl_from_template() -> str:
    """The REAL entity_aliases DDL, lifted from the REAL template. Using the template (rather
    than a hand-written copy) is what makes this a test of the shipped schema — a fixture that
    reinvents the DDL can pass while the template is broken."""
    sql = TEMPLATE.read_text()
    start = sql.index("CREATE TABLE IF NOT EXISTS entities")
    end = sql.index("CREATE TABLE IF NOT EXISTS entity_attributes")
    return sql[start:end]


@pytest.fixture()
def conn():
    dsn = _dsn()
    if not dsn:
        pytest.skip("no POSTGRES_DSN — DB round-trip skipped")
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(dsn)
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def fresh_tenant(conn):
    """A tenant provisioned from the REAL template — i.e. the shape a NEW user gets."""
    schema = "faultline_collate_new_" + uuid.uuid4().hex[:12]
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            # PRODUCTION ISOLATION SHAPE: tenant schema only, NO public on the search_path.
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(_alias_ddl_from_template())
            yield cur, schema
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _collation_of(cur, schema: str, table: str, column: str) -> str | None:
    cur.execute(
        """
        SELECT co.collname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_collation co ON co.oid = a.attcollation
        WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s
          AND a.attnum > 0 AND NOT a.attisdropped
        """,
        (schema, table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _seed(cur, words):
    for i, w in enumerate(words):
        cur.execute("INSERT INTO entities (id) VALUES (%s)", (f"e{i}",))
        cur.execute(
            "INSERT INTO entity_aliases (entity_id, alias, display_form) VALUES (%s, %s, %s)",
            (f"e{i}", w, w.capitalize()),
        )


def _ordered_aliases(cur) -> list[str]:
    """THE READ. A PLAIN `ORDER BY alias` with NO COLLATE clause — exactly the shape of the
    13 real sort sites. If the column collation is not doing the work, this returns byte
    order and the test fails."""
    cur.execute("SELECT alias FROM entity_aliases ORDER BY alias")
    return [r[0] for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════════════════════
# 1. THE READ — a fresh tenant sorts linguistically, with no query change
# ══════════════════════════════════════════════════════════════════════════════════════

def test_a_fresh_tenant_sorts_accented_names_linguistically(fresh_tenant):
    """VERIFY THE READ: the bug is that `école` sorts after `zebra`. Prove it does not, by
    querying rows back — and with an UNMODIFIED `ORDER BY alias`, since the entire point of
    a column-level collation is that no query has to carry a COLLATE clause."""
    cur, _ = fresh_tenant
    _seed(cur, SAMPLE)

    got = _ordered_aliases(cur)

    assert got == ICU_ORDER, f"expected linguistic order {ICU_ORDER}, got {got}"
    assert got != BYTE_ORDER, "still on byte ordering — the collation is not being applied"
    assert got.index("école") < got.index("zebra"), (
        "an accented name still sorts after 'z' — this is the production defect"
    )


def test_accented_english_names_sort_correctly(fresh_tenant):
    """The defect is ENGLISH, not French: résumé / naïve / café / Zoë are the names real
    users type, and every one of them sorts wrong under byte ordering."""
    cur, _ = fresh_tenant
    _seed(cur, ENGLISH_SAMPLE)
    assert _ordered_aliases(cur) == ENGLISH_ICU_ORDER


def test_both_name_columns_carry_the_collation(fresh_tenant):
    """`alias` is the sort key; `display_form` is what is actually RENDERED. Leaving the two
    sibling name columns on divergent collations is the latent trap — the first
    `ORDER BY display_form` anyone writes would silently get byte order back."""
    cur, schema = fresh_tenant
    assert _collation_of(cur, schema, "entity_aliases", "alias") == TARGET
    assert _collation_of(cur, schema, "entity_aliases", "display_form") == TARGET


def test_identifier_columns_are_deliberately_NOT_collated(fresh_tenant):
    """SCOPE DISCIPLINE. `entity_id` holds UUID surrogates — never displayed, never
    linguistically sorted. Converting it would rebuild the primary/foreign key indexes for no
    benefit. If this goes red someone blanket-converted every text column."""
    cur, schema = fresh_tenant
    assert _collation_of(cur, schema, "entity_aliases", "entity_id") != TARGET


# ══════════════════════════════════════════════════════════════════════════════════════
# 2. THE SAFETY PROPERTY — deterministic: ORDERING changes, EQUALITY does not
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_collation_is_deterministic(conn):
    """If this is ever false, equality stops being bytewise and dedup / UUID surrogates /
    ON CONFLICT all silently change meaning."""
    with conn.cursor() as cur:
        cur.execute("SELECT collisdeterministic FROM pg_collation WHERE collname = %s LIMIT 1",
                    (TARGET,))
        row = cur.fetchone()
    assert row is not None, f"{TARGET} is not present in this PostgreSQL build"
    assert row[0] is True, f"{TARGET} is NON-deterministic — this would corrupt dedup"


def test_equality_is_unchanged(fresh_tenant):
    """THE LOAD-BEARING ASSERTION. A deterministic collation changes ORDERING ONLY.
    `'ete' = 'été'` must stay FALSE. If this goes red, a NON-deterministic collation was
    introduced and distinct people are about to be merged into one entity — fix the
    collation, do not relax this test."""
    cur, _ = fresh_tenant
    _seed(cur, ["ete", "été", "resume", "résumé"])

    cur.execute("SELECT (SELECT alias FROM entity_aliases WHERE alias='ete') = 'été'")
    assert cur.fetchone()[0] is False, "'ete' = 'été' became TRUE — collation is not deterministic"

    cur.execute("SELECT count(*) FROM entity_aliases WHERE alias = 'resume'")
    assert cur.fetchone()[0] == 1, "an accent-insensitive match leaked into equality"


def test_lower_is_unchanged(fresh_tenant):
    """The graph normalises on lowercase (DB triggers lowercase subject_id/object_id/rel_type,
    all compares are pre-lowercased, UUID v5 is derived from name.lower().strip()). Case
    folding must be byte-for-byte what it was."""
    cur, _ = fresh_tenant
    cur.execute("SELECT lower(%s::text)", ("RÉSUMÉ",))
    assert cur.fetchone()[0] == "résumé"
    cur.execute("SELECT lower(%s::text)", ("ÉCOLE ÎLE ZOË",))
    assert cur.fetchone()[0] == "école île zoë"


def test_unique_constraint_and_on_conflict_still_work(fresh_tenant):
    """`UNIQUE (entity_id, alias)` is an ON CONFLICT target all over the ingest path. The
    collation change rebuilds that index; it must not change its semantics."""
    psycopg2 = pytest.importorskip("psycopg2")
    cur, _ = fresh_tenant
    cur.execute("INSERT INTO entities (id) VALUES ('e1')")
    cur.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES ('e1', 'avion')")

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES ('e1', 'avion')")

    cur.execute(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES ('e1','avion') "
        "ON CONFLICT (entity_id, alias) DO UPDATE SET is_preferred = true"
    )
    cur.execute("SELECT is_preferred FROM entity_aliases WHERE entity_id='e1'")
    assert cur.fetchone()[0] is True, "ON CONFLICT target no longer resolves"


def test_accent_distinct_names_remain_distinct_rows(fresh_tenant):
    """THE DEDUP FIREWALL, stated as data rather than as theory: `avion` and `avión` are two
    different names and must stay two different rows. Under a non-deterministic collation the
    second INSERT would be swallowed as a duplicate."""
    cur, _ = fresh_tenant
    cur.execute("INSERT INTO entities (id) VALUES ('e1')")
    cur.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES ('e1','avion')")
    cur.execute("INSERT INTO entity_aliases (entity_id, alias) VALUES ('e1','avión')")
    cur.execute("SELECT count(*) FROM entity_aliases WHERE entity_id='e1'")
    assert cur.fetchone()[0] == 2, "accent-distinct names were merged — dedup is corrupted"


# ══════════════════════════════════════════════════════════════════════════════════════
# 3. THE MIGRATION — run the real file against a pre-migration tenant
# ══════════════════════════════════════════════════════════════════════════════════════

# The legacy columns are declared COLLATE "C" — byte ordering, which is exactly what production's
# `en_US.utf8`-on-musl collation silently degrades to (the defect above). Leaving them on the
# DATABASE default made the fixture depend on how the runner's postgres was initdb'd: on an
# ICU-initialised throwaway (the truenas floor rig) the "before" read was already linguistic and
# test_migration_flips_a_pre_migration_tenant went red on the environment, not the code (#44).
_PRE_MIGRATION_DDL = """
CREATE TABLE entities (id TEXT PRIMARY KEY);
CREATE TABLE entity_aliases (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT COLLATE "C" NOT NULL,
    display_form TEXT COLLATE "C" DEFAULT NULL,
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (entity_id, alias)
);
"""

# A tenant that has NOT taken migration 214 — no display_form column at all. The migration
# must convert `alias` and step over the missing column rather than erroring.
_PRE_214_DDL = """
CREATE TABLE entities (id TEXT PRIMARY KEY);
CREATE TABLE entity_aliases (
    id SERIAL PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT COLLATE "C" NOT NULL,
    is_preferred BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (entity_id, alias)
);
"""


@pytest.fixture()
def legacy_tenants(conn):
    """Three tenant schemas in the shapes the migration will really meet, plus a non-tenant
    schema that must be left alone."""
    tag = uuid.uuid4().hex[:10]
    names = {
        "full": f"faultline_collate_old_{tag}",
        "pre214": f"faultline_collate_p214_{tag}",
        "partial": f"faultline_collate_part_{tag}",
        "outsider": f"notatenant_collate_{tag}",
    }
    try:
        with conn.cursor() as cur:
            for key, ddl in (("full", _PRE_MIGRATION_DDL), ("pre214", _PRE_214_DDL)):
                cur.execute(f'CREATE SCHEMA "{names[key]}"')
                cur.execute(f'SET search_path TO "{names[key]}"')
                cur.execute(ddl)
                for i, w in enumerate(SAMPLE):
                    cur.execute("INSERT INTO entities (id) VALUES (%s)", (f"e{i}",))
                    cur.execute(
                        "INSERT INTO entity_aliases (entity_id, alias) VALUES (%s,%s)",
                        (f"e{i}", w),
                    )
            # partially provisioned: schema exists, entity_aliases does not
            cur.execute(f'CREATE SCHEMA "{names["partial"]}"')
            cur.execute(f'SET search_path TO "{names["partial"]}"')
            cur.execute("CREATE TABLE entities (id TEXT PRIMARY KEY)")
            # a schema outside the faultline_ namespace
            cur.execute(f'CREATE SCHEMA "{names["outsider"]}"')
            cur.execute(f'SET search_path TO "{names["outsider"]}"')
            cur.execute("CREATE TABLE entity_aliases (id SERIAL, alias TEXT NOT NULL)")
            cur.execute("SET search_path TO public")
        yield names
    finally:
        with conn.cursor() as cur:
            for s in names.values():
                cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')


def _run_migration(conn):
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text())


def test_migration_flips_a_pre_migration_tenant(conn, legacy_tenants):
    """VERIFY THE READ. Capture the ordering BEFORE (it must be the byte order the defect
    produces), run the real migration file, then re-read."""
    full = legacy_tenants["full"]
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{full}"')
        before = _ordered_aliases(cur)
    assert before == BYTE_ORDER, (
        f"fixture did not reproduce the defect (got {before}) — if the DATABASE collation is "
        "already ICU this test cannot prove anything; run it against a byte-ordered DB"
    )

    _run_migration(conn)

    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{full}"')
        after = _ordered_aliases(cur)
    assert after == ICU_ORDER, f"migration did not fix the read: {after}"


def test_migration_is_idempotent(conn, legacy_tenants):
    """Run it twice; the second run must convert nothing and must not change the read."""
    full = legacy_tenants["full"]
    _run_migration(conn)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{full}"')
        first = _ordered_aliases(cur)

    _run_migration(conn)
    with conn.cursor() as cur:
        cur.execute(f'SET search_path TO "{full}"')
        second = _ordered_aliases(cur)

    assert first == second == ICU_ORDER


def test_migration_tolerates_a_pre_214_tenant(conn, legacy_tenants):
    """A tenant without `display_form` must have `alias` converted and the absent column
    stepped over — not an exception that aborts the whole loop and leaves later tenants
    unconverted."""
    pre214 = legacy_tenants["pre214"]
    _run_migration(conn)
    with conn.cursor() as cur:
        assert _collation_of(cur, pre214, "entity_aliases", "alias") == TARGET
        cur.execute(f'SET search_path TO "{pre214}"')
        assert _ordered_aliases(cur) == ICU_ORDER


def test_migration_skips_partial_and_non_tenant_schemas(conn, legacy_tenants):
    """A partially provisioned schema must not abort the run, and a schema outside the
    `faultline_` namespace must be left alone — the loop is scoped, not global."""
    _run_migration(conn)
    with conn.cursor() as cur:
        # the outsider keeps whatever it had; it must NOT have been converted
        assert _collation_of(cur, legacy_tenants["outsider"], "entity_aliases", "alias") != TARGET
