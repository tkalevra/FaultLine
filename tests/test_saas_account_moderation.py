"""ACCOUNT MODERATION (block / delete / ban) + the global signup spam filter.

Migrations 228 (moderation columns + public.identity_bans) and 229
(public.signup_filter_rules). Modules under test: saas/email_identity.py,
saas/signup_filter.py, saas/moderation.py, plus the operator routes in saas/admin.py and the
enforcement points in saas/user_console.py and saas/tenant_signup.py.

Follows the tests/test_saas_tenant_control.py conventions: every control-plane call is either
monkeypatched or served by the ``FakeDB`` below, so this file is always green without a real
control plane.

The proofs that would hurt if they regressed:
  1. canonicalisation folds the owner's Gmail plus/dot cases onto ONE identity …
  2. … and does NOT fold john.smith@ onto johnsmith@ on a non-Gmail domain. This is the
     false-positive guard: getting it wrong silently denies signup to an innocent person, and
     they never find out why. It is asserted explicitly, twice.
  3. each verb writes the state it claims to and is AUDITED with the operator as actor.
  4. a banned identity is refused at BOTH signup doors, with NOTHING provisioned.
  5. a pathological / regex / over-long filter pattern is refused at SAVE time, not at signup.
  6. a broken rule store FAILS OPEN — the spam filter can never take the funnel down.
  7. every moderation route is operator-only.
  8. the historical tabs list the right rows (mutually exclusive predicates).

Run: python3 -m pytest tests/test_saas_account_moderation.py -q   (tests/ gitignored →
git add -f)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saas import admin as saas_admin
from saas import email_identity as ei
from saas import moderation as mod
from saas import signup_filter as sf


OP_KEY = "op-moderation-secret-key"
IND_ID = "11111111-1111-4111-8111-111111111111"
ACCT_ID = "22222222-2222-4222-8222-222222222222"


# ════════════════════════════════════════════════════════════════════════════════
# FakeDB — records every statement, serves canned rows. Lets the verb tests assert the
# ACTUAL SQL (which status value, which guard in the WHERE clause) rather than trusting a
# stubbed-out wrapper to have done the right thing.
# ════════════════════════════════════════════════════════════════════════════════

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = db.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.statements.append((" ".join(str(sql).split()), params))
        self.rowcount = self.db.rowcount

    def fetchone(self):
        return self.db.rows.pop(0) if self.db.rows else None

    def fetchall(self):
        out, self.db.rows = list(self.db.rows), []
        return out


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1


class FakeDB:
    """``rows`` is a FIFO of fetchone()/fetchall() results; ``rowcount`` what UPDATE reports."""

    def __init__(self, rows=None, rowcount=1, fail=False):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.fail = fail
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def connection(self):
        if self.fail:
            raise RuntimeError("control plane down")
        db = self

        class _CM:
            def __enter__(self_inner):
                return FakeConn(db)

            def __exit__(self_inner, *a):
                return False

        return _CM()

    def sql(self):
        return " || ".join(s for s, _ in self.statements)


def _subscriber_row(email="ct8ball@gmail.com", status="active",
                    blocked=None, deleted=None, banned=None):
    """One row shaped exactly like moderation._ROSTER_COLS."""
    return (IND_ID, "33333333-3333-4333-8333-333333333333", ACCT_ID, email,
            ei.canonical_email(email), "Chris", status, "individual",
            None, None, None,
            blocked, None, None, deleted, None, None, banned, None, None)


@pytest.fixture(autouse=True)
def clean_filter_cache():
    """The rule cache is process-global with a TTL; drop it either side of every test so one
    test's FakeDB can never answer another test's evaluation."""
    sf.invalidate()
    yield
    sf.invalidate()


@pytest.fixture
def audit_spy(monkeypatch):
    """Capture every audit row instead of writing one."""
    rows = []
    monkeypatch.setattr(mod._audit, "record",
                        lambda action, **kw: rows.append((action, kw)) or True)
    return rows


# ════════════════════════════════════════════════════════════════════════════════
# 1. EMAIL CANONICALISATION — the identity the ban is matched on
# ════════════════════════════════════════════════════════════════════════════════

def test_gmail_plus_tags_fold_to_one_identity():
    """The owner's own example: two plus-tags, one mailbox."""
    a = ei.canonical_email("ct8ball+account1@gmail.com")
    b = ei.canonical_email("ct8ball+account2@gmail.com")
    assert a == b == "ct8ball@gmail.com"
    assert ei.same_identity("ct8ball+account1@gmail.com", "ct8ball+account2@gmail.com")


def test_gmail_dots_and_googlemail_fold_too():
    """Gmail ignores dots, and googlemail.com is the same namespace — both documented."""
    variants = ["ct8ball@gmail.com", "ct.8ball@gmail.com", "c.t.8.b.a.l.l@gmail.com",
                "CT8Ball@Gmail.COM", "ct8ball@googlemail.com",
                "ct.8ball+throwaway@googlemail.com"]
    canon = {ei.canonical_email(v) for v in variants}
    assert canon == {"ct8ball@gmail.com"}


def test_non_gmail_dots_are_NOT_collapsed():
    """THE FALSE-POSITIVE GUARD.

    john.smith@ and johnsmith@ at a corporate domain are plausibly two DIFFERENT employees.
    Folding them would refuse signup to an innocent person on the strength of a Gmail-specific
    quirk, and they would never learn why. Dot-stripping stays confined to Gmail.
    """
    a = ei.canonical_email("john.smith@company.com")
    b = ei.canonical_email("johnsmith@company.com")
    assert a == "john.smith@company.com"
    assert b == "johnsmith@company.com"
    assert a != b
    assert not ei.same_identity("john.smith@company.com", "johnsmith@company.com")


def test_non_gmail_dots_survive_a_ban(audit_spy, monkeypatch):
    """The same guard, at the layer that matters: a ban on john.smith@ must not catch
    johnsmith@. Asserted through the real matcher, not just the canonicaliser."""
    db = FakeDB(rows=[])          # no ban row comes back → not banned
    monkeypatch.setattr(mod, "db", db)
    assert mod.is_banned("johnsmith@company.com") is False
    # and the value it looked up is the DOTTED form, not a collapsed one
    assert db.statements[0][1] == ("johnsmith@company.com",)


def test_plus_tag_stripped_on_other_domains_but_dots_kept():
    assert ei.canonical_email("john.smith+newsletter@company.com") == "john.smith@company.com"


def test_plus_stripping_is_overridable(monkeypatch):
    """The one broad assumption has a documented escape hatch."""
    monkeypatch.setenv("SAAS_EMAIL_NO_PLUS_DOMAINS", "weird.example")
    assert ei.canonical_email("a+b@weird.example") == "a+b@weird.example"
    assert ei.canonical_email("a+b@other.example") == "a@other.example"
    # Gmail is NEVER affected by the override — plus-tagging there is documented, not assumed.
    assert ei.canonical_email("a+b@gmail.com") == "a@gmail.com"


def test_quoted_local_part_is_left_alone():
    """Inside quotes '.' and '+' are literal data, not syntax — rewriting them would fold a
    DIFFERENT mailbox into this one."""
    assert ei.canonical_email('"weird.name+tag"@example.com') == '"weird.name+tag"@example.com'


@pytest.mark.parametrize("bad", [None, "", "   ", "no-at-sign", "@nodomain.com", "local@",
                                 "a@b", "+tag@gmail.com", "x" * 250 + "@example.com"])
def test_unusable_addresses_canonicalise_to_none(bad):
    """A canonical form of None means "not an identity we hold". Notably '+tag@gmail.com'
    strips to an EMPTY local part — returning '' there would collide every such address into
    one identity and ban strangers wholesale."""
    assert ei.canonical_email(bad) is None


def test_canonicalisation_is_idempotent():
    for addr in ["ct.8ball+x@googlemail.com", "john.smith+y@company.com", "A@B.co"]:
        once = ei.canonical_email(addr)
        assert ei.canonical_email(once) == once


# ════════════════════════════════════════════════════════════════════════════════
# 2. SPAM FILTER — refused at SAVE time; fails OPEN at signup time
# ════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pattern", [
    "(a+)+$",                 # the classic catastrophic-backtracking pattern
    "^spam.*",                # anchors + star
    "a{1,9999}",              # bounded repetition
    "[a-z]+@evil",            # character class
    "(x|y)*z",                # alternation + star
    "\\d+",                   # escape
])
def test_regex_patterns_are_rejected_at_save_time(pattern):
    """A pathological pattern must never reach the signup path. Python has no regex timeout and
    a running match is not interruptible, so the ONLY safe place to say no is here."""
    out = sf.validate_rule(sf.KIND_EMAIL_CONTAINS, pattern)
    assert out["ok"] is False
    assert out["status_code"] == 400
    assert "regular expression" in out["reason"] or "not allowed" in out["reason"]


def test_over_long_pattern_is_rejected_at_save_time():
    out = sf.validate_rule(sf.KIND_EMAIL_CONTAINS, "a" * (sf.MAX_PATTERN_LENGTH + 1))
    assert out["ok"] is False and out["status_code"] == 400
    assert "too long" in out["reason"]


def test_unknown_kind_and_action_are_rejected():
    assert sf.validate_rule("regex", "anything")["status_code"] == 400
    assert sf.validate_rule(sf.KIND_EMAIL_CONTAINS, "spam", "quarantine")["status_code"] == 400


def test_domain_rule_requires_a_dotted_domain():
    assert sf.validate_rule(sf.KIND_DOMAIN_SUFFIX, "tk")["ok"] is False
    ok = sf.validate_rule(sf.KIND_DOMAIN_SUFFIX, ".co.tk")
    assert ok["ok"] is True and ok["pattern"] == "co.tk"   # leading dot normalised away


@pytest.mark.parametrize("kind,pattern,hit,miss", [
    (sf.KIND_DOMAIN_EQUALS, "spam.tk", "a@spam.tk", "a@notspam.tk"),
    (sf.KIND_DOMAIN_SUFFIX, "spam.tk", "a@mail.spam.tk", "a@myspam.tk"),
    (sf.KIND_LOCAL_PREFIX, "noreply", "noreply-bot@x.com", "reply-noreply@x.com"),
    (sf.KIND_LOCAL_CONTAINS, "casino", "big.casino.win@x.com", "casio@x.com"),
    (sf.KIND_EMAIL_CONTAINS, "viagra", "a@viagra-shop.com", "a@example.com"),
])
def test_each_rule_kind_matches_what_it_claims(kind, pattern, hit, miss):
    rules = [{"id": "r1", "kind": kind, "pattern": pattern, "action": sf.ACTION_BLOCK}]
    assert sf.evaluate(hit, rules=rules).allowed is False
    assert sf.evaluate(miss, rules=rules).allowed is True


def test_domain_suffix_respects_the_label_boundary():
    """'spam.tk' must not match 'myspam.tk' — a suffix rule that was really a substring rule
    would quietly block real customers."""
    rules = [{"id": "r1", "kind": sf.KIND_DOMAIN_SUFFIX, "pattern": "spam.tk",
              "action": sf.ACTION_BLOCK}]
    assert sf.evaluate("a@myspam.tk", rules=rules).allowed is True
    assert sf.evaluate("a@spam.tk", rules=rules).allowed is False
    assert sf.evaluate("a@x.spam.tk", rules=rules).allowed is False


def test_allow_rule_overrides_a_too_broad_block():
    """The escape hatch: rescue a real customer by ADDING a rule, not by unpicking one."""
    rules = [
        {"id": "b", "kind": sf.KIND_DOMAIN_SUFFIX, "pattern": "co.tk", "action": sf.ACTION_BLOCK},
        {"id": "a", "kind": sf.KIND_DOMAIN_EQUALS, "pattern": "good.co.tk",
         "action": sf.ACTION_ALLOW},
    ]
    assert sf.evaluate("someone@good.co.tk", rules=rules).allowed is True
    assert sf.evaluate("someone@bad.co.tk", rules=rules).allowed is False


def test_filter_fails_OPEN_when_the_rule_store_is_unreadable(monkeypatch):
    """A broken spam filter must NOT take signup down. This is the asymmetry the whole design
    turns on."""
    monkeypatch.setattr(sf, "db", FakeDB(fail=True))
    sf.invalidate()
    assert sf.active_rules() is None                       # read failed
    assert sf.evaluate("anyone@example.com").allowed is True
    assert "OPEN" in sf.evaluate("anyone@example.com").reason


def test_filter_fails_OPEN_when_a_rule_blows_up_mid_match(monkeypatch):
    """One bad rule is skipped, never fatal — and never a reason to refuse a real applicant."""
    class Exploding(dict):
        def get(self, k, d=None):
            if k == "action":
                raise RuntimeError("boom")
            return super().get(k, d)

    rules = [Exploding(id="bad", kind=sf.KIND_EMAIL_CONTAINS, pattern="x",
                       action=sf.ACTION_BLOCK)]
    assert sf.evaluate("anyone@example.com", rules=rules).allowed is True


def test_malformed_stored_rows_are_skipped_not_fatal(monkeypatch):
    """A hand-edited or pre-constraint row must not poison the whole rule set."""
    db = FakeDB(rows=[
        ("r1", "regex", "(a+)+", "block"),          # kind not in the vocabulary → skipped
        ("r2", sf.KIND_EMAIL_CONTAINS, "spam", "nuke"),   # bad action → skipped
        ("r3", sf.KIND_EMAIL_CONTAINS, "casino", "block"),  # good
    ])
    monkeypatch.setattr(sf, "db", db)
    sf.invalidate()
    loaded = sf.active_rules(fresh=True)
    assert [r["id"] for r in loaded] == ["r3"]


def test_over_long_address_is_not_this_filters_decision():
    rules = [{"id": "r", "kind": sf.KIND_EMAIL_CONTAINS, "pattern": "a",
              "action": sf.ACTION_BLOCK}]
    assert sf.evaluate("a" * 300 + "@example.com", rules=rules).allowed is True


# ════════════════════════════════════════════════════════════════════════════════
# 3. THE THREE VERBS — each changes the state it claims to, and is audited
# ════════════════════════════════════════════════════════════════════════════════

def test_block_suspends_the_login_and_is_audited(monkeypatch, audit_spy):
    db = FakeDB(rows=[_subscriber_row(), _subscriber_row(status="suspended")],
                rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    out = mod.block_subscriber(IND_ID, reason="spamming", actor="root-operator")
    assert out["ok"] and out["status_code"] == 200

    sql = db.sql()
    assert "SET status = %s" in sql
    assert "blocked_at = NOW()" in sql
    # BLOCK reuses the EXISTING 'suspended' value — no new status, no parallel state machine.
    assert mod.STATUS_SUSPENDED in [p for st, params in db.statements
                                    for p in (params or ()) if isinstance(p, str)]
    # and it refuses to demote a stronger state
    assert "deleted_at IS NULL AND banned_at IS NULL" in sql

    action, kw = audit_spy[-1]
    assert action == "moderation.block"
    assert kw["actor_type"] == "operator" and kw["actor_id"] == "root-operator"
    assert kw["target_type"] == "individual_user" and kw["target_id"] == IND_ID
    assert "spamming" in kw["detail"]


def test_delete_is_a_soft_delete_that_keeps_the_address_claimed(monkeypatch, audit_spy):
    row = _subscriber_row()
    db = FakeDB(rows=[row, row, row], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    out = mod.delete_subscriber(IND_ID, confirm_email="ct8ball@gmail.com",
                                reason="abuse", actor="root-operator")
    assert out["ok"]
    sql = db.sql()
    assert "deleted_at = NOW()" in sql
    # It is a soft delete: the row is UPDATEd, never DELETEd. That is what keeps the UNIQUE
    # email claimed and what makes a "Deleted (historical)" tab possible at all.
    assert "DELETE FROM public.individual_users" not in sql
    assert audit_spy[-1][0] == "moderation.delete"
    assert audit_spy[-1][1]["actor_id"] == "root-operator"


def test_delete_requires_the_typed_address_confirm(monkeypatch, audit_spy):
    db = FakeDB(rows=[_subscriber_row()], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    out = mod.delete_subscriber(IND_ID, confirm_email="somethingelse@gmail.com",
                                actor="root-operator")
    assert out["ok"] is False and out["status_code"] == 400
    # Nothing was written and nothing was audited as a success.
    assert "UPDATE" not in db.sql()
    assert not [a for a, _ in audit_spy if a == "moderation.delete"]


def test_ban_writes_the_enforcement_record_BEFORE_stamping_the_row(monkeypatch, audit_spy):
    """Order is the safety property: a crash between the two leaves an ENFORCED ban with an
    un-stamped row (the door still holds), never a roster claiming a ban nobody enforces."""
    row = _subscriber_row()
    db = FakeDB(rows=[row, ("ban-1",), row, row], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    out = mod.ban_subscriber(IND_ID, confirm_email="ct8ball@gmail.com",
                             reason="fraud", actor="root-operator")
    assert out["ok"], out

    stmts = [s for s, _ in db.statements]
    ban_ix = next(i for i, s in enumerate(stmts) if "INSERT INTO public.identity_bans" in s)
    row_ix = next(i for i, s in enumerate(stmts) if "banned_at = NOW()" in s)
    assert ban_ix < row_ix

    # the ban is stored on the CANONICAL identity, not the raw string
    ban_params = db.statements[ban_ix][1]
    assert ban_params[0] == "ct8ball@gmail.com"

    actions = [a for a, _ in audit_spy]
    assert "moderation.ban" in actions
    ban_audit = [kw for a, kw in audit_spy if a == "moderation.ban"][-1]
    assert ban_audit["actor_type"] == "operator" and ban_audit["actor_id"] == "root-operator"


def test_ban_also_disables_and_soft_deletes_the_login(monkeypatch, audit_spy):
    row = _subscriber_row()
    db = FakeDB(rows=[row, ("ban-1",), row, row], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    mod.ban_subscriber(IND_ID, confirm_email="ct8ball@gmail.com", actor="op")
    sql = db.sql()
    assert "banned_at = NOW()" in sql
    assert "deleted_at = COALESCE(deleted_at, NOW())" in sql
    assert mod.STATUS_DISABLED in [p for _s, params in db.statements
                                   for p in (params or ()) if isinstance(p, str)]


def test_restore_refuses_while_the_row_is_still_banned(monkeypatch, audit_spy):
    """Unban and restore are two decisions on purpose — nobody is resurrected by accident."""
    # migration 304: restore now reads the target itself and runs the _address_reclaimed
    # guard first; the guard has its own tests in test_saas_deleted_email_posture.py.
    monkeypatch.setattr(mod, "_address_reclaimed", lambda *a, **k: False)
    db = FakeDB(rows=[_subscriber_row(deleted="x", banned="x")] * 2, rowcount=0)
    monkeypatch.setattr(mod, "db", db)
    out = mod.restore_subscriber(IND_ID, actor="op")
    assert out["ok"] is False and out["status_code"] == 409
    assert "banned" in out["reason"]
    # the guard lives in the WHERE clause, so a racing operator gets the same answer
    assert "banned_at IS NULL" in db.sql()


def test_restore_returns_a_previously_blocked_row_to_suspended_not_active(monkeypatch,
                                                                         audit_spy):
    """Restoring undoes the DELETE, not the BLOCK."""
    monkeypatch.setattr(mod, "_address_reclaimed", lambda *a, **k: False)
    row = _subscriber_row(blocked="x", deleted="x")
    db = FakeDB(rows=[row, row, row], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    mod.restore_subscriber(IND_ID, actor="op")
    assert "CASE WHEN blocked_at IS NOT NULL THEN %s ELSE %s END" in db.sql()


def test_a_verb_that_changes_nothing_is_a_409_not_a_silent_success(monkeypatch, audit_spy):
    row = _subscriber_row(blocked="already")
    db = FakeDB(rows=[row, row], rowcount=0)
    monkeypatch.setattr(mod, "db", db)
    out = mod.block_subscriber(IND_ID, actor="op")
    assert out["ok"] is False and out["status_code"] == 409
    assert not [a for a, _ in audit_spy if a == "moderation.block"]


def test_unknown_verb_is_a_400_never_a_no_op():
    out = mod.moderate(IND_ID, "obliterate", actor="op")
    assert out["ok"] is False and out["status_code"] == 400


# ════════════════════════════════════════════════════════════════════════════════
# 4. BAN MATCHING + fail-CLOSED
# ════════════════════════════════════════════════════════════════════════════════

def test_is_banned_matches_a_plus_tagged_gmail_against_the_bare_ban(monkeypatch):
    db = FakeDB(rows=[("ban-1", "ct8ball@gmail.com", "ct8ball@gmail.com", "fraud", None, "op")])
    monkeypatch.setattr(mod, "db", db)
    assert mod.is_banned("ct8ball+brand-new-account@gmail.com") is True
    # the lookup value is the folded identity, which is what makes the evasion fail
    assert db.statements[0][1] == ("ct8ball@gmail.com",)


def test_is_banned_fails_CLOSED_when_the_control_plane_is_unreadable(monkeypatch):
    """Contrast with the spam filter, which fails OPEN: a ban is a decision a human made about
    a specific person and must not evaporate on a blip."""
    monkeypatch.setattr(mod, "db", FakeDB(fail=True))
    assert mod.is_banned("anyone@example.com") is True
    assert mod.ban_record("anyone@example.com")["degraded"] is True


def test_is_banned_is_false_for_an_unusable_address(monkeypatch):
    monkeypatch.setattr(mod, "db", FakeDB(fail=True))   # would fail closed if it got that far
    assert mod.is_banned("not-an-address") is False


def test_ban_identity_is_idempotent(monkeypatch, audit_spy):
    """ON CONFLICT on the partial unique index turns a re-ban into a no-op, and the caller gets
    a 200: the operator's intent ("this person is banned") is satisfied either way."""
    db = FakeDB(rows=[None, ("ban-1", "a@b.com", "a@b.com", "why", None, "op")])
    monkeypatch.setattr(mod, "db", db)
    out = mod.ban_identity("a@b.com", actor="op")
    assert out["ok"] and out["status_code"] == 200
    assert "already banned" in out["reason"]


def test_lift_ban_keeps_history_and_clears_the_roster_stamps(monkeypatch, audit_spy):
    db = FakeDB(rows=[("ban-1",)], rowcount=1)
    monkeypatch.setattr(mod, "db", db)
    out = mod.lift_ban("ct8ball@gmail.com", reason="appealed", actor="op")
    assert out["ok"]
    sql = db.sql()
    assert "lifted_at = NOW()" in sql
    assert "DELETE FROM public.identity_bans" not in sql     # history is KEPT
    assert "SET banned_at = NULL" in sql                     # roster and ban list agree
    assert audit_spy[-1][0] == "moderation.unban"


# ════════════════════════════════════════════════════════════════════════════════
# 5. THE HISTORICAL TABS — the four buckets are mutually exclusive
# ════════════════════════════════════════════════════════════════════════════════

def test_every_roster_state_has_a_predicate():
    assert set(mod.ROSTER_STATES) == set(mod._STATE_PREDICATE)


@pytest.mark.parametrize("state,must_contain", [
    ("active", "deleted_at IS NULL AND banned_at IS NULL AND blocked_at IS NULL"),
    ("blocked", "blocked_at IS NOT NULL AND banned_at IS NULL AND deleted_at IS NULL"),
    ("banned", "banned_at IS NOT NULL"),
    ("deleted", "deleted_at IS NOT NULL AND banned_at IS NULL"),
])
def test_each_tab_queries_its_own_bucket(monkeypatch, state, must_contain):
    db = FakeDB(rows=[])
    monkeypatch.setattr(mod, "db", db)
    mod.list_subscribers(state)
    assert must_contain in db.sql()


def test_the_buckets_do_not_overlap():
    """A banned-and-deleted row must appear ONCE, or the tab badges lie. Proven on the
    predicates themselves rather than on a live table."""
    import re as _re

    def holds(pred, blocked, deleted, banned):
        expr = pred
        for col, val in (("blocked_at", blocked), ("deleted_at", deleted),
                         ("banned_at", banned)):
            expr = _re.sub(rf"{col} IS NOT NULL", "True" if val else "False", expr)
            expr = _re.sub(rf"{col} IS NULL", "False" if val else "True", expr)
        return eval(expr.replace("AND", "and").replace("OR", "or"))  # noqa: S307 - test-local

    for blocked in (False, True):
        for deleted in (False, True):
            for banned in (False, True):
                matched = [s for s in mod.ROSTER_STATES
                           if holds(mod._STATE_PREDICATE[s], blocked, deleted, banned)]
                assert len(matched) == 1, (blocked, deleted, banned, matched)


def test_deleted_tab_shows_the_soft_deleted_row(monkeypatch):
    db = FakeDB(rows=[_subscriber_row(status="disabled", deleted="2026-08-07T00:00:00+00:00")])
    monkeypatch.setattr(mod, "db", db)
    rows = mod.list_subscribers("deleted")
    assert len(rows) == 1
    assert rows[0]["email"] == "ct8ball@gmail.com"
    assert rows[0]["deleted_at"] is not None
    # the raw address is never overwritten; the canonical form travels beside it
    assert rows[0]["email_canonical"] == "ct8ball@gmail.com"


def test_an_unknown_state_falls_back_to_active_rather_than_interpolating_it(monkeypatch):
    db = FakeDB(rows=[])
    monkeypatch.setattr(mod, "db", db)
    mod.list_subscribers("'; DROP TABLE public.accounts; --")
    assert "DROP TABLE" not in db.sql()
    assert mod._STATE_PREDICATE["active"] in db.sql()


def test_roster_reads_fail_safe_to_empty(monkeypatch):
    monkeypatch.setattr(mod, "db", FakeDB(fail=True))
    assert mod.list_subscribers("banned") == []
    assert mod.roster_counts() == {s: 0 for s in mod.ROSTER_STATES}
    assert mod.list_bans() == []


# ════════════════════════════════════════════════════════════════════════════════
# 6. OPERATOR-ONLY ENFORCEMENT — the server is the boundary, not the hidden button
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saas_on(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_OPERATOR_KEY", OP_KEY)
    monkeypatch.setenv("SAAS_SESSION_SECRET", "account-moderation-secret-0123456789")
    yield


@pytest.fixture
def client(saas_on):
    app = FastAPI()
    app.include_router(saas_admin.router)
    return TestClient(app)


def _op():
    return {"Authorization": f"Bearer {OP_KEY}"}


MODERATION_ROUTES = [
    ("GET", "/admin/moderation/subscribers", None),
    ("POST", f"/admin/moderation/subscribers/{IND_ID}/moderate", {"action": "block"}),
    ("GET", "/admin/moderation/bans", None),
    ("POST", "/admin/moderation/bans", {"email": "a@b.com"}),
    ("POST", "/admin/moderation/bans/lift", {"email": "a@b.com"}),
    ("GET", "/admin/signup-filter/rules", None),
    ("POST", "/admin/signup-filter/rules",
     {"kind": "domain_equals", "pattern": "spam.tk", "action": "block"}),
    ("PUT", "/admin/signup-filter/rules/r1", {"enabled": False}),
    ("DELETE", "/admin/signup-filter/rules/r1", None),
    ("POST", "/admin/signup-filter/test", {"email": "a@b.com"}),
]


@pytest.mark.parametrize("method,path,body", MODERATION_ROUTES)
def test_every_moderation_route_is_operator_only(client, method, path, body):
    """No credential → 403. Hiding a button in the console is an affordance; THIS is the
    boundary."""
    r = client.request(method, path, json=body)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", MODERATION_ROUTES)
def test_every_moderation_route_404s_when_saas_is_off(monkeypatch, method, path, body):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    app = FastAPI()
    app.include_router(saas_admin.router)
    c = TestClient(app)
    r = c.request(method, path, json=body)
    assert r.status_code == 404


def test_operator_can_moderate_and_the_route_passes_the_operator_identity(client, monkeypatch):
    seen = {}

    def _moderate(individual_id, action, **kw):
        seen.update({"individual_id": individual_id, "action": action, **kw})
        return {"ok": True, "status_code": 200, "subscriber": {"email": "a@b.com"},
                "reason": "ok"}

    monkeypatch.setattr(saas_admin._moderation, "moderate", _moderate)
    r = client.post(f"/admin/moderation/subscribers/{IND_ID}/moderate",
                    json={"action": "ban", "confirm_email": "a@b.com", "reason": "fraud"},
                    headers=_op())
    assert r.status_code == 200, r.text
    assert seen["action"] == "ban"
    assert seen["confirm_email"] == "a@b.com"
    # the break-glass key is attributed as 'operator-key', never as an anonymous actor
    assert seen["actor"] == "operator-key"


def test_roster_route_rejects_an_unknown_state(client):
    r = client.get("/admin/moderation/subscribers?state=everything", headers=_op())
    assert r.status_code == 400


def test_rule_save_refuses_a_regex_through_the_route(client, monkeypatch):
    """The refusal is a 400 at SAVE time, with a message that names the problem — so nothing
    pathological can ever be sitting in the table when a signup arrives."""
    r = client.post("/admin/signup-filter/rules",
                    json={"kind": "email_contains", "pattern": "(a+)+$", "action": "block"},
                    headers=_op())
    assert r.status_code == 400
    assert "regular expression" in r.json()["detail"]


# ════════════════════════════════════════════════════════════════════════════════
# 7. BOTH SIGNUP DOORS — a ban enforced on only one door is not a ban
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def signup_env(monkeypatch):
    monkeypatch.setenv("SAAS_MODE", "1")
    monkeypatch.setenv("SAAS_SESSION_SECRET", "account-moderation-secret-0123456789")
    yield


def _tenant_client():
    from saas import tenant_signup as ts
    app = FastAPI()
    app.include_router(ts.router)
    return TestClient(app), ts


def _individual_client():
    from saas import user_console as uc
    app = FastAPI()
    app.include_router(uc.router)
    return TestClient(app), uc


def _neutral_guards(monkeypatch, m):
    """Everything upstream of the ban/filter checks PASSES, so a rejection can only be theirs."""
    monkeypatch.setattr(m._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(m._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(m._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(m._audit, "record", lambda *a, **kw: True)


def _tenant_downstream_must_not_run(monkeypatch, ts, tripped):
    monkeypatch.setattr(ts._auth, "get_user_by_username",
                        lambda u: tripped.append("dup_check") or None)
    monkeypatch.setattr(ts, "pending_request_for_email",
                        lambda e: tripped.append("dup_check") or False)
    monkeypatch.setattr(ts._signup_ledger, "decide",
                        lambda e: tripped.append("ladder") or ts._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(ts._accounts, "create_account",
                        lambda *a, **kw: tripped.append("create_account") or None)


def _individual_downstream_must_not_run(monkeypatch, uc, tripped):
    monkeypatch.setattr(uc, "_email_taken", lambda e: tripped.append("dup_check") or False)
    monkeypatch.setattr(uc._signup_ledger, "decide",
                        lambda e: tripped.append("ladder") or uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account",
                        lambda: tripped.append("provision") or None)


TENANT_BODY = {"company": "Acme", "email": "ct8ball+fresh@gmail.com",
               "password": "correct horse battery", "display_name": "C"}
INDIVIDUAL_BODY = {"email": "ct8ball+fresh@gmail.com", "password": "correct horse battery",
                   "display_name": "C"}


def test_banned_identity_is_refused_at_the_TENANT_signup_door(signup_env, monkeypatch):
    c, ts = _tenant_client()
    _neutral_guards(monkeypatch, ts)
    monkeypatch.setattr(ts._signup_filter, "evaluate",
                        lambda e, **kw: ts._signup_filter.Decision(True))
    monkeypatch.setattr(ts._moderation, "signup_ban_check",
                        lambda e: {"banned": True, "degraded": False})
    tripped = []
    _tenant_downstream_must_not_run(monkeypatch, ts, tripped)

    r = c.post("/signup/team", json=TENANT_BODY)
    assert r.status_code == 400
    assert r.json()["detail"] == ts._signup_filter.REJECTION_MESSAGE
    assert tripped == [], f"work happened past the ban check: {tripped}"


def test_banned_identity_is_refused_at_the_INDIVIDUAL_signup_door(signup_env, monkeypatch):
    c, uc = _individual_client()
    _neutral_guards(monkeypatch, uc)
    monkeypatch.setattr(uc._name_guard, "check_display_name",
                        lambda n: uc._name_guard.Result(True, "", ""))
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e, **kw: uc._signup_filter.Decision(True))
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"banned": True, "degraded": False})
    tripped = []
    _individual_downstream_must_not_run(monkeypatch, uc, tripped)

    r = c.post("/me/signup", json=INDIVIDUAL_BODY)
    assert r.status_code == 400
    assert r.json()["detail"] == uc._signup_filter.REJECTION_MESSAGE
    assert tripped == [], f"work happened past the ban check: {tripped}"


def test_the_ban_check_sees_the_plus_tagged_address_at_both_doors(signup_env, monkeypatch):
    """The evasion the owner named: sign up again as ct8ball+account2@gmail.com. The doors hand
    the RAW address to is_banned, which folds it — so both doors refuse it."""
    seen = []
    for build, filt in ((_tenant_client, "tenant"), (_individual_client, "individual")):
        c, m = build()
        _neutral_guards(monkeypatch, m)
        if filt == "individual":
            monkeypatch.setattr(m._name_guard, "check_display_name",
                                lambda n: m._name_guard.Result(True, "", ""))
        monkeypatch.setattr(m._signup_filter, "evaluate",
                            lambda e, **kw: m._signup_filter.Decision(True))
        monkeypatch.setattr(m._moderation, "signup_ban_check",
                            lambda e: (seen.append(e),
                                       {"banned": True, "degraded": False})[1])
        body = TENANT_BODY if filt == "tenant" else INDIVIDUAL_BODY
        c.post("/signup/team" if filt == "tenant" else "/me/signup", json=body)
    assert seen == ["ct8ball+fresh@gmail.com", "ct8ball+fresh@gmail.com"]
    assert {ei.canonical_email(s) for s in seen} == {"ct8ball@gmail.com"}


def test_spam_filter_blocks_at_both_doors(signup_env, monkeypatch):
    blocked = sf.Decision(False, rule_id="r1", kind=sf.KIND_DOMAIN_SUFFIX, pattern="spam.tk",
                          action=sf.ACTION_BLOCK)
    for build, path, body in ((_tenant_client, "/signup/team", TENANT_BODY),
                              (_individual_client, "/me/signup", INDIVIDUAL_BODY)):
        c, m = build()
        _neutral_guards(monkeypatch, m)
        if path == "/me/signup":
            monkeypatch.setattr(m._name_guard, "check_display_name",
                                lambda n: m._name_guard.Result(True, "", ""))
        monkeypatch.setattr(m._signup_filter, "evaluate", lambda e, **kw: blocked)
        monkeypatch.setattr(m._moderation, "signup_ban_check",
                            lambda e: {"banned": False, "degraded": False})
        r = c.post(path, json=body)
        assert r.status_code == 400, path
        assert r.json()["detail"] == sf.REJECTION_MESSAGE


def test_an_unreadable_ban_list_refuses_HONESTLY_not_as_a_ban(signup_env, monkeypatch):
    """Still fail-closed (no signup happens) — but a 503 "try again", never "you may not sign
    up". Telling an innocent applicant they are barred because our database hiccuped is a lie
    they never get the chance to correct."""
    for build, path, body in ((_tenant_client, "/signup/team", TENANT_BODY),
                              (_individual_client, "/me/signup", INDIVIDUAL_BODY)):
        c, m = build()
        _neutral_guards(monkeypatch, m)
        if path == "/me/signup":
            monkeypatch.setattr(m._name_guard, "check_display_name",
                                lambda n: m._name_guard.Result(True, "", ""))
        monkeypatch.setattr(m._signup_filter, "evaluate",
                            lambda e, **kw: m._signup_filter.Decision(True))
        monkeypatch.setattr(m._moderation, "db", FakeDB(fail=True))
        tripped = []
        if path == "/signup/team":
            _tenant_downstream_must_not_run(monkeypatch, m, tripped)
        else:
            _individual_downstream_must_not_run(monkeypatch, m, tripped)
        r = c.post(path, json=body)
        assert r.status_code == 503, (path, r.text)
        assert r.json()["detail"] != sf.REJECTION_MESSAGE
        assert tripped == [], f"{path}: work happened past a fail-closed ban check"


def test_signup_ban_check_separates_banned_from_unreadable(monkeypatch):
    monkeypatch.setattr(mod, "db", FakeDB(fail=True))
    assert mod.signup_ban_check("a@b.com") == {"banned": False, "degraded": True}
    monkeypatch.setattr(mod, "db", FakeDB(rows=[]))
    assert mod.signup_ban_check("a@b.com") == {"banned": False, "degraded": False}
    monkeypatch.setattr(mod, "db", FakeDB(rows=[("id", "a@b.com", "a@b.com", None, None, "op")]))
    assert mod.signup_ban_check("a@b.com") == {"banned": True, "degraded": False}


def test_the_two_rejections_are_indistinguishable():
    """A banned person and a filtered domain get the SAME message. If they differed, an evader
    could probe the rule set one address at a time."""
    assert sf.REJECTION_MESSAGE and "ban" not in sf.REJECTION_MESSAGE.lower()


def test_a_broken_filter_does_NOT_take_signup_down(signup_env, monkeypatch):
    """The rule store is unreadable at request time. Both doors must sail past it."""
    for build, path, body in ((_tenant_client, "/signup/team", TENANT_BODY),
                              (_individual_client, "/me/signup", INDIVIDUAL_BODY)):
        c, m = build()
        _neutral_guards(monkeypatch, m)
        if path == "/me/signup":
            monkeypatch.setattr(m._name_guard, "check_display_name",
                                lambda n: m._name_guard.Result(True, "", ""))
        monkeypatch.setattr(m._signup_filter, "db", FakeDB(fail=True))
        m._signup_filter.invalidate()
        monkeypatch.setattr(m._moderation, "signup_ban_check",
                            lambda e: {"banned": False, "degraded": False})
        reached = []
        if path == "/signup/team":
            _tenant_downstream_must_not_run(monkeypatch, m, reached)
        else:
            _individual_downstream_must_not_run(monkeypatch, m, reached)
        r = c.post(path, json=body)
        # It got PAST the filter — the downstream stubs were reached, and the response is not
        # the filter's rejection.
        assert reached, f"{path}: the broken filter blocked the request"
        assert not (r.status_code == 400
                    and r.json().get("detail") == sf.REJECTION_MESSAGE), path


def test_signup_stores_the_canonical_form_beside_the_raw_address():
    """The INSERT must carry BOTH columns — the raw address is never overwritten."""
    import inspect
    from saas import user_console as uc
    src = inspect.getsource(uc._create_individual)
    assert "email_canonical" in src
    assert "_email_identity.canonical_email(email)" in src
