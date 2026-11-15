"""Two OPERATOR-console controls that existed in the DB but had no console surface.

  1. ``POST /admin/accounts/{account_id}/dashboard-users/{user_id}/billing-access`` — the
     operator's backstop for ``dashboard_users.can_view_billing`` (migration 159).

     The flag defaults FALSE for EVERY login a tenant mints, INCLUDING the account owner, and
     the only surface that granted it (``PUT /tenant/billing-access``) is owner-gated. A tenant
     with no usable owner login therefore had no way back in. This endpoint is that way back.

  2. ``mfa_prompt_grace_logins`` on ``PUT /admin/platform-settings`` (migration 230). The column,
     the clamp and ``platform_settings.update()`` already honoured it; only the request model was
     missing the field, so the value was DB-tunable but not console-tunable.

What this file pins — the parts that are boundaries, not affordances:

  * the operator endpoint flips the flag BOTH ways and writes an audit row;
  * it is OPERATOR-ONLY — anonymous and a tenant ACCOUNT KEY are both refused, with no write;
  * BOLA — the write is scoped to the account named in the PATH, so an operator naming account A
    cannot touch a login that belongs to account B (404, B untouched);
  * the OWNER invariant survives on BOTH surfaces: the owner may be GRANTED billing but never
    stripped of it, by the operator OR by themselves on the tenant path;
  * ``require_billing_access`` is UNCHANGED — a login without the flag is still 403'd;
  * ``PUT /admin/platform-settings`` now accepts ``mfa_prompt_grace_logins`` and CLAMPS it to
    [0, 100] (a non-number is still a 400), and the write is operator-only.

Every control-plane function is monkeypatched, so this file is green without a DB. The operator
key is set via ``monkeypatch.setenv`` inside a fixture (never at module import) so a full-suite
run cannot have a sibling module's process-wide key decide the result — ``saas/admin.py`` reads
``SAAS_OPERATOR_KEY`` LIVE per request.

Run: python3 -m pytest tests/test_saas_operator_billing_and_mfa_controls.py -q
(tests/ is gitignored → git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import admin as saas_admin
from saas import dashboard_auth as auth
from saas import platform_settings as saas_platform
from saas import tenant_console as tc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

OWNER_A = "11111111-1111-4111-8111-111111111111"   # earliest login of ACCT_A → the OWNER
ADMIN_A = "22222222-2222-4222-8222-222222222222"   # a second login of ACCT_A (not the owner)
OWNER_B = "33333333-3333-4333-8333-333333333333"   # a login of ACCT_B — never touchable via A

KEY_A = "flk_aaaaaaaa_secretA"                     # a TENANT account key — not an operator
OP_KEY = "operator-billing-mfa-super-secret-key"


# ════════════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "operator-billing-mfa-signing-secret-0123456789")
    yield


@pytest.fixture
def op_client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


@pytest.fixture
def tenant_client(saas_on):
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


@pytest.fixture
def audit(monkeypatch):
    """Capture every audit row either router writes."""
    rows = []

    def _rec(action, **kw):
        rows.append({"action": action, **kw})
        return True

    monkeypatch.setattr(saas_admin._audit, "record", _rec)
    monkeypatch.setattr(tc._audit, "record", _rec)
    return rows


@pytest.fixture
def billing_store(monkeypatch):
    """In-memory stand-in for ``dashboard_users.can_view_billing``, keyed (account_id, user_id).

    ``_set`` reproduces the REAL function's account scoping: the UPDATE carries
    ``WHERE id = %s AND account_id = %s``, so a mismatched pair changes no row and returns
    False. That is the BOLA property the endpoint leans on, so the stub must keep it.
    """
    class _Store(dict):
        """A (account_id, user_id) → bool map that also records every write ATTEMPT, so a test
        can tell "refused before the write" from "written then reverted"."""
        calls: list = []

    store = _Store({
        (ACCT_A, OWNER_A): False,     # the deadlock as shipped: a fresh OWNER has no billing
        (ACCT_A, ADMIN_A): False,
        (ACCT_B, OWNER_B): False,
    })
    store.calls = []
    owners = {ACCT_A: OWNER_A, ACCT_B: OWNER_B}

    def _set(account_id, target_user_id, value):
        store.calls.append((account_id, target_user_id, bool(value)))
        key = (account_id, target_user_id)
        if key not in store:
            return False                      # WHERE matched no row of THIS account
        store[key] = bool(value)
        return True

    def _is_owner(account_id, user_id):
        return owners.get(account_id) == user_id

    def _can_view(user_id):
        for (_acct, uid), val in store.items():
            if uid == user_id:
                return val
        return False

    monkeypatch.setattr(saas_admin._dashboard_auth, "set_can_view_billing", _set)
    monkeypatch.setattr(saas_admin._dashboard_auth, "is_account_owner", _is_owner)
    monkeypatch.setattr(saas_admin._dashboard_auth, "can_view_billing", _can_view)
    return store


def _grant(client, account_id, user_id, value, headers=None):
    return client.post(
        f"/admin/accounts/{account_id}/dashboard-users/{user_id}/billing-access",
        json={"can_view_billing": value},
        headers=_op() if headers is None else headers)


# ════════════════════════════════════════════════════════════════════════════════
# 1. the operator endpoint flips can_view_billing BOTH ways, and is audited
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_grants_then_revokes(op_client, billing_store, audit):
    r = _grant(op_client, ACCT_A, ADMIN_A, True)
    assert r.status_code == 200, r.text
    assert r.json() == {"account_id": ACCT_A, "dashboard_user_id": ADMIN_A,
                        "can_view_billing": True}
    assert billing_store[(ACCT_A, ADMIN_A)] is True

    r = _grant(op_client, ACCT_A, ADMIN_A, False)
    assert r.status_code == 200, r.text
    assert r.json()["can_view_billing"] is False
    assert billing_store[(ACCT_A, ADMIN_A)] is False


def test_flip_is_audited_both_ways(op_client, billing_store, audit):
    _grant(op_client, ACCT_A, ADMIN_A, True)
    _grant(op_client, ACCT_A, ADMIN_A, False)
    rows = [a for a in audit if a["action"] == "billing.access_grant"]
    assert len(rows) == 2
    for row in rows:
        assert row["actor_type"] == "operator" and row["result"] == "success"
        assert row["actor_id"] == "operator-key"        # server-derived, never client-supplied
        assert row["account_id"] == ACCT_A
        assert row["target_type"] == "dashboard_user" and row["target_id"] == ADMIN_A
    assert "True" in (rows[0].get("detail") or "")
    assert "False" in (rows[1].get("detail") or "")


def test_unknown_login_is_404(op_client, billing_store, audit):
    ghost = "99999999-9999-4999-8999-999999999999"
    assert _grant(op_client, ACCT_A, ghost, True).status_code == 404
    assert not [a for a in audit if a["action"] == "billing.access_grant"]


# ════════════════════════════════════════════════════════════════════════════════
# 2. OPERATOR-ONLY — a non-operator and an account key are both refused
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("headers", [
    {},                                             # no credential at all
    {"Authorization": f"Bearer {KEY_A}"},           # a TENANT account key is not an operator
    {"Authorization": "Bearer not-the-operator-key"},
], ids=["anonymous", "account-key", "wrong-bearer"])
def test_non_operator_cannot_flip(op_client, billing_store, audit, monkeypatch, headers):
    """An account KEY resolves to a real account elsewhere in this router — it still may not
    reach this lever. The tenant's own path to the flag is the OWNER-gated
    PUT /tenant/billing-access; a key that could self-grant would make that gate meaningless."""
    monkeypatch.setattr(saas_admin._accounts, "resolve_key_to_account",
                        lambda presented: ACCT_A if presented == KEY_A else None)
    r = _grant(op_client, ACCT_A, ADMIN_A, True, headers=headers)
    assert r.status_code == 403, r.text
    assert billing_store[(ACCT_A, ADMIN_A)] is False        # no partial write
    assert not billing_store.calls                      # the store was never even called
    assert not [a for a in audit if a["action"] == "billing.access_grant"]


# ════════════════════════════════════════════════════════════════════════════════
# 3. BOLA — the write is scoped to the account named in the PATH
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_cannot_flip_a_login_of_another_account(op_client, billing_store, audit):
    """OWNER_B belongs to ACCT_B. Naming ACCT_A in the path can never reach it: the UPDATE
    carries both id AND account_id, so it matches no row → 404, and B is untouched."""
    r = _grant(op_client, ACCT_A, OWNER_B, True)
    assert r.status_code == 404, r.text
    assert billing_store[(ACCT_B, OWNER_B)] is False
    assert not [a for a in audit if a["action"] == "billing.access_grant"]


def test_write_uses_the_path_account_never_the_users_own(op_client, billing_store, audit):
    """The account_id handed to set_can_view_billing is the PATH's, verbatim — the handler never
    re-derives it from the target login (which would defeat the scoping above)."""
    _grant(op_client, ACCT_A, OWNER_B, True)
    assert billing_store.calls == [(ACCT_A, OWNER_B, True)]


# ════════════════════════════════════════════════════════════════════════════════
# 4. the OWNER invariant — grantable, never strippable, on BOTH surfaces
# ════════════════════════════════════════════════════════════════════════════════

def test_operator_can_grant_billing_to_the_owner(op_client, billing_store, audit):
    """THE reason this endpoint exists: a fresh owner starts with can_view_billing FALSE and is
    403'd out of the only tenant screen that could grant it."""
    r = _grant(op_client, ACCT_A, OWNER_A, True)
    assert r.status_code == 200, r.text
    assert billing_store[(ACCT_A, OWNER_A)] is True


def test_operator_cannot_strip_the_owner(op_client, billing_store, audit):
    """Stripping the owner would re-create the exact deadlock this control exists to break."""
    _grant(op_client, ACCT_A, OWNER_A, True)
    billing_store.calls.clear()
    r = _grant(op_client, ACCT_A, OWNER_A, False)
    assert r.status_code == 400, r.text
    assert "owner" in r.json()["detail"].lower()
    assert billing_store[(ACCT_A, OWNER_A)] is True     # unchanged
    assert not billing_store.calls                  # refused BEFORE the write


def test_owner_still_cannot_strip_themselves_on_the_tenant_path(tenant_client, billing_store,
                                                                audit):
    """Regression guard on the surface that already existed: the owner-only tenant grant panel
    still refuses to revoke the owner's own billing. Unchanged by this work."""
    billing_store[(ACCT_A, OWNER_A)] = True
    tenant_client.cookies.set(
        auth.SESSION_COOKIE_NAME,
        auth.issue_session(dashboard_user_id=OWNER_A, account_id=ACCT_A, username="owner"))
    r = tenant_client.put(
        "/tenant/billing-access",
        json={"dashboard_user_id": OWNER_A, "can_view_billing": False},
        headers={"x-csrf-token": auth.issue_csrf(dashboard_user_id=OWNER_A)})
    assert r.status_code == 400, r.text
    assert "owner" in r.json()["detail"].lower()
    assert billing_store[(ACCT_A, OWNER_A)] is True


# ════════════════════════════════════════════════════════════════════════════════
# 5. require_billing_access is UNCHANGED — the operator lever did not soften the gate
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("flag,expected", [(False, 403), (True, 200)])
def test_require_billing_access_still_gates_on_the_flag(tenant_client, monkeypatch, flag,
                                                        expected):
    monkeypatch.setattr(tc._auth, "can_view_billing", lambda user_id: flag)
    monkeypatch.setattr(tc._accounts, "get_billing_contact_email", lambda a: None)
    monkeypatch.setattr(tc._accounts, "get_invoice_send_day", lambda a: 1)
    tenant_client.cookies.set(
        auth.SESSION_COOKIE_NAME,
        auth.issue_session(dashboard_user_id=ADMIN_A, account_id=ACCT_A, username="ops"))
    r = tenant_client.get("/tenant/billing-contact")
    assert r.status_code == expected, r.text


# ════════════════════════════════════════════════════════════════════════════════
# 6. PUT /admin/platform-settings accepts + CLAMPS mfa_prompt_grace_logins
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def settings_store(monkeypatch):
    """Capture what actually reaches platform_settings.update() (no DB)."""
    captured = {}

    def _update(changes, updated_by=None):
        captured.update(changes)
        captured["_updated_by"] = updated_by
        return {"ok": True, "status_code": 200, "settings": dict(changes)}

    monkeypatch.setattr(saas_admin._platform_settings, "update", _update)
    monkeypatch.setattr(saas_admin._audit, "record", lambda *a, **k: None)
    return captured


def _put_grace(client, value):
    return client.put("/admin/platform-settings",
                      json={"mfa_prompt_grace_logins": value}, headers=_op())


@pytest.mark.parametrize("good", [0, 1, 10, 50, 100])
def test_in_range_value_reaches_the_store(op_client, settings_store, good):
    """0 is a VALID configured value (non-dismissible from the first sign-in), not "unset" —
    the handler filters on ``is not None``, so it must survive the trip."""
    r = _put_grace(op_client, good)
    assert r.status_code == 200, r.text
    assert settings_store["mfa_prompt_grace_logins"] == good


@pytest.mark.parametrize("given,stored", [(-1, 0), (-500, 0), (101, 100), (100000, 100)])
def test_out_of_range_is_clamped_not_rejected(op_client, settings_store, given, stored):
    """CLAMPED, deliberately — this setting's contract is a clamp everywhere else it is touched
    (the reader clamps too), so a 400 here would still leave a stored value that disagrees with
    the effective one."""
    r = _put_grace(op_client, given)
    assert r.status_code == 200, r.text
    assert settings_store["mfa_prompt_grace_logins"] == stored


def test_clamp_matches_the_modules_own_bounds(op_client, settings_store):
    """The router clamps to platform_settings' OWN constants, so the two can never drift."""
    _put_grace(op_client, 10 ** 6)
    assert settings_store["mfa_prompt_grace_logins"] == \
        saas_platform._MAX_MFA_PROMPT_GRACE_LOGINS
    _put_grace(op_client, -(10 ** 6))
    assert settings_store["mfa_prompt_grace_logins"] == \
        saas_platform._MIN_MFA_PROMPT_GRACE_LOGINS


def test_non_numeric_is_a_422_or_400(op_client, settings_store):
    """A malformed value is a bad REQUEST, not a range to clamp — and nothing reaches the store."""
    r = _put_grace(op_client, "ten")
    assert r.status_code in (400, 422), r.text
    assert "mfa_prompt_grace_logins" not in settings_store


def test_grace_write_is_operator_only(op_client, settings_store):
    for headers in ({}, {"Authorization": f"Bearer {KEY_A}"}):
        r = op_client.put("/admin/platform-settings",
                          json={"mfa_prompt_grace_logins": 3}, headers=headers)
        assert r.status_code == 403, r.text
    assert "mfa_prompt_grace_logins" not in settings_store


def test_grace_rides_the_bulk_save_alongside_its_neighbours(op_client, settings_store):
    """The console's bulk save posts every field at once — the new one must not disturb the
    validation of the settings around it."""
    r = op_client.put("/admin/platform-settings",
                      json={"trial_days": 14, "retention_grace_days": 3,
                            "mfa_prompt_grace_logins": 250}, headers=_op())
    assert r.status_code == 200, r.text
    assert settings_store["trial_days"] == 14
    assert settings_store["retention_grace_days"] == 3
    assert settings_store["mfa_prompt_grace_logins"] == 100      # clamped, others untouched


def test_the_field_exists_on_the_request_model():
    """The ONLY thing that was missing: the column, the clamp and update() already honoured it."""
    assert "mfa_prompt_grace_logins" in saas_admin.PlatformSettingsRequest.model_fields
    assert "mfa_prompt_grace_logins" in saas_platform._SETTABLE
    assert "mfa_prompt_grace_logins" in saas_platform._COLS
