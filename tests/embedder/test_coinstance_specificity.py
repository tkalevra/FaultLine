"""
CO-INSTANCE SPECIFICITY (BREED6) — the ±6 must ground the more-SPECIFIC of two co-classifying
types UNDER the more-general one, not leave them as SIBLINGS under a broad seeded root.

THE GAP: "Fraggle is a poodle" + "Fraggle is a dog" captures `fraggle instance_of poodle` +
`fraggle instance_of dog` correctly, but the eager leaf-anchor attaches BOTH types straight to
the seeded backbone root — `poodle subclass_of animal` AND `dog subclass_of animal` (siblings).
There is then no `poodle subclass_of dog` ancestor ladder, so the shipped most-specific recall
collapse has nothing to act on and recall shows BOTH "instance of dog" and "instance of poodle".

`_ground_coinstance_specificity` closes it on the ingest (re_embedder) side: the co-instance
signal is the ground truth that one type subsumes the other; the SAME classifier the splice uses
(`_query_llm_full_chain`) decides the DIRECTION (poodle IS-A dog), and the pass grounds
`poodle subclass_of dog` while soft-retiring the too-direct `poodle subclass_of animal` sibling
edge. Subject-agnostic, engine-driven, deterministic placement (no breed/type literal).

DB-backed: requires a reachable Postgres via FAULTLINE_TEST_DSN (or POSTGRES_DSN); skipped
otherwise. Creates + drops a disposable schema; the LLM is mocked (no network).
"""
import os
import uuid

import psycopg2
import pytest

_DSN = os.environ.get("FAULTLINE_TEST_DSN") or os.environ.get("POSTGRES_DSN") or ""


def _dsn_reachable(dsn: str) -> bool:
    if not dsn:
        return False
    try:
        c = psycopg2.connect(dsn)
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _dsn_reachable(_DSN),
    reason="no reachable Postgres (set FAULTLINE_TEST_DSN / POSTGRES_DSN)",
)

# Canonical entity-type backbone roots (what _seeded_backbone_roots returns without a domain
# taxonomy). 'animal' is a root, so `poodle subclass_of animal` is the too-direct sibling edge.
_ROOTS = {"person", "animal", "organization", "location", "object", "concept"}

_FRAGGLE = "11111111-0000-0000-0000-000000000001"
_POODLE = "22222222-0000-0000-0000-000000000002"
_DOG = "33333333-0000-0000-0000-000000000003"
_CANINE = "44444444-0000-0000-0000-000000000004"
_ANIMAL = "55555555-0000-0000-0000-000000000005"


def _provision_own_tenant_schema(schema: str, user_id: str) -> None:
    """Build a tenant schema of OUR OWN from the real template + seed (#44).

    The fixture used to clone table DDL from "whichever faultline_% schema has climb_state" —
    in a shared-DB run that is another test's tenant (possibly half-built or already being
    dropped), so the outcome depended on collection order (measured: red on es-base/es-head,
    green on it-base). Now: the provisioning template via the same psql seam
    create_user_schema uses, then schema_manager.seed_tenant_metadata — the single seeding
    source — so this schema carries exactly what a freshly minted tenant carries.
    """
    import tempfile
    from pathlib import Path

    from src.provisioning import schema_manager as sm

    conn = psycopg2.connect(_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
        template = (Path(sm.__file__).parent / "templates" / "user_schema.sql").read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tmp:
            tmp.write(template.replace("{schema_name}", schema))
        try:
            ok, msg = sm.execute_psql_file(Path(tmp.name), schema, sm.parse_postgres_dsn(_DSN))
        finally:
            os.unlink(tmp.name)
        assert ok, f"template apply failed: {msg}"
        conn.autocommit = False
        failure = sm.seed_tenant_metadata(conn, schema, user_id, schema)
        assert failure is None, failure
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def repro_schema():
    """Disposable schema seeded with the BROKEN sibling state (poodle & dog both ⊂ animal)."""
    schema = "faultline_breed6_pytest_" + uuid.uuid4().hex[:8]
    _provision_own_tenant_schema(schema, str(uuid.uuid4()))
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO \"{schema}\"")
            for eid in (_FRAGGLE, _POODLE, _DOG, _CANINE, _ANIMAL):
                cur.execute(f'INSERT INTO "{schema}".entities (id) VALUES (%s)', (eid,))
            for eid, alias in ((_FRAGGLE, "fraggle"), (_POODLE, "poodle"), (_DOG, "dog"),
                               (_CANINE, "canine"), (_ANIMAL, "animal")):
                cur.execute(
                    f'INSERT INTO "{schema}".entity_aliases (entity_id, alias, is_preferred)'
                    " VALUES (%s, %s, true)", (eid, alias))
            # fraggle instance_of poodle AND dog (the named instance, co-classified — Class A).
            for obj in (_POODLE, _DOG):
                cur.execute(
                    f'INSERT INTO "{schema}".facts (subject_id, object_id, rel_type, fact_class,'
                    " is_hierarchy_rel) VALUES (%s, %s, 'instance_of', 'A', true)", (_FRAGGLE, obj))
            # BROKEN sibling state: poodle ⊂ animal (too-direct) + dog ⊂ canine ⊂ animal.
            for subj, obj, cls in ((_POODLE, _ANIMAL, "C"), (_DOG, _CANINE, "B"),
                                   (_CANINE, _ANIMAL, "B")):
                cur.execute(
                    f'INSERT INTO "{schema}".staged_facts (subject_id, object_id, rel_type,'
                    " fact_class, is_hierarchy_rel) VALUES (%s, %s, 'subclass_of', %s, true)",
                    (subj, obj, cls))
    finally:
        conn.close()
    yield schema
    c2 = psycopg2.connect(_DSN)
    c2.autocommit = True
    with c2.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    c2.close()


def _live_parent_names(conn, schema, subject_id):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO \"{schema}\"")
        cur.execute(
            "SELECT oa.alias FROM staged_facts s"
            "  JOIN entity_aliases oa ON oa.entity_id = s.object_id"
            " WHERE s.subject_id = %s AND s.rel_type = 'subclass_of' AND s.deleted_at IS NULL"
            " UNION"
            " SELECT oa.alias FROM facts f"
            "  JOIN entity_aliases oa ON oa.entity_id = f.object_id"
            " WHERE f.subject_id = %s AND f.rel_type = 'subclass_of' AND f.superseded_at IS NULL",
            (subject_id, subject_id))
        return {r[0] for r in cur.fetchall()}


def test_coinstance_grounds_specific_under_general(repro_schema, monkeypatch, request):
    from src.re_embedder import embedder

    # Classifier (engine-driven direction): poodle IS-A dog; dog IS-A canine … (mock — no network).
    chains = {
        "poodle": ["dog", "canine", "canidae", "mammal", "animal"],
        "dog": ["canine", "canidae", "carnivora", "mammal", "animal"],
        "canine": ["canidae", "mammal", "animal"],
    }
    monkeypatch.setattr(
        embedder, "_query_llm_full_chain",
        lambda name, url=None, context=None: list(chains.get((name or "").strip().lower(), [])))
    monkeypatch.setenv("POSTGRES_DSN", _DSN)

    conn = psycopg2.connect(_DSN)
    request.addfinalizer(lambda: (conn.rollback(), conn.close()) if not conn.closed else None)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO \"{repro_schema}\"")
    conn.commit()

    out = embedder._ground_coinstance_specificity(conn, "http://mock", "breed6", _ROOTS)
    assert out["grounded"] == 1, out

    # poodle now climbs UNDER dog; the too-direct poodle⊂animal sibling edge is retired.
    poodle_parents = _live_parent_names(conn, repro_schema, _POODLE)
    assert "dog" in poodle_parents, poodle_parents
    assert "animal" not in poodle_parents, poodle_parents

    # THE HARD LINE: the NAMED INSTANCE fraggle never receives a subclass_of.
    assert _live_parent_names(conn, repro_schema, _FRAGGLE) == set()

    # Idempotent: a second pass places nothing new (pair already ordered → no duplicate).
    out2 = embedder._ground_coinstance_specificity(conn, "http://mock", "breed6", _ROOTS)
    assert out2["grounded"] == 0, out2
    assert "dog" in _live_parent_names(conn, repro_schema, _POODLE)
    conn.close()


def test_coinstance_undetermined_direction_is_a_noop(repro_schema, monkeypatch, request):
    """When the classifier cannot relate the two co-instance types, NOTHING is placed (no guess)."""
    from src.re_embedder import embedder

    # Neither chain mentions the other → direction undetermined → skip, never invert.
    monkeypatch.setattr(
        embedder, "_query_llm_full_chain",
        lambda name, url=None, context=None: {"poodle": ["animal"], "dog": ["animal"]}.get(
            (name or "").strip().lower(), []))
    monkeypatch.setenv("POSTGRES_DSN", _DSN)

    conn = psycopg2.connect(_DSN)
    request.addfinalizer(lambda: (conn.rollback(), conn.close()) if not conn.closed else None)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO \"{repro_schema}\"")
    conn.commit()

    out = embedder._ground_coinstance_specificity(conn, "http://mock", "breed6", _ROOTS)
    assert out["grounded"] == 0 and out["undetermined"] >= 1, out
    # poodle stays exactly where it was — no fabricated ordering.
    assert _live_parent_names(conn, repro_schema, _POODLE) == {"animal"}
    conn.close()
