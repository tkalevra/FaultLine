"""P1 RECALL-FIDELITY LANE - supersession-refuse-then-insert-anyway is closed, both arms.

Measured wound (benchmarks/walk_litmus, DEV/BUGS/BUG-walklitmus-correction-supersession-
3-classes-20261106.md, Class B): a predicate-drift correction ("what I listen to is
afrobeats now, NOT trip hop") whose re-derived rel_type matched NO stored row returned
status="failed" ("Old relational fact not found: listen_to") - while the MCP
intent-independent harvest had ALREADY committed the new value UNSUPERSEDED before the
correction route ran. One failure mode (no update) was silently converted into a worse
one: a live contradiction the seat held and the response denied (bench correction
honoring 1/3).

The authority ruling (user-is-truth, both arms honest) fixed BOTH halves:
  1. ORDER (mcp/server.py): the RETRACTION/CORRECTION route now runs BEFORE the
     intent-independent harvest; the harvest then runs AFTER the route for the OTHER
     buried facts (dedup-safe: the ingest upsert is upgrade-only on provenance). QUERY
     keeps its harvest-before-order untouched.
  2. REFUSAL (src/api/main.py correct_fact): where the whole lookup cascade refuses the
     supersession, a CORRECTION commits its new value MARKED - a countable
     retraction_outcomes row (detected_as_retraction=False, retraction_method=
     'correction_unresolved_supersession', was_correct=False) - and the response says so:
     status="corrected_unresolved", facts_superseded=0. Never a silent contradiction;
     any commit error still falls through to the honest refusal (nothing inserted).
     Flag CORRECTION_UNRESOLVED_SUPERSESSION (default ON).

These pins are STRUCTURAL (no DB in unit scope): they pin the order, the arm-before-
refusal placement, the countable marker, the status vocabulary, and the fail-safe
fallthrough - the behavioral proof rides the walk-litmus bench BEFORE/AFTER.

fail-on-old: before the fix the correction route ran after the harvest and the refusal
arm did not exist, so "failed" was returned while the value sat inserted unsuperseded.
"""
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_correction_route_runs_before_harvest():
    src = (_ROOT / "src" / "mcp" / "server.py").read_text()
    # the pre-route harvest no longer swallows CORRECTION/RETRACTION turns
    assert 'intent not in ("STATEMENT", "RETRACTION", "CORRECTION")' in src
    # the correction route captures its result and the harvest runs AFTER it
    i_route = src.find("_corr_result = await retract_fact_tool(")
    i_harvest_after = src.find("await _harvest_turn_facts(text, user_id, source=_ing_source)",
                               i_route)
    assert i_route > 0 and i_harvest_after > i_route


def test_unresolved_supersession_arm_precedes_refusal():
    src = (_ROOT / "src" / "api" / "main.py").read_text()
    i_arm0 = src.find("# == CORRECTION REFUSAL - UNRESOLVED SUPERSESSION")
    i_arm = src.find('status="corrected_unresolved"')
    i_refusal = src.find('Old {dimension.lower()} fact not found')
    assert i_arm0 > 0 and i_arm > i_arm0, "unresolved-supersession arm missing"
    assert i_refusal > i_arm > 0, "arm must precede the honest refusal (fail-safe fallthrough)"
    # the arm is flag-gated and the marker is countable
    assert "CORRECTION_UNRESOLVED_SUPERSESSION" in src
    assert "correction_unresolved_supersession" in src
    assert "retraction_outcomes" in src[i_arm0:i_refusal]


def test_corrected_unresolved_status_vocabulary():
    from src.api.models import FactCorrectionResponse
    r = FactCorrectionResponse(status="corrected_unresolved", facts_superseded=0,
                               new_rel_type="listen_to", new_value="afrobeats",
                               old_rel_type="have", old_value="trip hop",
                               dimension="OBJECT", confidence=0.9,
                               message="Stored the new value")
    assert r.status == "corrected_unresolved" and r.facts_superseded == 0


def test_flag_defaults_on():
    import src.api.main as main
    assert main._flag_on("CORRECTION_UNRESOLVED_SUPERSESSION", default="true") is True
    assert main.NEVER_STATED_VALUE_GATE is True
    assert main.COUNT_SCALAR_MEASURE_OVERRIDE is True
