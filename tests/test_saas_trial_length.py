"""Operator-configurable FREE-TRIAL LENGTH valve (migration 178) — CLOSED-SOURCE SaaS layer.

The first-month-free trial length was a hardcoded constant (saas/plans.py TRIAL_DAYS = 30). It is
now an OPERATOR-owned platform setting the global admin changes from the console — no redeploy.

What this pins:
  * the RESOLVER (saas/plans.trial_days) reads the operator-configured value, CLAMPS it to
    [0, 365], and FAIL-SAFES to the historical 30 on any error / missing value;
  * the platform-settings getter (saas/platform_settings.trial_days) fail-safes to 30 when the
    control plane is unreadable;
  * the operator write endpoint bounds-checks SERVER-SIDE — <0 or >365 is a 400 (not a 422), a
    valid value reaches the store;
  * the GET/SET endpoints are OPERATOR-gated — no credential (or a non-operator credential) → 403.

SAAS_MODE (which decides the import-time SaaS wiring) is the suite-wide baseline set once in
tests/conftest.py. This module's own operator key is bound PER TEST below — never at import,
which is process-wide and let a sibling module's different key decide this file's result.
The rejection paths reject BEFORE any DB access (gate → validation → DB), so they run WITHOUT a
control-plane DB. tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug TRIAL-LENGTH --test tests/test_saas_trial_length.py
"""

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import saas.admin as admin
from saas import plans as saas_plans
from saas import platform_settings as saas_platform

OP_KEY = "trial-length-op-super-secret-key"


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key for the duration of ONE test, then restore.

    ``saas/admin.py`` reads SAAS_OPERATOR_KEY LIVE per request, so function-scoped monkeypatch is
    exactly the right lifetime — and collection order can no longer decide the result.
    """
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)



def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def _clear_cache():
    """platform_settings caches globally — reset around every test so state never leaks."""
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


# ══════════════════════════════════════════════════════════════════════════════════
# (a) the RESOLVER reads the setting, clamps, and fails safe to 30
# ══════════════════════════════════════════════════════════════════════════════════

def test_resolver_reads_configured_value(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"trial_days": 45})
    assert saas_platform.trial_days() == 45
    assert saas_plans.trial_days() == 45


def test_resolver_clamps_high_value(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"trial_days": 100000})
    assert saas_plans.trial_days() == 365


def test_resolver_clamps_negative_to_zero(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"trial_days": -5})
    assert saas_plans.trial_days() == 0


def test_resolver_zero_is_a_valid_no_trial_value(monkeypatch):
    """0 = no free trial — a legitimate configured value, NOT coerced back to 30."""
    monkeypatch.setattr(saas_platform, "state", lambda **k: {"trial_days": 0})
    assert saas_plans.trial_days() == 0


def test_resolver_missing_key_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **k: {})   # no trial_days key
    assert saas_platform.trial_days() == 30
    assert saas_plans.trial_days() == 30


def test_resolver_falls_back_when_getter_raises(monkeypatch):
    def _boom():
        raise RuntimeError("control plane unreachable")

    monkeypatch.setattr(saas_platform, "trial_days", _boom)
    assert saas_plans.trial_days() == 30


def test_platform_getter_failsafe_when_db_unreadable(monkeypatch):
    """DB down + nothing cached → the getter resolves to the hardcoded default, never 0."""
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(saas_platform.db, "connection", _boom)
    assert saas_platform.trial_days() == 30
    assert saas_plans.trial_days() == 30


# ══════════════════════════════════════════════════════════════════════════════════
# (b) SERVER-SIDE bounds validation on the write endpoint (400, not 422)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [-1, -30, 366, 1000])
def test_out_of_range_is_rejected_400(client, bad):
    """Rejected at validation, BEFORE any DB write — so this needs no control plane."""
    r = client.put("/admin/platform-settings", json={"trial_days": bad}, headers=_op())
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("good", [0, 1, 30, 365])
def test_valid_value_passes_validation_and_reaches_the_store(client, monkeypatch, good):
    captured = {}

    def _stub_update(changes, updated_by=None):
        captured.update(changes)
        return {"ok": True, "status_code": 200, "settings": {"trial_days": changes.get("trial_days")}}

    monkeypatch.setattr(admin._platform_settings, "update", _stub_update)
    monkeypatch.setattr(admin._audit, "record", lambda *a, **k: None)

    r = client.put("/admin/platform-settings", json={"trial_days": good}, headers=_op())
    assert r.status_code == 200, r.text
    assert captured["trial_days"] == good     # int, in-range, reached the store


# ══════════════════════════════════════════════════════════════════════════════════
# (c)/(d) operator gate — no credential / a non-operator credential cannot GET or SET
# ══════════════════════════════════════════════════════════════════════════════════

def test_get_requires_operator(client):
    assert client.get("/admin/platform-settings").status_code == 403


def test_set_requires_operator(client):
    r = client.put("/admin/platform-settings", json={"trial_days": 14})
    assert r.status_code == 403


def test_non_operator_credential_cannot_set(client):
    """A bearer that is NOT the operator key (a tenant/account key, a leaked string) is denied —
    a non-operator session can never move the platform trial length."""
    r = client.put("/admin/platform-settings", json={"trial_days": 14},
                   headers={"Authorization": "Bearer not-the-operator-key"})
    assert r.status_code == 403


def test_non_operator_credential_cannot_read(client):
    r = client.get("/admin/platform-settings",
                   headers={"Authorization": "Bearer not-the-operator-key"})
    assert r.status_code == 403
