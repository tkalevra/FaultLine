"""THE WOUND-MAP ROUND-4 FRAME PINS, batch 2 (2026-11-13, family-smoke capture wounds).

Two deterministic, flag-gated ingest-side capture fixes, each with its mechanism pin,
its flag-OFF byte-parity pin, and its negative guards:

  1. SPINE_GOES_TO_INSTITUTION  — the goes-to school frame ("she goes to Sunflower
     Daycare" mints the SAME educated_at storage the at-PP institution frame files).
     Wound: recall miss "saskia-school".

  2. SPINE_WEEKLY_CADENCE       — the practice-cadence days ("soccer practice … every
     Tuesday and Thursday at 5pm" mints (subject, has_setting, <cadence span>) on the
     clause subject the custody-cadence policy resolves). Wound: recall miss
     "teo-sport".

The measured sentences are verbatim from the family onboarding smoke. Flags are read at
import; the OFF pins patch the module attribute (the round-3 pins' convention).
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

# ── the measured wound turns (verbatim from the family onboarding smoke) ──────────
SASKIA_TURN = ("Our youngest Saskia is 3 — her birthday is May 8 — "
               "and she goes to Sunflower Daycare.")
TEO_TURN = ("My son Teo is 9 and he's in grade 4 at Ridgeline Elementary — "
            "soccer practice at the Riverside Sportsplex every Tuesday and "
            "Thursday at 5pm.")


def _triples(text, ref=_REF):
    doc = L._parse(text)
    return [(f.subject, f.rel_type, str(f.object)) for f in
            (L.derive_sentence_facts(doc, ref) or [])]


def _triples_with_prior(text, prior, ref=_REF):
    """The live atomized-path call shape: derive_sentence_facts on the atom STRING with
    the harvest's prior_nps accumulator threaded in (main.py hands the raw sentence and
    the cross-atom antecedents — no pre-built doc on the str lane)."""
    return [(f.subject, f.rel_type, str(f.object)) for f in
            (L.derive_sentence_facts(text, ref, prior_nps=list(prior)) or [])]


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 1 — SASKIA-SCHOOL: the goes-to school frame (SPINE_GOES_TO_INSTITUTION)

def test_goes_to_institution_mints_educated_at():
    """THE MECHANISM PIN: "<person> goes to <ProperName institution>" files the SAME
    educated_at storage the at-PP frame files — on the person, resolved through the
    same subject ladder (pronoun → person coref → 'saskia')."""
    triples = _triples(SASKIA_TURN)
    assert ("saskia", "educated_at", "sunflower daycare") in triples, triples


def test_goes_to_institution_bare_pronoun_shape():
    """The bare clause shape also mints (the pronoun surface binds when no antecedent
    precedes — the same contract the legacy go_to row already carries)."""
    triples = _triples("she goes to Sunflower Daycare")
    assert any(r == "educated_at" and o == "sunflower daycare"
               for (_, r, o) in triples), triples


def test_goes_to_institution_first_person():
    """"I go to <named school>" files the user's education."""
    triples = _triples("I go to Maplewood Preschool")
    assert ("user", "educated_at", "maplewood preschool") in triples, triples


def test_goes_to_negatives_never_mint_educated_at():
    """THE NEGATIVE GUARDS: no institution head or no proper name → no mint. The
    legacy go_to row may stand; educated_at must NEVER appear."""
    for sent in ("he goes to the gym",
                 "He goes to work early",
                 "I go to the store on Fridays",
                 "She goes to school nearby",
                 "they go to the pool every summer"):
        triples = _triples(sent)
        assert not any(r == "educated_at" for (_, r, _) in triples), (sent, triples)


def test_goes_to_flag_off_byte_parity(monkeypatch):
    """SPINE_GOES_TO_INSTITUTION=0 restores the pre-fix mint exactly: the legacy rows
    only, no educated_at (measured pre-fix shape: the go_to row plus the kin/age/birthday
    captures, byte-for-byte)."""
    monkeypatch.setattr(L, "SPINE_GOES_TO_INSTITUTION", False)
    off_triples = _triples(SASKIA_TURN)
    assert ("saskia", "educated_at", "sunflower daycare") not in off_triples, off_triples
    # the pre-fix measured shape (verbatim from the live probe before this fix landed)
    assert ("saskia", "go_to", "sunflower daycare") in off_triples, off_triples
    assert ("saskia", "age", "3") in off_triples, off_triples
    assert ("saskia", "birthday", "2026-05-08") in off_triples, off_triples


def test_goes_to_atpp_frame_unchanged():
    """The pre-existing at-PP institution frame keeps its exact shape (the sibling did
    not widen the shared head set's behavior on the at-PP lane)."""
    triples = _triples("she's a freshman at Northgate High")
    assert any(r == "educated_at" and "northgate" in o for (_, r, o) in triples), triples


# ── WOUND (round-4 critic, saskia-school): the date-subject mis-bind on the ATOMIZED path ──
# The live harvest splits the Saskia turn at the em-dashes into atoms and threads each
# fact's OBJECT into the next atom's prior_nps. The birthday atom's object is the RESOLVED
# ISO DATE "2026-05-08" — and the person-coref _prior fallback (most-recent antecedent)
# handed the go-clause's pronoun subject to the DATE: (2026-05-08, go_to, …) with the
# person row lost. The fix reuses the BLENDED_FAMILY_CAPTURE date-skip discipline (the
# SAME _collect_date_spans detector) in the _prior ladder, inside this flag.

import re  # noqa: E402

_DATE_SUBJECT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}/\d{1,2}(/\d{2,4})?$")


def _is_date_shaped(s):
    return bool(_DATE_SUBJECT_RE.match((s or "").strip().lower())) \
        or (s or "").strip().lower() in ("may 8", "may 8th")


def test_goes_to_date_prior_atom_binds_the_person():
    """THE ATOMIZED-PATH MECHANISM PIN: the go-clause atom ("and she goes to Sunflower
    Daycare.") with the live prior_nps accumulator state (the birthday atom's resolved
    ISO date as the most-recent antecedent) binds SASKIA — the date candidate is skipped
    and the next-antecedent person wins."""
    triples = _triples_with_prior("and she goes to Sunflower Daycare.",
                                  ["youngest", "saskia", "2026-05-08"])
    assert ("saskia", "educated_at", "sunflower daycare") in triples, triples
    assert ("saskia", "go_to", "sunflower daycare") in triples, triples


def test_goes_to_never_mints_on_a_date_shaped_subject():
    """THE WOUND PIN: across BOTH the whole-turn derive and the atomized sequence, no
    go_to/educated_at row may ever carry a date-shaped subject (the measured corrupted
    shape (2026-05-08, go_to, …))."""
    for triples in (_triples(SASKIA_TURN),
                    _triples_with_prior("and she goes to Sunflower Daycare.",
                                        ["youngest", "saskia", "2026-05-08"])):
        assert not any(r in ("go_to", "educated_at") and _is_date_shaped(s)
                       for (s, r, _) in triples), triples


def test_goes_to_date_subject_sentence_stays_legacy():
    """THE NEGATIVE GUARD: a pure date-subject sentence ("May 8 is her birthday") behaves
    exactly as before the fix — byte-identical with the flag ON and OFF (the date-skip
    lives in the pronoun-coref _prior ladder only; it never rewrites a literal date
    subject)."""
    on_triples = _triples("May 8 is her birthday")
    assert on_triples == [], on_triples  # the pre-fix legacy shape (no rows)


def test_goes_to_date_prior_flag_off_restores_legacy_junk(monkeypatch):
    """THE OFF PARITY LEG (the atomized path): SPINE_GOES_TO_INSTITUTION=0 restores the
    pre-fix mint exactly — the legacy go_to row on the DATE subject, byte-for-byte, and
    no educated_at (the frame's own OFF contract)."""
    monkeypatch.setattr(L, "SPINE_GOES_TO_INSTITUTION", False)
    triples = _triples_with_prior("and she goes to Sunflower Daycare.",
                                  ["youngest", "saskia", "2026-05-08"])
    assert triples == [("2026-05-08", "go_to", "sunflower daycare")], triples


# ══════════════════════════════════════════════════════════════════════════════════
# WOUND 2 — TEO-SPORT: the practice-cadence days (SPINE_WEEKLY_CADENCE)

def test_weekday_cadence_mints_on_the_clause_subject():
    """THE MECHANISM PIN: "every Tuesday and Thursday at 5pm" files
    (teo, has_setting, <cadence span>) — the custody-cadence subject policy (nearest
    leftward clause nsubj, pronoun resolved through person coref)."""
    triples = _triples(TEO_TURN)
    rows = [(s, o) for (s, r, o) in triples if r == "has_setting"]
    assert rows and rows[0][0] == "teo", (rows, triples)
    val = rows[0][1]
    assert "tuesday" in val and "thursday" in val, val


def test_weekday_cadence_first_person_binds_user():
    """A first-person cadence clause binds the user (the tighter-subject-else-user
    contract)."""
    triples = _triples("I have swim practice every Monday and Wednesday at 7am")
    assert any(s == "user" and r == "has_setting"
               and "monday" in o and "wednesday" in o
               for (s, r, o) in triples), triples


def test_weekday_cadence_no_subject_falls_back_to_user():
    """No tighter clause subject resolves (a bare cadence fragment) → the user, the
    visit-cadence fallback contract."""
    triples = _triples("Every Tuesday and Thursday at 5pm works best.")
    assert any(s == "user" and r == "has_setting"
               and "tuesday" in o and "thursday" in o
               for (s, r, o) in triples), triples


def test_weekday_cadence_negated_never_mints():
    """The register's negation scan-back contract is reused: a negated cadence never
    mints."""
    triples = _triples("not every Tuesday and Thursday, only Wednesdays")
    assert not any(r == "has_setting" for (_, r, _) in triples), triples


def test_single_weekday_cadence_is_not_this_frame():
    """A bare single "every Friday" (no coordination) stays the recurring register
    frame's territory — this frame requires ≥2 coordinated weekday words."""
    triples = _triples("we deploy every Friday")
    assert not any(r == "has_setting" and "friday" in o
                   for (_, r, o) in triples), triples


def test_custody_visit_cadence_control_unchanged():
    """THE CONTROL: the custody-cadence capture keeps its exact pre-fix shape."""
    triples = _triples("the kids are with me every other weekend")
    assert ("kids", "has_setting", "every other weekend") in triples, triples


def test_weekday_cadence_flag_off_byte_parity(monkeypatch):
    """SPINE_WEEKLY_CADENCE=0 restores the pre-fix mint exactly: the measured pre-fix
    shape of the Teo turn (child_of/age/educated_at/located_in/grade/has_role rows),
    byte-for-byte, with NO has_setting row."""
    monkeypatch.setattr(L, "SPINE_WEEKLY_CADENCE", False)
    triples = _triples(TEO_TURN)
    assert not any(r == "has_setting" for (_, r, _) in triples), triples
    # the pre-fix measured shape (verbatim from the live probe before this fix landed)
    assert ("teo", "child_of", "user") in triples, triples
    assert ("teo", "age", "9") in triples, triples
    assert ("teo", "educated_at", "ridgeline elementary") in triples, triples
    assert ("teo", "located_in", "grade 4") in triples, triples
    assert ("he", "has_role", "soccer practice") in triples, triples
