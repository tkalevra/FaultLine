"""CANONICAL SIGNUP IDENTITY (migration 231) — one mailbox, one account, on BOTH doors.

Signup uniqueness used to key on the RAW address, so ``a+1@gmail.com`` and ``a+2@gmail.com``
were two accounts and two free trials. This file pins the fix and — just as importantly — the
three things the fix must NOT break.

Follows the tests/test_saas_tenant_self_signup.py conventions: every control-plane collaborator
is stubbed, so the file is green without a live control plane or a Redis. The ONE thing that is
not stubbed away is the uniqueness itself: ``FakeControlPlane`` below implements the actual
UNIQUE indexes migration 231 creates, and raises a real 23505-shaped error when they are
violated, so the "a race still gets a clean 409" proof exercises the code path a live database
would exercise.

WHAT IS PROVEN
  1. A '+tag' variant of a registered address is refused — at /me/signup AND at /signup/team.
  2. A Gmail DOTTED variant is refused (documented Google folding).
  3. A NON-Gmail dotted variant is STILL ALLOWED. This is the false-positive guard and it is the
     most important test in the file: john.smith@corp.com and johnsmith@corp.com are plausibly
     two different employees, and under a UNIQUE index a wrong fold is not a mis-matched ban, it
     is a stranger who cannot sign up and never learns why.
  4. LOGIN STILL RESOLVES ON THE RAW ADDRESS, including a '+tag' one, and does NOT resolve on the
     canonical form. Nobody's stored email is rewritten. People keep credentials in a password
     manager; silently changing a username locks them out of their own vault entry.
  5. The rejection copy is IDENTICAL for an exact-address match and for a variant match — same
     words, same status. Different copy would be an account-enumeration oracle.
  6. The DATABASE, not the pre-check, is the authority: a signup that passes the SELECT and then
     loses the INSERT race gets the SAME 409, not a 500.

Run: python3 -m pytest tests/test_saas_canonical_signup_identity.py -q   (tests/ is gitignored →
git add -f).
"""

import os

os.environ["SAAS_MODE"] = "1"
os.environ.setdefault("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")

import uuid  # noqa: E402

import pytest  # noqa: E402

from saas import db as saas_db  # noqa: E402
from saas import email_identity as ei  # noqa: E402
from saas import tenant_signup as ts  # noqa: E402
from saas import user_console as uc  # noqa: E402

ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
REQUEST_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


# ══════════════════════════════════════════════════════════════════════════════════
# A control plane that enforces migration 231's indexes and nothing else
# ══════════════════════════════════════════════════════════════════════════════════

class FakeUniqueViolation(Exception):
    """Shaped like psycopg2.errors.UniqueViolation: SQLSTATE 23505 on ``pgcode``.

    saas/db.is_unique_violation reads ``pgcode`` first, so this exercises the exact branch a
    live PostgreSQL takes — not just the message-text fallback."""

    pgcode = "23505"

    def __init__(self, index: str):
        super().__init__(f'duplicate key value violates unique constraint "{index}"')


class FakeControlPlane:
    """public.individual_users / dashboard_users / tenant_signup_requests, in three lists.

    Only the statements this feature issues are interpreted; anything else returns no rows, which
    is what the best-effort writes on the signup path (last_login_at, trial stamping, audit) want
    anyway. Uniqueness is enforced EXACTLY as migration 231 declares it:

        individual_users        UNIQUE(email)            -- migration 149
                                UNIQUE(email_canonical) WHERE NOT NULL
        dashboard_users         UNIQUE(username)         -- pre-existing
                                UNIQUE(email_canonical) WHERE NOT NULL
        tenant_signup_requests  UNIQUE(email_canonical) WHERE NOT NULL AND status='pending'
    """

    def __init__(self):
        self.individuals = []
        self.dashboard = []
        self.pending = []
        # Set by a test to make the NEXT INSERT lose a race it had already won the SELECT for.
        self.race_on_insert = False

    # ── seeding (bypasses nothing: the same constraints apply) ──
    def register_individual(self, email, status="active"):
        row = {"id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()), "account_id": ACCOUNT_ID,
               "email": email, "email_canonical": ei.canonical_email(email),
               "password_hash": "$argon2id$stub", "totp_secret": None, "mfa_enabled": False,
               "display_name": "Seeded", "plan": "individual", "status": status,
               "tenant_context": "none", "can_control_endpoint": True, "transfer_locked": False}
        self._check_individual(row)
        self.individuals.append(row)
        return row

    def register_dashboard(self, username, status="active"):
        row = {"id": str(uuid.uuid4()), "account_id": ACCOUNT_ID, "username": username,
               "email_canonical": ei.canonical_email(username),
               "password_hash": "$argon2id$stub", "totp_secret": None, "mfa_enabled": False,
               "role": "admin", "status": status, "display_name": None}
        self._check_dashboard(row)
        self.dashboard.append(row)
        return row

    def register_pending(self, email):
        row = {"id": str(uuid.uuid4()), "email": email,
               "email_canonical": ei.canonical_email(email), "status": "pending"}
        self._check_pending(row)
        self.pending.append(row)
        return row

    # ── the indexes ──
    def _check_individual(self, row):
        for r in self.individuals:
            if r["email"] == row["email"]:
                raise FakeUniqueViolation("individual_users_email_key")
            if r["email_canonical"] and r["email_canonical"] == row["email_canonical"]:
                raise FakeUniqueViolation("uq_individual_users_email_canonical")

    def _check_dashboard(self, row):
        for r in self.dashboard:
            if r["username"] == row["username"]:
                raise FakeUniqueViolation("dashboard_users_username_key")
            if r["email_canonical"] and r["email_canonical"] == row["email_canonical"]:
                raise FakeUniqueViolation("uq_dashboard_users_email_canonical")

    def _check_pending(self, row):
        for r in self.pending:
            if r["status"] != "pending":
                continue
            if r["email_canonical"] and r["email_canonical"] == row["email_canonical"]:
                raise FakeUniqueViolation("uq_tenant_signup_requests_pending_canonical")

    # ── statement dispatch ──
    def execute(self, sql, params):
        s = " ".join(str(sql).split())
        p = tuple(params or ())

        if "INSERT INTO public.individual_users" in s:
            if self.race_on_insert:
                self.race_on_insert = False
                raise FakeUniqueViolation("uq_individual_users_email_canonical")
            row = {"id": str(uuid.uuid4()), "user_id": p[0], "account_id": p[1], "email": p[2],
                   "email_canonical": p[3], "password_hash": p[4], "totp_secret": p[5],
                   "mfa_enabled": bool(p[6]), "display_name": p[7], "plan": "individual",
                   "status": p[8], "tenant_context": "none", "can_control_endpoint": True,
                   "transfer_locked": False}
            self._check_individual(row)
            self.individuals.append(row)
            return [(row["id"],)]

        if "INSERT INTO public.dashboard_users" in s:
            if self.race_on_insert:
                self.race_on_insert = False
                raise FakeUniqueViolation("uq_dashboard_users_email_canonical")
            row = {"id": str(uuid.uuid4()), "account_id": p[0], "username": p[1],
                   "password_hash": p[2], "totp_secret": p[3], "mfa_enabled": bool(p[4]),
                   "role": p[5], "status": "active", "display_name": None,
                   "email_canonical": p[7]}
            self._check_dashboard(row)
            self.dashboard.append(row)
            return [(row["id"],)]

        if "INSERT INTO public.tenant_signup_requests" in s:
            if self.race_on_insert:
                self.race_on_insert = False
                raise FakeUniqueViolation("uq_tenant_signup_requests_pending_canonical")
            row = {"id": REQUEST_ID, "email": p[2], "email_canonical": p[3], "status": "pending"}
            self._check_pending(row)
            self.pending.append(row)
            return [(row["id"],)]

        # ── the uniqueness pre-checks ──
        if "FROM public.individual_users" in s and "email_canonical IS NOT NULL" in s:
            email, canonical = p[0], p[1]
            hit = [r for r in self.individuals
                   if r["email"] == email
                   or (canonical and r["email_canonical"] == canonical)]
            return [(1,)] if hit else []

        if "FROM public.dashboard_users" in s and "email_canonical IS NOT NULL" in s:
            username, canonical = p[0], p[1]
            hit = [r for r in self.dashboard
                   if r["username"] == username
                   or (canonical and r["email_canonical"] == canonical)]
            return [(1,)] if hit else []

        if "FROM public.tenant_signup_requests" in s and "email_canonical IS NOT NULL" in s:
            email, canonical = p[0], p[1]
            hit = [r for r in self.pending
                   if r["status"] == "pending"
                   and (r["email"].lower() == str(email).lower()
                        or (canonical and r["email_canonical"] == canonical))]
            return [(1,)] if hit else []

        # ── the LOGIN resolvers (raw address only — this is the point) ──
        if "FROM public.individual_users" in s and "status = 'active'" in s:
            hit = [r for r in self.individuals
                   if r["email"] == p[0] and r["status"] == "active"]
            return [tuple(r[c] for c in (
                "id", "user_id", "account_id", "email", "password_hash", "totp_secret",
                "mfa_enabled", "display_name", "plan", "status", "tenant_context",
                "can_control_endpoint", "transfer_locked")) for r in hit]

        if "FROM public.dashboard_users" in s and "status = 'active'" in s:
            hit = [r for r in self.dashboard
                   if r["username"] == p[0] and r["status"] == "active"]
            return [tuple(r[c] for c in (
                "id", "account_id", "username", "password_hash", "totp_secret", "mfa_enabled",
                "role", "status", "display_name")) for r in hit]

        return []


class _FakeCursor:
    def __init__(self, plane):
        self._plane = plane
        self._rows = []

    def execute(self, sql, params=()):
        self._rows = self._plane.execute(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    @property
    def rowcount(self):
        return len(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, plane):
        self._plane = plane

    def cursor(self):
        return _FakeCursor(self._plane)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def plane(monkeypatch):
    """One fake control plane behind saas.db.connection — the SAME module object user_console,
    dashboard_auth and tenant_signup all import, so both doors see one identity store."""
    p = FakeControlPlane()

    import contextlib

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(p)

    monkeypatch.setattr(saas_db, "connection", _conn)
    return p


# ══════════════════════════════════════════════════════════════════════════════════
# Door A — POST /me/signup
# ══════════════════════════════════════════════════════════════════════════════════

INDIVIDUAL_BODY = {"email": "chris@example.com", "password": "correct horse battery",
                   "display_name": "Chris"}


@pytest.fixture
def individual_client(monkeypatch, plane):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(uc._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(uc._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(uc._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e, **kw: uc._signup_filter.Decision(True))
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt", lambda e, **kw: True)
    monkeypatch.setattr(uc._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", lambda: ACCOUNT_ID)
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(uc._plans, "trial_days", lambda: 30)
    monkeypatch.setattr(uc, "_provision_individual_seat", lambda a, u: {"ok": True, "reason": ""})
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda who: {"delivery": "skipped"})
    monkeypatch.setattr(uc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_stub", "token_prefix": "flu_stu"})
    monkeypatch.setattr(uc._audit, "record", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def _signup(client, email):
    return client.post("/me/signup", json=dict(INDIVIDUAL_BODY, email=email))


def test_individual_plus_tag_variant_of_a_registered_address_is_refused(individual_client, plane):
    """THE HOLE: ct8ball+a@gmail.com and ct8ball+b@gmail.com are one inbox and one human."""
    plane.register_individual("ct8ball@gmail.com")
    r = _signup(individual_client, "ct8ball+faultline@gmail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE
    assert len(plane.individuals) == 1          # nothing was written


def test_individual_gmail_dotted_variant_is_refused(individual_client, plane):
    """Google documents that dots in a Gmail local part are ignored — one mailbox."""
    plane.register_individual("ct8ball@gmail.com")
    r = _signup(individual_client, "ct.8.ball@googlemail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_individual_NON_gmail_dotted_variant_is_STILL_ALLOWED(individual_client, plane):
    """THE FALSE-POSITIVE GUARD — the most important test here.

    john.smith@corp.com and johnsmith@corp.com are plausibly TWO DIFFERENT EMPLOYEES. Almost no
    provider outside Gmail ignores dots. Folding them under a UNIQUE index would refuse signup to
    a person who has done nothing, invisibly to us and infuriatingly to them."""
    plane.register_individual("john.smith@corp.com")
    r = _signup(individual_client, "johnsmith@corp.com")
    assert r.status_code == 200, r.text
    assert len(plane.individuals) == 2
    # ...and both keep their own raw address, untouched.
    assert {row["email"] for row in plane.individuals} == {
        "john.smith@corp.com", "johnsmith@corp.com"}


def test_individual_non_gmail_plus_tag_IS_folded(individual_client, plane):
    """Plus-tagging IS folded off Gmail (very widely correct, and overridable per deployment via
    SAAS_EMAIL_NO_PLUS_DOMAINS). Only DOT-folding is Gmail-only."""
    plane.register_individual("john.smith@corp.com")
    r = _signup(individual_client, "john.smith+newsletter@corp.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_individual_unrelated_address_still_signs_up(individual_client, plane):
    plane.register_individual("ct8ball@gmail.com")
    r = _signup(individual_client, "someone.else@gmail.com")
    assert r.status_code == 200, r.text
    assert len(plane.individuals) == 2


def test_individual_pending_row_also_claims_the_canonical_mailbox(individual_client, plane):
    """Status-AGNOSTIC: a held/denied application claims the mailbox too, or a denied applicant
    re-applies around the decision by pressing '+'."""
    plane.register_individual("ct8ball@gmail.com", status="pending_approval")
    r = _signup(individual_client, "ct8ball+again@gmail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


# ── the DB is the authority: a race that beats the SELECT still gets a clean 409 ──

def test_individual_racing_insert_gets_the_same_409_not_a_500(individual_client, plane):
    """Two requests can both pass the pre-check SELECT before either INSERTs. The UNIQUE index
    settles it, and the loser must be told the ordinary thing — not "server error"."""
    plane.race_on_insert = True                 # the SELECT sees a free mailbox; the INSERT does not
    r = _signup(individual_client, "ct8ball+racer@gmail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_individual_racing_insert_on_the_HELD_path_too(individual_client, plane, monkeypatch):
    """Same guarantee with the approval valve ON, where the login row is written PENDING."""
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: True)
    plane.race_on_insert = True
    r = _signup(individual_client, "ct8ball+racer@gmail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_create_individual_raises_identity_taken_on_a_unique_violation(plane):
    """The seam itself: a 23505 becomes _IdentityTaken, never a silent None (which the callers
    render as a 500 "could not persist the login")."""
    plane.register_individual("ct8ball@gmail.com")
    with pytest.raises(uc._IdentityTaken):
        uc._create_individual(user_id=str(uuid.uuid4()), account_id=ACCOUNT_ID,
                              email="ct8ball+x@gmail.com", password_hash="$argon2id$stub",
                              display_name=None)


def test_create_login_for_seat_still_returns_None_on_conflict(plane, monkeypatch):
    """The OPERATOR seat surface documents "None on conflict" and its callers render that. The
    new exception must not escape and turn a handled 409 into a 500."""
    monkeypatch.setattr(uc._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(uc._auth, "generate_temp_password", lambda: "temp-pass-1234")
    plane.register_individual("ct8ball@gmail.com")
    assert uc.create_login_for_seat(user_id=str(uuid.uuid4()), account_id=ACCOUNT_ID,
                                    email="ct8ball+x@gmail.com") is None


# ══════════════════════════════════════════════════════════════════════════════════
# Door B — POST /signup/team
# ══════════════════════════════════════════════════════════════════════════════════

TEAM_BODY = {"company": "Acme Robotics", "email": "owner@acmerobotics.com",
             "password": "correct horse battery", "display_name": "Wile E. Coyote"}


@pytest.fixture
def team_client(monkeypatch, plane):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ts._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(ts._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(ts._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(ts._signup_filter, "evaluate",
                        lambda e, **kw: ts._signup_filter.Decision(True))
    monkeypatch.setattr(ts._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(ts._signup_ledger, "decide", lambda e: ts._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(ts._signup_ledger, "record_attempt", lambda e, **kw: True)
    monkeypatch.setattr(ts._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(ts._platform_settings, "notify_email", lambda: None)
    monkeypatch.setattr(ts._platform_settings, "trial_days", lambda: 30)
    monkeypatch.setattr(ts._accounts, "create_account",
                        lambda name, slug, seat_limit, plan, account_type="standard",
                        account_kind="tenant": {
                            "account_id": ACCOUNT_ID, "slug": slug, "account_kind": account_kind,
                            "account_type": account_type, "key_prefix": "fl_abc",
                            "api_key": "fl_abcSECRET"})
    monkeypatch.setattr(ts._accounts, "start_account_trial", lambda a, source=None: {"days": 30})
    monkeypatch.setattr(ts._accounts, "set_chatbot_trial", lambda *a, **kw: None)
    monkeypatch.setattr(ts._accounts, "set_account_status", lambda *a, **kw: True)
    monkeypatch.setattr(ts._accounts, "delete_account", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(ts._auth, "set_display_name", lambda *a, **kw: True)
    monkeypatch.setattr(ts._auth, "touch_last_login", lambda *a, **kw: None)
    monkeypatch.setattr(ts, "send_welcome_to_tenant",
                        lambda **kw: {"delivery": "skipped"})
    monkeypatch.setattr(ts, "_notify_operator_of_application", lambda **kw: None)
    monkeypatch.setattr(ts._audit, "record", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(ts.router)
    return TestClient(app)


def _team_signup(client, email):
    return client.post("/signup/team", json=dict(TEAM_BODY, email=email))


def test_team_plus_tag_variant_of_a_registered_owner_is_refused(team_client, plane):
    """The SAME rule on the SAME product — a farm that is closed on one door and open on the
    other is not closed."""
    plane.register_dashboard("owner@acmerobotics.com")
    r = _team_signup(team_client, "owner+second@acmerobotics.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE
    assert len(plane.dashboard) == 1


def test_team_gmail_dotted_variant_is_refused(team_client, plane):
    plane.register_dashboard("ct8ball@gmail.com")
    r = _team_signup(team_client, "ct.8ball@googlemail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_team_NON_gmail_dotted_variant_is_STILL_ALLOWED(team_client, plane):
    """The false-positive guard, on the tenant door. Two employees at one company must both be
    able to open a workspace."""
    plane.register_dashboard("john.smith@corp.com")
    r = _team_signup(team_client, "johnsmith@corp.com")
    assert r.status_code == 200, r.text
    assert {row["username"] for row in plane.dashboard} == {
        "john.smith@corp.com", "johnsmith@corp.com"}


def test_team_variant_of_a_PENDING_application_is_refused(team_client, plane, monkeypatch):
    """Approval ON: the mailbox is claimed by the queued application too, so it cannot be
    re-applied for under a tag while a human decides."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: True)
    plane.register_pending("owner@acmerobotics.com")
    r = _team_signup(team_client, "owner+again@acmerobotics.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE
    assert len(plane.pending) == 1


def test_team_racing_owner_login_insert_gets_409_and_rolls_the_account_back(team_client, plane,
                                                                            monkeypatch):
    """The pre-check passed, the UNIQUE index did not. The applicant gets the ordinary 409 and the
    orphan account is rolled back rather than stranded holding a slug nobody can sign in to."""
    rolled = {}
    monkeypatch.setattr(ts._accounts, "set_account_status",
                        lambda a, status, **kw: rolled.update(status=status) or True)
    monkeypatch.setattr(ts._accounts, "delete_account",
                        lambda a, slug, **kw: rolled.update(deleted=a) or {"ok": True})
    plane.race_on_insert = True
    r = _team_signup(team_client, "owner+racer@acmerobotics.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE
    assert rolled.get("status") == "archived" and rolled.get("deleted") == ACCOUNT_ID


def test_team_racing_pending_insert_is_409_not_503(team_client, plane, monkeypatch):
    """A queue-write failure is a 503 ("signups are temporarily unavailable"). A mailbox COLLISION
    is not a failure — it must not be dressed up as one."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: True)
    plane.race_on_insert = True
    r = _team_signup(team_client, "owner+racer@acmerobotics.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


# ══════════════════════════════════════════════════════════════════════════════════
# THE CONSTRAINT THAT MATTERS MOST — login is untouched
# ══════════════════════════════════════════════════════════════════════════════════

def test_login_resolves_on_the_RAW_address_including_a_plus_tag(plane):
    """Somebody who registered chris+faultline@gmail.com signs in with EXACTLY that string. It is
    what their password manager holds; canonicalising the credential would lock them out of their
    own vault entry for a benefit they never asked for."""
    plane.register_individual("chris+faultline@gmail.com")
    found = uc.get_individual_by_email("chris+faultline@gmail.com")
    assert found is not None
    assert found["email"] == "chris+faultline@gmail.com"


def test_login_does_NOT_resolve_on_the_canonical_form(plane):
    """The canonical form is an identity KEY, never a credential. If it authenticated, anyone who
    guessed a shorter variant of a registered address would be at the right login row."""
    plane.register_individual("chris+faultline@gmail.com")
    assert ei.canonical_email("chris+faultline@gmail.com") == "chris@gmail.com"
    assert uc.get_individual_by_email("chris@gmail.com") is None
    assert uc.get_individual_by_email("ch.ris@gmail.com") is None


def test_tenant_login_resolves_on_the_raw_username(plane):
    plane.register_dashboard("owner+billing@acmerobotics.com")
    assert ts._auth.get_user_by_username("owner+billing@acmerobotics.com") is not None
    assert ts._auth.get_user_by_username("owner@acmerobotics.com") is None


def test_signup_never_rewrites_the_stored_address(individual_client, plane):
    """The row stores the address AS TYPED (lowercased, as it always was) and the canonical form
    BESIDE it — never instead of it."""
    r = _signup(individual_client, "Chris+FaultLine@Gmail.com")
    assert r.status_code == 200, r.text
    row = plane.individuals[0]
    assert row["email"] == "chris+faultline@gmail.com"
    assert row["email_canonical"] == "chris@gmail.com"


def test_login_lookup_sql_has_no_canonical_column():
    """A source guard: if someone later 'helpfully' switches the login resolver to the canonical
    column, every person with a '+tag' address silently loses their login."""
    import inspect
    src = inspect.getsource(uc.get_individual_by_email)
    assert "WHERE email = %s" in src
    assert "email_canonical" not in src
    src2 = inspect.getsource(ts._auth.get_user_by_username)
    assert "WHERE username = %s" in src2
    assert "email_canonical" not in src2


# ══════════════════════════════════════════════════════════════════════════════════
# NO ENUMERATION ORACLE — the copy is the same either way
# ══════════════════════════════════════════════════════════════════════════════════

def test_exact_match_and_variant_match_are_indistinguishable(individual_client, plane):
    """Different copy for "that exact address is registered" vs "a variant of it is" would let a
    caller map out which addresses exist by probing +1, +2, dotted forms… Same words, same
    status, either way."""
    plane.register_individual("ct8ball@gmail.com")
    exact = _signup(individual_client, "ct8ball@gmail.com")
    variant = _signup(individual_client, "ct8ball+probe@gmail.com")
    dotted = _signup(individual_client, "ct.8.ball@gmail.com")
    assert exact.status_code == variant.status_code == dotted.status_code == 409
    assert exact.json()["detail"] == variant.json()["detail"] == dotted.json()["detail"]


def test_both_doors_use_the_same_words(individual_client, team_client, plane):
    """One product, one rule, one explanation. Two wordings would also be a fingerprint telling a
    prober which door they are at."""
    plane.register_individual("ct8ball@gmail.com")
    plane.register_dashboard("ct8ball@gmail.com")
    a = _signup(individual_client, "ct8ball+x@gmail.com")
    b = _team_signup(team_client, "ct8ball+x@gmail.com")
    assert a.status_code == b.status_code == 409
    assert a.json()["detail"] == b.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_the_rejection_copy_actually_explains_itself():
    """A refusal a person cannot understand reads as a bug. Someone who typed a fresh-looking
    '+tag' has to learn WHY it is refused and WHERE to go next, without being told whether the
    exact address they typed exists."""
    m = ei.IDENTITY_TAKEN_MESSAGE
    assert "already exists" in m                      # what happened
    assert "+tag" in m or "+tag" in m.replace("“", "").replace("”", "")
    assert "Gmail" in m and "dots" in m               # why a fresh-looking address is refused
    assert "sign in" in m.lower()                     # where to go
    assert "password" in m.lower()                    # ...and the recovery route
    # And it must NOT reveal which of the two cases the caller hit.
    for leak in ("variant", "exact", "instead of", "you typed"):
        assert leak not in m.lower()


# ══════════════════════════════════════════════════════════════════════════════════
# The canonicalisation contract this feature now leans on (do not weaken)
# ══════════════════════════════════════════════════════════════════════════════════

def test_canonicalisation_contract_is_unchanged():
    """Restated at the point of USE, because uniqueness is a far less forgiving consumer than ban
    matching: a wrong fold used to mis-match a ban, and now refuses a signup outright."""
    assert ei.canonical_email("a+1@gmail.com") == ei.canonical_email("a+2@gmail.com")
    assert ei.canonical_email("c.t.8ball@googlemail.com") == "ct8ball@gmail.com"
    # NON-Gmail: '+' folded, '.' PRESERVED.
    assert ei.canonical_email("john.smith+x@corp.com") == "john.smith@corp.com"
    assert ei.canonical_email("john.smith@corp.com") != ei.canonical_email("johnsmith@corp.com")
