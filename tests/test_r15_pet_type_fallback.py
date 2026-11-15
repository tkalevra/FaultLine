"""R15/R16 - pet-lane type fallback (critic R14b-M2 + R15-M2).

The fresh pet plant (We have a genet named Sable) type_refused committed=0: GLiNER
could not type the obscure animal word and the pet lane refused honestly. The pet lane
HAS the context to resolve it - the clause is pet-shaped - so the object type falls
back to Animal by LANE CONTEXT (declared-in-pet-clause rule): the GUARD is the signal,
never a word list.

R16 widening (R15-M2, the live proof): GLiNER typing is round-to-round variance - it
typed genet OBJECT in R14b and LOCATION in R15 - so the fallback is TYPE-AGNOSTIC on
the non-Animal set, and the lane identity is the POSSESSION FAMILY (owns OR has_pet -
the codebase own _POSSESSION_CLASS; the named-instance capture emits owns, which is
the rel the R15 live refusal actually carried: owns accepts Animal/Object/Organization
there). Retype -> revalidate -> admit whenever the family identity is present,
regardless of which non-Animal type GLiNER picked.

Honest marker ingest.pet_type_from_lane_context on every fallback; the granite rule is
preserved (non-pet clauses never enter - their rels are not in the family). Possessive
pet shapes share the seam. A cleanly typed pet word (tarsier) is untouched - the
fallback only fires on a refusal whose object type is not already Animal. Refusals
that survive re-validation still run the original refusal body verbatim.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import main as api_main  # noqa: E402
from src.wgm.gate import TYPE_REFUSED_STATUS  # noqa: E402


def _fallback_block():
    import inspect
    src = inspect.getsource(api_main._ingest_impl)
    start = src.find("PET-LANE TYPE FALLBACK")
    assert start > 0, "fallback block missing"
    return src[start:start + 3400]


def test_r16_guard_is_possession_family_and_type_agnostic():
    """The guard is the POSSESSION FAMILY identity (owns OR has_pet - the codebase own
    _POSSESSION_CLASS) and the type condition is TYPE-AGNOSTIC: anything non-Animal
    falls back (Location-typed pet names included - the R15 live failure)."""
    import inspect
    src = inspect.getsource(api_main._ingest_impl)
    assert 'canonical_rel_type in ("owns", "has_pet")' in src
    assert "final_object_type != " + '"Animal"' in src
    assert 'validation.get("role") == "tail"' in src
    assert '"Animal" in list(validation.get("allowed") or [])' in src
    assert "ingest.pet_type_from_lane_context" in src
    # no species vocabulary in the executable guard chain
    guard_lines = [ln for ln in _fallback_block().splitlines()
                   if ln.strip().startswith(("if status", "and "))]
    guard_text = chr(10).join(guard_lines).lower()
    for species in ("genet", "tarsier", "selkie", "kelpie"):
        assert species not in guard_text, species


def test_r16_location_typed_shape_reaches_fallback():
    """The R15 failure shape, as it occurred: the pet rel carried owns and the object
    type was LOCATION - the widened guard admits that exact combination (the family
    check passes, the type check passes because Location != Animal)."""
    import inspect
    src = inspect.getsource(api_main._ingest_impl)
    start = src.find('if status == TYPE_REFUSED_STATUS and canonical_rel_type in ("owns", "has_pet")')
    head = src[start:start + 400]
    assert 'validation.get("role") == "tail"' in head
    assert "final_object_type != " + '"Animal"' in head


def test_r15_fallback_revalidates_and_clears_stale_refusal():
    """On pass the stale refusal record is cleared (the 94 mirror) and the re-run
    validation replaces the refused one; on continued refusal the original refusal
    body still runs (honest decline preserved)."""
    fb = _fallback_block()
    assert "_clear_edge_refusal(db, fact_subject, canonical_rel_type," in fb
    assert "_pet_reval.get(" + '"status"' + ") != TYPE_REFUSED_STATUS" in fb
    assert "object_type=" + '"Animal"' in fb


def test_r16_non_pet_clauses_never_enter():
    """The granite rule: the fallback's FIRST condition is the possession family - a
    spouse or scalar clause (any non-family rel) can never reach the fallback
    regardless of typing."""
    import inspect
    src = inspect.getsource(api_main._ingest_impl)
    marker = 'if status == TYPE_REFUSED_STATUS and canonical_rel_type in ("owns", "has_pet")'
    assert marker in src
    fb = src[src.find(marker):]
    assert "ingest.pet_type_from_lane_context" in fb


def test_r15_gate_status_constant_unchanged():
    """The refusal vocabulary is untouched - the fallback composes with the existing
    gate contract rather than replacing it."""
    assert TYPE_REFUSED_STATUS == "type_refused"
