# -*- coding: utf-8 -*-
"""CONVERGENCE RESIDUAL CLOSER pins (the acceptance5→acceptance6 residual set).

THE MEASURED RESIDUALS (acceptance5, seat 19fa579d, image c27d55211d, 2026-11-15 —
tools/regression_results/convergence/results/acceptance5-report.md; store 65%, serve
92%, corrections 86% — re-measured on the kept autopsy seat 6c891e26,
results/residual-autopsy4-report.md, which also proved the DB types were CLEAN all
along: marla1='Person', fraggle='Animal'):
  1. T04 "Fraggle is Mars's morkie." type_refused — the WGM refusal checked the
     CALLER-PASSED per-mention surface guesses ('mars'→LOCATION planet heuristic,
     'fraggle'→PERSON name NER) against has_pet's declared {Person}/{Animal} while the
     STORED world-model types were correct. FIX: WGM_STORED_TYPE_PRECEDENCE — a resolved
     entity's concrete stored kind outranks the contradicting passed guess (the same
     ground-truth doctrine the confidence-bypass pre-gate already states).
  2. T02 flaked again through the OUTAGE lane (extraction_degraded,
     llm_rate_deferred): the kin-name floor ran AFTER the Stage-2 LLM draw, so a
     rate-deferred draw aborted the turn before the deterministic extraction ever ran.
     FIX: the floor is HOISTED above the draw — when it fires the LLM is never called.
  3. S05 generic-kid twin-scatter — '13-year-old son'/'13-year-old daughter' entities
     live beside the named kids forever. FIX: GENERIC_KIN_TWIN_CONSOLIDATION (a bare
     age-embedded generic twin consolidates into the unique same-age named kid at the
     moment an age lands) + REGISTRY_GENERIC_KIN_WELD (a future generic surface resolves
     straight to the kid so the twin can never re-mint).
  4. S11a/b/c shape-junk — undeclared (pet, belong_to, owner) inverse twins, a talk
     filed as a pet, a born_on on a Document. FIX: SHAPE_RECONCILIATION_SWEEP — three
     deterministic arms keyed on the tenant's own metadata at every Class-A commit.

Pure pins + stub-DB pins; no database. Fixture shapes from the litmus's own generator
(tools/regression_results/convergence/litmus_lib.py).
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "regression_results", "convergence"))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.pop("POSTGRES_DSN", None)

import src.api.main as M  # noqa: E402
import litmus_lib as L  # noqa: E402
from src.entity_registry.registry import EntityRegistry  # noqa: E402
from wgm.gate import WGMValidationGate  # noqa: E402

SEAT = "19fa579d-044e-4e43-a000-000000000006"  # acceptance5-seat-shaped (reproducible)
E = L.entity_ids(SEAT)


# ════════════════════════════════════════════════════════════════════════════════
# ordered stub — every execute() must match its expected marker, in order
# (fail-loud: an unexpected query is a pin bug, never silently empty)

class _SeqCur:
    def __init__(self, steps):
        self._steps = list(steps)   # [(marker, rows) | (marker, rows, rowcount), ...]
        self.executed = []
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if not self._steps:
            raise AssertionError(f"unexpected query #{len(self.executed)}: {sql[:120]}")
        step = self._steps.pop(0)
        marker, rows = step[0], step[1]
        if marker not in sql:
            raise AssertionError(
                f"query #{len(self.executed)} does not match marker {marker!r}: "
                f"{sql[:160]}")
        self._rows = rows
        self.rowcount = step[2] if len(step) > 2 else len(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SeqDb:
    """A db double shaped like the request connection (cursor() context manager)."""
    def __init__(self, steps):
        self.cur = _SeqCur(steps)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self.cur

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ════════════════════════════════════════════════════════════════════════════════
# FIX 1 — WGM_STORED_TYPE_PRECEDENCE: the T04 misroute class

# has_pet on the tenant: head {Person}, tail {Animal} (measured 2026-11-15,
# rel_types on the pre-prod tenant — the acceptance5 envelope refused
# subject_type=LOCATION / object_type=PERSON against exactly this ontology).
_HAS_PET = {
    "has_pet": {
        "head_types": ["Person"],
        "tail_types": ["Animal"],
        "is_hierarchy_rel": False,
        "is_symmetric": False,
        "inverse_rel_type": None,
        "category": None,
    }
}


def _t04_gate():
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    return WGMValidationGate(conn)


def _run_t04(g, subject_type="LOCATION", object_type="PERSON",
             stored_subject="Person", stored_object="Animal", hier=None):
    from unittest.mock import patch

    def _stored(eid):
        return {"mars1": stored_subject, "fraggle": stored_object}.get(str(eid))

    with patch.object(g, "get_current_ontology", return_value=_HAS_PET), \
         patch.object(g, "_resolve_entity_type", side_effect=_stored), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=hier), \
         patch.object(g, "_find_conflicting_relationships", return_value=[]), \
         patch.object(g, "_supersede_conflicting_facts", return_value=None), \
         patch.object(g, "_delete_conflicting_staged_hierarchy_facts", return_value=None):
        return g.validate_edge(
            "mars1", "fraggle", "has_pet",
            subject_type=subject_type, object_type=object_type,
            confidence=0.8, fact_provenance="user_stated",
        )


def test_stored_type_precedence_defaults_on():
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    import wgm.gate as _g
    assert _g._stored_type_precedence_on() is True


def test_t04_admits_on_stored_types_despite_surface_guesses():
    """THE pin: marla1('mars')=Person and fraggle='Animal' are STORED concrete — the
    passed planet/name surface guesses (LOCATION/PERSON) cannot refuse the pet edge."""
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    res = _run_t04(_t04_gate())
    assert res.get("status") == "valid", res


def test_t04_both_roles_covered_head_first():
    """Tail admitted on the stored Animal AND the head on the stored Person — the head
    check (the reported acceptance5 subject_type=LOCATION) admits on stored too."""
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    g = _t04_gate()
    res = _run_t04(g, subject_type="LOCATION", object_type="Animal")
    assert res.get("status") == "valid", res
    res = _run_t04(g, subject_type="Person", object_type="PERSON")
    assert res.get("status") == "valid", res


def test_stored_precedence_off_is_byte_parity_passed_type_decides():
    """WGM_STORED_TYPE_PRECEDENCE=false → the pre-fix verdict: the passed guess refuses."""
    import wgm.gate as _g
    g = _t04_gate()
    try:
        os.environ["WGM_STORED_TYPE_PRECEDENCE"] = "false"
        assert _g._stored_type_precedence_on() is False
        res = _run_t04(g, subject_type="LOCATION", object_type="PERSON")
        assert res.get("status") in ("type_refused", "type_mismatch"), res
        assert res.get("entity_type") in ("PERSON", "Location", None) or \
            "LOCATION" in str(res.get("reason")), res
    finally:
        os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)


def test_t04_survives_every_type_seam_not_just_the_loud_one():
    """THE residual-autopsy lesson: fixing only the loud refusal seam moved the verdict
    to ``type_mismatch`` (the quiet _check_type_constraints seam) — ALL THREE seams
    (the loud refusal, the constraint check, the seed withholding) must admit on the
    stored kinds. The full-shape pin runs the whole validate_edge flow."""
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    g = _t04_gate()
    res = _run_t04(g, subject_type="LOCATION", object_type="PERSON",
                   stored_subject="Person", stored_object="Animal")
    assert res.get("status") == "valid", res


def test_seed_declared_violation_checks_the_stored_kind():
    """The seed-withholding seam (the user-authored exemption's disciplinarian) checks
    the STORED world-model kind — the planet guess cannot quarantine the pet edge."""
    from unittest.mock import patch
    import wgm.gate as _g
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    g = _t04_gate()
    with patch.object(_g, "_seed_role_type_constraints",
                      return_value={"head_types": ["Person"], "tail_types": ["Animal"]}), \
         patch.object(g, "_resolve_entity_type",
                      side_effect=lambda eid: {"mars1": "Person",
                                               "fraggle": "Animal"}.get(str(eid))), \
         patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None):
        assert g._seed_declared_constraint_violation(
            "has_pet", "mars1", "fraggle", "LOCATION", "PERSON") is None
    # byte-parity lever: OFF → the passed guess decides (withholding fires)
    try:
        os.environ["WGM_STORED_TYPE_PRECEDENCE"] = "false"
        with patch.object(_g, "_seed_role_type_constraints",
                          return_value={"head_types": ["Person"], "tail_types": ["Animal"]}), \
             patch.object(g, "_resolve_entity_type",
                          side_effect=lambda eid: {"mars1": "Person",
                                                   "fraggle": "Animal"}.get(str(eid))), \
             patch.object(g, "_resolve_entity_type_via_hierarchy", return_value=None):
            assert g._seed_declared_constraint_violation(
                "has_pet", "mars1", "fraggle", "LOCATION", "Animal") == "head"
    finally:
        os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)


def test_fresh_entity_keeps_passed_type_semantics():
    """A FRESH entity (stored unknown) keeps today's behavior: the passed type decides,
    so genuinely-new mistyped extractions still refuse (the ORGANIZATION-spouse class)."""
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    g = _t04_gate()
    res = _run_t04(g, subject_type="Person", object_type="PERSON",
                   stored_subject=None, stored_object=None)
    assert res.get("status") == "type_refused", res
    assert res.get("role") == "tail", res


def test_stored_concrete_mismatch_still_refuses_honestly():
    """Precedence is not an amnesty: a stored kind that genuinely violates the
    declaration refuses — and the verdict reports the STORED kind (the world model)."""
    os.environ.pop("WGM_STORED_TYPE_PRECEDENCE", None)
    g = _t04_gate()
    res = _run_t04(g, subject_type="Person", object_type="PERSON",
                   stored_subject="Person", stored_object="Organization")
    assert res.get("status") == "type_refused", res
    assert res.get("entity_type") == "Organization", res


def test_stored_precedence_source_names_no_new_literals():
    """The precedence edit adds no rel/type word lists (the standing refusal-predicate
    source pin's discipline, extended to the new branch)."""
    src = inspect.getsource(WGMValidationGate._concrete_role_violation).lower()
    for literal in ("'morkie'", "'mars'", "'fraggle'", "'dog'", "'planet'",
                    "has_pet", "participated_in"):
        assert literal not in src, literal


# ════════════════════════════════════════════════════════════════════════════════
# FIX 2 — the kin-name floor HOIST: degradation-immune deterministic-first

def test_kin_floor_runs_before_the_llm_draw_in_correct_fact():
    """THE source-shape pin: inside correct_fact, the floor's cursor call must appear
    BEFORE the Stage-2 LLM draw, and the draw must be guarded by
    ``if not _kinname_floor_fired:`` — a rate-deferred draw can no longer abort the
    turn before the deterministic extraction ran (the residual-autopsy4 T02 wound)."""
    src = inspect.getsource(M.correct_fact)
    i_floor = src.find("_kin_name_correction_floor(req.text")
    i_draw = src.find("_unified_correction_extraction_llm(")
    assert i_floor != -1 and i_draw != -1, (i_floor, i_draw)
    assert i_floor < i_draw, "the floor must be hoisted ABOVE the LLM draw"
    i_guard = src.find("if not _kinname_floor_fired:")
    assert i_guard != -1 and i_guard < i_draw, "the draw must sit behind the floor guard"


def test_kin_floor_hoist_leaves_exactly_one_floor_block():
    """No double-run: the old post-draw floor block is gone (exactly one call site)."""
    src = inspect.getsource(M.correct_fact)
    assert src.count("_kin_name_correction_floor(req.text") == 1


def test_kin_floor_still_consumed_downstream_after_the_draw():
    """The post-commit slot-sweep / write lanes keyed on ``_kinname_floor_fired`` still
    exist downstream of the (now guarded) draw — the hoist moved the DECISION, never
    the consumption."""
    src = inspect.getsource(M.correct_fact)
    i_guard = src.find("if not _kinname_floor_fired:")
    i_consume = src.find("_kinname_floor_fired and EXTRACTION_STRONG_SUBJECT_BIND")
    assert i_consume > i_guard > 0


# ════════════════════════════════════════════════════════════════════════════════
# FIX 3a — GENERIC_KIN_TWIN_CONSOLIDATION: the generic-kid twin-scatter class

_TAX = [
    # (taxonomy_name, member_entity_types, rel_types_defining_group)
    ("pets", ["Person", "Animal"], ["has_pet"]),
    ("family", ["Person"], ["child_of", "parent_of", "sibling_of", "spouse"]),
    ("location", ["Location"], ["located_at", "part_of"]),
]


def _consolidation_steps(twin_rows, holders=(1,), slot_rows=(("child_of", True),),
                         update_rowcounts=(), kid_gender=("",)):
    steps = [
        ("FROM entity_taxonomies", _TAX),                       # kin-rel census
        ("SELECT DISTINCT rel_type FROM facts f",               # the kid's slots
         [(r[0],) for r in slot_rows]),
        ("kid_is_subject", list(slot_rows)),                    # the kid's direction
        ("count(DISTINCT a.entity_id)", [(holders[0],)]),       # unique age holder
        ("FROM entity_aliases a", twin_rows),                   # the bare twins probe
        ("attribute IN ('has_gender', 'gender')", list(kid_gender)),  # the kid's gender
    ]
    for rc in update_rowcounts:
        # per consolidated twin, the merge COMPLETES (acceptance6b): facts retirement,
        # then the twin's attr rows retire (engine-provenance only, by admission),
        # then its aliases demote (ONE live identity per person).
        steps.append(("UPDATE facts SET superseded_at", [], rc))
        steps.append(("UPDATE entity_attributes SET superseded_at", [], 0))
        steps.append(("UPDATE entity_aliases SET is_preferred", [], 0))
    return steps


def test_consolidation_fires_on_the_fixture_shape():
    """'Desmonde is 13.' landed age 13 → BOTH bare 13-embedded twins consolidate: their
    (twin, child_of, user) rows retire; the count rides the ingest supersession."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(
        twin_rows=[(E["gen-son13"], "13-year-old son"),
                   (E["gen-daughter13"], "13-year-old daughter")],
        update_rowcounts=(1, 1)))
    retired = M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13)
    assert retired == 2, retired
    assert db.commits == 1
    # the retirement is a supersession UPDATE, never a delete
    updates = [sql for sql, _p in db.cur.executed if "UPDATE facts" in sql]
    assert updates and all("superseded_at = now()" in u for u in updates)
    assert not any("DELETE FROM" in u.upper() for u in updates)
    # same direction as the kid's own edge: (twin, child_of, user) — subject=twin
    assert any(
        "subject_id = %s" in sql and p and str(p[0]) == E["gen-son13"]
        for sql, p in db.cur.executed), db.cur.executed


def test_consolidation_abstains_when_the_age_holder_is_not_unique():
    """Two same-age kids → honest abstain: no UPDATE ever executes (a wrong merge beats
    a lost twin)."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(twin_rows=[], holders=(2,)))
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13) == 0
    assert not any("UPDATE" in sql for sql, _p in db.cur.executed)


def test_consolidation_flag_off_is_byte_parity():
    M.GENERIC_KIN_TWIN_CONSOLIDATION = False
    db = _SeqDb([])
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13) == 0
    assert db.cur.executed == []


def test_consolidation_declines_junk_inputs():
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    assert M._consolidate_generic_kin_twins(None, SEAT, E["desmonde"], 13) == 0
    db = _SeqDb([])
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], None) == 0
    assert M._consolidate_generic_kin_twins(db, SEAT, "not-an-int", "thirteen") == 0
    # the kid IS the speaker → never consolidates
    assert M._consolidate_generic_kin_twins(db, SEAT, SEAT, 13) == 0
    assert db.cur.executed == []


def test_consolidation_bare_guard_lives_in_the_twins_probe():
    """The admission SQL itself excludes a twin holding any live HUMAN-AUTHORITY
    attribute (engine-provenance attrs are junk, not truth — they retire with the
    twin) or ANY live fact beyond the kin slot — a real child the user taught never
    collapses."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(twin_rows=[]))
    M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13)
    twins_sql = next(sql for sql, _p in db.cur.executed
                     if "FROM entity_aliases a" in sql)
    assert "NOT EXISTS (SELECT 1 FROM entity_attributes" in twins_sql
    assert "ax.provenance" in twins_sql  # the authority discipline, not bareness
    assert "rel_type <> ALL" in twins_sql


def test_consolidation_declines_a_non_kin_age_embedded_twin():
    """'a 20-year-old car' is a CAR, never the same-age kid: the alias must name an
    ACTIVE kinship noun on the kid's own slot rel (the cue rail's bootstrap floor)."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(
        twin_rows=[(E["gen-son13"], "20-year-old car")], holders=(1,)))
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 20) == 0
    assert not any("UPDATE" in sql for sql, _p in db.cur.executed)


def test_consolidation_abstains_on_gender_contradiction():
    """'13-year-old son' (intrinsically male, the kinship_gender rail) never
    consolidates into a kid whose live has_gender says female — honest abstain."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(
        twin_rows=[(E["gen-son13"], "13-year-old son")],
        kid_gender=("female",)))
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13) == 0
    assert not any("UPDATE" in sql for sql, _p in db.cur.executed)


def test_consolidation_gender_unknown_kin_word_never_blocks():
    """A kin noun with no intrinsic gender ('child'/'kid') consolidates regardless of
    the kid's known gender — the guard is CONTRADICTION-only."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(
        twin_rows=[(E["gen-son13"], "13-year-old child")],
        kid_gender=("female",), update_rowcounts=(1,)))
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13) == 1


# ── acceptance6b residual: THE MERGE COMPLETES (attr rows + alias collapse) ───────

def test_consolidation_completes_the_merge_attrs_and_aliases():
    """THE PIN (acceptance6b): consolidating a twin retires not just its kin EDGE —
    the twin's live entity_attributes rows supersede (engine-provenance junk, never a
    hard delete) AND its preferred aliases demote, so exactly ONE live identity (the
    named kid) remains and the serve can never voice the generic remnant again."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(
        twin_rows=[(E["gen-son13"], "13-year-old child")],
        update_rowcounts=(1,)))
    assert M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13) == 1
    executed = [sql for sql, _p in db.cur.executed]
    assert any("UPDATE entity_attributes SET superseded_at" in s
               for s in executed)  # the twin's attr rows retire
    assert any("UPDATE entity_aliases SET is_preferred = false" in s
               for s in executed)  # the alias collapse
    assert not any("DELETE FROM" in s.upper() for s in executed)  # supersede, never delete


def test_consolidation_bare_guard_admits_engine_provenance_twins_only():
    """The provenance discipline: a twin whose only live attrs are ENGINE-provenance
    still consolidates (they retire with it); the admission SQL excludes only
    HUMAN-AUTHORITY attrs (user_stated / user_correction / mcp) — a real child the
    user taught never collapses."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(twin_rows=[]))
    M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13)
    twins_sql = next(sql for sql, _p in db.cur.executed
                     if "FROM entity_aliases a" in sql)
    for _auth in ("'user_stated'", "'user_correction'", "'mcp'"):
        assert _auth in twins_sql, twins_sql


def test_registry_generic_kin_weld_declines_non_kin_surface():
    """'the 13-year-old car' — the kin-word guard declines BEFORE any DB query."""
    reg = _registry()
    cur = _SeqCur([])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 13-year-old car") is None
    assert cur.executed == []


def test_registry_generic_kin_weld_carries_the_kin_gender_guard():
    """The holder probe rides the kin noun's intrinsic gender ('son' → male) — a holder
    whose live has_gender contradicts it is excluded SQL-side (param-level pin)."""
    reg = _registry()
    tax2 = [(m, r) for (_n, m, r) in _TAX]
    cur = _SeqCur([
        ("FROM entity_taxonomies", tax2),
        ("FROM entities e", [(E["desmonde"],)]),
    ])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 13-year-old son") == E["desmonde"]
    holder_sql, holder_p = next((sql, p) for sql, p in cur.executed
                                if "FROM entities e" in sql)
    assert "has_gender" in holder_sql
    assert "male" in [str(x).lower() for x in (holder_p or [])]


def test_consolidation_kin_rels_are_metadata_computed_not_word_listed():
    """located_at (declared by the non-animate location taxonomy too) can never be a kin
    slot: the exclusively-animate-declared discipline excludes it by construction."""
    M.GENERIC_KIN_TWIN_CONSOLIDATION = True
    db = _SeqDb(_consolidation_steps(twin_rows=[]))
    M._consolidate_generic_kin_twins(db, SEAT, E["desmonde"], 13)
    slots_sql, slots_p = next((sql, p) for sql, p in db.cur.executed
                              if "SELECT DISTINCT rel_type FROM facts f" in sql)
    assert "child_of" in str(slots_p[0])
    assert "located_at" not in str(slots_p[0])
    assert "part_of" not in str(slots_p[0])


# ════════════════════════════════════════════════════════════════════════════════
# FIX 3b — REGISTRY_GENERIC_KIN_WELD: the future generic surface resolves to the kid

def _registry():
    return EntityRegistry.__new__(EntityRegistry)


_WELD_STEPS_UNIQUE = None  # superseded: the registry probe reads two-column tax rows


def test_registry_generic_kin_weld_target_unique_holder():
    import src.entity_registry.registry as R
    assert R.REGISTRY_GENERIC_KIN_WELD is True
    reg = _registry()
    # the registry probe's census reads TWO columns (member kinds + defining rels)
    tax2 = [(m, r) for (_n, m, r) in _TAX]
    cur = _SeqCur([
        ("FROM entity_taxonomies", tax2),
        ("FROM entities e", [(E["desmonde"],)]),             # the unique age holder
    ])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 13-year-old son") == E["desmonde"]


def test_registry_generic_kin_weld_requires_year_old_morphology():
    """A bare number, a name, or a breed word never triggers the weld probe."""
    reg = _registry()
    for surface in ("13", "desmonde", "morkie", "13 cats"):
        cur = _SeqCur([])
        assert reg._generic_kin_surface_target(cur, SEAT, surface) is None, surface
        assert cur.executed == [], surface


def test_registry_generic_kin_weld_ambiguous_age_abstains():
    reg = _registry()
    tax2 = [(m, r) for (_n, m, r) in _TAX]
    cur = _SeqCur([
        ("FROM entity_taxonomies", tax2),
        ("FROM entities e", [(E["desmonde"],), (E["cyrus"],)]),   # two same-age kids
    ])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 13-year-old son") is None


def test_registry_generic_kin_weld_age_range_guard():
    reg = _registry()
    cur = _SeqCur([])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 999-year-old son") is None
    assert cur.executed == []


def test_registry_generic_kin_weld_holder_must_be_named():
    """The age holder must carry an alias NOT embedding the age — a second generic
    surface is never a weld target (the named-kid requirement)."""
    reg = _registry()
    tax2 = [(m, r) for (_n, m, r) in _TAX]
    cur = _SeqCur([
        ("FROM entity_taxonomies", tax2),
        ("FROM entities e", []),          # the holder probe's named-alias guard matched none
    ])
    assert reg._generic_kin_surface_target(cur, SEAT, "the 13-year-old son") is None
    holder_sql = next(sql for sql, _p in cur.executed if "FROM entities e" in sql)
    assert "!~ " in holder_sql


def test_registry_generic_kin_weld_arm_guards_in_resolve():
    """The resolve() arm is flag-gated and skipped on force_mint_fresh (a taint-declared
    mint must mint fresh) — and fires BEFORE the alias probe (a stored generic surface
    on a consolidated twin re-points to the kid)."""
    src = inspect.getsource(EntityRegistry.resolve)
    i_arm = src.find("REGISTRY_GENERIC_KIN_WELD and not force_mint_fresh")
    i_alias = src.find("Check if it's a known alias")
    assert i_arm != -1 and i_alias != -1 and i_arm < i_alias


# ════════════════════════════════════════════════════════════════════════════════
# FIX 4 — SHAPE_RECONCILIATION_SWEEP: the S11a/b/c shape-junk class

def _shape_steps(arm_a, arm_b, arm_c, tax=_TAX):
    steps = [
        ("FROM entity_taxonomies", tax),
        ("NOT EXISTS (SELECT 1 FROM rel_types rt", arm_a),   # the undeclared twins
    ]
    for _ in arm_a:
        steps.append(("UPDATE facts SET superseded_at", [], 1))
    steps.append(("JOIN entities e ON e.id = f.object_id", arm_b))   # kind-foreign members
    for _ in arm_b:
        steps.append(("UPDATE facts SET superseded_at", [], 1))
    steps.append(("lower(a.attribute) = 'born_on'", arm_c))          # misshelved attributes
    for _ in arm_c:
        steps.append(("UPDATE entity_attributes", [], 1))
    return steps


def test_shape_sweep_retires_the_fixture_junk():
    """The acceptance5 S11 rows all retire in one sweep: the belong_to twin (inverse
    has_pet live), the talk-as-pet (an Activity), the Document's born_on."""
    M.SHAPE_RECONCILIATION_SWEEP = True
    db = _SeqDb(_shape_steps(
        arm_a=[(E["fraggle"], "belong_to", E["marla1"])],
        arm_b=[(SEAT, "has_pet", E["talk"], "Activity")],
        arm_c=[(E["doc"], "born_on", "Document")]))
    retired = M._reconcile_shape_junk(db, SEAT)
    assert retired == 3, retired
    assert db.commits == 1
    updates = [sql for sql, _p in db.cur.executed if "UPDATE" in sql]
    assert updates and all("superseded_at = now()" in u for u in updates)
    assert not any("DELETE FROM" in u.upper() for u in updates)


def test_shape_sweep_healthy_store_touches_nothing():
    M.SHAPE_RECONCILIATION_SWEEP = True
    db = _SeqDb(_shape_steps(arm_a=[], arm_b=[], arm_c=[]))
    assert M._reconcile_shape_junk(db, SEAT) == 0
    assert not any("UPDATE" in sql for sql, _p in db.cur.executed)
    assert db.commits == 0


def test_shape_sweep_flag_off_is_byte_parity():
    M.SHAPE_RECONCILIATION_SWEEP = False
    db = _SeqDb([])
    assert M._reconcile_shape_junk(db, SEAT) == 0
    assert db.cur.executed == []


def test_shape_sweep_arm_b_excludes_non_animate_declared_rels():
    """located_at is declared by the location taxonomy too — structurally excluded from
    the kind-foreign arm, so healthy placement rows can never be touched."""
    M.SHAPE_RECONCILIATION_SWEEP = True
    db = _SeqDb(_shape_steps(arm_a=[], arm_b=[], arm_c=[]))
    M._reconcile_shape_junk(db, SEAT)
    arm_b_sql, arm_b_p = next((sql, p) for sql, p in db.cur.executed
                              if "JOIN entities e ON e.id = f.object_id" in sql)
    assert "has_pet" in str(arm_b_p[0])
    assert "located_at" not in str(arm_b_p[0])
    assert "child_of" in str(arm_b_p[0])


def test_shape_sweep_arm_c_birth_kinds_floor():
    """The misshelved-attribute arm's allowed-birth-kind floor is the established
    person-shape set, and a birthable kind can never be swept (param-level pin)."""
    M.SHAPE_RECONCILIATION_SWEEP = True
    db = _SeqDb(_shape_steps(arm_a=[], arm_b=[], arm_c=[]))
    M._reconcile_shape_junk(db, SEAT)
    arm_c_sql, arm_c_p = next((sql, p) for sql, p in db.cur.executed
                              if "lower(a.attribute) = 'born_on'" in sql)
    kinds = {str(k) for k in (arm_c_p[0] or [])}
    assert kinds == {"person", "animal", "pet", "character"}


def test_shape_sweep_undeclared_arm_is_metadata_gated():
    """Arm A keys on the rel_types registry (undeclared) + the taxonomies' defining
    groups (the live inverse) — no rel-name literal anywhere in the arm SQL."""
    M.SHAPE_RECONCILIATION_SWEEP = True
    db = _SeqDb(_shape_steps(arm_a=[], arm_b=[], arm_c=[]))
    M._reconcile_shape_junk(db, SEAT)
    arm_a_sql = next(sql for sql, _p in db.cur.executed
                     if "NOT EXISTS (SELECT 1 FROM rel_types rt" in sql)
    assert "belong_to" not in arm_a_sql and "has_pet" not in arm_a_sql
    assert "rel_types_defining_group" in db.cur.executed[0][0]


def test_shape_sweep_runs_at_the_wire_commit_hook():
    """The sweep hook sits in the ingest wire right after the slot-sweep hook — the
    same post-commit moment, count riding the ingest supersession ledger."""
    src = inspect.getsource(M._ingest_impl)
    i_slot = src.find("_slot_retired = _supersede_parallel_slot_rows(")
    i_shape = src.find("_shape_retired = _reconcile_shape_junk(db, user_id)")
    assert i_slot != -1 and i_shape != -1 and i_slot < i_shape
    assert "_facts_superseded_by_ingest += _shape_retired" in src


def test_consolidation_runs_at_the_scalar_commit_hook():
    """The consolidation hook fires at the moment an age attribute lands (the T06
    scalar commit — a zero-class-A turn the wire hook never sees)."""
    src = inspect.getsource(M._ingest_impl)
    i_age = src.find('if _ss_attr == "age":')
    i_consolidate = src.find("_consolidate_generic_kin_twins(")
    assert i_age != -1 and i_consolidate != -1 and i_age < i_consolidate
    assert "_facts_superseded_by_ingest += _gkt" in src
