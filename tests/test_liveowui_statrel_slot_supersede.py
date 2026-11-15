# -*- coding: utf-8 -*-
"""THE STATE-REL SLOT SUPERSESSION + THE AUTHORITY LADDER (LIVE-OPENWEBUI FIX 4).

THE MEASURED WOUND (DEV/BUGS/family-smoke-20261111/LIVE-OPENWEBUI-DIAGNOSIS.md, the owner's
prod seat): "My spouse is Dana Feld and she prefers to be called Day" added the new
spouse row BESIDE the prior spouse slot — ``ingest.temporal_coexist`` "same-slot state
conflict kept BOTH rows live (keep-both)" — and five test-junk spouse rows stayed live
with it. The keep-both fired on a SINGLE-VALUED state slot (spouse is OWL-functional:
one current holder) where it should supersede.

THE FIX (two pieces):
  1. ``_decide_supersede_or_coexist`` gains the single-valued state-slot branch
     (INGEST_STATEREL_SLOT_SUPERSEDE default ON; state class + is_single_valued + affirmed
     + incoming 'now' → supersede_existing, mirroring the F6 residence gates exactly;
     OFF → byte-for-byte coexist). Migration 326 seeds spouse is_single_valued=true.
  2. ``_PROVENANCE_AUTHORITY`` gains user_correction (the pin ladder
     user_stated > user_correction > llm_inferred); previously user_correction ranked 0 —
     BELOW llm_inferred — so a user-correction row could never supersede an LLM row.

Pure decider: no database.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.pop("POSTGRES_DSN", None)

import src.api.main as M  # noqa: E402

_SPOUSE_META = {"temporal_class": "state", "is_single_valued": True}
_MULTI_META = {"temporal_class": "state", "is_single_valued": False}
_EVENT_META = {"temporal_class": "event", "is_single_valued": True}


def _new(prov="user_stated", **kw):
    base = {"fact_provenance": prov, "is_correction": False, "polarity": "affirmed",
            "rel_type": "spouse", "temporal_status": "now"}
    base.update(kw)
    return base


def _old(prov="llm_inferred"):
    return {"fact_provenance": prov}


def test_new_user_stated_spouse_supersedes_llm_inferred_slot():
    """THE PIN: the new user_stated spouse row supersedes the old llm_inferred rows."""
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("llm_inferred"), _SPOUSE_META) == "supersede_existing"


def test_new_user_stated_spouse_supersedes_test_junk_llm_learned_slot():
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("llm_learned"), _SPOUSE_META) == "supersede_existing"


def test_new_user_stated_spouse_supersedes_prior_user_stated_slot():
    """A single-current slot: the later user_stated holder archives the earlier (soft)."""
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("user_stated"), _SPOUSE_META) == "supersede_existing"


def test_user_correction_rank_fixes_the_ladder():
    """user_correction (3) outranks llm_inferred (1) — it ranked 0 (below llm_inferred!)
    before the fix, so a user-correction row could never supersede an LLM row."""
    assert M._provenance_rank("user_stated") > M._provenance_rank("user_correction")
    assert M._provenance_rank("user_correction") > M._provenance_rank("llm_learned")
    assert M._provenance_rank("llm_learned") > M._provenance_rank("llm_inferred")
    assert M._decide_supersede_or_coexist(
        _new("user_correction"), _old("llm_inferred"), _SPOUSE_META) == "supersede_existing"


def test_lower_authority_incoming_still_drops():
    """The trust firewall is untouched: llm_inferred incoming never takes a user slot."""
    assert M._decide_supersede_or_coexist(
        _new("llm_inferred"), _old("user_stated"), _SPOUSE_META) == "drop_incoming"


def test_multi_valued_state_rel_keeps_coexist():
    """knows/friend_of stay multi-valued (migration 326 touches spouse only): coexist."""
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("llm_inferred"), _MULTI_META) == "coexist"


def test_event_class_keeps_coexist():
    """Distinct datable occurrences never supersede."""
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("llm_inferred"), _EVENT_META) == "coexist"


def test_negated_polarity_never_enters_the_branch():
    """A negation never archives via this branch — the negation lanes own that."""
    assert M._decide_supersede_or_coexist(
        _new("user_stated", polarity="negated"), _old("llm_inferred"),
        _SPOUSE_META) == "coexist"


def test_flag_off_is_byte_parity(monkeypatch):
    monkeypatch.setattr(M, "INGEST_STATEREL_SLOT_SUPERSEDE", False)
    assert M._decide_supersede_or_coexist(
        _new("user_stated"), _old("llm_inferred"), _SPOUSE_META) == "coexist"
