"""THE BLENDED-FAMILY PINS — the family gate as pins (lane/blended-family-smoke-20261112).

The blended-family smoke (DEV/BUGS/family-smoke-20261111/blended_family_smoke.py) drove the
owner's real family shape — current wife + ex-wife + 5 kids + 3 pets + house + homelab +
medical + custody — through 20 turns on a fresh pre-prod seat and measured 15/36 (42%) raw
recall. EVERY miss was reproduced LOCALLY through the deterministic spine (the same sentences,
the same wounds — the extraction half is deterministic, not LLM variance), and each defect
class is FIXED behind BLENDED_FAMILY_CAPTURE / REGISTER_SCHEDULE_TIER (OFF → byte-parity) and
seeded by migration 320 (the kinship cues + the birthday alias). These pins freeze each fix at
its MEASURED sentence so the family gate cannot regress:

  BF1  PERSON-FIRST PRONOUN COREF — "…this September; her birthday is August 3" bound Wren's
       birthday ON a september entity (a temporal PROPN won the nearest-NP race). The closed
       personal-pronoun set resolves through _person_coref FIRST; date-span-overlapping
       PROPN candidates are skipped.
  BF2  KIN-AMOD / TYPE-PREMODIFIER SPLIT — "Our youngest Saskia" welded the superlative into
       the surface ("youngest saskia" beside a bare "saskia" — TWO nodes for ONE kid); "our
       bearded dragon Cecil" welded the species into the pet's name. A PROPN head with
       non-PROPN premodifiers is a NAMED INSTANCE: the name is the entity, the modifiers are
       its type phrase (instance_of); a pet-class head files has_pet.
  BF3  BIRTHDAY PAST BINDING — a bare month-day birthday resolved to the NEXT occurrence
       ("her birthday is May 8" → 2027-05-08, a birth date in the FUTURE). The birthday-class
       attribute steps back to the most recent past occurrence.
  BF4  APPOSITIVELY-RENAMED SUPERLATIVE HEAD — "Our eldest, Leila, turned 14" filed the age
       on the bare adjective "eldest" (spaCy tags it ADJ; the appositive-name gate required
       NOUN). The apposed PROPN names the SAME referent.
  BF5  INSTITUTION AT-PP FRAME — "at Ridgeline Elementary" / "at Northgate High" declined as
       a pp-embedded label and flowed to the growth residue. An at-PP whose pobj heads with a
       school noun (closed functional set) + a stated NAME files educated_at on the resolved
       subject.
  BF6  KIN-OF UPGRADE — "Isabelle is the mother of Wren and Teo" emitted related_to edges the
       mother query could never reach. An identifying of-PP on a kin-cue complement head
       emits the map's kin rel (mother→parent_of) per conjunct.
  BF7  SCHEDULE TIER EXTENSIONS — the appointment day-name ("is on December 3rd at 9:15am")
       was refused (sched-datetime required a bare digit day); the visitation cadence ("the
       kids are with me every other weekend") had NO frame at all and filed on the nearest
       dobj lead ("joint"). sched-datetime admits a month-name day; the visit-cadence frame
       pairs the value with the frame's own copula subject.
  BF8  FLAG PARITY — BLENDED_FAMILY_CAPTURE=false → every new branch is a dead entry
       (byte-parity with the pre-fix extraction).

Hermetic pins run wherever spaCy + en_core_web_sm loads (SPACY_MODEL=en_core_web_sm); they
SKIP otherwise (the module-level skipif — disclosed, never a silent pass).
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

pytest.importorskip("spacy")

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

import src.extraction.linguistics as L  # noqa: E402

pytestmark = pytest.mark.skipif(
    not L.linguistics_available(),
    reason="spaCy linguistic layer unavailable (en_core_web_sm not installed) — "
           "spine deriver no-ops; executes wherever the model loads",
)

_REF = __import__("datetime").datetime(2026, 11, 12)

# ── THE MEASURED SENTENCES (verbatim from the blended-family smoke driver) ──────────
WREN = "Our daughter Wren started kindergarten this September; her birthday is August 3."
SASKIA = "Our youngest Saskia is 3 — her birthday is May 8 — and she goes to Sunflower Daycare."
LEILA = ("Our eldest, Leila, turned 14 on January 22; she's a freshman at Northgate High "
         "and carries an EpiPen for a bee-sting allergy.")
TEO = ("My son Teo is 9 and he's in grade 4 at Ridgeline Elementary — soccer practice at "
       "the Riverside Sportsplex every Tuesday and Thursday at 5pm.")
CECIL = ("Nutmeg's vet appointment is on the 22nd at 9:30am at Cedar Grove Veterinary "
         "Clinic, and our bearded dragon Cecil lives in the 40-gallon tank by the window.")
EX2 = "Isabelle is the mother of Wren and Teo."
CUSTODY = "We have joint custody — the kids are with me every other weekend and with Isabelle on weekdays."
APPT = "My cardiology follow-up is on December 3rd at 9:15am."
MED2_LEGACY = ("My endocrinologist appointment is on the 15th at 2:30pm, endocrinology "
               "clinic, third floor.")


def _triples(text, ref=_REF):
    doc = L._parse(text)
    return [(f.subject, f.rel_type, str(f.object)) for f in
            (L.derive_sentence_facts(doc, ref) or [])]


def _attr_values(text, ref=_REF):
    doc = L._parse(text)
    out = {}
    for f in (L.derive_sentence_facts(doc, ref) or []):
        out[(f.subject, f.rel_type)] = str(f.object)
    return out


# ══════════════════════════════════════════════════════════════════════════════════
# BF1 — the birthday binds to the PERSON, never to a temporal PROPN

def test_bf1_wren_birthday_binds_to_wren():
    vals = _attr_values(WREN)
    assert vals.get(("wren", "birthday")) == "2026-08-03", vals
    assert not any(s == "september" for (s, _r) in vals), vals


def test_bf1_saskia_birthday_binds_to_saskia():
    vals = _attr_values(SASKIA)
    assert vals.get(("saskia", "birthday")) == "2026-05-08", vals


# ══════════════════════════════════════════════════════════════════════════════════
# BF2 — one node per kid/pet (the weld split)

def test_bf2_saskia_is_one_node_not_a_weld():
    triples = _triples(SASKIA)
    subjects = {s for (s, _r, _o) in triples}
    assert "youngest saskia" not in subjects, triples
    assert "saskia" in subjects, triples
    assert ("saskia", "age", "3") in triples, triples


def test_bf2_cecil_species_is_the_type_place():
    triples = _triples(CECIL)
    subjects = {s for (s, _r, _o) in triples}
    assert "bearded dragon cecil" not in subjects, triples
    assert ("cecil", "instance_of", "bearded dragon") in triples, triples


def test_bf2_cecil_owned_by_name_not_weld():
    triples = _triples(CECIL)
    assert ("user", "owns", "cecil") in triples or ("user", "has_pet", "cecil") in triples, \
        triples
    assert not any(_r in ("owns", "has_pet") and _o == "bearded dragon cecil"
                   for (_s, _r, _o) in triples), triples


# ══════════════════════════════════════════════════════════════════════════════════
# BF3 — a birth date is never in the future

def test_bf3_saskia_birthday_steps_back_to_the_past():
    vals = _attr_values(SASKIA)
    bday = vals.get(("saskia", "birthday"), "")
    assert bday and bday[:10] <= "2026-11-12", vals  # never ahead of the reference


# ══════════════════════════════════════════════════════════════════════════════════
# BF4 — the appositively-renamed superlative head is the named person

def test_bf4_leila_owns_her_turn_measure():
    triples = _triples(LEILA)
    subjects = {s for (s, _r, _o) in triples}
    assert "eldest" not in subjects, triples
    # SPINE_TURNED_AGE (wound-map round 4, wound "leila-age"): the turned-N clause now
    # mints the AGE attribute (the 'X is 9' storage) instead of the opaque `turn` scalar
    # that recall served as "Leila's turn is 2027-01-22". Pinned here because this test
    # froze the legacy (leila, turn, '14') shape the fix replaces.
    assert ("leila", "age", "14") in triples, triples
    assert not any(r == "turn" for (_s, r, _o) in triples), triples


def test_bf4_leila_school_and_epipen_land_on_leila():
    triples = _triples(LEILA)
    assert ("leila", "carry", "epipen") in triples, triples
    assert ("leila", "instance_of", "freshman") in triples, triples


# ══════════════════════════════════════════════════════════════════════════════════
# BF5 — the institution at-PP frame

def test_bf5_teo_educated_at_ridgeline():
    triples = _triples(TEO)
    assert ("teo", "educated_at", "ridgeline elementary") in triples, triples


def test_bf5_leila_educated_at_northgate():
    triples = _triples(LEILA)
    assert ("leila", "educated_at", "northgate high") in triples, triples


# ══════════════════════════════════════════════════════════════════════════════════
# BF6 — the kin-of upgrade

def test_bf6_isabelle_parent_of_both_children():
    triples = _triples(EX2)
    assert ("isabelle", "parent_of", "wren") in triples, triples
    assert ("isabelle", "parent_of", "teo") in triples, triples


# ══════════════════════════════════════════════════════════════════════════════════
# BF7 — the schedule tier extensions

def test_bf7_visit_cadence_subject_is_the_kids():
    hits = L.detect_instruction_settings(CUSTODY, L._parse(CUSTODY))
    assert ("kids", "every other weekend", "has_setting") in [
        (s, v, r) for (s, v, _st, _en, r) in hits], hits
    assert not any(s == "joint" for (s, _v, _st, _en, _r) in hits), hits


def test_bf7_sched_datetime_admits_month_name_day():
    hits = L.detect_instruction_settings(APPT, L._parse(APPT))
    vals = [v for (_s, v, _st, _en, _r) in hits]
    assert any("december 3rd" in v.lower() and "9:15am" in v.lower() for v in vals), hits


def test_bf7_sched_datetime_legacy_bare_day_unchanged():
    hits = L.detect_instruction_settings(MED2_LEGACY, L._parse(MED2_LEGACY))
    assert ("endocrinologist appointment", "the 15th at 2:30pm", "has_setting") in [
        (s, v, r) for (s, v, _st, _en, r) in hits], hits


# ══════════════════════════════════════════════════════════════════════════════════
# BF8 — flag parity: OFF → the pre-fix extraction, byte-for-byte

def test_bf8_flag_off_no_weld_split(monkeypatch):
    monkeypatch.setattr(L, "BLENDED_FAMILY_CAPTURE", False)
    triples = _triples(CECIL)
    subjects = {s for (s, _r, _o) in triples}
    assert "bearded dragon cecil" in subjects, triples  # the welded surface returns


def test_bf8_flag_off_no_birthday_rebind(monkeypatch):
    monkeypatch.setattr(L, "BLENDED_FAMILY_CAPTURE", False)
    vals = _attr_values(WREN)
    # the pre-fix wound: the birthday bound on the september entity
    assert vals.get(("september", "birthday")) == "2026-08-03", vals


def test_bf8_flag_off_no_visit_cadence_hint(monkeypatch):
    monkeypatch.setattr(L, "REGISTER_SCHEDULE_TIER", False)
    hits = L.detect_instruction_settings(CUSTODY, L._parse(CUSTODY))
    assert hits == [], hits


def test_bf8_flag_off_no_education_frame(monkeypatch):
    monkeypatch.setattr(L, "BLENDED_FAMILY_CAPTURE", False)
    triples = _triples(TEO)
    assert not any(_r == "educated_at" for (_s, _r, _o) in triples), triples
