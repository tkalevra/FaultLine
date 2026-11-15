"""BUG-FAMILY-9 — the child-entity type refusal + the medical class-query ground.

The two production-verified residuals from the family-shape probe on the production door
(the sparse-store re-verify's filed residuals, 2026-11-13):

RESIDUAL 1 — THE CHILD ENTITY TYPE REFUSAL. "My daughter('s name is) Wren …" sentences
returned status=type_refused BOTH seats: the kinship rail emitted exactly
(wren, child_of, user) (migration 109's daughter → child_of map works), but the bind
endpoint's surface arrives PIPELINE-LOWERCASED ('wren'), so R14's kin-copula person
assertion (which keys on the casing the user wrote) never fired, and the bird word kept
the turn-local GLiNER type ANIMAL — child_of declares Person-only roles, the gate refused
(head ANIMAL vs ['Person']), and the kids could not be stored. The fix: the assertion's
DISPLAY-FORM second rung (migration 214's observed casing overlay retains the verbatim
PROPN run from the raw turn) + the object-side stamp made unconditional when kin-
adjudicated (the subject side already was; the name-on-object arrangement left the row
Animal, the gate's registry read saw Animal, registry-wins never fired).

RESIDUAL 2 — THE MEDICAL CLASS-QUERY GROUND. "what are my medical conditions?" abstained
honest-empty on a seat holding LIVE medical rows (metformin + penicillin both stored
valid) while the entity-shaped control ("what allergy should be flagged before
prescriptions?") served from the SAME seat. The health-shelf admission was (a) missing
the has_medical_condition nouns entirely and (b) single-rel — a matched surface admitted
only its own rel's rows. The fix: the SEEDED GROWABLE medical_shelf_noun cue class
(migration 322, the 316/308/318 pattern) + the CLASS-GROUP admission (the query naming a
class noun probes the anchor's live rows across the whole declared medical shelf class —
the 314 sensitivity census, medical subset, mirrored in _HEALTH_SHELF_REL_CLASS).

MEASURED DROPS pinned alongside (the same repro seat): the age word-number ("she is
seven years old") dropped whole at the scalar_datatype_preflight — the spelled-cardinal
rung in _parse_numeric_scalar closes it.

Flag gates: RECALL_HEALTH_SHELF_CLASS (default ON; OFF → the legacy matched-rels-only
admission byte-for-byte) and SCALAR_WORD_NUMBER_COERCE (default ON; OFF → digit-only
parse). The negation scan-back, the correction marker, and the WGM gates are untouched.
"""
import os
import re
from pathlib import Path

import pytest

from src.api.kin_weld_repoint import (
    kin_copula_person_assertion,
    kin_rel_guard_set,
)
from src.extraction.display_case import (
    display_form_for,
    reset_display_forms,
    set_display_forms,
)

_KIN_NOUNS = frozenset({
    "mother", "father", "mom", "dad", "parent", "sister", "brother", "sibling",
    "son", "daughter", "child", "wife", "husband", "spouse", "partner",
    "uncle", "aunt", "cousin", "grandmother", "grandfather", "grandma", "grandpa",
    "ex-wife", "ex wife", "ex-husband", "ex husband", "stepson", "step-son",
    "stepdaughter", "step-daughter", "eldest", "youngest", "oldest",
})

_REPO = Path(__file__).resolve().parents[2]
_MAIN_SRC = _REPO / "src" / "api" / "main.py"


def _main_source() -> str:
    """The test's OWN checkout (never an absolute shared path — the shared tree moves
    under concurrent sessions)."""
    return _MAIN_SRC.read_text(encoding="utf-8")


# ── RESIDUAL 1: the display-form second rung ────────────────────────────────────────


class _DisplayOverlay:
    """Bind/unbind the migration-214 casing overlay per test (token-based, like ingest)."""

    def __enter__(self):
        self._token = set_display_forms(self.forms)
        return self

    def __exit__(self, *exc):
        reset_display_forms(self._token)
        return False

    def __init__(self, forms):
        self.forms = dict(forms or {})


def test_display_rung_asserts_the_pipeline_lowercased_kin_name():
    """THE measured residual shape: the user wrote 'Wren' capitalized; the deriver's
    surface is pipeline-lowercased ('wren'); the observed display overlay carries the
    verbatim form. The kin-frame person assertion must fire through the rung — the
    child_of bind's head type then flips to Person and the bind admits."""
    with _DisplayOverlay({"wren": "Wren"}):
        assert kin_copula_person_assertion("wren", _KIN_NOUNS) is True
        # the role-word refusal survives the rung: 'wife' observed as 'Wife' is still a
        # kin role word — the tautology contract (F1-R7) is untouched.
        with _DisplayOverlay({"wife": "Wife"}):
            assert kin_copula_person_assertion("wife", _KIN_NOUNS) is False


def test_display_rung_declines_without_observation_byte_parity():
    """No overlay entry → None → the decline stands BYTE-FOR-BYTE (the pre-rung
    behavior): an unbound ContextVar (every direct unit caller, every test) and a
    lowercase-written 'granite' (never a PROPN run) both decline exactly as pinned
    since R5-2."""
    with _DisplayOverlay({}):
        assert kin_copula_person_assertion("wren", _KIN_NOUNS) is False
        assert kin_copula_person_assertion("granite", _KIN_NOUNS) is False
        assert kin_copula_person_assertion("wife", _KIN_NOUNS) is False


def test_rung_contract_keys_on_the_users_own_casing():
    """The R5-2 contract was written against the CALLER'S casing: 'granite' written
    lowercase declines forever; a surface the user DID capitalize ('Slate') asserts —
    unchanged from R14. The rung only recovers casing the pipeline erased."""
    with _DisplayOverlay({"slate": "Slate"}):
        assert kin_copula_person_assertion("slate", _KIN_NOUNS) is True
    with _DisplayOverlay({}):
        assert kin_copula_person_assertion("Slate", _KIN_NOUNS) is True
        assert kin_copula_person_assertion("granite", _KIN_NOUNS) is False


def test_display_overlay_invariant_holds_for_the_rung_input():
    """display_form_for folds back to the alias (the migration-214 invariant the rung
    relies on) — the rung never sees a different NAME, only the user's casing."""
    with _DisplayOverlay({"wren": "Wren", "bad": "differentname"}):
        assert display_form_for("wren") == "Wren"
        assert display_form_for("bad") is None  # casing overlay only, never content


def test_object_side_stamp_is_unconditional_when_kin_adjudicated():
    """BUG-FAMILY-9 (the name-on-object arrangement): the R14 object-side port left the
    kin-adjudicated stamp unknown-only (the subject side made its own unconditional in
    R14; the mirror never landed) — so a PRE-EXISTING non-Person child row never flipped,
    the gate's registry read saw the stale type, and the bind refused. The object-side
    block must carry the same `or _kwr_stamp_adjudicated` guard as the subject side.
    Q4 VEHICLE UPDATE (2026-11-13, by design): the condition EXTENDED to
    `or _kwr_stamp_adjudicated or _kinmint_obj` (KIN_TYPE_AT_MINT — the kin-cue lane
    context is a third unconditional-why); the mechanism assertion is unchanged — the
    stamp must update UNCONDITIONALLY when kin-adjudicated, and the extended condition
    only widens the unconditional class."""
    s = _main_source()
    obj_side = s.index("_stamp_type = edge.object_type.title()")
    window = s[obj_side:obj_side + 6000]
    assert re.search(r"if\s+_is_class_a_correction\s+or\s+_kwr_stamp_adjudicated"
                     r"(?:\s+or\s+_kinmint_obj)?\s*:",
                     window), (
        "the object-side kin-adjudicated stamp must UPDATE unconditionally (the mirror "
        "of the subject side's R14 rule) — an unknown-only write leaves a pre-existing "
        "Animal row Animal and the registry-wins adjudication starves")


def test_child_kin_rel_guard_covers_the_child_shapes():
    """The guard set (the door the adjudication arms on) must cover the child rels from
    BOTH spellings: the canonical value (child_of, parent_of) and the raw surface the
    spine may emit (daughter, son) — the 109/320 seed map rides in."""
    relmap = {"daughter": "child_of", "son": "child_of", "child": "child_of",
              "mother": "parent_of", "wife": "spouse", "stepson": "step_parent_of"}
    guard = kin_rel_guard_set(set(relmap.values()), relmap)
    for rel in ("child_of", "parent_of", "spouse", "step_parent_of",
                "daughter", "son", "child"):
        assert rel in guard


# ── RESIDUAL 2: the medical class-query ground ──────────────────────────────────────


def test_class_mode_admits_the_condition_nouns():
    """THE measured abstention: "what are my medical conditions?" matched NOTHING before
    (no condition surface) — the DB-down floor of the seeded class must admit it."""
    import src.api.main as main
    saved, main._RECALL_HEALTH_SHELF_CLASS = main._RECALL_HEALTH_SHELF_CLASS, True
    saved_dsn = os.environ.pop("POSTGRES_DSN", None)
    try:
        rels = main._query_health_shelf_rels("what are my medical conditions?")
        assert "has_medical_condition" in rels
        assert "has_allergy" in main._query_health_shelf_rels("what are my allergies?")
        assert "has_medication" in main._query_health_shelf_rels(
            "what medications do I take?")
        # the measured control phrase keeps its single-rel admission
        assert "has_allergy" in main._query_health_shelf_rels(
            "what allergy should be flagged before prescriptions?")
    finally:
        main._RECALL_HEALTH_SHELF_CLASS = saved
        if saved_dsn is not None:
            os.environ["POSTGRES_DSN"] = saved_dsn


def test_class_flag_off_is_byte_parity_legacy_matched_rels_only():
    """RECALL_HEALTH_SHELF_CLASS OFF → the legacy two-rel map, byte-for-byte: the
    condition nouns admit NOTHING (pre-fix behavior), the legacy tuple order is the
    map's order."""
    import src.api.main as main
    saved, main._RECALL_HEALTH_SHELF_CLASS = main._RECALL_HEALTH_SHELF_CLASS, False
    try:
        assert main._query_health_shelf_rels("what are my medical conditions?") == ()
        assert main._query_health_shelf_rels("what are my allergies?") == ("has_allergy",)
        assert main._query_health_shelf_rels("what medications do I take?") == (
            "has_medication",)
        assert main._query_health_shelf_rels("no health nouns here") == ()
    finally:
        main._RECALL_HEALTH_SHELF_CLASS = saved


def test_class_group_probe_spans_the_declared_medical_shelf_class():
    """The CLASS-GROUP admission probes the anchor's live rows across the WHOLE declared
    medical shelf class — the 314 sensitivity census, medical subset, mirrored in-code.
    The measured seat's rows (has_allergy + the LLM lane's takes_medication) must BOTH
    sit in the probe set, and the set must never carry the non-medical sensitive shelves
    (phone/email/financial/credential)."""
    import src.api.main as main
    cls = set(main._HEALTH_SHELF_REL_CLASS)
    for rel in ("has_allergy", "has_medication", "has_medical_condition",
                "takes_medication", "has_condition", "has_prescription"):
        assert rel in cls
    for foreign in ("has_phone", "has_email", "has_bank_account", "has_password",
                    "has_salary", "has_social_security_number"):
        assert foreign not in cls


def test_medical_shelf_noun_seed_and_floor_mirror():
    """The migration-322 seed and the overlay bootstrap floor must stay row-for-row
    (the social_role lesson: a floor narrower than the seed erodes the seed on the
    DB-down path; a floor wider fabricates admissions)."""
    from src.api.linguistic_cue_overlay import (
        _BOOTSTRAP_MEDICAL_SHELF_NOUNS,
        MEDICAL_SHELF_NOUN_CATEGORY,
    )
    sql = (_REPO / "migrations" / "322_seed_medical_shelf_noun_cues.sql").read_text(
        encoding="utf-8")
    seeded = dict(re.findall(
        r"\('([a-z]+)',\s*'" + MEDICAL_SHELF_NOUN_CATEGORY + r"',\s*'([a-z_]+)'",
        sql))
    assert seeded, "the migration's public seed section vanished"
    assert seeded == _BOOTSTRAP_MEDICAL_SHELF_NOUNS, (
        "migration 322's seed and the overlay's bootstrap floor drifted — mirror them "
        "row-for-row (the floor is the DB-down carrier, the seed is the DB rail)")


# ── the measured drop: the age word-number ──────────────────────────────────────────


def test_spelled_cardinal_parses_for_integer_slots():
    """"she is seven years old" dropped whole: the integer datatype's digit parse refused
    'seven' at the scalar preflight. The rung parses the closed cardinal-word class."""
    import src.api.main as main
    saved, main._SCALAR_WORD_NUMBER_COERCE = main._SCALAR_WORD_NUMBER_COERCE, True
    try:
        assert main._parse_numeric_scalar("seven", "integer") == 7.0
        assert main._parse_numeric_scalar("twenty five", "integer") == 25.0
        assert main._parse_numeric_scalar("twenty-five", "integer") == 25.0
        ok, reason = main._validate_scalar_datatype("integer", "seven",
                                                    {"value_min": 0})
        assert ok, reason
        # the closed class is 0-99: out-of-class words keep the legacy refusal (never
        # fabricated) — the boundary is the class, not a guess
        ok, reason = main._validate_scalar_datatype("integer", "three hundred",
                                                    {"value_min": 0})
        assert not ok and "numeric value" in reason
    finally:
        main._SCALAR_WORD_NUMBER_COERCE = saved


def test_digit_parse_is_unchanged_and_flag_off_is_byte_parity():
    """Every previously-parsing value behaves byte-for-byte; SCALAR_WORD_NUMBER_COERCE
    OFF restores the digit-only parse."""
    import src.api.main as main
    saved = main._SCALAR_WORD_NUMBER_COERCE
    try:
        assert main._parse_numeric_scalar("9", "integer") == 9.0
        assert main._parse_numeric_scalar("180 cm", "quantity") == 180.0
        assert main._parse_numeric_scalar("few", "integer") is None
        main._SCALAR_WORD_NUMBER_COERCE = False
        assert main._parse_numeric_scalar("seven", "integer") is None
    finally:
        main._SCALAR_WORD_NUMBER_COERCE = saved


def test_word_number_rung_never_fabricates_from_non_numbers():
    """A non-number word ('few', a sentence fragment) stays None — the rung consults the
    closed grammatical cardinal class only, never fuzzy."""
    import src.api.main as main
    saved, main._SCALAR_WORD_NUMBER_COERCE = main._SCALAR_WORD_NUMBER_COERCE, True
    try:
        assert main._parse_numeric_scalar("few", "integer") is None
        assert main._parse_numeric_scalar("some", "integer") is None
        assert main._parse_numeric_scalar("hello world", "integer") is None
    finally:
        main._SCALAR_WORD_NUMBER_COERCE = saved


# ── the measured sentences' emit half (the deriver-level pin; the store half is the
#    ON-image door litmus — see tools/bf9_litmus.py) ──────────────────────────────────



def test_measured_child_sentences_emit_the_kinship_rail_edges():
    """Both measured residual sentences must emit (wren, child_of, user) — the kinship
    rail itself was NEVER the wound (the refusal was the bind endpoint's GLiNER type);
    this pin guards the emit against regressions while the type fix rides the stamp
    adjudication."""
    from src.extraction.linguistics import derive_sentence_facts, linguistics_available
    if not linguistics_available():
        pytest.skip("spaCy linguistic layer unavailable")
    for sent in ("My daughter's name is Wren and she is seven years old.",
                 "My daughter Wren started kindergarten this September."):
        out = derive_sentence_facts(sent, "2026-09-01")
        edges = [(getattr(f, "subject", None), getattr(f, "rel_type", None),
                  getattr(f, "object", None)) for f in
                 (out if isinstance(out, list) else out.get("facts", []))]
        assert ("wren", "child_of", "user") in edges, (
            f"the kinship rail lost the child_of emit on the measured sentence: {sent!r} "
            f"→ {edges}")
    # the age emit (the measured drop's other half — the value itself)
    out = derive_sentence_facts(
        "My daughter's name is Wren and she is seven years old.", "2026-09-01")
    edges = [(getattr(f, "subject", None), getattr(f, "rel_type", None),
              getattr(f, "object", None)) for f in
             (out if isinstance(out, list) else out.get("facts", []))]
    assert ("wren", "age", "seven") in edges
