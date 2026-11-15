"""Billing COUNTING BASIS (entitled, not occupied) + operator COMP / EXTEND-TRIAL levers.
CLOSED-SOURCE SaaS layer.

THE BUG (owner, 2026-08, with a screenshot of the ExampleCorp bill): a Team account holding
TWO Expert licences with only ONE hired into was billed ``Expert × 1 — CAD $50``. The per-seat
line counted OCCUPANCY (``account_users`` active rows) instead of the ENTITLEMENT
(``account_entitlements.quantity``) — a straight revenue leak for every customer who buys
capacity ahead of hiring.

Pinned here:
  * the owner's EXACT shape — 2 Expert entitled / 1 occupied → ``Expert × 2``, subtotal $850;
  * the base-vs-overage interaction — seats INCLUDED in the base package never also draw a
    per-seat line (no double charge), and only the overage bills;
  * the occupancy FLOOR — the max() that keeps a non-entitlement-gated type (Team standard,
    which is ordered BY occupying) honest, and that never over-charges a gated type;
  * COMP — the bill is computed in FULL and waived by ONE explicit credit line, never a silent
    $0; reason required; expiry honoured; the individual-house grouping refused;
  * EXTEND TRIAL — extends from the LATER of now / current end, validates its inputs.

Pure + monkeypatched (no live DB). Run:
    python3 tools/fltest.py --bug BILLQTY --test tests/test_saas_billing_quantity_and_comp.py
"""

import pytest

from saas import entitlements as ent
from saas import plans


# ── stub the control plane (no DB) ──────────────────────────────────────────────────

def _stub(monkeypatch, *, kind, usage=None, entitlements=None, comp=None):
    monkeypatch.setattr(ent, "_account_kind", lambda a: kind)
    monkeypatch.setattr(ent, "seat_type_usage", lambda a: dict(usage or {}))
    monkeypatch.setattr(ent, "get_entitlements", lambda a: dict(entitlements or {}))
    monkeypatch.setattr(ent, "_contract_term",
                        lambda a: {"term_months": 6, "expires_at": None, "expired": False})
    monkeypatch.setattr(ent, "ledger_summary",
                        lambda a, **k: {"periods": [], "grand_total": 0.0})
    monkeypatch.setattr(ent, "_account_tax", lambda a: {"tax_rate": 0.0, "tax_amount": 0.0})
    monkeypatch.setattr(ent, "scheduled_downgrades", lambda a: [])
    monkeypatch.setattr(ent, "comp_state", lambda a: dict(comp or {
        "comped": False, "active": False, "reason": None, "by": None,
        "at": None, "until": None, "expired": False}))


def _line(view, seat_type, kind="addon"):
    for l in view["lines"]:
        if l["seat_type"] == seat_type and l["kind"] == kind:
            return l
    return None


# ════════════════════════════════════════════════════════════════════════════════════
# THE REPORTED BUG — the per-seat line must count ORDERED, not IN USE
# ════════════════════════════════════════════════════════════════════════════════════

def test_team_expert_bills_entitled_not_occupied(monkeypatch):
    """The owner's exact account: 2 Expert LICENSED, 1 OCCUPIED → the bill reads Expert × 2.

    Before the fix this line read ``Expert × 1 — CAD $50`` and the subtotal was $800.
    """
    _stub(monkeypatch, kind="team",
          usage={"standard": 2, "expert": 1},                       # 1 Expert in use
          entitlements={"expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}})
    b = ent.billing_view("acct")
    expert = _line(b, "expert")
    assert expert is not None, "the Expert per-seat line disappeared"
    assert expert["quantity"] == 2, "billed the OCCUPIED count, not the ENTITLED count"
    assert expert["monthly"] == 100.0            # 2 × $50 Team Expert rate
    assert b["monthly_subtotal"] == 850.0        # $750 base + $100 Expert
    # Occupancy is still SHOWN — it is just never the basis of the charge.
    assert expert["licensed"] == 2 and expert["occupied"] == 1


def test_team_zero_occupied_still_bills_the_licence(monkeypatch):
    """Bought ahead of hiring: 3 Agent licensed, NOBODY in them → still billed. The capacity is
    held whether or not a body sits in it; this is the leak's most extreme form."""
    _stub(monkeypatch, kind="team",
          usage={"standard": 1},
          entitlements={"agent": {"quantity": 3, "base_quantity": 0, "provisioned": 3}})
    b = ent.billing_view("acct")
    agent = _line(b, "agent")
    assert agent["quantity"] == 3 and agent["occupied"] == 0
    assert b["monthly_subtotal"] == 750.0 + 3 * 100.0


def test_enterprise_expert_bills_entitled_not_occupied(monkeypatch):
    """The Enterprise view already counted entitlement — pin it so it cannot regress the
    other way (to occupancy) either."""
    _stub(monkeypatch, kind="tenant",
          usage={"standard": 5, "expert": 1},
          entitlements={"standard": {"quantity": 5, "base_quantity": 5, "provisioned": 0},
                        "expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}})
    b = ent.billing_view("acct")
    assert _line(b, "expert")["quantity"] == 2
    assert b["monthly_subtotal"] == 1500.0 + 2 * 45.0


# ════════════════════════════════════════════════════════════════════════════════════
# INCLUDED-IN-BASE vs PER-SEAT OVERAGE — no double charge, no free ride
# ════════════════════════════════════════════════════════════════════════════════════

def test_team_included_standard_never_also_draws_a_per_seat_line(monkeypatch):
    """2 Standard entitled on a Team == exactly the included allotment → base line only.
    An entitlement of 2 Standard must NOT suddenly add a per-seat line on top of the base."""
    _stub(monkeypatch, kind="team",
          usage={"standard": 2},
          entitlements={"standard": {"quantity": 2, "base_quantity": 0, "provisioned": 2}})
    b = ent.billing_view("acct")
    assert _line(b, "standard") is None, "included seats were double-charged"
    assert b["monthly_subtotal"] == 750.0
    base = _line(b, "standard", kind="base")
    assert base["quantity"] == ent.TEAM_INCLUDED_STANDARD and base["monthly"] == 750.0


def test_team_standard_bills_only_the_overage(monkeypatch):
    """5 Standard licensed, 2 included → only 3 bill per-seat."""
    _stub(monkeypatch, kind="team",
          usage={"standard": 5},
          entitlements={"standard": {"quantity": 5, "base_quantity": 0, "provisioned": 5}})
    b = ent.billing_view("acct")
    std = _line(b, "standard")
    assert std["quantity"] == 3 and std["monthly"] == 3 * 12.5
    assert b["monthly_subtotal"] == 750.0 + 37.5


def test_enterprise_included_standard_never_also_draws_a_per_seat_line(monkeypatch):
    """The Enterprise half of the same rule: 5 Standard, all base → base line only."""
    _stub(monkeypatch, kind="tenant",
          usage={"standard": 5},
          entitlements={"standard": {"quantity": 5, "base_quantity": 5, "provisioned": 0}})
    b = ent.billing_view("acct")
    assert _line(b, "standard") is None
    assert b["monthly_subtotal"] == 1500.0


def test_enterprise_standard_overage_bills_above_the_included_five(monkeypatch):
    _stub(monkeypatch, kind="tenant",
          usage={"standard": 7},
          entitlements={"standard": {"quantity": 7, "base_quantity": 5, "provisioned": 2}})
    b = ent.billing_view("acct")
    assert _line(b, "standard")["quantity"] == 2
    assert b["monthly_subtotal"] == 1500.0 + 2 * 10.0


# ════════════════════════════════════════════════════════════════════════════════════
# the occupancy FLOOR — max(ordered, in use)
# ════════════════════════════════════════════════════════════════════════════════════

def test_floor_never_lowers_a_licensed_count():
    """Entitlement is the BASIS; occupancy can only ever be a floor, never a ceiling."""
    assert ent._billable_count(5, 0) == 5
    assert ent._billable_count(5, 3) == 5
    assert ent._billable_count(0, 0) == 0


def test_floor_catches_seats_running_without_capacity(capsys):
    """A gated type whose roster exceeds its entitlement bills the roster AND says so LOUD —
    money never silently leaks in either direction."""
    assert ent._billable_count(1, 4, account_id="a", seat_type="expert") == 4
    assert "occupancy exceeds entitlement" in capsys.readouterr().err


def test_floor_is_quiet_for_a_type_ordered_by_occupying(capsys):
    """Team STANDARD has no entitlement row (it is capped by accounts.seat_limit), so the floor
    binding is the normal case, not an alarm — it must not cry wolf."""
    assert ent._billable_count(0, 4, account_id="a", seat_type="standard", gated=False) == 4
    assert "occupancy exceeds entitlement" not in capsys.readouterr().err


def test_team_standard_without_an_entitlement_row_still_bills(monkeypatch):
    """A Team carries NO standard entitlement — creating the seat IS the order. Billing purely
    on entitlement would hand those seats away for free."""
    _stub(monkeypatch, kind="team", usage={"standard": 4}, entitlements={})
    b = ent.billing_view("acct")
    assert _line(b, "standard")["quantity"] == 2      # 4 held − 2 included
    assert b["monthly_subtotal"] == 750.0 + 25.0


def test_typed_seats_over_entitlement_bill_the_roster(monkeypatch):
    """Belt-and-braces: if the entitlement row and the roster ever diverge on a gated type,
    the customer is billed for what is RUNNING, not the stale smaller number."""
    _stub(monkeypatch, kind="team", usage={"expert": 3},
          entitlements={"expert": {"quantity": 1, "base_quantity": 0, "provisioned": 1}})
    b = ent.billing_view("acct")
    assert _line(b, "expert")["quantity"] == 3


# ════════════════════════════════════════════════════════════════════════════════════
# COMP — an explicit, auditable waiver; never a silent $0
# ════════════════════════════════════════════════════════════════════════════════════

_ACTIVE_COMP = {"comped": True, "active": True, "reason": "owner's own account",
                "by": "chris", "at": "2026-08-03T10:00:00+00:00", "until": None,
                "expired": False}


def test_comp_waives_the_bill_with_an_explicit_line(monkeypatch):
    _stub(monkeypatch, kind="team", usage={"standard": 2, "expert": 1},
          entitlements={"expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}},
          comp=_ACTIVE_COMP)
    b = ent.billing_view("acct")
    assert b["comped"] is True
    assert b["monthly_subtotal"] == 0.0 and b["monthly_total"] == 0.0
    # The REAL bill is preserved, not erased — the operator can see what the comp costs.
    assert b["gross_subtotal"] == 850.0 and b["comped_value"] == 850.0
    credit = [l for l in b["lines"] if l["kind"] == "comp"]
    assert len(credit) == 1, "a comped bill must carry ONE explicit credit line"
    assert credit[0]["monthly"] == -850.0
    # ...and it must EXPLAIN itself: who, when, why.
    assert "owner's own account" in credit[0]["label"]
    assert "chris" in credit[0]["label"] and "2026-08-03" in credit[0]["label"]


def test_comp_keeps_every_charge_line_visible(monkeypatch):
    """A comp waives; it does not hide. The customer still sees WHAT was waived."""
    _stub(monkeypatch, kind="team", usage={"standard": 2},
          entitlements={"expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}},
          comp=_ACTIVE_COMP)
    b = ent.billing_view("acct")
    assert _line(b, "expert")["quantity"] == 2      # the charge line survives the waiver


def test_an_uncomped_account_is_untouched(monkeypatch):
    """SAAS billing with no comp is byte-for-byte the pre-feature shape (plus read-only keys)."""
    _stub(monkeypatch, kind="team", usage={"standard": 2, "expert": 1},
          entitlements={"expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}})
    b = ent.billing_view("acct")
    assert b["comped"] is False
    assert not [l for l in b["lines"] if l["kind"] == "comp"]
    assert b["monthly_subtotal"] == 850.0 and b["gross_subtotal"] == 850.0
    assert b["comped_value"] == 0.0


def test_expired_comp_bills_again_on_its_own(monkeypatch):
    """comp_until is evaluated at READ time — a lapsed comp needs no reaper to resume billing."""
    lapsed = dict(_ACTIVE_COMP, active=False, expired=True, until="2026-07-01")
    _stub(monkeypatch, kind="team", usage={"standard": 2},
          entitlements={"expert": {"quantity": 2, "base_quantity": 0, "provisioned": 2}},
          comp=lapsed)
    b = ent.billing_view("acct")
    assert b["comped"] is False and b["monthly_subtotal"] == 850.0
    assert not [l for l in b["lines"] if l["kind"] == "comp"]


def test_comp_requires_a_written_reason(monkeypatch):
    """An unexplained $0 bill is the silent-zero this feature exists to prevent."""
    monkeypatch.setattr(ent, "_account_kind", lambda a: "team")
    out = ent.set_comp("acct", True, reason="   ")
    assert out["ok"] is False and out["status_code"] == 400
    assert "reason" in out["reason"].lower()


def test_comp_rejects_a_malformed_expiry(monkeypatch):
    monkeypatch.setattr(ent, "_account_kind", lambda a: "team")
    out = ent.set_comp("acct", True, reason="pilot", until="next tuesday")
    assert out["ok"] is False and out["status_code"] == 400


def test_comp_refuses_the_shared_individual_house(monkeypatch):
    """Comping the Singles grouping would comp every self-serve individual inside it at once."""
    monkeypatch.setattr(ent, "_account_kind", lambda a: "individual_house")
    out = ent.set_comp("acct", True, reason="oops")
    assert out["ok"] is False and out["status_code"] == 400
    assert "individual" in out["reason"].lower()


def test_comp_state_fails_toward_billing(monkeypatch, capsys):
    """A control-plane read failure must never silently zero a bill — fail LOUD and bill."""
    class _Boom:
        def __enter__(self):
            raise RuntimeError("control plane down")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ent.db, "connection", lambda *a, **k: _Boom())
    state = ent.comp_state("acct")
    assert state["active"] is False and state["comped"] is False
    assert "comp_state read failed" in capsys.readouterr().err


def test_comp_actions_are_ledgerable_but_need_no_charge_signature():
    """comp/comp_off are recorded in the append-only billing ledger. They are NOT in
    BILLABLE_ACTIONS: a waiver reduces what is owed, so the typed-signature gate that guards
    CHARGES does not apply — the operator identity + reason are the authorization record."""
    assert "comp" in ent.LEDGER_ACTIONS and "comp_off" in ent.LEDGER_ACTIONS
    assert "comp" not in ent.BILLABLE_ACTIONS


# ════════════════════════════════════════════════════════════════════════════════════
# EXTEND TRIAL — moves the date the system honours
# ════════════════════════════════════════════════════════════════════════════════════

def test_extend_trial_requires_a_reason():
    out = plans.extend_trial("2b0c9a1e-0000-4000-8000-000000000001", 30, reason="")
    assert out["ok"] is False and out["status_code"] == 400


@pytest.mark.parametrize("days", [0, -5, 3651])
def test_extend_trial_rejects_an_out_of_range_window(days):
    """A typo must not mint a permanent free ride — that is what the comp is for."""
    out = plans.extend_trial("2b0c9a1e-0000-4000-8000-000000000001", days, reason="pilot")
    assert out["ok"] is False and out["status_code"] == 400


def test_extend_trial_extends_from_the_later_of_now_and_current_end(monkeypatch):
    """A LAPSED trial is revived for the FULL window (not handed a window already in the past);
    a LIVE trial is extended, not truncated. Pinned on the SQL the writer actually issues."""
    seen = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.setdefault("sql", []).append(" ".join(sql.split()))
            seen["params"] = params

        def fetchone(self):
            from datetime import datetime, timezone
            return (datetime(2026, 9, 2, tzinfo=timezone.utc),)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            seen["committed"] = True

        def rollback(self):
            pass

    monkeypatch.setattr(plans.db, "connection", lambda *a, **k: _Conn())
    out = plans.extend_trial("2b0c9a1e-0000-4000-8000-000000000001", 30, reason="pilot")
    assert out["ok"] is True and seen.get("committed") is True
    update = [s for s in seen["sql"] if s.startswith("UPDATE")][0]
    assert "GREATEST(COALESCE(trial_ends_at, NOW()), NOW())" in update
    assert "make_interval(days => %s)" in update
    # The trial the system HONOURS — individual_users.trial_ends_at, not a display field.
    assert "public.individual_users" in update and "trial_ends_at" in update


def test_extend_trial_is_bounded_at_a_decade():
    """3650 is IN range; 3651 is not (pinned above). The ceiling is deliberate — an unbounded
    'extension' is a comp, and a comp is explicit and separately audited."""
    out = plans.extend_trial("", 3650, reason="pilot")     # rejected on user_id, not on days
    assert out["status_code"] == 400 and "user_id" in out["reason"]


def test_extend_trial_fails_loud_on_a_control_plane_error(monkeypatch, capsys):
    class _Boom:
        def __enter__(self):
            raise RuntimeError("control plane down")

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(plans.db, "connection", lambda *a, **k: _Boom())
    out = plans.extend_trial("2b0c9a1e-0000-4000-8000-000000000001", 30, reason="pilot")
    assert out["ok"] is False and out["status_code"] == 500
    assert "extend_trial FAILED" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════════════════
# SAAS_MODE OFF — the new levers add NO surface to the open core
# ════════════════════════════════════════════════════════════════════════════════════

def test_comp_and_trial_routes_are_operator_gated_and_saas_gated(monkeypatch):
    """Both give-away levers sit behind ``require_operator``, which itself calls
    ``_require_saas``. With SAAS off the route 404s; with SAAS on but no operator credential
    it 403s. A tenant admin can never comp their own bill."""
    from fastapi import HTTPException, Request
    from saas import admin as _admin

    paths = {r.path for r in _admin.router.routes if hasattr(r, "path")}
    for path in ("/admin/accounts/{account_id}/comp", "/admin/users/{user_id}/extend-trial"):
        assert path in paths, f"{path} is not registered"

    req = Request({"type": "http", "method": "POST", "headers": [], "path": "/x",
                   "query_string": b""})

    monkeypatch.delenv("SAAS_MODE", raising=False)           # SAAS OFF
    with pytest.raises(HTTPException) as off:
        _admin.require_operator(req, authorization=None)
    assert off.value.status_code == 404                      # no surface at all

    monkeypatch.setenv("SAAS_MODE", "1")                     # SAAS ON, no credential
    monkeypatch.setattr(_admin, "_operator_key", lambda: "")
    with pytest.raises(HTTPException) as on:
        _admin.require_operator(req, authorization=None)
    assert on.value.status_code == 403


def test_audit_record_is_a_noop_with_saas_off(monkeypatch):
    """The comp / trial-extend audit rows ride the SAME ledger as every other control-plane
    action — a no-op that touches no DB when SAAS is off."""
    from saas import audit as _audit
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.setattr(_audit.db, "connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("DB touched with SAAS off")))
    assert _audit.record("account.comp", account_id="a") is False
    assert _audit.record("individual.trial_extend") is False
