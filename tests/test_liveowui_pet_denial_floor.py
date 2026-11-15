# -*- coding: utf-8 -*-
"""THE PET-RELATION DENIAL FLOOR + THE HONEST ZERO-WRITE AUDIT (LIVE-OPENWEBUI FIX 3).

THE MEASURED WOUND (DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, the owner's
prod seat): "Quillfeather is not a pet of Dan's" classified CORRECTION → ZERO DB writes,
no retraction_outcomes row, the old has_pet rows stayed live. The SEC-BUG-1 family: the
correction executor produced nothing and reported nothing.

THE FIX (two pieces):
  1. ARM P of ``_negated_name_correction_floor`` (``_pet_possession_denial_scaffold``): the
     THIRD-PERSON pet/possession denial builds the denial scaffold deterministically —
     subject = the OWNER, old_rel_type = the possession rel the noun's own class resolves
     to, old_value = the pet name, denial_surfaces = [pet name] — so the caller's
     grammatical-cessation arm archives the denied edge and the no-match path reports
     honestly. ARM N (first-person name denial) is untouched.
  2. The incomplete-extraction exit ("relationship type not identified") now WRITES the
     retraction_outcomes row (actually_retracted=False, was_correct=False) — every
     correction path either writes or reports honestly, never silent.

Pure grammar: no database (the possession resolver fail-opens without a DSN; the pet-class
pin still resolves has_pet for the pet nouns)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.pop("POSTGRES_DSN", None)

import src.api.main as M  # noqa: E402

# THE MEASURED PROD SENTENCE (the diagnosis §2, 03:22:41 turn).
MEASURED = "Quillfeather is not a pet of Dan's"


def test_measured_pet_denial_builds_the_scaffold():
    r = M._negated_name_correction_floor(MEASURED)
    assert r is not None
    assert r["old_rel_type"] == "has_pet"
    assert r["old_value"].lower() == "quillfeather"
    assert r["denial_surfaces"] == ["Quillfeather"]
    assert r["dimension"] == "RELATIONAL"
    assert r["new_value"] is None and r["new_rel_type"] is None


def test_pet_denial_owner_surface_is_lowercased():
    r = M._negated_name_correction_floor("Quillfeather is not a pet of Dan")
    assert r is not None and r["subject_name"] == "dan"


def test_first_person_pet_denial_owns_the_user_slot():
    r = M._negated_name_correction_floor("Goose is not my cat")
    assert r is not None
    assert r["subject_name"] == "user"
    assert r["old_rel_type"] == "has_pet"


def test_first_person_possessive_pronoun_object_maps_to_user():
    """'of mine' — en_core_web_sm tags it NOUN with no Person morph; the closed
    function-word surface set still maps it to the user."""
    r = M._negated_name_correction_floor("Bella is not a dog of mine")
    assert r is not None and r["subject_name"] == "user"


def test_arm_n_name_denial_unchanged():
    """The proven wife-denial shape keeps its exact contract."""
    r = M._negated_name_correction_floor("My wife is not Isolde")
    assert r is not None
    assert r["old_rel_type"] == "spouse"
    assert r["subject_name"] == "user"
    assert r["old_value"] == "Isolde"


def test_class_noun_plural_subject_declines():
    """'Pets are not a burden of mine' — a class noun, never a denied instance name."""
    assert M._negated_name_correction_floor("Pets are not a burden of mine") is None


def test_non_negated_root_declines():
    assert M._negated_name_correction_floor("Quillfeather is a pet of Dan") is None


def test_incomplete_exit_writes_the_outcome_row():
    """SEC-BUG-1 honesty contract, source-level pin: the 'relationship type not
    identified' terminal writes retraction_outcomes (actually_retracted=False,
    was_correct=False) before returning — the silent zero-write exit is gone."""
    src = open(M.__file__, encoding="utf-8").read()
    start = src.index("if not old_rel_type:")
    end = src.index("relationship type not identified", start)
    block = src[start:end]
    assert "INSERT INTO retraction_outcomes" in block
    assert "actually_retracted, was_correct" in block
    assert "extraction_incomplete" in block
