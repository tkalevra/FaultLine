"""Fog-family R7 (lane C, critic R5-4): possessive-address capture must be DETERMINISTIC.

THE WOUND (measured live, bench seat, image 4ddbff092225): "We live at 31
Scrimgeour's Lane in Weftmoor." dropped the city+number ENTIRELY on 1-of-3
same-family turns (committed=1, junk related_to only) while identical shapes captured
full twice. The deriver is byte-deterministic in-process (20/20 identical signatures,
shuffled order + jittered timing) - the variance is the ATOMIZER CUT: with
SPINE_DETERMINISTIC_SEGMENTATION OFF the LLM atomizer rewrote the turn FIRST and the
deterministic spine derived from the atoms; on the drop turn the cut surfaced the
street WITHOUT its nummod and locative PP (the harvested alias was exactly
"scrimgeour's lane") and the residence value was unrecoverable.

THE FIX: the deterministic-first decomposer is now the DEFAULT (det units always
survive; the atomizer escalates only on residual-signal units and its atoms are
unioned on top - the capture can only grow, never lose). These pins hold the
deterministic capture contract on the exact wound family.
"""
import pytest

from src.extraction.linguistics import (derive_sentence_facts, segment_clauses,
                                        decompose_assertions)


def _edges(sentence):
    return [e if isinstance(e, dict) else dict(getattr(e, "__dict__", {}))
            for e in (derive_sentence_facts(sentence, None) or [])]


def _live_at(sentence):
    return [e.get("object") for e in _edges(sentence) if e.get("rel_type") == "live_at"]


def test_deterministic_segmentation_is_the_default():
    # The R5-4 cure: the atomizer cut must not sit between the turn and the deriver by
    # default. Flag-flip ships with a rollback lever (env=false restores legacy).
    from src.api import main as api_main
    assert api_main.SPINE_DETERMINISTIC_SEGMENTATION is True


def test_scrimgeour_wound_shape_captures_full_address():
    assert _live_at("We live at 31 Scrimgeour's Lane in Weftmoor.") \
        == ["31 scrimgeour's lane in weftmoor"]


def test_scrimgeour_shape_mints_no_junk_twin():
    twins = [e for e in _edges("We live at 31 Scrimgeour's Lane in Weftmoor.")
             if e.get("rel_type") in ("related_to", "instance_of", "located_in")]
    assert twins == []


def test_repeat_loop_is_single_variant():
    # The nondeterminism pin: N repetitions, ONE edge-signature variant - and the
    # variant carries the full address value.
    s = "We live at 31 Scrimgeour's Lane in Weftmoor."
    sigs = set()
    for _ in range(5):
        edges = _edges(s)
        sigs.add(tuple(sorted((e.get("rel_type"), e.get("object")) for e in edges)))
    assert len(sigs) == 1
    (sig,) = sigs
    assert any(r == "live_at" and o == "31 scrimgeour's lane in weftmoor"
               for r, o in sig)


def test_deterministic_units_carry_the_full_address():
    # The decomposer (what the deriver now reads FIRST) never drops the number or the
    # city: every deterministic unit of the wound sentence still contains the full
    # address surface, so no atomizer cut is needed to recover it.
    s = "We live at 31 Scrimgeour's Lane in Weftmoor."
    units = list(segment_clauses(s) or [])
    units = units + [u for u in (decompose_assertions(s) or []) if u not in units]
    assert units
    assert any("31 scrimgeour's lane in weftmoor" in u.lower() for u in units)


def test_cordwainer_control_full_value_unchanged():
    assert _live_at("We live at 22 Cordwainer's Stair in Weftmoor.") \
        == ["22 cordwainer's stair in weftmoor"]


def _n_loop_signature(sentence, n):
    sigs = set()
    for _ in range(n):
        edges = _edges(sentence)
        sigs.add(tuple(sorted((e.get("rel_type"), e.get("object")) for e in edges)))
    return sigs


def test_poss_street_plain_city_donor_is_whole_and_stable():
    # Critic R5-4 addendum: the nondeterminism reproduced at STREET-POSSESSIVE level
    # (possessive street feeding the capture, plain city) - the donor variant the
    # original loop harness did not isolate. Whole value, ONE signature across N reps.
    s = "We live at 31 Scrimgeour's Lane in Weftmoor."
    sigs = _n_loop_signature(s, 8)
    assert len(sigs) == 1
    (sig,) = sigs
    assert any(r == "live_at" and o == "31 scrimgeour's lane in weftmoor"
               for r, o in sig)
    assert not any(r == "related_to" for r, _o in sig)


def test_poss_street_poss_city_donor_is_whole_and_stable():
    # The harder donor variant: possessive street AND possessive city - both
    # genitives are PLACE-NAME members, never possessive bindings, and the value
    # carries the whole address with no junk twin.
    s = "We live at 22 Tinker's Court in Pilgrim's Reach."
    sigs = _n_loop_signature(s, 8)
    assert len(sigs) == 1
    (sig,) = sigs
    assert any(r == "live_at" and o == "22 tinker's court in pilgrim's reach"
               for r, o in sig)
    assert not any(r == "related_to" for r, _o in sig)
