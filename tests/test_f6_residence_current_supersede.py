"""FOG-FAMILY F6 — ingest-side CURRENT-RESIDENCE supersession (walk-litmus P5 class).

THE WOUND (bench lab, DEV/FINDINGS-fog-family-lab-20261107.md F6): "where do I live?" rendered
BOTH "currently Porto (2027-03)" (the planted corpus fact) AND "currently Greyharbor" (the later
user_stated fact) — two CURRENT readings of one mutable slot. Root: the ingest supersede/coexist
decider keeps BOTH state values live (history preserved) and defers "current" to a present-state
collapse the renderer does not perform for date-carrying rows.

THE FIX (INGEST_RESIDENCE_SUPERSEDE, default ON): DESIGN-memory-temporal-lifecycle section 4's own
matrix sanctions the outcome — "state | new current value | incoming >= existing -> SUPERSEDE-
as-current: old stays date-stamped history, new becomes current". The branch applies to the SAFE
SUBSET only: mutable-residence rels (linguistics._residence_predicate_identities), state class,
affirmed polarity, incoming temporal_status == 'now'. Soft archive (superseded_at) — history
stays queryable (NEVER-MODIFY-MEMORY intact).

PURE tests on the ONE decider (no DB, no network) — same discipline as
tests/test_negation_supersede_171.py. Run: python3 -m pytest tests/test_f6_residence_current_supersede.py -q
"""
import pytest

import src.api.main as M


def _new(rel="lives_at", prov="user_stated", status="now", pol="affirmed", correction=False):
    return {"fact_provenance": prov, "is_correction": correction, "polarity": pol,
            "rel_type": rel, "temporal_status": status}


def _old(prov="user_stated"):
    return {"fact_provenance": prov}


# ── THE WOUND, CLOSED: later current residence archives the earlier one ────────────────────
@pytest.mark.parametrize("rel", ["lives_at", "lives_in", "located_at"])
def test_later_current_residence_supersedes_earlier(rel):
    """Same subject, same residence rel: the later 'now' user fact SUPERSEDES-as-current
    (soft archive) instead of coexisting as a rival 'currently X'."""
    assert M._decide_supersede_or_coexist(_new(rel), _old(), {"temporal_class": "state"}) \
        == "supersede_existing"


def test_residence_identity_resolver_safe_subset():
    """The gate is the ONE authority — LOCATION-category, MUTABLE (non-immutable) identities.
    born_in is immutable -> never a supersede lane; occupation/employer are not residence."""
    assert M._rel_is_mutable_current_residence("lives_at") is True
    assert M._rel_is_mutable_current_residence("lives_in") is True
    assert M._rel_is_mutable_current_residence("born_in") is False
    assert M._rel_is_mutable_current_residence("occupation") is False
    assert M._rel_is_mutable_current_residence("") is False
    assert M._rel_is_mutable_current_residence(None) is False


# ── THE SAFE SUBSET: everything outside it coexists exactly as before ──────────────────────
def test_non_residence_state_still_coexists():
    """A non-residence state slot (occupation class) keeps the blanket-coexist history rule."""
    assert M._decide_supersede_or_coexist(_new(rel="occupation"), _old(),
                                          {"temporal_class": "state"}) == "coexist"


def test_event_class_still_coexists():
    """Distinct datable occurrences coexist — the branch is state-class only."""
    assert M._decide_supersede_or_coexist(_new(rel="visited_in"), _old(),
                                          {"temporal_class": "event"}) == "coexist"


def test_past_stated_incoming_still_coexists():
    """The incoming fact must assert a CURRENT residence (temporal_status 'now') to archive;
    a past-stamped statement keeps both rows (history is the point)."""
    assert M._decide_supersede_or_coexist(_new(status="past"), _old(),
                                          {"temporal_class": "state"}) == "coexist"


def test_negated_incoming_still_coexists():
    """A negation never archives via this branch (polarity gate) — the negation lanes own that."""
    assert M._decide_supersede_or_coexist(_new(pol="negated"), _old(),
                                          {"temporal_class": "state"}) == "coexist"


def test_lower_authority_incoming_still_drops():
    """The trust firewall outranks the branch: llm_learned never archives a user_stated row."""
    assert M._decide_supersede_or_coexist(_new(prov="llm_learned"), _old(prov="user_stated"),
                                          {"temporal_class": "state"}) == "drop_incoming"


def test_correction_still_supersedes_upstream_of_the_branch():
    """The correction path's verdict is unchanged (it returned before the branch ever existed)."""
    assert M._decide_supersede_or_coexist(_new(correction=True), _old(),
                                          {"temporal_class": "state"}) == "supersede_existing"


def test_flag_off_restores_blanket_coexist(monkeypatch):
    """INGEST_RESIDENCE_SUPERSEDE=OFF -> byte-for-byte the pre-fix decision (coexist)."""
    monkeypatch.setattr(M, "INGEST_RESIDENCE_SUPERSEDE", False)
    assert M._decide_supersede_or_coexist(_new(), _old(), {"temporal_class": "state"}) \
        == "coexist"


def test_immutable_class_supersede_unchanged():
    """The immutable authority ladder (section 4) is untouched by the branch."""
    assert M._decide_supersede_or_coexist({"fact_provenance": "user_stated",
                                           "is_correction": False, "polarity": "affirmed",
                                           "rel_type": "has_email", "temporal_status": "now"},
                                          _old(), {"temporal_class": "immutable"}) \
        == "supersede_existing"
