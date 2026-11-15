"""CORRECTION-INTEGRITY RAILS (gauntlet critic R1, lane D 2026-11-07) — pins.

Covers the three correction-integrity findings, each with its deterministic rail:

  F3a  is_interrogative_clause mis-read the FUSED RELATIVE ("..., not what I said
       before.") as a main-clause question — spaCy mis-roots the sentence on the
       relative's own verb, so the wh reaches the ROOT without a subordinate dep. The
       fused-relative guard (FREE_RELATIVE_DECLARATIVE, default ON) requires a
       sentence-initial wh OR subject-aux inversion before calling it interrogative.
       Measured live on bench 76e214a0: the whole correction routed QUERY@0.95 through
       the interrogative preroute, 2/2 rounds ("Use recall_memory for queries").

  F3b  negated_name_assertion: "My wife is not Isolde." (negated finite root +
       first-person subject + PROPN complement) is a DENIAL OF A STORED IDENTITY VALUE
       — routed CORRECTION deterministically before GLiNER2 (which scored it QUERY@0.55
       over the 0.50 gate, eating the correction 2/2). Negated CLASS complements
       ("my wife is not a vegetarian") are brand-new negative facts and stay None.

  F2   _filter_suppressed_harvest_edges: the correction turn's own old_value
       ("not Seren") re-ingested by the intent-independent harvest RESURRECTS the row
       /retract/correct just retired (store.py superseded_at CASE: unmarked user_stated
       re-ingest clears superseded_at) — the false success at the facts table. The
       correction-turn harvest drops those edges by surface identity.

  F5   _reconcile_correction_harvest_report: "Actually I moved to Ashbell." reported
       status=failed while the harvest's ingest LANDED the residence change (DB showed
       the old row superseded + Ashbell live; recall rendered Ashbell). Verify-then-
       report: the reconcile reports the verified flips; keeps the refusal (with truth
       appended) when the harvest only added rows; byte-identical when nothing landed.

Flag-OFF = pre-fix behavior is pinned per rail (monkeypatched module flags).
"""
import os

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")

from src.mcp.server import (  # noqa: E402
    _filter_suppressed_harvest_edges,
    _reconcile_correction_harvest_report,
)

linguistics = pytest.importorskip("src.extraction.linguistics")


# ── F3a: fused-relative misroot guard ────────────────────────────────────────────

FUSED_RELATIVE_CORRECTIONS = [
    "Actually my daughter Osric is 19 years old, not what I said before.",
    "Actually my daughter Lark is 17 years old, not what I said before.",
]

GENUINE_QUESTIONS = [
    "where do I live?",
    "What did I say before?",
    "What is my car's tire pressure?",
]

CERTIFIED_DECLARATIVE_SHAPES = [
    "Actually my daughter Osric is 19 years old, not 14.",
    "Actually my wife is Magnus, not Seren.",
    "Actually I moved to Ashbell.",
    "My wife is Seren.",
]


@pytest.mark.parametrize("text", FUSED_RELATIVE_CORRECTIONS)
def test_fused_relative_correction_is_declarative(text):
    """The anaphora correction ('not what I said before') is DECLARATIVE — it must reach
    the correction rails, never the interrogative QUERY preroute."""
    assert linguistics.is_interrogative_clause(text) is False


@pytest.mark.parametrize("text", GENUINE_QUESTIONS)
def test_genuine_questions_stay_interrogative(text):
    """The guard must not swallow real questions (wh-initial or inverted)."""
    assert linguistics.is_interrogative_clause(text) is True


@pytest.mark.parametrize("text", CERTIFIED_DECLARATIVE_SHAPES)
def test_certified_declarative_shapes_unchanged(text):
    """The certified correction surface keeps today's (False) verdict byte-identically."""
    assert linguistics.is_interrogative_clause(text) is False


def test_fused_relative_guard_flag_off_restores_pre_fix(monkeypatch):
    """FREE_RELATIVE_DECLARATIVE=0 -> the pre-fix verdict (True) on the fused relative."""
    monkeypatch.setattr(linguistics, "FREE_RELATIVE_DECLARATIVE", False)
    assert linguistics.is_interrogative_clause(
        "Actually my daughter Osric is 19 years old, not what I said before.") is True


# ── F3b: negated name assertion ──────────────────────────────────────────────────

def test_negated_name_assertion_denial_shape():
    got = linguistics.negated_name_assertion("My wife is not Isolde.")
    assert got is not None
    _subject, denied = got
    assert denied == "Isolde"


def test_negated_name_assertion_self_shape():
    got = linguistics.negated_name_assertion("I am not Sam.")
    assert got is not None
    _subject, denied = got
    assert denied == "Sam"


def test_negated_class_negation_is_not_a_correction():
    """A negated CLASS complement is a brand-new negative FACT — must stay None
    (lowercase class/quality words: vegetarian, red, tall)."""
    assert linguistics.negated_name_assertion(
        "My wife is not a vegetarian.") is None
    assert linguistics.negated_name_assertion("My car is not red.") is None
    assert linguistics.negated_name_assertion("My wife is not tall.") is None


def test_unseen_name_tagged_adj_is_still_a_denial():
    """The tagger is not a name signal (en_core_web_sm tags the unseen name Isolde
    ADJ/acomp — measured live): the orthographic name-surface rule must catch it."""
    got = linguistics.negated_name_assertion("My wife is not Isolde.")
    assert got is not None
    _subject, denied = got
    assert denied == "Isolde"


def test_positive_statement_is_not_a_denial():
    assert linguistics.negated_name_assertion("My wife is Seren.") is None


def test_question_is_never_a_negated_name_correction():
    assert linguistics.negated_name_assertion("Is my wife not Isolde?") is None


# ── F2: correction-turn harvest suppression ──────────────────────────────────────

EDGES = [
    {"subject": "magnus", "rel_type": "spouse", "object": "user"},
    {"subject": "seren", "rel_type": "spouse", "object": "user"},
    {"subject": "seren", "rel_type": "also_known_as", "object": "wife"},
    {"subject": "user", "rel_type": "lives_in", "object": "ashbell"},
]


def test_suppression_drops_edges_naming_the_old_value():
    kept, dropped = _filter_suppressed_harvest_edges(EDGES, ["Seren"])
    assert dropped == 2
    assert all(
        "seren" not in {str(e.get("subject", "")).lower(), str(e.get("object", "")).lower()}
        for e in kept)
    assert len(kept) == 2


def test_suppression_is_exact_surface_identity():
    """No fuzzy matching: a different surface sharing a substring is kept."""
    kept, _ = _filter_suppressed_harvest_edges(
        [{"subject": "serenade", "rel_type": "feels", "object": "joy"}], ["seren"])
    assert kept and kept[0]["subject"] == "serenade"


def test_suppression_noop_without_surfaces():
    kept, dropped = _filter_suppressed_harvest_edges(EDGES, None)
    assert dropped == 0 and len(kept) == 4


def test_suppression_handles_missing_surface_fields():
    kept, dropped = _filter_suppressed_harvest_edges(
        [{"rel_type": "spouse"}, {"subject": "seren", "rel_type": "spouse"}], ["seren"])
    assert dropped == 1 and len(kept) == 1


# ── F5: verify-then-report reconcile ─────────────────────────────────────────────

LANED_FAILED = {
    "status": "failed", "facts_superseded": 0, "old_value": None, "new_value": None,
    "old_rel_type": None, "new_rel_type": None, "subject_name": None,
    "message": "Incomplete correction — relationship type not identified",
    "isError": True,
}
LANED_CLARIFY = {
    "status": "clarification_needed", "facts_superseded": 0, "confidence": 0.0,
    "message": "You have several 'lives_in' facts and I couldn't tell which one to "
               "replace. Could you name the specific old value?",
    "isError": True,
}


def test_reconcile_reports_verified_flips_as_corrected():
    got = _reconcile_correction_harvest_report(
        dict(LANED_FAILED), {"edges": 1, "committed": 1, "superseded": 2})
    assert got["status"] == "corrected"
    assert got["facts_superseded"] == 2
    assert got["isError"] is False
    assert "ingest lane" in got["message"]


def test_reconcile_flips_refusal_status_when_only_rows_added():
    """SUPERSEDED R2-D-2 CONTRACT (R5 scope-add, critic R4 Annex A C-city): no
    supersession -> the refusal's wording stays in the rebuilt message as the CAUSE
    clause with the verified ingest-lane write as the SINGLE effect statement, but the
    STATUS no longer reports the refusal — a verified landed write is corrected_
    unresolved (in _NOT_A_FAILURE), never failed/clarification-shaped over an effect
    that actually happened. (R2-D-2's single-effect rule itself stands unchanged.)"""
    got = _reconcile_correction_harvest_report(
        dict(LANED_CLARIFY), {"edges": 1, "committed": 1, "superseded": 0})
    assert got["status"] == "corrected_unresolved"
    assert got["isError"] is False
    assert got["message"].count("DID update") == 1
    assert "Could not route the correction" in got["message"]
    assert "1 row(s) written" in got["message"]


def test_reconcile_byte_identical_when_nothing_landed():
    corr = dict(LANED_FAILED)
    got = _reconcile_correction_harvest_report(
        corr, {"edges": 0, "committed": 0, "superseded": 0})
    assert got == corr


def test_reconcile_never_touches_a_landed_write():
    """A corrected response is the correction lane's own verified transaction — the
    reconcile must return it unchanged (its counts stay its own)."""
    corr = {"status": "corrected", "facts_superseded": 1, "isError": False,
            "message": "OK user: spouse=Thessaly -> spouse=Salacia"}
    got = _reconcile_correction_harvest_report(
        corr, {"edges": 1, "committed": 1, "superseded": 3})
    assert got == corr


def test_reconcile_tolerates_non_dict_inputs():
    corr = dict(LANED_FAILED)
    assert _reconcile_correction_harvest_report(corr, None) == corr
    assert _reconcile_correction_harvest_report(None, {}) is None


def test_reconcile_tolerates_non_numeric_counts():
    got = _reconcile_correction_harvest_report(
        dict(LANED_FAILED), {"edges": 1, "committed": "two", "superseded": None})
    assert got == LANED_FAILED


# == R3 (critic R2-D-1/D-2): negated-name correction floor + single-effect message ==

import src.api.main as _api_main  # noqa: E402  (env pinned above by the module import)


class TestNegatedNameCorrectionFloor:
    """_negated_name_correction_floor - the deterministic scaffold for the shapes the R1
    rails route CORRECTION but the LLM extractor answers with NO JSON (R2:
    correction_extraction.llm_no_json 4/4). GAP-gated by the caller; these pins cover the
    pure function. Denial -> the grammatical-cessation reclassification (old rel + value,
    new None); fused-relative scalar -> the addressing seam (subject-anchored)."""

    def test_role_denial_scaffold(self):
        got = _api_main._negated_name_correction_floor("My wife is not Isolde.")
        assert got is not None
        assert got["subject_name"] == "user"
        assert got["old_rel_type"] == "spouse"
        assert got["old_value"] == "Isolde"
        assert got["new_rel_type"] is None and got["new_value"] is None
        assert got["dimension"] == "RELATIONAL"
        assert got["confidence"] >= 0.9

    def test_self_name_denial_scaffold(self):
        got = _api_main._negated_name_correction_floor("I am not Sam.")
        assert got is not None
        assert got["subject_name"] == "user"
        assert got["old_rel_type"] == "name"
        assert got["old_value"] == "Sam"
        assert got["new_value"] is None

    def test_contraction_self_denial_scaffold(self):
        got = _api_main._negated_name_correction_floor("I'm not Sam.")
        assert got is not None
        assert got["old_rel_type"] == "name"
        assert got["old_value"] == "Sam"

    def test_plural_conj_denial_names_both(self):
        got = _api_main._negated_name_correction_floor(
            "My daughters are not Seren and Wren.")
        assert got is not None
        assert got["old_rel_type"] == "child_of"
        assert "Seren" in got["old_value"] and "Wren" in got["old_value"]
        assert got["new_value"] is None

    def test_fused_relative_scalar_scaffold(self):
        got = _api_main._negated_name_correction_floor(
            "Actually my daughter Osric is 19 years old, not what I said before.")
        assert got is not None
        assert got["subject_name"] == "osric"
        assert got["new_rel_type"] == "age"
        assert got["new_value"] and "19" in str(got["new_value"])
        assert got["old_value"] is None
        assert got["dimension"] == "SCALAR"

    def test_negated_class_complement_declines(self):
        """A negated CLASS complement is a new negative FACT - never a removal scaffold."""
        assert _api_main._negated_name_correction_floor(
            "My wife is not a vegetarian.") is None

    def test_out_of_map_role_declines(self):
        """A role outside the kinship map must decline, never guess a rel."""
        assert _api_main._negated_name_correction_floor(
            "My boss is not Zedric.") is None

    def test_positive_statement_declines(self):
        assert _api_main._negated_name_correction_floor("My wife is Seren.") is None

    def test_question_declines(self):
        assert _api_main._negated_name_correction_floor("Is my wife not Isolde?") is None

    def test_fused_relative_without_measured_unit_declines(self):
        """No NUM+unit complement -> no attribute guess -> decline."""
        assert _api_main._negated_name_correction_floor(
            "Actually my daughter Osric is delightful, not what I said before.") is None


class TestReconcileSingleEffectStatement:
    """R2-D-2: the reconcile must emit EXACTLY ONE truthful effect statement - the
    rebuilt message must never carry the lane's no-change claim AND a landed claim as
    two contradicting effect statements."""

    LANED = {"status": "failed", "facts_superseded": 0, "old_value": None,
             "new_value": None, "old_rel_type": None, "new_rel_type": None,
             "subject_name": None, "isError": True,
             "message": "Incomplete correction - relationship type not identified"}

    def test_add_only_arm_rebuilds_around_single_effect(self):
        got = _reconcile_correction_harvest_report(
            dict(self.LANED), {"edges": 1, "committed": 2, "superseded": 0})
        assert got["message"].count("DID update") == 1
        assert got["message"].startswith("Could not route the correction")
        assert "2 row(s) written" in got["message"]
        assert "relationship type not identified" in got["message"]

    def test_add_only_arm_flips_status_off_failed(self):
        """R5 scope-add (critic R4 Annex A, C-city 10:03:28Z): a verified landed
        write must never REPORT as failed. The lane's refusal stays as the message's
        cause clause; the status flips to corrected_unresolved (in _NOT_A_FAILURE)
        and isError clears - the effect statement rides the lane RESULT."""
        got = _reconcile_correction_harvest_report(
            dict(self.LANED), {"edges": 2, "committed": 2, "superseded": 0})
        assert got["status"] == "corrected_unresolved"
        assert got["isError"] is False
        assert "relationship type not identified" in got["message"]
        assert got["message"].count("DID update") == 1
        # the lane's own dict is never mutated in place
        assert self.LANED["status"] == "failed" and self.LANED["isError"] is True

    def test_flip_arm_stays_corrected(self):
        got = _reconcile_correction_harvest_report(
            dict(self.LANED), {"edges": 1, "committed": 2, "superseded": 1})
        assert got["status"] == "corrected"
        assert got["facts_superseded"] == 1
        assert got["isError"] is False

    def test_nothing_landed_stays_byte_identical(self):
        corr = dict(self.LANED)
        got = _reconcile_correction_harvest_report(
            corr, {"edges": 0, "committed": 0, "superseded": 0})
        assert got == corr

    def test_flip_arm_stays_single_effect(self):
        got = _reconcile_correction_harvest_report(
            dict(self.LANED), {"edges": 1, "committed": 1, "superseded": 3})
        assert got["status"] == "corrected"
        assert got["message"].count("landed") == 1


# == R4 (critic R3-3a/3-3b): denial exact-keying + weld partition + None-crash contract ==

from src.api.main import _denial_exact_key_probe  # noqa: E402


class _FakeCur:
    """A cursor whose answers are KEYED ON THE SQL SHAPE (the fake-cursor rule: a fixed
    fetchone() silently mis-answers any NEW probe a later commit adds)."""

    def __init__(self, aliases, facts_rows):
        # aliases: {lower_alias: entity_uuid}; entity_aliases: {uuid: set(lower aliases)}
        self._alias_to_entity = {a.lower(): e for a, e in aliases.items()}
        self._entity_aliases = {}
        for a, e in aliases.items():
            self._entity_aliases.setdefault(e, set()).add(a.lower())
        # facts_rows: {(subject, object, rel): [fact_id, ...]}
        self._facts = facts_rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        sql, params = self.executed[-1]
        if sql.startswith("SELECT entity_id FROM entity_aliases WHERE lower(alias)"):
            e = self._alias_to_entity.get(str(params[0]).lower())
            return (e,) if e else None
        return None

    def fetchall(self):
        sql, params = self.executed[-1]
        if sql.startswith("SELECT lower(alias) FROM entity_aliases WHERE entity_id"):
            return [(a,) for a in sorted(self._entity_aliases.get(str(params[0]), set()))]
        if sql.startswith("SELECT id FROM facts WHERE subject_id"):
            subj, obj, rel = params
            return [(i,) for i in self._facts.get((str(subj), str(obj), str(rel)), [])]
        return []


_USER = "76e214a0-0ae9-4523-914c-f1709568bfa6"


class TestDenialExactKeyProbe:
    """R3-3b: a denial may ONLY flip rows whose object surface the turn EXACTLY names
    and whose object entity carries no off-name aliases — the denials' side of the
    lane partition (denials = named surfaces; weld-repoint = pure-weld entities)."""

    def test_exact_pure_surface_is_flip_ready(self):
        cur = _FakeCur(
            {"isolde": "ent-iso"},
            {(_USER, "ent-iso", "spouse"): [507]})
        got = _denial_exact_key_probe(cur, _USER, "spouse", ["Isolde"])
        assert got["verdict"] == "flipped_ready"
        assert got["ids"] == [(507, "ent-iso")]

    def test_named_weld_entity_is_refused_with_extra_aliases(self):
        """'seren' lives on a weld entity that ALSO carries anouk/elowen: the denial
        refuses (flipping it would retire names the turn never mentioned) and the
        report NAMES the off-name aliases."""
        cur = _FakeCur(
            {"seren": "ent-weld", "anouk": "ent-weld", "elowen": "ent-weld"},
            {(_USER, "ent-weld", "child_of"): [449]})
        got = _denial_exact_key_probe(cur, _USER, "child_of", ["Seren"])
        assert got["verdict"] == "refused_weld"
        assert got["ids"] == []
        assert "anouk" in got["detail"] and "elowen" in got["detail"]

    def test_conj_denial_all_pure_is_flip_ready(self):
        cur = _FakeCur(
            {"seren": "ent-s", "wren": "ent-w"},
            {(_USER, "ent-s", "child_of"): [701], (_USER, "ent-w", "child_of"): [702]})
        got = _denial_exact_key_probe(cur, _USER, "child_of", ["Seren", "Wren"])
        assert got["verdict"] == "flipped_ready"
        assert {i for i, _ in got["ids"]} == {701, 702}

    def test_conj_denial_with_one_welded_name_changes_nothing(self):
        """ALL-OR-NOTHING: one welded name among the conj → nothing flips."""
        cur = _FakeCur(
            {"seren": "ent-s", "wren": "ent-weld", "anouk": "ent-weld"},
            {(_USER, "ent-s", "child_of"): [701], (_USER, "ent-weld", "child_of"): [449]})
        got = _denial_exact_key_probe(cur, _USER, "child_of", ["Seren", "Wren"])
        assert got["verdict"] == "refused_weld"
        assert got["ids"] == []

    def test_unknown_name_is_a_miss_not_a_mint(self):
        """A denial of a name that resolves to nothing: lookup-only (entity_aliases
        SELECT, never a mint) → missed, nothing changes, no entity created."""
        cur = _FakeCur({}, {})
        got = _denial_exact_key_probe(cur, _USER, "spouse", ["Nohbody"])
        assert got["verdict"] == "missed"
        assert got["ids"] == []

    def test_never_writes(self):
        """The probe NEVER writes — every statement it executes is a SELECT."""
        cur = _FakeCur(
            {"isolde": "ent-iso"},
            {(_USER, "ent-iso", "spouse"): [507]})
        _denial_exact_key_probe(cur, _USER, "spouse", ["Isolde"])
        assert all(sql.lstrip().upper().startswith("SELECT")
                   for sql, _ in cur.executed)

    def test_partition_named_vs_weld(self):
        """THE PARTITION PIN: the same surface flips when pure and refuses when welded —
        denials own named pure-surface rows; weld entities belong to the repoint lane."""
        pure = _FakeCur(
            {"val": "ent-pure"},
            {(_USER, "ent-pure", "spouse"): [1]})
        assert _denial_exact_key_probe(
            pure, _USER, "spouse", ["Val"])["verdict"] == "flipped_ready"
        welded = _FakeCur(
            {"val": "ent-weld", "other": "ent-weld"},
            {(_USER, "ent-weld", "spouse"): [2]})
        assert _denial_exact_key_probe(
            welded, _USER, "spouse", ["Val"])["verdict"] == "refused_weld"


class TestFloorDenialSurfacesMarker:
    """The floor must hand the gate the turn's EXACT named surfaces."""

    def test_single_denial_surface(self):
        got = _api_main._negated_name_correction_floor("My wife is not Isolde.")
        assert got["denial_surfaces"] == ["Isolde"]

    def test_conj_denial_surfaces(self):
        got = _api_main._negated_name_correction_floor(
            "My daughters are not Seren and Wren.")
        assert set(got["denial_surfaces"]) == {"Seren", "Wren"}

    def test_scalar_arm_carries_no_denial_marker(self):
        got = _api_main._negated_name_correction_floor(
            "Actually my daughter Osric is 19 years old, not what I said before.")
        assert not got.get("denial_surfaces")

# == R4 follow-up: the "married to <NAME>" participial denial rail (live bench wound) ==
# "I am not married to Sam." used to file as a STATEMENT and INVERT: the ingest lane
# minted a positive (user, spouse, sam) row (fabricated marriage — bench row 670) and
# "…not married to Wren." re-validated the [wife]-weld row 469. The rail makes the
# negated_name_assertion detector (and therefore the CORRECTION preroute + the harvest
# suppression union) see the participial shape, and the floor scaffold it with
# denial_surfaces so the exact-key gate owns the write.

from src.extraction.linguistics import negated_name_assertion  # noqa: E402


class TestMarriedToDenialRail:
    """Negated finite copula + first-person subject + 'married to <NAME>' — the
    participial denial. Same semantics as the possessive shapes; scope bounded to
    'married to' ONLY (dating/engaged/divorced generalization is a planner decision)."""

    def test_nna_married_to_name_is_detected(self):
        """THE SAM-MINT RAIL: the detector the preroute consumes fires — the turn
        routes CORRECTION, never STATEMENT, so no positive spouse row can be minted."""
        assert negated_name_assertion("I am not married to Sam.") == ("I", "Sam")

    def test_nna_married_to_wren_shape_detected(self):
        assert negated_name_assertion("I am not married to Wren.") == ("I", "Wren")

    def test_nna_married_to_conj_names(self):
        got = negated_name_assertion("I am not married to Sam or Dana.")
        assert got is not None and got[0] == "I"
        assert {w for w in got[1].split() if w not in ("or",)} == {"Sam", "Dana"}

    def test_nna_married_to_class_pobj_stays_out(self):
        """A negated CLASS complement is a new negative FACT, not a correction."""
        assert negated_name_assertion("I am not married to a vegetarian.") is None

    def test_nna_family_scope_held(self):
        """ONE shape only: other participials stay out (planner decision to extend)."""
        assert negated_name_assertion("I am not divorced from Sam.") is None

    def test_floor_married_to_scaffold_matches_possessive(self):
        got = _api_main._negated_name_correction_floor("I am not married to Sam.")
        assert got["old_rel_type"] == "spouse"
        assert got["denial_surfaces"] == ["Sam"]
        assert got["new_value"] is None and got["new_rel_type"] is None

    def test_floor_married_to_conj_scaffold(self):
        got = _api_main._negated_name_correction_floor(
            "I am not married to Sam or Dana.")
        assert set(got["denial_surfaces"]) == {"Sam", "Dana"}

    def test_wren_weld_not_revalidated(self):
        """THE WREN-REVALIDATE RAIL: the floor hands the gate the turn's named surface;
        the exact-key probe REFUSES the [wife]-weld entity (wren is one of many aliases
        on it) and flips NOTHING — the weld row stays live for the repoint lane only."""
        cur = _FakeCur(
            {"wren": "ent-wifeweld", "wife": "ent-wifeweld", "seren": "ent-wifeweld"},
            {(_USER, "ent-wifeweld", "spouse"): [469]})
        got = _denial_exact_key_probe(cur, _USER, "spouse",
                                      _api_main._negated_name_correction_floor(
                                          "I am not married to Wren.")["denial_surfaces"])
        assert got["verdict"] == "refused_weld"
        assert got["ids"] == []
        assert "wife" in got["detail"] and "seren" in got["detail"]

    def test_sam_mint_blocked_end_to_end_scaffold(self):
        """Composition of the Sam wound: detection fires (CORRECTION preroute), the
        floor scaffolds the denial, and the gate MISSES on an absent stored marriage —
        honest failed response, zero writes, no fabricated positive row anywhere."""
        assert negated_name_assertion("I am not married to Sam.") == ("I", "Sam")
        surf = _api_main._negated_name_correction_floor(
            "I am not married to Sam.")["denial_surfaces"]
        cur = _FakeCur({}, {})
        got = _denial_exact_key_probe(cur, _USER, "spouse", surf)
        assert got["verdict"] == "missed"
        assert got["ids"] == []
        assert all(sql.lstrip().upper().startswith("SELECT")
                   for sql, _ in cur.executed)


# == R4 wedge fix: the suppression-union detector call must NEVER run on the event loop ==
# Measured wedge (critic R4 round A): on a cold MCP process the first correction turn paid
# the spaCy model load inside the union's SYNCHRONOUS detector call - on the loop. The loop
# thread blocked in the loader (PID1 D-state, __cv_timedwait_common), the /retract/correct
# 200 had already landed, nothing was logged after intent_classified, and every later call
# - even pure reads - timed out. The door was dead for the whole gauntlet.

import threading  # noqa: E402


class TestSuppressionOffLoop:
    """The correction branch's deterministic detector call is off-loop, and the loader
    lock cannot self-deadlock a re-entrant same-thread caller."""

    def test_union_detector_call_is_off_loop(self):
        """SOURCE PIN: inside the CORRECTION branch, the negated_name_assertion call is
        wrapped in asyncio.to_thread under the CORRECTION_HARVEST_SUPPRESS_OFFLOOP flag
        (default ON). A sync _nna(text) on the loop IS the wedge."""
        src_server = open(
            "/workspace/FaultLine-dev-saas/src/mcp/server.py", encoding="utf-8").read()
        marker = src_server.index("negated_name_assertion as _nna")
        body = src_server[marker:marker + 900]
        assert "if CORRECTION_HARVEST_SUPPRESS_OFFLOOP:" in body
        assert "await _asyncio_mod.to_thread(_nna, text)" in body

    def test_offloop_flag_defaults_on(self):
        from src.mcp.server import CORRECTION_HARVEST_SUPPRESS_OFFLOOP
        assert CORRECTION_HARVEST_SUPPRESS_OFFLOOP is True

    def test_loader_lock_is_reentrant(self):
        """The model loader's guard is an RLock: a same-thread re-entry during the
        (long, GIL-releasing) first load returns via the _load_attempted flag instead of
        self-deadlocking the only thread that could release the lock."""
        from src.extraction import linguistics
        assert type(linguistics._load_lock).__name__ == "RLock"


# == R5: critic R4-5 (denial inversion) + R4-4 (repair-path honesty) pins ==

import os as _os_r5
_sys_r5 = __import__("sys")
_sys_r5.path.insert(0, "/workspace/FaultLine-dev-saas")


class TestDenialTurnKeyingR5:
    """R4-5: the denial marker is keyed on the TURN (the same detector the intent
    preroute uses), not on which extraction claimed the turn first — and a
    stringified placeholder new value ('none') completes the denial instead of
    minting a junk entity and filing an AFFIRMED row."""

    def test_gate_reads_turn_detector(self):
        """SOURCE PIN: the RELATIONAL gate hook falls back to negated_name_assertion
        under CORRECTION_DENIAL_TURN_KEYING when the extraction carries no marker."""
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        marker = src_main.index("DENIAL EXACT-KEY GATE")
        body = src_main[marker:marker + 2600]
        assert "CORRECTION_DENIAL_TURN_KEYING" in body
        assert "negated_name_assertion as _nna_gate" in body
        assert "_nna_gate(req.text)" in body

    def test_none_stringified_completes_denial(self):
        """SOURCE PIN: the insert branch's completion guard treats a placeholder new
        value ('none'/'null'/'nil'/'n/a') on a denial-keyed turn as NO new value —
        the wound filed a LIVE AFFIRMED (user, spouse, none) row from it."""
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        marker = src_main.index("DENIAL COMPLETION GUARD")
        body = src_main[marker:marker + 1800]
        assert '_denial_surfaces' in body
        assert '"none", "null", "nil", "n/a"' in body

    def test_turn_keying_flag_defaults_on(self):
        from src.api.main import CORRECTION_DENIAL_TURN_KEYING
        assert CORRECTION_DENIAL_TURN_KEYING is True

    def test_detector_feeds_gate_surfaces(self):
        """The detector output splits into the exact surfaces the gate consumes."""
        from src.extraction.linguistics import negated_name_assertion
        import re
        hit = negated_name_assertion("I am not married to Wren.")
        assert hit and hit[1] == "Wren"
        surfaces = [w for w in re.split(r"[^0-9A-Za-z]+", str(hit[1]))
                    if w and w.lower() not in ("or", "and", "nor")]
        assert surfaces == ["Wren"]


class TestRepairPathHonestyR5:
    """R4-4: the refusal message admits alias-granularity side effects, and the
    zero-capture verdict acknowledges a landed type assertion (the repair step the
    refusal message itself instructs) instead of dead-ending it."""

    def test_type_refusal_message_is_alias_honest(self):
        from src.mcp.server import _type_refusal_message
        msg = _type_refusal_message([
            {"subject": "user", "rel_type": "name", "object": "Torsten",
             "entity": "torsten", "entity_type": "unknown",
             "allowed": ["Person"]}], landed=False)
        assert "not stored as facts" in msg
        assert "label" in msg
        assert "Nothing about the refused statement(s) is in memory" not in msg

    def test_zero_capture_repair_message_when_type_landed(self):
        from src.mcp.server import (_zero_capture_message,
                                    _TYPE_RECORDED_REPAIR_MESSAGE,
                                    _NOTHING_LANDED_MESSAGE)
        landed = {"committed": 0, "staged": 0, "scalar_committed": 0,
                  "facts": [{"rel_type": "instance_of", "status": "valid"}]}
        assert _zero_capture_message(landed) is _TYPE_RECORDED_REPAIR_MESSAGE
        bare = {"committed": 0, "staged": 0, "scalar_committed": 0}
        assert _zero_capture_message(bare) is _NOTHING_LANDED_MESSAGE
        assert "restate the original fact" in _TYPE_RECORDED_REPAIR_MESSAGE


# == R7: critic R5-2 (denial-retire resurrection) pins ==

import sys as _sys_r7
if "/workspace/FaultLine-dev-saas" not in _sys_r7.path:
    _sys_r7.path.insert(0, "/workspace/FaultLine-dev-saas")
from src.fact_store.store import FactStoreManager as _R7FactStore, _FACTS_INSERT_REVIVE_SQL as _R7_REVIVE_SQL  # noqa: E402


class _R7FakeCur:
    """Shape-keyed fake cursor for the F8 route decision (the fake-cursor rule:
    answers key on the SQL text, never a fixed sequence)."""

    def __init__(self, retired_probe, retired_id_row):
        self._retired_probe = retired_probe      # row for the state probe
        self._retired_id_row = retired_id_row    # (id, polarity) for the id probe
        self.executed = []                       # (sql, params)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        sql = self.executed[-1][0]
        if "ORDER BY (superseded_at IS NULL) DESC" in sql:
            return self._retired_probe
        if "SELECT id, polarity FROM facts" in sql:
            return self._retired_id_row
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # taxonomy lookup helper
    def fetchall(self):
        return []


class _R7FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def rollback(self):
        pass


class TestDenialRetireReviveGuardR7:
    """R5-2: the F8 revive CASE is for superseded-by-CORRECTION rows only. A
    denial-retired row (polarity='negated') stays dead on a positive re-statement;
    the re-statement files a FRESH row with fresh provenance. The tautology
    ('My wife is Wife.') files nothing at all."""

    _ROW = ("76e214a0-0ae9-4523-914c-f1709568bfa6", "u1", "o1", "spouse", "user_stated")

    def _run(self, retired_id_row):
        cur = _R7FakeCur((True,), retired_id_row)
        store = _R7FactStore.__new__(_R7FactStore)
        store.db_conn = _R7FakeConn(cur)
        store.commit([self._ROW], fact_class="A",
                     fact_provenance="user_stated", replay=False)
        return cur

    def test_denial_retired_revive_refused_fresh_row(self):
        cur = self._run((469, "negated"))
        sqls = [s for s, _ in cur.executed]
        assert not any("ON CONFLICT (id)" in s for s in sqls),             "the revive CASE ran on a denial-retired row — resurrection not guarded"
        inserts = [(s, p) for s, p in cur.executed
                   if s.startswith("INSERT INTO facts")]
        assert inserts, "no fresh row planted"
        assert "ON CONFLICT (subject_id, object_id, rel_type) WHERE superseded_at IS NULL" in inserts[0][0]
        # fresh provenance = the incoming statement's own (user_stated), not the dead row's
        assert "user_stated" in inserts[0][1]

    def test_affirmed_retired_still_revives(self):
        cur = self._run((469, "affirmed"))
        sqls = [s for s, _ in cur.executed]
        assert any("ON CONFLICT (id)" in s for s in sqls),             "superseded-by-correction rows must keep the shipped revive contract"

    def test_gate_retire_stamps_negated_polarity(self):
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        marker = src_main.index("DENIAL EXACT-KEY GATE (critic R3-3b; R5 turn-keyed)")
        body = src_main[marker:marker + 16000]
        assert "polarity = 'negated'" in body
        assert "DENIAL-RETIRE MARKER" in body

    def test_tautology_predicate(self):
        from src.api.main import _kin_tautology_drop
        nouns = frozenset({"wife", "husband", "mother", "father", "sister"})
        rels = {"spouse", "parent_of", "child_of", "sibling_of"}
        assert _kin_tautology_drop("Wife", "spouse", nouns, rels) is True
        assert _kin_tautology_drop("wife", "spouse", nouns, rels) is True
        assert _kin_tautology_drop("Seraphina", "spouse", nouns, rels) is False
        assert _kin_tautology_drop("mother", "likes", nouns, rels) is False
        assert _kin_tautology_drop("", "spouse", nouns, rels) is False
        assert _kin_tautology_drop("wife", "", nouns, rels) is False
        assert _kin_tautology_drop("wife", "spouse", None, rels) is False
        assert _kin_tautology_drop("wife", "spouse", nouns, None) is False

    def test_tautology_wired_into_normalizer(self):
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "INGEST_KIN_TAUTOLOGY_GUARD" in src_main
        assert "_kin_tautology_drop(" in src_main
        assert 'stage="tautology_role_surface"' in src_main

    def test_cache_echo_marks_replay(self):
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        marker = src_main.index("ingest.idempotency_cache_hit")
        body = src_main[marker:marker + 900]
        assert "cache_replay" in body
        assert "cache replay" in body


# == R8: critic RG-C (affirmation reinforces negation) + RG-E (counting field) pins ==

from src.fact_store.store import _FACTS_UPSERT_DO_UPDATE as _R8_DO_UPDATE  # noqa: E402
from src.api.models import FactCorrectionResponse as _R8_FCR  # noqa: E402


class TestAffirmationNeverBumpsNegatedR8:
    """RG-C: the natural-key BUMP path may never strengthen a negation. Doctrine
    pick (b): the DO UPDATE flips the negated twin in place — polarity -> affirmed,
    count RESTARTS at 1, provenance refreshes from the fresh affirmation."""

    def test_do_update_refuses_negated_bump(self):
        assert "WHEN facts.polarity = 'negated' THEN 1" in _R8_DO_UPDATE
        assert "WHEN facts.polarity = 'negated' THEN 'affirmed'" in _R8_DO_UPDATE
        assert "WHEN facts.polarity = 'negated' THEN EXCLUDED.provenance" in _R8_DO_UPDATE

    def test_both_upsert_routes_share_the_guard(self):
        from src.fact_store.store import (_FACTS_INSERT_LIVE_SQL as live,
                                          _FACTS_INSERT_REVIVE_SQL as revive)
        assert "WHEN facts.polarity = 'negated' THEN 1" in live
        assert "WHEN facts.polarity = 'negated' THEN 1" in revive
        assert "polarity        = CASE" in live or "polarity = CASE" in live or "polarity        = CASE" in revive

    def test_re_denial_restamp_path_intact(self):
        """The API's post-commit polarity stamp (the ONLY writer of negated class-A
        rows) is untouched - a re-denial re-stamps 'negated' after the transient flip."""
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "_class_a_polarity" in src_main
        assert "ingest.class_a_polarity_stamped" in src_main


class TestSupersessionCountingFieldR8:
    """RG-E: EVERY shape that reports a correction carries the standard-name
    supersession counter - a real supersede is never uncountable on the envelope."""

    def test_correction_response_model_carries_field(self):
        assert _R8_FCR(status="failed").facts_superseded_by_ingest == 0
        assert _R8_FCR(status="corrected",
                       facts_superseded_by_ingest=2).facts_superseded_by_ingest == 2

    def test_success_builder_populates_field(self):
        src_main = open(
            "/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        marker = src_main.index(
            'response = FactCorrectionResponse(' + chr(10) + '            status="corrected",')
        body = src_main[marker:marker + 400]
        assert "facts_superseded_by_ingest=_superseded_count" in body

    def test_reconcile_attaches_field_both_arms(self):
        from src.mcp.server import _reconcile_correction_harvest_report
        lane = {"status": "failed", "facts_superseded": 0, "isError": True,
                "message": "Incomplete correction - relationship type not identified"}
        got = _reconcile_correction_harvest_report(
            dict(lane), {"edges": 2, "committed": 2, "superseded": 0})
        assert got["facts_superseded_by_ingest"] == 0
        assert got["status"] == "corrected_unresolved"
        got2 = _reconcile_correction_harvest_report(
            dict(lane), {"edges": 1, "committed": 2, "superseded": 1})
        assert got2["facts_superseded_by_ingest"] == 1
        assert got2["status"] == "corrected"

    def test_committed_message_drops_unverifiable_negative(self):
        from src.mcp.server import _reconcile_correction_harvest_report
        lane = {"status": "failed", "facts_superseded": 0, "isError": True,
                "message": "no change"}
        got = _reconcile_correction_harvest_report(
            dict(lane), {"edges": 1, "committed": 2, "superseded": 0})
        assert "nothing superseded" not in got["message"]
        assert "2 row(s) written" in got["message"]


# == R11: critic RG-Z zombie-flush pins (backlog quarantine + age policy + single-current reconcile) ==

from src.api import ingest_transport as _r11_transport  # noqa: E402


class TestBacklogReconcileTransportR11:
    """The reconcile marker parses exactly like the replay marker (fail-safe truthy set); the
    age hint is informational and fails safe to 0.0."""

    def test_reconcile_parser_truthy_set(self):
        assert _r11_transport.backlog_reconcile_from_raw("1") is True
        assert _r11_transport.backlog_reconcile_from_raw("true") is True
        assert _r11_transport.backlog_reconcile_from_raw("yes") is True
        assert _r11_transport.backlog_reconcile_from_raw(None) is False
        assert _r11_transport.backlog_reconcile_from_raw("") is False
        assert _r11_transport.backlog_reconcile_from_raw("garbled") is False

    def test_age_parser_fail_safe(self):
        assert _r11_transport.backlog_age_from_raw(None) == 0.0
        assert _r11_transport.backlog_age_from_raw("junk") == 0.0
        assert _r11_transport.backlog_age_from_raw("-5") == 0.0
        assert _r11_transport.backlog_age_from_raw("50400") == 50400.0


class TestBacklogCurrentFresherProbeR11:
    """THE ZOMBIE-SHAPE PROBE: a different-object CURRENT row fresher than the ancient
    turn's statement moment wins the single-current slot; the ancient edge reconciles."""

    class _Cur:
        def __init__(self, row):
            self._row = row

        def execute(self, *a):
            pass

        def fetchone(self):
            return self._row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, row):
            self._row = row

        def cursor(self):
            return TestBacklogCurrentFresherProbeR11._Cur(self._row)

    def test_fresher_current_row_wins(self):
        from src.api.main import _backlog_current_fresher
        assert _backlog_current_fresher(
            self._Conn(("marble-uuid",)), "user-uuid", "spouse", "magnus-uuid", 50400.0) is True

    def test_no_current_row_lets_the_edge_land(self):
        from src.api.main import _backlog_current_fresher
        assert _backlog_current_fresher(
            self._Conn(None), "user-uuid", "spouse", "magnus-uuid", 50400.0) is False

    def test_probe_error_fails_safe_to_false(self):
        from src.api.main import _backlog_current_fresher

        class _Boom:
            def cursor(self):
                raise RuntimeError("db gone")

        assert _backlog_current_fresher(_Boom(), "u", "spouse", "m", 50400.0) is False


class TestBacklogQuarantineMechanicsR11:
    """Flap => quarantine, never mass flush; health => re-evaluate; age policy governs at POST."""

    def _src(self):
        return open("/workspace/FaultLine-dev-saas/src/mcp/server.py", encoding="utf-8").read()

    def test_worker_parks_on_degraded_marker(self):
        s = self._src()
        assert "_ingest_defer_quarantine.append((body, label, ts))" in s
        assert "ingest_defer_quarantined" in s
        assert "PARKED retry-degraded" in s

    def test_release_preserves_enqueue_order_and_ts(self):
        s = self._src()
        assert "ingest_defer_quarantine_released" in s
        assert "put_nowait((_q_body, _q_label, _q_ts))" in s

    def test_age_policy_reconcile_headers(self):
        s = self._src()
        assert "ingest_defer_ancient_reconcile" in s
        assert "_INGEST_DEFER_MAX_AGE_S" in s
        assert "BACKLOG_RECONCILE_HEADER" in s
        assert "BACKLOG_AGE_HEADER" in s

    def test_statement_cv_set_and_reset(self):
        s = self._src()
        assert "_backlog_reconcile_age_cv.set(" in s
        assert "_backlog_reconcile_age_cv.reset(_cv_tok)" in s

    def test_ingest_with_retry_merges_marker(self):
        s = self._src()
        marker = s.index("_backlog_age = _backlog_reconcile_age_cv.get()")
        replay = s.index("if attempt > 0:", marker)
        assert 0 < replay - marker < 900  # the merge sits BEFORE the replay merge

    def test_degraded_helper_failsafe(self):
        from src.mcp.server import _seat_write_lane_degraded
        # a healthy (nonexistent) seat is not degraded - the drain proceeds
        assert _seat_write_lane_degraded("00000000-0000-0000-0000-000000000000") is False

    def test_partition_c_owns_end_state_guard(self):
        """The partition: D owns backlog/age/quarantine; C's residence end-state guard
        (8b68de4e pattern) is NOT touched by this lane."""
        s = self._src()
        assert "_ingest_defer_quarantine" in s
        main_src = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "_backlog_current_fresher" in main_src


class TestZombieShapeDisclosedR11:
    """The backend guard + the honest reconciled-from-backlog disclosure."""

    def test_guard_source_pins(self):
        s = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "ingest.backlog_reconcile_current_wins" in s
        assert '_t_pol != "negated"' in s
        assert "is_single_valued" in s
        assert "_rel_is_mutable_current_residence(_t_rel_l)" in s
        assert "_backlog_reconciled += 1" in s

    def test_response_discloses_reconciled(self):
        s = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert '"backlog_reconciled": _backlog_reconciled' in s

    def test_nonlocal_discipline_held(self):
        s = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "nonlocal _facts_superseded_by_ingest, _backlog_reconciled" in s

# == R12: critic RG-Z-2 FILED-ROW SWEEP pins (ghost self-heal at drain time) ==


class TestGhostSweepR12:
    def _src(self):
        return open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()

    @staticmethod
    def _fake_db():
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        ancient = now - timedelta(hours=2)

        class Cur:
            def __init__(self, conn):
                self._c = conn
                self.rowcount = 0

            def execute(self, sql, params=None):
                s = str(sql).strip()
                self._c.executed.append((s, params))
                if "GROUP BY subject_id, rel_type" in s:
                    self._c.next_fetch = list(self._c.slot_rows)
                elif "ORDER BY updated_at DESC" in s and params:
                    self._c.next_fetch = list(
                        self._c.rows_by_slot.get((params[0], params[1]), []))
                elif "UPDATE facts SET superseded_at" in s:
                    self._c.updates.append(params[0])
                    self.rowcount = 1
                else:
                    self._c.next_fetch = []

            def fetchall(self):
                return self._c.next_fetch

            def fetchone(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def __init__(self, slot_rows, rows_by_slot):
                self.slot_rows = slot_rows
                self.rows_by_slot = rows_by_slot
                self.executed = []
                self.updates = []
                self.next_fetch = []
                self.committed = False

            def cursor(self):
                return Cur(self)

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

        return Conn, now, ancient

    @staticmethod
    def _run_sweep(monkeypatch, slot_rows, rows_by_slot):
        from src.api import main as M
        M._backlog_sweep_last_run.clear()
        monkeypatch.setattr(M, "_rel_meta",
                           lambda rel=None: {"spouse": {"is_single_valued": True}}.get(rel, {}))
        Conn, now, ancient = TestGhostSweepR12._fake_db()
        db = Conn(slot_rows, rows_by_slot)
        out = M._backlog_ghost_sweep(db, "user-uuid-1234", force=True)
        return M, db, out

    def test_ancient_loser_retires_winner_stays(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        M, db, out = self._run_sweep(
            monkeypatch,
            [("sub-uuid", "spouse", 2)],
            {("sub-uuid", "spouse"): [
                ("row-winner", "marble-uuid", datetime.now(timezone.utc)),
                ("row-loser", "magnus-uuid", datetime.now(timezone.utc) - timedelta(hours=2)),
            ]})
        assert out["ran"] is True and out["retired"] == 1 and out["kept"] == 0
        assert db.updates == ["row-loser"]  # the WINNER is never touched
        assert db.committed is True

    def test_fresher_competitor_shape_kept(self, monkeypatch):
        from datetime import datetime, timezone
        M, db, out = self._run_sweep(
            monkeypatch,
            [("sub-uuid", "spouse", 2)],
            {("sub-uuid", "spouse"): [
                ("row-winner", "marble-uuid", datetime.now(timezone.utc)),
                ("row-young", "wulfrun-uuid", datetime.now(timezone.utc)),
            ]})
        assert out["retired"] == 0 and out["kept"] == 1 and db.updates == []

    def test_no_zero_live_invariant_by_construction(self):
        s = self._src()
        assert "_winner = _rows[0]" in s
        assert "for _loser in _rows[1:]" in s  # the freshest row is never a candidate

    def test_non_family_rel_skipped(self, monkeypatch):
        from datetime import datetime, timezone
        M, db, out = self._run_sweep(
            monkeypatch,
            [("sub-uuid", "related_to", 3)],
            {("sub-uuid", "related_to"): [
                ("r1", "o1", datetime.now(timezone.utc)),
                ("r2", "o2", datetime.now(timezone.utc)),
                ("r3", "o3", datetime.now(timezone.utc)),
            ]})
        assert out["retired"] == 0 and out["slots"] == 0 and db.updates == []

    def test_throttled_per_schema(self, monkeypatch):
        from src.api import main as M
        from src.api.main import _backlog_ghost_sweep
        M._backlog_sweep_last_run.clear()
        monkeypatch.setattr(M, "_rel_meta", lambda rel=None: {})

        class EmptyCur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass

            def fetchall(self):
                return []

        class EmptyConn:
            def cursor(self):
                return EmptyCur()

            def commit(self):
                pass

        out1 = _backlog_ghost_sweep(EmptyConn(), "u")
        out2 = _backlog_ghost_sweep(EmptyConn(), "u")
        assert out1.get("ran") is True
        assert out2.get("ran") is False and out2.get("reason") == "throttled"

    def test_drain_time_home_documented(self):
        s = self._src()
        assert "NATURAL HOME = DRAIN TIME" in s
        assert "_backlog_ghost_sweep" in s

    def test_disclosure_and_stats(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        M, db, out = self._run_sweep(
            monkeypatch,
            [("sub-uuid", "spouse", 2)],
            {("sub-uuid", "spouse"): [
                ("row-winner", "marble-uuid", datetime.now(timezone.utc)),
                ("row-loser", "magnus-uuid", datetime.now(timezone.utc) - timedelta(hours=2)),
            ]})
        assert M._backlog_sweep_stats["retired"] >= 1
        s = self._src()
        assert "ingest.backlog_ghost_retired" in s
        assert "ingest.backlog_ghost_sweep" in s

    def test_partition_c_end_state_untouched(self):
        s = self._src()
        assert "_rel_is_mutable_current_residence" in s  # shared family predicate, composing


# == R13: critic RG-Z-2 API-twin pins (the reextract drain joins the discipline) ==


class TestReextractDrainDisciplineR13:
    """The R12b flush came from the REEXTRACT drain (the API twin of the MCP deferred
    queue): R1-era retained turns re-mined during a flap filed LIVE with no age policy,
    no quarantine, no reconcile marker. This lane now carries the same discipline."""

    def _src(self):
        return open("/workspace/FaultLine-dev-saas/src/re_embedder/embedder.py", encoding="utf-8").read()

    def test_age_policy_source(self):
        s = self._src()
        assert "_REEXTRACT_MAX_AGE_S" in s
        assert "REEXTRACT_MAX_AGE_S" in s
        assert "reextract_ancient_reconcile" in s
        assert "BACKLOG_RECONCILE_HEADER" in s and "BACKLOG_AGE_HEADER" in s

    def test_headers_merged_into_post(self):
        s = self._src()
        assert "headers=_rx_headers" in s
        assert "_rx_headers[_ingest_transport.BACKLOG_RECONCILE_HEADER]" in s

    def test_flap_quarantine_parks_batch(self):
        s = self._src()
        assert "reextract_quarantined_degraded" in s
        assert "_seat_lane_degraded(user_id)" in s
        assert "PARKED" in s  # park, never eat: un-stamped rows retry next cycle

    def test_degraded_probe_failsafe(self):
        from src.re_embedder.embedder import _seat_lane_degraded
        # a healthy (nonexistent) seat is not degraded - the drain proceeds
        assert _seat_lane_degraded("00000000-0000-0000-0000-000000000000") is False

    def test_arbitration_chain_closed(self):
        # The marker the reextract drain emits is the SAME header the backend landing
        # loop arbitrates on (R11) and the drain-time sweep keys on (R12): chain closed.
        emb = self._src()
        main_src = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "BACKLOG_RECONCILE_HEADER" in emb
        assert "_backlog_reconcile = _ingest_transport.backlog_reconcile_from_raw" in main_src
        assert "if ('_backlog_reconcile' in locals() and _backlog_reconcile" in main_src  # the sweep fires on marked posts

    def test_sweep_throttle_window_documented(self):
        main_src = open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()
        assert "_BACKLOG_SWEEP_MIN_INTERVAL_S" in main_src
        assert "throttled" in main_src

    def test_doc_lane_boundary_deliberate(self):
        # Boundary note: the DOC-lane chunk commit (replay=True re-mines) stays
        # replay-marker-only BY DESIGN - a document re-mine is a deliberate same-content
        # re-send, and the R12b zombie wound came from the EPISODIC lane, not the doc lane.
        s = self._src()
        marker = s.index("headers=_rx_headers")
        doc_site = s.index("_ingest_headers = {")
        assert marker > 0 and doc_site > 0


# == R14: critic RG-Z-4 re-mine provenance gate pins (invented-place withhold) ==


class TestRemineProvenanceGateR14:
    """R13's battery_a zombies (mirefoot/hollowmere/rainhollow, 17:17:41) were hallucinated
    by a degraded brain at RE-EXTRACT time - places named in NO user statement. The wound
    is a TRUST gate, not a transport gate: a re-mine's fresh-minted place surface that
    appears nowhere in the retained turn's own text must withhold, never live-current."""

    def _src(self):
        return open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()

    @staticmethod
    def _fake_db(fetchone, raiser=False):
        class Cur:
            def __init__(self, conn):
                self._c = conn

            def execute(self, sql, params=None):
                if raiser:
                    raise RuntimeError("probe fault")
                assert "FROM entities e" in str(sql)

            def fetchone(self):
                return self._c.fetchone

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def __init__(self, fetchone):
                self.fetchone = fetchone

            def cursor(self):
                return Cur(self)

        return Conn(fetchone)

    def test_real_reextract_passes(self):
        from src.api import main as M
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # fresh mint, alias IS named in the retained turn: a real re-extraction
        db = self._fake_db((now - timedelta(seconds=30), ["kelpie brine"]))
        assert M._remine_object_invented(
            db, "obj-uuid", now, "I live at Kelpie Brine these days") is False

    def test_invented_place_withholds(self):
        from src.api import main as M
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # fresh mint, alias named NOWHERE in the retained turn: invented
        db = self._fake_db((now - timedelta(seconds=30), ["mirefoot"]))
        assert M._remine_object_invented(
            db, "obj-uuid", now, "I live at Kelpie Brine these days") is True

    def test_old_mint_passes(self):
        from src.api import main as M
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        # minted by an earlier user statement era: a real prior name - pass
        db = self._fake_db((now - timedelta(days=3), ["kelpie brine"]))
        assert M._remine_object_invented(
            db, "obj-uuid", now, "totally unrelated text") is False

    def test_missing_entity_withholds(self):
        from src.api import main as M
        from datetime import datetime, timezone
        db = self._fake_db(None)  # no entity row at all: the surface never existed
        assert M._remine_object_invented(
            db, "obj-uuid", datetime.now(timezone.utc), "text") is True

    def test_probe_failure_fails_toward_data(self):
        from src.api import main as M
        from datetime import datetime, timezone
        db = self._fake_db(None, raiser=True)
        assert M._remine_object_invented(
            db, "obj-uuid", datetime.now(timezone.utc), "text") is False

    def test_gate_shape_source(self):
        s = self._src()
        assert "_replay_lane == \"reextract\"" in s  # keys on the re-mine lane hint
        assert "_remine_object_invented(" in s
        assert "ingest.remine_invented_place_withheld" in s
        assert "_rel_is_mutable_current_residence(_t_rel_l)" in s  # residence family
        assert "_t_pol != \"negated\"" in s  # negated edges exempt, as everywhere

    def test_withhold_is_disclosed_not_silent(self):
        s = self._src()
        assert "remine_withheld" in s  # counter + response field
        assert "_remine_withheld += 1" in s
        assert "\"remine_withheld\": _remine_withheld" in s

    def test_fail_safe_never_eats_user_facts(self):
        s = self._src()
        assert "probe failure must never drop a real fact" in s
        assert "_REMINE_MINT_SKEW_S" in s  # the re-mine extraction-roundtrip window

    def test_flap_timing_honest(self):
        # The R13 quarantine stays advisory-keyed BY DESIGN; the invented names prove the
        # brain was degraded regardless of whether the advisory probe caught the window.
        emb = open("/workspace/FaultLine-dev-saas/src/re_embedder/embedder.py", encoding="utf-8").read()
        assert "_seat_lane_degraded(user_id)" in emb  # unchanged advisory-keyed park
        assert "Fail-safe False" in emb  # quarantine never eats data


# == R14 ADDENDUM pins (lane C's live trace, routed to the correction rails) ==


class TestTwinRetireTurnWindowR14A:
    """The scalar-slot twin retire swept live residence REL rows with NO age window and
    killed a same-turn destination row 112ms after insert (1043 candler's rest) - the
    correction ended zero-live and the end-state restore resurrected the OLD city. The
    INGEST_SELF_SUPERSEDE_MIN_AGE_SEC turn-window contract now governs the sweep."""

    def _src(self):
        return open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()

    def test_twin_retire_respects_turn_window(self):
        s = self._src()
        seg = s.split("def _retire_scalar_slot_relational_twins")[1].split("\ndef ")[0]
        assert "INGEST_SELF_SUPERSEDE_MIN_AGE_SEC" in seg
        assert "created_at < now() - make_interval(secs => %s)" in seg

    def test_never_kills_same_turn_destination(self):
        # the window predicate is on BOTH stores (facts.created_at, staged.first_seen_at)
        seg = self._src().split("def _retire_scalar_slot_relational_twins")[1].split("\ndef ")[0]
        assert "first_seen_at < now() - make_interval(secs => %s)" in seg
        assert "superseded_at IS NULL" in seg and "deleted_at IS NULL" in seg

    def test_window_contract_is_the_shared_env(self):
        # the SAME env knob the rel-lane executor and the F4 attr guard read
        assert self._src().count("INGEST_SELF_SUPERSEDE_MIN_AGE_SEC") >= 4


class TestHeldWriteDisclosureR14A:
    """The F4/SS5 authority hold's refusal was invisible to callers - a held write
    reported as a silent zero (the F2 false-success class). The response now carries
    scalar_gate_held and the MCP surfaces it as held, never committed."""

    def _api(self):
        return open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()

    def _mcp(self):
        return open("/workspace/FaultLine-dev-saas/src/mcp/server.py", encoding="utf-8").read()

    def test_response_carries_held_field(self):
        s = self._api()
        assert '"scalar_gate_held": _scalar_gate_held' in s
        assert s.count("_scalar_gate_held += 1") == 2  # BOTH hold sites (SS5 + F4)
        assert "ingest.scalar_supersede_held_by_authority" in s
        assert "ingest.scalar_retire_held_by_authority" in s

    def test_held_never_counts_as_landed(self):
        s = self._api()
        # the landed predicate stays committed/staged/scalar_committed - held is absent
        assert "committed > 0 or staged > 0 or scalar_committed > 0" in s

    def test_mcp_surfaces_held_as_held(self):
        s = self._mcp()
        assert '"scalar_held": int(ingest_result.get("scalar_gate_held", 0) or 0)' in s
        assert "reported as held, not committed" in s
        assert "HELD by the authority gate" in s

    def test_committed_keys_untouched(self):
        assert ('_INGEST_COUNT_KEYS = ("committed", "staged", "scalar_committed")'
                in self._mcp())


# == R14 HOTFIX pins: the re-mine gate's entry clock (B's live-probe catch) ==


class TestIngestClockRealConnR14H:
    """The R14 gate's clock one-liner used datetime.now(timezone) WITHOUT .utc - TypeError
    'tzinfo argument must be None or of a tzinfo subclass, not type type' at HANDLER ENTRY,
    crashing every real /ingest. SQL-marker fake conns never reach handler entry, so unit
    stayed green while the live tree was broken. These pins exercise the clock on a
    real-conn-shaped path (the real handler + a genuine starlette request) so the class
    dies in unit too."""

    def _main_src(self):
        return open("/workspace/FaultLine-dev-saas/src/api/main.py", encoding="utf-8").read()

    def test_clock_source_uses_tzinfo_instance(self):
        import re
        s = self._main_src()
        m = re.search(r"_req_t0 = (_r14_dt\.now\([^)]*\))", s)
        assert m, "the entry clock line is gone"
        assert ".utc" in m.group(1), m.group(1)
        assert "_r14_dt.now(_r14_tz)" not in s  # the bare-class shape is banned verbatim

    def test_datetime_now_bare_class_is_the_wound(self):
        # the failure shape, pinned: the tzinfo CLASS is not a tzinfo INSTANCE
        from datetime import datetime, timezone
        try:
            datetime.now(timezone)
            raise AssertionError("datetime.now(timezone) unexpectedly succeeded")
        except TypeError as e:
            assert "tzinfo" in str(e)
        _ok = datetime.now(timezone.utc)
        assert _ok.tzinfo is not None

    def test_real_handler_entry_never_typeerrors_on_clock(self, monkeypatch):
        import asyncio
        from datetime import datetime, timezone
        from src.api import main as M
        from src.api.models import IngestRequest
        from starlette.requests import Request as _SReq
        monkeypatch.setattr(M, "_INGEST_ENABLED", True)
        monkeypatch.setattr(M, "paused_for_background", lambda: False)
        _scope = {"type": "http", "method": "POST", "path": "/ingest",
                  "headers": [(b"x-fl-replay-lane", b"reextract")],
                  "query_string": b"", "server": ("test", 80), "scheme": "http"}
        _starl = _SReq(_scope)  # genuine request object: header reads run the real path
        _req = IngestRequest(text="I live at Kelpie Brine.",
                             user_id="00000000-0000-0000-0000-000000000000")
        _err = None
        try:
            asyncio.run(M._ingest_impl(_req, _starl, None))
        except BaseException as e:  # noqa: BLE001 - downstream no-DB failure is expected
            _err = e
        # The ONE assertion that matters: the entry clock never raised the tzinfo-class
        # TypeError. Any OTHER downstream failure (no DB in unit) is acceptable.
        assert not (_err is not None and "tzinfo argument" in str(_err)), repr(_err)[:200]
