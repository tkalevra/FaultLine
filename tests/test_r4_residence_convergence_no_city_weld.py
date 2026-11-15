"""Fog-family R4 (lane C, R3-4): a street ADDRESS must never convergence-bind onto its CITY.

THE WOUND (measured live, bench seat, cross-city plain-address correction): the harvest
minted the fresh city entity ("harlow bend") and, moments later, the street phrase
"31 alder gate in harlow bend" resolved through the issue-#226 restatement-convergence
rung — rule `modifier_added` (same head token "bend", alias tokens a subset) — and the
FULL STREET PHRASE was registered as an ALIAS OF THE CITY ENTITY. Every subsequent
lives_in through that entity voiced the street as the city ("where do I live?" -> "31
alder gate in harlow bend"), and the stale-looking address render hid behind the weld.

THE GRAMMAR: a locative preposition introduces a DIFFERENT referent. "31 alder gate IN
harlow bend" is [street NP] + [locative PP] — the city is the PP TAIL, not an appended
modifier. _convergence_compat now declines any bind whose alias occurrence is preceded
by a closed-class locative preposition; genuine modifier-appended restatements (alias at
the head side, e.g. "old harlow bend" for "harlow bend") still bind, and the
REGISTRY_RESTATEMENT_CONVERGENCE rollback lever still restores byte-for-byte legacy.
"""
import pytest

from src.entity_registry import registry as reg_mod
from src.entity_registry import weld_guard
from src.entity_registry.registry import EntityRegistry, _CONVERGENCE_LOCATIVE_PREPS


def _norm(s):
    return EntityRegistry._convergence_norm_tokens(s)


def _compat(new, alias):
    return EntityRegistry._convergence_compat(_norm(new), _norm(alias), frozenset())


# ── the wound shapes: an address never convergence-binds onto its city ──────────────
def test_possessive_street_declines_city_bind():
    assert _compat("31 alder gate in harlow bend", "harlow bend") is None


def test_possessive_street_chandlery_declines_city_bind():
    assert _compat("6 chandler's wharf in windrow", "windrow") is None


def test_plain_street_declines_city_bind():
    assert _compat("12 alder lane in rainhollow", "rainhollow") is None


def test_at_locative_declines_too():
    assert _compat("9 kessel street at mills crossing", "mills crossing") is None


def test_exact_restatement_still_binds():
    assert _compat("harlow bend", "harlow bend") == "exact"


def test_modifier_dropped_across_locative_boundary_declines():
    # R6 SYMMETRIC ARM (apostrophe-city shred): the harvest minted the full address
    # FIRST; the bare city resolved seconds later and modifier_dropped (new inside
    # alias, same head) read the city as the address minus a modifier - welding the
    # city onto the phrase entity so lives_in voiced the whole address as the
    # residence. The boundary between the city tokens and the rest of the phrase is
    # the LOCATIVE prep, so the symmetric PP-tail guard declines it - whichever side
    # is shorter.
    assert _compat("harlow bend", "31 alder gate in harlow bend") is None
    assert _compat("pilgrim's reach", "2 nettlefold row in pilgrim's reach") is None


def test_genuine_appended_modifier_still_binds():
    """The legit #226 shape: a modifier prepended to the SAME head ("old harlow bend"
    restating "harlow bend") keeps binding — the alias is not prep-governed."""
    assert _compat("old harlow bend", "harlow bend") == "modifier_added"


def test_different_head_never_bound_even_with_prep():
    """Heads must match for any bind; a city-tailed NP with a different head was never
    a candidate (the shipped head rule) — pinned so the guard cannot widen it."""
    assert _compat("blue bottle in berkeley", "blue bottle") is None


def test_locative_prep_set_is_closed_class():
    assert {"in", "at", "on", "near", "inside", "within"} <= _CONVERGENCE_LOCATIVE_PREPS
    assert "of" not in _CONVERGENCE_LOCATIVE_PREPS and "with" not in _CONVERGENCE_LOCATIVE_PREPS


# ── registry-level: the probe returns no target for the address shape ───────────────
class _FakeCur:
    def __init__(self, all_rows=None, one_rows=None):
        self.all_rows = list(all_rows or [])
        self.one_rows = list(one_rows or [])
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(str(sql).split()))

    def fetchall(self):
        return self.all_rows.pop(0) if self.all_rows else []

    def fetchone(self):
        return self.one_rows.pop(0) if self.one_rows else None


class _FakeConn:
    def cursor(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass


_EID = "5ec89b52-9ea8-59b5-8ecd-e1e6650221be"
_SCHEMA = "faultline_5d8e93b4_0ae9_4523_914c_f1709568bfa6"


def _registry(monkeypatch):
    monkeypatch.setattr(weld_guard, "seat_entity_id", lambda schema: None, raising=False)
    return EntityRegistry(db_conn=_FakeConn(), schema_name=_SCHEMA)


def test_probe_returns_no_target_for_address_vs_recent_city(monkeypatch):
    """The exact live wound: a fresh city alias in the convergence window must NOT accept
    the street phrase as its restatement."""
    reg = _registry(monkeypatch)
    cur = _FakeCur(all_rows=[[( _EID, "harlow bend")]])
    assert reg._restatement_convergence_target(
        cur, "5d8e93b4-0ae9-4523-914c-f1709568bfa6",
        "31 alder gate in harlow bend") is None


def test_probe_still_binds_genuine_restatement(monkeypatch):
    """The #226 contract survives: a deterministically-compatible restatement binds."""
    reg = _registry(monkeypatch)
    cur = _FakeCur(all_rows=[[( _EID, "harlow bend")]], one_rows=[None])
    assert reg._restatement_convergence_target(
        cur, "5d8e93b4-0ae9-4523-914c-f1709568bfa6", "old harlow bend") == _EID


def test_rollback_lever_still_restores_legacy(monkeypatch):
    monkeypatch.setattr(reg_mod, "REGISTRY_RESTATEMENT_CONVERGENCE", False)
    reg = _registry(monkeypatch)
    cur = _FakeCur(all_rows=[[( _EID, "harlow bend")]])
    assert reg._restatement_convergence_target(
        cur, "5d8e93b4-0ae9-4523-914c-f1709568bfa6",
        "old harlow bend") is None
