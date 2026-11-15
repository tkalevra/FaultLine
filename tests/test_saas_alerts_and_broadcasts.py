"""Alert space — actionable items (Part A) + operator broadcasts (Part B) + PROFANITY GUARD.

Three units, no control-plane DB required (DB-backed functions are monkeypatched):

  1. content_guard  — the hardened profanity guard. Proves it CATCHES the obfuscation battery
     ("f u c k", "f*ck", "fvck", "f0ck", "sh!t", leet/spacing/repeat variants) and does NOT
     false-block the Scunthorpe allowlist ("assassin", "Scunthorpe", "class", "analysis", …).
     Fail-closed: a guard exception → BLOCK.
  2. alerts        — the actionable-item REGISTRY shape (kind/severity/message/action deep-link)
     and per-realm aggregation; a broken provider is swallowed (never breaks the strip).
  3. broadcasts    — audience-for-realm mapping + the compose seam is guard-gated at the router
     (a profane compose → 400 that does not echo the word; a clean compose passes the guard).

Run: python3 -m pytest tests/test_saas_alerts_and_broadcasts.py -q   (tests/ gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import content_guard as cg
from saas import alerts as al
from saas import broadcasts as bc
from saas import dashboard_auth as auth
from saas import admin as admin_mod


OP_KEY = "op-super-secret-key"
OP_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


# ══════════════════════════════════════════════════════════════════════════════════
# 1) PROFANITY GUARD — the hardening proof
# ══════════════════════════════════════════════════════════════════════════════════

# Obfuscation battery that MUST be blocked. Covers spacing, punctuation separators, leet
# substitution, dropped/substituted vowels, repeats, and mixed case.
_MUST_BLOCK = [
    "fuck", "FUCK", "FuCk",
    "f u c k", "f-u-c-k", "f.u.c.k", "f*ck", "fck", "fvck", "f0ck", "fuuuuck",
    "sh!t", "sh1t", "$hit", "s h i t",
    "b1tch", "b!tch", "c u n t", "cvnt",
    "a$$hole", "assh0le",
    "you absolute piece of shit",
    "n1gger", "f@ggot",
]

# Innocent words that must NEVER be blocked (Scunthorpe problem + normal copy).
_MUST_ALLOW = [
    "assassin", "assassinate", "Scunthorpe", "class", "classic", "classification",
    "analysis", "analyst", "analytics", "password", "assess", "assessment", "asset",
    "bass", "grass", "glass", "mass", "Massachusetts", "cocktail", "cockpit", "Hancock",
    "pass the class to the analyst", "analog",
    "Scheduled maintenance tonight at 02:00 UTC — recall stays up.",
    "Please review the Q3 analysis and reset your password.",
    "",  # empty is clean
]


@pytest.mark.parametrize("text", _MUST_BLOCK)
def test_guard_blocks_obfuscations(text):
    ok, reason = cg.check(text)
    assert ok is False, f"guard should BLOCK {text!r}"
    # the reason must NOT echo the offending word
    assert reason == "message blocked: contains disallowed language"
    assert text.strip().lower() not in reason.lower()


@pytest.mark.parametrize("text", _MUST_ALLOW)
def test_guard_allows_clean_and_scunthorpe(text):
    ok, _ = cg.check(text)
    assert ok is True, f"guard should ALLOW {text!r}"


def test_guard_fail_closed_on_error(monkeypatch):
    # Any internal error → BLOCK (fail-closed), never a silent pass.
    monkeypatch.setattr(cg, "_collapse_repeats", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, reason = cg.check("perfectly clean text")
    assert ok is False
    assert reason == "message blocked: contains disallowed language"


# ══════════════════════════════════════════════════════════════════════════════════
# 2) ACTIONABLE-ITEM REGISTRY — shape + aggregation + fail-safe
# ══════════════════════════════════════════════════════════════════════════════════

def _assert_item_shape(item):
    assert set(item.keys()) >= {"kind", "severity", "message", "action"}
    assert item["severity"] in ("info", "warning", "critical")
    assert isinstance(item["message"], str) and item["message"]
    action = item["action"]
    assert set(action.keys()) == {"tab", "subtab", "anchor"}
    assert isinstance(action["tab"], str) and action["tab"]
    assert isinstance(action["anchor"], str) and action["anchor"]


def test_tenant_actionable_item_shape_and_deep_link(monkeypatch):
    # two pending seat resets → one summarizing item pointing at the reset-approvals panel
    monkeypatch.setattr(al._reset_approvals, "list_pending_for_account",
                        lambda a: [{"id": "1"}, {"id": "2"}])
    items = al.pending_actions_for_tenant(ACCT_A)
    assert len(items) == 1
    _assert_item_shape(items[0])
    assert items[0]["kind"] == "reset_approval"
    assert "2 password-reset requests" in items[0]["message"]
    assert items[0]["action"] == {"tab": "advanced", "subtab": "security",
                                  "anchor": "reset-approvals-panel"}


def test_tenant_singular_plural(monkeypatch):
    monkeypatch.setattr(al._reset_approvals, "list_pending_for_account", lambda a: [{"id": "1"}])
    items = al.pending_actions_for_tenant(ACCT_A)
    assert "1 password-reset request " in items[0]["message"]  # singular, no trailing 's'


def test_tenant_empty_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(al._reset_approvals, "list_pending_for_account", lambda a: [])
    assert al.pending_actions_for_tenant(ACCT_A) == []


def test_operator_actionable_item_deep_link(monkeypatch):
    monkeypatch.setattr(al._reset_approvals, "list_pending_for_operator", lambda: [{"id": "1"}])
    items = al.pending_actions_for_operator()
    assert len(items) == 1
    _assert_item_shape(items[0])
    assert items[0]["action"]["tab"] == "reset-approvals"
    assert items[0]["action"]["anchor"] == "op-reset-approvals-panel"


def test_individual_has_no_actionable_items_yet():
    assert al.pending_actions_for_individual({"id": "x", "user_id": "y"}) == []


def test_broken_provider_is_swallowed(monkeypatch):
    def _boom(a):
        raise RuntimeError("provider blew up")
    monkeypatch.setattr(al, "_tenant_reset_approvals", _boom)
    # a provider that raises contributes nothing; the strip does not break
    assert al.pending_actions_for_tenant(ACCT_A) == []


# ══════════════════════════════════════════════════════════════════════════════════
# 3) BROADCASTS — audience mapping + the guard-gated compose ROUTER seam
# ══════════════════════════════════════════════════════════════════════════════════

def test_audience_for_realm_mapping():
    assert bc.audience_for_realm("tenant") == ("admins", "all")
    assert bc.audience_for_realm("individual") == ("users", "all")
    assert bc.audience_for_realm("operator") == ("all",)
    assert bc.audience_for_realm("bogus") == ("all",)   # fail-safe


@pytest.fixture
def op_client(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "alerts-broadcasts-unit-secret-0123456789")
    app = FastAPI()
    app.include_router(admin_mod.router)
    return TestClient(app)


def _op_headers():
    return {"Authorization": "Bearer " + OP_KEY}


def test_compose_profane_is_rejected_without_echo(op_client, monkeypatch):
    created = {}
    monkeypatch.setattr(admin_mod._broadcasts, "create_broadcast",
                        lambda **k: created.update(k) or {"id": "x", **k})
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    r = op_client.post("/admin/broadcasts", headers=_op_headers(),
                       json={"audience": "all", "subject": "hey", "body": "f u c k this outage",
                             "severity": "warning"})
    assert r.status_code == 400
    assert r.json()["detail"] == "message blocked: contains disallowed language"
    assert "fuck" not in r.text.lower()
    assert created == {}   # a blocked compose NEVER reaches the store


def test_compose_clean_passes_guard(op_client, monkeypatch):
    captured = {}
    monkeypatch.setattr(admin_mod._broadcasts, "create_broadcast",
                        lambda **k: captured.update(k) or {"id": "bc-1", **k})
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: True)
    r = op_client.post("/admin/broadcasts", headers=_op_headers(),
                       json={"audience": "admins", "subject": "Scheduled maintenance",
                             "body": "Analysis complete; please reset your password.",
                             "severity": "info"})
    assert r.status_code == 200, r.text
    assert captured["audience"] == "admins"
    assert captured["author"] == "operator-key"   # break-glass principal, server-derived


def test_compose_rejects_bad_audience(op_client):
    r = op_client.post("/admin/broadcasts", headers=_op_headers(),
                       json={"audience": "everyone", "subject": "x", "body": "y"})
    assert r.status_code == 400


def test_broadcast_endpoints_require_operator(op_client):
    # no credential → fail-closed (403 operator credential required)
    assert op_client.get("/admin/broadcasts").status_code == 403
    assert op_client.post("/admin/broadcasts",
                          json={"audience": "all", "subject": "x", "body": "y"}).status_code == 403
    assert op_client.get("/admin/alerts").status_code == 403


def test_operator_alerts_payload_shape(op_client, monkeypatch):
    monkeypatch.setattr(admin_mod._alerts, "pending_actions_for_operator",
                        lambda: [{"kind": "reset_approval", "severity": "warning",
                                  "message": "1 tenant-admin reset request awaiting your approval",
                                  "action": {"tab": "reset-approvals", "subtab": None,
                                             "anchor": "op-reset-approvals-panel"}}])
    monkeypatch.setattr(admin_mod._broadcasts, "active_for", lambda realm, pid: [])
    r = op_client.get("/admin/alerts", headers=_op_headers())
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"actions", "broadcasts"}
    assert body["actions"][0]["action"]["anchor"] == "op-reset-approvals-panel"
