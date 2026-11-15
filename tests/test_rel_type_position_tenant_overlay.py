"""`_infer_entity_type_from_rel_type` must resolve rel_type constraints from the BOUND TENANT
schema, never from the `public` seed.

THE BUG THIS PINS (fixed 2026-08-12). The function used to open a bare
``psycopg2.connect(dsn)`` with **no** ``SET search_path``, so it bound the DSN default
(``"$user", public``) and read ``public.rel_types``. Per CLAUDE.md's hard rule, ``public`` is
the SEED/TEMPLATE and is *never* read at runtime by ingest or query — the tenant schema is
seeded from it at provisioning and then grows. A read of the seed therefore cannot see a
tenant-GROWN rel_type and loses to the seed on a tenant-OVERRIDDEN constraint. The call site
(the ``also_known_as``/``pref_name`` alias lane in ``/ingest``) runs inside the request that
binds ``rel_type_overlay``'s schema ContextVar, so the tenant answer was available the whole
time and was being thrown away.

Two arms, deliberately:

  * The SEAM arm runs anywhere. It proves the function reads the per-tenant overlay and that
    it opens **no** connection of its own — ``psycopg2.connect`` is booby-trapped to raise, so
    a regression to the old fresh-connection read is a hard failure, not a silent
    fall-through to ``'unknown'``.
  * The DB arm (``WELD_GUARD_TEST_DSN``) executes the real overlay SQL against a real
    PostgreSQL holding a real ``public.rel_types`` **and** a tenant schema that DISAGREES with
    it. Only a genuine per-tenant read can tell the two apart; the mocked arm cannot catch a
    resolver that silently falls back to the seed.

Why the seam arm alone is not enough: mocking the overlay proves the function *calls* it, not
that the overlay reaches the tenant's rows. Why the DB arm alone is not enough: it cannot
prove the absence of a stray connection. Each covers the other's blind spot.
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.api import main as api_main  # noqa: E402
from src.api import rel_type_overlay  # noqa: E402


# --------------------------------------------------------------------------------------
# SEAM ARM — no database required.
# --------------------------------------------------------------------------------------

@pytest.fixture()
def no_fresh_connections(monkeypatch):
    """Make any bare `psycopg2.connect` inside main.py a LOUD failure.

    The pre-fix implementation's only DB access was `psycopg2.connect(dsn)`, and its `except`
    swallowed everything into `'unknown'`. Without this trap a regression would read the seed
    and merely return the *wrong* answer where a `'unknown'` is also the fail-safe — i.e. it
    would look like a passing test on some inputs. Trapping the connect makes the old code
    path observable rather than merely wrong.
    """
    def _boom(*_a, **_k):  # pragma: no cover - only reached on a regression
        raise AssertionError(
            "_infer_entity_type_from_rel_type opened its own connection. It must resolve "
            "rel_type metadata through the per-tenant overlay (_rel_meta), which is "
            "ContextVar-bound and DB-free on a cache hit."
        )
    monkeypatch.setattr(api_main.psycopg2, "connect", _boom)


@pytest.fixture()
def tenant_meta(monkeypatch):
    """Bind a fake per-tenant overlay whose constraints DIFFER from any plausible seed."""
    meta = {
        # A tenant that NARROWED the head constraint to a single concrete type. On the seed
        # both identity rels are multi-valued, so `Organization` can only have come from the
        # tenant's own row.
        "pref_name": {"head_types": ["Organization"], "tail_types": ["SCALAR"]},
        # Multi-valued head — constrains nothing usable.
        "also_known_as": {
            "head_types": ["Animal", "Event", "Location", "Object", "Organization", "Person"],
            "tail_types": ["SCALAR"],
        },
        # Wildcard — explicitly NOT a type.
        "likes": {"head_types": ["ANY"], "tail_types": ["ANY"]},
        # A rel that exists ONLY in this tenant (grown). The seed cannot answer for it at all.
        "grown_tenant_rel": {"head_types": ["Person"], "tail_types": ["Animal"]},
    }
    monkeypatch.setattr(api_main, "_rel_meta", lambda rt=None: meta.get(rt, {}) if rt else meta)
    return meta


def test_a_tenant_narrowed_head_constraint_is_what_answers(no_fresh_connections, tenant_meta):
    """The tenant's own row wins. Under the pre-fix code this read public.rel_types."""
    assert api_main._infer_entity_type_from_rel_type("pref_name", position="head") == "Organization"


def test_a_rel_that_exists_only_in_the_tenant_resolves(no_fresh_connections, tenant_meta):
    """The growth-engine case: a rel_type the tenant grew is invisible in the seed.

    This is the defect's whole point — a `public` read returns no row and the function
    answers `'unknown'` for a constraint the tenant genuinely declares.
    """
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel", position="head") == "Person"
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel", position="tail") == "Animal"


def test_a_multi_valued_or_wildcard_constraint_stays_unknown(no_fresh_connections, tenant_meta):
    """Fail direction is UNCHANGED: only a single CONCRETE type is an answer.

    The consumer treats `'unknown'` as the strict alias word-cap, so an over-eager answer here
    would WIDEN what may be written to `entity_aliases`. Guard both non-answers.
    """
    assert api_main._infer_entity_type_from_rel_type("also_known_as", position="head") == "unknown"
    assert api_main._infer_entity_type_from_rel_type("likes", position="head") == "unknown"
    assert api_main._infer_entity_type_from_rel_type("likes", position="tail") == "unknown"


def test_an_unknown_rel_type_stays_unknown(no_fresh_connections, tenant_meta):
    assert api_main._infer_entity_type_from_rel_type("no_such_rel", position="head") == "unknown"
    assert api_main._infer_entity_type_from_rel_type("", position="head") == "unknown"


def test_the_subject_object_vocabulary_maps_onto_head_tail(no_fresh_connections, tenant_meta):
    """`position` accepts both the ontology's head/tail and the call sites' subject/object.

    The pre-fix body tested only the literals 'head' and 'tail' while the signature DEFAULTED
    to 'object' — so a caller taking the default matched neither branch and always got
    `'unknown'`, no matter what the ontology said. That default is now live and correct.
    """
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel", position="subject") == "Person"
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel", position="object") == "Animal"
    # ...which is exactly what the default argument selects.
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel") == "Animal"


def test_a_nonsense_position_is_not_silently_treated_as_a_slot(no_fresh_connections, tenant_meta):
    assert api_main._infer_entity_type_from_rel_type("grown_tenant_rel", position="sideways") == "unknown"


# --------------------------------------------------------------------------------------
# DB ARM — real overlay SQL, real schemas, seed and tenant deliberately in conflict.
# --------------------------------------------------------------------------------------

DSN = os.environ.get("WELD_GUARD_TEST_DSN")

_REL_TYPES_DDL = """
CREATE TABLE {sch}.rel_types (
    rel_type text PRIMARY KEY,
    category text, tail_types text[], head_types text[],
    storage_target text, fact_class text,
    is_symmetric boolean, inverse_rel_type text, is_hierarchy_rel boolean,
    correction_behavior text, label text,
    natural_language text, natural_language_2p text,
    temporal_class text, source text,
    scalar_datatype text, value_min numeric, value_max numeric, unit text,
    is_single_valued boolean DEFAULT false
);
"""


def _insert_rel(cur, sch, rel_type, head_types, tail_types):
    cur.execute(
        f"INSERT INTO {sch}.rel_types (rel_type, head_types, tail_types) VALUES (%s, %s, %s)",
        (rel_type, head_types, tail_types),
    )


@pytest.fixture()
def seed_and_tenant():
    """A `public.rel_types` and a tenant schema that DISAGREE about the same rel_type.

    `public` is the real shared schema in the throwaway database, so it is created only if
    absent and always dropped again — the disagreement is the entire experiment, and a
    resolver that reads the seed must produce a visibly different answer.
    """
    if not DSN:
        pytest.skip("set WELD_GUARD_TEST_DSN to a THROWAWAY postgres for the DB-backed arm")
    sch = "faultline_isofix_" + uuid.uuid4().hex[:12]
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    made_public_table = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.rel_types')")
            if cur.fetchone()[0] is None:
                cur.execute(_REL_TYPES_DDL.format(sch="public"))
                made_public_table = True
            # THE SEED SAYS: multi-valued (no usable constraint) — production's real shape.
            cur.execute("DELETE FROM public.rel_types WHERE rel_type IN ('pref_name','grown_only')")
            _insert_rel(cur, "public", "pref_name",
                        ["Person", "Organization", "Location"], ["SCALAR"])

            cur.execute(f"CREATE SCHEMA {sch}")
            cur.execute(_REL_TYPES_DDL.format(sch=sch))
            # THE TENANT SAYS: narrowed to one concrete type, and knows a rel the seed does not.
            _insert_rel(cur, sch, "pref_name", ["Organization"], ["SCALAR"])
            _insert_rel(cur, sch, "grown_only", ["Animal"], ["SCALAR"])
        rel_type_overlay.invalidate()
        yield sch
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
            cur.execute("DELETE FROM public.rel_types WHERE rel_type IN ('pref_name','grown_only')")
            if made_public_table:
                cur.execute("DROP TABLE IF EXISTS public.rel_types")
        conn.close()
        rel_type_overlay.invalidate()


@pytest.fixture()
def bound_to(monkeypatch):
    """Bind a tenant schema on the overlay ContextVar, exactly as `/ingest` does."""
    tokens = []

    def _bind(schema):
        monkeypatch.setenv("POSTGRES_DSN", DSN)
        tokens.append(rel_type_overlay.set_current_schema(schema))

    yield _bind
    for tok in reversed(tokens):
        rel_type_overlay.reset_current_schema(tok)


def test_db_the_bound_tenant_answers_not_the_seed(seed_and_tenant, bound_to):
    """Real SQL, real schemas: the tenant's narrowed constraint beats the seed's.

    Pre-fix this read `public.rel_types` (multi-valued) and returned `'unknown'`.
    """
    bound_to(seed_and_tenant)
    assert api_main._infer_entity_type_from_rel_type("pref_name", position="head") == "Organization"


def test_db_a_rel_absent_from_the_seed_still_resolves(seed_and_tenant, bound_to):
    """The self-growth loop: `grown_only` exists in NO seed row at all."""
    bound_to(seed_and_tenant)
    assert api_main._infer_entity_type_from_rel_type("grown_only", position="head") == "Animal"


def test_db_with_no_tenant_bound_the_seed_is_the_template_of_last_resort(seed_and_tenant, bound_to):
    """CONTROL — proves the two arms are genuinely distinguishable.

    With nothing bound there is no tenant to read, so the overlay's documented unscoped
    fallback reads `public`, which declares the multi-valued constraint → `'unknown'`. If this
    returned `'Organization'` the tenant arm above would be proving nothing.
    """
    bound_to(None)
    assert api_main._infer_entity_type_from_rel_type("pref_name", position="head") == "unknown"
    assert api_main._infer_entity_type_from_rel_type("grown_only", position="head") == "unknown"
