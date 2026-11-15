"""BILLGATE — the contractual/billing figures are password-gated, and a configuration change
applies to NEW SIGN-UPS ONLY.

THE OWNER'S INSTRUCTION, verbatim, is the specification for both halves of this file:

    "don't know what you've called it for those figures/amounts, but of note they should be
     intentionally read-only(displayed as read-only) with an "edit" button, requiring password
     authorization to not accidentally fuck up the billing. Further any changes to those amounts
     is ONLY for new sign-ups, historic must be maintained as it's accounting!"

PART 1 — the GATE. A write to a protected figure without a verified operator password is REFUSED
and the row is untouched; with one it succeeds and is audited old→new; a wrong password is
refused AND recorded.

PART 2 — RETROACTIVITY. The headline test is
``test_changing_the_configured_term_does_not_reterm_an_existing_account_at_renewal``: move the
console term 6 → 12, drive a real ``invoice.paid`` renewal for an EXISTING account, and its
``term_months`` must still be 6 — while a subscription purchased AFTERWARDS gets 12.

⚠️ WHY THE DEFECT WAS INVISIBLE, AND WHY THE TWO FIXES ARE ONE CHANGE.
``tenant_provisioning.tenant_term_months()`` looked the setting up as
``platform_settings.tenant_contract_term_months`` — a name that exists nowhere. ``getattr``
returned None, the call fell through to ``entitlements.DEFAULT_TERM_MONTHS`` (6), and the
retroactive UPDATE was therefore always a no-op re-stamp of 6 → 6. Repairing that name — which
had to happen for the operator's console setting to work AT ALL — is exactly what would have made
the retroactive re-term live on every existing tenant's next invoice. So
``test_the_configured_term_is_actually_read`` and the headline test are two halves of one change,
and neither is safe to land alone.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_billgate \\
    python3 tools/fltest.py --bug BILLGATE --test tests/test_billing_settings_gate.py

THROWAWAY DB: apply every migration numbered >= 131 to an empty database (migration 248 included
— the term-of-record latch lives there). It must NOT be a real tenant database.

FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the LAST test. A
DSN-gated suite that skips its DB half reports "PASS | N passed, M skipped" and looks green; that
has produced a false green in this repo repeatedly.
"""

import os
import sys
import time
import uuid

import pytest

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.mcp.http_server as h            # noqa: E402
import saas.admin as admin                 # noqa: E402  (also registers the router)
from saas import dashboard_auth as A       # noqa: E402
from saas import platform_settings as saas_platform   # noqa: E402
from saas import tenant_provisioning as prov          # noqa: E402
from saas import tenant_checkout as checkout          # noqa: E402
from saas import accounts as accounts_mod             # noqa: E402
from saas import entitlements as ent_mod              # noqa: E402
from saas import stripe_webhooks as swh               # noqa: E402

OP_KEY = "billgate-operator-break-glass-key-do-not-use-anywhere-real"
OP_PASSWORD = "correct-horse-battery-staple-billgate"
WRONG_PASSWORD = "not-the-password-at-all"

_DB_RAN = {"value": False}


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


requires_db = pytest.mark.skipif(not _dsn(), reason="POSTGRES_DSN not set — control plane needed")


# ════════════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    # A session secret is required to mint a real operator session cookie. Set per-test so
    # collection order can never decide this file's result.
    monkeypatch.setenv("SAAS_SESSION_SECRET", "billgate-session-secret-0123456789abcdef")


@pytest.fixture(autouse=True)
def _clear_cache():
    """platform_settings caches process-wide (5s TTL). Reset around EVERY test — a cached term
    leaking between tests would make the retroactivity assertions read a stale number and pass
    for the wrong reason."""
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture
def db(monkeypatch):
    dsn = _dsn()
    if not dsn:
        pytest.skip("POSTGRES_DSN not set")
    if "test" not in dsn.rsplit("/", 1)[-1] and "billgate" not in dsn.rsplit("/", 1)[-1]:
        pytest.skip(f"refusing to run against a non-test database: {dsn}")
    import psycopg2
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.accounts'), "
                    "       to_regclass('public.platform_settings'), "
                    "       to_regclass('public.dashboard_users'), "
                    "       to_regclass('public.saas_audit_log')")
        present = cur.fetchone()
        cur.execute("SELECT 1 FROM pg_attribute "
                    " WHERE attrelid = to_regclass('public.accounts') "
                    "   AND attname = 'term_months_set_at' AND NOT attisdropped")
        latch = cur.fetchone()
    if not all(present):
        conn.close()
        pytest.skip("throwaway DB is missing the SaaS control plane — see this file's header")
    if latch is None:
        conn.close()
        pytest.skip("migration 248 (accounts.term_months_set_at) not applied to the test DB")
    _DB_RAN["value"] = True
    yield conn
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.platform_settings SET data_retention_days = 30, "
                        "backup_overwrite_days = 35, fulfilment_days = 5, "
                        "retention_grace_days = 7, contract_term_months = 6 "
                        "WHERE singleton = TRUE")
    finally:
        conn.close()
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


@pytest.fixture
def operator(db):
    """A REAL operator login with a REAL argon2id hash, and a REAL signed session + CSRF token.

    Not a mock. The whole point of this suite is the password path, so mocking
    ``reauthenticate_operator`` would be pinning the test's own stub instead of the gate."""
    username = f"billgate-op-{uuid.uuid4().hex[:10]}"
    pw_hash = A.hash_password(OP_PASSWORD)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO public.dashboard_users (account_id, username, password_hash, role, "
            "status) VALUES (NULL, %s, %s, 'operator', 'active') RETURNING id::text",
            (username, pw_hash),
        )
        op_id = cur.fetchone()[0]
    session = A.issue_operator_session(dashboard_user_id=op_id, username=username)
    csrf = A.issue_operator_csrf(dashboard_user_id=op_id)
    return {"id": op_id, "username": username, "session": session, "csrf": csrf}


def _session_put(client, operator, payload):
    """PUT /admin/platform-settings as the signed-in OPERATOR SESSION (with CSRF).

    Deliberately NOT the ``Authorization: Bearer <SAAS_OPERATOR_KEY>`` door — that is the
    break-glass key, it carries no password, and the gate refuses it for protected fields. Using
    the session here is what makes these tests exercise the real console path."""
    client.cookies.set(A.OPERATOR_SESSION_COOKIE_NAME, operator["session"])
    return client.put("/admin/platform-settings", json=payload,
                      headers={"x-csrf-token": operator["csrf"]})


def _row(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT data_retention_days, backup_overwrite_days, fulfilment_days, "
                    "retention_grace_days, contract_term_months "
                    "FROM public.platform_settings WHERE singleton = TRUE")
        return cur.fetchone()


def _set_term_setting(conn, months):
    """Move the EFFECTIVE tenant contract term to ``months``.

    ⚠️ THIS HELPER GREW A SECOND WRITE, AND THE REASON IS A DELIBERATE CHANGE OF AUTHORITY — read
    this before "simplifying" it back to one UPDATE.

    It used to write ``platform_settings.contract_term_months`` alone, because that setting WAS
    the authority for ``tenant_term_months()``. It no longer is. Migration 256 freezes
    ``CONTRACT_TERM_MONTHS`` onto each published contract template, and the agreement the customer
    reads renders from that frozen value — so the ENFORCED term is now taken from the ACTIVE
    TEMPLATE, with the platform setting as the fallback for a template that carries none.
    (Otherwise the agreement said "6-month term" while the tick-box beneath it authorized 24
    months and CA$36,000; see saas/contracts.py::active_contract_term_months.)

    Every assertion in the term tests below is unchanged in INTENT — "a new subscription gets the
    configured term", "an existing account is never re-termed" — but the term has to be moved
    where it now comes from, or the tests are only measuring a fallback that never fires. The
    operator-facing meaning is the same and is the point of the change: an operator changes the
    term by publishing a new agreement that states it, not by editing a number underneath a
    document that says something else."""
    with conn.cursor() as cur:
        cur.execute("UPDATE public.platform_settings SET contract_term_months = %s "
                    "WHERE singleton = TRUE", (int(months),))
        # The AUTHORITY: the active (non-retired) template's own frozen number. Guarded on the
        # column and on a row existing, so this helper still works against a control plane where
        # migration 246 has not run — there it degrades to exactly the old single-write behaviour.
        cur.execute("SELECT 1 FROM pg_attribute "
                    " WHERE attrelid = to_regclass('public.contract_templates') "
                    "   AND attname = 'retention_values' AND NOT attisdropped")
        if cur.fetchone() is not None:
            cur.execute(
                "UPDATE public.contract_templates "
                "   SET retention_values = "
                "       COALESCE(retention_values, '{}'::jsonb) "
                "       || jsonb_build_object('CONTRACT_TERM_MONTHS', %s::int) "
                " WHERE retired_at IS NULL", (int(months),))
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


def _audit_since(conn, watermark, action):
    """Audit rows for ``action`` newer than ``watermark``.

    A WATERMARK, never a DELETE: ``public.saas_audit_log`` is append-only via a row trigger, so a
    cleanup DELETE passes the first run (0 matching rows → the trigger never fires) and fails the
    second. A test that clears an append-only table is a test that only works once."""
    with conn.cursor() as cur:
        cur.execute("SELECT result, detail FROM public.saas_audit_log "
                    " WHERE action = %s AND ts > %s ORDER BY ts", (action, watermark))
        return cur.fetchall()


def _now(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT NOW()")
        return cur.fetchone()[0]


# ════════════════════════════════════════════════════════════════════════════════════
# PART 0 — the declaration itself
# ════════════════════════════════════════════════════════════════════════════════════

def test_the_protected_set_is_exactly_the_four_contractual_figures():
    """WHAT IS GATED IS A DELIBERATE, NARROW LIST — and this test is where the scoping decision
    is recorded, so widening or narrowing it is a conscious act rather than a drift.

    IN: the three Terms-of-Service windows and the tenant contract term. Each is substituted
    VERBATIM into a published Terms version and, once anybody signs it, frozen permanently in an
    append-only legal record; the term additionally governs billing. A slip here is not typeable
    back.

    OUT, and each for a stated reason:
      * ``offline_mode`` — ⛔ MUST NEVER BE GATED. This console is exempt from the offline gate
        precisely so the switch cannot strand itself ON. A password in front of it re-creates
        that one-way trap by another route.
      * ``retention_grace_days`` — the close call. Same panel, and it does bound irreversible
        deletion. Still out: it is in no signed agreement and bills nobody (the owner's concern
        was "the BILLING"); the console copy already says it is "NOT one of the three contractual
        windows; do not conflate", and gating it beside them re-conflates exactly what that copy
        separates; and it is a designed bulk-save valve whose contract is pinned by
        ``test_saas_operator_billing_and_mfa_controls::
        test_grace_rides_the_bulk_save_alongside_its_neighbours``. Its reaper is DRY-RUN unless
        ``SAAS_RETENTION_PURGE_ENABLED`` is armed — if purge is ever armed by default, revisit
        this decision FIRST.
      * the rest — reversible operational valves. Gating them would spend the operator's
        attention where a mistake costs nothing, which is how a confirmation prompt decays into a
        reflex and stops protecting the four that matter."""
    assert set(saas_platform.PROTECTED_SETTINGS) == {
        "data_retention_days", "backup_overwrite_days", "fulfilment_days",
        "contract_term_months",
    }
    for never in ("offline_mode", "offline_message", "trial_days", "require_signup_approval",
                  "notify_email", "welcome_from_address", "mfa_prompt_grace_logins",
                  "re_embedder_enabled", "email_kill_switch", "retention_grace_days"):
        assert never not in saas_platform.PROTECTED_SETTINGS, (
            f"{never} must stay ungated — see this test's docstring")


def test_protected_changes_is_a_deterministic_intersection():
    assert saas_platform.protected_changes({"trial_days": 30}) == ()
    assert saas_platform.protected_changes({"contract_term_months": 12}) == ("contract_term_months",)
    assert saas_platform.protected_changes({"retention_grace_days": 7}) == ()
    got = saas_platform.protected_changes({"fulfilment_days": 5, "trial_days": 1,
                                           "backup_overwrite_days": 40})
    assert set(got) == {"fulfilment_days", "backup_overwrite_days"}


def test_a_malformed_changes_object_is_treated_as_maximally_protected():
    """FAIL-SAFE TOWARD PROTECTION. Something that is not a mapping must be refused by the gate,
    not waved past it because the intersection came out empty."""
    assert saas_platform.protected_changes(object()) == tuple(saas_platform.PROTECTED_SETTINGS)


# ════════════════════════════════════════════════════════════════════════════════════
# PART 1 — the RE-AUTHORIZATION helper, in isolation
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_reauthenticate_operator_accepts_the_right_password(db, operator):
    v = A.reauthenticate_operator(operator["id"], OP_PASSWORD)
    assert v == {"ok": True, "reason": "ok"}


@requires_db
@pytest.mark.parametrize("password,reason", [
    (WRONG_PASSWORD, "bad_password"),
    ("", "missing_password"),
    (None, "missing_password"),
    ("   ", "missing_password"),
])
def test_reauthenticate_operator_refuses_everything_else(db, operator, password, reason):
    v = A.reauthenticate_operator(operator["id"], password)
    assert v["ok"] is False and v["reason"] == reason


def test_reauthenticate_operator_refuses_an_unknown_principal():
    assert A.reauthenticate_operator(str(uuid.uuid4()), OP_PASSWORD)["ok"] is False
    assert A.reauthenticate_operator("", OP_PASSWORD)["ok"] is False


def test_reauthenticate_operator_refuses_when_the_auth_backend_is_unavailable(monkeypatch):
    """FAIL CLOSED ON AN UNREADABLE BACKEND. ``verify_password`` RAISES ``DashboardAuthError``
    when argon2-cffi is absent. An exception on a re-authorization gate must become a REFUSAL,
    never an escape past it — a gate that opens when it cannot check is not a gate."""
    monkeypatch.setattr(A, "get_operator_by_id",
                        lambda _id: {"id": _id, "password_hash": "$argon2id$fake"})

    def _boom(_h, _p):
        raise A.DashboardAuthError("argon2-cffi unavailable — cannot verify passwords")

    monkeypatch.setattr(A, "verify_password", _boom)
    v = A.reauthenticate_operator("op-1", OP_PASSWORD)
    assert v["ok"] is False and v["reason"] == "auth_backend_unavailable"


def test_reauthenticate_operator_refuses_a_row_with_no_password_hash(monkeypatch):
    monkeypatch.setattr(A, "get_operator_by_id", lambda _id: {"id": _id, "password_hash": None})
    v = A.reauthenticate_operator("op-1", OP_PASSWORD)
    assert v["ok"] is False and v["reason"] == "no_password_set"


# ════════════════════════════════════════════════════════════════════════════════════
# PART 2 — the ROUTE: refused without a password, applied with one, audited either way
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_protected_write_without_a_password_is_refused_and_the_row_is_unchanged(
        db, client, operator):
    """THE HEADLINE REFUSAL. Not "it warns" — the row must be byte-identical afterwards."""
    before = _row(db)
    r = _session_put(client, operator, {"contract_term_months": 24})
    assert r.status_code == 401, r.text
    assert _row(db) == before, "a refused write must not change the row"


@requires_db
def test_a_wrong_password_is_refused_and_recorded(db, client, operator):
    """Refused AND findable. A failed attempt on the billing configuration is precisely the
    event an operator needs to discover later, and an exception would otherwise discard it."""
    mark = _now(db)
    before = _row(db)
    r = _session_put(client, operator,
                     {"contract_term_months": 24, "confirm_password": WRONG_PASSWORD})
    assert r.status_code == 401, r.text
    assert _row(db) == before
    rows = _audit_since(db, mark, "platform.protected_settings_denied")
    assert rows, "a wrong password on the billing configuration was not recorded"
    assert any(res == "denied" and "bad_password" in (detail or "") for res, detail in rows), rows


@requires_db
def test_a_correct_password_applies_the_change_and_audits_old_to_new(db, client, operator):
    mark = _now(db)
    r = _session_put(client, operator,
                     {"contract_term_months": 9, "confirm_password": OP_PASSWORD})
    assert r.status_code == 200, r.text
    assert _row(db)[4] == 9
    rows = _audit_since(db, mark, "platform.protected_settings")
    assert rows, "a successful protected change must be audited"
    result, detail = rows[-1]
    assert result == "success"
    assert "contract_term_months: 6→9" in detail, detail
    assert "password re-authorized" in detail, detail


@requires_db
def test_an_unprotected_field_still_saves_with_no_password(db, client, operator):
    """The gate must not become a tax on every setting. trial_days is ungated by design."""
    r = _session_put(client, operator, {"trial_days": 21})
    assert r.status_code == 200, r.text


@requires_db
def test_the_purge_grace_stays_ungated_and_still_rides_the_key_door(db, client):
    """THE SCOPING DECISION, ASSERTED AS BEHAVIOUR rather than only as a constant. It shares the
    Retention panel with the three contractual windows but is not one of them — see
    ``test_the_protected_set_is_exactly_the_four_contractual_figures``. If this goes red the gate
    has widened; re-read that docstring before "fixing" it."""
    r = client.put("/admin/platform-settings", json={"retention_grace_days": 9},
                   headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200, r.text
    assert _row(db)[3] == 9


@requires_db
def test_the_offline_switch_is_never_password_gated(db, client, operator):
    """⛔ THE ONE THAT MUST NEVER BE GATED. The offline gate exempts this console so the switch
    cannot strand itself ON. A password in front of it re-creates that one-way trap by another
    route — an operator who cannot recall their password could not bring the product back up."""
    try:
        r = _session_put(client, operator, {"offline_mode": True})
        assert r.status_code == 200, r.text
    finally:
        _session_put(client, operator, {"offline_mode": False})


@requires_db
def test_the_break_glass_operator_key_cannot_change_a_protected_figure(db, client):
    """The static key is shared, lives in an environment variable and never expires. It keeps
    every other setting — including the offline switch — and is refused for the figures that
    end up in a signed agreement and in billing."""
    before = _row(db)
    r = client.put("/admin/platform-settings", json={"contract_term_months": 36},
                   headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 403, r.text
    assert _row(db) == before
    # …and it still works for an ungated one.
    assert client.put("/admin/platform-settings", json={"trial_days": 14},
                      headers={"Authorization": f"Bearer {OP_KEY}"}).status_code == 200


@requires_db
def test_an_unauthenticated_caller_cannot_reach_the_gate_at_all(db, client):
    before = _row(db)
    assert client.put("/admin/platform-settings",
                      json={"contract_term_months": 36}).status_code == 403
    assert _row(db) == before


@requires_db
def test_a_refused_protected_write_does_not_partially_apply_its_UNPROTECTED_siblings(
        db, client, operator):
    """The gate runs BEFORE any value is written, so an unprotected field riding the same
    refused request is not applied either. A partial apply on a refused write is the worst of
    both answers."""
    before = _row(db)
    r = _session_put(client, operator, {"contract_term_months": 30, "trial_days": 99})
    assert r.status_code == 401
    assert _row(db) == before
    st = saas_platform.state(fresh=True)
    assert st["trial_days"] != 99, "an unprotected sibling leaked through a refused write"


@requires_db
def test_the_password_is_never_echoed_or_stored(db, client, operator):
    mark = _now(db)
    r = _session_put(client, operator,
                     {"fulfilment_days": 7, "confirm_password": OP_PASSWORD})
    assert r.status_code == 200, r.text
    assert OP_PASSWORD not in r.text
    for _res, detail in _audit_since(db, mark, "platform.protected_settings"):
        assert OP_PASSWORD not in (detail or "")
    with db.cursor() as cur:
        cur.execute("SELECT updated_by FROM public.platform_settings WHERE singleton = TRUE")
        assert OP_PASSWORD not in str(cur.fetchone()[0] or "")


@requires_db
def test_the_get_route_tells_the_console_which_fields_are_locked(db, client):
    r = client.get("/admin/platform-settings", headers={"Authorization": f"Bearer {OP_KEY}"})
    assert r.status_code == 200, r.text
    assert set(r.json()["protected_settings"]) == set(saas_platform.PROTECTED_SETTINGS)


# ════════════════════════════════════════════════════════════════════════════════════
# PART 3 — 🔴 NEW SIGN-UPS ONLY. HISTORIC IS ACCOUNTING.
# ════════════════════════════════════════════════════════════════════════════════════

def _mk_account(conn, *, kind="team"):
    slug = f"bg{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.accounts (name, slug, plan, account_type, account_kind, "
            "seat_limit, status) VALUES (%s, %s, 'standard', 'standard', %s, %s, 'active') "
            "RETURNING account_id::text",
            (slug, slug, kind, int(accounts_mod.hard_seat_cap(kind))),
        )
        aid = cur.fetchone()[0]
    return aid


def _term(conn, aid):
    with conn.cursor() as cur:
        cur.execute("SELECT term_months, term_months_set_at, term_months_source "
                    "FROM public.accounts WHERE account_id = %s", (aid,))
        return cur.fetchone()


def _purchase(aid, tier="enterprise"):
    """The PURCHASE moment — ``checkout.session.completed`` carries a Checkout Session id."""
    return prov.provision_from_checkout(
        account_id=aid, tier_row=checkout.get_tier(tier),
        subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
        session_id=f"cs_{uuid.uuid4().hex[:12]}", contract_id=1)


def _renewal(aid, tier="enterprise"):
    """The RENEWAL moment — ``invoice.paid`` passes ``session_id=None``."""
    return prov.provision_from_checkout(
        account_id=aid, tier_row=checkout.get_tier(tier),
        subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
        session_id=None, contract_id=None)


@requires_db
def test_the_configured_term_is_actually_read(db):
    """⚠️ THE MASKING BUG. ``tenant_term_months()`` looked up
    ``platform_settings.tenant_contract_term_months`` — a name that exists NOWHERE — so it always
    fell through to ``DEFAULT_TERM_MONTHS`` and the operator's console setting was dead on this
    path. If this goes red, the getter name has drifted again and the CONTRACT group in the
    operator console is decorative.

    ⚠️ WIDENED, NOT WEAKENED. The setting is no longer the top authority — the ACTIVE AGREEMENT'S
    frozen term is, with the setting as the fallback (see ``_set_term_setting``). So this now pins
    BOTH halves: the getter still resolves the console value, AND the resolution order is right.
    A test that only checked the setting could not tell the two apart, and the whole defect being
    fixed is that the machine and the document disagreed."""
    # ESTABLISH the baseline, never assume it. ``contract_templates`` allows exactly one active
    # row, so any sibling suite that publishes a template becomes "the agreement in force" and
    # brings its own frozen term with it — measured: this line read 8 after another suite ran.
    # Under the old authority the baseline came from the platform singleton, which this file
    # already controlled; under the new one it comes from a row this file does not own.
    _set_term_setting(db, 6)
    assert prov.tenant_term_months() == 6

    # (a) THE ANTI-DRIFT CHECK THIS TEST WAS WRITTEN FOR: the console's value is really read.
    with db.cursor() as cur:
        cur.execute("UPDATE public.platform_settings SET contract_term_months = 11 "
                    "WHERE singleton = TRUE")
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    try:
        assert saas_platform.contract_term_months() == 11, (
            "the operator-configured tenant term is NOT being read — check that "
            "platform_settings.contract_term_months still exists and is resolved")

        # (b) THE AUTHORITY. The agreement in force says 6; the setting says 11. The enforced
        # term must follow the DOCUMENT, or the customer signs one number and is billed another.
        assert prov.tenant_term_months() == 6, (
            "tenant_term_months() followed the live setting over the agreement in force — the "
            "document/machine divergence is back")

        # (c) THE FALLBACK. With no term on the active template, the setting governs again, so
        # the console control is not decorative on a control plane that has no frozen value.
        with db.cursor() as cur:
            cur.execute("UPDATE public.contract_templates "
                        "   SET retention_values = retention_values - 'CONTRACT_TERM_MONTHS' "
                        " WHERE retired_at IS NULL")
        assert prov.tenant_term_months() == 11, (
            "with no term stated by the active agreement, the configured setting must govern")
    finally:
        _set_term_setting(db, 6)


@requires_db
def test_a_new_subscription_gets_the_configured_term_and_it_is_put_of_record(db):
    _set_term_setting(db, 12)
    aid = _mk_account(db)
    assert _purchase(aid)["provisioned"] is True
    term, set_at, source = _term(db, aid)
    assert term == 12, "a NEW subscription must get the configured term"
    assert set_at is not None, "the term must be latched of record at purchase"
    assert source == "provision"


@requires_db
def test_changing_the_configured_term_does_not_reterm_an_existing_account_at_renewal(db):
    """🔴🔴 THE HEADLINE TEST — the owner's rule, on real rows.

    An account buys at a 6-month term. The operator later moves the console setting to 12. The
    account's NEXT RENEWAL — ``invoice.paid``, which re-provisions every month forever — must
    leave it at 6, because 6 is what that customer signed and historic terms are accounting.
    A subscription purchased AFTER the change gets 12.

    The old code did the opposite: ``_machine_written_terms()`` included the configured value, so
    ``UPDATE … WHERE term_months = ANY(6,12)`` matched the existing 6 and silently rewrote it."""
    _set_term_setting(db, 6)
    existing = _mk_account(db)
    _purchase(existing)
    assert _term(db, existing)[0] == 6

    # The operator changes the platform setting.
    _set_term_setting(db, 12)
    assert prov.tenant_term_months() == 12, "precondition: the new setting must be live"

    # …and the EXISTING account renews. Repeatedly — a renewal happens every month, forever.
    for _ in range(3):
        _renewal(existing)
        assert _term(db, existing)[0] == 6, (
            "RETROACTIVE RE-TERM: an existing customer was moved to the new configured term at "
            "renewal, against the term they signed")

    # A NEW sign-up, after the change, does get the new term.
    fresh = _mk_account(db)
    _purchase(fresh)
    assert _term(db, fresh)[0] == 12, "a NEW subscription must get the newly configured term"

    _set_term_setting(db, 6)


@requires_db
def test_a_legacy_account_meeting_this_code_at_a_renewal_is_frozen_not_re_termed(db):
    """THE MIGRATION CASE, and the one that decides whether the fix protects the EXISTING
    customer base or only accounts created after it ships.

    A legacy row pre-dates the term-of-record latch: ``term_months_set_at`` is NULL and
    ``term_months`` is 6 because that is simply the schema default. Live configuration must not
    reach it either — we have no evidence this customer ever agreed to today's number — so it is
    FROZEN at what it has been running on and stamped ``legacy_frozen``."""
    _set_term_setting(db, 6)
    legacy = _mk_account(db)
    _purchase(legacy)
    # Force the row back to the pre-248 shape: a term, no latch.
    with db.cursor() as cur:
        cur.execute("UPDATE public.accounts SET term_months_set_at = NULL, "
                    "term_months_source = NULL WHERE account_id = %s", (legacy,))
    _set_term_setting(db, 18)
    _renewal(legacy)
    term, set_at, source = _term(db, legacy)
    assert term == 6, "a legacy account was re-termed from live configuration at its renewal"
    assert set_at is not None and source == "legacy_frozen"
    # …and it stays frozen on subsequent renewals.
    _renewal(legacy)
    assert _term(db, legacy)[0] == 6
    _set_term_setting(db, 6)


@requires_db
def test_an_operator_set_term_still_survives_a_renewal(db):
    """UNCHANGED BEHAVIOUR, RE-PINNED. ``entitlements.seed_base_entitlement`` ends with an
    UNCONDITIONAL re-stamp of its own 6-month default and runs on EVERY renewal, so merely
    declining to write would still leave an operator's 24 destroyed — by a different function.
    Authority order: user-stated > seed > growth."""
    _set_term_setting(db, 6)
    aid = _mk_account(db)
    _purchase(aid)
    with db.cursor() as cur:
        cur.execute("UPDATE public.accounts SET term_months = 24 WHERE account_id = %s", (aid,))
    for _ in range(2):
        _renewal(aid)
        assert _term(db, aid)[0] == 24, "an operator's deliberate term was clobbered"


@requires_db
def test_a_stripe_redelivery_of_the_purchase_does_not_re_decide_the_term(db):
    """Stripe redelivers for up to three days. A redelivered purchase is still a purchase, but
    the latch is already stamped from the first delivery, so a configuration change made in
    between cannot reach the account."""
    _set_term_setting(db, 6)
    aid = _mk_account(db)
    _purchase(aid)
    assert _term(db, aid)[0] == 6
    _set_term_setting(db, 15)
    for _ in range(3):
        _purchase(aid)                      # same customer, redelivered event
        assert _term(db, aid)[0] == 6, "a redelivery re-derived the term from live config"
    _set_term_setting(db, 6)


@requires_db
def test_a_renewal_through_the_REAL_webhook_handler_does_not_reterm(db, monkeypatch):
    """END-TO-END, through ``stripe_webhooks.handle_invoice_paid`` rather than the module
    helper. The unit path above proves the decision; this proves the WIRING — that the renewal
    door really does pass ``session_id=None`` and therefore really is classified as a renewal.
    Verify the read, not just the write."""
    _set_term_setting(db, 6)
    aid = _mk_account(db)
    sub = f"sub_{uuid.uuid4().hex[:16]}"
    prov.provision_from_checkout(account_id=aid, tier_row=checkout.get_tier("enterprise"),
                                 subscription_id=sub, session_id=f"cs_{uuid.uuid4().hex[:12]}",
                                 contract_id=1)
    assert _term(db, aid)[0] == 6

    _set_term_setting(db, 12)
    inv = {"id": f"evt_{uuid.uuid4().hex[:22]}", "type": "invoice.paid", "livemode": False,
           "created": int(time.time()),
           "data": {"object": {"id": f"in_{uuid.uuid4().hex[:16]}", "subscription": sub,
                               "customer": f"cus_{uuid.uuid4().hex[:14]}",
                               "amount_paid": 150000, "currency": "cad",
                               "metadata": {"account_id": aid, "plan_id": "enterprise"}}}}
    swh.handle_invoice_paid(inv)
    assert _term(db, aid)[0] == 6, (
        "the REAL invoice.paid handler re-termed an existing account from live configuration")
    _set_term_setting(db, 6)


@requires_db
def test_the_sales_led_door_is_untouched(db):
    """``accounts.create_account`` is the operator's door for a negotiated enterprise contract
    and never reaches this module. If this goes red, real six-month contracts have been
    silently converted."""
    slug = f"sl{uuid.uuid4().hex[:12]}"
    res = accounts_mod.create_account(name=slug, slug=slug, account_kind="tenant")
    assert _term(db, res["account_id"])[0] == int(ent_mod.DEFAULT_TERM_MONTHS) == 6


# ════════════════════════════════════════════════════════════════════════════════════
# PART 4 — an already-signed contract is untouched by a settings change
# ════════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_an_already_signed_contract_snapshot_is_byte_identical_after_a_settings_change(
        db, client, operator):
    """The other half of "historic must be maintained". ``publish_terms_version`` substitutes the
    configured numbers ONCE, at publish time, and ``signed_contracts.contract_snapshot`` is the
    frozen copy the customer actually saw. A later settings change must not rewrite one byte of
    it — this asserts the md5 of the stored snapshot, not merely that a number looks right."""
    import hashlib
    # SIGN ONE OURSELVES rather than skipping when the throwaway DB happens to have none. A
    # skipped immutability test is indistinguishable from a passing one in the summary line, and
    # this is the assertion the owner's "historic must be maintained" rests on.
    with db.cursor() as cur:
        cur.execute("SELECT id FROM public.contract_templates ORDER BY id LIMIT 1")
        tpl = cur.fetchone()
        if tpl is None:
            pytest.skip("no contract template seeded — migration 243 not applied")
        snapshot = ("Terms as the customer saw them. Data is retained for 30 days; backups are "
                    "overwritten within a further 35 days; requests are fulfilled within 5 "
                    "business days; the term is 6 months. " + uuid.uuid4().hex)
        cur.execute(
            "INSERT INTO public.signed_contracts (contract_template_id, signer_email, tier, "
            "contract_snapshot, contract_version, agreed_amount_cents, agreed_currency, "
            "agreed_interval) VALUES (%s, %s, 'enterprise', %s, 'billgate-v1', 150000, 'CAD', "
            "'month') RETURNING id",
            (tpl[0], f"billgate-{uuid.uuid4().hex[:8]}@example.invalid", snapshot),
        )
        row = (cur.fetchone()[0], snapshot)
    before = hashlib.md5(row[1].encode("utf-8")).hexdigest()
    r = _session_put(client, operator,
                     {"data_retention_days": 90, "backup_overwrite_days": 120,
                      "fulfilment_days": 9, "confirm_password": OP_PASSWORD})
    assert r.status_code == 200, r.text
    with db.cursor() as cur:
        cur.execute("SELECT contract_snapshot FROM public.signed_contracts WHERE id = %s",
                    (row[0],))
        after = hashlib.md5(cur.fetchone()[0].encode("utf-8")).hexdigest()
    assert after == before, "a settings change rewrote an already-signed contract snapshot"


@requires_db
def test_the_signed_contract_term_reader_is_read_only_and_fails_safe(db):
    """``signed_contract_term`` is EVIDENCE, never an authority to write with. An account with
    no contract answers None, and asking must not change anything."""
    aid = _mk_account(db)
    before = _term(db, aid)
    assert prov.signed_contract_term(aid) is None
    assert prov.signed_contract_term("") is None
    assert _term(db, aid) == before


# ════════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN GUARD — must stay LAST
# ════════════════════════════════════════════════════════════════════════════════════

def test_zzz_no_test_was_silently_skipped():
    """A DSN-gated suite that skips its DB half prints "PASS | N passed, M skipped" and looks
    green. If the database half did not run, this suite proved nothing."""
    assert _DB_RAN["value"], (
        "the DB half never ran — pass POSTGRES_DSN pointing at the throwaway control plane "
        "(see this file's header). A skipped DB half is NOT a pass.")
