"""R9 pins — fog-family critic re-gate (RG-A2 HIGH + RG-G-M4 contract).

RG-A2 (HIGH): "My wife is Obsidian." — the stone name parses ADJ acomp (the sm tagger's
measure-NP coin-flip class), so the copula-name chain's PROPN gate missed it and the
state-attribution lane mangled the ROLE WORD into a subject-entity:
  (wife, has_state, obsidian)   ← the type gate then refused it identically before and
                                  after the repair stamp — the repaired-type path was
                                  unreachable for exactly the names that need it.
THE FIX (kin-frame precedence): a copula with a 1st-person-possessed KIN-TAIL role routes
to the kin binding whenever the complement is a NAME-CANDIDATE surface (PROPN as before,
or a capitalized-medial det-free token however the tagger labelled it — orthography is the
structural signal; the TYPE decision stays downstream with the adjudication path):
  (obsidian, spouse, user) + (obsidian, also_known_as, wife)   ← the Petrel/Wren shape.
Kin-scoped: "my car is fast" keeps its state reading; non-kin roles keep the PROPN gate.

RG-G-M4 contract pins (pieces 1+2): the residence attribute lane lands ONLY through
  deriver live_at (whole address) → ingest morphology fold live_at→lives_at
  → SCALAR tail routing → F4/F6 residence-current supersede on attr collision.
Piece (1) needs the deriver's whole-address live_at edge on BOTH plant and move shapes
(R8 pins carry the shapes; here we pin the FOLD CONTRACT), and piece (2) needs the
decider to SUPERSEDE (not drop) a mutable-residence attr collision for an affirmed,
current, equal-authority incoming — the R1-finding-5 drop is dead at HEAD.
"""

import pytest

from src.extraction.linguistics import derive_sentence_facts
from src.ontology.canonical import normalize_rel


def _edges(sentence):
    return derive_sentence_facts(sentence, None) or []


def _triples(edges):
    out = []
    for e in edges:
        d = e if isinstance(e, dict) else getattr(e, "__dict__", {})
        out.append((str(d.get("subject", "")).lower(), str(d.get("rel_type", "")).lower(),
                    str(d.get("object", "")).lower()))
    return out


# ── RG-A2 ────────────────────────────────────────────────────────────────────────────

def test_r9_kin_copula_adtagged_complement_binds_the_name():
    # "Obsidian" parses ADJ acomp — the tagger's coin-flip class. The kin frame must bind
    # the NAME (spouse to user + the role as alias), never a state on the role word.
    trips = _triples(_edges("My wife is Obsidian."))
    assert ("obsidian", "spouse", "user") in trips
    assert ("obsidian", "also_known_as", "wife") in trips
    assert not any(r == "has_state" and s == "wife" for s, r, o in trips), trips


def test_r9_kin_copula_role_word_never_becomes_subject_entity():
    for sentence, name in (("My wife is Wren.", "wren"), ("My husband is Oakley.", "oakley")):
        trips = _triples(_edges(sentence))
        assert (name, "spouse", "user") in trips, (sentence, trips)
        assert not any(s == ("wife" if "wife" in sentence else "husband")
                       for s, r, o in trips), (sentence, trips)


def test_r9_state_lane_keeps_nonkin_and_lowercase_readings():
    # Kin-scoped decline only: a genuine state on a non-kin possessed thing is untouched,
    # and a LOWERCASE complement is an ordinary adjective reading, never a name bind.
    trips = _triples(_edges("My car is fast."))
    assert ("user", "car", "fast") in trips, trips
    assert not any(r in ("spouse", "also_known_as") for s, r, o in trips), trips
    trips2 = _triples(_edges("My wife is stubborn."))
    # a lowercase complement is NOT a name: nothing binds it (no alias leg, no spouse on
    # the complement surface); the standing role weld on the role word is unchanged
    assert not any(s == "stubborn" for s, r, o in trips2), trips2
    assert not any(r == "also_known_as" for s, r, o in trips2), trips2
    assert ("wife", "spouse", "user") in trips2, trips2


# ── RG-G-M4 piece 1: the fold contract ───────────────────────────────────────────────

def test_r9_residence_rel_folds_lives_at_family_to_one_identity():
    # ingest resolve_seeded_by_morphology('live_at') hits the seeded lives_at because the
    # normalized forms coincide — the deriver's verb-derived rel and the seeded SCALAR-tailed
    # canonical are the SAME identity, so the address lands on the lives_at attribute lane.
    assert normalize_rel("live_at") == normalize_rel("lives_at")
    assert normalize_rel("lived_at") == normalize_rel("lives_at")
    # ...and the preposition stays load-bearing (city vs address never fold together).
    assert normalize_rel("live_in") != normalize_rel("lives_at")


def test_r9_move_and_plant_emit_the_same_whole_address_live_at_shape():
    plant = _triples(_edges("We live at 22 Cordwainer's Stair in Weftmoor."))
    move = _triples(_edges("We've moved to 31 Scrimgeour's Lane in Weftmoor."))
    assert ("user", "live_at", "22 cordwainer's stair in weftmoor") in plant
    assert ("user", "live_at", "31 scrimgeour's lane in weftmoor") in move
    # the city is a separate WHOLE lives_in row, never shredded inside the address phrase
    assert ("user", "lives_in", "weftmoor") in move
    for trips in (plant, move):
        for s, r, o in trips:
            if r == "live_at":
                assert not o.endswith("heath"), (r, o)


# ── RG-G-M4 piece 2: the collision decider supersedes ────────────────────────────────

def test_r9_residence_attr_collision_supersedes_not_drops():
    from src.api import main as api_main
    from src.api.main import _decide_supersede_or_coexist
    verdict = _decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": False,
         "rel_type": "lives_at", "temporal_status": "now", "polarity": "affirmed"},
        {"fact_provenance": "user_stated"},
        None,
    )
    assert verdict == "supersede_existing", verdict


def test_r9_residence_attr_collision_still_drops_lower_authority():
    from src.api.main import _decide_supersede_or_coexist
    verdict = _decide_supersede_or_coexist(
        {"fact_provenance": "llm_learned", "is_correction": False,
         "rel_type": "lives_at", "temporal_status": "now", "polarity": "affirmed"},
        {"fact_provenance": "user_stated"},
        None,
    )
    assert verdict == "drop_incoming", verdict


def test_r9_nonresidence_state_collision_still_coexists():
    # the F6 subset is residence-scoped: a general state attr keeps blanket coexist
    from src.api.main import _decide_supersede_or_coexist
    verdict = _decide_supersede_or_coexist(
        {"fact_provenance": "user_stated", "is_correction": False,
         "rel_type": "has_mood", "temporal_status": "now", "polarity": "affirmed"},
        {"fact_provenance": "user_stated"},
        None,
    )
    assert verdict == "coexist", verdict
