# -*- coding: utf-8 -*-
"""CORRECTION_REPLACE_NOT_PARALLEL pins (convergence litmus, parallel-truth class).

THE MEASURED WOUND (live litmus autopsy seat 3e7b2cc4, image b9161e04, 2026-11-14 —
tools/regression_results/convergence/results/autopsy-report.md): corrections minted
correct rows BESIDE the planted wrong ones and never superseded them; the litmus
fixture minted a PARALLEL USER ENTITY (uuid5 off the seat) beside the seat-uuid one so
entity-linking by id never touched the planted rows; "Desmond is 13." landed
attribute=has_value beside the stale age=12 (the SEAM-1 non-animate retype); and a
negated twin served beside its affirmed counterpart.

THE FIX, flag CORRECTION_REPLACE_NOT_PARALLEL default ON (OFF = byte-parity):
  1. ``_supersede_parallel_slot_rows`` / ``_slot_supersession_plan`` — generalized slot
     supersession on the post-commit sweep (subject side for is_single_valued slots,
     object side for taxonomy-defining rels; metadata-driven, no rel-name literals);
  2. ``_seam1_age_shelf`` — prior-slot precedence for the bare-number age guard;
  3. ``_disengage_negated_twins`` — the affirmation wins the serve on a shared natural
     key; the negated twin renders only on archive/before asks;
  4. litmus_lib.entity_ids — the user entity binds to the SEAT UUID (the twin-mint is
     dead at its actual mint path).

Pure pins + a stub-DB executor pin; no database. Fixture shapes reused from the
litmus's own generator (tools/regression_results/convergence/litmus_lib.py).
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "regression_results", "convergence"))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.pop("POSTGRES_DSN", None)

import src.api.main as M  # noqa: E402
import litmus_lib as L  # noqa: E402


SEAT = "3e7b2cc4-a305-459a-bbe9-e44e824f468b"  # the autopsy seat (reproducible shapes)

# rel metadata as MEASURED on the litmus seat's rel_types/entity_taxonomies
REL_META = {
    "spouse":  {"correction_behavior": "supersede", "is_single_valued": True},
    "has_pet": {"correction_behavior": "supersede", "is_single_valued": False},
    "child_of": {"correction_behavior": "immutable", "is_single_valued": False},
    "likes":   {"correction_behavior": "supersede", "is_single_valued": False},
}
DEFINING = {"has_pet", "located_at", "member_of", "part_of"}  # entity_taxonomies probe


# ── class 2: the twin-mint is dead — the user entity binds to the SEAT UUID ──────

def test_user_entity_binds_to_seat_uuid():
    """THE PIN: the fixture's own mint path (entity_ids) returns the seat for 'user'."""
    E = L.entity_ids(SEAT)
    assert E["user"] == SEAT


def test_fixture_plants_user_rows_on_the_seat_anchor():
    """The generated fixture SQL carries the seat id as the user row id — no uuid5
    twin beside the provisioning-created seat-uuid user entity."""
    sql, E = L.fixture_sql(SEAT, "faultline_%s" % SEAT.replace("-", "_"))
    assert f"('{SEAT}', 'Person')" in sql
    assert "convlitmus:user" not in sql  # the uuid5 twin mint is dead


def test_store_checks_key_the_user_at_the_seat_anchor():
    checks = L.store_checks(SEAT, "schema", L.entity_ids(SEAT))
    s01 = next(c for c in checks if c[0] == "S01-spouse-marla-live")
    assert f"'{SEAT}'" in s01[2]


# ── class 1: generalized slot supersession — the plan (pure) ─────────────────────

def test_plan_single_valued_slot_sweeps_subject_side():
    """spouse is is_single_valued: a newly affirmed holder arms the SUBJECT-side sweep
    (the incumbents retire; the fresh row is the keep operand)."""
    plan = M._slot_supersession_plan(
        [("u1", "marla2", "spouse")], set(), REL_META, DEFINING)
    assert plan["subject_side"] == {("u1", "spouse"): "marla2"}
    assert ("spouse", "marla2") not in plan["object_side"]


def test_plan_taxonomy_defining_rel_sweeps_object_side():
    """has_pet defines the 'pets' taxonomy group: a newly affirmed owner arms the
    OBJECT-side sweep — the pet's OTHER live claimant (the misrouted user row) retires."""
    plan = M._slot_supersession_plan(
        [("cato", "goose", "has_pet")], set(), REL_META, DEFINING)
    assert plan["object_side"] == {("has_pet", "goose"): "cato"}
    assert ("cato", "has_pet") not in plan["subject_side"]  # multi-valued subject side


def test_plan_immutable_and_plain_supersede_rels_never_sweep():
    """child_of is immutable; likes is supersede but neither single-valued nor
    taxonomy-defining — neither side arms (likes must not retire other people's likes)."""
    plan = M._slot_supersession_plan(
        [("kid", "u1", "child_of"), ("a", "b", "likes")], set(), REL_META, DEFINING)
    assert plan == {"subject_side": {}, "object_side": {}}


def test_plan_negated_incoming_edge_never_arms_the_sweep():
    """'I don't have a pet named Goose.' is a denial — the negation lanes own it; the
    sweep never fires off a negated commit."""
    plan = M._slot_supersession_plan(
        [("u1", "goose", "has_pet")], {("u1", "goose", "has_pet")}, REL_META, DEFINING)
    assert plan == {"subject_side": {}, "object_side": {}}


# ── class 1: the executor (stub DB; SQL shape + flag parity) ─────────────────────

class _StubCur:
    def __init__(self, rows_by_query, update_rowcount=0):
        self.rows_by_query = rows_by_query
        self.update_rowcount = update_rowcount
        self.executed = []
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("UPDATE facts"):
            self.rowcount = self.update_rowcount
        for marker, rows in self.rows_by_query.items():
            if marker in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubDB:
    def __init__(self, rows_by_query, update_rowcount=0):
        self.rows_by_query = rows_by_query
        self.update_rowcount = update_rowcount
        self.cursors = []
        self.committed = 0

    def cursor(self):
        c = _StubCur(self.rows_by_query, self.update_rowcount)
        self.cursors.append(c)
        return c

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


def _stub_db(update_rowcount=1, live_replacement=True):
    # rel_types probe → spouse/has_pet metadata; entity_taxonomies probe → has_pet;
    # the RETIRE-CONDITIONAL-ON-REPLACEMENT probe → the replacement row IS live
    # (the landed-correction shape; a failed correction stub passes False).
    return _StubDB({
        "FROM rel_types": [
            ("spouse", "supersede", True), ("has_pet", "supersede", False)],
        "FROM entity_taxonomies": [("has_pet",)],
        "FROM facts": [(1,)] if live_replacement else [],
    }, update_rowcount=update_rowcount)


def test_executor_retires_parallel_rows_and_commits():
    """THE PIN: 'Goose is Cato's cat.' (Class A, affirmed) retires the OTHER live
    (subject, has_pet, goose) claimant and commits before counting."""
    db = _stub_db()
    rows = [("uid", "cato", "goose", "has_pet", "mcp")]
    n = M._supersede_parallel_slot_rows(db, rows)
    assert n == 1  # object-side sweep retired the misrouted user-goose row
    upd = [(s, p) for c in db.cursors for s, p in c.executed
           if s.startswith("UPDATE facts")]
    assert len(upd) == 1
    sql, params = upd[0]
    assert "object_id = %s" in sql and "subject_id <> %s" in sql
    assert params[0] == "goose" and params[2] == "cato"
    assert "superseded_at IS NULL" in sql and "superseded_at = now()" in sql
    assert db.committed == 1


def test_executor_flag_off_is_byte_parity():
    """OFF → 0, and NO cursor is ever opened (the sweep never runs)."""
    db = _stub_db()
    prev = M.CORRECTION_REPLACE_NOT_PARALLEL
    M.CORRECTION_REPLACE_NOT_PARALLEL = False
    try:
        assert M._supersede_parallel_slot_rows(
            db, [("uid", "cato", "goose", "has_pet", "mcp")]) == 0
    finally:
        M.CORRECTION_REPLACE_NOT_PARALLEL = prev
    assert db.cursors == []
    assert db.committed == 0


# ── class 1b: RETIRE-CONDITIONAL-ON-REPLACEMENT (acceptance2, measured live) ──────
#
# THE MEASURED WOUND (acceptance2, 2026-11-14 seat e9271c7a, image 03500a15727d,
# tools/regression_results/convergence/results/acceptance2-report.md): T02
# "Actually my wife's name is Marla MacPhee." flaked to clarification_needed — the
# (user, spouse, marla) replacement NEVER committed — yet the post-commit slot sweep
# still retired the live spouse rows and the family serve LOST
# Marla/Desmond/Bianca/Fraggle/Sophia (store 10/17, serve 48%). THE CONTRACT: a
# sweep is CONDITIONAL on its replacement actually committing — retire-the-old only
# when the-new landed; a failed correction leaves the store untouched.

def test_acceptance2_failed_correction_leaves_store_untouched_subject_side():
    """THE PIN (the acceptance2 shape on the litmus's own fixture): the T02 wife-name
    correction flakes — class_a_rows still carries the intended (user, spouse, marla2)
    edge but NO live replacement row exists — the subject-side sweep DOES NOT RUN:
    the three planted spouse incumbents stay live, the store is untouched."""
    db = _stub_db(update_rowcount=3, live_replacement=False)
    rows = [("uid", SEAT, "marla2", "spouse", "mcp")]  # intended, never landed
    assert M._supersede_parallel_slot_rows(db, rows) == 0
    upd = [(s, p) for c in db.cursors for s, p in c.executed
           if s.startswith("UPDATE facts")]
    assert upd == []  # NO retirement — the truth rows stay live, serve intact
    assert db.committed == 0


def test_acceptance2_failed_correction_leaves_store_untouched_object_side():
    """Same guard, OBJECT side: 'Goose is Cato's cat.' flakes before commit — the
    (cato, has_pet, goose) replacement is not live — the taxonomy-defining sweep for
    (has_pet, goose) DOES NOT RUN and the misrouted (user, has_pet, goose) row stays
    live (better a parallel row than a lost pet)."""
    db = _stub_db(update_rowcount=1, live_replacement=False)
    rows = [("uid", "cato", "goose", "has_pet", "mcp")]
    assert M._supersede_parallel_slot_rows(db, rows) == 0
    upd = [(s, p) for c in db.cursors for s, p in c.executed
           if s.startswith("UPDATE facts")]
    assert upd == []
    assert db.committed == 0


def test_acceptance2_skipped_sweep_names_the_slot_and_reason():
    """The skipped sweep is AUDITED: an ingest.slot_supersede_skipped line carrying
    the slot (rel + sides) and the why (replacement not live)."""
    from structlog.testing import capture_logs
    db = _stub_db(update_rowcount=1, live_replacement=False)
    with capture_logs() as logs:
        M._supersede_parallel_slot_rows(db, [("uid", "cato", "goose", "has_pet", "mcp")])
    skipped = [e for e in logs if e.get("event") == "ingest.slot_supersede_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["side"] == "object" and skipped[0]["rel"] == "has_pet"
    assert "did not land" in skipped[0]["reason"]


def test_acceptance2_landed_correction_sweep_proceeds_both_sides():
    """Corrections land → the sweep PROCEEDS (the arm's original wins stay intact):
    subject side retires the spouse incumbents; object side retires the misrouted
    claimant — each gated by its OWN slot's live replacement probe."""
    # subject side: the landed (user, spouse, marla2) retires the twin incumbents
    db = _stub_db(update_rowcount=2)
    assert M._supersede_parallel_slot_rows(
        db, [("uid", SEAT, "marla2", "spouse", "mcp")]) == 2
    # object side: the landed (cato, has_pet, goose) retires the user-goose misroute
    db = _stub_db(update_rowcount=1)
    assert M._supersede_parallel_slot_rows(
        db, [("uid", "cato", "goose", "has_pet", "mcp")]) == 1


def test_acceptance2_mixed_batch_sweeps_only_landed_slots():
    """A batch with one landed and one failed replacement: ONLY the landed slot's
    sweep runs — the guard is per-slot, not per-turn."""
    # teach the facts probe to answer per-params: live only for the spouse key
    class _ProbeCur(_StubCur):
        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if sql.startswith("UPDATE facts"):
                self.rowcount = self.update_rowcount
                return
            if "FROM facts" in sql:
                self._rows = [(1,)] if tuple(params[:2]) == (SEAT, "spouse") else []
                return
            for marker, rows in self.rows_by_query.items():
                if marker in sql:
                    self._rows = rows
                    return
            self._rows = []

    class _ProbeDB(_StubDB):
        def cursor(self):
            c = _ProbeCur(self.rows_by_query, self.update_rowcount)
            self.cursors.append(c)
            return c

    db = _ProbeDB({
        "FROM rel_types": [
            ("spouse", "supersede", True), ("has_pet", "supersede", False)],
        "FROM entity_taxonomies": [("has_pet",)],
    }, update_rowcount=1)
    n = M._supersede_parallel_slot_rows(db, [
        ("uid", SEAT, "marla2", "spouse", "mcp"),     # landed → sweep runs
        ("uid", "cato", "goose", "has_pet", "mcp"),  # flaked → sweep skipped
    ])
    assert n == 1  # only the spouse slot retired rows
    upd = [(s, p) for c in db.cursors for s, p in c.executed
           if s.startswith("UPDATE facts")]
    assert len(upd) == 1 and upd[0][1][1] == "spouse"


# ── class 1c: ACCEPTANCE3 — the ENTITY-wipe mis-bind + the dark probe ─────────────
#
# THE MEASURED WOUND (acceptance3, kept seat 932cc9e0, schema faultline_932cc9e0_
# 68e6_4c46_b324_926167c738f1, 2026-11-15 — autopsy via shipped SQL, NOT the slot
# sweep): every family row superseded in ONE transaction (superseded_at identical to
# the microsecond, 00:33:05.98928) while T02 FAILED and T04 type_refused. The slot
# sweep never fired — it ERRORED first (ingest.slot_supersede_failed: entity_
# taxonomies has no `rel_type` column → fail-safe no-op → the object-side arm was
# DARK live). The killer was T07 "Remove Rex.": retraction_extraction returned
# subject=user / rel_type=pref_name, dimension ENTITY keyed on the SPEAKER, and
# correct_fact's remove_all_facts ran `UPDATE facts ... WHERE (subject_id=user OR
# object_id=user)` — spouse ×3, child_of ×5 (object=user), user-has-fraggle — while
# the response reported facts_superseded=0 + success.

def test_entity_retraction_speaker_misbind_detected():
    """THE PIN (acceptance3): a whole-ENTITY retraction keyed on the SPEAKER is a
    mis-bind — 'Remove Rex.' extracted subject=user → the guard fires, the wipe
    never runs, the store keeps every family row."""
    assert M._entity_retraction_speaker_misbind(SEAT, SEAT) is True


def test_entity_retraction_named_target_is_not_a_misbind():
    """A whole-entity retraction that NAMED a real target (Rex's own entity) is the
    legitimate 'forget Aurora completely' shape — the guard declines, the wipe runs."""
    assert M._entity_retraction_speaker_misbind("rex-entity-uuid", SEAT) is False
    assert M._entity_retraction_speaker_misbind(None, SEAT) is False


def test_entity_retraction_guard_flag_off_is_byte_parity():
    """OFF → never a mis-bind → the pre-guard unbounded wipe behavior returns."""
    assert M._entity_retraction_speaker_misbind(SEAT, SEAT, enabled=False) is False


def test_sweep_taxonomy_probe_uses_the_real_column():
    """THE PIN (the dark arm): the entity_taxonomies probe must select
    unnest(rel_types_defining_group) — the table has NO `rel_type` column; the old
    `SELECT DISTINCT rel_type` errored live on every call (ingest.slot_supersede_
    failed) and the object-side sweep silently never ran."""
    db = _stub_db()
    M._supersede_parallel_slot_rows(db, [("uid", "cato", "goose", "has_pet", "mcp")])
    tax = [(s, p) for c in db.cursors for s, p in c.executed
           if "FROM entity_taxonomies" in s]
    assert len(tax) == 1
    sql = tax[0][0]
    assert "unnest(rel_types_defining_group)" in sql
    assert "DISTINCT rel_type FROM entity_taxonomies" not in sql


def test_object_side_sweep_retires_only_the_rehomed_object():
    """THE SPECIFICITY PIN (the parent's fix-2): the object-side sweep's UPDATE keys
    on the SPECIFIC re-homed object — (cato, has_pet, goose) retires (user, has_pet,
    goose) ONLY; (user, has_pet, fraggle) is a different object_id and must never be
    touched by the goose sweep."""
    db = _stub_db()
    M._supersede_parallel_slot_rows(db, [("uid", "cato", "goose", "has_pet", "mcp")])
    upd = [(s, p) for c in db.cursors for s, p in c.executed
           if s.startswith("UPDATE facts")]
    assert len(upd) == 1
    sql, params = upd[0]
    assert "object_id = %s" in sql and params[0] == "goose"  # goose-scoped only


def test_entity_wipe_count_surfaces_in_facts_superseded():
    """SOURCE PIN (SEC-BUG-1 honest count): the ENTITY wipe's rowcount rides
    _entity_wipe_retired into _superseded_count — the wipe can never again report
    facts_superseded=0 after retiring nine rows, and the guard's rejection is the
    byte-parity OFF shape."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "api", "main.py")).read()
    assert "_entity_wipe_retired = cur.rowcount or 0" in src
    # the wipe's count reaches _superseded_count (the train later interleaved the slot
    # sweep and pets-space counts onto the same expression — assert presence, not layout)
    assert "+ _entity_wipe_retired" in src
    assert "+ _pet_space_retired)" in src
    assert "_entity_retraction_speaker_misbind(subject_uuid, req.user_id)" in src


def test_age_shelf_prior_slot_beats_non_animate_retype():
    """THE PIN: a subject holding a LIVE age row keeps the age shelf even when its
    entity_type is non-animate — 'Desmond is 13.' supersedes age=12, never mints a
    parallel has_value twin (the autopsy wound: has_value=13 beside age=12)."""
    assert M._seam1_age_shelf("Location", True, enabled=True) == "age"
    assert M._seam1_age_shelf("unknown", True, enabled=True) == "age"


def test_age_shelf_guard_unchanged_without_prior_row():
    """The SEAM-1 staging-port class is intact: a non-animate subject with NO age row
    still retypes to has_value ('the port is 8010' must not become '8010 years old')."""
    assert M._seam1_age_shelf("Object", False, enabled=True) == "has_value"
    assert M._seam1_age_shelf("Location", False, enabled=True) == "has_value"


def test_age_shelf_animate_keeps_age():
    assert M._seam1_age_shelf("Person", False, enabled=True) == "age"
    assert M._seam1_age_shelf("Animal", False, enabled=True) == "age"


def test_age_shelf_flag_off_ignores_prior_row():
    """OFF → byte-for-byte pre-fix decision (prior-slot precedence disabled)."""
    assert M._seam1_age_shelf("Location", True, enabled=False) == "has_value"


# ── class 4: negation disengagement at the serve filter (pure) ───────────────────

def _f(s, o, r, pol="affirmed"):
    return {"subject": s, "object": o, "rel_type": r, "polarity": pol,
            "source": "db"}


def test_affirmation_wins_over_negated_twin():
    """THE PIN: same natural key, affirmed + negated — the negated twin is withdrawn
    from the serve (it renders only on archive/before asks)."""
    out = M._disengage_negated_twins(
        [_f("cato", "goose", "has_pet"), _f("cato", "goose", "has_pet", "negated")],
        enabled=True)
    assert [f["polarity"] for f in out] == ["affirmed"]


def test_lone_negation_still_serves():
    """A denial with no affirmed twin is the live truth — it serves as today."""
    out = M._disengage_negated_twins(
        [_f("u1", "goose", "has_pet", "negated")], enabled=True)
    assert len(out) == 1 and out[0]["polarity"] == "negated"


def test_negation_disengagement_flag_off_is_byte_parity():
    rows = [_f("cato", "goose", "has_pet"), _f("cato", "goose", "has_pet", "negated")]
    out = M._disengage_negated_twins(rows, enabled=False)
    assert out is rows


def test_disengagement_keys_on_the_full_natural_key():
    """A negated row on a DIFFERENT triple never rides the disengagement (the user's
    own denial about their pets survives a neighbour's affirmation)."""
    out = M._disengage_negated_twins(
        [_f("cato", "goose", "has_pet"), _f("u1", "goose", "has_pet", "negated")],
        enabled=True)
    assert len(out) == 2


# ── flag default ─────────────────────────────────────────────────────────────────

def test_flag_defaults_on():
    assert M.CORRECTION_REPLACE_NOT_PARALLEL is True


def test_fixture_shapes_survive_the_bind():
    """The litmus fixture still plants the full fuckered shape (twins, generic dupes,
    junk rows, the negated contradiction, the stale age) — only the user id moved."""
    sql, E = L.fixture_sql(str(uuid.uuid4()), "sch")
    for alias in ("'marla'", "'wife'", "13-year-old son", "belong_to", "born_on"):
        assert alias in sql
    assert E["desmond"] != E["user"]
