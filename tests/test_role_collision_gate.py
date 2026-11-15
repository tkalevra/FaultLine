"""Fog-family critic R8 re-gate pins - RG-F possessed-role composition junk.

LIVE WOUND (R5b seat e4a72b6d, measured): "who is Bricmer's wife?" voiced the
IMPOSSIBLE line "Godgifu has role Magnus". DB trace: the fenland doc-lane ingest
wrote the staged row (godgifu, has_role, e6efb2a2) binding the doc person to a ROLE
SLOT; e6efb2a2 is the conversational wife-weld entity (statement lane minted it as
person "magnus", role-slot accretion added the alias "wife"), so composition resolved
the object to its PREFERRED PERSON ALIAS and rendered a person name as the role.
entity_attributes was EMPTY for both entities - the line composes from the staged ROW,
not from attributes.

Two seams fixed (lane A, composition/display - the ROW creation is Lane B's staged
lane, attributed in the R8 report):

  1. RECALL_ROLE_COLLISION_GATE (src/api/main.py, default ON): a has_role row whose
     OBJECT entity carries BOTH a role-word alias (the per-tenant person-role rail)
     AND a non-role alias is an alias-collision weld; the prose is declined (the row
     stays in structured facts). Pin: possessed-role answers must never compose a
     role line binding two entities with no stored attr/row evidence - the weld's
     person name IS the no-evidence binding.
  2. POSSESSED_ROLE_FRAME_FILTER extension (R7 lever, same flag): on a third-party
     possessed-kinship frame a USER-SUBJECT line of ANY rel is an answer about the
     wrong party ("Work is busy" voicing under the Bricmer query - folded OBS).
"""

from unittest.mock import MagicMock, patch

from src.api import main as main_mod
from src.api.main import (
    _entity_alias_collision_weld,
    _filter_possessed_role_user_kinship,
    convert_to_prose,
)


# -- mocks -------------------------------------------------------------------------

def _db_with_aliases(rows):
    """A db whose cursor.fetchall serves the entity's alias rows (the collision
    detector's one indexed read); every other read comes back empty."""
    mock_db = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = list(rows)
    mock_db.cursor.return_value.__enter__.return_value = cursor
    return mock_db


def _patch_rail(monkeypatch, role_heads=None):
    monkeypatch.setattr(
        "src.extraction.linguistics._possessed_person_role_rel",
        lambda s: role_heads.get(str(s).strip().lower(), False))


HAS_ROLE = {"natural_language": "X has role Y", "natural_language_2p": None,
            "tail_types": ["ANY"], "label": "has role"}

NAMES = {"a8f0": "Godgifu", "e6ef": "Magnus", "78dc": "Leopold", "tr1": "Trainer"}

UID = "11111111-2222-3333-4444-555555555555"


def _prose_for(monkeypatch, facts, db, gate_on=True):
    monkeypatch.setattr(main_mod, "RECALL_ROLE_COLLISION_GATE", gate_on)
    _patch_rail(monkeypatch, role_heads={"wife": True, "husband": True})
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"has_role": HAS_ROLE}):
        return convert_to_prose(facts, db, anchor=None, user_id=UID,
                                preferred_alias_map=NAMES)


# == 1. THE WOUND PIN: weld object never voices as a person-role line ==============

def test_collision_weld_object_suppressed(monkeypatch):
    """THE WOUND: (godgifu, has_role, e6ef) where e6ef aliases [magnus, wife] - the
    weld makes the row read 'Godgifu has role Magnus'. The line must not compose."""
    facts = [{"rel_type": "has_role", "subject": "Godgifu", "object": "Magnus",
              "_subject_id": "a8f0", "_object_id": "e6ef",
              "fact_provenance": "llm_inferred", "fact_class": "B"}]
    db = _db_with_aliases([("magnus",), ("wife",)])
    prose = _prose_for(monkeypatch, facts, db)
    assert not any("Godgifu has role" in line for line in prose)


def test_pure_role_object_voices(monkeypatch):
    """A role object with ONLY the role alias (trainer) is an honest doc reading -
    'Leopold has role Trainer' keeps voicing (the R5b junk was the WELD, not this)."""
    facts = [{"rel_type": "has_role", "subject": "Leopold", "object": "Trainer",
              "_subject_id": "78dc", "_object_id": "tr1",
              "fact_provenance": "llm_inferred", "fact_class": "B"}]
    db = _db_with_aliases([("trainer",)])
    prose = _prose_for(monkeypatch, facts, db)
    assert any("Leopold has role Trainer" in line for line in prose)


def test_pure_person_object_voices(monkeypatch):
    """A person-only object (no role alias) carries the row as evidence - voicing is
    honest composition, not the collision."""
    facts = [{"rel_type": "has_role", "subject": "Godgifu", "object": "Magnus",
              "_subject_id": "a8f0", "_object_id": "e6ef",
              "fact_provenance": "llm_inferred", "fact_class": "B"}]
    db = _db_with_aliases([("magnus",)])
    prose = _prose_for(monkeypatch, facts, db)
    assert any("Godgifu has role Magnus" in line for line in prose)


def test_flag_off_is_legacy(monkeypatch):
    monkeypatch.setattr(main_mod, "RECALL_ROLE_COLLISION_GATE", False)
    facts = [{"rel_type": "has_role", "subject": "Godgifu", "object": "Magnus",
              "_subject_id": "a8f0", "_object_id": "e6ef",
              "fact_provenance": "llm_inferred", "fact_class": "B"}]
    db = _db_with_aliases([("magnus",), ("wife",)])
    _patch_rail(monkeypatch, role_heads={"wife": True, "husband": True})
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"has_role": HAS_ROLE}):
        prose = convert_to_prose(facts, db, anchor=None, user_id=UID,
                                 preferred_alias_map=NAMES)
    assert any("Godgifu has role Magnus" in line for line in prose)


# == 2. the weld detector itself ====================================================

def test_weld_detector_shapes():
    assert _entity_alias_collision_weld(
        _db_with_aliases([("magnus",), ("wife",)]), "e6ef") is True
    assert _entity_alias_collision_weld(
        _db_with_aliases([("trainer",)]), "tr1") is False
    assert _entity_alias_collision_weld(
        _db_with_aliases([("leopold",)]), "78dc") is False
    assert _entity_alias_collision_weld(_db_with_aliases([]), "x") is False


# == 3. frame-filter extension: user-subject lines drop on third-party frames ======

def _patch_frame_rails(monkeypatch):
    monkeypatch.setattr(
        "src.extraction.linguistics._possessed_person_role_rel",
        lambda s: {"wife": "spouse"}.get(str(s).strip().lower()))
    import src.api.linguistic_cue_overlay as _lco
    monkeypatch.setattr(_lco, "resolve_kinship_rel_map",
                        lambda dsn: {"wife": "spouse"})
    monkeypatch.setattr(_lco, "resolve_social_role_map", lambda dsn: {})


class _NC:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchone(self):
        return None


class _DB:
    def cursor(self):
        return _NC()


def test_user_line_drops_on_third_party_frame(monkeypatch):
    """'Work is busy' (user-subject, has_state) must not voice under
    'who is Bricmer's wife?' - subject-is-user is the evidence, any rel."""
    _patch_frame_rails(monkeypatch)
    rows = [{"rel_type": "has_state", "subject": UID,
             "object": "busy", "definition": "Work is busy",
             "fact_provenance": "user_stated"},
            {"rel_type": "has_role", "subject": "godgifu", "object": "magnus",
             "definition": "Godgifu has role Magnus",
             "fact_provenance": "llm_inferred"}]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Bricmer's wife?", UID, _DB())
    assert [k.get("subject") for k in kept] == ["godgifu"]


def test_first_person_frame_still_identity(monkeypatch):
    """R7 regression guard: 'my wife' frames keep user rows byte-identical."""
    _patch_frame_rails(monkeypatch)
    rows = [{"rel_type": "has_state", "subject": UID,
             "object": "busy", "definition": "Work is busy",
             "fact_provenance": "user_stated"}]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is my wife?", UID, _DB())
    assert len(kept) == 1
