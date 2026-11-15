# -*- coding: utf-8 -*-
"""THE PROD-DRIVE PINS (DEV/BUGS/family-smoke-20261111/PROD-DRIVE-RESULTS.md, 2026-11-12).

The honest faultline.ca wire drive measured 2/8 formal accuracy on fresh natural
phrasings. Four of the misses are the four PROD bugs this suite pins:

  • PROD-BUG-2 (HIGH, extraction): "I'm severely allergic to shellfish — please always
    take that into account with food." produced ZERO rows — the landed HEALTH_SHELF
    detector's own code floor (``allergic``) sat IN the sentence. Root cause measured:
    en_core_web_sm mis-tags a verb-ambiguous object NP as an infinitive clause
    ("shellfish" VERB/xcomp, "to" PART/aux — NO prep child survives on the adjective),
    so ``_adj_prep_objects`` sees nothing and the frame declines. THE FIX: the
    to-infinitive mis-tag recovery (``_recover_adj_to_xcomp_object``), CLASS-SCOPED to a
    declared medical_relation_adj member (a member's ``to`` complement is the allergen
    reading, never a true infinitive; "afraid to jump" names no member and stays []).
  • PROD-BUG-3 (MED, extraction): "My brother Aldous lives in Vancouver." parses with NO
    VERB AT ALL ("lives" NOUN/NNS appos of the kin head, "Aldous" a TitleCase ADJ amod on
    it) and garbled to (brother, has_role, "aldous lives") with Vancouver never stored.
    THE FIX: ``_repair_verbless_sform_appositive`` — a structural repair that re-reads the
    verbless S-form appositive into the healthy parse shape the Callum sentence already
    has, so the binder files (aldous, sibling_of, user) + (aldous, live_in, vancouver).
  • PROD-BUG-1 (HIGH, serve): "what does my homelab hypervisor run?" unreachable on ANY
    phrasing while (xcp-ng, run_on, old dell tower …) sat LIVE — the question's verb
    "run" bound nothing. THE FIX: the seeded ASPECT-REL-AFFINITY shelf
    (migration 319's aspect_rel_affinity class; overlay floor here) + the
    ENTITY-ANCHORED admission in fetch_facts_from_anchor (the wire re-drive is the
    integration proof; these pins hold the shelf resolution + word-boundary contract).
  • PROD-BUG-4 (MED, correction): the address-class correction never retired the old
    address's own fragment rows. THE FIX: the residence-class old-value row retirement +
    the seeded address surfaces (address/home_address/street_address → lives_at, migration
    319) so the corrected value actually SERVES on the address asks.

Deterministic, flag-gated (OFF = byte-parity), the negation scan-back contract holds.
"""
import os
import pathlib

import pytest

from src.extraction import linguistics as ling
from src.api import linguistic_cue_overlay as lco

MEASURED_ALLERGY = ("I'm severely allergic to shellfish — please always take that into "
                    "account with food.")
MEASURED_BROTHER = "My brother Aldous lives in Vancouver."
LITMUS_BROTHER = "My brother Callum lives in Portland and visits every Thanksgiving."


# ────────────────────────────── PROD-BUG-2 ──────────────────────────────────

def test_measured_shellfish_sentence_files_the_allergy_row(monkeypatch):
    # THE measured sentence: the detector's own floor was in it and still returned [].
    # Root cause = the to-infinitive mis-tag; the recovery reads the xcomp object.
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    hits = ling.analyze_copula_relational_predicate(MEASURED_ALLERGY)
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "has_allergy", "shellfish")]
    assert hits[0]["negated"] is False


def test_allergy_recovery_holds_both_mistag_anchors(monkeypatch):
    # MEASURED on en_core_web_sm: the xcomp hangs off the ADJ on the bare sentence
    # ("shellfish" xcomp <- allergic) and off the COPULA on the compound one
    # ("shellfish" xcomp <- "'m"). Both shapes file the row.
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    for sentence in ("I'm severely allergic to shellfish.",
                     "I am allergic to shellfish."):
        hits = ling.analyze_copula_relational_predicate(sentence)
        assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
            ("user", "has_allergy", "shellfish")], sentence


def test_allergy_negation_scan_back_contract_holds(monkeypatch):
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    hits = ling.analyze_copula_relational_predicate("I'm not allergic to shellfish.")
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "has_allergy", "shellfish")]
    assert hits[0]["negated"] is True


def test_non_member_to_infinitives_stay_silent(monkeypatch):
    # CLASS-SCOPED: "afraid to jump" / "happy to help" are TRUE infinitive clauses — no
    # declared member, no recovery, byte-for-byte today's [] (never a has_allergy mint).
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    for sentence in ("I'm afraid to jump.", "I am happy to help with dinner.",
                     "I'm free to call tomorrow."):
        hits = ling.analyze_copula_relational_predicate(sentence)
        assert hits == [] or all(e["rel_type"] != "has_allergy" for e in hits), sentence


def test_allergy_recovery_flag_off_is_byte_parity(monkeypatch):
    # OFF → the recovery arm never runs → the mis-parse shape stays [] exactly as today.
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    monkeypatch.setattr(ling, "HEALTH_SHELF_DETECTOR", False)
    assert ling.analyze_copula_relational_predicate(MEASURED_ALLERGY) == []


def test_prep_frame_penicillin_lane_unchanged(monkeypatch):
    # the healthy parse ("penicillin" NOUN/pobj) rides the ORIGINAL frame — the recovery
    # adds nothing and removes nothing there.
    monkeypatch.setattr(ling, "_medical_relation_adjs", lambda: {"allergic"})
    hits = ling.analyze_copula_relational_predicate(
        "I'm allergic to penicillin — always flag it before any prescription.")
    assert [(e["subject"], e["rel_type"], e["object"]) for e in hits] == [
        ("user", "has_allergy", "penicillin")]


# ────────────────────────────── PROD-BUG-3 ──────────────────────────────────

def _facts_triples(facts):
    return [(str(f.subject), str(f.rel_type), str(f.object)) for f in facts]


def test_measured_brother_sentence_binds_name_and_residence():
    facts = ling.derive_sentence_facts(MEASURED_BROTHER, reference=None)
    triples = _facts_triples(facts)
    assert ("aldous", "sibling_of", "user") in triples
    assert ("aldous", "live_in", "vancouver") in triples
    # THE GARBLED READING IS GONE: no junk role value naming the verb phrase.
    assert not any(rel == "has_role" and "lives" in str(obj) for _s, rel, obj in triples)


def test_litmus_brother_compound_binds_name_city_and_visits():
    # the re-phrased litmus (Callum/Portland/Thanksgiving) — the healthy parse this repair
    # restores the Aldous shape TO — must stay exactly as it was (no regression upward).
    facts = ling.derive_sentence_facts(LITMUS_BROTHER, reference=None)
    triples = _facts_triples(facts)
    assert ("callum", "sibling_of", "user") in triples
    assert ("callum", "live_in", "portland") in triples


def test_brother_repair_flag_off_is_byte_parity():
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ling, "SPINE_VERBLESS_SFORM_REPAIR", False)
        facts = ling.derive_sentence_facts(MEASURED_BROTHER, reference=None)
        triples = _facts_triples(facts)
        # OFF → today's parse: the garbled role value naming the nominalized verb.
        assert ("brother", "has_role", "aldous lives") in triples
        assert not any(rel == "live_in" for _s, rel, _o in triples)
    finally:
        monkeypatch.undo()


def test_brother_repair_declines_verbless_nominal_fragments():
    # the guard rails: a supplemented-appositive fragment (comma set-off) and a plural-noun
    # NP with a PP are NOT predications — the repair must leave their parse untouched.
    assert ling._repair_verbless_sform_appositive(ling._parse(
        "My brother's hobbies in Vancouver, a big concern of mine.")) == 0
    assert ling._repair_verbless_sform_appositive(ling._parse(
        "The stores on Main Street.")) == 0
    assert ling._repair_verbless_sform_appositive(ling._parse(
        "My brother Callum lives in Portland.")) == 0  # healthy parse: verbless gate


# ────────────────────────────── PROD-BUG-1 ──────────────────────────────────

def test_aspect_affinity_floor_resolves_without_db(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    # main.py's helper reads the env fresh — with no DSN the keyed resolver returns the
    # code floor (DB-down fail-safe), so the shelf still names the measured binding.
    import src.api.main as m
    monkeypatch.setattr(m, "_ASPECT_AFFINITY_WORD_RE_CACHE", {})
    assert m._query_aspect_rel_affinity_rels("what does my homelab hypervisor run?") == ("run_on",)
    assert m._query_aspect_rel_affinity_rels("which software runs on the rack?") == ("run_on",)
    assert m._query_aspect_rel_affinity_rels("what is running on the hypervisor?") == ("run_on",)


def test_aspect_affinity_word_boundary_refuses_embedded_surfaces(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    import src.api.main as m
    monkeypatch.setattr(m, "_ASPECT_AFFINITY_WORD_RE_CACHE", {})
    # "runway"/"ran"/"rerun" never admit the rel — whole-word only.
    assert m._query_aspect_rel_affinity_rels("how long is the runway at the airport?") == ()
    assert m._query_aspect_rel_affinity_rels("did I lock the rerun job?") == ()
    assert m._query_aspect_rel_affinity_rels("tell me about my cat") == ()


def test_aspect_affinity_class_is_registered_in_the_bootstrap_registry():
    # an unregistered category resolves EMPTY + logs critical — the shelf MUST be in the
    # registry (the 316 contract), with the same members as the migration seed.
    assert lco._BOOTSTRAP_BY_CATEGORY.get(lco.ASPECT_REL_AFFINITY_CATEGORY) == frozenset(
        lco._BOOTSTRAP_ASPECT_REL_AFFINITIES)
    # BY-DESIGN GROWTH (the s1 hobby two-seam fix, 2026-11-13; migration 321): the hobby
    # activity members joined the class (ride/play/restore/do → their own rels) — the
    # vehicle grows, the MECHANISM (floor == registry registration; the measured 319
    # members intact) is byte-identical.
    assert lco._BOOTSTRAP_ASPECT_REL_AFFINITIES == {
        "run": "run_on", "runs": "run_on", "running": "run_on",
        "ride": "ride", "rides": "ride", "riding": "ride",
        "play": "play", "plays": "play", "playing": "play",
        "restore": "restore", "restores": "restore", "restoring": "restore",
        "do": "do",
    }
    # the auxiliary inflections of "do" never seed (the "what does my X run?" wound)
    assert "does" not in lco._BOOTSTRAP_ASPECT_REL_AFFINITIES
    assert "doing" not in lco._BOOTSTRAP_ASPECT_REL_AFFINITIES


# ────────────────────────────── PROD-BUG-4 ──────────────────────────────────

def test_migration_319_seeds_affinity_class_and_address_aliases():
    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations" /
           "319_seed_aspect_rel_affinity_and_address_alias.sql").read_text()
    # the affinity class: public seed + per-tenant fan-out (the 308/316 pattern)
    assert "aspect_rel_affinity" in sql
    assert "'run_on'" in sql and "run" in sql
    assert sql.count("ON CONFLICT (cue, category) DO NOTHING") >= 2
    assert "LIKE 'faultline\\_%'" in sql
    # the address surfaces onto the canonical scalar rel (the 030 discipline)
    assert "('lives_at',   'address')" in sql
    assert "('lives_at',   'home_address')" in sql
    assert "('lives_at',   'street_address')" in sql
    # the compound qualifiers consume their own compounds (the sibling-serve guard)
    assert "('has_email',  'email_address')" in sql
    # GUARDED inserts: a canonical rel missing in a schema must never abort the file
    assert "WHERE EXISTS (SELECT 1 FROM public.rel_types" in sql
    assert "ON CONFLICT (alias) DO NOTHING" in sql
    # the guarded loop skips a partially-provisioned seat (the 313 pattern)
    assert "WHEN undefined_table THEN NULL" in sql


def test_correction_residence_flag_defaults_on():
    from src.api import main as m
    assert m.CORRECTION_RESIDENCE_RETIRE is True


def test_migration_files_are_numbered_dense():
    # 323 landed (the sense layer, LANGUAGE-STANDARDS-SPEC.md §2) after 322
    # (seed_medical_shelf_noun_cues, BUG-FAMILY-9 residual 2), then 324
    # (growth_signals — renumbered to 324 on landing) — and 321
    # (seed_hobby_activity_aspects, the s1 hobby two-seam serve half) landed in
    # the same window; the RESERVED pointer names 325. Vehicle updated by design
    # (2026-11-13): the pointer's whole contract
    # is to move as migrations land — the density mechanism (the pointer names the
    # next free number) is byte-identical.
    reserved = (pathlib.Path(__file__).resolve().parents[1] /
                "migrations" / "RESERVED.md").read_text()
    assert "Next free saas number: 325" in reserved
    assert (pathlib.Path(__file__).resolve().parents[1] /
            "migrations" / "323_sense_layer_seed.sql").exists()
    assert (pathlib.Path(__file__).resolve().parents[1] /
            "migrations" / "321_seed_hobby_activity_aspects.sql").exists()
