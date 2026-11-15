# -*- coding: utf-8 -*-
"""EXTRACTION_STRONG_SUBJECT_BIND pins (acceptance4 subject-bind class).

THE MEASURED WOUND (convergence litmus acceptance4, seat fe5a992c, image 670be985,
2026-11-15 — tools/regression_results/convergence/results/acceptance4-report.md;
corrections landed 57%, store 41%): all three residual shapes are ONE class —
extraction bound the WRONG SUBJECT, so the named THING never became the target:
  1. T07 "Remove Rex." — retraction_extraction returned subject=user, rel=pref_name
     (dimension ENTITY); the Rex entity (an Animal in the pets space) never became the
     removal target and the speaker guard refused the whole turn.
  2. T02 "Actually my wife's name is Marla MacPhee." — flakes to
     clarification_needed / failed ~2 of 3 runs (the R4-J extraction-draw variance
     class; no contrast clause for the deterministic ", not <Name>" marker lanes).
  3. 'mars'/'desmonde' — person-shaped, kin/pet-membership-bearing entities retyped
     entity_type=Location in place by the ingest class-a unconditional stamp riding a
     zero-shot type guess, cascading type_refused on T04 "Fraggle is Mars's morkie."

THE FIX, flag EXTRACTION_STRONG_SUBJECT_BIND default ON (OFF = byte-parity):
  a. ``_retraction_named_thing_rebind`` — a speaker-bound whole-thing/identity
     retraction rebinds its target to the entity the extracted surface STRONG-matches
     (full alias equality, exactly one holder, not the speaker — the FORGET_ENTITY_SWEEP
     admission discipline);
  b. ``_kin_name_correction_floor`` — the contrast-free kin-name designation frame is
     extracted deterministically FIRST; the LLM draw never decides it;
  c. ``_membership_retype_blocked`` — a Location retype is refused on person-shaped /
     taxonomy-membership-bearing entities.

Pure pins + stub-DB pins; no database. Fixture shapes from the litmus's own
generator (tools/regression_results/convergence/litmus_lib.py).
"""
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


SEAT = "fe5a992c-9df1-495f-a9a0-000000000001"  # acceptance4-seat-shaped (reproducible)
E = L.entity_ids(SEAT)


class _StubCur:
    """Marker-matched stub: the LAST matching marker's rows win (query order matters)."""

    def __init__(self, rows_by_query):
        self.rows_by_query = rows_by_query
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._rows = []
        for marker, rows in self.rows_by_query.items():
            if marker in sql:
                self._rows = rows
                # do NOT break: later markers are more specific in these fixtures

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# ── the STRONG admission rule (shared by arms 1 and 2) ────────────────────────────

def test_strong_match_admits_single_non_speaker_holder():
    cur = _StubCur({"FROM entity_aliases": [(E["rex"],)]})
    eid, reason = M._strong_subject_alias_match(cur, "Rex", SEAT)
    assert eid == E["rex"] and reason is None


def test_strong_match_declines_ambiguous_and_speaker_and_absent():
    cur = _StubCur({"FROM entity_aliases": [(E["marla1"],), (E["marla2"],)]})
    assert M._strong_subject_alias_match(cur, "marla", SEAT) == (None, "ambiguous_alias:2")
    cur = _StubCur({"FROM entity_aliases": [(SEAT,)]})
    assert M._strong_subject_alias_match(cur, "user", SEAT) == (None, "speaker_entity")
    cur = _StubCur({})
    assert M._strong_subject_alias_match(cur, "nobody", SEAT) == (None, None)


# ── ARM 1: T07 "Remove Rex." rebinds the retraction target to the Rex entity ──────

def _t07_extraction():
    return {"subject": "user", "rel_type": "pref_name", "object": "Rex",
            "action": "remove", "dimension": "ENTITY", "confidence": 0.95}


def test_rebind_targets_the_named_thing():
    """THE PIN: the acceptance4 T07 shape — subject=user, rel=pref_name, dimension
    ENTITY — rebinds to the Rex entity the object surface strong-matches."""
    cur = _StubCur({"FROM entity_aliases": [(E["rex"],)]})
    out = M._retraction_named_thing_rebind(
        cur, SEAT, "user", _t07_extraction(), SEAT)
    assert out == E["rex"]


def test_rebind_prefers_subject_surface_then_object():
    # a non-first-person SUBJECT surface wins ("Remove Rex." with subject="Rex")
    cur = _StubCur({"FROM entity_aliases": [(E["rex"],)]})
    ex = {"subject": "Rex", "rel_type": "pref_name", "object": None,
          "dimension": "ENTITY"}
    assert M._retraction_named_thing_rebind(cur, SEAT, "Rex", ex, SEAT) == E["rex"]


def test_rebind_off_is_byte_parity():
    cur = _StubCur({"FROM entity_aliases": [(E["rex"],)]})
    assert M._retraction_named_thing_rebind(
        cur, SEAT, "user", _t07_extraction(), SEAT, enabled=False) is None
    assert cur.executed == []  # OFF never even probes


def test_rebind_never_fires_off_the_identity_lane():
    # a RELATIONAL has_pet removal keys on the subject's own row — untouched
    cur = _StubCur({"FROM entity_aliases": [(E["rex"],)]})
    ex = {"subject": "user", "rel_type": "has_pet", "object": "Rex",
          "dimension": "RELATIONAL"}
    assert M._retraction_named_thing_rebind(cur, SEAT, "user", ex, SEAT) is None


def test_rebind_pronoun_only_keeps_the_speaker_binding():
    # nothing strong-matches → None → today's binding (the honest guard refusal)
    cur = _StubCur({})
    assert M._retraction_named_thing_rebind(
        cur, SEAT, "user", _t07_extraction(), SEAT) is None


def test_rebind_ambiguous_surface_keeps_the_speaker_binding():
    cur = _StubCur({"FROM entity_aliases": [(E["marla1"],), (E["marla2"],)]})
    ex = {"subject": "user", "rel_type": "pref_name", "object": "marla",
          "dimension": "ENTITY"}
    assert M._retraction_named_thing_rebind(cur, SEAT, "user", ex, SEAT) is None


# ── ARM 2: T02 deterministic on the fixture ───────────────────────────────────────

def _patch_role_maps(monkey=None, mapping=None):
    """Point the per-tenant cue maps at the litmus seat's kin map (wife → spouse)."""
    import src.api.linguistic_cue_overlay as LCO
    orig_kin, orig_soc = LCO.resolve_kinship_rel_map, LCO.resolve_social_role_map
    LCO.resolve_kinship_rel_map = lambda dsn: (mapping or {"wife": "spouse"})
    LCO.resolve_social_role_map = lambda dsn: {}

    def _restore():
        LCO.resolve_kinship_rel_map = orig_kin
        LCO.resolve_social_role_map = orig_soc
    return _restore


T02_TEXT = "Actually my wife's name is Marla MacPhee."


def test_kin_name_floor_is_deterministic_on_the_fixture():
    """THE PIN: the contrast-free kin-name frame extracts IDENTICALLY on every call —
    subject=user, rel=spouse (the role's cue-map rel), new_value the asserted name —
    no LLM draw involved, so the R4-J variance class cannot fire."""
    restore = _patch_role_maps()
    try:
        outs = []
        for _ in range(5):
            cur = _StubCur({"FROM entity_aliases": [(E["marla2"],)]})
            outs.append(M._kin_name_correction_floor(T02_TEXT, cur, SEAT))
        assert all(o == outs[0] for o in outs)
        o = outs[0]
        assert o is not None
        assert o["subject"] == "user" and o["subject_name"] == "user"
        assert o["old_rel_type"] == "spouse" and o["new_rel_type"] == "spouse"
        assert o["old_value"] is None            # no contrast clause names an incumbent
        assert o["new_value"] == "marla macphee"  # the asserted label surface
        assert o["dimension"] == "RELATIONAL"
        assert o["confidence"] >= 0.9
    finally:
        restore()


def test_kin_name_floor_requires_a_strong_value_match():
    """The asserted name must strong-resolve to exactly ONE stored entity — a fresh
    never-seen name (or an ambiguous twin alias) declines to today's honest path."""
    restore = _patch_role_maps()
    try:
        # 'marla' is held by TWO planted twins → ambiguous → decline
        cur = _StubCur({"FROM entity_aliases": [(E["marla1"],), (E["marla2"],)]})
        assert M._kin_name_correction_floor(T02_TEXT, cur, SEAT) is None
        cur = _StubCur({})  # nothing stored under the label → decline
        assert M._kin_name_correction_floor(T02_TEXT, cur, SEAT) is None
    finally:
        restore()


def test_kin_name_floor_declines_unmapped_role_and_non_first_person():
    restore = _patch_role_maps(mapping={"mother": "parent_of"})
    try:
        cur = _StubCur({"FROM entity_aliases": [(E["marla2"],)]})
        assert M._kin_name_correction_floor(T02_TEXT, cur, SEAT) is None  # wife unmapped
        restore()
        restore = _patch_role_maps()
        # non-first-person possessor: "Diane's wife's name is …" → decline
        cur = _StubCur({"FROM entity_aliases": [(E["marla2"],)]})
        assert M._kin_name_correction_floor(
            "Diane's wife's name is Marla MacPhee.", cur, SEAT) is None
    finally:
        restore()


# ── ARM 3: the Location retype is refused on membership-bearing entities ──────────

def _retype_cur(entity_type="Person", membership=False):
    rows = {}
    if entity_type is not None:
        rows["FROM entities WHERE id"] = [(entity_type,)]
    if membership:
        rows["FROM entity_taxonomies"] = [("has_pet",)]
    return _StubCur(rows)


def test_retype_refused_on_person_shaped_entity():
    """THE PIN: the autopsy shape — a Person-typed entity (desmonde/mars, holding live
    kin edges) is never retyped Location by the class-a unconditional stamp."""
    cur = _retype_cur("Person")
    assert M._membership_retype_blocked(cur, E["desmonde"], "Location") is True
    cur = _retype_cur("Animal")
    assert M._membership_retype_blocked(cur, E["marla1"], "location") is True  # casefold


def test_retype_refused_on_membership_edges_even_when_type_is_junk():
    # an 'unknown'-typed entity that HOLDS a taxonomy-defining edge (has_pet/spouse
    # group member) is still a member of a family space, never a place
    cur = _retype_cur("unknown", membership=True)
    assert M._membership_retype_blocked(cur, E["rex"], "Location") is True


def test_retype_allowed_for_genuine_places_and_non_location_stamps():
    cur = _retype_cur(None, membership=False)   # no entities row, no membership rows
    assert M._membership_retype_blocked(cur, "some-uuid", "Location") is False
    cur = _retype_cur("Person")
    assert M._membership_retype_blocked(cur, E["desmonde"], "Animal") is False
    assert M._membership_retype_blocked(cur, E["desmonde"], "Concept") is False


def test_retype_flag_off_is_byte_parity():
    cur = _retype_cur("Person", membership=True)
    assert M._membership_retype_blocked(
        cur, E["desmonde"], "Location", enabled=False) is False
    assert cur.executed == []  # OFF never probes


# ── the flag itself ───────────────────────────────────────────────────────────────

def test_flag_defaults_on():
    assert M.EXTRACTION_STRONG_SUBJECT_BIND is True


# ── ARM 1 value completion: THE PETS-SPACE NARROWING (acceptance6b T07) ───────────
#
# THE MEASURED RESIDUAL (acceptance6b, seat 0b1d6522, image d53585089,
# 2026-11-15): T07 "Remove Rex." still left (user, has_pet, rex) live and the final
# serve voiced the poison ("You have a pet that is Rex") — the retraction's
# rel-exact retirement keys on the extracted rel, and 'rex' (3 chars) never
# qualifies for the ≥4-char value sweeps. THE DOCTRINE: a removal targets a THING in
# a KNOWN L4 space — when the target surface strong-resolves to an entity that HOLDS
# a live membership in a taxonomy defining-group space (has_pet for pets), that
# thing's OWN member rows retire regardless of span length.

def _space_cur(holders, member_ids=(101,), space=(("has_pet",),)):
    return _StubCur({
        "FROM entity_aliases": holders,
        "FROM entity_taxonomies": [(tuple(s),) for s in space],
        "RETURNING id": [(i,) for i in member_ids],
    })


def test_space_sweep_retires_a_short_named_pet_member():
    """THE PIN (the acceptance6b shape): 'rex' (3 chars) strong-resolves to the Rex
    entity, which holds a live pets-space membership → its member row retires — the
    span-length guard never applies to a THING in a known space."""
    cur = _space_cur([(E["rex"],)], member_ids=(101,))
    n, eid = M._retraction_space_member_sweep(cur, SEAT, "rex")
    assert n == 1 and eid == E["rex"]
    upd = next(s for s, _p in cur.executed if s.startswith("UPDATE facts"))
    assert "object_id = %s" in upd and "superseded_at = now()" in upd
    # the rel scope is the tenant's OWN defining groups (metadata, no word list)
    _p = next(p for s, p in cur.executed if s.startswith("UPDATE facts"))
    assert "has_pet" in list(_p[1])


def test_space_sweep_declines_a_non_member_entity():
    """A short surface against an entity with NO live space membership still declines
    — the guard is the space membership, never the span."""
    cur = _space_cur([(E["rex"],)], member_ids=())
    n, eid = M._retraction_space_member_sweep(cur, SEAT, "rex")
    assert (n, eid) == (0, E["rex"]) or (n, eid) == (0, None)
    assert not any(s.startswith("UPDATE") for s, _p in cur.executed) or n == 0


def test_space_sweep_declines_unresolvable_and_speaker_surfaces():
    # nothing strong-matches → no probe past the alias lookup, no UPDATE
    cur = _space_cur([])
    assert M._retraction_space_member_sweep(cur, SEAT, "nobody") == (0, None)
    # the speaker is never the removal target
    cur = _space_cur([(SEAT,)])
    assert M._retraction_space_member_sweep(cur, SEAT, "user") == (0, None)
    # ambiguous holder identifies no thing
    cur = _space_cur([(E["marla1"],), (E["marla2"],)])
    assert M._retraction_space_member_sweep(cur, SEAT, "marla") == (0, None)
    # empty surface declines before any query
    cur = _space_cur([])
    assert M._retraction_space_member_sweep(cur, SEAT, "") == (0, None)
    assert cur.executed == []


def test_space_sweep_no_spaces_no_sweep():
    """A tenant that declares NO taxonomy defining groups → the narrowing has no
    known space → declines (byte-parity with the old behavior)."""
    cur = _space_cur([(E["rex"],)], space=())
    assert M._retraction_space_member_sweep(cur, SEAT, "rex") == (0, None)


def test_space_sweep_count_rides_facts_superseded():
    """SOURCE PIN: the sweep's count reaches the response's facts_superseded (the
    SEC-BUG-1 honesty contract) and the lane hook fires on the retraction path only."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "api", "main.py")).read()
    assert "+ _pet_space_retired)" in src
    assert "if is_retraction and EXTRACTION_STRONG_SUBJECT_BIND:" in src
    assert "correct_fact.retraction_space_member_retired" in src
