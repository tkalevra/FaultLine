"""SPINE_ROLE_HEAD_NO_BIND — the role-word-head NO-BIND rule on the kin copula frame
(Fog Family Lab F1, DEV/FINDINGS-fog-family-lab-20261107.md).

THE WOUND (deployed default, SPINE_NAMING_CHAIN off — no deployed stack carries SPINE_* env):
"My wife is Ophelia." welded the ROLE WORD as the spouse entity — (wife, spouse, user) — while
Ophelia dropped as uncovered residue (or mis-filed as a has_state twin when the sm tagger
mis-typed the name), so recall voiced "You and Wife are spouses/partners" and the named person
was invisible. Root cause: the possessive chain's interplay guard with the copula-name chain was
gated on SPINE_NAMING_CHAIN (owner opt-in, default OFF), so with the broad chain dormant
_chain_possessive's kin arm read "my wife" blind to the copula's proper-name complement.

THE RULE (TAC KBP name-slot doctrine — the slot's filler is the PROPER NAME, the role word is
the slot's trigger, never the filler; tac.nist.gov/2012/KBP task guidelines): in a copular
clause whose nsubj is a 1st-person-possessed KIN role noun and whose bare complement is a
PROPER NAME, the role word must never BE the bound entity — the NAME binds the kin relation
(seeded kinship_noun cue map) and the role surface files as an also_known_as alias on that
person (THE HARD LINE: a role is a slot/alias, not a second entity).

THE FIX (src/extraction/linguistics.py, flag SPINE_ROLE_HEAD_NO_BIND default ON):
  • _chain_copula_name: the KINSHIP slice arms under either flag; the NON-KIN <role>_of leg
    (novel rel growth) stays strictly behind SPINE_NAMING_CHAIN (owner opt-in — the
    SPINE_CORRECTION_NAME_REPAIR split discipline: a new default never inherits a dormant
    broad flag's opt-in surface).
  • _chain_possessive: the interplay step-aside de-gates for KIN heads only.

PRESERVED (all pinned below): negation absence ("My wife is not Ophelia." binds nothing), the
"is not named X but Y" predicate-negation shape (derives [] byte-identically), the correction
shape (the ASSERTED name binds; the role word never welds), non-kin possessed heads ("my manager
is Priya Sharma" keeps its legacy reading), sortals ("my car is a Tesla Model 3" untouched), the
broad flag's certified flag-ON behavior (byte-parity), and the rollback arm
(SPINE_ROLE_HEAD_NO_BIND=false → the pre-fix baseline exactly, weld included).

Known residual (out of scope, pre-existing in BOTH flag states): a name the sm tagger mis-types
NOUN/ADJ (Cassian → NOUN, Winifred/Odile → ADJ acomp) is not a PROPER-NAME complement — the
naming chains decline and the legacy weld/has_state twin remains; and the INVERSE copula order
("Marla is my wife.") is a different frame (role in the attr slot), owned by the caller-seam
naming analyzer behind SPINE_NAMING_CHAIN.
"""
import datetime

import pytest

from src.extraction import linguistics as m

pytestmark = pytest.mark.skipif(
    not m.linguistics_available(),
    reason="spaCy linguistic layer unavailable (SPACY_MODEL unset) — spine deriver no-ops",
)

_REF = datetime.date(2026, 11, 7)


def _triples(facts):
    return [(f.subject, f.rel_type, f.object) for f in facts]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    # Deterministic per-test flags: the production default (broad chain dormant, no-bind ON)
    # unless a test pins otherwise.
    monkeypatch.setattr(m, "SPINE_NAMING_CHAIN", False)
    monkeypatch.setattr(m, "SPINE_ROLE_HEAD_NO_BIND", True)


def test_flag_default_is_on():
    # THE GATE (cortex rule: "flag exists" ≠ "feature works" — the DEFAULT is the deployed
    # behavior since the deployed stacks carry no SPINE_* env): a fresh module read must show
    # the no-bind rail armed.
    assert m.SPINE_ROLE_HEAD_NO_BIND is True


def test_f1_spouse_wife_binds_the_name_not_the_role_word():
    triples = _triples(m.derive_sentence_facts("My wife is Ophelia.", _REF))
    # THE NAME is the spouse entity (the seeded kin cue map: wife → spouse)
    assert ("ophelia", "spouse", "user") in triples, triples
    # the role surface files as an ALIAS on the named person (the walk resolves "my wife" → ophelia)
    assert ("ophelia", "also_known_as", "wife") in triples, triples
    # THE WELD IS GONE: the role word is never the bound entity
    assert ("wife", "spouse", "user") not in triples, triples
    assert not any(s == "wife" for s, _r, _o in triples), triples


@pytest.mark.parametrize("text,name,kin_rel,role", [
    ("My husband is Anselm.", "anselm", "spouse", "husband"),
    ("My spouse is Ophelia.", "ophelia", "spouse", "spouse"),
    ("My partner is Vesper.", "vesper", "spouse", "partner"),
    ("My mother is Perpetua.", "perpetua", "parent_of", "mother"),
    ("My father is Anselm.", "anselm", "parent_of", "father"),
    ("My daughter is Bryony.", "bryony", "child_of", "daughter"),
    ("My son is Casimir.", "casimir", "child_of", "son"),
    ("My brother is Anselm.", "anselm", "sibling_of", "brother"),
    ("My sister is Sarah.", "sarah", "sibling_of", "sister"),
    ("My sibling is Alex.", "alex", "sibling_of", "sibling"),
    ("My kid is Rolo.", "rolo", "child_of", "kid"),
    ("My child is Rolo.", "rolo", "child_of", "child"),
    ("My wife is Gladys.", "gladys", "spouse", "wife"),
])
def test_kin_role_list_binds_name_never_role_entity(text, name, kin_rel, role):
    # The F1 class across the whole kin role list (metadata-driven cue map — no in-code nouns).
    triples = _triples(m.derive_sentence_facts(text, _REF))
    assert (name, kin_rel, "user") in triples, triples
    assert (name, "also_known_as", role) in triples, triples
    assert not any(s == role for s, _r, _o in triples), triples


def test_negated_copula_binds_nothing():
    # "My wife is not Ophelia." — absence. Neither the name nor the role word binds.
    triples = _triples(m.derive_sentence_facts("My wife is not Ophelia.", _REF))
    assert ("ophelia", "spouse", "user") not in triples, triples
    assert ("wife", "spouse", "user") not in triples, triples


def test_predicate_negation_shape_stays_empty():
    # PRESERVE LIST: "is not named X but Y" — predicate negation asserts absence, owned by the
    # negation lanes; the frame derives nothing, byte-identically to the pre-fix baseline.
    triples = _triples(m.derive_sentence_facts(
        "My wife is not named Ophelia but Seraphina.", _REF))
    assert triples == [], triples


def test_correction_shape_binds_the_asserted_name():
    # The replacive-negation shape: the ASSERTED name (Seraphina) binds the spouse edge; the
    # role word never welds. (The REJECTED surface's edges are the appositive lanes' business —
    # the correction endpoint supersedes at ingest; only the asserted-name + no-weld invariants
    # are pinned here.)
    triples = _triples(m.derive_sentence_facts(
        "Actually my wife is Seraphina, not Ophelia.", _REF))
    assert ("seraphina", "spouse", "user") in triples, triples
    assert ("wife", "spouse", "user") not in triples, triples


def test_nonkin_role_stays_owner_opt_in():
    # The <role>_of novel-rel leg must NOT arm under the no-bind flag alone — a new default
    # never inherits a dormant broad flag's owner-opt-in surface.
    triples = _triples(m.derive_sentence_facts("My manager is Priya Sharma.", _REF))
    assert not any(r == "manager_of" for _s, r, _o in triples), triples
    assert not any(r == "also_known_as" and o == "manager" for _s, r, o in triples), triples
    # legacy certified reading unchanged
    assert ("user", "manager", "priya sharma") in triples, triples


def test_sortal_possessed_head_untouched():
    # Kin-scoped step-aside: a sortal head keeps its possessive/value reading byte-identically.
    triples = _triples(m.derive_sentence_facts("My car is a Tesla Model 3.", _REF))
    assert ("user", "car", "tesla model 3") in triples, triples
    assert not any(r == "car_of" for _s, r, _o in triples), triples


def test_rollback_off_reproduces_the_legacy_baseline():
    # SPINE_ROLE_HEAD_NO_BIND=false → byte-identical legacy behavior, weld included (the
    # documented rollback arm; this pin IS the pre-fix baseline).
    monkey_flags = pytest.MonkeyPatch()
    try:
        monkey_flags.setattr(m, "SPINE_ROLE_HEAD_NO_BIND", False)
        triples = _triples(m.derive_sentence_facts("My wife is Ophelia.", _REF))
        assert triples == [("wife", "spouse", "user")], triples
    finally:
        monkey_flags.undo()


def test_broad_flag_parity_kin_slice_byte_equivalent():
    # With the broad chain ON the certified flag-ON behavior is unchanged (the guard's
    # SPINE_NAMING_CHAIN arm runs first): name + alias + instance_of + gender, no role weld.
    monkey_flags = pytest.MonkeyPatch()
    try:
        monkey_flags.setattr(m, "SPINE_NAMING_CHAIN", True)
        triples = _triples(m.derive_sentence_facts("My wife is Ophelia.", _REF))
        assert ("ophelia", "spouse", "user") in triples, triples
        assert ("ophelia", "also_known_as", "wife") in triples, triples
        assert ("ophelia", "instance_of", "wife") in triples, triples
        assert ("wife", "spouse", "user") not in triples, triples
    finally:
        monkey_flags.undo()


def test_genitive_name_frames_still_bind():
    # The ungated genitive chain ("my wife's name is Ophelia") was already correct — the fix
    # must not disturb it (both apostrophe spellings).
    for text in ("My wife's name is Ophelia.", "My wifes name is Ophelia."):
        triples = _triples(m.derive_sentence_facts(text, _REF))
        assert ("ophelia", "spouse", "user") in triples, (text, triples)
        assert ("wife", "spouse", "user") not in triples, (text, triples)


# ── R2 (live bench 76e214a0 residual) — the attr/acomp PARSE-SHAPE COIN FLIP ─────────────────
# The sm tagger parses "My wife is Seren." with the name as attr, and "My wife is Isolde." with
# the SAME construction's name as PROPN acomp — the measure-NP dobj/npadvmod coin-flip class.
# The R1 complement scan missed acomp, the possessive weld edge shipped, and ingest linked it
# onto the seat's PRE-EXISTING [wife]-aliased weld entity (committed=1, Isolde orphaned). The
# acomp arm rides SPINE_ROLE_HEAD_NO_BIND (rollback keeps the pre-R2 baseline byte-identically)
# and the complement-neg skip covers the acomp-shape negation parse ("not" hangs on the NAME).


def test_acomp_parse_shape_binds_the_name():
    # "My wife is Isolde." — Isolde parses PROPN acomp; the bind must not depend on the coin flip.
    triples = _triples(m.derive_sentence_facts("My wife is Isolde.", _REF))
    assert ("isolde", "spouse", "user") in triples, triples
    assert ("isolde", "also_known_as", "wife") in triples, triples
    assert not any(s == "wife" for s, _r, _o in triples), triples


def test_acomp_parse_shape_negated_complement_binds_nothing():
    # "My wife is not Isolde." — negation preserves the NAME-absence semantic: the negated name
    # must never bind. NOTE the sm tagger parses this shape with Isolde as ADJ acomp and "not"
    # neg on the copula, so the frame declines at the PROPN guard and the certified PRESUPPOSITION
    # baseline rows (the role-presupposition weld, cf. flag-OFF "my wife is not Ophelia.") may
    # still appear — the tagger-boundary residual documented in the module docstring. Only the
    # name bind is pinned here.
    triples = _triples(m.derive_sentence_facts("My wife is not Isolde.", _REF))
    assert ("isolde", "spouse", "user") not in triples, triples
    assert ("isolde", "also_known_as", "wife") not in triples, triples


def test_acomp_arm_rollback_keeps_pre_r2_baseline():
    monkey_flags = pytest.MonkeyPatch()
    try:
        monkey_flags.setattr(m, "SPINE_ROLE_HEAD_NO_BIND", False)
        triples = _triples(m.derive_sentence_facts("My wife is Isolde.", _REF))
        # the documented pre-R2 baseline for the acomp shape: the role-word weld, name in residue
        assert triples == [("wife", "spouse", "user")], triples
    finally:
        monkey_flags.undo()
