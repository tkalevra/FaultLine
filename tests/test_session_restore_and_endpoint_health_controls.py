"""SESSION RESTORE ACROSS RELOAD + the ENDPOINT-HEALTH controls (SESSHEALTH).

Two owner-reported defects, both verified live against a real SAAS_MODE server and a real
headless Chromium before these tests were written. The pins here are the ones that would have
CAUGHT each defect; each test names the measurement it stands in for.

──────────────────────────────────────────────────────────────────────────────────────────
DEFECT 1 — "Pressing F5 logs you out, even freshly logged in." (TENANT console)

ROOT CAUSE, measured: NOT the cookie and NOT the TTL. ``fl_dash_session`` is httpOnly,
``Max-Age=3600``, ``SameSite=strict``, and after F5 the browser still sent it and
``GET /tenant/me`` still answered 200. The console rendered the login screen because
``tenant.js::boot()`` issued **ZERO** ``/tenant/*`` calls on load — it had no restore path at
all, while the operator console (app.js:4318) and the individual console (user.js:195) both
have one. The client forgot; the server was never asked.

The second half is CSRF DELIVERY — the same family as the signup bug pinned in
tests/test_saas_individual_csrf_delivery.py. The token was only ever returned in the BODY of
``/tenant/login`` / ``/tenant/mfa/verify`` and kept in a plain JS variable, so a reload
destroyed it while the session stayed valid: every GET would work and every WRITE would 403.
``GET /tenant/csrf`` closes delivery for a session the caller has ALREADY proved.

⚠️ WHAT MUST NOT BE "FIXED" BY WEAKENING — pinned below: the token is still REQUIRED on unsafe
methods, an unauthenticated caller still gets NO token, and re-minting must NOT re-set the
session cookie (a reload must not buy a fresh TTL).

──────────────────────────────────────────────────────────────────────────────────────────
DEFECT 2 — "'Test & Calibrate' doesn't do shit."

TWO independent causes, both measured:

(a) IT WAS NOT INERT, IT WAS UNBOUNDED. Driven live against an unreachable endpoint — the exact
    state the button exists for — one click took **110 seconds** to answer (20s extraction
    probe + ~90s on calibration's first sample, whose per-call timeout is 90s and whose
    total-budget guard is only consulted BETWEEN samples). For those 110s the console sat on
    "measuring your endpoint…" with the button disabled. Now: the dead case short-circuits at
    ~24s with an actionable sentence, and a slow-but-healthy endpoint is still fully measured.

(b) A GOVERNED SEAT WAS OFFERED A CONTROL IT CANNOT USE. ``banner()`` computed ``governed`` and
    then consulted it ONLY in the ``not configured`` branch, so a managed seat with a failing
    org endpoint got ``cta_target="recalibrate"`` — a button on a tab that ``applyBrainGate``
    HIDES for it, calling an endpoint that ``_require_endpoint_control`` refuses with 403.
    Verified live: the governed seat's banner was byte-identical to the self-owned seat's.

⚠️ CLEAR MUST NOT BECOME HIDE. ``clear_probe_state`` deletes the RECORD; it writes no ``ok``
and sets no suppression flag. Pinned: the post-clear band is never green, and the very first
real probe re-asserts (verified live — one call to the still-dead endpoint brought the banner
straight back).

tests/ is gitignored (git add -f).
Run: python3 tools/fltest.py --bug SESSHEALTH \
        --test tests/test_session_restore_and_endpoint_health_controls.py
"""

import pytest

from saas import brain_health
from saas import dashboard_auth as auth
from saas import tenant_console as tc
from saas import user_console as uc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ADMIN_A = "11111111-1111-4111-8111-111111111111"
SEAT_A = "33333333-3333-4333-8333-333333333333"
SEAT_B = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(tc.router)
    return TestClient(app)


def _session():
    return auth.issue_session(dashboard_user_id=ADMIN_A, account_id=ACCT_A, username="alice")


# ══════════════════════════════════════════════════════════════════════════════════════
# DEFECT 1 — GET /tenant/csrf: close the DELIVERY hole without relaxing the CHECK
# ══════════════════════════════════════════════════════════════════════════════════════

def test_tenant_csrf_remints_for_a_live_session(client):
    """A reload-shaped call — cookie only, no token in hand — gets a USABLE token back.

    This is the whole F5 fix on the server side: the console can recover a token for a session
    it already holds instead of being forced back to the login screen.
    """
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session())
    r = client.get("/tenant/csrf")
    assert r.status_code == 200, r.text
    token = r.json()["csrf_token"]
    # The re-minted token must satisfy the REAL verifier, bound to the session's principal.
    assert auth.verify_csrf(token, dashboard_user_id=ADMIN_A) is True


def test_tenant_csrf_refuses_an_unauthenticated_caller(client):
    """No session → 401 and NO token. The token is only ever a re-issue for a proved session;
    if this ever 200s without a cookie it is a token oracle, not a convenience."""
    r = client.get("/tenant/csrf")
    assert r.status_code == 401
    assert "csrf_token" not in r.text


def test_tenant_csrf_does_not_extend_the_session(client):
    """⚠️ THE ANTI-WEAKENING PIN. Restoring a session must not PROLONG one.

    ``SESSION_TTL_SECONDS`` runs absolutely from LOGIN. If this route ever re-set
    ``fl_dash_session``, every reload would silently buy a fresh hour and the grace period
    would become unbounded for anyone who keeps a tab open. It must mint the CSRF token and
    touch nothing else.
    """
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session())
    r = client.get("/tenant/csrf")
    assert r.status_code == 200
    set_cookies = [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
    assert not any(auth.SESSION_COOKIE_NAME in c for c in set_cookies), (
        "GET /tenant/csrf re-set the session cookie — that silently extends the TTL on every "
        "page reload. Re-mint the token only.")


def test_unsafe_methods_still_require_csrf(client, monkeypatch):
    """The gate is UNCHANGED. Delivery was the hole; verification was never the problem — so a
    write with no token must still be refused after this fix."""
    monkeypatch.setattr(tc._seats, "is_active_seat", lambda a, u: True, raising=False)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session())
    r = client.post(f"/tenant/seats/{SEAT_A}/brain-health/clear")
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════════════
# DEFECT 2(b) — a governed seat must be pointed at its ADMIN, never at a control it cannot use
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state", [brain_health.STATE_FLAPPING, brain_health.STATE_FAIL])
def test_governed_seat_is_never_offered_recalibrate(state):
    """THE REGRESSION PIN for the measured defect.

    A managed seat's Brain tab is hidden and ``_require_endpoint_control`` would 403 it, so
    offering "Test & calibrate" names a control that cannot be reached. It must get the help
    CTA and an addressee instead.
    """
    b = brain_health.banner(
        configured=True, state=state, tenant_context="real", can_control_endpoint=False,
        base_url="http://198.51.100.7:8080", admin_contact="admin@corp.test")
    assert b is not None
    assert b["cta_target"] == brain_health.CTA_HELP, (
        "a governed seat was handed an endpoint-control CTA it will be refused")
    assert b["cta_target"] != brain_health.CTA_RECALIBRATE
    assert b["admin_contact"] == "admin@corp.test"
    assert "admin@corp.test" in b["action"]
    # It still TELLS the seat what is happening — hiding the fault would be worse than an
    # inert button, so the state is reported; only the ACTION changes hands.
    assert "198.51.100.7:8080" in b["title"]


@pytest.mark.parametrize("state", [brain_health.STATE_FLAPPING, brain_health.STATE_FAIL])
def test_self_owned_seat_keeps_the_working_control(state):
    """The other arm: a seat that OWNS its endpoint must still be given the button. The fix
    must not demote everybody to 'ask your admin'."""
    b = brain_health.banner(
        configured=True, state=state, tenant_context="house", can_control_endpoint=True,
        base_url="http://198.51.100.7:8080")
    assert b is not None
    assert b["cta_target"] == brain_health.CTA_RECALIBRATE
    # No addressee for a self-owned seat — there is no admin to name. The key is absent on
    # these two banners rather than present-and-None, and either shape means the same thing.
    assert b.get("admin_contact") is None


def test_governed_fault_action_never_invents_an_addressee():
    """Unresolved contact → name the DESTINATION, never a dead-end 'contact your admin' with
    nobody on the other end. Same rule ``setup_action`` already follows."""
    line = brain_health.governed_fault_action(brain_health.STATE_FAIL, "198.51.100.7:8080", None)
    assert "tenant console" in line.lower()
    assert "none" not in line.lower().split()


def test_healthy_configured_seat_still_shows_no_banner():
    """A banner that is always present is a banner nobody reads — unchanged by this work."""
    assert brain_health.banner(configured=True, state=brain_health.STATE_OK,
                               tenant_context="real", can_control_endpoint=False) is None


# ══════════════════════════════════════════════════════════════════════════════════════
# DEFECT 2(a) — the calibration bound. Skip a DEAD endpoint; still measure a SLOW one.
# ══════════════════════════════════════════════════════════════════════════════════════

def test_no_answer_classifier_skips_only_transport_failures():
    """⚠️ THE DISTINCTION THAT KEEPS THE FIX HONEST.

    'responded but produced no clean triples' means the endpoint IS answering — its latency is
    real and is exactly what may need re-picking, so it MUST still be calibrated. Only a
    genuine no-answer may skip. Getting this backwards would silently stop calibrating the
    endpoints most likely to need it.
    """
    no_answer = {"ok": False, "detail": "call failed: ConnectTimeout: timed out"}
    breaker = {"ok": False, "detail": "circuit breaker open for this endpoint (recent failures)"}
    answered_badly = {"ok": False, "detail": "brain responded but produced no clean triples"}
    healthy = {"ok": True, "detail": "brain returned clean structured triples"}

    assert uc._endpoint_gave_no_answer(no_answer) is True
    assert uc._endpoint_gave_no_answer(breaker) is True
    assert uc._endpoint_gave_no_answer(answered_badly) is False, (
        "an endpoint that ANSWERED was skipped — its latency is measurable and meaningful")
    assert uc._endpoint_gave_no_answer(healthy) is False


def test_unknown_failure_signature_falls_back_to_calibrating():
    """Fail-safe direction: a signature this list has never seen must NOT skip a real
    measurement. Unrecognised → calibrate (still bounded by the deadline)."""
    assert uc._endpoint_gave_no_answer(
        {"ok": False, "detail": "something nobody has seen before"}) is False


def test_calibration_deadline_returns_a_bounded_honest_result(monkeypatch):
    """A probe that outlives the deadline yields a RESULT, not a hang and not an exception —
    and it must not claim a profile it never measured."""
    import time as _t
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 0.3)

    def _slow(_account_id, _cfg):
        _t.sleep(5)
        return {"ok": True, "profile": "slow", "samples": 5}

    out = uc._calibrate_within_deadline({"id": "x", "account_id": ACCT_A}, {}, _slow)
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert out.get("profile") is None, "a timed-out run must not report a profile"
    assert out["samples"] == 0


def test_calibration_deadline_passes_a_fast_result_straight_through(monkeypatch):
    """The healthy path is untouched: an in-time probe's REAL measurement is returned verbatim,
    so the deadline can never quietly degrade a good calibration."""
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 30)
    real = {"ok": True, "profile": "hosted-fast", "samples": 5, "p50_ms": 402, "p95_ms": 402}
    out = uc._calibrate_within_deadline({"id": "x", "account_id": ACCT_A}, {},
                                        lambda a, c: dict(real))
    assert out == real


def test_calibration_deadline_zero_is_the_legacy_rollback_lever(monkeypatch):
    """``SAAS_CALIBRATION_DEADLINE_S=0`` must restore the original inline, unbounded call —
    the documented rollback."""
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 0)
    seen = {}

    def _probe(account_id, cfg):
        seen["called"] = True
        return {"ok": True, "profile": "local", "samples": 5}

    out = uc._calibrate_within_deadline({"id": "x", "account_id": ACCT_A}, {}, _probe)
    assert seen.get("called") is True
    assert out["profile"] == "local"


def test_deadline_default_admits_a_slow_but_healthy_endpoint():
    """⚠️ SIZED BY MEASUREMENT, AND THE FIRST VALUE WAS WRONG.

    A stub answering in 8s/call needs ~40s for five samples. The original 25s deadline reported
    `timed_out` and left the profile unchanged — i.e. it broke calibration for exactly the
    endpoints the 'slow'/'local' profiles exist to serve. If this floor is ever lowered again,
    re-measure against a slow endpoint first.
    """
    assert uc._CALIBRATION_DEADLINE_S >= 45


def test_individual_log_id_is_robust_to_both_shapes():
    """The two dict shapes the shared brain helpers receive: ``id`` (seat session) and
    ``individual_id`` (managed login). A log line must never be the thing that raises."""
    assert uc._individual_log_id({"id": "a", "account_id": ACCT_A}) == "a"
    assert uc._individual_log_id(
        {"individual_id": "b", "user_id": SEAT_A, "account_id": ACCT_A}) == "b"
    assert uc._individual_log_id({"account_id": ACCT_A}) == "?"


def test_calibration_timeout_survives_the_managed_shape(monkeypatch):
    """⚠️ THE PRODUCTION DEFECT, pinned. The tenant admin route hands the shared helper a
    ``get_managed_individual()`` dict (``individual_id``, no ``id``). The deadline's timeout log
    read ``individual["id"]``, raised KeyError, and the caller's ``except Exception`` then
    discarded the real ``timed_out`` verdict and reported "calibration could not run". The honest
    result must pass through for this shape, not raise."""
    import time as _t
    monkeypatch.setattr(uc, "_CALIBRATION_DEADLINE_S", 0.3)

    def _slow(_a, _c):
        _t.sleep(5)
        return {"ok": True, "profile": "slow", "samples": 5}

    managed = {"individual_id": "b", "user_id": SEAT_A, "account_id": ACCT_A}
    out = uc._calibrate_within_deadline(managed, {}, _slow)
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert out.get("profile") is None


def test_run_test_and_calibrate_survives_the_managed_shape(monkeypatch):
    """The full shared helper, fed the managed shape: a REAL successful calibration must be
    returned, not swallowed by a KeyError in a log line and replaced with "could not run"."""
    from saas import llm_admin as la
    managed = {"individual_id": "b", "user_id": SEAT_A, "account_id": ACCT_A}
    monkeypatch.setattr(uc, "_brain_resolve",
                        lambda ind: {"brain_scope": "seat", "account_id": ACCT_A,
                                     "base_url": "http://198.51.100.7:8080", "model": "m"})
    monkeypatch.setattr(la, "_probe_extraction",
                        lambda a, c: {"ok": True, "model": "m", "triples_found": 1})
    monkeypatch.setattr(uc, "_calibrate_within_deadline",
                        lambda ind, cfg, probe: {"ok": True, "profile": "local", "samples": 5,
                                                 "p50_ms": 300, "p95_ms": 500})
    monkeypatch.setattr(uc, "_brain_mark_tested", lambda ind, scope="seat": None)
    monkeypatch.setattr(uc, "_brain_save_calibration", lambda ind, scope="seat", **kw: True)
    monkeypatch.setattr(brain_health, "probe_event", lambda *a, **k: None)

    out = uc._run_test_and_calibrate(managed)
    assert out["ok"] is True
    assert out["calibration"]["ok"] is True
    assert out["calibration"]["profile"] == "local"


def test_run_test_and_calibrate_persists_to_the_resolved_scope(monkeypatch):
    """A GOVERNED seat resolves the ACCOUNT brain, so the measurement must persist to the ACCOUNT
    row — not the seat's, which the resolver never reads for a governed seat. Persisting the
    wrong way leaves the org profile uncalibrated after a successful admin test."""
    from saas import llm_admin as la
    managed = {"individual_id": "b", "user_id": SEAT_A, "account_id": ACCT_A}
    seen = {}
    monkeypatch.setattr(uc, "_brain_resolve",
                        lambda ind: {"brain_scope": "account", "account_id": ACCT_A,
                                     "base_url": "http://198.51.100.7:8080", "model": "m"})
    monkeypatch.setattr(la, "_probe_extraction",
                        lambda a, c: {"ok": True, "model": "m", "triples_found": 1})
    monkeypatch.setattr(uc, "_calibrate_within_deadline",
                        lambda ind, cfg, probe: {"ok": True, "profile": "local", "samples": 5,
                                                 "p50_ms": 300, "p95_ms": 500})
    monkeypatch.setattr(uc, "_brain_mark_tested",
                        lambda ind, scope="seat": seen.setdefault("mark_scope", scope))
    monkeypatch.setattr(uc, "_brain_save_calibration",
                        lambda ind, scope="seat", **kw: seen.setdefault("save_scope", scope))
    monkeypatch.setattr(brain_health, "probe_event", lambda *a, **k: None)

    out = uc._run_test_and_calibrate(managed)
    assert out["calibration"]["ok"] is True
    assert seen["mark_scope"] == "account"
    assert seen["save_scope"] == "account"


# ══════════════════════════════════════════════════════════════════════════════════════
# CLEAR must forget, never hide
# ══════════════════════════════════════════════════════════════════════════════════════

def test_clear_is_a_noop_when_saas_is_off(monkeypatch):
    """SAAS off ⇒ the whole lane is inert, byte-for-byte."""
    monkeypatch.setenv("SAAS_MODE", "0")
    out = brain_health.clear_probe_state(SEAT_A)
    assert out["ok"] is False and out["cleared"] == 0


def test_clear_never_writes_an_ok_outcome():
    """⚠️ THE 'CLEAR IS NOT HIDE' PIN, enforced at the source.

    The honesty of this control rests on it having NO way to assert health: it only DELETEs.
    A future edit that made it UPSERT an 'ok' row — or set any suppression flag — would turn
    'this alarm is stale' into 'stop telling me', which is the one thing it must never mean.
    """
    import inspect
    src = inspect.getsource(brain_health.clear_probe_state)
    assert "DELETE FROM brain_probe_state" in src
    assert "INSERT" not in src.upper(), "clear_probe_state must never write a state row"
    assert "UPDATE" not in src.upper(), "clear_probe_state must never write a state row"
    # It must also COMMIT: saas.db.connection() only CLOSES (== rollback), and the first cut of
    # this function omitted the commit — it reported {"cleared": true, "rows": 2} while both
    # rows survived and the banner never went away.
    assert "conn.commit()" in src, "the DELETE must be committed or the clear silently no-ops"


def test_unknown_outranks_nothing_so_a_cleared_seat_is_never_green():
    """After a clear the seat has NO outcomes, which must read as 'no data' (unknown), never as
    'healthy' (ok). ``worst_state`` ranks unknown at the bottom, so an empty state map cannot
    produce a green assertion."""
    assert brain_health.worst_state({}) == brain_health.STATE_UNKNOWN
    assert brain_health.worst_state({}) != brain_health.STATE_OK


# ══════════════════════════════════════════════════════════════════════════════════════
# Admin-side controls: BOLA + the governance split
# ══════════════════════════════════════════════════════════════════════════════════════

def test_admin_cannot_act_on_a_foreign_seat(client, monkeypatch):
    """BOLA, fail-closed and BEFORE any read or write: a seat outside the SESSION account 403s.
    The gate is evaluated against the session's account_id, never client input."""
    monkeypatch.setattr(tc._seats, "is_active_seat",
                        lambda account_id, user_id: account_id == ACCT_A and user_id == SEAT_A,
                        raising=False)
    client.cookies.set(auth.SESSION_COOKIE_NAME, _session())
    csrf = auth.issue_csrf(dashboard_user_id=ADMIN_A)
    for path in ("recalibrate", "clear"):
        r = client.post(f"/tenant/seats/{SEAT_B}/brain-health/{path}",
                        headers={"x-csrf-token": csrf})
        assert r.status_code == 403, f"{path} allowed a cross-account seat: {r.text}"


def test_seat_is_governed_matches_the_enforcement_gate():
    """The banner's routing and the endpoint's 403 must agree, or a seat gets pointed at a
    control that then refuses it — the original defect in miniature. Only 'real' WITHOUT the
    grant is governed; the fail direction is towards self-owned."""
    assert brain_health.seat_is_governed("real", False) is True
    assert brain_health.seat_is_governed("real", True) is False
    assert brain_health.seat_is_governed("house", False) is False
    assert brain_health.seat_is_governed("none", False) is False
    assert brain_health.seat_is_governed(None, None) is False
