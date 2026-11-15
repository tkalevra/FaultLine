"""RETENTION as operator-owned configuration, and its binding to the signed contract (mig 246).

THE PROBLEM THIS PINS. The signed Terms of Service (migration 243) makes specific, legally-binding
retention promises — "retained … for 30 days", "overwritten within a further 35 days", "within five
business days". They were HARDCODED PROSE, while the platform's only real retention knob
(``retention_grace_days``, DEFAULT 7) drives a reaper that touches lapsed self-serve INDIVIDUAL
trials and never a tenant. Contract and configuration could not agree, and the configuration was
not reachable from the operator console at all.

WHAT IS PINNED, on REAL ROWS in a throwaway control plane:
  * the three windows exist as platform settings, and a value set through the OPERATOR ROUTE is
    what the getter returns;
  * an unreadable / absent / below-floor value resolves to the DOCUMENTED DEFAULT and NEVER 0 —
    for a retention window "safe" is the LONGER value, because deleting early is unrecoverable;
  * out-of-range input is REFUSED (400) and the stored row is untouched;
  * the three are OPERATOR-GATED — no operator credential, no read and no write;
  * ``publish_terms_version`` writes the CURRENT configured numbers into the new version's text,
    records them, and REFUSES to publish a version still carrying a placeholder;
  * ⭐ THE IMPORTANT ONE — an ALREADY-SIGNED contract's snapshot is UNCHANGED by a later settings
    change. Proven with TWO signed rows, one on each side of a settings change.
  * ``retention.enforcement_summary`` reports exactly ONE enforced value and three manual ones, so
    the console cannot claim machinery this repo does not have;
  * the TENANT contract term (6 months, billed monthly, seat removals to term-end) is the same
    class of setting and rides the same publish-time binding — and it is TENANT-ONLY, because an
    individual is month-to-month by design.

⚠️ FALSE-GREEN GUARD. Every DB-backed test here would SKIP silently without a control plane, and a
"PASS | 0 passed, N skipped" run has lied in this repo before. ``_DB_RAN`` is set by the DB fixture
and ``test_no_test_was_silently_skipped`` (LAST) turns a fully-skipped run RED.

Run (never raw pytest — owner rule):
  PYTHONPATH="src:.venv/lib64/python3.14/site-packages" POSTGRES_DSN=... \\
  python3 tools/fltest.py --bug RETENTION --test tests/test_retention_settings.py
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

import src.mcp.http_server as h
import saas.admin as admin  # noqa: F401  (imported for router registration side effects)
from saas import contracts as saas_contracts
from saas import platform_settings as saas_platform
from saas import retention as saas_retention

OP_KEY = "retention-settings-op-super-secret-key"

# Set by the DB fixture. The final test asserts it — see the module docstring.
_DB_RAN = {"value": False}

# Control-plane singletons (the append-only ACTIVE contract chain, the platform settings row,
# the global operator realm) are shared by every suite in one database: the suites that
# write or depend on them run on one xdist worker (under --dist loadgroup, see tests/conftest.py).
pytestmark = pytest.mark.xdist_group("control_plane")


def _dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or "").strip()


requires_db = pytest.mark.skipif(not _dsn(), reason="POSTGRES_DSN not set — control plane needed")


@pytest.fixture(autouse=True)
def _own_operator_key(monkeypatch):
    """Bind THIS module's operator key for ONE test, then restore.

    saas/admin.py reads SAAS_OPERATOR_KEY LIVE per request, so a function-scoped monkeypatch is the
    right lifetime and collection order can never decide this file's result."""
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    # Needed to mint the operator SESSION the password-gated door requires.
    monkeypatch.setenv("SAAS_SESSION_SECRET",
                       "retention-settings-session-secret-0123456789abcdef")


@pytest.fixture(autouse=True)
def _clear_cache():
    """platform_settings caches process-wide — reset around every test so state never leaks."""
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    yield
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


@pytest.fixture
def client():
    return TestClient(h.app, follow_redirects=False)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


# ══════════════════════════════════════════════════════════════════════════════════
# THE PASSWORD-GATED DOOR (BILLGATE)
# ══════════════════════════════════════════════════════════════════════════════════
#
# ⚠️ THE OPERATOR KEY CAN NO LONGER MOVE THESE NUMBERS, AND THAT IS THE POINT.
# The four contractual figures + retention_grace_days are in
# ``platform_settings.PROTECTED_SETTINGS``: they are written verbatim into a signed, append-only
# legal record and into billing, so changing one now requires a signed-in operator SESSION plus a
# re-verified password (owner: "requiring password authorization to not accidentally fuck up the
# billing"). The static break-glass ``SAAS_OPERATOR_KEY`` has no password to re-prove and is
# refused with 403 — proved by
# ``tests/test_billing_settings_gate.py::test_the_break_glass_operator_key_cannot_change_a_protected_figure``.
#
# So every write BELOW that touches a protected field goes through ``_put_settings``, which mints
# a REAL operator login with a REAL argon2id hash and a REAL signed session cookie. Unprotected
# fields keep using the key door, which still works and is still tested.
OP_PASSWORD = "retention-settings-operator-password"
_OPERATOR = {}


def _operator_session():
    """A real operator login + signed session, created once per process against the test DB."""
    if _OPERATOR:
        return _OPERATOR
    import psycopg2
    from saas import dashboard_auth as _A
    username = f"retention-op-{uuid.uuid4().hex[:10]}"
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.dashboard_users (account_id, username, password_hash, role, "
                "status) VALUES (NULL, %s, %s, 'operator', 'active') RETURNING id::text",
                (username, _A.hash_password(OP_PASSWORD)),
            )
            op_id = cur.fetchone()[0]
    finally:
        conn.close()
    _OPERATOR.update({
        "id": op_id,
        "session": _A.issue_operator_session(dashboard_user_id=op_id, username=username),
        "csrf": _A.issue_operator_csrf(dashboard_user_id=op_id),
        "cookie": _A.OPERATOR_SESSION_COOKIE_NAME,
    })
    return _OPERATOR


def _put_settings(client, payload):
    """PUT /admin/platform-settings through the SESSION door, adding the password confirmation
    when the payload touches a protected figure. Unprotected payloads are sent unchanged."""
    op = _operator_session()
    body = dict(payload)
    if saas_platform.protected_changes(body):
        body["confirm_password"] = OP_PASSWORD
    client.cookies.set(op["cookie"], op["session"])
    return client.put("/admin/platform-settings", json=body,
                      headers={"x-csrf-token": op["csrf"]})


@pytest.fixture
def db(monkeypatch):
    """A real control-plane connection, restored to the shipped defaults after every test."""
    dsn = _dsn()
    if not dsn:
        pytest.skip("POSTGRES_DSN not set")
    import psycopg2
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    _DB_RAN["value"] = True
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
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


def _row(conn) -> tuple:
    with conn.cursor() as cur:
        cur.execute("SELECT data_retention_days, backup_overwrite_days, fulfilment_days, "
                    "retention_grace_days, contract_term_months "
                    "FROM public.platform_settings WHERE singleton = TRUE")
        return cur.fetchone()


def _set_raw(conn, **cols) -> None:
    """Write columns DIRECTLY, bypassing the API — for exercising the getters' own resolution."""
    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE public.platform_settings SET {sets} WHERE singleton = TRUE",
                    list(cols.values()))
    saas_platform._cache = None
    saas_platform._cache_at = 0.0


# ══════════════════════════════════════════════════════════════════════════════════
# 1. The settings are real, and the OPERATOR ROUTE is what moves them
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_shipped_defaults_match_the_contract_text(db):
    """The out-of-the-box row states exactly what migration 243's seeded ToS promises."""
    assert _row(db)[:3] == (30, 35, 5)
    assert saas_platform.data_retention_days() == 30
    assert saas_platform.backup_overwrite_days() == 35
    assert saas_platform.fulfilment_days() == 5


@requires_db
def test_value_set_through_the_operator_route_is_what_the_getter_returns(db, client):
    """VERIFY THE READ, NOT THE WRITE — drive the real PUT, then read the real row + the getter."""
    r = _put_settings(client, {
        "data_retention_days": 45, "backup_overwrite_days": 60, "fulfilment_days": 3})
    assert r.status_code == 200, r.text
    assert _row(db)[:3] == (45, 60, 3)
    saas_platform._cache_at = 0.0
    assert saas_platform.data_retention_days() == 45
    assert saas_platform.backup_overwrite_days() == 60
    assert saas_platform.fulfilment_days() == 3


@requires_db
def test_the_get_route_surfaces_the_three_windows(db, client):
    r = client.get("/admin/platform-settings", headers=_op())
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    for key in ("data_retention_days", "backup_overwrite_days", "fulfilment_days",
                "retention_grace_days", "contract_term_months"):
        assert key in s, f"{key} missing from the operator settings payload"


def test_the_retention_settings_are_operator_gated(client):
    """No operator credential → no read and no write. Rejected BEFORE any DB access."""
    assert client.get("/admin/platform-settings").status_code in (401, 403)
    assert client.put("/admin/platform-settings",
                      json={"data_retention_days": 45}).status_code in (401, 403)
    bad = {"Authorization": "Bearer not-the-operator-key"}
    assert client.get("/admin/platform-settings", headers=bad).status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════════════
# 2. FAIL-SAFE: never 0, always the LONGER contracted default
# ══════════════════════════════════════════════════════════════════════════════════

def test_an_unreadable_control_plane_resolves_to_the_contracted_default_not_zero(monkeypatch):
    """The single most important property here. A DB blip must never say "retained for 0 days"."""
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.state()["degraded"] is True, "expected the fail-closed state"
    assert saas_platform.data_retention_days() == 30
    assert saas_platform.backup_overwrite_days() == 35
    assert saas_platform.fulfilment_days() == 5
    for v in (saas_platform.data_retention_days(), saas_platform.backup_overwrite_days(),
              saas_platform.fulfilment_days()):
        assert v > 0, "a retention window resolved to 0 — that reads as 'delete immediately'"


def test_an_absent_key_resolves_to_the_contracted_default(monkeypatch):
    """A settings dict with the key MISSING (an older row shape) still resolves to 30/35/5."""
    monkeypatch.setattr(saas_platform, "state", lambda **kw: {"degraded": False})
    assert saas_platform.data_retention_days() == 30
    assert saas_platform.backup_overwrite_days() == 35
    assert saas_platform.fulfilment_days() == 5


@pytest.mark.parametrize("stored", [0, -1, -999])
def test_a_below_floor_value_resolves_to_the_default_and_never_to_the_floor(monkeypatch, stored):
    """The reason these getters do NOT clamp up to _MIN like their neighbours.

    Clamping a corrupt 0 to a floor of 1 would still promise a ONE-DAY retention window. For a
    retention window the safe direction is the LONGER value."""
    monkeypatch.setattr(saas_platform, "state",
                        lambda **kw: {"data_retention_days": stored,
                                      "backup_overwrite_days": stored,
                                      "fulfilment_days": stored})
    assert saas_platform.data_retention_days() == 30
    assert saas_platform.backup_overwrite_days() == 35
    assert saas_platform.fulfilment_days() == 5


def test_an_above_max_value_is_clamped_to_the_max(monkeypatch):
    monkeypatch.setattr(saas_platform, "state",
                        lambda **kw: {"data_retention_days": 99999,
                                      "backup_overwrite_days": 99999,
                                      "fulfilment_days": 99999})
    assert saas_platform.data_retention_days() == 3650
    assert saas_platform.backup_overwrite_days() == 3650
    assert saas_platform.fulfilment_days() == 30


def test_a_non_numeric_stored_value_resolves_to_the_default(monkeypatch):
    monkeypatch.setattr(saas_platform, "state", lambda **kw: {"data_retention_days": "thirty"})
    assert saas_platform.data_retention_days() == 30


@requires_db
def test_a_below_floor_row_written_around_the_api_still_reads_as_the_default(db):
    """Belt AND braces: the DB CHECK refuses it, and if it somehow lands the getter ignores it."""
    import psycopg2
    with pytest.raises(psycopg2.errors.CheckViolation):
        _set_raw(db, data_retention_days=0)
    assert _row(db)[0] == 30, "the CHECK should have refused 0 and left the row alone"


# ══════════════════════════════════════════════════════════════════════════════════
# 3. Out-of-range input through the operator route is REFUSED, not clamped
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
@pytest.mark.parametrize("payload", [
    {"data_retention_days": 0},
    {"data_retention_days": -5},
    {"data_retention_days": 4000},
    {"backup_overwrite_days": 0},
    {"fulfilment_days": 0},
    {"fulfilment_days": 90},
])
def test_out_of_range_is_a_400_and_the_stored_row_is_untouched(db, client, payload):
    """A silently-corrected legal number is worse than a rejected form — these go into a contract."""
    before = _row(db)
    r = _put_settings(client, payload)
    assert r.status_code == 400, r.text
    assert _row(db) == before, "a refused request still changed the stored row"


@requires_db
def test_a_rejected_field_does_not_partially_apply_its_siblings(db, client):
    """Validation runs before the UPDATE, so one bad field cannot leave a half-applied change."""
    before = _row(db)
    r = _put_settings(client, {
        "data_retention_days": 90, "fulfilment_days": 0})
    assert r.status_code == 400
    assert _row(db) == before


# ══════════════════════════════════════════════════════════════════════════════════
# 4. The individual-trial grace is a DIFFERENT thing and must not be conflated
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_grace_days_and_the_contract_windows_are_independent(db, client):
    """Moving one must not move the other; 0 stays LEGAL for the grace and ILLEGAL for the rest."""
    assert _put_settings(client, {"retention_grace_days": 0}).status_code == 200
    assert _row(db) == (30, 35, 5, 0, 6), "changing the trial grace moved a contractual window"
    assert saas_platform.retention_grace_days() == 0
    assert saas_platform.data_retention_days() == 30
    assert _put_settings(client, {"data_retention_days": 0}).status_code == 400


@requires_db
def test_enforcement_summary_names_exactly_one_enforced_value(db):
    """The console renders this. If a tenant reaper is ever built, this test SHOULD go red."""
    s = saas_retention.enforcement_summary()
    assert [e["key"] for e in s["enforced"]] == ["retention_grace_days"]
    assert sorted(m["key"] for m in s["manual"]) == [
        "backup_overwrite_days", "data_retention_days", "fulfilment_days"]
    assert s["purge_armed"] is False, "SAAS_RETENTION_PURGE_ENABLED must not be armed in tests"
    for m in s["manual"]:
        assert m["value"] > 0
    joined = " ".join(m["note"] for m in s["manual"]).lower()
    assert "no tenant reaper" in joined or "by hand" in joined


@requires_db
def test_the_get_route_carries_the_enforcement_summary(db, client):
    r = client.get("/admin/platform-settings", headers=_op())
    assert r.status_code == 200
    e = r.json().get("retention_enforcement")
    assert e and [x["key"] for x in e["enforced"]] == ["retention_grace_days"]
    assert len(e["manual"]) == 3


# ══════════════════════════════════════════════════════════════════════════════════
# 5. The contract binding — substitution at PUBLISH, never on a signed row
# ══════════════════════════════════════════════════════════════════════════════════

_TOKENISED = (
    "## 7. Termination\n"
    "1. Your data is retained for **{{RETENTION_DAYS}} days**.\n"
    "2. At the end of those {{RETENTION_DAYS}} days it is permanently deleted.\n"
    "3. Backups are overwritten within a further **{{BACKUP_OVERWRITE_DAYS}} days**.\n"
    "## 4. Export\nWe complete it within {{FULFILMENT_DAYS}} business days.\n"
)


@pytest.fixture
def clean_templates(db):
    """A per-test version-string prefix. There is DELIBERATELY no cleanup — there cannot be.

    ⚠️ MEASURED, and it corrects a claim in ``saas/contracts.py``'s own docstring: BOTH tables are
    append-only. Migration 243 puts ``trg_contract_templates_immutable`` (BEFORE UPDATE OR DELETE)
    and ``trg_contract_templates_no_truncate`` on ``contract_templates``, so a tidy-up fixture that
    DELETEs templates dies with *"public.contract_templates is append-only — DELETE is not
    permitted"*. ``retired_at`` is write-once too, so a retired version cannot be re-activated.

    So these tests do what the product does: publish a NEW uniquely-named version on top of the
    chain and let ``publish_terms_version`` retire the incumbent. That exercises the REAL table
    rather than a table the test first defanged. The throwaway control plane accumulates rows,
    which is what a throwaway is for."""
    import uuid
    return "ret-" + uuid.uuid4().hex[:10]


@requires_db
def test_publish_writes_the_currently_configured_numbers_into_the_text(db, clean_templates, client):
    _put_settings(client, {
        "data_retention_days": 45, "backup_overwrite_days": 60, "fulfilment_days": 7})
    out = saas_contracts.publish_terms_version(
        version=clean_templates + "-v1", title="Terms", body_markdown=_TOKENISED,
        created_by="test",
        # REQUIRED when superseding an incumbent (migration 256): revised terms published with
        # no statement of what changed are not sufficient notice, so the publish is refused.
        change_summary="test fixture: retention numbers re-bound at publish")
    assert out["ok"] is True, out
    assert out["retention_values"] == {"RETENTION_DAYS": 45, "BACKUP_OVERWRITE_DAYS": 60,
                                       "FULFILMENT_DAYS": 7, "CONTRACT_TERM_MONTHS": 6}
    assert out["placeholders_substituted"] == 4
    body = saas_contracts.get_current_terms()["body_markdown"]
    assert "retained for **45 days**" in body
    assert "those 45 days" in body
    assert "further **60 days**" in body
    assert "within 7 business days" in body
    assert "{{" not in body
    assert saas_contracts.get_template_retention_values(out["id"]) == {
        "RETENTION_DAYS": 45, "BACKUP_OVERWRITE_DAYS": 60, "FULFILMENT_DAYS": 7,
        "CONTRACT_TERM_MONTHS": 6, "placeholders_substituted": 4}


@requires_db
def test_a_body_with_no_placeholders_publishes_byte_for_byte(db, clean_templates):
    """Degrade-safely: migration 243's literal seeded text keeps working, unchanged."""
    literal = "Your data is retained for **30 days**. Backups within a further **35 days**."
    out = saas_contracts.publish_terms_version(
        version=clean_templates + "-literal", title="Terms", body_markdown=literal,
        change_summary="test fixture: literal body, no placeholders")
    assert out["ok"] is True, out
    assert out["placeholders_substituted"] == 0
    assert saas_contracts.get_current_terms()["body_markdown"] == literal


@requires_db
@pytest.mark.parametrize("bad", [
    "retained for {{RETENTION_DAY}} days",       # typo — not a known token
    "kept {{ RETENTION_DAYS }} days, purged {{PURGE_DAYS}}",
])
def test_publishing_an_unsubstituted_placeholder_is_refused(db, clean_templates, bad):
    """A legal document reading "{{RETENTION_DAY}} days" must be impossible to publish."""
    seed = saas_contracts.publish_terms_version(
        version=clean_templates + "-incumbent", title="Terms",
        body_markdown="Kept for 30 days.", change_summary="test fixture: the incumbent")
    assert seed["ok"] is True
    out = saas_contracts.publish_terms_version(version=clean_templates + "-bad", title="Terms",
                                               body_markdown=bad,
                                               change_summary="test fixture: a bad token")
    assert out["ok"] is False
    assert out["unsubstituted"], out
    # SAFE FAILURE: the incumbent is still the single active version — a refused publish must not
    # leave the platform with no terms (the self-inflicted-outage class this module already fixed).
    active = saas_contracts.get_current_terms()
    assert active is not None and active["version"] == clean_templates + "-incumbent"


@requires_db
def test_substitute_false_still_refuses_a_surviving_placeholder(db, clean_templates):
    """The escape hatch skips substitution, NOT the refusal — it cannot sneak a token through."""
    out = saas_contracts.publish_terms_version(
        version=clean_templates + "-nosub", title="Terms",
        body_markdown="Kept {{RETENTION_DAYS}} days.",
        substitute_retention=False, change_summary="test fixture: substitution skipped")
    assert out["ok"] is False and out["unsubstituted"] == ["RETENTION_DAYS"]


@requires_db
def test_a_template_carrying_raw_tokens_never_reaches_a_customer(db, clean_templates):
    """Read-side net: a row inserted OUTSIDE publish_terms_version (a migration seed) is
    substituted before it can be displayed — or, far worse, SIGNED into a permanent snapshot."""
    with db.cursor() as cur:
        # retired_at is set AT INSERT: idx_contract_templates_one_active is a partial UNIQUE index
        # permitting exactly ONE non-retired row, and the seeded ToS already holds that slot.
        cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown, "
                    "effective_at, retired_at, created_by) "
                    "VALUES (%s,'Terms',%s,NOW(),NOW(),'seed') RETURNING id",
                    (clean_templates + "-seed", _TOKENISED))
        tid = cur.fetchone()[0]
    body = saas_contracts.get_terms_by_id(tid)["body_markdown"]
    assert "{{" not in body, "a raw placeholder would have been shown to the customer"
    assert "retained for **30 days**" in body


# ══════════════════════════════════════════════════════════════════════════════════
# 6. ⭐ THE ONE THAT MATTERS — a signed contract is FROZEN
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_a_settings_change_never_alters_an_already_signed_contract(db, clean_templates, client):
    """TWO signed rows, one on each side of a settings change.

    Row A signs version v1, published when retention was 30 days. The operator then changes the
    setting to 90 and publishes v2. Row B signs v2 and sees 90. Row A's snapshot STILL SAYS 30 —
    which is the PIPEDA/ESIGN requirement: the agreement must be retrievable in the form it was
    presented. If this ever goes red, someone has started interpolating live values into a signed
    record and every historical contract has been silently rewritten."""
    _put_settings(client, {"data_retention_days": 30})
    v1 = saas_contracts.publish_terms_version(
        version=clean_templates + "-sign-v1", title="Terms", body_markdown=_TOKENISED,
        change_summary="test fixture: sign-v1")
    assert v1["ok"] is True, v1
    id_a = saas_contracts.record_acceptance(
        template_id=v1["id"], signer_email=clean_templates + "-a@example.com", signer_name="Signer A", tier="pro",
        agreed_amount_cents=75000)
    assert id_a

    # ── the operator changes the promise and publishes a new version ──
    assert _put_settings(client, {"data_retention_days": 90}).status_code == 200
    saas_platform._cache_at = 0.0
    v2 = saas_contracts.publish_terms_version(
        version=clean_templates + "-sign-v2", title="Terms", body_markdown=_TOKENISED,
        change_summary="test fixture: sign-v2")
    assert v2["ok"] is True, v2
    id_b = saas_contracts.record_acceptance(
        template_id=v2["id"], signer_email=clean_templates + "-b@example.com", signer_name="Signer B", tier="pro",
        agreed_amount_cents=75000)
    assert id_b

    snap_a = saas_contracts.get_signed_contract(id_a)["contract_snapshot"]
    snap_b = saas_contracts.get_signed_contract(id_b)["contract_snapshot"]
    assert "retained for **30 days**" in snap_a, "an already-signed contract was rewritten"
    assert "**90 days**" not in snap_a
    assert "retained for **90 days**" in snap_b
    assert "**30 days**" not in snap_b
    # And the v1 TEMPLATE is frozen too — its text was literalised at publish.
    with db.cursor() as cur:
        cur.execute("SELECT body_markdown FROM public.contract_templates WHERE id = %s",
                    (v1["id"],))
        assert "retained for **30 days**" in cur.fetchone()[0]


@requires_db
def test_the_signed_snapshot_carries_no_placeholder(db, clean_templates):
    """Whatever path a template took, a permanent legal record never freezes a raw token."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO public.contract_templates (version, title, body_markdown, "
                    "effective_at, retired_at, created_by) "
                    "VALUES (%s,'Terms',%s,NOW(),NOW(),'seed') RETURNING id",
                    (clean_templates + "-snapseed", _TOKENISED))
        tid = cur.fetchone()[0]
    cid = saas_contracts.record_acceptance(
        template_id=tid, signer_email=clean_templates + "-c@example.com", signer_name="Signer C", tier="pro")
    assert cid
    assert "{{" not in saas_contracts.get_signed_contract(cid)["contract_snapshot"]


# ══════════════════════════════════════════════════════════════════════════════════
# 7. Pure helpers (no DB) + the false-green guard
# ══════════════════════════════════════════════════════════════════════════════════

def test_substitution_leaves_an_unknown_token_alone_so_the_publish_gate_can_catch_it():
    out, _vals, n = saas_contracts.substitute_retention_placeholders(
        "{{RETENTION_DAYS}} and {{NOPE}}", values={"RETENTION_DAYS": 12})
    assert out == "12 and {{NOPE}}"
    assert n == 1
    assert saas_contracts.find_unsubstituted_placeholders(out) == ["NOPE"]


def test_find_unsubstituted_is_empty_on_clean_text():
    assert saas_contracts.find_unsubstituted_placeholders("plain 30 days") == []
    assert saas_contracts.find_unsubstituted_placeholders("") == []


# ══════════════════════════════════════════════════════════════════════════════════
# 8. The TENANT contract term — same discipline, and TENANT-ONLY
# ══════════════════════════════════════════════════════════════════════════════════

@requires_db
def test_the_contract_term_defaults_to_six_months(db):
    assert _row(db)[4] == 6
    assert saas_platform.contract_term_months() == 6


@requires_db
def test_the_contract_term_moves_through_the_operator_route(db, client):
    r = _put_settings(client, {"contract_term_months": 12})
    assert r.status_code == 200, r.text
    assert _row(db)[4] == 12
    saas_platform._cache_at = 0.0
    assert saas_platform.contract_term_months() == 12
    # and it moved NOTHING else — the retention windows are a different concern.
    assert _row(db)[:4] == (30, 35, 5, 7)


@requires_db
@pytest.mark.parametrize("bad", [0, -1, 61, 1000])
def test_an_out_of_range_contract_term_is_refused_and_changes_nothing(db, client, bad):
    """0 is the load-bearing case: a zero-month term is no commitment at all."""
    before = _row(db)
    r = _put_settings(client, {"contract_term_months": bad})
    assert r.status_code == 400, r.text
    assert _row(db) == before


def test_an_unreadable_contract_term_resolves_to_six_never_zero(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    saas_platform._cache = None
    saas_platform._cache_at = 0.0
    assert saas_platform.contract_term_months() == 6


@pytest.mark.parametrize("stored,expected", [(0, 6), (-3, 6), (999, 60), ("six", 6), (None, 6)])
def test_the_contract_term_never_resolves_below_its_floor(monkeypatch, stored, expected):
    monkeypatch.setattr(saas_platform, "state", lambda **kw: {"contract_term_months": stored})
    assert saas_platform.contract_term_months() == expected


@requires_db
def test_a_below_floor_contract_term_cannot_be_written_around_the_api(db):
    import psycopg2
    with pytest.raises(psycopg2.errors.CheckViolation):
        _set_raw(db, contract_term_months=0)
    assert _row(db)[4] == 6


@requires_db
def test_the_contract_term_is_bound_at_publish_and_frozen_in_a_signature(db, clean_templates,
                                                                        client):
    """Two signed rows again — the term a customer agreed to must survive a later settings change.

    ⚠️ This is the setting whose meaning was got BACKWARDS once already: an agent forced self-serve
    tenants to a 1-month term believing the 6-month default contradicted the contract, when it was
    the contract TEXT that was wrong. Binding the text to the setting is what stops that class of
    repair from being needed again."""
    body = "## 3. Term\nYou commit for {{CONTRACT_TERM_MONTHS}} months, billed monthly.\n"
    _put_settings(client, {"contract_term_months": 6})
    v1 = saas_contracts.publish_terms_version(
        version=clean_templates + "-term-v1", title="Terms", body_markdown=body,
        change_summary="test fixture: term-v1")
    assert v1["ok"] is True and v1["retention_values"]["CONTRACT_TERM_MONTHS"] == 6
    id_a = saas_contracts.record_acceptance(
        template_id=v1["id"], signer_email=clean_templates + "-t1@example.com",
        signer_name="Tenant One", tier="team")
    assert id_a

    assert _put_settings(client, {"contract_term_months": 24}).status_code == 200
    saas_platform._cache_at = 0.0
    v2 = saas_contracts.publish_terms_version(
        version=clean_templates + "-term-v2", title="Terms", body_markdown=body,
        change_summary="test fixture: term-v2")
    assert v2["ok"] is True and v2["retention_values"]["CONTRACT_TERM_MONTHS"] == 24
    id_b = saas_contracts.record_acceptance(
        template_id=v2["id"], signer_email=clean_templates + "-t2@example.com",
        signer_name="Tenant Two", tier="team")

    try:
        snap_a = saas_contracts.get_signed_contract(id_a)["contract_snapshot"]
        snap_b = saas_contracts.get_signed_contract(id_b)["contract_snapshot"]
        assert "commit for 6 months" in snap_a and "24 months" not in snap_a
        assert "commit for 24 months" in snap_b and "for 6 months" not in snap_b
    finally:
        # Put the ACTIVE agreement back at the shipped term. The chain is append-only, so the
        # only way to un-publish term-v2's frozen 24 is to supersede it; left active, it made
        # every later suite's tenant term 24 (#44: test_tenant_provisioning 6/6 red).
        _put_settings(client, {"contract_term_months": 6})
        saas_platform._cache_at = 0.0
        saas_contracts.publish_terms_version(
            version=clean_templates + "-term-restore", title="Terms", body_markdown=body,
            change_summary="test fixture: restore the shipped 6-month term after term-v2")


def test_the_contract_term_token_is_wired_to_the_tenant_term_setting():
    """A token nobody resolves is a publish that always refuses — pin the wiring, not the dict."""
    assert "CONTRACT_TERM_MONTHS" in saas_contracts.resolve_retention_placeholders()
    out, _v, n = saas_contracts.substitute_retention_placeholders(
        "term {{CONTRACT_TERM_MONTHS}} months", values={"CONTRACT_TERM_MONTHS": 6})
    assert out == "term 6 months" and n == 1


def test_there_is_no_individual_term_setting():
    """Individuals are month-to-month BY DESIGN. A configurable individual term would be the bug.

    If someone ever adds one, this goes red and they have to justify locking in a person who is
    still kicking the tires."""
    names = [n for n in dir(saas_platform)
             if "term" in n.lower() and not n.startswith("_") and callable(getattr(saas_platform, n))]
    assert names == ["contract_term_months"], f"unexpected term setting(s): {names}"


def test_zzz_no_test_was_silently_skipped():
    """LAST. Turns "PASS | 0 passed, N skipped" into a RED — the false green this repo has hit."""
    assert _DB_RAN["value"], (
        "No DB-backed test ran. Pass POSTGRES_DSN pointing at a THROWAWAY control plane with "
        "migrations >= 131 applied; a fully-skipped run proves nothing.")
