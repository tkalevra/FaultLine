"""MFA-ENROLMENT PROMPT + bounded server-side grace (migration 230) — unit + seam tests.

Proves the thing the feature actually claims: a console login with no second factor is PROMPTED,
the "remind me later" allowance is counted in the CONTROL PLANE (not the browser), it is spent at
most once per SIGN-IN, it stops being dismissible at the operator's threshold, and no client-side
lie moves any of that. Follows tests/test_saas_tenant_auth.py conventions — the live-DB paths are
faked in memory, so this file is green without a control plane.

The fake store below is NOT a mock that returns canned answers: it re-implements the two guards
the real UPDATE carries (the ceiling, and `deferred_at < last_login_at`) so the SEMANTICS are
under test. The literal SQL is asserted separately, so a refactor that quietly drops a guard
clause fails here rather than in production.

Run: python3 -m pytest tests/test_saas_mfa_prompt.py -q   (tests/ is gitignored → git add -f)
"""

import pytest

from saas import dashboard_auth as auth
from saas import mfa as _mfa
from saas import mfa_prompt as MP
from saas import platform_settings as _ps
from saas import tenant_console as tc
from saas import user_console as uc


ACCT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
INDIV_A = "33333333-3333-4333-8333-333333333333"


# ════════════════════════════════════════════════════════════════════════════════
# In-memory stand-in for public.dashboard_users / public.individual_users
# ════════════════════════════════════════════════════════════════════════════════

class _Store:
    """One login row per principal id, plus a clock, plus a tape of every statement issued."""

    def __init__(self):
        self.rows = {}          # principal_id -> {deferrals, deferred_at, last_login_at}
        self.clock = 100
        self.sql = []           # every statement text, in order
        self.tables = []        # table name touched by each statement
        self.fail = False       # make every statement raise (unreadable control plane)

    def add(self, pid, *, deferrals=0, deferred_at=None, last_login_at=100):
        self.rows[pid] = {"deferrals": deferrals, "deferred_at": deferred_at,
                          "last_login_at": last_login_at}
        return self

    def sign_in(self, pid):
        """What touch_last_login / _touch_individual_login do at every session issue."""
        self.clock += 10
        self.rows[pid]["last_login_at"] = self.clock
        return self

    def deferrals(self, pid):
        return self.rows[pid]["deferrals"]


class _Cur:
    def __init__(self, store):
        self.s = store
        self.rowcount = 0
        self._row = None

    def execute(self, sql, params=None):
        if self.s.fail:
            raise RuntimeError("control plane unreadable")
        self.s.sql.append(sql)
        self.s.tables.append("dashboard_users" if "dashboard_users" in sql else
                             ("individual_users" if "individual_users" in sql else "?"))
        params = params or ()
        pid = params[0]
        row = self.s.rows.get(pid)

        if sql.lstrip().upper().startswith("SELECT"):
            if row is None:
                self._row = None
                return
            deferred_this_login = (row["deferred_at"] is not None
                                   and row["last_login_at"] is not None
                                   and row["deferred_at"] >= row["last_login_at"])
            self._row = (row["deferrals"], deferred_this_login)
            return

        if "mfa_prompt_deferrals = 0" in sql:              # clear() after enrolment
            self.rowcount = 0
            if row and (row["deferrals"] != 0 or row["deferred_at"] is not None):
                row["deferrals"] = 0
                row["deferred_at"] = None
                self.rowcount = 1
            return

        if "mfa_prompt_deferrals + 1" in sql:              # defer()
            grace = params[1]
            self.rowcount = 0
            if row is None:
                return
            under_ceiling = row["deferrals"] < grace
            fresh_sign_in = (row["deferred_at"] is None
                             or row["last_login_at"] is None
                             or row["deferred_at"] < row["last_login_at"])
            if under_ceiling and fresh_sign_in:
                self.s.clock += 1
                row["deferrals"] += 1
                row["deferred_at"] = self.s.clock
                self.rowcount = 1
            return

        raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, store):
        self.s = store

    def cursor(self):
        return _Cur(self.s)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(MP.db, "connection", lambda *a, **k: _Conn(s))
    return s


@pytest.fixture
def grace10(monkeypatch):
    """Operator threshold = 10 (the shipped default), read through the REAL clamped accessor."""
    monkeypatch.setattr(_ps, "state", lambda **kw: {"mfa_prompt_grace_logins": 10})
    return 10


def _enrolled(monkeypatch, value):
    """value: True (has a factor) / False (has none) / 'error' (cannot determine)."""
    if value == "error":
        def boom(realm, pid):
            raise _mfa.MfaError("unresolvable")
        monkeypatch.setattr(_mfa, "require_mfa_decision", boom)
    else:
        monkeypatch.setattr(_mfa, "require_mfa_decision", lambda realm, pid: value)


# ════════════════════════════════════════════════════════════════════════════════
# The threshold is operator-tunable, and clamps sanely
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stored,expected", [
    (None, 10),      # NULL/absent → the shipped default, never 0
    (10, 10),
    (3, 3),          # an operator who wants a tighter window
    (0, 0),          # VALID: non-dismissible from the very first sign-in
    (-5, 0),         # clamped up
    (99999, 100),    # clamped down — "for up to N logins" must stay meaningful
    ("nonsense", 10),  # unparseable → the default, not a crash
])
def test_threshold_is_operator_tunable_and_clamped(monkeypatch, stored, expected):
    monkeypatch.setattr(_ps, "state", lambda **kw: {"mfa_prompt_grace_logins": stored})
    assert _ps.mfa_prompt_grace_logins() == expected
    assert MP.grace_logins() == expected


def test_threshold_falls_back_to_default_when_settings_unreadable(monkeypatch):
    # A DB blip must NOT resolve to 0 — 0 means "non-dismissible right now", which would put a
    # hard interstitial in front of every console user at once for an advisory prompt.
    def boom(**kw):
        raise RuntimeError("no control plane")
    monkeypatch.setattr(_ps, "state", boom)
    assert _ps.mfa_prompt_grace_logins() == 10


# ════════════════════════════════════════════════════════════════════════════════
# state(): who gets prompted
# ════════════════════════════════════════════════════════════════════════════════

def test_login_without_mfa_is_prompted(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is True
    assert st["dismissible"] is True
    assert st["deferrals_used"] == 0
    assert st["deferrals_remaining"] == 10
    assert st["grace_logins"] == 10
    assert st["degraded"] is False


def test_login_with_mfa_is_not_prompted(store, grace10, monkeypatch):
    _enrolled(monkeypatch, True)
    store.add(USER_A)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is False
    assert st["degraded"] is False


def test_completing_enrolment_clears_the_counter(store, grace10, monkeypatch):
    # Someone who deferred 7 times and then enrolled must not carry that history: remove the
    # factor later and they get a FULL window again, not an exhausted one.
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=7, deferred_at=105)
    assert MP.state("dashboard", USER_A)["deferrals_used"] == 7

    _enrolled(monkeypatch, True)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is False
    assert store.deferrals(USER_A) == 0
    assert store.rows[USER_A]["deferred_at"] is None

    _enrolled(monkeypatch, False)
    back = MP.state("dashboard", USER_A)
    assert back["required"] is True and back["deferrals_remaining"] == 10


def test_clear_is_a_noop_on_an_already_clean_row(store, grace10, monkeypatch):
    """The lazy clear runs on every /tenant/me for the enrolled majority, so it must carry a
    WHERE guard that makes it touch no rows — otherwise every console load writes a login row."""
    _enrolled(monkeypatch, True)
    store.add(USER_A, deferrals=0, deferred_at=None)
    assert MP.clear("dashboard", USER_A) is False        # rowcount 0 → nothing was written
    upd = [s for s in store.sql if "mfa_prompt_deferrals = 0" in s]
    assert len(upd) == 1
    assert "mfa_prompt_deferrals <> 0" in upd[0]
    assert "mfa_prompt_deferred_at IS NOT NULL" in upd[0]


# ════════════════════════════════════════════════════════════════════════════════
# The counter: server-side, persisted, once per SIGN-IN
# ════════════════════════════════════════════════════════════════════════════════

def test_defer_increments_and_persists(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)

    out = MP.defer("dashboard", USER_A)
    assert out["ok"] is True and out["counted"] is True
    assert store.deferrals(USER_A) == 1                     # persisted in the control plane
    assert out["state"]["deferrals_used"] == 1
    assert out["state"]["deferrals_remaining"] == 9
    assert out["state"]["deferred_this_login"] is True

    # A fresh read (a new request, nothing carried in the process) sees the same number.
    assert MP.state("dashboard", USER_A)["deferrals_used"] == 1


def test_second_defer_in_the_same_sign_in_does_not_double_count(store, grace10, monkeypatch):
    # Mashing "remind me later", or an F5 (which restores a session WITHOUT a new login), must
    # not burn extra grace. The owner asked for N LOGINS, not N clicks.
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    MP.defer("dashboard", USER_A)
    for _ in range(5):
        out = MP.defer("dashboard", USER_A)
        assert out["ok"] is True and out["counted"] is False
    assert store.deferrals(USER_A) == 1


def test_one_deferral_per_sign_in_over_ten_sign_ins(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    for n in range(1, 11):
        MP.defer("dashboard", USER_A)                       # this sign-in's dismissal
        MP.defer("dashboard", USER_A)                       # a double-click — must not count
        assert store.deferrals(USER_A) == n
        store.sign_in(USER_A)                               # they come back tomorrow
    assert MP.state("dashboard", USER_A)["dismissible"] is False


def test_the_update_carries_both_guards(store, grace10, monkeypatch):
    """The ceiling is re-asserted INSIDE the UPDATE, so two concurrent defers cannot both slip
    an increment past it, and the per-sign-in guard is in the same statement."""
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    MP.defer("dashboard", USER_A)
    upd = [s for s in store.sql if "mfa_prompt_deferrals + 1" in s]
    assert len(upd) == 1
    assert "mfa_prompt_deferrals < %s" in upd[0]
    assert "mfa_prompt_deferred_at < last_login_at" in upd[0]


# ════════════════════════════════════════════════════════════════════════════════
# The threshold: non-dismissible, and the server refuses
# ════════════════════════════════════════════════════════════════════════════════

def test_at_the_threshold_the_prompt_is_no_longer_dismissible(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=10, deferred_at=None)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is True
    assert st["dismissible"] is False
    assert st["deferrals_remaining"] == 0


def test_defer_past_the_threshold_is_refused_and_writes_nothing(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=10, deferred_at=None)
    out = MP.defer("dashboard", USER_A)
    assert out["ok"] is False and out["status_code"] == 409
    assert store.deferrals(USER_A) == 10                    # the ceiling held
    assert not any("mfa_prompt_deferrals + 1" in s for s in store.sql)


def test_a_zero_grace_setting_is_non_dismissible_immediately(store, monkeypatch):
    monkeypatch.setattr(_ps, "state", lambda **kw: {"mfa_prompt_grace_logins": 0})
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is True and st["dismissible"] is False
    assert MP.defer("dashboard", USER_A)["status_code"] == 409


def test_lowering_the_threshold_retroactively_hardens_existing_users(store, monkeypatch):
    """An operator tightening the window must bite on people who already coasted past the new
    number — the comparison is `used < grace`, evaluated fresh, not a stored decision."""
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=5)
    monkeypatch.setattr(_ps, "state", lambda **kw: {"mfa_prompt_grace_logins": 10})
    assert MP.state("dashboard", USER_A)["dismissible"] is True
    monkeypatch.setattr(_ps, "state", lambda **kw: {"mfa_prompt_grace_logins": 3})
    assert MP.state("dashboard", USER_A)["dismissible"] is False


# ════════════════════════════════════════════════════════════════════════════════
# Degraded control plane: fail QUIET, and consume nothing
# ════════════════════════════════════════════════════════════════════════════════

def test_unresolvable_mfa_state_shows_no_prompt_and_spends_no_grace(store, grace10, monkeypatch):
    _enrolled(monkeypatch, "error")
    store.add(USER_A, deferrals=3)
    st = MP.state("dashboard", USER_A)
    assert st["required"] is False and st["degraded"] is True
    out = MP.defer("dashboard", USER_A)
    assert out["ok"] is False and out["status_code"] == 503
    assert store.deferrals(USER_A) == 3     # an outage must not walk anyone toward the wall


def test_unreadable_counter_shows_no_prompt(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    store.fail = True
    st = MP.state("dashboard", USER_A)
    assert st["required"] is False and st["degraded"] is True


def test_missing_login_row_is_degraded_not_a_crash(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)          # no store.add() → no row
    st = MP.state("dashboard", USER_A)
    assert st["required"] is False and st["degraded"] is True


def test_unknown_realm_never_raises_out_of_state(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    assert MP.state("operator", USER_A)["degraded"] is True


# ════════════════════════════════════════════════════════════════════════════════
# Realm separation — the two consoles share the module, not the table
# ════════════════════════════════════════════════════════════════════════════════

def test_each_realm_reads_its_own_table(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A).add(INDIV_A)
    MP.state("dashboard", USER_A)
    assert store.tables[-1] == "dashboard_users"
    MP.state("individual", INDIV_A)
    assert store.tables[-1] == "individual_users"


def test_individual_realm_has_the_same_bounded_grace(store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(INDIV_A, deferrals=9)
    assert MP.defer("individual", INDIV_A)["ok"] is True
    assert store.deferrals(INDIV_A) == 10
    store.sign_in(INDIV_A)
    assert MP.state("individual", INDIV_A)["dismissible"] is False
    assert MP.defer("individual", INDIV_A)["status_code"] == 409


# ════════════════════════════════════════════════════════════════════════════════
# ENDPOINT seam — the console gets a decision it cannot argue with
# ════════════════════════════════════════════════════════════════════════════════

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


def _session(client):
    tok = auth.issue_session(dashboard_user_id=USER_A, account_id=ACCT_A, username="alice")
    client.cookies.set(auth.SESSION_COOKIE_NAME, tok)
    return {"x-csrf-token": auth.issue_csrf(dashboard_user_id=USER_A)}


def test_routes_are_registered():
    assert "/tenant/mfa-prompt/defer" in {r.path for r in tc.router.routes}
    assert "/me/mfa-prompt/defer" in {r.path for r in uc.router.routes}


def test_tenant_me_carries_the_prompt_for_an_unenrolled_login(client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    _session(client)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: False)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: False)

    body = client.get("/tenant/me").json()
    assert body["mfa_prompt"]["required"] is True
    assert body["mfa_prompt"]["dismissible"] is True
    assert body["mfa_prompt"]["grace_logins"] == 10


def test_tenant_me_omits_the_prompt_for_an_enrolled_login(client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, True)
    store.add(USER_A)
    _session(client)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: False)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: False)

    assert client.get("/tenant/me").json()["mfa_prompt"]["required"] is False


def test_defer_endpoint_requires_a_session(client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    assert client.post("/tenant/mfa-prompt/defer").status_code == 401


def test_defer_endpoint_requires_csrf(client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A)
    _session(client)                     # cookie set, header deliberately omitted
    assert client.post("/tenant/mfa-prompt/defer").status_code == 403
    assert store.deferrals(USER_A) == 0


def test_defer_endpoint_spends_only_the_session_principals_grace(client, store, grace10,
                                                                 monkeypatch):
    # BOLA: the id comes from the signed session, and there is no id in the request to tamper
    # with. A body naming somebody else changes nothing.
    _enrolled(monkeypatch, False)
    store.add(USER_A).add(USER_B, deferrals=0)
    h = _session(client)
    r = client.post("/tenant/mfa-prompt/defer", headers=h,
                    json={"dashboard_user_id": USER_B, "principal_id": USER_B})
    assert r.status_code == 200
    assert store.deferrals(USER_A) == 1
    assert store.deferrals(USER_B) == 0


def test_a_client_that_forges_dismissal_state_cannot_bypass_the_threshold(client, store, grace10,
                                                                         monkeypatch):
    """The whole point of putting the counter in Postgres. A patched console can claim anything
    it likes — every read and every write recomputes from the control plane."""
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=10)
    h = _session(client)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: False)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: False)

    forged = {"mfa_prompt": {"required": False, "dismissible": True, "deferrals_used": 0,
                             "grace_logins": 999}}
    r = client.post("/tenant/mfa-prompt/defer", headers=h, json=forged)
    assert r.status_code == 409
    assert store.deferrals(USER_A) == 10

    # And the very next read still tells the truth, whatever the client sent a moment ago.
    st = client.get("/tenant/me").json()["mfa_prompt"]
    assert st["required"] is True and st["dismissible"] is False and st["grace_logins"] == 10


def test_the_threshold_is_a_refusal_not_a_lockout(client, store, grace10, monkeypatch):
    """The session stays fully valid past the threshold — including on the enrolment surface the
    prompt points at. Locking the console would strand the user outside the only screen that can
    satisfy the requirement (the billing-deadlock defect, with a counter instead of an invoice)."""
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=10)
    h = _session(client)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: False)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: False)

    assert client.post("/tenant/mfa-prompt/defer", headers=h).status_code == 409
    # Same session, same moment: ordinary console reads still work.
    assert client.get("/tenant/me").status_code == 200
    # ...and so does the session dependency the enrolment endpoints hang off.
    assert tc.require_dashboard_session(
        _Req(cookies={auth.SESSION_COOKIE_NAME: client.cookies.get(auth.SESSION_COOKIE_NAME)})
    ) == ACCT_A


def test_enrolling_clears_the_state_through_the_endpoint(client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(USER_A, deferrals=10)
    h = _session(client)
    monkeypatch.setattr(tc, "_seat_management_allowed", lambda acct: True)
    monkeypatch.setattr(auth, "role_for", lambda uid: "admin")
    monkeypatch.setattr(auth, "can_view_billing", lambda uid: False)
    monkeypatch.setattr(auth, "is_account_owner", lambda a, u: False)
    assert client.get("/tenant/me").json()["mfa_prompt"]["dismissible"] is False

    _enrolled(monkeypatch, True)                     # they add a passkey
    st = client.get("/tenant/me").json()["mfa_prompt"]
    assert st["required"] is False
    assert store.deferrals(USER_A) == 0


class _Req:
    def __init__(self, method="GET", cookies=None, headers=None):
        self.method = method
        self.cookies = cookies or {}
        self.headers = headers or {}


# ── the individual realm, through its own router ────────────────────────────────────

@pytest.fixture
def me_client(saas_on):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def _individual_row():
    return {"id": INDIV_A, "user_id": USER_B, "account_id": ACCT_A, "email": "chris@example.com",
            "display_name": "Chris", "plan": "individual", "status": "active",
            "tenant_context": "none", "can_control_endpoint": True, "transfer_locked": False,
            "mfa_enabled": False}


def test_me_carries_the_prompt_and_defer_is_bounded(me_client, store, grace10, monkeypatch):
    _enrolled(monkeypatch, False)
    store.add(INDIV_A, deferrals=9)
    row = _individual_row()
    monkeypatch.setattr(uc, "get_individual_by_id", lambda i: row if i == INDIV_A else None)
    monkeypatch.setattr(uc, "_voice_state", lambda uid: (None, "personal"))

    tok = auth.issue_individual_session(individual_id=INDIV_A, user_id=USER_B,
                                        email=row["email"])
    me_client.cookies.set(auth.INDIVIDUAL_SESSION_COOKIE_NAME, tok)
    h = {"x-csrf-token": auth.issue_individual_csrf(individual_id=INDIV_A)}

    st = me_client.get("/me").json()["mfa_prompt"]
    assert st["required"] is True and st["dismissible"] is True and st["deferrals_remaining"] == 1

    assert me_client.post("/me/mfa-prompt/defer", headers=h).status_code == 200
    assert store.deferrals(INDIV_A) == 10
    store.sign_in(INDIV_A)

    assert me_client.get("/me").json()["mfa_prompt"]["dismissible"] is False
    assert me_client.post("/me/mfa-prompt/defer", headers=h).status_code == 409
    assert store.deferrals(INDIV_A) == 10
    # Not a lockout: the ordinary profile read still answers 200 on the same session.
    assert me_client.get("/me").status_code == 200
