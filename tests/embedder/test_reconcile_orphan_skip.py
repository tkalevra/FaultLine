"""ORPHAN-SKIP for the re-embedder reconcile loop.

Bug (live 2026-06-23): a swept tenant's PG schema is DROP SCHEMA CASCADE'd but its per-user
Qdrant collection (`faultline-<uuid>`) can be left behind. 141 such orphaned collections
accumulated → reconcile_qdrant scrolled every one EVERY cycle → backend churn + health blips.

Fix: reconcile_qdrant fetches the set of existing tenant schemas once per cycle and SKIPS any
collection whose paired PG schema no longer exists (an orphan). FAIL-SAFE: if the
schema-existence check is unavailable (None), it processes the collection (never silently
drops work on a check failure). The orphan is NOT deleted here — that's the sweep's job.

These tests pin the PURE decision helpers (collection→schema derivation + skip/process), which
hold the whole safety contract.
"""
from src.re_embedder.embedder import (
    collection_to_schema_name,
    should_reconcile_collection,
)

ORACLE = "11111111-2222-4333-8444-555555555555"
ORACLE_COLL = f"faultline-{ORACLE}"
ORACLE_SCHEMA = "faultline_" + ORACLE.replace("-", "_")

T1 = "11111111-2222-4333-8444-000000000001"
T1_COLL = f"faultline-{T1}"
T1_SCHEMA = "faultline_" + T1.replace("-", "_")


# ── collection → schema derivation (dash → underscore) ──────────────────────────────

def test_collection_to_schema_dash_to_underscore():
    assert collection_to_schema_name(T1_COLL) == T1_SCHEMA
    assert collection_to_schema_name(ORACLE_COLL) == ORACLE_SCHEMA


def test_collection_to_schema_shared_collections_return_none():
    # shared/legacy collections have no per-tenant schema → None → never skipped downstream
    for shared in ("faultline-test", "faultline-main", "faultline-anonymous",
                   "faultline-legacy", "faultline-"):
        assert collection_to_schema_name(shared) is None


def test_collection_to_schema_non_faultline_returns_none():
    assert collection_to_schema_name("some-other-collection") is None
    assert collection_to_schema_name("") is None
    assert collection_to_schema_name(None) is None


# ── skip/process decision (the safety contract) ─────────────────────────────────────

def test_live_tenant_is_processed():
    # schema present → process (reconcile as today)
    schemas = {ORACLE_SCHEMA, T1_SCHEMA}
    assert should_reconcile_collection(T1_COLL, schemas) is True


def test_oracle_is_always_processed_when_present():
    schemas = {ORACLE_SCHEMA}
    assert should_reconcile_collection(ORACLE_COLL, schemas) is True


def test_orphan_is_skipped():
    # collection exists but its schema is GONE → orphan → skip
    schemas = {ORACLE_SCHEMA}  # T1 schema swept away
    assert should_reconcile_collection(T1_COLL, schemas) is False


def test_check_failure_falls_through_to_process():
    # existing_schemas is None (schema-existence check errored) → MUST process, never skip
    assert should_reconcile_collection(T1_COLL, None) is True
    assert should_reconcile_collection(ORACLE_COLL, None) is True


def test_shared_collection_always_processed():
    # shared/test/legacy collections are processed regardless of the schema set
    for shared in ("faultline-test", "faultline-main", "faultline-legacy"):
        assert should_reconcile_collection(shared, set()) is True
        assert should_reconcile_collection(shared, {ORACLE_SCHEMA}) is True
        assert should_reconcile_collection(shared, None) is True


def test_empty_schema_set_skips_orphan_but_not_shared():
    # empty set (all tenants swept) → per-tenant orphan skipped, shared still processed
    assert should_reconcile_collection(T1_COLL, set()) is False
    assert should_reconcile_collection("faultline-test", set()) is True
