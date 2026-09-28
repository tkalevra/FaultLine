"""Open-core migration corpus pins.

1. HYGIENE — the open-core corpus carries no closed-layer schema (hosted accounts/seats/billing,
   the agent cortex) and no private example data. A migration that references a table this
   corpus never creates is not portable as-is; it fails every fresh boot.
2. FIRST BOOT — on an EMPTY database, one boot applies the whole corpus with ZERO rejected
   statements, and a second boot applies nothing (the ledger converges in one pass). Opt-in:
   set FOSS_FRESH_DB_DSN to an EMPTY throwaway database (it is written to).
"""
import os
import re

import pytest

_MIG_DIR = os.environ.get("FAULTLINE_MIGRATIONS_DIR", "migrations")

# Closed-layer schema objects that must never appear in the open-core corpus.
_CLOSED_LAYER = re.compile(
    r"public\.accounts\b|\baccount_users\b|\baccount_api_keys\b|\baccount_llm_config\b|"
    r"\bdashboard_users\b|\bflagent_|\bcortex_(notes|episodes|evidence)\b|\bstripe_|\bsaas[_/.]|"
    r"\bbrain_(health_samples|probe_state|alert_state)\b|\breembedder_enabled\b",
    re.IGNORECASE,
)

# Files renamed for first-boot order (003 -> 013, 016 -> 017) and the open-core copies
# renumbered back to their upstream ids (215/216/217 -> 185/186/215).
_RETIRED = {"003_confirmation_source.sql", "016_builtin_scalar_rel_types.sql",
            "215_generic_identifier_atomic_pattern.sql", "216_linguistic_cues_identifier_noun.sql",
            "217_postal_code_scalar_reltype.sql"}
_EXPECTED = {"013_confirmation_source.sql", "017_seed_builtin_scalar_rel_types.sql",
             "021_seed_code_referenced_rel_types.sql", "185_generic_identifier_atomic_pattern.sql",
             "186_linguistic_cues_identifier_noun.sql", "215_postal_code_scalar_reltype.sql",
             "216_icu_collation_for_display_text.sql", "273_scalar_attributes_history.sql",
             "275_seed_has_port_rel_type.sql", "281_linguistic_cues_measure_noun.sql"}


def _sql_files():
    if not os.path.isdir(_MIG_DIR):
        pytest.skip(f"migrations dir {_MIG_DIR!r} not found")
    return sorted(n for n in os.listdir(_MIG_DIR) if n.endswith(".sql"))


def test_no_closed_layer_schema_in_corpus():
    hits = []
    for name in _sql_files():
        body = open(os.path.join(_MIG_DIR, name), encoding="utf-8").read()
        for m in _CLOSED_LAYER.finditer(body):
            hits.append(f"{name}: {m.group(0)}")
    assert not hits, f"closed-layer schema referenced by the open-core corpus: {hits[:10]}"


def test_renames_landed_and_retired_names_are_gone():
    names = set(_sql_files())
    assert not (names & _RETIRED), f"retired migration ids still present: {names & _RETIRED}"
    assert _EXPECTED <= names, f"missing migrations: {_EXPECTED - names}"


def test_migration_ids_are_unique():
    ids = [n[:-4] for n in _sql_files()]
    assert len(ids) == len(set(ids))


_FRESH = os.environ.get("FOSS_FRESH_DB_DSN")


@pytest.mark.skipif(not _FRESH, reason="FOSS_FRESH_DB_DSN (an EMPTY throwaway db) not set")
def test_first_boot_applies_everything_and_second_boot_nothing(monkeypatch):
    from src.provisioning import boot_migrations as bm

    monkeypatch.setenv("FAULTLINE_MIGRATION_LEDGER", "true")
    first = bm.run_boot_migrations(_FRESH, _MIG_DIR, echo=False)
    assert first.ledger_active
    assert first.errored == [], f"first boot rejected statements: {first.diagnoses}"
    assert first.ran_count == len(_sql_files())
    second = bm.run_boot_migrations(_FRESH, _MIG_DIR, echo=False)
    assert second.ran_count == 0 and second.errored == []
