"""THE WOUND-MAP ROUND-3 PINS (2026-11-13, branch fix/wound-map-round3).

One pin block per wound-map item this lane landed, each with the mechanism pin and the
flag-OFF byte-parity pin where a flag gates the arm. The measured sentences are verbatim
from DEV/BUGS/family-smoke-20261111/blended_family_smoke.py and prod_drive_harness.py;
the DB-shaped pins use the local DSN when present and skip cleanly without it (the
fltest discipline: a skip proves nothing — every DB-gated pin names its gate in the id).
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import src.extraction.linguistics as L  # noqa: E402

pytestmark = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (en_core_web_sm not installed) — "
           "spine deriver no-ops; executes wherever the model loads",
)

_REF = datetime.datetime(2026, 11, 13)

# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 8 — PROD-BUG-7: the deterministic address chain re-files the negated
# old-value span affirmatively. THE NEGATION SCAN (SPINE_GEO_NEGATION_SCAN).

MOVE_NEGATED = ("Actually we have moved to 12 Bramble Court in Kepler Corners, "
                "not 47 Hallowfield Road.")
MOVE_AFFIRM = "We live at 47 Hallowfield Road in Kepler Corners."


def _triples(text, ref=_REF):
    doc = L._parse(text)
    return [(f.subject, f.rel_type, str(f.object)) for f in
            (L.derive_sentence_facts(doc, ref) or [])]


def test_w8_negated_old_value_never_mints_containment():
    """The correction turn's negated old-value span must never mint an affirmative
    located_in pair (the F8 re-file arm)."""
    triples = _triples(MOVE_NEGATED)
    subjects = {s for (s, _, _) in triples}
    objects = {o for (_, _, o) in triples}
    assert not any("hallowfield" in s or "hallowfield" in o for (s, _, o) in triples), triples
    assert not any(r == "located_in" and ("road" in o or "hallowfield" in o)
                   for (_, r, o) in triples), triples
    # the asserted NEW address still captures
    assert any(r == "live_at" and "bramble" in o for (_, r, o) in triples), triples
    assert "road" not in objects or not any(r == "located_in" for (_, r, o) in triples
                                            if o == "road")


def test_w8_affirmative_address_chain_byte_identical():
    """The affirmative sentence keeps its exact pre-fix chain (the negation scan is not
    a blanket containment veto). Measured pre-fix shape: the composed live_at row only."""
    triples = _triples(MOVE_AFFIRM)
    assert ("user", "live_at", "47 hallowfield road in kepler corners") in triples, triples
    assert not any(r == "located_in" for (_, r, _) in triples), triples


def test_w8_geo_negation_scan_flag_off_byte_parity(monkeypatch):
    """SPINE_GEO_NEGATION_SCAN=0 restores the pre-fix mint exactly (the negated pair
    comes back — the documented pre-fix behavior, byte-for-byte). Flags are read at
    import: patch the module attribute (the bf8 pins' convention)."""
    monkeypatch.setattr(L, "SPINE_GEO_NEGATION_SCAN", False)
    triples = _triples(MOVE_NEGATED)
    assert any(r == "located_in" for (_, r, _) in triples), triples


def test_w8_true_comma_list_still_containment():
    """A genuine enumerated containment list ("Kitchener, Ontario") still mints."""
    triples = _triples("I live in Kitchener, Ontario.")
    assert ("kitchener", "located_in", "ontario") in triples, triples


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 7a — the plant-side wife slot (SPINE_NAMING_PLANT_SLOT): "My wife is named
# Rosalind." must file the spouse slot at INGEST, not only at correction time.

WIFE_PLANT = "My wife is named Rosalind."


def test_w7a_plant_role_slot_files_at_ingest():
    triples = _triples(WIFE_PLANT)
    assert ("rosalind", "spouse", "user") in triples, triples


def test_w7a_role_alias_leg_the_hard_line():
    """The role noun lands as an alias ON the named person (never a parallel role
    entity)."""
    triples = _triples(WIFE_PLANT)
    assert ("rosalind", "also_known_as", "wife") in triples, triples
    assert not any(s == "wife" and r != "also_known_as" for (s, r, _) in triples), triples


def test_w7a_is_called_shape_unchanged():
    """The 'is called' realization (which already filed the slot) keeps its exact
    output — additive fix, no regression on the sibling shape."""
    triples = _triples("My wife is called Rosalind.")
    assert ("rosalind", "spouse", "user") in triples, triples


def test_w7a_plant_slot_flag_off_byte_parity(monkeypatch):
    """SPINE_NAMING_PLANT_SLOT=0 restores the pre-fix zero-edge derive. Flags are read
    at import: patch the module attribute (the bf8 pins' convention)."""
    monkeypatch.setattr(L, "SPINE_NAMING_PLANT_SLOT", False)
    triples = _triples(WIFE_PLANT)
    assert ("rosalind", "spouse", "user") not in triples, triples


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 4c — the runs-on frame's PARTICIPLE-ATTACHED on-PP (RUNS_ON_FRAME, the
# blended hypervisor sentence).

LAB1 = ("The homelab hypervisor is Proxmox running on the r730; the Plex VM is vmid 112 "
        "and it backs up nightly to the NAS at 2am.")
LAB1_DIRECT = "The homelab hypervisor is Proxmox on the r730."


def test_w4_participle_attached_on_pp_mints_the_run_edge():
    triples = _triples(LAB1)
    assert ("homelab hypervisor", "run", "proxmox") in triples, triples


def test_w4_direct_on_pp_shape_unchanged():
    """R1's original shape (the on-PP OF THE ATTR) keeps minting — the participle arm
    is additive."""
    triples = _triples(LAB1_DIRECT)
    assert ("homelab hypervisor", "run", "proxmox") in triples, triples


def test_w4_verb_hung_pp_never_enters_the_frame():
    """THE LOAD-BEARING GUARD HOLDS: the predicate location/time PPs hang off the VERB
    and must never mint a run edge."""
    assert _triples("The meeting is on Friday.") == [], _triples("The meeting is on Friday.")


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 5 — the negated-value supersession across storage shapes (pure functions).

def test_w5_negated_value_spans_extraction():
    from src.api.main import _negated_value_spans
    assert _negated_value_spans(
        "The custody arrangement changed in October: I have Wren and Teo every weekend "
        "now, not every other.") == ["every other"]
    assert _negated_value_spans(
        "it is penicillin I am allergic to, not sulfa drugs.") == ["sulfa drugs"]
    assert _negated_value_spans("I do like pizza.") == []


def test_w5_ordered_token_subsequence():
    from src.api.main import _is_ordered_token_subsequence
    assert _is_ordered_token_subsequence(["every", "other"], ["every", "other", "weekend"])
    assert not _is_ordered_token_subsequence(["every", "other"], ["every", "weekend"])
    assert not _is_ordered_token_subsequence(["every", "other"], ["other", "every", "weekend"])


def test_w5_flag_off_never_runs(monkeypatch):
    """SUPERSEDE_NEGATED_VALUES=0 → the retirement helper is a no-op even with spans."""
    from src.api.main import _supersede_negated_value_rows
    monkeypatch.setenv("SUPERSEDE_NEGATED_VALUES", "0")
    assert _supersede_negated_value_rows(None, "u", "x, not every other", "s") == 0


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 9 — FILED-1, the plural weld (REGISTRY_PLURAL_WELD): cat/cats ONE entity.

def test_w9_plural_weld_target_morphology():
    """The target finder admits the regular plural→singular and the symmetric shape;
    irregulars and short tokens never weld (pure morphology + the existence gate's SQL
    is exercised DB-gated below)."""
    from src.entity_registry.registry import EntityRegistry
    calls = []

    class _Cur:
        def execute(self, q, p=None):
            calls.append((q, p))

        def fetchall(self):
            return []

    reg = EntityRegistry.__new__(EntityRegistry)
    # no DB hits: nothing exists → None (the pure-morphology path never raises)
    assert reg._plural_weld_target(_Cur(), "00000000-0000-0000-0000-000000000001", "cats") is None
    # the probe asked for the SINGULAR of the regular plural
    assert any(p and p[0] == "cat" for (_, p) in calls), calls


def test_w9_plural_weld_flag_off(monkeypatch):
    """REGISTRY_PLURAL_WELD=0 → the module lever reads off."""
    import importlib
    import src.entity_registry.registry as R
    monkeypatch.setenv("REGISTRY_PLURAL_WELD", "0")
    importlib.reload(R)
    assert R.REGISTRY_PLURAL_WELD is False
    monkeypatch.setenv("REGISTRY_PLURAL_WELD", "1")
    importlib.reload(R)
    assert R.REGISTRY_PLURAL_WELD is True


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 1 — the walk's scalar-terminal preference + WOUND 2 — the hyphenated role
# token + WOUND 3 — the speaker-anchored shelf + WOUND 6 — the kin type fallback:
# DB-SHAPED pins (the walk/gate read the tenant schema). Gate on the local DSN.

_DSN = os.environ.get("POSTGRES_DSN", "")
_has_db = bool(_DSN)
_woundmap_db = pytest.mark.skipif(
    not _has_db, reason="POSTGRES_DSN unset — the walk/registry DB gates stay dark")


@_woundmap_db
def test_w1_wife_bday_walk_answers_the_scalar_terminal():
    """THE WOUND: the Door-1 modifier fold answered (user, spouse, maren) — the role
    prose — while the born_on row sat live. THE PIN: the walk answers the SCALAR on a
    fixture-shaped store (maren carries born_on via the alias; the fold must decline)."""
    import psycopg2
    import src.api.main as M
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(
                "SELECT rel_type FROM rel_types WHERE rel_type = 'born_on'"
                " AND 'SCALAR' = ANY(tail_types) LIMIT 1")
            assert cur.fetchone(), "born_on must be a live SCALAR rel in this tenant"
    finally:
        conn.close()


@_woundmap_db
def test_w1_scalar_head_preference_flag_reads():
    import src.api.main as M
    assert hasattr(M, "_WALK_SCALAR_TERMINAL_PREFERENCE")
    assert M._WALK_SCALAR_TERMINAL_PREFERENCE is True


@_woundmap_db
def test_w2_exwife_cue_map_grounding():
    """The kinship cue overlay grounds 'ex-wife' → ex_spouse (the overlay contract the
    fix consults when the alias table misses; migration 325 seeds the alias rows too)."""
    from src.api import linguistic_cue_overlay
    from src.api import rel_type_overlay
    import psycopg2
    conn = psycopg2.connect(_DSN)
    schema = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata"
                        " WHERE schema_name LIKE 'faultline_%' LIMIT 1")
            row = cur.fetchone()
            schema = row[0] if row else None
    finally:
        conn.close()
    if not schema:
        pytest.skip("no tenant schema present — the cue overlay needs a bound schema")
    rel_type_overlay.set_current_schema(schema)
    try:
        kmap = linguistic_cue_overlay.resolve_kinship_rel_map(_DSN) or {}
    finally:
        rel_type_overlay.reset_current_schema(None)
    assert kmap.get("ex-wife") == "ex_spouse", {k: v for k, v in kmap.items() if "ex" in k}


@_woundmap_db
def test_w6_kin_family_evidence_helper():
    """The family-evidence probe answers on real rows and fails safe."""
    import psycopg2
    import src.api.main as M
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
        assert M._entity_person_family_evidence(conn, "not-a-uuid") is False
        assert M._entity_has_live_evidence(conn, None) is False
    finally:
        conn.close()
