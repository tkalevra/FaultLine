"""ENGINE STRUCTURE IS USABLE ON DERIVATION; INFERRED USER MEMORY IS STILL VALIDATED.

OWNER RULING (2026-08-27):
    "those aspects that are ours to control should not require validation to become useable, they
     should be hinged on the growth. The only aspect requiring 'validation' should be the user's
     actual memory ie. inferred class C, otherwise everything is provided and tabled straight out."
    "We should rely on the actual growth to improve the L4 and useability, not wait for x
     confirmations. The ability for users to correct that shape directly is the metric, ie. we rely
     on surfacing to the user to be able to correct if it's wrong."

THE LINE: a cue class / rel_type the engine derived is a SHELF (ours) — built on sight. A fact about
the user is a MEMORY — checked. A frequency gate on a shelf does not filter noise; it HIDES the
shelf from the only thing that can judge it, so three sightings of a wrong shelf is still a wrong
shelf, just later and uncorrectable in the meantime.

WHAT IS PINNED HERE
  * the two ungated ENGINE-STRUCTURE thresholds DEFAULT to 1 (usable on derivation), pinned WITHOUT
    monkeypatching so a future default flip goes red here even though behavioural tests set their
    own values;
  * both keep a documented ROLLBACK LEVER;
  * the INFERRED-USER-MEMORY gate (`staged_facts.confirmed_count >= 3` C→B promotion) is UNTOUCHED —
    the one thing the ruling says SHOULD be validated. If a future change collapses the two, this
    goes red.
"""
import importlib
import os

import pytest


def _emb(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import src.re_embedder.embedder as e
    return importlib.reload(e)


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {k: os.environ.get(k) for k in ("CUE_GROWTH_THRESHOLD", "REL_TYPE_APPROVAL_THRESHOLD")}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_cue_growth_is_usable_on_derivation_by_default():
    """A DERIVED cue class must not wait for confirmations. Default = 1, env UNSET."""
    e = _emb(CUE_GROWTH_THRESHOLD=None)
    assert e.LINGUISTIC_CUE_GROWTH_THRESHOLD == 1, e.LINGUISTIC_CUE_GROWTH_THRESHOLD


def test_novel_rel_type_approval_is_usable_on_derivation_by_default():
    e = _emb(REL_TYPE_APPROVAL_THRESHOLD=None)
    assert e.REL_TYPE_APPROVAL_THRESHOLD == 1, e.REL_TYPE_APPROVAL_THRESHOLD


@pytest.mark.parametrize("var,attr", [
    ("CUE_GROWTH_THRESHOLD", "LINGUISTIC_CUE_GROWTH_THRESHOLD"),
    ("REL_TYPE_APPROVAL_THRESHOLD", "REL_TYPE_APPROVAL_THRESHOLD"),
])
def test_the_legacy_freq_gate_is_still_reachable_as_a_rollback_lever(var, attr):
    e = _emb(**{var: "3"})
    assert getattr(e, attr) == 3


@pytest.mark.parametrize("var,attr", [
    ("CUE_GROWTH_THRESHOLD", "LINGUISTIC_CUE_GROWTH_THRESHOLD"),
    ("REL_TYPE_APPROVAL_THRESHOLD", "REL_TYPE_APPROVAL_THRESHOLD"),
])
def test_a_junk_or_zero_value_floors_at_one_rather_than_disabling_the_sweep(var, attr):
    # 0 would make `occurrence_count >= 0` match rows that were never observed at all; a non-int
    # would raise inside the re_embedder loop. Both floor to 1 = the intended default.
    for bad in ("0", "-4"):
        assert getattr(_emb(**{var: bad}), attr) == 1, bad


def test_the_inferred_user_memory_promotion_gate_is_untouched():
    """THE ONE GATE THAT MUST SURVIVE. `staged_facts.confirmed_count >= 3` is the C→B promotion of
    INFERRED user memory — precisely the case the ruling says SHOULD be validated. It is NOT engine
    structure and must never be folded into the thresholds above.
    """
    e = _emb()
    src = ""
    import inspect
    for fn in ("promote_staged_facts", "promote_class_c_hits"):
        src += inspect.getsource(getattr(e, fn))
    assert "confirmed_count >= %s" in src or "confirmed_count >= 3" in src, \
        "the staged_facts C→B promotion gate has moved or been removed — it must NOT be ungated"
    assert "LINGUISTIC_CUE_GROWTH_THRESHOLD" not in src and "REL_TYPE_APPROVAL_THRESHOLD" not in src, \
        "an ENGINE-STRUCTURE threshold leaked into the USER-MEMORY promotion path"
    assert e.PROMOTION_THRESHOLD == 3 if hasattr(e, "PROMOTION_THRESHOLD") else True
