"""Fog-family critic-R2 pins (2026-11-07) - the two R1 residuals in lane-A render/dose territory.

R1 verdict residuals (live bench seat e4a72b6d):

  1. F7 (MINOR/NOTE) - the RECALL_PROVENANCE_DOSE cap-2 was NOT observable on the chunk-citation
     band: with 3 matching documents a personal query rendered THREE trailing citations, because
     the walk dial (_DOSE_DOC_CITED_ROW_CAP, main.py) governs document-cited STAGED-ROW SELECTION
     only, and nothing capped the rendered citation lines. Wire (chosen alternative): an explicit
     separate citation-band cap, RECALL_DOCUMENT_TRAIL_BAND_CAP (default 2 - render-layer twin of
     the walk dial), applied whenever the band TRAILS a fact band; the doc-only answer (nothing
     to trail behind) and the legacy lead order stay uncapped.
  2. F6 (MINOR) - display-name resolution preferred ROLE ALIAS over proper name everywhere: pet
     rendered "you have a parrot named Dog" (old alias; Nimbus is a LATER proper alias on the
     merged pet entity); corrected wife rendered "Wife" not Wren; "who is Wren?" answered "Wife
     is an instance of person". Wire: display resolution ranks PROPER-NAME surfaces above
     role/type-word alias surfaces (proper-name mention > role alias) at BOTH sanctioned
     read-time naming seams (resolve_display_name and the /query Step 6 preferred_names batch).
     The role-word class is METADATA, no word zoo: the per-tenant person-role rail (kinship /
     social_role / role_noun via linguistics._possessed_person_role_rel) plus the per-tenant
     pet-noun cue class (Lane C resolve_pet_nouns, migration 308 - "dog" is a pet noun, not a
     name). Entity-MERGE mechanics stay out (Lane B owns the kin path, Lane C the pet path).

Grounding: attribution placed above an answer captures the attention that was the answer's
(PMC13513764); retrieved chunks are ordered/capped grounding material, never the answer itself
(Azure RAG prompt engineering); provenance as a query-time ranking prior (arXiv 2607.22584).
"""

from src.api import main as main_mod
from src.api.main import (
    _rank_display_surfaces_first,
    _display_rail_word_surfaces,
    resolve_display_name,
)
from src.mcp.response_types import render
from src.mcp.server import _render_document_hit_lines


# -- F7 fixtures: a three-document match (the R1 wound shape) ----------------------

_DOC3 = [
    {"title": "Dog Manual", "document_id": 31,
     "chunk_text": "The worked example pairs a keeper with a warden.", "chunk_index": 0,
     "status": "ready"},
    {"title": "Kennel Notes", "document_id": 32,
     "chunk_text": "Second matched passage.", "chunk_index": 1, "status": "ready"},
    {"title": "Almanac", "document_id": 33,
     "chunk_text": "Third matched passage.", "chunk_index": 2, "status": "ready"},
]


def _doc_marker_count(out):
    return out.count("From your document") + out.count("You have a document:")


def test_trailing_citation_band_caps_at_default_two():
    """F7 WOUND PIN: 3 matching documents + a spoken fact band rendered THREE trailing
    citations under R1; the trailing band now obeys the band cap (default 2) - citations
    trail the answer AND never crowd it (first N by the stub lane rank order)."""
    out = render(
        None,
        event_lines=[],
        assert_lines=["Your dog is Rex."],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC3),
    )
    assert _doc_marker_count(out) == 2
    # the survivors are the BEST-RANKED pair (rank order preserved, tail dropped)
    assert "Dog Manual" in out and "Kennel Notes" in out
    assert "Almanac" not in out
    # and they still trail the answer
    assert out.index("Your dog is Rex.") < out.index("From your document")


def test_doc_only_answer_band_stays_uncapped():
    """The migration-299 DOC-ONLY answer keeps its count: with NO fact band the citations
    are the whole answer and all three render (nothing is trailed)."""
    out = render(
        None, event_lines=[], assert_lines=[], hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC3),
    )
    assert _doc_marker_count(out) == 3


def test_band_cap_dial_env_override():
    """The band cap is a DIAL: RECALL_DOCUMENT_TRAIL_BAND_CAP=1 narrows the trailing band
    to one ranked citation."""
    import os
    old = os.environ.get("RECALL_DOCUMENT_TRAIL_BAND_CAP")
    os.environ["RECALL_DOCUMENT_TRAIL_BAND_CAP"] = "1"
    try:
        out = render(
            None,
            event_lines=[],
            assert_lines=["Your dog is Rex."],
            hold_lines=[],
            document_lines=_render_document_hit_lines(_DOC3),
        )
    finally:
        if old is None:
            os.environ.pop("RECALL_DOCUMENT_TRAIL_BAND_CAP", None)
        else:
            os.environ["RECALL_DOCUMENT_TRAIL_BAND_CAP"] = old
    assert _doc_marker_count(out) == 1
    assert "Dog Manual" in out and "Kennel Notes" not in out


def test_legacy_lead_order_uncapped():
    """RECALL_DOCUMENT_TRAIL=false restores the BUG-09 lead order AND its uncapped band
    (the cap is a property of TRAILING, not of the document band per se)."""
    import os
    old_trail = os.environ.get("RECALL_DOCUMENT_TRAIL")
    os.environ["RECALL_DOCUMENT_TRAIL"] = "false"
    try:
        out = render(
            None,
            event_lines=[],
            assert_lines=["Your dog is Rex."],
            hold_lines=[],
            document_lines=_render_document_hit_lines(_DOC3),
        )
    finally:
        if old_trail is None:
            os.environ.pop("RECALL_DOCUMENT_TRAIL", None)
        else:
            os.environ["RECALL_DOCUMENT_TRAIL"] = old_trail
    assert _doc_marker_count(out) == 3
    assert out.index("From your document") < out.index("Your dog is Rex.")


# -- F6 fixtures: rail monkeypatches + a fake alias table ---------------------------


def _patch_rails(monkeypatch, roles, pets):
    _roles = {str(r).strip().lower() for r in roles}
    monkeypatch.setattr(
        "src.extraction.linguistics._possessed_person_role_rel",
        lambda s: str(s).strip().lower() in _roles)
    import src.api.linguistic_cue_overlay as _lco
    monkeypatch.setattr(_lco, "resolve_pet_nouns", lambda dsn: frozenset(pets))


def test_role_alias_loses_to_proper_name(monkeypatch):
    """THE WIFE PIN: the naming-chain role alias ("Wife", registered preferred at the
    weld) never renders while a proper-name surface ("Wren") exists on the entity."""
    _patch_rails(monkeypatch, roles={"wife"}, pets=set())
    ranked = _rank_display_surfaces_first(["Wife", "Wren"])
    assert ranked == ["Wren", "Wife"]


def test_pet_noun_alias_loses_to_later_proper_name(monkeypatch):
    """THE PET PIN: "Dog" is a pet NOUN (Lane C pet-noun cue class, migration 308) - a
    type word, not a name - so the later proper alias "Nimbus" renders."""
    _patch_rails(monkeypatch, roles=set(), pets={"dog"})
    ranked = _rank_display_surfaces_first(["Dog", "Nimbus"])
    assert ranked == ["Nimbus", "Dog"]


def test_homogeneous_proper_set_keeps_preferred_order(monkeypatch):
    """The class-homogeneous majority (all surfaces proper) keeps the legacy order
    byte-identical."""
    _patch_rails(monkeypatch, roles={"wife"}, pets={"dog"})
    ranked = _rank_display_surfaces_first(["Rex", "Nimbus"])
    assert ranked == ["Rex", "Nimbus"]


def test_homogeneous_rail_set_keeps_order(monkeypatch):
    """All-rail sets (a pure role entity) keep the legacy order - the rail word still
    renders when NO proper surface exists."""
    _patch_rails(monkeypatch, roles={"wife"}, pets={"dog"})
    ranked = _rank_display_surfaces_first(["wife", "dog"])
    assert ranked == ["wife", "dog"]


def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(main_mod, "DISPLAY_PROPER_NAME_RANK", False)
    ranked = _rank_display_surfaces_first(["Wife", "Wren"])
    assert ranked == ["Wife", "Wren"]


def test_rail_failure_fails_safe_to_legacy_order(monkeypatch):
    """An unresolvable rail classifies NOTHING - the ranking degrades to the legacy
    preferred-first order rather than guessing."""
    def _boom(_s):
        raise RuntimeError("rail down")
    monkeypatch.setattr("src.extraction.linguistics._possessed_person_role_rel", _boom)
    ranked = _rank_display_surfaces_first(["Wife", "Wren"])
    assert ranked == ["Wife", "Wren"]


class _FakeCursor:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql

    def fetchone(self):
        seq = self._db.fetchone_seq
        return seq.pop(0) if seq else None

    def fetchall(self):
        seq = self._db.fetchall_seq
        return seq.pop(0) if seq else []


class _FakeDB:
    def __init__(self, fetchall_seq=None, fetchone_seq=None):
        self.fetchall_seq = list(fetchall_seq or [])
        self.fetchone_seq = list(fetchone_seq or [])

    def cursor(self):
        return _FakeCursor(self)


_ENTITY = "11111111-2222-3333-4444-555555555555"


def test_resolve_display_name_prefers_proper_over_preferred_role(monkeypatch):
    """The per-entity resolver: full ordered read (preferred-first), proper ranked to the
    head, same retained-casing guard."""
    _patch_rails(monkeypatch, roles={"wife"}, pets=set())
    monkeypatch.setattr(main_mod, "_entity_is_l4_place", lambda db, eid: False)
    db = _FakeDB(fetchall_seq=[[("wife", "Wife"), ("wren", "Wren")]])
    assert resolve_display_name(_ENTITY, db) == "Wren"


def test_resolve_display_name_prefers_pet_proper(monkeypatch):
    _patch_rails(monkeypatch, roles=set(), pets={"dog"})
    monkeypatch.setattr(main_mod, "_entity_is_l4_place", lambda db, eid: False)
    db = _FakeDB(fetchall_seq=[[("dog", "Dog"), ("nimbus", "Nimbus")]])
    assert resolve_display_name(_ENTITY, db) == "Nimbus"


def test_resolve_display_name_homogeneous_set_legacy_answer(monkeypatch):
    """All-proper alias set: the ranked head IS the legacy preferred row - identical
    answer, single query."""
    _patch_rails(monkeypatch, roles={"wife"}, pets={"dog"})
    monkeypatch.setattr(main_mod, "_entity_is_l4_place", lambda db, eid: False)
    db = _FakeDB(fetchall_seq=[[("nimbus", "Nimbus"), ("rex", "Rex")]])
    assert resolve_display_name(_ENTITY, db) == "Nimbus"


def test_resolve_display_name_flag_off_legacy_answer(monkeypatch):
    monkeypatch.setattr(main_mod, "DISPLAY_PROPER_NAME_RANK", False)
    _patch_rails(monkeypatch, roles={"wife"}, pets=set())
    monkeypatch.setattr(main_mod, "_entity_is_l4_place", lambda db, eid: False)
    db = _FakeDB(fetchone_seq=[("wife", "Wife")])
    assert resolve_display_name(_ENTITY, db) == "Wife"


def test_resolve_display_name_no_aliases_falls_through(monkeypatch):
    """An entity with no aliases falls through to the legacy chain and returns the UUID
    (never None, never a fabricated label)."""
    _patch_rails(monkeypatch, roles={"wife"}, pets=set())
    db = _FakeDB(fetchall_seq=[[]])
    assert resolve_display_name(_ENTITY, db) == _ENTITY


def test_display_rail_word_surfaces_lowercase_matches(monkeypatch):
    _patch_rails(monkeypatch, roles={"wife"}, pets={"dog"})
    hits = _display_rail_word_surfaces(["Wife", "Dog", "Wren"])
    assert hits == frozenset({"wife", "dog"})


# -- R3 (critic R2 verdict GAPS): the band-cap SCOPE boundary -----------------------
# The critic observed a THIRD-PARTY poison query rendering 3 citations and asked which
# of two hypotheses holds: a class scope hole in the cap gate, or the doc-only path.
# CODE-PROOF: render() has exactly three fact bands (event/assert/hold) and the gate
# reads only "did ANY of them speak" -- never a query class -- so a trailing band is
# ALWAYS capped (third-party included), and an uncapped band is either the DOC-ONLY
# answer (no fact band spoke; the stub lane's migration-299 <=3-hit answer IS the whole
# answer, which includes third-party queries about entities with no stored facts) or
# the RECALL_DOCUMENT_TRAIL=false legacy lead. The observed 3-citation render is the
# doc-only path. These pins freeze both sides of the boundary.


_THIRD_PARTY_DOCS = [
    {"title": "Bartholomew Genealogy", "document_id": 41,
     "chunk_text": "Bartholomew's wife Clementine kept the court ledger.", "chunk_index": 0,
     "status": "ready"},
    {"title": "Court Rolls", "document_id": 42,
     "chunk_text": "Second matched passage.", "chunk_index": 1, "status": "ready"},
    {"title": "Almanac", "document_id": 43,
     "chunk_text": "Third matched passage.", "chunk_index": 2, "status": "ready"},
]


def test_third_party_facts_trailing_band_still_capped():
    """NO CLASS SCOPE HOLE: a third-party query whose walk DID return facts (here: a
    held-band staged row about Bartholomew) trails and caps at the dial exactly like a
    personal query -- the gate never reads a query class, only whether a fact band
    spoke."""
    out = render(
        None,
        event_lines=[],
        assert_lines=[],
        hold_lines=["Bartholomew's wife is Clementine [held: staged]"],
        document_lines=_render_document_hit_lines(_THIRD_PARTY_DOCS),
    )
    assert _doc_marker_count(out) == 2
    assert out.index("Bartholomew's wife is Clementine") < out.index("From your document")


def test_third_party_doc_only_answer_renders_full_band():
    """THE R2-B-1 OBSERVED SHAPE (boundary pin): a third-party query about an entity
    with NO stored facts speaks no fact band, so the stub lane's migration-299 answer
    renders ALONE and UNCAPPED -- all three citations, leading. This is the certified
    doc-only shape (capping it would starve the only grounded surface), not a cap scope
    hole."""
    out = render(
        None,
        event_lines=[],
        assert_lines=[],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_THIRD_PARTY_DOCS),
    )
    assert _doc_marker_count(out) == 3
    # and it is the LEADING band -- nothing spoke for it to trail behind
    assert out.startswith("From your document")


def test_hold_band_counts_as_spoken_fact_band():
    """GATE COMPLETENESS: every fact band render() has (event/assert/hold) gates the
    cap -- a held-band-only answer still trails and caps, so no lane can push an
    uncapped band under facts."""
    out = render(
        None,
        event_lines=[],
        assert_lines=[],
        hold_lines=["peng et al. created 111"],
        document_lines=_render_document_hit_lines(_DOC3),
    )
    assert _doc_marker_count(out) == 2
    assert out.index("peng et al. created 111") < out.index("From your document")


# -- R4 (critic R3 verdict GAPS): the POSSESSED-ROLE boundary shape -----------------
# "who is Osric's wife?" (anchor Osric HAS a fact; 3 matching docs) rendered 3
# UNCAPPED citations, and the anchor-vs-target question was open: whose facts does
# the gate read? LIVE DETERMINATION (critic seat e4a72b6d-..., measured 2026-11-07):
# the walk returned ONE fact -- (osric, spouse, "husband"), Class B, llm_inferred,
# doc-derived from the court poison chunk ("witness Osric's husband Anouk") -- and it
# arrived in the payload WITHOUT a definition, because the F4 kinship gate DECLINED
# its prose (explicitly non-user kinship row, pair uncorroborated: the poison defense
# working as designed). The MCP layer drops prose-less rows from every band, so the
# ANSWER band legitimately spoke nothing -> doc-only, uncapped. The gate reads the
# ANSWER's fact bands, never the raw walk result: an anchor's unrelated facts and a
# poison row that lost its prose to the kinship gate are both band-silence. Pinned as
# the possessed-role shape: band-silence + stub hits = doc-only, at any anchor state.


def test_possessed_role_prose_declined_row_is_doc_only():
    """THE R3-5 SHAPE PIN: the walked payload carries the poison kinship row, but its
    prose was declined by the kinship gate (definition-less -- exactly what the API
    returned live), so NO fact line exists for it and the document band renders alone,
    uncapped. The render gate reads fact BANDS, not the walk result: the anchor's seat
    facts (Osric has 1 elsewhere) and the suppressed row are both band-silent."""
    out = render(
        None,
        event_lines=[],
        assert_lines=[],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC3),
    )
    # identical to the doc-only answer: nothing spoke, the citations ARE the answer
    assert _doc_marker_count(out) == 3
    assert out.startswith("From your document")


def test_possessed_role_corroborated_row_trails_capped():
    """THE FLIP SIDE: when the possessed-role answer DOES speak -- a fact band with a
    real line (corroborated target, or an anchor fact that survives relevance) -- the
    band caps at the dial even with 3 matches. Band-speech, not anchor state, decides."""
    out = render(
        None,
        event_lines=[],
        assert_lines=["Osric's wife is Petra."],
        hold_lines=[],
        document_lines=_render_document_hit_lines(_DOC3),
    )
    assert _doc_marker_count(out) == 2
    assert out.index("Osric's wife is Petra.") < out.index("From your document")
