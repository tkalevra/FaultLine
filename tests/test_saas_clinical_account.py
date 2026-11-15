"""Clinical account type (migration 170) — real DB gates + governance + attribution + audit.

Runs against the LOCAL postgres (POSTGRES_DSN). SAAS_MODE on. Exercises the ACTUAL saas modules
end to end: net-new clinical provisioning, doctor sub-admin governance (admin-can't-hold-a-key),
per-doctor patient mint attribution for TWO doctors, and the append-only compliance audit.

Every test here needs a REAL control-plane database, so the whole module is gated on the
``requires_postgres`` fixture (tests/conftest.py) — no database means an honest SKIP instead of
eight ``psycopg2.OperationalError`` errors. The fixture connects rather than trusting the DSN
string, because a truthy POSTGRES_DSN is not evidence of a database: a sibling module used to
plant ``postgresql://unused/...`` into os.environ at import, process-wide, for everyone.

SAAS_MODE + the session secret are the suite-wide baseline set once in tests/conftest.py.
"""

import uuid

import pytest

from saas import accounts as _accounts
from saas import clinical as _clinical
from saas import dashboard_auth as _auth
from saas import db as _db
from saas import patients as _patients
from saas import seats as _seats

pytestmark = pytest.mark.usefixtures("requires_postgres")


def _exec(sql, params=()):
    with _db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
        conn.commit()
    return row


@pytest.fixture()
def clinical_account():
    """A fresh net-new clinical account; cleaned up (CASCADE) after the test."""
    slug = f"clinic-{uuid.uuid4().hex[:10]}"
    res = _clinical.provision_clinical_account("Test Clinic", slug)
    account_id = res["account_id"]
    yield res
    # NB: clinical_audit_log is APPEND-ONLY (cannot be deleted, by design) and carries no FK to
    # accounts, so its rows intentionally survive account teardown — they are the immutable trail.
    try:
        _exec("DELETE FROM public.accounts WHERE account_id = %s", (account_id,))
    except Exception:
        pass


def _make_doctor(account_id, email):
    did = _auth.create_dashboard_user(
        account_id=account_id, username=email,
        password_hash=_auth.hash_password("pw-" + uuid.uuid4().hex),
        role=_clinical.DOCTOR_ROLE)
    assert did is not None
    return did


# ── 1. net-new clinical account is kind='clinical', lane ON, allotment seeded, $4k base ──
def test_provision_clinical_account(clinical_account):
    account_id = clinical_account["account_id"]
    assert clinical_account["account_kind"] == "clinical"
    assert clinical_account["base_price"] == 4000.0
    assert clinical_account["included_patients"] == 250
    row = _exec("SELECT account_kind, patient_provisioning_enabled FROM public.accounts "
                "WHERE account_id = %s", (account_id,))
    assert row[0] == "clinical"
    assert row[1] is True
    ent = _exec("SELECT quantity, base_quantity FROM public.account_entitlements "
                "WHERE account_id = %s AND seat_type = 'patient'", (account_id,))
    assert ent[0] == 250 and ent[1] == 250
    plan = _clinical.clinical_plan(account_id)
    assert plan["base_price"] == 4000.0 and plan["licensed"] == 250


# ── 2. GOVERNANCE: an admin CANNOT mint/hold a provisioning key (fail-closed) ──
def test_admin_cannot_hold_provisioning_key(clinical_account):
    account_id = clinical_account["account_id"]
    # (a) the low-level guard: mint_key with the provision scope + NO owner → None (refused).
    assert _accounts.mint_key(account_id, scopes=["provision_patients"],
                              owner_dashboard_user_id=None) is None
    # (b) a doctor who was NEVER granted the ability cannot mint either.
    doc = _make_doctor(account_id, f"doc-{uuid.uuid4().hex[:8]}@x.test")
    denied = _clinical.mint_provisioning_key(account_id, doc)
    assert denied["ok"] is False and denied["status_code"] == 403


# ── 3. doctor mints an ATTRIBUTED key AFTER admin grant; attribution resolves ──
def test_grant_then_doctor_mints_key(clinical_account):
    account_id = clinical_account["account_id"]
    doc = _make_doctor(account_id, f"doc-{uuid.uuid4().hex[:8]}@x.test")
    g = _clinical.grant_key_ability(account_id, doc, actor_id="admin-1")
    assert g["ok"] and g["can_provision_patients"] is True
    minted = _clinical.mint_provisioning_key(account_id, doc)
    assert minted["ok"] is True
    full_key = minted["api_key"]
    assert "provision_patients" in minted["scopes"]
    attr = _accounts.resolve_key_attribution(full_key)
    assert attr is not None
    assert attr["owner_dashboard_user_id"] == doc
    assert "provision_patients" in attr["scopes"]


# ── 4. TWO doctors: each doctor's patients trace to THAT doctor (attribution) ──
def test_two_doctor_attribution(clinical_account):
    account_id = clinical_account["account_id"]
    doc1 = _make_doctor(account_id, f"d1-{uuid.uuid4().hex[:8]}@x.test")
    doc2 = _make_doctor(account_id, f"d2-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doc1)
    _clinical.grant_key_ability(account_id, doc2)
    k1 = _clinical.mint_provisioning_key(account_id, doc1)
    k2 = _clinical.mint_provisioning_key(account_id, doc2)
    a1 = _accounts.resolve_key_attribution(k1["api_key"])
    a2 = _accounts.resolve_key_attribution(k2["api_key"])

    # doc1 mints 2 patients, doc2 mints 1 — via the SAME mint path the /v1 gate drives.
    for ext in ("p-a", "p-b"):
        r = _patients.mint_patient(account_id, ext,
                                   provisioned_by_key_id=a1["key_id"],
                                   provisioned_by_sub_admin=a1["owner_dashboard_user_id"])
        assert r["ok"], r
    r = _patients.mint_patient(account_id, "p-c",
                               provisioned_by_key_id=a2["key_id"],
                               provisioned_by_sub_admin=a2["owner_dashboard_user_id"])
    assert r["ok"], r

    # DB-level attribution: the seat rows carry the minting doctor.
    c1 = _exec("SELECT COUNT(*) FROM public.account_users WHERE account_id=%s "
               "AND provisioned_by_sub_admin=%s AND seat_type='patient'", (account_id, doc1))[0]
    c2 = _exec("SELECT COUNT(*) FROM public.account_users WHERE account_id=%s "
               "AND provisioned_by_sub_admin=%s AND seat_type='patient'", (account_id, doc2))[0]
    assert c1 == 2 and c2 == 1

    summary = {s["sub_admin_id"]: s["attributed_patient_count"]
               for s in _clinical.attribution_summary(account_id)}
    assert summary[doc1] == 2 and summary[doc2] == 1


# ── 5. admin REVOKE kills the doctor's key (leaked key becomes useless) ──
def test_revoke_disables_key(clinical_account):
    account_id = clinical_account["account_id"]
    doc = _make_doctor(account_id, f"doc-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doc)
    minted = _clinical.mint_provisioning_key(account_id, doc)
    assert _accounts.resolve_key_attribution(minted["api_key"]) is not None
    rv = _clinical.revoke_key_ability(account_id, doc, actor_id="admin-1")
    assert rv["ok"] and rv["keys_revoked"] >= 1
    # Revoked key no longer resolves (fail-closed).
    assert _accounts.resolve_key_attribution(minted["api_key"]) is None


# ── 6. AUDIT trail is written on every event AND is append-only (DELETE/UPDATE blocked) ──
def test_audit_append_only_and_populated(clinical_account):
    account_id = clinical_account["account_id"]
    doc = _make_doctor(account_id, f"doc-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doc)
    minted = _clinical.mint_provisioning_key(account_id, doc)
    a = _accounts.resolve_key_attribution(minted["api_key"])
    _patients.mint_patient(account_id, "audited-p",
                           provisioned_by_key_id=a["key_id"],
                           provisioned_by_sub_admin=doc)
    rows = _clinical.query_audit(account_id, limit=100)
    actions = {r["action"] for r in rows}
    assert {"account_provision", "key_grant", "key_mint", "patient_mint"} <= actions, actions

    # Append-only: a raw DELETE / UPDATE must RAISE (the immutable trigger).
    import psycopg
    with pytest.raises(Exception):
        _exec("DELETE FROM public.clinical_audit_log WHERE account_id = %s", (account_id,))
    with pytest.raises(Exception):
        _exec("UPDATE public.clinical_audit_log SET detail='tamper' WHERE account_id = %s",
              (account_id,))


# ── 7. capacity SAFETY RAIL: a compromised key cannot exceed the patient cap → 402 ──
def test_patient_capacity_rail(clinical_account):
    account_id = clinical_account["account_id"]
    # Shrink capacity to 1 to prove the cap fires (register_seat's per-type gate).
    _exec("UPDATE public.account_entitlements SET quantity=1, base_quantity=1 "
          "WHERE account_id=%s AND seat_type='patient'", (account_id,))
    r1 = _patients.mint_patient(account_id, "cap-1")
    assert r1["ok"], r1
    r2 = _patients.mint_patient(account_id, "cap-2")
    assert r2["ok"] is False and r2["status_code"] == 402


# ══════════════════════════════════════════════════════════════════════════════════
# HTTP-level SEPARATION OF DUTIES (the transport firewall): an ADMIN session is 403'd on
# the doctor-only key-mint endpoint; a DOCTOR session mints. Exercised via the real router.
# ══════════════════════════════════════════════════════════════════════════════════

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from saas.tenant_console import router as tc_router
    app = FastAPI()
    app.include_router(tc_router)
    return TestClient(app)


def _session_cookiejar(client, *, dashboard_user_id, account_id):
    tok = _auth.issue_session(dashboard_user_id=dashboard_user_id, account_id=account_id,
                              username="x@x.test")
    client.cookies.set(_auth.SESSION_COOKIE_NAME, tok)
    return _auth.issue_csrf(dashboard_user_id=dashboard_user_id)


def test_http_admin_cannot_mint_key_doctor_can(clinical_account):
    account_id = clinical_account["account_id"]
    # The account owner (first-created dashboard login) is the ADMIN.
    admin_id = _auth.create_dashboard_user(
        account_id=account_id, username=f"admin-{uuid.uuid4().hex[:8]}@x.test",
        password_hash=_auth.hash_password("x"), role="admin")
    doctor_id = _make_doctor(account_id, f"doc-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doctor_id)

    client = _client()

    # ADMIN session → doctor-only POST /tenant/clinical/my-key → 403 (can't hold a key).
    csrf = _session_cookiejar(client, dashboard_user_id=admin_id, account_id=account_id)
    r_admin = client.post("/tenant/clinical/my-key", headers={"x-csrf-token": csrf})
    assert r_admin.status_code == 403, r_admin.text

    # ADMIN CAN govern: grant is admin-only and returns NO key.
    r_grant = client.post(f"/tenant/clinical/sub-admins/{doctor_id}/grant-key",
                          headers={"x-csrf-token": csrf})
    assert r_grant.status_code == 200, r_grant.text
    assert "api_key" not in r_grant.json()

    # DOCTOR session → mints their own key (returned once).
    csrf_d = _session_cookiejar(client, dashboard_user_id=doctor_id, account_id=account_id)
    r_doc = client.post("/tenant/clinical/my-key", headers={"x-csrf-token": csrf_d})
    assert r_doc.status_code == 200, r_doc.text
    assert r_doc.json().get("api_key", "").startswith("flk_")

    # DOCTOR session → cannot GOVERN (grant is admin-only) → 403.
    r_doc_grant = client.post(f"/tenant/clinical/sub-admins/{doctor_id}/grant-key",
                              headers={"x-csrf-token": csrf_d})
    assert r_doc_grant.status_code == 403, r_doc_grant.text


# ── 9. ROTATION IS AN INVARIANT, NOT A HOPE (single live provisioning key) ──────────────
#
# `mint_provisioning_key`'s docstring promises "Any existing active provisioning key of this
# doctor is revoked first (single live key = clean rotation)". Its revoke used to be wrapped in
# a bare `except Exception: pass` with `cur.rowcount` never read, and the mint ran regardless —
# so a transient control-plane blip produced TWO live keys, both carrying PROVISION_KEY_SCOPES,
# with only the newer one returned. The survivor stayed live and INVISIBLE, because
# `my_key_status` reports a single prefix.
#
# Nothing structural backstops this: the only UNIQUE indexes on public.account_api_keys are the
# PK (key_id) and (key_prefix). `idx_acct_keys_owner` is a PLAIN index. The code IS the
# enforcement, which is why these pins exist.

def _active_keys(account_id, doctor_id):
    with _db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key_prefix FROM public.account_api_keys "
                "WHERE account_id = %s AND owner_dashboard_user_id = %s "
                "  AND status = 'active' AND revoked_at IS NULL ORDER BY key_prefix",
                (account_id, doctor_id))
            return [r[0] for r in cur.fetchall()]


def test_rotation_leaves_exactly_one_live_key(clinical_account):
    """The happy path of the invariant: minting twice must not accumulate live keys."""
    account_id = clinical_account["account_id"]
    doc = _make_doctor(account_id, f"rot-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doc)

    first = _clinical.mint_provisioning_key(account_id, doc)
    assert first["ok"], first
    assert len(_active_keys(account_id, doc)) == 1

    second = _clinical.mint_provisioning_key(account_id, doc)
    assert second["ok"], second
    live = _active_keys(account_id, doc)
    assert len(live) == 1, f"rotation accumulated live keys: {live}"
    assert live[0] == second["key_prefix"], "the SURVIVING key must be the newly minted one"


def test_a_revoke_that_does_not_settle_REFUSES_to_mint(clinical_account, monkeypatch):
    """THE DISCRIMINATING PIN — RED against the bare `except Exception: pass`.

    If the rotation's revoke cannot be proven to have run, minting anyway is what creates the
    second live key. The correct failure is to refuse: no new key, prior key untouched, 500 to
    the caller — matching the three structural twins at clinical.py :373/:438/:551.
    """
    account_id = clinical_account["account_id"]
    doc = _make_doctor(account_id, f"rev-{uuid.uuid4().hex[:8]}@x.test")
    _clinical.grant_key_ability(account_id, doc)

    first = _clinical.mint_provisioning_key(account_id, doc)
    assert first["ok"], first
    before = _active_keys(account_id, doc)
    assert len(before) == 1

    # Fail ONLY the revoke. `_is_doctor_of` opens the first connection inside the call; the
    # revoke opens the next one. Counting is what keeps the doctor-check working so we exercise
    # the revoke path rather than an early 403.
    real_connection = _db.connection
    calls = {"n": 0}

    def flaky_connection(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:                     # the revoke
            raise RuntimeError("simulated control-plane blip during rotation")
        return real_connection(*a, **kw)

    monkeypatch.setattr(_clinical.db, "connection", flaky_connection)
    res = _clinical.mint_provisioning_key(account_id, doc)
    monkeypatch.undo()

    assert res["ok"] is False, f"minted despite an unsettled revoke: {res}"
    assert res["status_code"] == 500, res
    after = _active_keys(account_id, doc)
    assert after == before, (
        f"the prior key must survive untouched and NO new key may exist; before={before} after={after}")
