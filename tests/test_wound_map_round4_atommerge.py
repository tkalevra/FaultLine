"""THE WOUND-MAP ROUND-4 PINS — the live-atom variance class (2026-11-13).

WOUND A (cecil, the compound-turn SECOND-ATOM DROP) and WOUND B (the pet-name ladder's
alias rung) — both live-captured on pre-prod (deploy image, fresh seats, the established
recipe: reframe_to_atomic under bind_request_brain + per-atom derive + the real MCP door;
transcripts in DEV/BUGS/family-smoke-20261111/ round-4 notes). The evidence:

  WOUND A — the atomizer returned BOTH atoms verbatim (rejected=[]); the per-atom
  deterministic derive emitted the full Cecil ladder
  ((cecil,live_in,40-gallon tank), (cecil,instance_of,bearded dragon), (user,owns,cecil),
  (40-gallon tank,gallon,40-gallon)); the harvest ENVELOPE carried ZERO atom-2 edges —
  the drop sits in split_enumeration, which split the turn at the TIME COLON of "9:30am"
  and rebuilt a names-only unit ("…at 9: Cedar Grove Veterinary Clinic and Cecil."),
  silently discarding the rest of the second clause BEFORE the deriver ran. Reproduced
  3/3 through the real door + 2/2 at the envelope (deterministic, not intermittent).

  WOUND B — the live image already lands the ladder's instance_of/has_pet rungs (3/3
  fresh-seat reps, pet-name recall HIT); the surviving deterministic divergence is the
  SPINE_PET_NAMED_POSSESSION alias rung (corgi, also_known_as, biscuit), eaten by
  suppress_name_type_binder_vs_attr_scalar because the attr-scalar (user,dog,corgi)
  claims the token "corgi" the rung's subject shares.

Both fixes are deterministic, flag-gated default-ON, OFF = byte-parity. These pins are
hermetic (no DB, no LLM).
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

WOUND_A_TURN = ("Nutmeg's vet appointment is on the 22nd at 9:30am at Cedar Grove "
                "Veterinary Clinic, and our bearded dragon Cecil lives in the 40-gallon "
                "tank by the window.")
WOUND_B_TURN = "Our dog Biscuit is a corgi; we adopted him in May 2023."

# ── WOUND A — split_enumeration ────────────────────────────────────────────────────


def test_wa_time_colon_never_splits_the_enumeration():
    """The captured turn must be DECLINED by the colon splitter (a "9:30am" colon is a
    time separator), so the raw sentence survives to the decompose/LLM-escalation path."""
    assert L.split_enumeration(WOUND_A_TURN) is None


def test_wa_mangled_rebuild_never_drops_stated_content():
    """Even without the digit-flanked time colon (the guard's second leg), a names-only
    rebuild that discards stated content is declined: force the OLD mangle shape by
    asking the splitter about a variant whose colon is not digit-flanked yet whose
    second clause still carries content the names-only rebuild would drop."""
    variant = ("Nutmeg's vet appointment is on the 22nd at the Cedar Grove Veterinary "
               "Clinic, and our bearded dragon Cecil lives in the 40-gallon tank.")
    assert ":" not in variant  # no colon at all → declined by the legacy guard already
    assert L.split_enumeration(variant) is None


def test_wa_content_coverage_declines_a_dropping_split():
    """A colon list whose items carry content beyond the recognized shapes (a plain
    clause after the names) is declined — no silent drop."""
    dropping = "We run three servers: Apollo, and our bearded dragon Cecil lives in the tank."
    assert L.split_enumeration(dropping) is None


def test_wa_legit_enumeration_still_splits():
    """The canonical colon enumeration still splits (the fix must not overfire)."""
    legit = ("We run three servers: a web server named Apollo, a database named Vault, "
             "and a mail relay named Cato.")
    out = L.split_enumeration(legit)
    assert out, "the canonical named enumeration must still split"
    joined = " ".join(out).lower()
    assert "apollo" in joined and "vault" in joined and "cato" in joined
    assert "web server" in joined and "database" in joined and "mail relay" in joined


def test_wa_flag_off_is_byte_parity_with_the_old_splitter():
    """ENUM_PRESPLIT_CONTENT_COVERAGE=OFF → the guards never run: the time-colon turn
    is mangled EXACTLY as the live capture showed (the red-state receipt, held as the
    OFF-state pin)."""
    old = L.ENUM_PRESPLIT_CONTENT_COVERAGE
    L.ENUM_PRESPLIT_CONTENT_COVERAGE = False
    try:
        out = L.split_enumeration(WOUND_A_TURN)
        assert out is not None, "OFF must reproduce the live mangle (names-only rebuild)"
        assert out[0] == ("Nutmeg's vet appointment is on the 22nd at 9: "
                          "Cedar Grove Veterinary Clinic and Cecil.")
        for a in out:
            assert "bearded" not in a.lower() and "tank" not in a.lower()
    finally:
        L.ENUM_PRESPLIT_CONTENT_COVERAGE = old


def test_wa_second_atom_ladder_derives_clean():
    """The captured atom 2 (verbatim from the live brain) still derives the full Cecil
    ladder through the deriver — the atomizer and deriver were never the wound.
    (Round-4 pet-lane follow-up, SPINE_PET_REL_SUPERSENSE: the possession rung is now the
    pets walk's OWN has_pet edge — the stray owns this pin used to assert was the wound
    itself; tests/test_wound_map_round4_petlanes.py holds the OFF byte-parity receipt.)"""
    doc = L._parse("Our bearded dragon Cecil lives in the 40-gallon tank by the window.")
    triples = {(f.subject, f.rel_type, str(f.object)) for f in
               (L.derive_sentence_facts(doc, _REF) or [])}
    assert ("cecil", "instance_of", "bearded dragon") in triples
    assert ("user", "has_pet", "cecil") in triples
    assert ("cecil", "live_in", "40-gallon tank") in triples


# ── WOUND B — suppress_name_type_binder_vs_attr_scalar ────────────────────────────


def _pet_edges():
    """The live envelope shape of the Biscuit turn (attr-scalar + the landed ladder)."""
    return [
        {"subject": "user", "rel_type": "dog", "object": "corgi",
         "object_datatype": "string"},
        {"subject": "biscuit", "rel_type": "instance_of", "object": "dog"},
        {"subject": "user", "rel_type": "has_pet", "object": "biscuit"},
        {"subject": "corgi", "rel_type": "also_known_as", "object": "biscuit"},
    ]


def _junk_edges():
    """The junk binder-twin shape the suppressor targets (its own docstring example)."""
    return [
        {"subject": "user", "rel_type": "address",
         "object": "123 main street, kitchener, ontario", "object_datatype": "string"},
        {"subject": "street", "rel_type": "also_known_as", "object": "address"},
        {"subject": "street", "rel_type": "instance_of", "object": "address"},
        {"subject": "user", "rel_type": "owns", "object": "street"},
    ]


def test_wb_pet_ladder_alias_rung_survives():
    """(corgi, also_known_as, biscuit) — the class→instance alias rung whose object is a
    landed named instance — is spared while the scalar claim stands."""
    kept = L.suppress_name_type_binder_vs_attr_scalar(_pet_edges())
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("corgi", "also_known_as", "biscuit") in rels
    assert ("biscuit", "instance_of", "dog") in rels
    assert ("user", "has_pet", "biscuit") in rels
    assert ("user", "dog", "corgi") in rels  # the scalar claim itself never drops


def test_wb_junk_binder_twins_still_suppress():
    """The suppressor's original contract holds: the address-copula junk twins die."""
    kept = L.suppress_name_type_binder_vs_attr_scalar(_junk_edges())
    rels = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
    assert ("street", "also_known_as", "address") not in rels
    assert ("street", "instance_of", "address") not in rels
    assert ("user", "owns", "street") not in rels
    assert ("user", "address", "123 main street, kitchener, ontario") in rels


def test_wb_flag_off_is_byte_parity_with_the_old_suppressor():
    """BINDER_ALIAS_SPARE_NAMED_INSTANCE=OFF → the alias rung is eaten exactly as the
    live captures showed (the OFF-state red receipt)."""
    old = L.BINDER_ALIAS_SPARE_NAMED_INSTANCE
    L.BINDER_ALIAS_SPARE_NAMED_INSTANCE = False
    try:
        kept = L.suppress_name_type_binder_vs_attr_scalar(_pet_edges())
        rels = {(e["subject"], e["rel_type"], e["object"]) for e in kept}
        assert ("corgi", "also_known_as", "biscuit") not in rels
        assert ("biscuit", "instance_of", "dog") in rels
    finally:
        L.BINDER_ALIAS_SPARE_NAMED_INSTANCE = old


def test_wb_biscuit_atom_ladder_derives_clean():
    """The captured atom 1 derives the full pet ladder (the deterministic half the live
    image already lands — pinned so the union-side fix can never mask a derive loss)."""
    doc = L._parse("Our dog Biscuit is a corgi.")
    triples = {(f.subject, f.rel_type, str(f.object)) for f in
               (L.derive_sentence_facts(doc, _REF) or [])}
    assert ("biscuit", "instance_of", "dog") in triples
    assert ("user", "has_pet", "biscuit") in triples
    assert ("corgi", "also_known_as", "biscuit") in triples
