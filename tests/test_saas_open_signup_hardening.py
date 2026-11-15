"""OPEN-SIGNUP HARDENING — the two controls that carry real weight once approval is OFF.

Both of these were "nice to have" while ``platform_settings.require_signup_approval`` was ON and
a human reviewed every application. With the valve OFF they are the whole gate, and both were
weak. Follows the tests/test_saas_tenant_self_signup.py conventions: every control-plane
collaborator is stubbed, so this file is green without a control plane, a Redis or a relay.

── GAP 1: the 3-strikes ledger was walkable with a plus-tag (migration 232) ──
``signup_ledger.normalize_email`` only lowercased, so a@gmail.com / a+1@gmail.com / a+2@gmail.com
were THREE identities to a THREE-strikes ladder. It now keys on
``email_identity.canonical_email`` — the same identity the ban list (228) and signup uniqueness
(231) already use. What is proven here:

  * one mailbox, one counter: '+tag' and Gmail-dotted variants share a ledger key, and three
    attempts across three different variants reach REJECT.
  * THE FALSE-POSITIVE GUARD: a NON-Gmail dotted pair stays SEPARATE. john.smith@corp.com and
    johnsmith@corp.com are plausibly two different employees; folding them would hang a
    stranger's strikes on somebody who did nothing. This is the test that must never be
    "fixed" by making canonicalisation more aggressive.
  * EXISTING HISTORY, which is the part a previous agent stalled on. The key is
    sha256(salt||norm) and only ``norm`` changed, so every untagged address keeps a BIT-IDENTICAL
    key and loses nothing. For the tagged minority the reader consults the caller's legacy
    lowercase-only key too, so a pre-232 row still counts against the exact address it recorded.
    Nobody's strike count goes DOWN.

── GAP 2: the honeypot told the bot it was caught ──
A trip returned "pending_approval" regardless of mode. With approval OFF a genuine signup returns
200 + "authenticated" + a session cookie, so one request identified the trap field; on the
individual door the trip was also a bare 200 where the genuine hold is a 202 with a
retention_notice, i.e. distinguishable even with approval ON. What is proven here, on BOTH doors
and in BOTH modes: the status code, the JSON key set, the value shapes and the Set-Cookie header
of a trip match a genuine outcome — and nothing is provisioned.

Run: python3 -m pytest tests/test_saas_open_signup_hardening.py -q   (tests/ is gitignored →
git add -f).
"""

import os

os.environ["SAAS_MODE"] = "1"
os.environ.setdefault("SAAS_SESSION_SECRET", "unit-test-signing-secret-0123456789")
os.environ.setdefault("MCP_API_KEY", "unit-test-key")

import re  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

from saas import email_identity as ei  # noqa: E402
from saas import signup_decoy as decoy  # noqa: E402
from saas import signup_ledger as sl  # noqa: E402
from saas import tenant_signup as ts  # noqa: E402
from saas import user_console as uc  # noqa: E402

ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
OWNER_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
INDIVIDUAL_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ══════════════════════════════════════════════════════════════════════════════════
# PART 1 — THE LEDGER KEY (migration 232)
#
# A fake control plane small enough to read: signup_ledger issues exactly three statements, so
# the cursor dispatches on which one it was handed rather than parsing SQL. The POINT of using a
# fake rather than stubbing get_attempt_count is that the two-key READ and the canonical WRITE
# are what changed — stubbing them away would prove nothing.
# ══════════════════════════════════════════════════════════════════════════════════

class FakeLedger:
    """``email_hash -> attempt_count`` with signup_ledger's three statements implemented."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.written_keys = []

    # -- context-manager plumbing --------------------------------------------------
    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    # -- the three statements ------------------------------------------------------
    def execute(self, sql, params=()):
        if "SUM(attempt_count)" in sql:
            # WHERE email_hash IN (%s, %s) — set() so a caller whose canonical and legacy keys
            # agree counts its single row ONCE, exactly as the SQL predicate does.
            keys = set(params)
            self._result = (sum(self.rows.get(k, 0) for k in keys),)
        elif "INSERT INTO public.signup_ledger" in sql:
            key = params[0]
            self.written_keys.append(key)
            if "attempt_count + 1" in sql:                      # record_attempt
                self.rows[key] = self.rows.get(key, 0) + 1
            else:                                               # record_purge — no bump
                self.rows.setdefault(key, 1)
            self._result = None
        else:  # pragma: no cover - activity_summary is not what this file is about
            self._result = (0,)

    def fetchone(self):
        return self._result

    def fetchall(self):  # pragma: no cover
        return []


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeLedger()
    monkeypatch.setattr(sl.db, "connection", fake.connection)
    monkeypatch.setenv("SAAS_SIGNUP_LEDGER_SALT", "unit-test-ledger-salt")
    return fake


# ── one mailbox, one key ─────────────────────────────────────────────────────────

def test_gmail_tag_and_dot_variants_share_one_ledger_key(ledger):
    """The bypass this closes: three ways to write one Gmail mailbox, one strike counter."""
    base = sl.hash_email("chrisb@gmail.com")
    assert sl.hash_email("chrisb+1@gmail.com") == base
    assert sl.hash_email("chris.b@gmail.com") == base
    assert sl.hash_email("CHRIS.B+whatever@GoogleMail.com") == base


def test_the_dotted_gmail_fold_is_gmails_own_rule_not_a_looser_one(ledger):
    """``a.a@gmail.com`` folds to ``aa@gmail.com`` — NOT to ``a@gmail.com``. Gmail ignores dots;
    it does not ignore the characters between them. Pinned because "treat a.a and a as one" is
    the plausible-sounding over-fold that would start rejecting strangers."""
    assert ei.canonical_email("a.a@gmail.com") == "aa@gmail.com"
    assert sl.hash_email("a.a@gmail.com") == sl.hash_email("aa+tag@gmail.com")
    assert sl.hash_email("a.a@gmail.com") != sl.hash_email("a@gmail.com")
    # ...while the '+tag' arm of the task's example does fold, which is the actual bypass.
    assert sl.hash_email("a+1@gmail.com") == sl.hash_email("a@gmail.com")


def test_three_attempts_across_three_variants_reach_reject(ledger):
    """The ladder walked end-to-end through the door's own decide/record contract."""
    assert sl.decide("farmer@gmail.com") == sl.DECISION_ALLOW
    sl.record_attempt("farmer@gmail.com", outcome=sl.OUTCOME_ALLOWED)

    assert sl.decide("farmer+one@gmail.com") == sl.DECISION_ALLOW_CHEEKY
    sl.record_attempt("farmer+one@gmail.com", outcome=sl.OUTCOME_ALLOWED_CHEEKY)

    assert sl.decide("far.mer+two@gmail.com") == sl.DECISION_REJECT
    # ...and all three landed on ONE row.
    assert len(set(ledger.written_keys)) == 1


# ── THE FALSE-POSITIVE GUARD — do not weaken ─────────────────────────────────────

def test_non_gmail_dotted_addresses_stay_separate_identities(ledger):
    """john.smith@corp.com and johnsmith@corp.com may be two different employees. Folding them
    would attach one person's strikes to another's signup, and the victim can never find out why
    they were refused. Dots are stripped for Gmail ONLY."""
    assert sl.hash_email("john.smith@corp.com") != sl.hash_email("johnsmith@corp.com")
    sl.record_attempt("john.smith@corp.com", outcome=sl.OUTCOME_ALLOWED)
    sl.record_attempt("john.smith@corp.com", outcome=sl.OUTCOME_ALLOWED)
    assert sl.decide("john.smith@corp.com") == sl.DECISION_REJECT      # burned their own strikes
    assert sl.decide("johnsmith@corp.com") == sl.DECISION_ALLOW        # a different person


def test_non_gmail_plus_tags_still_fold(ledger):
    """The one broad assumption the product already makes everywhere else."""
    assert sl.hash_email("john.smith+faultline@corp.com") == sl.hash_email("john.smith@corp.com")


def test_the_no_plus_domain_override_is_honoured(ledger, monkeypatch):
    """A deployment can declare '+' literal for a domain (SAAS_EMAIL_NO_PLUS_DOMAINS). The ladder
    reads the same escape hatch as the ban matcher — one identity definition, not two."""
    monkeypatch.setenv("SAAS_EMAIL_NO_PLUS_DOMAINS", "weird.example")
    assert sl.hash_email("bob+x@weird.example") != sl.hash_email("bob@weird.example")
    assert sl.hash_email("bob+x@other.example") == sl.hash_email("bob@other.example")


def test_an_uncanonicalisable_address_falls_back_instead_of_collapsing(ledger):
    """'+tag@x.com' strips to an empty local part. Returning "" would give every malformed
    address ONE shared identity and reject the third person to mistype their email."""
    assert sl.normalize_email("+tag@example.com") == "+tag@example.com"
    assert sl.hash_email("+tag@example.com") != sl.hash_email("+other@example.com")
    assert sl.hash_email("not-an-email") != sl.hash_email("")


# ── EXISTING HISTORY: what survives migration 232, and what does not ─────────────

def test_untagged_addresses_keep_a_bit_identical_key(ledger):
    """The claim the migration rests on: only the normalisation changed, so an address whose
    canonical form IS its lowercase form hashes to exactly what it hashed to before. That is the
    overwhelming majority of the table and it needs no migration at all."""
    for address in ("chris@example.com", "OWNER@AcmeRobotics.com", "john.smith@corp.com",
                    "plain@gmail.com"):
        assert sl.hash_email(address) == sl.legacy_hash_email(address)


def test_a_pre_232_row_for_a_tagged_address_still_counts_against_that_address(ledger):
    """The dual READ. A row written under the OLD lowercase-only key is not orphaned for the
    person who re-types that exact address: their history is consulted, so this change can never
    LOWER somebody's strike count."""
    ledger.rows[sl.legacy_hash_email("victim+tag@corp.com")] = 2       # 2 pre-232 strikes
    assert sl.get_attempt_count("victim+tag@corp.com") == 2
    assert sl.decide("victim+tag@corp.com") == sl.DECISION_REJECT


def test_pre_232_and_post_232_strikes_add_up_rather_than_double_count(ledger):
    """The two arms count DISJOINT sets of attempts — a pre-232 bump only ever reached the legacy
    row, a post-232 bump only ever reaches the canonical one — so summing is the true total."""
    ledger.rows[sl.legacy_hash_email("mix+tag@corp.com")] = 1          # pre-232, literal string
    ledger.rows[sl.hash_email("mix@corp.com")] = 1                     # post-232, canonical
    assert sl.get_attempt_count("mix+tag@corp.com") == 2
    # ...and an untagged caller, whose two keys AGREE, counts its single row once.
    assert sl.get_attempt_count("mix@corp.com") == 1


def test_writes_go_to_the_canonical_key_only(ledger):
    """Legacy keys are read, never written — so the residue is finite and ages out."""
    sl.record_attempt("mix+tag@corp.com", outcome=sl.OUTCOME_ALLOWED)
    assert ledger.written_keys == [sl.hash_email("mix@corp.com")]
    assert sl.legacy_hash_email("mix+tag@corp.com") not in ledger.written_keys


def test_a_purge_row_is_recorded_on_the_canonical_key_without_burning_a_strike(ledger):
    """The reaper's purge-surviving marker follows the same key, and still does not advance the
    counter (a purge is not a signup attempt)."""
    sl.record_attempt("gone+1@gmail.com", outcome=sl.OUTCOME_ALLOWED)
    sl.record_purge("gone@gmail.com")
    assert sl.get_attempt_count("gone+2@gmail.com") == 1


def test_the_hash_is_still_one_way_and_holds_no_plaintext(ledger):
    """Why no backfill exists, restated as a test: the stored value is a 64-hex digest that
    contains no recoverable address, so there is nothing to re-canonicalise. This is the
    confirmation, not an assumption."""
    key = sl.hash_email("someone@example.com")
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert "someone" not in key and "example" not in key


def test_an_unreadable_ledger_still_fails_safe_to_allow(monkeypatch):
    """Unchanged posture: never lock a legitimate user out on an infra blip."""
    def _boom():
        raise RuntimeError("control plane down")
    monkeypatch.setattr(sl.db, "connection", _boom)
    assert sl.get_attempt_count("x@example.com") is None
    assert sl.decide("x@example.com") == sl.DECISION_ALLOW


# ══════════════════════════════════════════════════════════════════════════════════
# PART 2 — THE HONEYPOT MUST NOT ANNOUNCE ITSELF
# ══════════════════════════════════════════════════════════════════════════════════

def _shape(value):
    """A comparable fingerprint of a response VALUE — what a bot can actually measure.

    Random ids and freshly minted keys differ between two genuine signups too, so comparing them
    literally would prove nothing. What must match is the shape: a UUID where a UUID belongs, an
    ``flk_``/``flu_`` key where a key belongs, the same booleans, the same nulls."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, str):
        if _UUID_RE.match(value):
            return "<uuid>"
        for canary in ("flk_", "flu_"):
            if value.startswith(canary):
                # LENGTH, not segment count: the real generators' secret half is token_urlsafe,
                # whose alphabet includes '_', so a genuine key splits into 3 or 4 parts at
                # random. Length is fixed (canary + 8 hex + '_' + 43) and is what a bot measures.
                return f"<{canary}key:{len(value)}>"
        if value.count(".") == 2 and len(value) > 40:
            return "<signed-token>"
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return "<timestamp>"
        return value
    return type(value).__name__


def _fingerprint(response):
    """Everything a caller can see: status, body shape, and the session-cookie header."""
    cookies = {}
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        attrs = sorted(a.strip().split("=")[0].lower() for a in rest.split(";")[1:] if a.strip())
        cookies[name] = attrs
    return {"status": response.status_code, "body": _shape(response.json()), "cookies": cookies}


# ── the TENANT door: POST /signup/team ───────────────────────────────────────────

TEAM_BODY = {"company": "Acme Robotics", "email": "owner@acmerobotics.com",
             "password": "correct horse battery", "display_name": "Wile E. Coyote"}


class Writes:
    def __init__(self):
        self.names = []

    def note(self, name):
        self.names.append(name)
        return True


@pytest.fixture
def team_client(monkeypatch):
    """The tenant door with every collaborator neutral and every WRITE recorded."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    w = Writes()
    monkeypatch.setattr(ts._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(ts._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(ts._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(ts._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(ts._signup_filter, "evaluate",
                        lambda e, **kw: ts._signup_filter.Decision(True))
    monkeypatch.setattr(ts._signup_ledger, "decide", lambda e: ts._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(ts._signup_ledger, "record_attempt", lambda e, **kw: True)
    monkeypatch.setattr(ts._auth, "get_user_by_username", lambda u: None)
    monkeypatch.setattr(ts._auth, "username_canonically_taken", lambda u: False)
    monkeypatch.setattr(ts, "pending_request_for_email", lambda e: False)
    monkeypatch.setattr(ts._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(ts._platform_settings, "notify_email", lambda: None)
    monkeypatch.setattr(ts._platform_settings, "welcome_from_address", lambda: None)
    monkeypatch.setattr(ts._platform_settings, "trial_days", lambda: 30)
    monkeypatch.setattr(ts._audit, "record", lambda *a, **kw: True)
    monkeypatch.setattr(ts._smtp_config, "resolve_config", lambda: {})
    for sender in ("send_welcome_email", "send_signup_notice_email",
                   "send_final_rejection_email", "send_cheeky_repeat_email"):
        monkeypatch.setattr(ts._smtp_sender, sender,
                            lambda to, _s=sender, **kw: w.note(_s) or {"ok": True})

    monkeypatch.setattr(ts._accounts, "create_account",
                        lambda *a, **kw: w.note("create_account") and {
                            "account_id": ACCOUNT_ID, "slug": a[1], "account_kind": "team",
                            "key_prefix": "flk_11111111",
                            "api_key": "flk_11111111_" + "s" * 43})
    monkeypatch.setattr(ts._auth, "create_dashboard_user",
                        lambda **kw: w.note("create_dashboard_user") and OWNER_ID)
    monkeypatch.setattr(ts._auth, "set_display_name", lambda uid, dn: w.note("set_display_name"))
    monkeypatch.setattr(ts._auth, "touch_last_login", lambda uid: None)
    monkeypatch.setattr(ts._accounts, "start_account_trial",
                        lambda a, **kw: w.note("start_account_trial") and decoy.trial(
                            30, ts.TRIAL_SOURCE))
    monkeypatch.setattr(ts._accounts, "set_chatbot_trial",
                        lambda a, **kw: w.note("set_chatbot_trial") and {"active": True})
    monkeypatch.setattr(ts, "record_pending",
                        lambda **kw: w.note("record_pending") and str(uuid.uuid4()))

    app = FastAPI()
    app.include_router(ts.router)
    client = TestClient(app)
    client.writes = w
    return client


@pytest.mark.parametrize("approval_on,expected_status", [(True, 202), (False, 200)])
def test_team_honeypot_is_indistinguishable_in_both_modes(team_client, monkeypatch,
                                                          approval_on, expected_status):
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: approval_on)

    genuine = team_client.post("/signup/team", json=TEAM_BODY)
    team_client.cookies.clear()
    team_client.writes.names.clear()
    trapped = team_client.post("/signup/team",
                               json=dict(TEAM_BODY, website="http://spam.example"))

    assert genuine.status_code == expected_status, genuine.text
    assert _fingerprint(trapped) == _fingerprint(genuine)
    # ...and the trip created NOTHING: no account, no login, no queue row, no trial, no email.
    assert team_client.writes.names == []


def test_team_honeypot_with_approval_on_is_byte_identical(team_client, monkeypatch):
    """With approval ON both answers are fully deterministic, so "same shape" is not good enough
    — the bytes must match."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: True)
    genuine = team_client.post("/signup/team", json=TEAM_BODY)
    trapped = team_client.post("/signup/team",
                               json=dict(TEAM_BODY, website="http://spam.example"))
    assert trapped.status_code == genuine.status_code == 202
    assert trapped.content == genuine.content


def test_team_honeypot_carries_a_session_cookie_that_authenticates_nothing(team_client,
                                                                          monkeypatch):
    """A "success" with no Set-Cookie is a one-request tell, so the trip sets one — but it must
    never be a usable session. It is signed under a salt NOTHING verifies against."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    r = team_client.post("/signup/team", json=dict(TEAM_BODY, website="x"))
    from saas import dashboard_auth as auth
    raw = r.cookies[auth.SESSION_COOKIE_NAME].strip('"')
    assert raw                                              # the header is there...
    assert auth.verify_session(raw) is None                 # ...and it is worth nothing
    assert auth.verify_operator_session(raw) is None
    assert auth.verify_individual_session(raw) is None


def test_team_honeypot_does_not_skip_the_rest_of_the_anti_abuse_chain(team_client, monkeypatch):
    """Answering the honeypot EARLY was itself an oracle: send a bad email with the field filled
    and without it, compare 200 vs 400, and the field is identified. A trip must still 400."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    trapped = team_client.post("/signup/team",
                               json=dict(TEAM_BODY, email="not-an-email", website="x"))
    genuine = team_client.post("/signup/team", json=dict(TEAM_BODY, email="not-an-email"))
    assert trapped.status_code == genuine.status_code == 400
    assert trapped.json() == genuine.json()


def test_team_honeypot_still_reports_a_taken_mailbox_the_same_way(team_client, monkeypatch):
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(ts._auth, "get_user_by_username", lambda u: {"id": OWNER_ID})
    trapped = team_client.post("/signup/team", json=dict(TEAM_BODY, website="x"))
    genuine = team_client.post("/signup/team", json=TEAM_BODY)
    assert trapped.status_code == genuine.status_code == 409
    assert trapped.json() == genuine.json()


def test_team_honeypot_slug_matches_the_submitted_company(team_client, monkeypatch):
    """A random slug would not match the name the bot just sent — that mismatch is a tell."""
    monkeypatch.setattr(ts._platform_settings, "require_signup_approval", lambda: False)
    body = team_client.post("/signup/team", json=dict(TEAM_BODY, website="x")).json()
    assert body["slug"] == "acme-robotics"


# ── the INDIVIDUAL door: POST /me/signup ─────────────────────────────────────────

ME_BODY = {"email": "solo@example.com", "password": "correct horse battery",
           "display_name": "Solo"}


@pytest.fixture
def me_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    w = Writes()
    monkeypatch.setattr(uc._ratelimit, "allow_signup_attempt", lambda ip: True)
    monkeypatch.setattr(uc._name_guard, "is_disposable_email", lambda e: False)
    monkeypatch.setattr(uc._name_guard, "looks_gibberish", lambda n: False)
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e, **kw: uc._signup_filter.Decision(True))
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt", lambda e, **kw: True)
    monkeypatch.setattr(uc, "_email_taken", lambda e: False)
    monkeypatch.setattr(uc, "_consume_invite", lambda code: w.note("consume_invite"))
    monkeypatch.setattr(uc._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", lambda: ACCOUNT_ID)
    monkeypatch.setattr(uc._platform_settings, "retention_grace_days", lambda: 7)
    monkeypatch.setattr(uc._platform_settings, "notify_email", lambda: None)
    monkeypatch.setattr(uc._audit, "record", lambda *a, **kw: True)

    monkeypatch.setattr(uc, "_provision_individual_seat",
                        lambda a, u, seat_type=None: w.note("provision_seat") and {"ok": True})
    monkeypatch.setattr(uc, "_create_individual",
                        lambda **kw: w.note("create_individual") and INDIVIDUAL_ID)
    monkeypatch.setattr(uc, "_touch_individual_login", lambda i: None)
    monkeypatch.setattr(uc, "_delete_individual", lambda i: None)
    monkeypatch.setattr(uc._tokens, "mint_user_token",
                        lambda a, u: w.note("mint_token") and {
                            "token": "flu_22222222_" + "s" * 43,
                            "token_prefix": "flu_22222222"})
    monkeypatch.setattr(uc, "send_welcome_to_individual",
                        lambda t: w.note("welcome_email") and {"delivery": "emailed"})
    monkeypatch.setattr(uc._signup_approvals, "record_pending",
                        lambda **kw: w.note("record_pending") and str(uuid.uuid4()))
    monkeypatch.setattr(uc, "_notify_operator_of_signup", lambda r, **kw: None)

    app = FastAPI()
    app.include_router(uc.router)
    client = TestClient(app)
    client.writes = w
    return client


@pytest.mark.parametrize("approval_on,expected_status", [(True, 202), (False, 200)])
def test_me_signup_honeypot_is_indistinguishable_in_both_modes(me_client, monkeypatch,
                                                               approval_on, expected_status):
    """The individual door had the SAME class of problem, plus one more: its trip returned a bare
    200 where the genuine hold returns 202 with a retention_notice — so it was distinguishable
    even with approval ON, the mode the original comment assumed was safe."""
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: approval_on)

    genuine = me_client.post("/me/signup", json=ME_BODY)
    me_client.cookies.clear()
    me_client.writes.names.clear()
    trapped = me_client.post("/me/signup", json=dict(ME_BODY, website="http://spam.example"))

    assert genuine.status_code == expected_status, genuine.text
    assert _fingerprint(trapped) == _fingerprint(genuine)
    # ...and the trip created NOTHING: no seat, no login row, no token, no welcome email.
    assert me_client.writes.names == []


def test_me_signup_honeypot_with_approval_on_is_byte_identical(me_client, monkeypatch):
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: True)
    genuine = me_client.post("/me/signup", json=ME_BODY)
    trapped = me_client.post("/me/signup", json=dict(ME_BODY, website="x"))
    assert trapped.status_code == genuine.status_code == 202
    assert trapped.content == genuine.content


def test_me_signup_honeypot_session_cookie_authenticates_nothing(me_client, monkeypatch):
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    r = me_client.post("/me/signup", json=dict(ME_BODY, website="x"))
    from saas import dashboard_auth as auth
    raw = r.cookies[auth.INDIVIDUAL_SESSION_COOKIE_NAME].strip('"')
    assert raw
    assert auth.verify_individual_session(raw) is None
    assert auth.verify_session(raw) is None
    assert auth.verify_operator_session(raw) is None


def test_me_signup_honeypot_does_not_skip_the_rest_of_the_chain(me_client, monkeypatch):
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    trapped = me_client.post("/me/signup", json=dict(ME_BODY, email="nope", website="x"))
    genuine = me_client.post("/me/signup", json=dict(ME_BODY, email="nope"))
    assert trapped.status_code == genuine.status_code == 400
    assert trapped.json() == genuine.json()


def test_me_signup_honeypot_still_walks_the_three_strikes_ladder(me_client, monkeypatch):
    """A trapped bot on its third strike gets the ordinary not_eligible answer, not a decoy
    success — because the ladder runs BEFORE the fork, on both paths alike."""
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: False)
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: uc._signup_ledger.DECISION_REJECT)
    monkeypatch.setattr(uc._smtp_sender, "send_final_rejection_email",
                        lambda to, **kw: {"ok": True})
    trapped = me_client.post("/me/signup", json=dict(ME_BODY, website="x"))
    genuine = me_client.post("/me/signup", json=ME_BODY)
    assert trapped.status_code == genuine.status_code == 202
    assert trapped.json() == genuine.json() and trapped.json()["status"] == "not_eligible"


# ── the decoy primitives themselves ──────────────────────────────────────────────

def test_decoy_keys_come_from_the_real_generators(ledger):
    """Formats are minted, never imitated: a lookalike literal drifts the first time
    keys.generate_key or tokens.generate_user_token changes shape."""
    from saas import keys as k
    from saas import tokens as tk
    full, prefix = decoy.account_api_key()
    assert full.startswith("flk_") and prefix == k.prefix_of(full)
    full, prefix = decoy.user_api_key()
    assert full.startswith("flu_") and prefix == tk.prefix_of(full)


def test_decoy_tokens_never_verify_in_any_realm():
    from saas import dashboard_auth as auth
    token = decoy.session_token({"sub": "a", "acct": "b", "usr": "c"})
    assert auth.verify_session(token) is None
    assert auth.verify_operator_session(token) is None
    assert auth.verify_individual_session(token) is None
    assert auth.verify_csrf(token, dashboard_user_id="a") is False


def test_decoy_signing_failure_still_returns_a_plausible_token(monkeypatch):
    """A 500 on the honeypot path would be the loudest tell of all."""
    def _boom(*a, **kw):
        raise RuntimeError("itsdangerous unavailable")
    monkeypatch.setattr(decoy._auth, "_sign", _boom)
    token = decoy.session_token({"sub": "a"})
    assert token.count(".") == 2 and len(token) > 40
