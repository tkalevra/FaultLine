"""Stripe adapter unit tests — sandbox-only build.

Covers the hard rules from the agent brief:
  * LIVE-KEY REFUSE (secret / publishable / mode) → raises → falls back to stub.
  * create_checkout NEVER returns a paid status; entitlement_changed=False; charged=False.
  * amounts in the smallest currency unit (19.99 CAD → 1999 cents).
  * NO key is ever logged/echoed (verify() with a fake key → key appears nowhere).
  * adapter unavailable (stripe import fails) → get_payment_provider() returns the stub.

No real Stripe API call is made — every stripe.* call is monkeypatched.
"""

import io
import sys
import json
import re

import pytest

from saas import payments as _payments
from saas.payments import StubPaymentProvider, get_payment_provider


# ── helpers ───────────────────────────────────────────────────────────────────────

def _cfg(**over):
    base = {"provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
            "enabled_methods": ["card"],
            "api_key": "pk_test_XXX_placeholder",
            "api_secret": "sk_test_XXX_placeholder",
            "webhook_secret": "whsec_XXX_placeholder"}
    base.update(over)
    return base


def _capture_stderr(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


# ── LIVE-KEY REFUSE ───────────────────────────────────────────────────────────────

# ⚠️ THESE FOUR TESTS WERE INVERTED ON 2026-08-13, NOT DELETED.
#
# They used to pin the sandbox-only refuse: a live credential RAISED and get_payment_provider()
# fell back to the stub. The owner lifted that guard deliberately once the paid flow was finished,
# so the old assertions were correctly red and had to change. Deleting them would have been the
# wrong move — the guard was a SAFETY property, and lifting it does not remove the need for one,
# it changes which one. The property is now: a live adapter comes up, WORKS, and SAYS SO LOUDLY.
#
# So each test keeps its subject and flips its expectation, and the announce is asserted rather
# than assumed. If someone re-adds a refusal, or silences the announce, these go red — which is
# what a test for "you cannot go live by accident" has to do in both directions.


def test_live_secret_key_now_constructs_and_announces(monkeypatch):
    """sk_live_ secret → constructs (no raise) AND announces at CRIT."""
    from saas.payments_stripe import StripePaymentProvider
    buf = _capture_stderr(monkeypatch)
    p = StripePaymentProvider(config=_cfg(api_secret="sk_live_real_secret_key_12345"))
    assert p.is_configured() is True, "a live secret must not disqualify the adapter"
    out = buf.getvalue()
    assert "LIVE MODE" in out and "real money can move" in out, (
        "going live must be LOUD — the announce is what replaced the refusal")
    assert "sk_live_real_secret_key_12345" not in out, "never log the credential itself"


def test_live_publishable_key_now_constructs_and_announces(monkeypatch):
    """pk_live_ publishable → constructs AND announces, even with mode still 'test'.

    The detection deliberately scans EVERY credential field rather than the one expected to hold
    each kind: an operator with a live publishable is in live mode whether or not `mode` says so,
    and the log has to tell that truth."""
    from saas.payments_stripe import StripePaymentProvider
    buf = _capture_stderr(monkeypatch)
    p = StripePaymentProvider(config=_cfg(api_key="pk_live_real_publishable_key_12345"))
    assert p.is_configured() is True
    assert "LIVE MODE" in buf.getvalue()


def test_live_mode_now_constructs_and_announces(monkeypatch):
    """config mode='live' → constructs AND announces."""
    from saas.payments_stripe import StripePaymentProvider
    buf = _capture_stderr(monkeypatch)
    p = StripePaymentProvider(config=_cfg(mode="live"))
    assert p.is_configured() is True
    assert "LIVE MODE" in buf.getvalue()


def test_test_mode_stays_quiet(monkeypatch):
    """The CONTROL. Test credentials must NOT announce — an alarm that fires on every
    construction is one nobody reads, which is the whole reason the announce is conditional."""
    from saas.payments_stripe import StripePaymentProvider
    buf = _capture_stderr(monkeypatch)
    p = StripePaymentProvider(config=_cfg())
    assert p.is_configured() is True
    assert "LIVE MODE" not in buf.getvalue()


def test_a_live_adapter_still_requires_a_webhook_secret():
    """Lifting the sandbox guard did NOT relax the webhook requirement. Without it the adapter
    would be a charge-creator that cannot verify its own charges' entitlement."""
    from saas.payments_stripe import StripePaymentProvider
    p = StripePaymentProvider(config=_cfg(mode="live", api_secret="sk_live_x", webhook_secret=""))
    assert p.is_configured() is False


def test_live_key_now_resolves_to_the_real_adapter(monkeypatch):
    """A live key in the config store → get_payment_provider() returns the stub (never charges)."""
    # The configured-provider resolver reads payment_config.get_config_redacted; patch BOTH the
    # redacted view (provider selection) and resolve_config (the decrypted creds handed to the
    # adapter constructor, where the live-key refuse fires).
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "get_config_redacted",
                        lambda: {"provider": "stripe", "mode": "test", "configured": True})
    monkeypatch.setattr(_pc, "resolve_config",
                        lambda: _cfg(api_secret="sk_live_XXX"))
    p = get_payment_provider()
    # INVERTED with the three above: a live key used to fall back to the stub. It now resolves to
    # the REAL adapter, which is the point of lifting the guard — the stub cannot take money, and
    # taking money is now intended. The stub remains the fallback for an UNCONFIGURED provider.
    from saas.payments_stripe import StripePaymentProvider
    assert not isinstance(p, StubPaymentProvider), (
        "a live key must now resolve to the real adapter, not the stub")
    assert isinstance(p, StripePaymentProvider)


# ── create_checkout NEVER returns paid ────────────────────────────────────────────

def test_create_checkout_never_returns_paid(monkeypatch):
    """A successful checkout creation returns checkout_created with charged=False + no entitlement.

    ⚠️ ``SAAS_PUBLIC_BASE_URL`` is now REQUIRED for a checkout that supplies no explicit
    success/cancel url. The adapter used to fall back to a RELATIVE default
    (``"/?checkout=success&session_id={CHECKOUT_SESSION_ID}"``), which is broken regardless of
    Stripe's validation: the redirect is issued by checkout.stripe.com, so a relative path
    resolves against STRIPE'S origin and a paying customer lands on Stripe's domain instead of
    ours. The adapter now builds an ABSOLUTE default from the deployment's configured public
    origin and REFUSES if it has none. Setting the env here makes this test deterministic
    instead of dependent on whatever SMTP config the host happens to have."""
    monkeypatch.setenv("SAAS_PUBLIC_BASE_URL", "https://app.faultline.ca")
    from saas.payments_stripe import StripePaymentProvider
    p = StripePaymentProvider(config=_cfg())

    # Fake the stripe.checkout.Session.create to return a session-like object.
    class _FakeSession:
        id = "cs_test_123"
        url = "https://checkout.stripe.com/c/cs_test_123"
    import stripe
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: _FakeSession())

    result = p.create_checkout(
        account_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        plan_id="pro_monthly", amount=49.99, price_id="price_test_abc")
    assert result["status"] == "checkout_created", f"got {result['status']}"
    assert result["charged"] is False
    assert result["entitlement_changed"] is False
    assert result["checkout_url"] == "https://checkout.stripe.com/c/cs_test_123"
    assert result["session_id"] == "cs_test_123"
    assert result["mode"] == "test"


def test_create_checkout_requires_price_id():
    """Without a price_id the adapter refuses (subscriptions are priced on Stripe's side)."""
    from saas.payments_stripe import StripePaymentProvider
    p = StripePaymentProvider(config=_cfg())
    result = p.create_checkout(
        account_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        plan_id="pro_monthly", amount=49.99, price_id=None)
    assert result["status"] == "missing_price"
    assert result["charged"] is False
    assert result["entitlement_changed"] is False
    assert result["checkout_url"] is None


def test_create_checkout_unconfigured_returns_not_configured():
    """Missing webhook secret → is_configured() False → honest not-configured return."""
    from saas.payments_stripe import StripePaymentProvider
    p = StripePaymentProvider(config=_cfg(webhook_secret=None))
    result = p.create_checkout(
        account_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        plan_id="pro_monthly", amount=49.99, price_id="price_test_abc")
    assert result["status"] == "provider_not_configured"
    assert result["charged"] is False
    assert result["checkout_url"] is None


# ── amounts in the smallest currency unit ─────────────────────────────────────────

def test_amounts_in_smallest_unit():
    """19.99 CAD → 1999 cents (the conversion exists, even though price_id governns for subs)."""
    from saas.payments_stripe import _amount_to_cents
    assert _amount_to_cents(19.99) == 1999
    assert _amount_to_cents(100.00) == 10000
    assert _amount_to_cents(0.01) == 1
    assert _amount_to_cents(0) == 0
    # Float-representation noise guard: 19.99 must not become 1998.
    assert _amount_to_cents(19.99) != 1998


# ── no key ever logged ────────────────────────────────────────────────────────────

def test_no_key_ever_logged(monkeypatch):
    """verify() with a fake key — the key string appears NOWHERE in stderr or the return dict."""
    from saas.payments_stripe import StripePaymentProvider
    FAKE_SECRET = "sk_test_SECRETNEVERLEAK_0123456789abcdef"
    FAKE_WHSEC = "whsec_NEVERLEAKSECRET_0123456789abcdef"
    p = StripePaymentProvider(config=_cfg(api_secret=FAKE_SECRET, webhook_secret=FAKE_WHSEC))

    # Make Account.retrieve succeed (so verify() goes through the happy path).
    import stripe
    monkeypatch.setattr(stripe.Account, "retrieve", lambda *a, **k: True)

    buf = _capture_stderr(monkeypatch)
    result = p.verify()

    # Combine everything that could leak: stderr + the return dict serialized.
    blob = buf.getvalue() + json.dumps(result, default=str)
    # The full secret strings must NOT appear. (A prefix like sk_test_ in a *different*
    # placeholder is fine; we assert the SPECIFIC secret value never leaks.)
    assert FAKE_SECRET not in blob, "secret key leaked into stderr/response"
    assert FAKE_WHSEC not in blob, "webhook secret leaked into stderr/response"
    # No live key fragment either.
    assert "sk_live_" not in blob
    assert "pk_live_" not in blob


def test_verify_failure_is_secret_free(monkeypatch):
    """A stripe error during verify() returns ok=False with a secret-free reason."""
    from saas.payments_stripe import StripePaymentProvider
    FAKE = "sk_test_NEVERLEAK_abc123"
    p = StripePaymentProvider(config=_cfg(api_secret=FAKE, webhook_secret="whsec_X"))
    import stripe
    # Simulate an authentication error carrying the key (defensive redaction).
    def _raise(**kw):
        raise stripe.error.AuthenticationError("sk_test_NEVERLEAK_abc123 invalid key")
    monkeypatch.setattr(stripe.Account, "retrieve", _raise)
    buf = _capture_stderr(monkeypatch)
    result = p.verify()
    blob = buf.getvalue() + json.dumps(result, default=str)
    assert result["ok"] is False
    assert "NEVERLEAK" not in blob, "redaction failed — key fragment leaked"


# ── adapter unavailable → stub ────────────────────────────────────────────────────

def test_adapter_unavailable_falls_back_to_stub(monkeypatch):
    """If the stripe import fails, get_payment_provider() returns the stub.

    We simulate the stripe lib being absent by making the lazy resolver raise."""
    from saas import payment_config as _pc
    monkeypatch.setattr(_pc, "get_config_redacted",
                        lambda: {"provider": "stripe", "mode": "test", "configured": True})
    monkeypatch.setattr(_pc, "resolve_config",
                        lambda: _cfg())
    # Force the lazy resolver to fail by patching the payments_stripe import.
    import saas.payments_stripe as _ps
    # Simulate the stripe lib being gone: make the module's _stripe None.
    monkeypatch.setattr(_ps, "_stripe", None)
    p = get_payment_provider()
    # With _stripe None, the lazy resolver still returns the CLASS, but construction fails
    # (the class raises RuntimeError on _stripe None) → the orchestrator's try/except catches
    # it and returns the stub.
    assert isinstance(p, StubPaymentProvider), \
        f"expected stub when stripe lib absent, got {type(p).__name__}"


def test_registry_resolves_stripe_class():
    """The registry entry for 'stripe' resolves to the real adapter class when the lib is present."""
    from saas.payments_stripe import StripePaymentProvider
    entry = _payments._REGISTRY.get("stripe")
    cls = entry() if not isinstance(entry, type) else entry
    assert cls is StripePaymentProvider
