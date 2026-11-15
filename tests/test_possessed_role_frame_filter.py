"""Fog-family critic-R5-7 pins (2026-11-07) - the possessed-role frame filter
(POSSESSED_ROLE_FRAME_FILTER, default ON).

LIVE WOUND (critic R5 seat cd0c09ed-..., measured): "who is Bramwell's wife?" -
Bramwell has NO facts - leaked a USER-side kinship line into the answer. The anchor
lane fell through the kinship HEAD ("wife" -> spouse rail) and bound the user's spouse
entity; the walk returned the user's own stated row (Wren, spouse, Leofwine) WITH live
prose ("Wren and you are spouses/partners") - a first-person answer riding a
third-party question. THE FIX (display/walk seam, lane A): a FRAME FILTER - on a
possessed-kinship frame whose possessor is a NAMED THIRD PARTY, kinship rows that
speak the USER perspective (definition addresses "you", or a slot IS the user entity)
are dropped from the response composition. The A-boundary doctrine: the ANSWER band
about the TARGET decides. Detection is METADATA + grammar: the head resolves through
the per-tenant person-role rail (the SAME rail every possessed chain reads), the
possessor surface is probed against the user's own alias set. First-person frames
("my wife"), user-named possessors, and non-kinship heads ("Osric's parrot") never
fire; rows stay in the DB; the F4 gate governs poison rows independently.

Measured live shapes (critic seat): Bramwell's wife -> facts carried the user-stated
spouse row WITH prose (the leak) + 4 prose-declined B rows + 3 doc hits; the filter
leaves the prose-less B rows (band-silent anyway) and the doc band answers alone.
"""

from src.api import main as main_mod
from src.api.main import (
    _filter_possessed_role_user_kinship,
    _third_party_possessed_kinship_frame,
)


def _patch_rails(monkeypatch, role_heads, kin_rels):
    monkeypatch.setattr(
        "src.extraction.linguistics._possessed_person_role_rel",
        lambda s: role_heads.get(str(s).strip().lower()))
    import src.api.linguistic_cue_overlay as _lco
    monkeypatch.setattr(_lco, "resolve_kinship_rel_map",
                        lambda dsn: dict(kin_rels))
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


ROWS = [
    {   # THE LEAK ROW: user-side kinship with live prose
        "rel_type": "spouse", "subject": "Wren", "object": "Leofwine",
        "definition": "Wren and you are spouses/partners",
        "fact_provenance": "user_stated", "fact_class": "A",
    },
    {   # the target-side row: about the anchor's kinship, NOT the user
        "rel_type": "parent_of", "subject": "Tessa", "object": "Evander",
        "definition": "Tessa is the parent of Evander",
        "fact_provenance": "user_stated", "fact_class": "A",
    },
    {   # prose-declined poison debris (F4 gate): band-silent anyway
        "rel_type": "spouse", "subject": "Magnus", "object": "Alcuin",
        "definition": None, "fact_provenance": "llm_inferred", "fact_class": "B",
    },
]


def test_third_party_frame_drops_user_kinship(monkeypatch):
    """THE WOUND PIN: on a third-party possessed-kinship frame, the user's own spouse
    row (definition addresses 'you') does not ride the answer; the target-side row and
    the prose-declined debris survive."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    kept = _filter_possessed_role_user_kinship(
        ROWS, "who is Bramwell's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    subjects = [k.get("subject") for k in kept]
    assert "Wren" not in subjects
    assert "Tessa" in subjects and "Magnus" in subjects


def test_first_person_frame_is_noop(monkeypatch):
    """'my wife' is the FIRST-PERSON frame: user kinship rows ARE the answer -
    byte-identical passthrough."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    kept = _filter_possessed_role_user_kinship(
        ROWS, "who is my wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == len(ROWS)


def test_user_named_possessor_is_first_person(monkeypatch):
    """A possessor that names the USER themselves is a first-person frame: the alias
    probe fires and nothing is dropped."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    monkeypatch.setattr(
        main_mod, "_surface_is_the_user",
        lambda surface, user_id, db: str(surface).lower() == "leofwine")
    kept = _filter_possessed_role_user_kinship(
        ROWS, "who is Leofwine's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == len(ROWS)


def test_non_kinship_head_never_fires(monkeypatch):
    """'Osric's parrot' - the head is not on the person-role rail: the frame never
    fires and the facts pass untouched (a pet question is not a kinship frame)."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    kept = _filter_possessed_role_user_kinship(
        ROWS, "who is Osric's parrot?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == len(ROWS)


def test_flag_off_is_identity(monkeypatch):
    monkeypatch.setattr(main_mod, "POSSESSED_ROLE_FRAME_FILTER", False)
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    kept = _filter_possessed_role_user_kinship(
        ROWS, "who is Bramwell's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == len(ROWS)


def test_kinship_slot_equal_to_user_dropped(monkeypatch):
    """THE BELT: a kinship row whose OBJECT is the user entity is user-addressed even
    when the definition wording dodges 'you'."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    rows = [{"rel_type": "spouse", "subject": "Wren",
             "object": "11111111-2222-3333-4444-555555555555",
             "definition": "Wren is paired", "fact_provenance": "user_stated"}]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Bramwell's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert kept == []


def test_non_kinship_you_row_survives(monkeypatch):
    """REL-GATED: a non-kinship row whose definition happens to address 'you' is not
    the frame's concern (the filter narrows to kinship rels, never the whole payload)."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse"}, kin_rels={"wife": "spouse"})
    rows = [{"rel_type": "likes", "subject": "Wren", "object": "tea",
             "definition": "You like tea", "fact_provenance": "user_stated"}]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Bramwell's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == 1


def test_frame_detection_shapes(monkeypatch):
    """Detection itself: third-party possessed-kinship fires; first-person, the user's
    own name, and non-kinship heads do not."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse", "husband": "spouse"},
                 kin_rels={"wife": "spouse"})
    monkeypatch.setattr(
        main_mod, "_surface_is_the_user",
        lambda surface, user_id, db: str(surface).lower() == "leofwine")
    assert _third_party_possessed_kinship_frame(
        "who is Bramwell's wife?", "11111111-2222-3333-4444-555555555555", _DB()) is True
    assert _third_party_possessed_kinship_frame(
        "who is Osric's husband?", "11111111-2222-3333-4444-555555555555", _DB()) is True
    assert _third_party_possessed_kinship_frame(
        "who is my wife?", "11111111-2222-3333-4444-555555555555", _DB()) is False
    assert _third_party_possessed_kinship_frame(
        "who is Leofwine's wife?", "11111111-2222-3333-4444-555555555555", _DB()) is False
    assert _third_party_possessed_kinship_frame(
        "who is Osric's parrot?", "11111111-2222-3333-4444-555555555555", _DB()) is False


# == R9 (critic RG-F-M3): subject-must-be-the-target on the residue bands ==========

def _patch_target(monkeypatch, poss, target):
    monkeypatch.setattr(main_mod, "_surface_is_the_user", lambda s, u, db: False)
    monkeypatch.setattr(main_mod, "_frame_possessor_surface", lambda q, u, db: poss)
    monkeypatch.setattr(main_mod, "_surface_entity_id", lambda db, s: target)


def test_bricmer_frame_target_scoping(monkeypatch):
    """THE R9 WOUND SHAPE (measured live on the R5b seat): on the Bricmer frame only
    TARGET rows survive the structured bands - Leopold has_role Trainer, Wren
    instance_of person, and the user-stated Work-is-busy line (filed on an aspect
    entity, NOT the user uuid) all drop; the prose-less Magnus spouse row survives
    the filter (band-silent anyway - a different gate governs its prose)."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse", "father": "parent_of"}, kin_rels={"wife": "spouse", "father": "parent_of"})
    _patch_target(monkeypatch, poss="bricmer", target="22222222-2222-4222-8222-222222222222")
    rows = [
        {"rel_type": "has_role", "subject_id": "22222222-2222-4222-8222-222222222222",
         "subject": "Bricmer", "object": "head fisher",
         "definition": "Bricmer has role head fisher", "fact_provenance": "llm_inferred"},
        {"rel_type": "has_role", "subject_id": "leopold-uuid", "subject": "Leopold",
         "object": "trainer", "definition": "Leopold has role Trainer",
         "fact_provenance": "llm_inferred"},
        {"rel_type": "instance_of", "subject_id": "wren-uuid", "subject": "Wren",
         "object": "person", "definition": "Wren is an instance of person",
         "fact_provenance": "llm_inferred"},
        {"rel_type": "has_state", "subject_id": "work-uuid", "subject": "work",
         "object": "busy", "definition": "Work is busy", "fact_provenance": "user_stated"},
        {"rel_type": "spouse", "subject_id": "magnus-uuid", "subject": "Magnus",
         "object": "Alcuin", "fact_provenance": "llm_inferred"},
    ]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Bricmer's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert [k.get("subject") for k in kept] == ["Bricmer", "Magnus"]


def test_target_subject_pure_role_line_voices(monkeypatch):
    """The narrow-rule guarantee: a doc-true pure-role line whose SUBJECT IS THE
    TARGET keeps voicing - the filter is a subject scope, not a rel ban."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse", "father": "parent_of"}, kin_rels={"wife": "spouse", "father": "parent_of"})
    _patch_target(monkeypatch, poss="bricmer", target="22222222-2222-4222-8222-222222222222")
    rows = [{"rel_type": "has_role", "subject_id": "22222222-2222-4222-8222-222222222222",
             "subject": "Bricmer", "object": "head fisher",
             "definition": "Bricmer has role head fisher", "fact_provenance": "llm_inferred"}]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Bricmer's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert len(kept) == 1


def test_leofstan_frame_height_twin_drops(monkeypatch):
    """THE AGNES TWIN (measured live): under the Leofstan frame the user-stated Agnes
    age row AND its malformed height twin ('64 years old' as a HEIGHT) are non-target
    user-stated lines - both drop; a target-subject row survives."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse", "father": "parent_of"}, kin_rels={"wife": "spouse", "father": "parent_of"})
    _patch_target(monkeypatch, poss="leofstan", target="33333333-3333-4333-8333-333333333333")
    rows = [
        {"rel_type": "age", "subject_id": "agnes-uuid", "subject": "Agnes", "object": "64",
         "definition": "Agnes is 64 years old", "fact_provenance": "user_stated"},
        {"rel_type": "height", "subject_id": "agnes-uuid", "subject": "Agnes",
         "object": "64 years old", "definition": "Agnes's height is 64 years old",
         "fact_provenance": "user_stated"},
        {"rel_type": "parent_of", "subject_id": "leof-uuid", "subject": "Leofstan",
         "object": "Wulfhild", "fact_provenance": "llm_inferred"},
    ]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Leofstan's father?", "11111111-2222-3333-4444-555555555555", _DB())
    assert [k.get("subject") for k in kept] == ["Leofstan"]


def test_unresolvable_possessor_fail_safe(monkeypatch):
    """A possessor surface that resolves nowhere disables the target scoping (fail-safe
    keep) - but the R8 user-subject drop still applies (it needs no target)."""
    _patch_rails(monkeypatch, role_heads={"wife": "spouse", "father": "parent_of"}, kin_rels={"wife": "spouse", "father": "parent_of"})
    _patch_target(monkeypatch, poss="ghost-name", target=None)
    rows = [
        {"rel_type": "has_role", "subject_id": "leopold-uuid", "subject": "Leopold",
         "object": "trainer", "definition": "Leopold has role Trainer",
         "fact_provenance": "llm_inferred"},
        {"rel_type": "has_state", "subject_id": "11111111-2222-3333-4444-555555555555", "subject": "user",
         "object": "busy", "definition": "Work is busy", "fact_provenance": "user_stated"},
    ]
    kept = _filter_possessed_role_user_kinship(
        rows, "who is Ghost-name's wife?", "11111111-2222-3333-4444-555555555555", _DB())
    assert [k.get("subject") for k in kept] == ["Leopold"]
