"""
RESET-RESTORE: reset_tenant() must restore seed rel_type phrasing from public each run.

The DELETE-non-seed step only DROPS junk rels; it never REPAIRS a SURVIVING seed rel whose
natural_language was clobbered IN PLACE (the "X participated in unknown" corruption). The reset
used to append a hand-written UPDATE re-syncing the two template columns from public.rel_types.

Since gauntlet lme-reset-reseed (2026-09-16) the restore is the PROVISIONER'S OWN seed: after the
TRUNCATE, ``reset_tenant`` runs ``schema_manager.seed_tenant_metadata`` inside the api container
(``reseed_tenant``), whose ``INSERT INTO rel_types SELECT * FROM public.rel_types ON CONFLICT DO
UPDATE`` restores EVERY seed column (natural_language / natural_language_2p / flags), not two. The
in-place-clobber contract is pinned BEHAVIOURALLY by tests/test_lme_reset_reseed.py (LR3 litter sets
owns.natural_language := 'CLOBBERED' and the reset seat comes back row-set identical to a fresh
tenant). This file keeps the source-shape half: the reset still purges non-seed rels first, and the
restore is the shared seed — never a second, harness-side copy of what the seed writes.
"""
import os


def _harness_source():
    path = os.path.join(os.path.dirname(__file__),
                        "../../benchmarks/harness/run_longmemeval.py")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_reset_restores_seed_templates_from_public():
    src = _harness_source()
    # The restore is the provisioner's seed, invoked from reset_tenant after the TRUNCATE: the
    # harness ships a driver that imports + calls seed_tenant_metadata, and carries no
    # harness-side copy of the template re-sync (that copy restored two columns; the seed's
    # ON CONFLICT DO UPDATE restores them all — see schema_manager._execute_bootstrap_queries).
    assert "from src.provisioning.schema_manager import" in src and "seed_tenant_metadata" in src
    body = src[src.index("def reset_tenant("):]
    assert "reseed_tenant()" in body, "reset_tenant no longer reseeds after the truncate"
    assert "SET natural_language = p.natural_language" not in src, \
        "a harness-side template re-sync is a second seed list — the shared seed owns the restore"


def test_reset_still_deletes_nonseed_rels_first():
    """Restore is ADDITIVE — the pre-existing non-seed DELETE must remain so junk growth
    is still purged before seed phrasing is re-synced."""
    src = _harness_source()
    assert "WHERE rel_type NOT IN (SELECT rel_type FROM public.rel_types)" in src
    # Ordering: the non-seed DELETE (in the reset SQL) runs before the reseed call.
    body = src[src.index("def reset_tenant("):]
    del_idx = body.index("WHERE rel_type NOT IN (SELECT rel_type FROM public.rel_types)")
    seed_idx = body.index("reseed_tenant()")
    assert del_idx < seed_idx, "non-seed DELETE must precede the reseed"
