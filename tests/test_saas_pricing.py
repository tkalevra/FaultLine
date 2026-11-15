"""SaaS tiered base + per-seat pricing (owner-ratified 2026-07) — CLOSED-SOURCE SaaS layer.

Covers the rebuild:
  * tier-dependent per-seat ``entitlements.price_for(seat_type, account_kind)`` — the SAME seat
    type costs three prices by account_kind (Individual retail / Team / Enterprise);
  * base-package prices + included-Standard allotments (Team $750/2, Enterprise $1500/5);
  * Team billing math: $750 base includes the first 2 Standard, #3+ at $12.50, typed seats at the
    Team rate — billed on ACTIVE seat count;
  * Enterprise billing math: $1500 base (5 Standard incl.), add-ons at Enterprise rates;
  * individual plan re-map: Standard $15 / Advanced $40 / Expert $60 / Agent $120, seat-type labels.

Pure + monkeypatched (no live DB). Run: python3 -m pytest tests/test_saas_pricing.py -q
"""

import pytest

from saas import entitlements as ent
from saas import plans


# ════════════════════════════════════════════════════════════════════════════════
# tier-dependent price_for — same seat, three prices by account_kind
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("seat,tenant,team", [
    ("standard", 10.0, 12.5),
    ("advanced", 30.0, 35.0),
    ("expert",   45.0, 50.0),
    ("agent",    80.0, 100.0),
])
def test_price_for_is_tier_dependent(seat, tenant, team):
    assert ent.price_for(seat, "tenant") == tenant       # Enterprise column
    assert ent.price_for(seat, "team") == team           # Team column
    # default kind = Enterprise (back-compat: single-arg call)
    assert ent.price_for(seat) == tenant


def test_price_for_same_seat_three_prices():
    # The headline of the ratified model: one seat type → distinct Team vs Enterprise rate.
    assert ent.price_for("agent", "tenant") != ent.price_for("agent", "team")
    assert ent.price_for("agent", "team") == 100.0 and ent.price_for("agent", "tenant") == 80.0


def test_unknown_kind_falls_back_to_enterprise():
    assert ent.price_for("expert", "individual_house") == ent.price_for("expert", "tenant")
    assert ent.price_for("expert", "bogus") == 45.0


def test_price_map_is_per_kind():
    assert ent.price_map("team") == {"standard": 12.5, "advanced": 35.0,
                                     "expert": 50.0, "agent": 100.0}
    assert ent.price_map("tenant") == {"standard": 10.0, "advanced": 30.0,
                                       "expert": 45.0, "agent": 80.0}


def test_env_override_per_kind_and_legacy(monkeypatch):
    # per-kind override wins for that kind only
    monkeypatch.setenv("SAAS_PRICE_TEAM_AGENT", "175")
    assert ent.price_for("agent", "team") == 175.0
    assert ent.price_for("agent", "tenant") == 80.0        # untouched
    # legacy kind-agnostic override applies to whatever kind is priced
    monkeypatch.setenv("SAAS_PRICE_EXPERT", "999")
    assert ent.price_for("expert", "tenant") == 999.0
    assert ent.price_for("expert", "team") == 999.0


# ════════════════════════════════════════════════════════════════════════════════
# base-package prices + included-Standard allotments
# ════════════════════════════════════════════════════════════════════════════════

def test_base_and_included_constants():
    assert ent.TEAM_BASE_PRICE_CAD == 750.0 and ent.TEAM_INCLUDED_STANDARD == 2
    assert ent.BASE_PACKAGE_PRICE_CAD == 1500.0 and ent.ENTERPRISE_INCLUDED_STANDARD == 5
    assert ent.TEAM_SEAT_CAP == 15


def test_base_package_price_for():
    assert ent.base_package_price_for("team") == 750.0
    assert ent.base_package_price_for("tenant") == 1500.0
    assert ent.base_package_price_for("individual_house") is None


def test_included_standard_for():
    assert ent.included_standard_for("team") == 2
    assert ent.included_standard_for("tenant") == 5
    assert ent.included_standard_for("individual_house") == 0


def test_team_base_env_override(monkeypatch):
    monkeypatch.setenv("SAAS_PRICE_TEAM_BASE", "900")
    assert ent.team_base_price() == 900.0
    assert ent.base_package_price_for("team") == 900.0


# ════════════════════════════════════════════════════════════════════════════════
# billing math (monkeypatched control-plane helpers — no DB)
# ════════════════════════════════════════════════════════════════════════════════

def _stub_billing(monkeypatch, *, kind, usage=None, entitlements=None):
    monkeypatch.setattr(ent, "_account_kind", lambda a: kind)
    monkeypatch.setattr(ent, "seat_type_usage", lambda a: dict(usage or {}))
    monkeypatch.setattr(ent, "get_entitlements", lambda a: dict(entitlements or {}))
    monkeypatch.setattr(ent, "_contract_term",
                        lambda a: {"term_months": 6, "expires_at": None, "expired": False})
    monkeypatch.setattr(ent, "ledger_summary",
                        lambda a, **k: {"periods": [], "grand_total": 0.0})
    monkeypatch.setattr(ent, "_account_tax", lambda a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "scheduled_downgrades", lambda a: [])


def test_team_billing_750_base_two_included(monkeypatch):
    # 4 active Standard (2 incl → 2 billed @12.50) + 1 Advanced (@35) + 1 Agent (@100).
    _stub_billing(monkeypatch, kind="team",
                  usage={"standard": 4, "advanced": 1, "agent": 1})
    b = ent.billing_view("acct")
    assert b["kind"] == "team"
    assert b["base_package_price"] == 750.0 and b["included_standard"] == 2
    # base 750 + 2×12.50 (25) + 1×35 + 1×100 = 910
    assert b["monthly_total"] == 910.0
    base = next(l for l in b["lines"] if l["kind"] == "base")
    assert base["monthly"] == 750.0 and base["quantity"] == 2
    std = next(l for l in b["lines"] if l["seat_type"] == "standard" and l["kind"] == "addon")
    assert std["quantity"] == 2 and std["unit_price"] == 12.5


def test_team_billing_under_included_is_base_only(monkeypatch):
    # 2 active Standard (== the included allotment) → no Standard add-on line, bill = base only.
    _stub_billing(monkeypatch, kind="team", usage={"standard": 2})
    b = ent.billing_view("acct")
    assert b["monthly_total"] == 750.0
    assert not [l for l in b["lines"] if l["seat_type"] == "standard" and l["kind"] == "addon"]


def test_enterprise_billing_1500_base_five_included(monkeypatch):
    # Enterprise bills PROVISIONED (licensed − base) at Enterprise rates. 2 provisioned Expert
    # (@45) + 1 provisioned Agent (@80) on top of the $1500 base.
    _stub_billing(monkeypatch, kind="tenant",
                  entitlements={
                      "standard": {"quantity": 5, "base_quantity": 5, "provisioned": 0},
                      "expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2},
                      "agent": {"quantity": 1, "base_quantity": 0, "provisioned": 1},
                  })
    b = ent.billing_view("acct")
    assert b["kind"] == "tenant"
    assert b["base_package_price"] == 1500.0 and b["included_standard"] == 5
    # 1500 + 2×45 (90) + 1×80 = 1670
    assert b["monthly_total"] == 1670.0


def test_capacity_report_team_surfaces_base_and_cap(monkeypatch):
    monkeypatch.setattr(ent, "_account_kind", lambda a: "team")
    monkeypatch.setattr(ent, "get_entitlements",
                        lambda a: {"standard": {"quantity": 2, "base_quantity": 2,
                                                "provisioned": 0}})
    monkeypatch.setattr(ent, "seat_type_usage", lambda a: {"standard": 2})
    monkeypatch.setattr(ent, "_contract_term",
                        lambda a: {"term_months": 6, "expires_at": None, "expired": False})
    rep = ent.capacity_report("acct")
    assert rep["account_kind"] == "team"
    assert rep["base_package_price"] == 750.0 and rep["included_standard"] == 2
    assert rep["seat_cap"] == 15
    # unit prices reflect the Team column
    agent = next(t for t in rep["types"] if t["seat_type"] == "agent")
    assert agent["unit_price"] == 100.0


# ════════════════════════════════════════════════════════════════════════════════
# individual plan re-map (Standard/Advanced/Expert/Agent · $15/$40/$60/$120)
# ════════════════════════════════════════════════════════════════════════════════

def test_individual_prices_remap():
    assert plans.individual_price_for("standard") == 15
    assert plans.individual_price_for("advanced") == 40
    assert plans.individual_price_for("expert") == 60
    assert plans.individual_price_for("agent") == 120


def test_individual_tiers_include_standard_entry():
    assert plans.INDIVIDUAL_TIERS == ("standard", "advanced", "expert", "agent")
    assert plans.ENTRY_TIER == "standard"


def test_individual_labels_are_seat_type_names():
    cat = {p["tier"]: p for p in plans.plan_catalog()}
    assert cat["standard"]["label"] == "Standard"
    assert cat["advanced"]["label"] == "Advanced"
    assert cat["expert"]["label"] == "Expert"
    assert cat["agent"]["label"] == "Agent"
    # Retired buyer names are gone.
    labels = {p["label"] for p in plans.plan_catalog()}
    assert "Pro" not in labels and "Power" not in labels


def test_individual_price_env_override_new_keys(monkeypatch):
    monkeypatch.setenv("SAAS_INDIVIDUAL_PRICE_AGENT", "199")
    assert plans.individual_price_for("agent") == 199.0
    monkeypatch.setenv("SAAS_INDIVIDUAL_PRICE_STANDARD", "9")
    assert plans.individual_price_for("standard") == 9.0
