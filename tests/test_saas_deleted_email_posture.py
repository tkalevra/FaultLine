"""DELETED-EMAIL POSTURE (migration 304, BUG-07) — a soft-deleted address is REUSABLE.

The wound (BUG-07 P2): the sanctioned moderation DELETE is a soft delete (status='disabled' +
deleted_at tombstone), but signup uniqueness was status-agnostic with no deleted_at carve-out —
so the deleted person could never log in (status != active) AND never re-sign up (the pre-check
plus the raw/canonical UNIQUE indexes). The mailbox was held hostage forever; the only exit was
a manual hard delete on prod (the ct8ball incident).

POSTURE (a), chosen deliberately and documented in the BUG-07 record: DELETED = RELEASED.
Follows tests/test_saas_canonical_signup_identity.py conventions (control plane stubbed;
FakeControlPlane enforces migration 304's PARTIAL unique predicates with real 23505-shaped
errors). The proofs that would hurt if they regressed:

  1. a soft-deleted row does NOT claim its mailbox — at the pre-check AND at the DB (the
     partial unique index lets a direct INSERT through).
  2. the canonical arm is freed too: a '+tag' variant of a DELETED address signs up.
  3. the anti-abuse properties are INTACT: a pending row still claims; a denied applicant row
     (disabled, NO tombstone) still claims; a BANNED identity is still refused at the door by
     public.identity_bans ALONE (a banned row is tombstoned — its freed email must not mean a
     freed person).
  4. RESTORE refuses (409) when the address has been re-registered by a newer live row —
     without that guard the restore UPDATE would 500 on the partial index.
  5. the pre-check SQL itself carries deleted_at IS NULL on BOTH arms (source pin), and
     migration 304 declares the partial predicates (file pin).

Run: python3 -m pytest tests/test_saas_deleted_email_posture.py -q  (tests/ is gitignored →
git add -f).
"""

import os

os.environ["SAAS_MODE"] = "1"
os.environ.setdefault("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")

import contextlib  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from saas import db as saas_db  # noqa: E402
from saas import email_identity as ei  # noqa: E402
from saas import moderation as mod  # noqa: E402
from saas import user_console as uc  # noqa: E402

_REAL_SIGNUP_BAN_CHECK = mod.signup_ban_check

ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "304_deleted_email_reusable.sql"


class FakeUniqueViolation(Exception):
    """Shaped like psycopg2.errors.UniqueViolation: SQLSTATE 23505 on ``pgcode``."""

    pgcode = "23505"

    def __init__(self, index: str):
        super().__init__(f'duplicate key value violates unique constraint "{index}"')


class FakeControlPlane:
    """public.individual_users + public.identity_bans, in two lists.

    Uniqueness is enforced EXACTLY as migration 304 declares it:

        individual_users  UNIQUE(email)           WHERE deleted_at IS NULL
                          UNIQUE(email_canonical) WHERE deleted_at IS NULL AND NOT NULL

    A tombstoned row therefore releases BOTH keys — that is the posture under test.
    """

    def __init__(self):
        self.individuals = []
        self.bans = []          # identity_bans rows: {email_canonical, lifted_at}
        self.sql_log = []

    def register_individual(self, email, status="active", deleted=False, banned=False):
        row = {"id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()), "account_id": ACCOUNT_ID,
               "email": email, "email_canonical": ei.canonical_email(email),
               "password_hash": "$argon2id$stub", "totp_secret": None, "mfa_enabled": False,
               "display_name": "Seeded", "plan": "individual", "status": status,
               "tenant_context": "none", "can_control_endpoint": True, "transfer_locked": False,
               "deleted": deleted, "banned": banned}
        self._check_individual(row)
        self.individuals.append(row)
        return row

    def ban(self, email):
        self.bans.append({"email_canonical": ei.canonical_email(email), "lifted_at": None,
                          "email_raw": email})

    def _check_individual(self, row):
        for r in self.individuals:
            if r["deleted"]:
                continue
            if r["email"] == row["email"]:
                raise FakeUniqueViolation("uq_individual_users_email_live")
            if r["email_canonical"] and r["email_canonical"] == row["email_canonical"]:
                raise FakeUniqueViolation("uq_individual_users_email_canonical")

    def execute(self, sql, params):
        s = " ".join(str(sql).split())
        self.sql_log.append(s)
        p = tuple(params or ())

        if "INSERT INTO public.individual_users" in s:
            row = {"id": str(uuid.uuid4()), "user_id": p[0], "account_id": p[1], "email": p[2],
                   "email_canonical": p[3], "password_hash": p[4], "totp_secret": p[5],
                   "mfa_enabled": bool(p[6]), "display_name": p[7], "plan": "individual",
                   "status": p[8], "tenant_context": "none", "can_control_endpoint": True,
                   "transfer_locked": False, "deleted": False, "banned": False}
            self._check_individual(row)
            self.individuals.append(row)
            return [(row["id"],)]

        # _email_taken (migration 304: deleted_at IS NULL on BOTH arms)
        if "FROM public.individual_users" in s and "deleted_at IS NULL" in s \
                and "email_canonical IS NOT NULL" in s:
            email, canonical = p[0], p[1]
            hit = [r for r in self.individuals
                   if not r["deleted"]
                   and (r["email"] == email
                        or (canonical and r["email_canonical"] == canonical))]
            return [(1,)] if hit else []

        # moderation._address_reclaimed (restore collision guard)
        if "FROM public.individual_users" in s and "id != %s::uuid" in s:
            exclude, email, canonical = p[0], p[1], p[2]
            hit = [r for r in self.individuals
                   if not r["deleted"] and r["id"] != exclude
                   and (r["email"] == email
                        or (canonical and r["email_canonical"] == canonical))]
            return [(1,)] if hit else []

        # moderation.ban_record — the ENFORCEMENT record, keyed on the canonical mailbox
        if "FROM public.identity_bans" in s:
            hit = [b for b in self.bans
                   if b["lifted_at"] is None and b["email_canonical"] == p[0]]
            return [("ban-id", p[0], None, "banned in test", None, None)] if hit else []

        # the LOGIN resolver (raw address, active only — unchanged by this lane)
        if "FROM public.individual_users" in s and "status = 'active'" in s:
            hit = [r for r in self.individuals
                   if r["email"] == p[0] and r["status"] == "active"]
            return [tuple(r[c] for c in (
                "id", "user_id", "account_id", "email", "password_hash", "totp_secret",
                "mfa_enabled", "display_name", "plan", "status", "tenant_context",
                "can_control_endpoint", "transfer_locked")) for r in hit]

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
    p = FakeControlPlane()

    @contextlib.contextmanager
    def _conn():
        yield _FakeConn(p)

    monkeypatch.setattr(saas_db, "connection", _conn)
    return p


@pytest.fixture
def individual_client(monkeypatch, plane):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(uc._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(uc._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(uc._name_guard, "check_display_name",
                        lambda n: uc._name_guard.NameDecision(ok=True) if hasattr(
                            uc._name_guard, "NameDecision") else type("D", (), {"ok": True})())
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
    monkeypatch.setattr(uc._platform_settings, "require_email_activation", lambda: False)
    monkeypatch.setattr(uc._plans, "trial_days", lambda: 30)
    monkeypatch.setattr(uc, "_provision_individual_seat", lambda a, u, **kw: {"ok": True, "reason": ""})
    monkeypatch.setattr(uc, "send_welcome_to_individual", lambda who: {"delivery": "skipped"})
    monkeypatch.setattr(uc._tokens, "mint_user_token",
                        lambda a, u: {"token": "flu_stub", "token_prefix": "flu_stu"})
    monkeypatch.setattr(uc._audit, "record", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app)


def _signup(client, email):
    return client.post("/me/signup", json={"email": email, "password": "correct horse battery",
                                           "display_name": "Chris"})


# ══════════════════════════════════════════════════════════════════════════════════
# 1-2. A soft-deleted row does NOT claim — pre-check AND database
# ══════════════════════════════════════════════════════════════════════════════════

def test_soft_deleted_email_is_free_at_the_pre_check(plane):
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=True)
    assert uc._email_taken("ct8ball@gmail.com") is False


def test_soft_deleted_email_signs_up_again_end_to_end(individual_client, plane):
    """THE BUG-07 REPRO, inverted: the exact person the owner had to manually hard-delete on
    prod can now just... sign up again."""
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=True)
    r = _signup(individual_client, "ct8ball@gmail.com")
    assert r.status_code == 200, r.text
    assert len(plane.individuals) == 2
    assert sum(1 for x in plane.individuals if x["deleted"]) == 1


def test_plus_tag_variant_of_a_deleted_address_is_free(individual_client, plane):
    """The canonical arm is freed too — migration 304's partial predicate covers BOTH indexes."""
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=True)
    r = _signup(individual_client, "ct.8ball+new@gmail.com")   # folds to the same canonical box
    assert r.status_code == 200, r.text


def test_the_pre_check_sql_filters_the_tombstone_on_both_arms(plane):
    plane.register_individual("a@b.com")
    uc._email_taken("a@b.com")
    sql = plane.sql_log[-1]
    assert "deleted_at IS NULL" in sql
    assert "email = %s" in sql and "email_canonical = %s" in sql


# ══════════════════════════════════════════════════════════════════════════════════
# 3. The anti-abuse properties are INTACT
# ══════════════════════════════════════════════════════════════════════════════════

def test_pending_row_still_claims(individual_client, plane):
    plane.register_individual("ct8ball@gmail.com", status="pending_approval")
    r = _signup(individual_client, "ct8ball+again@gmail.com")
    assert r.status_code == 409
    assert r.json()["detail"] == ei.IDENTITY_TAKEN_MESSAGE


def test_denied_applicant_row_still_claims(individual_client, plane):
    """A DENIED applicant is 'disabled' with NO tombstone — only the moderation DELETE/BAN
    verbs stamp deleted_at — so the decision cannot be re-applied around."""
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=False)
    r = _signup(individual_client, "ct8ball@gmail.com")
    assert r.status_code == 409


def test_live_address_is_still_taken(individual_client, plane):
    plane.register_individual("ct8ball@gmail.com")
    assert uc._email_taken("ct8ball@gmail.com") is True
    r = _signup(individual_client, "ct8ball+x@gmail.com")
    assert r.status_code == 409


def test_a_BANNED_identity_is_still_refused_even_though_its_row_is_tombstoned(
        individual_client, plane, monkeypatch):
    """THE BAN-EVASION GUARD — why posture (a) is safe. A ban stamps banned_at AND deleted_at,
    so the roster row no longer claims the mailbox. The ban's teeth are public.identity_bans:
    both doors call signup_ban_check BEFORE the duplicate pre-check matters."""
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=True, banned=True)
    plane.ban("ct8ball@gmail.com")
    # serve the REAL moderation ban check against the fake identity_bans table
    monkeypatch.setattr(uc._moderation, "signup_ban_check", _REAL_SIGNUP_BAN_CHECK)
    assert mod.is_banned("ct.8ball@googlemail.com") is True     # canonical folding intact
    r = _signup(individual_client, "ct.8ball+whatever@gmail.com")
    assert r.status_code == 400
    assert r.json()["detail"] == uc._signup_filter.REJECTION_MESSAGE


def test_unbanned_deleted_identity_can_re_sign_up(individual_client, plane, monkeypatch):
    """UNBAN lifts identity_bans and the row stays deleted — the person may now re-register,
    which is exactly what unban's copy promises ('this person may sign up again')."""
    plane.register_individual("ct8ball@gmail.com", status="disabled", deleted=True)
    # no identity_bans row at all: not banned, just deleted
    r = _signup(individual_client, "ct8ball@gmail.com")
    assert r.status_code == 200, r.text


# ══════════════════════════════════════════════════════════════════════════════════
# 4. RESTORE collision guard
# ══════════════════════════════════════════════════════════════════════════════════

_DELETED_TARGET = {
    "individual_id": "11111111-1111-4111-8111-111111111111", "user_id": "u", 
    "account_id": ACCOUNT_ID, "email": "ct8ball@gmail.com",
    "email_canonical": "ct8ball@gmail.com", "display_name": "x", "status": "disabled",
    "plan": "individual", "created_at": None, "last_login_at": None, "trial_ends_at": None,
    "blocked_at": None, "blocked_by": None, "block_reason": None,
    "deleted_at": "2026-10-30T00:00:00+00:00", "deleted_by": "op", "delete_reason": "test",
    "banned_at": None, "banned_by": None, "ban_reason": None,
}


def test_restore_refuses_when_the_address_was_reregistered(monkeypatch, plane):
    plane.register_individual("ct8ball@gmail.com")      # the NEWER live row
    plane.individuals[0]["id"] = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(mod, "get_subscriber", lambda i: dict(_DELETED_TARGET))
    out = mod.restore_subscriber("11111111-1111-4111-8111-111111111111")
    assert out["ok"] is False
    assert out["status_code"] == 409
    assert "re-registered" in out["reason"]


def test_restore_proceeds_when_the_address_is_still_free(monkeypatch, plane):
    monkeypatch.setattr(mod, "get_subscriber", lambda i: dict(_DELETED_TARGET))
    monkeypatch.setattr(mod, "_apply", lambda *a, **k: {"ok": True, "status_code": 200})
    out = mod.restore_subscriber("11111111-1111-4111-8111-111111111111")
    assert out["ok"] is True


def test_address_reclaimed_query_excludes_the_row_itself_and_the_tombstoned(plane):
    row = plane.register_individual("ct8ball@gmail.com", deleted=True)
    assert mod._address_reclaimed("ct8ball@gmail.com", "ct8ball@gmail.com", row["id"]) is False
    live = plane.register_individual("someone@gmail.com")
    assert mod._address_reclaimed("someone@gmail.com", "someone@gmail.com", row["id"]) is True
    assert "deleted_at IS NULL" in plane.sql_log[-1]


def test_address_reclaimed_fails_closed(monkeypatch, plane):
    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("control plane down")
        yield

    from saas import db as _db
    monkeypatch.setattr(_db, "connection", _boom)
    assert mod._address_reclaimed("a@b.com", "a@b.com", "x") is True


# ══════════════════════════════════════════════════════════════════════════════════
# 5. File pins — the migration declares the partial predicates
# ══════════════════════════════════════════════════════════════════════════════════

def test_migration_304_declares_the_partial_unique_predicates():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS individual_users_email_key" in sql
    assert "uq_individual_users_email_live" in sql
    assert "WHERE deleted_at IS NULL" in sql
    # the canonical index must be REBUILT (drop + recreate), not IF-NOT-EXISTED into staleness
    assert "DROP INDEX IF EXISTS public.uq_individual_users_email_canonical" in sql
    assert "WHERE email_canonical IS NOT NULL AND deleted_at IS NULL" in sql


def test_email_taken_fails_closed_on_an_unreadable_control_plane(monkeypatch):
    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("control plane down")
        yield

    monkeypatch.setattr(saas_db, "connection", _boom)
    assert uc._email_taken("a@b.com") is True
