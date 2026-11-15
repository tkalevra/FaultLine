"""Fog-family lab doc-poison fixes (2026-11-07) - pins for lane-A's three seams.

Findings + wire evidence: DEV/FINDINGS-fog-family-lab-20261107.md (F2 chunk-citation
hijack, F3 document-as-family-member, F4 doc taxonomy-lane welds voiced as kinship).

Three deterministic seams, each flag-gated:

  1. RECALL_DOCUMENT_TRAIL (src/mcp/response_types.py, default ON) - document-citation
     lines TRAIL the fact bands when any fact band spoke; the migration-299 doc-only
     answer shape is unchanged; "false" restores the BUG-09 lead order byte-for-byte.
  2. RECALL_PROVENANCE_DOSE (src/api/main.py, default ON) - on the personal/kinship
     query class the composed walk result ranks user-stated rows first (stable) and
     caps document-cited (source_ref-bearing) staged rows; user rows are never capped;
     every other query class is byte-identical.
  3. RECALL_KINSHIP_DOC_GATE (src/api/main.py, default ON) - a kinship-class rel row
     whose provenance is explicitly non-user does NOT voice in prose unless the SAME
     (subject, object) pair is corroborated by a user-stated kinship row; the row stays
     in the structured facts; missing provenance voices (legacy fail-safe).

Grounding: provenance as a query-time ranking prior (arXiv 2607.22584), provenance
admissibility gated separately from relevance (arXiv 2608.10509), retrieved chunks as
grounding material ordered deliberately (Azure RAG prompt engineering), attribution
placement shapes attention (PMC13513764).
"""

from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock, patch

from src.api import main as main_mod
from src.api.main import _dose_personal_kinship_facts, convert_to_prose
from src.mcp.response_types import render, PERSONAL


# -- shared mocks (the seams convert_to_prose actually reads) ----------------------

def _empty_cursor(mock_db):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    mock_db.cursor.return_value.__enter__.return_value = cursor
    return mock_db


def _meta(**rows):
    return patch("src.api.main.rel_type_overlay.resolve_current", return_value=rows)


SPOUSE = {"natural_language": "X and Y are spouses/partners", "natural_language_2p": None,
          "tail_types": ["Person"], "label": "spouse", "is_symmetric": True}
CHILD_OF = {"natural_language": "X is the child of Y", "natural_language_2p": None,
            "tail_types": ["Person"], "label": "is the child of"}
LIKES = {"natural_language": "X likes Y", "natural_language_2p": "You like Y",
         "tail_types": ["ANY"], "label": "likes"}

_KIN_MAP = {"wife": "spouse", "daughter": "child_of", "son": "child_of"}


def _patch_kinship_rail(monkeypatch, kin=_KIN_MAP):
    monkeypatch.setattr(
        "src.api.linguistic_cue_overlay.resolve_kinship_rel_map", lambda dsn: dict(kin))
    monkeypatch.setattr(
        "src.api.linguistic_cue_overlay.resolve_social_role_map", lambda dsn: {})


# == 1. RECALL_DOCUMENT_TRAIL - render band placement ==============================

_DOCS = [
    "From your document Some Paper: Bartholomew's wife Clementine kept the lamp "
    "(document id 3, section 4).",
    "You have a document: On the Maintenance of Maritime Signal Lamps (document id 1).",
]


def test_facts_lead_documents_trail():
    """F2/F3: a user fact plus document citations - the answer leads, citations trail."""
    out = render(PERSONAL,
                 event_lines=[], assert_lines=["You are married to Seraphina."],
                 hold_lines=[], document_lines=list(_DOCS))
    assert out.index("You are married to Seraphina.") < out.index("From your document")
    assert out.index("You have a document:") > out.index("You are married to Seraphina.")


def test_documents_trail_below_held_band_too():
    """A held line is part of the answer bands; citations trail ALL of them."""
    out = render(None,
                 event_lines=[], assert_lines=["A certain fact."],
                 hold_lines=["a tentative line"],
                 document_lines=list(_DOCS))
    assert out.index("a tentative line") < out.index("From your document")


def test_doc_only_answer_still_renders_alone():
    """The migration-299 certified shape: no fact lines - the document band IS the
    answer (nothing to trail behind); never an abstention over a found passage."""
    out = render(None, event_lines=[], assert_lines=[], hold_lines=[],
                 document_lines=list(_DOCS))
    assert out.index("From your document") < out.index("You have a document")


def test_trail_flag_off_restores_legacy_lead(monkeypatch):
    """Env-off rollback lever: RECALL_DOCUMENT_TRAIL=false restores the BUG-09 lead order."""
    monkeypatch.setenv("RECALL_DOCUMENT_TRAIL", "false")
    out = render(PERSONAL,
                 event_lines=[], assert_lines=["You are married to Seraphina."],
                 hold_lines=[], document_lines=list(_DOCS))
    assert out.index("From your document") < out.index("You are married to Seraphina.")


# == 2. RECALL_PROVENANCE_DOSE - walk composition ==================================

def _f(rel="spouse", prov="llm_inferred", cls="B", cite=None):
    f = {"subject": str(uuid4()), "object": str(uuid4()),
         "rel_type": rel, "confidence": 0.6, "fact_class": cls,
         "source": "staged", "fact_provenance": prov}
    if cite is not None:
        f["source_ref"] = cite
    return f


_USER_F = _f(rel="spouse", prov="user_stated", cls="A")
_MEMBER_F = _f(rel="lives_in", prov="llm_inferred", cls="B")


def test_dose_ranks_user_stated_first_and_caps_doc_cited():
    """F2 walk half: user rows lead (stable), document-cited rows cap at 2."""
    doc_rows = [_f(rel="child_of", cite="doc:1"), _f(rel="spouse", cite="doc:1"),
                _f(rel="sibling_of", cite="doc:1")]
    facts = [doc_rows[0], dict(_MEMBER_F), doc_rows[1], dict(_USER_F), doc_rows[2]]
    out = _dose_personal_kinship_facts(facts, "tell me about my family",
                                       SimpleNamespace(axis="membership"))
    user_out = [f for f in out if f.get("fact_provenance") == "user_stated"]
    assert len(user_out) == 1
    assert out[0] is facts[3]                       # the user row FIRST (stable rank)
    assert len(out) == 4                            # 3 doc-cited rows -> 2 kept
    assert len([f for f in out if f.get("source_ref")]) == 2


def test_dose_never_caps_user_rows():
    """A user fact must still be present if it exists - user rows are never capped,
    no matter how many document-cited rows crowd the answer."""
    user_rows = [dict(_USER_F), dict(_USER_F), dict(_USER_F)]
    doc_rows = [_f(cite="doc:%d" % i) for i in range(6)]
    out = _dose_personal_kinship_facts(user_rows + doc_rows, "who is my wife?",
                                       SimpleNamespace(axis="membership"))
    assert len([f for f in out if f.get("fact_provenance") == "user_stated"]) == 3
    assert len([f for f in out if f.get("source_ref")]) == 2


def test_dose_noop_off_personal_class():
    """Every non-personal query class is byte-identical (no reorder, no cap)."""
    doc_rows = [_f(cite="doc:%d" % i) for i in range(5)]
    facts = [dict(_MEMBER_F)] + doc_rows
    out = _dose_personal_kinship_facts(list(facts), "what is espresso?",
                                       SimpleNamespace(axis=None))
    assert out == facts


def test_dose_noop_flag_off(monkeypatch):
    monkeypatch.setattr(main_mod, "RECALL_PROVENANCE_DOSE", False)
    doc_rows = [_f(cite="doc:%d" % i) for i in range(5)]
    facts = [dict(_USER_F)] + doc_rows
    out = _dose_personal_kinship_facts(list(facts), "who is my wife?",
                                       SimpleNamespace(axis="membership"))
    assert out == facts


def test_dose_class_via_possessive_predicate(monkeypatch):
    """The personal class keys on the first-person possessive grammar predicate too."""
    monkeypatch.setattr(main_mod, "_query_has_first_person_possessive", lambda q: True)
    monkeypatch.setattr(main_mod, "_query_possessed_person_role", lambda q: None)
    doc_rows = [_f(cite="doc:%d" % i) for i in range(4)]
    facts = [dict(_USER_F)] + doc_rows
    out = _dose_personal_kinship_facts(list(facts), "who is my wife?", None)
    assert len([f for f in out if f.get("source_ref")]) == 2


# == 3. RECALL_KINSHIP_DOC_GATE - kinship renderer voicing =========================

def test_doc_lane_spouse_does_not_voice_uncorroborated(monkeypatch):
    """F4 EXACT WIRE SHAPE: a staged llm_inferred spouse row between two document
    entities ('Wife and Bartholomew are spouses/partners') must NOT voice as a
    confident kinship row. The row stays in the facts - only the sentence is declined."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    wife, bart = str(uuid4()), str(uuid4())
    fact = {"subject_id": wife, "object_id": bart, "rel_type": "spouse",
            "fact_class": "B", "fact_provenance": "llm_inferred"}
    with _meta(spouse=SPOUSE), patch("src.api.main.resolve_display_name"):
        prose = convert_to_prose([fact], db)
    assert prose == []
    assert fact.get("_prose_suppressed") is True


def test_doc_lane_child_of_does_not_voice_uncorroborated(monkeypatch):
    """F4: 'Daughter is the child of Keeper' - same gate on the child_of arm."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    daughter, keeper = str(uuid4()), str(uuid4())
    fact = {"subject_id": daughter, "object_id": keeper, "rel_type": "child_of",
            "fact_class": "B", "fact_provenance": "llm_inferred"}
    with _meta(child_of=CHILD_OF), patch("src.api.main.resolve_display_name"):
        prose = convert_to_prose([fact], db)
    assert prose == []
    assert fact.get("_prose_suppressed") is True


def test_user_stated_kinship_still_voices(monkeypatch):
    """The certified family answer is untouched: a user-stated spouse row voices."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    user, seraphina = str(uuid4()), str(uuid4())
    fact = {"subject_id": user, "object_id": seraphina, "rel_type": "spouse",
            "fact_class": "A", "fact_provenance": "user_stated"}
    with _meta(spouse=SPOUSE), patch(
            "src.api.main.resolve_display_name", side_effect=["you", "seraphina"]):
        prose = convert_to_prose([fact], db, anchor=user, user_id=user)
    assert len(prose) == 1
    assert fact.get("_prose_suppressed") is not True


def test_corroborated_pair_admits_doc_twin(monkeypatch):
    """F4 spec: 'unless corroborated by a user_stated fact' - a user-stated row over
    the SAME (subject, object) pair admits the pair to voice (either direction)."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    a, b = str(uuid4()), str(uuid4())
    user_row = {"subject_id": a, "object_id": b, "rel_type": "spouse",
                "fact_class": "A", "fact_provenance": "user_stated"}
    doc_twin = {"subject_id": b, "object_id": a, "rel_type": "spouse",
                "fact_class": "B", "fact_provenance": "llm_inferred"}
    with _meta(spouse=SPOUSE), patch(
            "src.api.main.resolve_display_name",
            side_effect=["you", "nathaniel", "nathaniel", "you"]):
        prose = convert_to_prose([user_row, doc_twin], db, anchor=a, user_id=a)
    # BOTH rows voice: the gate admits the corroborated pair (the renderer's own
    # inverse-pair dedup may still collapse the SURFACE, but neither row is
    # kinship-gate suppressed - that is this pin's subject).
    assert len(prose) >= 1
    assert all("spouses/partners" in line for line in prose)
    assert user_row.get("_prose_suppressed") is not True
    assert doc_twin.get("_prose_suppressed") is not True


def test_missing_provenance_kinship_voices_legacy(monkeypatch):
    """Fail-safe: a pre-provenance legacy kinship row (no stamp at all) voices -
    the gate fires only on an EXPLICIT non-user provenance."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    a, b = str(uuid4()), str(uuid4())
    fact = {"subject_id": a, "object_id": b, "rel_type": "spouse", "fact_class": "B"}
    with _meta(spouse=SPOUSE), patch(
            "src.api.main.resolve_display_name", side_effect=["mara", "jo"]):
        prose = convert_to_prose([fact], db)
    assert len(prose) == 1


def test_gate_ignores_non_kinship_rels(monkeypatch):
    """Subject-agnostic scoping: the gate touches ONLY the kinship cue-map rel set -
    an llm_inferred likes row voices exactly as before."""
    _patch_kinship_rail(monkeypatch)
    db = _empty_cursor(MagicMock())
    a, b = str(uuid4()), str(uuid4())
    fact = {"subject_id": a, "object_id": b, "rel_type": "likes",
            "fact_class": "C", "fact_provenance": "llm_inferred"}
    with _meta(likes=LIKES), patch(
            "src.api.main.resolve_display_name", side_effect=["user", "art"]):
        prose = convert_to_prose([fact], db)
    assert len(prose) == 1


def test_gate_flag_off_restores_legacy_voicing(monkeypatch):
    """Rollback lever: RECALL_KINSHIP_DOC_GATE=false - the doc-lane row voices
    exactly as the pre-lane renderer did."""
    monkeypatch.setattr(main_mod, "RECALL_KINSHIP_DOC_GATE", False)
    db = _empty_cursor(MagicMock())
    wife, bart = str(uuid4()), str(uuid4())
    fact = {"subject_id": wife, "object_id": bart, "rel_type": "spouse",
            "fact_class": "B", "fact_provenance": "llm_inferred"}
    with _meta(spouse=SPOUSE), patch(
            "src.api.main.resolve_display_name", side_effect=["wife", "bartholomew"]):
        prose = convert_to_prose([fact], db)
    assert len(prose) == 1


def test_gate_disabled_without_resolvable_rail(monkeypatch):
    """Fail-safe: an unresolvable kinship rail (no DSN, no rows) disables the gate -
    legacy voicing - never a silent family-answer loss on a broken metadata seat."""
    def _boom(dsn):
        raise RuntimeError("no db")
    monkeypatch.setattr(
        "src.api.linguistic_cue_overlay.resolve_kinship_rel_map", _boom)
    monkeypatch.setattr(
        "src.api.linguistic_cue_overlay.resolve_social_role_map", lambda dsn: {})
    db = _empty_cursor(MagicMock())
    a, b = str(uuid4()), str(uuid4())
    fact = {"subject_id": a, "object_id": b, "rel_type": "spouse",
            "fact_class": "B", "fact_provenance": "llm_inferred"}
    with _meta(spouse=SPOUSE), patch(
            "src.api.main.resolve_display_name", side_effect=["wife", "bart"]):
        prose = convert_to_prose([fact], db)
    assert len(prose) == 1


def test_dose_with_only_document_rows_still_curates():
    """With ONLY document-cited rows and no user rows, the cap still curates a
    family answer (the doc-only passage lane serves the fuller text)."""
    doc_rows = [_f(cite="doc:%d" % i) for i in range(4)]
    out = _dose_personal_kinship_facts(list(doc_rows), "tell me about my family",
                                       SimpleNamespace(axis="membership"))
    assert len(out) == 2
