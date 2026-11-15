"""P2 RECALL-FIDELITY LANE - kinship-NAME corrections are structurally addressable.

Measured wound (benchmarks/walk_litmus, DEV/BUGS/BUG-walklitmus-correction-supersession-
3-classes-20261106.md, kinship class): "Correction: my father is named Jonas, not Dario
like I said before." welded BOTH names as parent_of users (jonas AND dario parent_of user)
plus a junk (correction, also_known_as, jonas) edge, and emitted NO preferred_label repair
edge - so _detect_name_repair_correction (which keys on a preferred_label=True edge) never
fired and the stored father name stayed Dario forever (bench kinship corrections 0/4).

Fix: the deriver's PASSIVE-NAME chain (_chain_passive_name) recognises "my <kin> is named
<NEW>(, not <OLD>)" frames with the SAME machinery the genitive repair uses
(corrective_negation_alternative): the REJECTED name carries the kin relation with a
preferred_label repair edge to the new name, the frame's subtree is suppressed from rival
welds, and the kin noun binds as a role alias.

FLAG SPLIT (recall-lane critic round 1, owner doctrine "user corrections are Class A
truth"): the REPAIR arm arms on SPINE_CORRECTION_NAME_REPAIR (DEFAULT ON - a correction
must never die "Incomplete correction" because an unrelated naming chain is dormant);
the PLAIN passive-naming arm and the genitive/copula chains stay gated behind
SPINE_NAMING_CHAIN (DEFAULT OFF, owner opt-in) exactly as before the split. These pins
run the repair through DEFAULT env (no SPINE_NAMING_CHAIN) to prove the default-on path,
and prove both gating boundaries.

Deterministic, subject-agnostic, no name/kin word lists beyond the existing kinship map.

fail-on-old: before the fix no preferred_label edge exists for the passive frame and both
names weld as parent_of; before the flag split the repair was inert on every deployed
stack (SPINE_NAMING_CHAIN defaults false and no compose env arms it).
"""
import datetime
import importlib
import os

import pytest

import src.extraction.linguistics as ling


def _reload(**env):
    # always pin BOTH flags explicitly so a prior test's env cannot leak across reloads
    env.setdefault("SPINE_NAMING_CHAIN", None)
    env.setdefault("SPINE_CORRECTION_NAME_REPAIR", None)
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(ling)


def _model_available() -> bool:
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm")
    return m.linguistics_available()


_HAS_MODEL = _model_available()
requires_model = pytest.mark.skipif(not _HAS_MODEL,
                                    reason="en_core_web_sm not installed in test env")

_REF = datetime.datetime(2023, 5, 30)


def _facts(text, **env):
    m = _reload(LINGUISTIC_LAYER="true", SPACY_MODEL="en_core_web_sm", **env)
    return list(m.derive_sentence_facts(text, _REF))


def _get(f, k, default=None):
    if isinstance(f, dict):
        return f.get(k, default)
    return getattr(f, k, default)


def _edge(facts, subject, rel, obj):
    for f in facts:
        if (str(_get(f, "subject", "")).lower() == subject
                and _get(f, "rel_type") == rel
                and str(_get(f, "object", "")).lower() == obj):
            return f
    return None


_REPAIR_TEXT = "Correction: my father is named Jonas, not Dario like I said before."
_PLAIN_TEXT = "My father is named Jonas."


@requires_model
def test_repair_arm_is_default_on_without_naming_chain():
    # THE S1 CONTRACT: the correction-critical repair works on DEFAULT env - no
    # SPINE_NAMING_CHAIN (which is default-off and un-armed on every deployed stack)
    facts = _facts(_REPAIR_TEXT)
    # the REJECTED name carries the kin relation (the anchor the naming intercept walks)
    assert _edge(facts, "dario", "parent_of", "user"), facts
    # the repair edge the naming-repair intercept keys on (subject=old, object=new)
    repair = _edge(facts, "dario", "pref_name", "jonas")
    assert repair and _get(repair, "preferred_label"), facts
    # the kin noun binds as a role alias on the anchor (referent resolution)
    assert _edge(facts, "dario", "also_known_as", "father"), facts
    # the NEW name must NOT weld as a rival parent_of (frame suppression)
    assert not _edge(facts, "jonas", "parent_of", "user"), facts
    # and the correction meta-noun must not weld onto the new name
    assert not _edge(facts, "correction", "also_known_as", "jonas"), facts


@requires_model
def test_repair_arm_owner_opt_out():
    # SPINE_CORRECTION_NAME_REPAIR=false = the legacy gated behavior BYTE-FOR-BYTE: no
    # correction-critical emission (no preferred_label repair edge, no role alias) - the
    # name-repair intercept declines and the correction dies "Incomplete correction".
    # The rival welds ((dario, parent_of, user) etc.) are the MEASURED legacy wound of the
    # ungated frame and return exactly as they do today with SPINE_NAMING_CHAIN=false -
    # this opt-out is the owner's reversible escape hatch, not a proposed default.
    facts = _facts(_REPAIR_TEXT, SPINE_CORRECTION_NAME_REPAIR="false")
    for f in facts:
        assert not _get(f, "preferred_label"), facts
    assert not _edge(facts, "dario", "pref_name", "jonas"), facts
    assert not _edge(facts, "dario", "also_known_as", "father"), facts


@requires_model
def test_plain_arm_stays_gated_on_naming_flag():
    # plain passive naming is NOT correction-critical: default env -> no binding
    # VEHICLE UPDATE (wound-map round 3, 2026-11-13, by design): the s2 residual's
    # PLANT-SIDE ROLE SLOT (SPINE_NAMING_PLANT_SLOT, default ON) now files the SLOT edge
    # (jonas, parent_of, user) in the default state — that is the fix, not a leak: the
    # chain's plain arm STAYS SPINE_NAMING_CHAIN-gated and the slot rides its own flag,
    # whose OFF state restores this pin's original byte-parity exactly. Mechanism
    # assertion unchanged: the repair flag still arms nothing here.
    facts = _facts(_PLAIN_TEXT)
    assert _edge(facts, "jonas", "parent_of", "user"), facts          # the plant slot
    assert _edge(facts, "jonas", "also_known_as", "father"), facts    # the role alias leg
    facts_parity = _facts(_PLAIN_TEXT, SPINE_NAMING_PLANT_SLOT="false")
    assert not _edge(facts_parity, "jonas", "parent_of", "user"), facts_parity
    # owner opt-in arms it
    facts2 = _facts(_PLAIN_TEXT, SPINE_NAMING_CHAIN="true")
    assert _edge(facts2, "jonas", "parent_of", "user"), facts2
    assert _edge(facts2, "jonas", "also_known_as", "father"), facts2
    # no repair edge without a corrective negation
    for f in facts2:
        assert not _get(f, "preferred_label"), facts2


# ── F-R2-1 (critic round 2): NAME-PARSE-FRAGILE repair admission ────────────────────────────
# The corrective-alternative detector keys on the rejected surface ATTACHING to the asserted
# nominal (appos/conj/amod children). MEASURED mis-parses on kin frames put the rejected
# surface ELSEWHERE entirely (Rosalind/VERB/ROOT with the 'Correction:' prefix; VERB/oprd
# sibling of the asserted name; appos of the 'Correction' meta-noun) - so the repair edge
# never fired and the correction died "Incomplete correction - relationship type not
# identified" (the pre-lane Class-A wound, silently lost). The bounded fix admits, in the
# REPAIR arm ONLY, the neg-governed title-case alpha token: 'not' reliably sits strictly
# between the asserted name and the rejected surface in ALL observed parses, and the
# rejected surface is the first title-case alpha token after that 'not' - regardless of
# its dep_/pos_ tag (the mis-tag IS the failure). The critic's exact MISS list:
_CRITIC_MISS_PAIRS = [
    ("mother", "Imogen", "Rosalind", "parent_of"),
    ("father", "Imogen", "Rosalind", "parent_of"),
    ("sister", "Imogen", "Rosalind", "sibling_of"),
    ("brother", "Casimir", "Bronislaw", "sibling_of"),
    ("daughter", "Odile", "Beatrix", "child_of"),
]

_KIN_REL = {"mother": "parent_of", "father": "parent_of", "sister": "sibling_of",
            "brother": "sibling_of", "daughter": "child_of"}


def _repair_tuple_set(facts):
    return sorted((str(_get(f, "subject")).lower(), _get(f, "rel_type"),
                   str(_get(f, "object")).lower(), bool(_get(f, "preferred_label")))
                  for f in facts)


@pytest.mark.parametrize("kin,new_name,old_name,rel", _CRITIC_MISS_PAIRS)
@requires_model
def test_critic_miss_pairs_repair_through_mistagged_parse(kin, new_name, old_name, rel):
    # RED-AT-BASELINE: before the neg-governed fallback these pairs emitted NO repair edge
    # (anchor, pref_name, alias all absent) - the correction was silently lost.
    text = f"Correction: my {kin} is named {new_name}, not {old_name} like I said before."
    facts = _facts(text)
    lo, hi = old_name.lower(), new_name.lower()
    assert _edge(facts, lo, rel, "user"), (text, _repair_tuple_set(facts))
    repair = _edge(facts, lo, "pref_name", hi)
    assert repair and _get(repair, "preferred_label"), (text, _repair_tuple_set(facts))
    assert _edge(facts, lo, "also_known_as", kin), (text, _repair_tuple_set(facts))
    # the asserted name must NOT weld as a rival parent/sibling/child
    assert not _edge(facts, hi, rel, "user"), (text, _repair_tuple_set(facts))
    # and the meta-noun must not weld onto the asserted name
    assert not _edge(facts, "correction", "also_known_as", hi), (text, _repair_tuple_set(facts))


@requires_model
def test_miss_pair_bare_parse_repair_no_correction_prefix():
    # the critic's other measured shape: WITHOUT the 'Correction:' prefix the rejected
    # surface lands as a different mis-tag (ADJ-oprd class) - same fallback must cover it
    facts = _facts("my mother is named Imogen, not Rosalind.")
    assert _edge(facts, "rosalind", "parent_of", "user"), _repair_tuple_set(facts)
    repair = _edge(facts, "rosalind", "pref_name", "imogen")
    assert repair and _get(repair, "preferred_label"), _repair_tuple_set(facts)
    assert _edge(facts, "rosalind", "also_known_as", "mother"), _repair_tuple_set(facts)


@requires_model
def test_miss_pair_owner_opt_out_stays_legacy():
    # SPINE_CORRECTION_NAME_REPAIR=false = byte-for-byte the gated past: no repair edges
    facts = _facts("Correction: my mother is named Imogen, not Rosalind like I said before.",
                   SPINE_CORRECTION_NAME_REPAIR="false")
    for f in facts:
        assert not _get(f, "preferred_label"), facts
    assert not _edge(facts, "rosalind", "pref_name", "imogen"), facts
    assert not _edge(facts, "rosalind", "also_known_as", "mother"), facts


@requires_model
def test_repair_widening_never_leaks_into_plain_arm():
    # the widening lives ONLY in the repair arm: a plain passive naming (no corrective
    # negation) must behave exactly as before the widening, in BOTH flag states
    # VEHICLE UPDATE (wound-map round 3, 2026-11-13, by design): the plant-side role slot
    # (SPINE_NAMING_PLANT_SLOT default ON) files the slot in the default state; the pin's
    # byte-parity leg (what the repair widening must never change) now reads under
    # SPINE_NAMING_PLANT_SLOT=false — the slot flag's OFF state is the pre-round-3
    # behavior this pin has always guarded. The repair flag STILL leaks nothing: no
    # preferred_label in any state.
    plain = "My mother is named Imogen."
    facts = _facts(plain)                                   # default: slot ON, chain OFF
    assert _edge(facts, "imogen", "parent_of", "user"), facts          # the plant slot
    assert not any(_get(f, "preferred_label") for f in facts), facts
    facts_parity = _facts(plain, SPINE_NAMING_PLANT_SLOT="false")
    assert not _edge(facts_parity, "imogen", "parent_of", "user"), facts_parity
    assert not any(_get(f, "preferred_label") for f in facts_parity), facts_parity
    facts2 = _facts(plain, SPINE_NAMING_CHAIN="true")       # owner opt-in: plain binding
    assert _edge(facts2, "imogen", "parent_of", "user"), facts2
    assert _edge(facts2, "imogen", "also_known_as", "mother"), facts2
    assert not any(_get(f, "preferred_label") for f in facts2), facts2


@pytest.mark.parametrize("asserted,rejected", [("Jonas", "Dario"), ("Jonas", "Agnes"),
                                               ("Agnes", "Jonas")])
@requires_model
def test_jonas_class_pairs_byte_identical(asserted, rejected):
    # pairs the appos/conj admission ALREADY resolves must emit the IDENTICAL fact set
    # after the widening (the fallback must never run for them, never add an edge)
    text = (f"Correction: my father is named {asserted}, not {rejected} "
            "like I said before.")
    facts = _facts(text)
    lo = rejected.lower()
    expected = sorted([
        ("user", "has_state", "say", False),
        (lo, "parent_of", "user", False),
        (lo, "pref_name", asserted.lower(), True),
        (lo, "also_known_as", "father", False),
    ])
    assert _repair_tuple_set(facts) == expected, _repair_tuple_set(facts)


@requires_model
def test_jonas_marta_wound_also_repairs():
    # same wound class, different junk signature: 'Marta' attached appos of the
    # 'Correction' meta-noun, so baseline emitted (correction, also_known_as, marta) and
    # NO repair - the widening must repair AND kill the weld
    facts = _facts("Correction: my father is named Jonas, not Marta like I said before.")
    assert _edge(facts, "marta", "parent_of", "user"), _repair_tuple_set(facts)
    repair = _edge(facts, "marta", "pref_name", "jonas")
    assert repair and _get(repair, "preferred_label"), _repair_tuple_set(facts)
    assert _edge(facts, "marta", "also_known_as", "father"), _repair_tuple_set(facts)
    assert not _edge(facts, "correction", "also_known_as", "marta"), _repair_tuple_set(facts)


@requires_model
def test_negation_but_shape_keeps_non_contradiction_guarantee():
    # 'is not named X but Y' derives NOTHING today (the pre-pass skips predicate negation
    # and the LLM lane owns that shape) - the widening must not start deriving from it
    facts = _facts("my father is not named Dario but Jonas.")
    assert _repair_tuple_set(facts) == [], facts
    assert not _edge(facts, "dario", "parent_of", "user"), facts
    assert not _edge(facts, "jonas", "parent_of", "user"), facts


@requires_model
def test_genitive_name_repair_control_unchanged():
    # the sibling repair (pre-existing genitive chain) is SPINE_NAMING_CHAIN territory:
    # owner opt-in arms it and it keeps working untouched
    facts = _facts("Wait - actually my sister's name is Soto, not Wrenna. I keep mixing "
                   "them up.", SPINE_NAMING_CHAIN="true")
    assert _edge(facts, "wrenna", "sibling_of", "user"), facts
    repair = _edge(facts, "wrenna", "pref_name", "soto")
    assert repair and _get(repair, "preferred_label"), facts
