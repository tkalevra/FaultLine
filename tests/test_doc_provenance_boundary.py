"""THE PROVENANCE-BOUNDARY LITMUS (owner directive 2026-11-15) — the document/user
world-model boundary, composed as pins.

THE OWNER'S TEST CASE (verbatim): a document is ingested containing the sentence —
In the book "Tom's wife Sally and their son Timmy live just next door".
The reference is valid INSIDE the document's L4-tied context, and must NOT leak into
the user's world model:
  (a) not filed as a USER fact ((user, spouse, sally) / (user, parent, timmy)); the
      narrative characters file as their OWN entities with no kin-space membership;
  (b) not served when the user asks "who is my wife?" / "tell me about my family";
  (c) the document-grounded retrieval still serves the passage WITH its citation.

THE LIVE RECEIPTS (pre-prod truenas, throwaway seat 3c329b32…, image sha256:52826027…,
2026-11-15): document drained committed=0 staged=5/9 (all prov=document,
fact_provenance=llm_inferred, fact_class=B, source_ref cited); the kinship edges were
NOT filed at all (the kin_role_word_surface pre-classification drop caught the
role-surface edge the extractor emitted); tom/sally/timmy are standalone Person
entities with zero edges touching the user; "who is my wife?" / "tell me about my
family" served honest-empty; the doc-grounded asks served the verbatim passage twice
cited (document id 1, section 1 / page 1). REVERSE GUARD (seat 8f31fe47…, first-person
doc): (user, spouse, isolde), (poppy, child_of, user), (user, has_pet, nimbus) all
FILED (staged B, document provenance, cited) — nimbus entered the pets space, and the
document never named the user (document_lane_never_names_the_user).

This file pins the DETERMINISTIC seams of that boundary (the extraction half is
LLM-backed and is covered by the live receipts + the ingest-side pins in
tests/api/test_doc_tier_ingest_authorship.py and tests/test_user_alias_speaker_frame_64.py;
the serve-side flags carry their own OFF pins in tests/test_recall_doc_poison_dose.py):

  1. THE ROUTER ASYMMETRY — source="document" can never mint user_stated
     (the root stamp the whole boundary hangs from).
  2. THE TAUTOLOGY GATE CONTRACT — a kinship ROLE WORD on the NAME slot refuses
     wherever it resolves (the live drop's shape); a genuine name binds and reaches
     the adjudication.
  3. THE FAMILY-WALK CONTAINMENT — _collect_member_taxonomy_entities is
     subject-anchored SQL: the doc-character rows are structurally unreachable from
     the user anchor, provenance regardless.
  4. THE USER-ANCHORED DOC KINSHIP REPLAY (owner litmus (b)) — a document-provenance
     (user, spouse, sally) row, even if extraction ever produced one, does NOT voice
     family prose and STAYS in the structured facts.
  5. THE REVERSE-GUARD VOICING SPLIT — first-person doc filings surface with the
     honest band discipline: non-kinship user-world rows (has_pet) voice in the held
     band; kinship rows stay prose-gated until chat-corroborated.
  6. THE DEFAULTS INVENTORY — every boundary flag ships ON.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

from src.api import main as main_mod
from src.api.kin_weld_repoint import kin_self_role_surface
from src.api.main import _collect_member_taxonomy_entities, _routed_fact_provenance, convert_to_prose

# -- fixtures: the owner's sentence names ------------------------------------------

USER = "552f63da-e580-4c0c-afde-d916eb35d045"      # the seat uuid IS the user entity id
TOM = str(uuid4())
SALLY = str(uuid4())
TIMMY = str(uuid4())
ISOLDE = str(uuid4())
NIMBUS = str(uuid4())
CITE = "provbound-tom-sally-timmy"

_KIN_NOUNS = frozenset({"wife", "husband", "son", "daughter", "mother", "father",
                        "sister", "brother", "spouse", "partner", "child"})


# == 1. THE ROUTER ASYMMETRY ========================================================

def test_document_source_can_never_mint_user_stated():
    """The boundary's root stamp: the router maps source='document' to llm_inferred
    UNCONDITIONALLY — a doc-lane edge may not ride its extraction-time user_stated
    stamp into Class A. mcp/assistant (the user's own chat) is the user_stated lane;
    the asymmetry IS the boundary."""
    assert _routed_fact_provenance("document", "user_stated") == "llm_inferred"
    assert _routed_fact_provenance("document", None) == "llm_inferred"
    assert _routed_fact_provenance("document", "mcp") == "llm_inferred"
    assert _routed_fact_provenance("mcp", None) == "user_stated"
    assert _routed_fact_provenance("assistant", None) == "user_stated"


# == 2. THE TAUTOLOGY GATE CONTRACT (the live drop's shape) =========================

def test_role_word_on_name_slot_refused_wherever_it_resolves():
    """kin_role_word_surface (the live drop, stage=kin_role_word_surface, seat
    552f63da): the extraction emitted an edge whose bind surface WAS a kinship role
    word — refused wherever it resolves. Both arrangements refuse."""
    assert kin_self_role_surface("wife", "tom", USER, _KIN_NOUNS) == "wife"
    assert kin_self_role_surface("Wife", "tom", USER, _KIN_NOUNS) == "wife"
    assert kin_self_role_surface("son", "timmy", USER, _KIN_NOUNS) == "son"
    # arrangement (b): the user's own role-word object — 'my wife is Wife.'
    assert kin_self_role_surface(USER, "wife", USER, _KIN_NOUNS) == "wife"


def test_genuine_name_binds_and_reaches_adjudication():
    """The gate never refuses a NAME: (tom, spouse, sally) — the third-person
    narrative bind the owner's sentence actually names — reaches the adjudication
    (where the doc-lane discipline, not this gate, decides)."""
    assert kin_self_role_surface("tom", "sally", USER, _KIN_NOUNS) is None
    assert kin_self_role_surface(TOM, SALLY, USER, _KIN_NOUNS) is None
    # inverse-copula family: 'Dana is my wife.' is a legitimate bind shape.
    assert kin_self_role_surface("dana", "wife", USER, _KIN_NOUNS) is None


# == 3. THE FAMILY-WALK CONTAINMENT =================================================

def _capturing_cursor(taxonomy_row):
    """A cursor that returns the taxonomy row, then empty sets — and RECORDS every
    (sql, params) pair so the pin can assert the subject scoping."""
    calls = []
    cursor = MagicMock()
    state = {"n": 0}

    def execute(sql, params=None):
        calls.append((" ".join(str(sql).split()), params))

    def fetchone():
        state["n"] += 1
        return taxonomy_row if state["n"] == 1 else None

    def fetchall():
        return []

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall
    db = MagicMock()
    db.cursor.return_value.__enter__.return_value = cursor
    return db, calls


def test_family_walk_never_reaches_rows_off_the_user_anchor():
    """THE CONTAINMENT (owner litmus (a), structural half): the family walk fetches
    member entities ONLY via defining rels WHERE subject_id = <the anchor>. A
    document-character row ((tom, works_for, harbour), (sally, lives_in, book)) is
    unreachable from the user anchor NO MATTER its provenance — the anchor scoping is
    the boundary, not a filter laid over it."""
    defining = ["child_of", "located_at", "parent_of", "part_of", "sibling_of", "spouse"]
    db, calls = _capturing_cursor((defining, [], False, []))
    members = _collect_member_taxonomy_entities(db, USER, "family")
    assert members == set()
    member_queries = [c for c in calls if "FROM facts" in c[0] or "FROM staged_facts" in c[0]]
    assert member_queries, "the walk never fetched defining-rel members"
    for sql, params in member_queries:
        assert "subject_id = %s" in sql, f"unscoped member fetch: {sql}"
        assert USER in (params or ()), f"member fetch not anchored on the user: {sql}"
        rel_lists = [p for p in (params or ()) if isinstance(p, (list, tuple, set))
                     and not isinstance(p, str)]
        assert rel_lists, f"member fetch carries no defining-rel scoping: {sql}"
        assert any(r in defining for r in rel_lists[0]), sql


# == 4. THE USER-ANCHORED DOC KINSHIP REPLAY (owner litmus (b)) =====================

def _patch_kinship_rail(monkeypatch):
    import src.api.linguistic_cue_overlay as overlay
    monkeypatch.setattr(overlay, "resolve_kinship_rel_map",
                        lambda dsn: {"wife": "spouse", "daughter": "child_of",
                                     "son": "child_of"})
    monkeypatch.setattr(overlay, "resolve_social_role_map", lambda dsn: {})


def _empty_cursor(mock_db):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    mock_db.cursor.return_value.__enter__.return_value = cursor
    return mock_db


_SPOUSE_META = {"natural_language": "X and Y are spouses/partners",
                "natural_language_2p": None, "tail_types": ["Person"],
                "label": "spouse", "is_symmetric": True}
_HAS_PET_META = {"natural_language": "X has pet Y", "natural_language_2p": None,
                 "tail_types": ["Animal"], "label": "has pet"}


def test_doc_provenance_user_spouse_row_stays_silent_on_family_ask(monkeypatch):
    """Owner litmus (b), belt-and-suspenders: EVEN IF extraction ever bound a
    document-derived (user, spouse, sally) row — staged B, document provenance,
    cited — it does not voice on the family ask. The row stays in the structured
    facts (a consumer reading triples still sees it; only the sentence declines).
    The chat-stated truth is the only kinship voice."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    doc_row = {"subject_id": USER, "object_id": SALLY, "rel_type": "spouse",
               "fact_class": "B", "fact_provenance": "llm_inferred",
               "source_ref": CITE}
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"spouse": _SPOUSE_META}), patch(
            "src.api.main.resolve_display_name"):
        prose = convert_to_prose([doc_row], db, anchor=USER, user_id=USER)
    assert prose == []
    assert doc_row.get("_prose_suppressed") is True


def test_chat_stated_spouse_row_voices_on_family_ask(monkeypatch):
    """The control for the replay above: the SAME pair stated in chat (user_stated,
    Class A) voices — the boundary never silences the user's own words."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    user_row = {"subject_id": USER, "object_id": SALLY, "rel_type": "spouse",
                "fact_class": "A", "fact_provenance": "user_stated"}
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"spouse": _SPOUSE_META}), patch(
            "src.api.main.resolve_display_name",
            side_effect=["you", "sally"]):
        prose = convert_to_prose([user_row], db, anchor=USER, user_id=USER)
    assert len(prose) == 1
    assert user_row.get("_prose_suppressed") is not True


def test_character_character_doc_rows_stay_silent(monkeypatch):
    """The live receipts' shape replayed with the owner's names: doc rows BETWEEN the
    narrative characters ((sally, spouse, tom)) never voice as family rows — the F4
    gate, the litmus names pinned for traceability."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    doc_row = {"subject_id": SALLY, "object_id": TOM, "rel_type": "spouse",
               "fact_class": "B", "fact_provenance": "llm_inferred",
               "source_ref": CITE}
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"spouse": _SPOUSE_META}), patch(
            "src.api.main.resolve_display_name"):
        prose = convert_to_prose([doc_row], db)
    assert prose == []
    assert doc_row.get("_prose_suppressed") is True


# == 5. THE REVERSE-GUARD VOICING SPLIT =============================================

def test_first_person_doc_pet_row_voices_in_held_band(monkeypatch):
    """REVERSE GUARD (seat 8f31fe47 receipts): a first-person document about the user
    files normally — (user, has_pet, nimbus) staged B / document / cited — and the
    non-kinship user-world row VOICES (the held-band discipline renders it; the
    kinship gate does not apply: has_pet is not a kinship-class rel)."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    pet_row = {"subject_id": USER, "object_id": NIMBUS, "rel_type": "has_pet",
               "fact_class": "B", "fact_provenance": "llm_inferred",
               "source_ref": "provbound-firstperson"}
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"has_pet": _HAS_PET_META}), patch(
            "src.api.main.resolve_display_name",
            side_effect=["you", "nimbus"]):
        prose = convert_to_prose([pet_row], db, anchor=USER, user_id=USER)
    assert len(prose) == 1
    assert pet_row.get("_prose_suppressed") is not True


def test_first_person_doc_kinship_rows_file_but_prose_gated(monkeypatch):
    """REVERSE GUARD, the kinship half: (user, spouse, isolde) and (poppy,
    child_of, user) FILE from the first-person doc (the receipts show both rows
    staged B / document / cited) — and stay prose-gated until the user states them
    in chat (the corroboration contract). Filing is normal; the VOICE waits for
    chat authority."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    _CHILD_OF_META = {"natural_language": "X is the child of Y",
                      "natural_language_2p": None, "tail_types": ["Person"],
                      "label": "is the child of"}
    spouse_row = {"subject_id": USER, "object_id": ISOLDE, "rel_type": "spouse",
                  "fact_class": "B", "fact_provenance": "llm_inferred",
                  "source_ref": "provbound-firstperson"}
    child_row = {"subject_id": str(uuid4()), "object_id": USER,
                 "rel_type": "child_of", "fact_class": "B",
                 "fact_provenance": "llm_inferred",
                 "source_ref": "provbound-firstperson"}
    with patch("src.api.main.rel_type_overlay.resolve_current",
               return_value={"spouse": _SPOUSE_META, "child_of": _CHILD_OF_META}), patch(
            "src.api.main.resolve_display_name"):
        prose = convert_to_prose([spouse_row, child_row], db, anchor=USER, user_id=USER)
    assert prose == []
    assert spouse_row.get("_prose_suppressed") is True
    assert child_row.get("_prose_suppressed") is True
    # the rows THEMSELVES are intact — filed, never deleted by the serve
    assert spouse_row["rel_type"] == "spouse"
    assert child_row["rel_type"] == "child_of"


# == 6. THE DEFAULTS INVENTORY ======================================================

def test_boundary_flags_default_on():
    """Every seam of the documented boundary ships ON; OFF arms are the legacy
    byte-parity levers (each carries its own rollback pin). The trail flag is read
    at render time — pin its default answer."""
    import src.mcp.response_types as response_types
    assert main_mod.RECALL_PROVENANCE_DOSE is True
    assert main_mod.RECALL_KINSHIP_DOC_GATE is True
    assert main_mod.RECALL_POSSESSIVE_KINSHIP is True
    assert main_mod.AXIS_SCOPED_QUERY is True
    assert response_types._document_trails() is True
