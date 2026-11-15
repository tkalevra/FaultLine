"""Registration NAME + ANTI-ABUSE guard (saas/name_guard.py) + signup wiring (saas/user_console).

This is the security core of self-serve signup — a stranger must not be able to register an
infra/reserved label, a brand, a LOOKALIKE of a brand, or profanity, and the endpoint must drop
bot/spam signups. Tested HARD: homoglyphs + Scunthorpe false-positives are the load-bearing cases.

SAAS_MODE OFF is unaffected (name_guard is pure + only invoked inside the SAAS-gated /me/signup).

Run: python3 tools/fltest.py --bug NAMEGUARD --test tests/test_saas_name_guard.py
"""

import types

import pytest
from fastapi import HTTPException, Response

from saas import name_guard as ng
from saas import ratelimit as rl
from saas import user_console as uc


# ══════════════════════════════════════════════════════════════════════════════════
# 1 — LABEL FORMAT / SUBDOMAIN SAFETY (owner's explicit edge cases)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [
    "@", "", ".", "*", "-", "--", " ", "   ", "_foo", "foo_bar", "xn--abc",
    "foo.bar", "a b", "foo/bar", "foo@bar",
    "-lead", "trail-", "a--b", "x" * 64, "123", "007",
])
def test_format_rejects(bad):
    r = ng.check_label(bad)
    assert not r.ok, f"{bad!r} should be rejected"
    # empty is its own code; everything else fails FORMAT before any denylist.
    assert r.code in ("format", "empty"), f"{bad!r} → {r.code}"


def test_uppercase_is_folded_not_a_format_error():
    # a normal label typed with capitals is normalized (casefold), not rejected on format.
    assert ng.check_label("Acme-Labs").ok
    # ...but an all-numeric or bad-char one still fails.
    assert not ng.check_label("ACME_LABS").ok


def test_punycode_prefix_banned():
    assert ng.check_label("xn--80ak6aa92e").code == "format"   # would decode to а lookalike


# ══════════════════════════════════════════════════════════════════════════════════
# 2 — RESERVED infra labels
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("word", [
    "www", "admin", "root", "api", "app", "dashboard", "operator", "login", "signup",
    "account", "billing", "mail", "email", "webmail",
    "smtp", "imap", "ftp", "ns", "ns1", "ns2", "mx", "dns", "autoconfig",
    "cdn", "static", "assets", "test", "staging", "internal", "localhost",
    "faultline", "volenti", "mcp", "cortex",
])
def test_reserved_blocked(word):
    r = ng.check_label(word)
    assert not r.ok and r.code == "reserved", f"{word!r} should be reserved → {r.code}"
    assert "isn't available" in r.message           # generic, does not leak WHY


# ── L2 (SECURITY audit 2026-07-15): the reserved list was over-broad. Generic PRODUCT / content
#    words are NO LONGER reserved (the owner's own product is ExampleCorp → `helpdesk` must
#    register), but every genuine infra / DNS / payment / impersonation label still blocks. ─────
@pytest.mark.parametrize("word", [
    "helpdesk", "support", "help", "store", "shop", "blog", "news", "dev", "demo",
    "status", "about", "home", "portal", "develop", "development",
])
def test_l2_generic_product_words_pass(word):
    r = ng.check_label(word)
    assert r.ok, f"{word!r} is a legit product word and must NOT be reserved (code={r.code})"


@pytest.mark.parametrize("word", [
    "www", "mail", "smtp", "imap", "ftp", "ns", "ns1", "ns2", "mx", "admin", "root",
    "api", "mcp", "app", "dashboard", "operator", "login", "signup", "account", "billing",
    "webmail", "autoconfig", "localhost", "faultline", "volenti", "cdn", "static", "assets",
    "email", "internal",
])
def test_l2_infra_and_impersonation_words_still_block(word):
    r = ng.check_label(word)
    assert not r.ok and r.code == "reserved", f"{word!r} must stay reserved → {r.code}"


def test_dmarc_underscore_label_is_rejected_on_format():
    # _dmarc has a leading underscore → fails FORMAT (which is correct: not a legal user label).
    assert ng.check_label("_dmarc").code == "format"


# ══════════════════════════════════════════════════════════════════════════════════
# 3 — BRANDS (exact)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("brand", [
    "google", "microsoft", "apple", "amazon", "meta", "facebook", "instagram",
    "whatsapp", "paypal", "stripe", "visa", "mastercard", "netflix", "openai",
    "anthropic", "chatgpt", "github", "cloudflare", "gmail", "outlook", "icloud",
    "aws", "azure", "coinbase", "binance", "claude",
])
def test_brand_blocked(brand):
    r = ng.check_label(brand)
    assert not r.ok and r.code in ("brand", "reserved"), f"{brand!r} → {r.code}"
    assert "isn't available" in r.message


# ══════════════════════════════════════════════════════════════════════════════════
# 4 — HOMOGLYPH / LOOKALIKE (THE IMPORTANT ONE)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("look", [
    "g00gle",            # leet zeros
    "g-o-o-g-l-e",       # separated
    "m1cros0ft",         # leet 1 + 0
    "micr0soft",
    "paypa1",            # trailing 1 → l
    "paypa11",           # doubled
    "app1e",             # 1 → l
    "amaz0n",
    "faceb00k",
    "netf1ix",
    "0penai",
    "githvb",            # v → u
    "c0inbase",
])
def test_leet_lookalikes_blocked(look):
    r = ng.check_label(look)
    assert not r.ok and r.code in ("brand", "reserved"), f"{look!r} should be a lookalike → {r.code}"


@pytest.mark.parametrize("look", [
    "gооgle",           # Cyrillic o o  → google
    "аpple",                 # Cyrillic a    → apple
    "pаypаl",           # Cyrillic a a  → paypal
    "microsоft",             # Cyrillic o    → microsoft
    "аmаzоn",      # Cyrillic a a o→ amazon
])
def test_cyrillic_homoglyphs_blocked(look):
    r = ng.check_label(look)
    # a Cyrillic char is not [a-z0-9-] → FORMAT catches it for a SLUG; either way it is blocked.
    assert not r.ok, f"{look!r} should be blocked"


def test_cyrillic_homoglyph_blocked_in_display_name():
    # display names allow unicode → here the SKELETON (not format) must catch the impersonation.
    assert not ng.check_display_name("gооgle").ok
    assert not ng.check_display_name("PayPa1 Support").ok
    assert not ng.check_display_name("Micros0ft").ok


@pytest.mark.parametrize("typo", [
    "gogle",             # google - 1
    "microsofte",        # microsoft + 1
    "netflx",            # netflix - 1 (vowel drop)
    "githb",             # github - 1
])
def test_fuzzy_typosquats_blocked(typo):
    r = ng.check_label(typo)
    assert not r.ok and r.code == "brand", f"{typo!r} typosquat should be caught → {r.code}"


# ── M2 (SECURITY audit 2026-07-15): visual MULTIGRAPH lookalikes — two glyphs that render as one
#    (rn→m, vv→w, cl→d, nn→m, ii→u). Single-char folds miss these; the brand-lane multigraph
#    pre-fold catches them. ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("look", [
    "rnicrosoft",        # rn → m  ⇒ microsoft
    "vvhatsapp",         # vv → w  ⇒ whatsapp
    "clropbox",          # cl → d  ⇒ dropbox
])
def test_m2_multigraph_lookalikes_blocked(look):
    r = ng.check_label(look)
    assert not r.ok and r.code in ("brand", "reserved"), f"{look!r} multigraph lookalike → {r.code}"


def test_m2_multigraph_blocked_in_display_name():
    assert not ng.check_display_name("Rnicrosoft").ok
    assert not ng.check_display_name("VVhatsApp Support").ok


# The multigraph fold is confined to the BRAND lane — it must NOT rewrite legit words that happen
# to contain rn / cl / nn / vv / ii on the reserved/format/profanity path, and even inside the
# brand lane their folded forms land on no brand. Regression guard (the M2 tradeoff).
@pytest.mark.parametrize("good", [
    "learn", "corner", "modern", "govern", "include", "uncle", "muscle",
    "cannon", "dinner", "connect", "savvy", "hawaii", "skiing",
])
def test_m2_multigraph_no_false_block_on_legit_words(good):
    r = ng.check_label(good)
    assert r.ok, f"{good!r} must NOT be false-blocked by the multigraph fold (code={r.code})"


# ══════════════════════════════════════════════════════════════════════════════════
# 5 — PROFANITY (blocked) WITHOUT the Scunthorpe problem (legit words pass)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("word", ["fuck", "shit", "fvck", "b1tch", "a55hole"])
def test_profanity_blocked(word):
    r = ng.check_label(word)
    assert not r.ok, f"{word!r} should be blocked"
    assert r.code in ("profanity", "brand", "reserved")


@pytest.mark.parametrize("word", [
    "class", "assess", "assessment", "analysis", "classic", "asset",
    "scunthorpe", "cockburn", "penistone", "sussex", "essex", "matsushita",
])
def test_scunthorpe_words_pass(word):
    r = ng.check_label(word)
    assert r.ok, f"{word!r} is legitimate and must NOT be blocked (code={r.code})"


# ══════════════════════════════════════════════════════════════════════════════════
# 6 — LEGITIMATE ordinary names ACCEPTED (guard against over-blocking)
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("good", [
    "acme-labs", "chris", "examplecorp", "acme", "widgets-inc", "north-star",
    "team42", "kitchener-it", "optimus-cannabis", "blue-ridge", "j5",
])
def test_legit_labels_accepted(good):
    r = ng.check_label(good)
    assert r.ok, f"{good!r} is legitimate and must be accepted (code={r.code}, msg={r.message})"


@pytest.mark.parametrize("good", [
    "Chris", "Acme Labs", "Dr. Jane Smith", "José García", "北京", "Helpdesk Pro",
])
def test_legit_display_names_accepted(good):
    assert ng.check_display_name(good).ok, f"{good!r} display name must be accepted"


def test_empty_display_name_is_ok():
    assert ng.check_display_name(None).ok
    assert ng.check_display_name("").ok
    assert ng.check_display_name("   ").ok


# ══════════════════════════════════════════════════════════════════════════════════
# 7 — DISPOSABLE EMAIL + GIBBERISH heuristic
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("email", [
    "a@mailinator.com", "x@guerrillamail.com", "y@10minutemail.com", "z@tempmail.com",
    "q@yopmail.com", "r@trashmail.com", "s@foo.mailinator.com",   # subdomain of disposable
])
def test_disposable_email_detected(email):
    assert ng.is_disposable_email(email)


@pytest.mark.parametrize("email", [
    "chris@gmail.com", "me@example.com", "a@uoft.ca", "x@company.co.uk", "not-an-email",
])
def test_real_email_not_disposable(email):
    assert not ng.is_disposable_email(email)


def test_gibberish_is_soft_signal():
    assert ng.looks_gibberish("xkjhgqwrtplbcd")       # high consonant run → flagged
    assert not ng.looks_gibberish("Christopher")        # a real name → not flagged
    assert not ng.looks_gibberish("chris")              # too short to judge
    assert not ng.looks_gibberish("José García")


# ══════════════════════════════════════════════════════════════════════════════════
# 8 — SIGNUP RATE LIMITER (fail-open contract)
# ══════════════════════════════════════════════════════════════════════════════════

def test_signup_rate_limit_fail_open_when_disabled(monkeypatch):
    monkeypatch.delenv("SAAS_SIGNUP_RATELIMIT_ENABLED", raising=False)
    assert rl.allow_signup_attempt("1.2.3.4") is True          # flag off → always allowed
    assert rl.allow_signup_attempt(None) is True
    assert rl.allow_signup_attempt("?") is True


# ══════════════════════════════════════════════════════════════════════════════════
# 9 — ENDPOINT WIRING (/me/signup): honeypot · disposable · display-name · rate limit
# ══════════════════════════════════════════════════════════════════════════════════

def _req(ip="9.9.9.9"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=ip))


def _body(**kw):
    base = dict(email="new@company.com", password="supersecret", invite_code="INV", website=None,
                display_name=None)
    base.update(kw)
    return uc.SignupRequest(**base)


@pytest.fixture(autouse=True)
def _moderation_guards_neutral(monkeypatch):
    """Migrations 228/229: /me/signup now also consults the platform BAN LIST and the global
    SIGNUP SPAM FILTER, both of which read the control plane this DB-less suite does not have
    (the ban check fails CLOSED by design). Stub them NEUTRAL — their own behaviour is proven in
    tests/test_saas_account_moderation.py; this file is about the NAME guards."""
    monkeypatch.setattr(uc._moderation, "signup_ban_check",
                        lambda e: {"banned": False, "degraded": False})
    monkeypatch.setattr(uc._signup_filter, "evaluate",
                        lambda e, **kw: uc._signup_filter.Decision(True))


@pytest.fixture
def _stub_signup(monkeypatch):
    """Neutralize SAAS gate + all downstream provisioning so we test ONLY the guard wiring.
    Any call to a provisioning primitive means a guard FAILED to short-circuit → test blows up."""
    monkeypatch.setattr(uc, "_require_saas", lambda: None)

    def _boom(*a, **k):
        raise AssertionError("provisioning ran — a guard should have short-circuited")

    monkeypatch.setattr(uc, "_consume_invite", _boom)
    monkeypatch.setattr(uc, "_provision_individual_seat", _boom)
    monkeypatch.setattr(uc, "_create_individual", _boom)
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", _boom)
    monkeypatch.setattr(rl, "allow_signup_attempt", lambda ip: True)
    return monkeypatch


def test_honeypot_filled_is_dropped_without_provisioning(_stub_signup, monkeypatch):
    """The honeypot is now DETECTED here and ANSWERED at the provisioning fork, with a decoy
    matching whichever approval mode is active — answering early was itself an oracle (fill the
    field with a bad email → "received"; leave it empty → 400, and the field is identified). So a
    trip walks the rest of the chain, which this test now supplies. The indistinguishability
    proof lives in tests/test_saas_open_signup_hardening.py; what this file still pins is that
    NOTHING is provisioned — _provision_individual_seat / _create_individual stay _boom."""
    monkeypatch.setattr(uc, "_consume_invite", lambda code: True)
    monkeypatch.setattr(uc, "_email_taken", lambda e: False)
    monkeypatch.setattr(uc._signup_ledger, "decide", lambda e: uc._signup_ledger.DECISION_ALLOW)
    monkeypatch.setattr(uc._signup_ledger, "record_attempt", lambda e, **kw: True)
    monkeypatch.setattr(uc._auth, "hash_password", lambda p: "$argon2id$stub")
    monkeypatch.setattr(uc._accounts, "get_or_create_singles_account", lambda: "singles-account")
    monkeypatch.setattr(uc._platform_settings, "require_signup_approval", lambda: True)
    out = uc.me_signup(_body(website="http://spam.example"), _req(), Response())
    assert out["status"] == "pending_approval"     # the mode-matched answer with approval ON
    # (the _boom stubs guarantee NOTHING was provisioned)


def test_disposable_email_rejected(_stub_signup):
    with pytest.raises(HTTPException) as ei:
        uc.me_signup(_body(email="bot@mailinator.com"), _req(), Response())
    assert ei.value.status_code == 400
    assert "permanent email" in ei.value.detail.lower()


def test_brand_display_name_rejected(_stub_signup):
    with pytest.raises(HTTPException) as ei:
        uc.me_signup(_body(display_name="Google"), _req(), Response())
    assert ei.value.status_code == 400


def test_rate_limited_signup_429(_stub_signup):
    _stub_signup.setattr(rl, "allow_signup_attempt", lambda ip: False)
    with pytest.raises(HTTPException) as ei:
        uc.me_signup(_body(), _req(), Response())
    assert ei.value.status_code == 429


def test_clean_signup_passes_guards_to_approval_gate(_stub_signup):
    """A legit signup must sail through every guard and reach the approval gate unharmed."""
    _stub_signup.setattr(uc._platform_settings, "require_signup_approval", lambda: True)
    _stub_signup.setattr(uc, "_email_taken", lambda e: False)
    _stub_signup.setattr(uc, "_consume_invite", lambda code: True)
    _stub_signup.setattr(uc._accounts, "get_or_create_singles_account", lambda: "acct-1")
    _stub_signup.setattr(uc._auth, "hash_password", lambda p: "hashed")
    sentinel = {"status": "pending_approval", "reached": "approval_gate"}
    _stub_signup.setattr(uc, "_hold_signup_for_approval", lambda **k: sentinel)

    out = uc.me_signup(_body(display_name="Chris", email="chris@company.com"), _req(), Response())
    assert out == sentinel                          # guards did NOT false-block a legit signup
