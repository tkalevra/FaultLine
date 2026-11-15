"""Webhook-endpoint API-version compatibility — the silent-grant-drop guard.

WHY THIS FILE EXISTS (2026-08-13, found on live production).

``STRIPE_API_VERSION`` pins the version our OUTBOUND requests use, and
``register_webhook_endpoint`` passes that same pin when it creates an endpoint. But an endpoint
created BY HAND in the Stripe Dashboard takes the ACCOUNT DEFAULT instead — and the Dashboard's
create form offers no historical versions to choose from, only the account default and the
current preview. On any Stripe account opened after Basil (2025-03-31) that default is
Basil-or-later, so the hand-made endpoint is incompatible from birth.

Incompatible how: Basil is a BREAKING release that MOVED three fields our handlers read
positionally with ``obj.get(...)`` in ``saas/stripe_webhooks.py``:

    Subscription.current_period_start/end  →  items.data[].current_period_*
    Invoice.subscription                   →  parent.subscription_details.subscription
    Invoice.charge                         →  payments

``dict.get`` on a moved key does not raise. It returns None, the handler carries on, the delivery
returns 200, and the event row records 'processed' — with a renewal that has no subscription link
and a cancellation that has no period boundary. Nothing looks wrong from any screen. That is the
precise failure the pin was added to prevent, and the pin did not cover endpoints we did not
create.

So the property under test is: an incompatible endpoint is IMPOSSIBLE TO MISS. It is named on the
operator's readiness check, and registration verifies its own readback rather than assuming the
pin was honoured.
"""

import sys
import types

import pytest

from saas.payments_stripe import (
    STRIPE_API_VERSION,
    _BREAKING_PAYLOAD_RELEASE,
    _endpoint_version_incompatible,
)


def _cfg(**over):
    base = {"provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
            "enabled_methods": ["card"],
            "api_key": "pk_test_XXX_placeholder",
            "api_secret": "sk_test_XXX_placeholder",
            "webhook_secret": "whsec_XXX_placeholder"}
    base.update(over)
    return base


def _fake_endpoint_list(monkeypatch, endpoints):
    """Make stripe.WebhookEndpoint.list(...).auto_paging_iter() yield these plain dicts."""
    import stripe

    class _Page:
        def auto_paging_iter(self):
            return iter(endpoints)

    monkeypatch.setattr(stripe.WebhookEndpoint, "list", lambda **kw: _Page())
    monkeypatch.setattr(stripe.Account, "retrieve", lambda *a, **k: True)


# ── the predicate ─────────────────────────────────────────────────────────────────

def test_the_pinned_version_is_compatible():
    """The version we actually pin must be judged safe, or the guard is self-contradictory."""
    assert _endpoint_version_incompatible(STRIPE_API_VERSION) is None


def test_basil_itself_is_incompatible():
    """The BOUNDARY case. Basil is the release that moved the fields, so Basil is already
    broken — not 'the release after Basil'. An off-by-one here passes the exact version that
    breaks us."""
    why = _endpoint_version_incompatible(_BREAKING_PAYLOAD_RELEASE + ".basil")
    assert why, "Basil must be judged incompatible — it IS the breaking release"


def test_the_version_production_was_actually_on_is_incompatible():
    """The real observed value from the live account on 2026-08-13."""
    why = _endpoint_version_incompatible("2026-07-29.dahlia")
    assert why
    # The reason must be actionable — it has to name what breaks, not just say 'too new'.
    assert "2026-07-29.dahlia" in why
    assert STRIPE_API_VERSION in why


def test_a_release_name_suffix_does_not_defeat_the_comparison():
    """Versions carry a trailing release name ('2026-07-29.dahlia'). Comparing the whole string
    against a bare date still works here, but the predicate compares the YYYY-MM-DD prefix
    explicitly so a future naming change cannot silently invert the result."""
    assert _endpoint_version_incompatible("2025-04-30.basil")
    assert _endpoint_version_incompatible("2030-01-01.something") is not None
    assert _endpoint_version_incompatible("2024-12-31") is None


def test_no_version_means_account_default_and_is_reported_unsafe():
    """api_version absent = the endpoint FOLLOWS THE ACCOUNT DEFAULT. That is not 'fine', it is
    'unpinned' — the account default moves when the account upgrades, and on a post-Basil account
    it is already wrong. Fail toward saying so."""
    for absent in (None, "", "   "):
        why = _endpoint_version_incompatible(absent)
        assert why, f"absent api_version {absent!r} must be reported, not assumed safe"
        assert "ACCOUNT DEFAULT" in why


# ── readiness surfaces it ─────────────────────────────────────────────────────────

def test_verify_flags_an_enabled_but_incompatible_endpoint(monkeypatch):
    """The whole point: the operator's 'Test connection' button must say this out loud.

    Credentials genuinely verify, so ok stays True — but ready must be False, because the
    integration would take money it cannot fully honour."""
    from saas.payments_stripe import StripePaymentProvider
    _fake_endpoint_list(monkeypatch, [
        {"id": "we_1", "status": "enabled", "api_version": "2026-07-29.dahlia",
         "url": "https://mcp.faultline.ca/payments/stripe-webhook"},
    ])
    p = StripePaymentProvider(config=_cfg())
    res = p.verify()

    assert res["ok"] is True, "the credentials DO authenticate — do not lie about that"
    assert res["ready"] is False, "an unreadable payload shape is not 'ready to take a payment'"
    blob = " ".join(res["warnings"])
    assert "INCOMPATIBLE" in blob.upper()
    assert "mcp.faultline.ca" in blob, "must name WHICH endpoint, not just that one is wrong"
    assert "INCOMPATIBLE" in res["message"].upper()


def test_verify_is_ready_when_the_endpoint_is_pinned(monkeypatch):
    """The CONTROL. A correctly-pinned endpoint must NOT warn, or the check is noise that gets
    ignored — and a check operators learn to ignore is worse than no check."""
    from saas.payments_stripe import StripePaymentProvider
    _fake_endpoint_list(monkeypatch, [
        {"id": "we_1", "status": "enabled", "api_version": STRIPE_API_VERSION,
         "url": "https://mcp.faultline.ca/payments/stripe-webhook"},
    ])
    p = StripePaymentProvider(config=_cfg())
    res = p.verify()

    assert res["ok"] is True
    assert res["ready"] is True, f"pinned endpoint must be clean, got: {res['warnings']}"
    assert res["warnings"] == []


def test_a_disabled_incompatible_endpoint_is_not_the_complaint(monkeypatch):
    """Only ENABLED endpoints deliver events. A disabled one on an old/new version is inert, and
    warning about it would train the operator to dismiss the warning."""
    from saas.payments_stripe import StripePaymentProvider
    _fake_endpoint_list(monkeypatch, [
        {"id": "we_dead", "status": "disabled", "api_version": "2026-07-29.dahlia",
         "url": "https://old.example/hook"},
        {"id": "we_live", "status": "enabled", "api_version": STRIPE_API_VERSION,
         "url": "https://mcp.faultline.ca/payments/stripe-webhook"},
    ])
    p = StripePaymentProvider(config=_cfg())
    res = p.verify()
    assert res["ready"] is True, f"disabled endpoint should not warn, got: {res['warnings']}"


# ── registration verifies its own readback ────────────────────────────────────────

def _fake_create(monkeypatch, returned_version, captured):
    import stripe

    def _create(**kw):
        captured.update(kw)
        return types.SimpleNamespace(id="we_new", secret="whsec_minted_once",
                                     api_version=returned_version)

    monkeypatch.setattr(stripe.WebhookEndpoint, "create", _create)


def test_registration_sends_the_pin(monkeypatch):
    """The request must carry our pin — this is the ONLY way to get a historical version, since
    the Stripe Dashboard's create form cannot select one."""
    from saas.payments_stripe import StripePaymentProvider
    captured: dict = {}
    _fake_create(monkeypatch, STRIPE_API_VERSION, captured)
    p = StripePaymentProvider(config=_cfg())
    out = p.register_webhook_endpoint("https://mcp.faultline.ca/payments/stripe-webhook")

    assert captured.get("api_version") == STRIPE_API_VERSION
    assert out["ok"] is True
    assert out.get("warning") is None
    assert out["api_version"] == STRIPE_API_VERSION


def test_registration_screams_if_stripe_ignores_the_pin(monkeypatch, capsys):
    """If Stripe hands back a version we did not ask for, a silently-ignored pin is
    INDISTINGUISHABLE from a successful one at this seam. So read it back and say so.

    It stays ok=True on purpose: the endpoint now EXISTS at Stripe and its signing secret is
    minted exactly once, so refusing here would strand an orphan endpoint whose secret we threw
    away. Loud, not fatal."""
    from saas.payments_stripe import StripePaymentProvider
    captured: dict = {}
    _fake_create(monkeypatch, "2026-07-29.dahlia", captured)
    p = StripePaymentProvider(config=_cfg())
    out = p.register_webhook_endpoint("https://mcp.faultline.ca/payments/stripe-webhook")

    assert out["ok"] is True, "must not strand the endpoint/secret"
    assert out["secret"] == "whsec_minted_once", "the secret must still reach the caller to store"
    assert out.get("warning"), "an ignored pin must be reported"
    assert out["api_version"] == "2026-07-29.dahlia", "report what we GOT, not what we asked for"

    err = capsys.readouterr().err
    assert "WRONG API VERSION" in err, "give-up must be loud on stderr"
    assert "DO NOT treat this registration as done" in err


def test_registered_events_match_the_dispatch_map():
    """HANDLED_EVENT_TYPES is what register_webhook_endpoint SUBSCRIBES to; _HANDLERS is what we
    can actually process. They must be the same set, in BOTH directions.

    Found live on 2026-08-13: `invoice.voided` — the RESUME DOOR that reinstates a subscription
    and reverses a term-acceleration receivable — was in _HANDLERS and NOT in HANDLED_EVENT_TYPES.
    The two were kept in step by a comment saying "keep in sync", which is not a mechanism.

    Each direction fails differently and both are silent:
      handler without subscription → the event never arrives. Endpoint healthy, handler present,
        tests green, books quietly diverge from Stripe's.
      subscription without handler → deliveries we acknowledge and drop, which reads as
        'processed' in the event log.
    """
    from saas.payments_stripe import HANDLED_EVENT_TYPES
    from saas.stripe_webhooks import _HANDLERS

    subscribed, dispatched = set(HANDLED_EVENT_TYPES), set(_HANDLERS)
    assert not (dispatched - subscribed), (
        f"handler exists but nothing subscribes to it: {sorted(dispatched - subscribed)} — "
        f"add it to HANDLED_EVENT_TYPES or the event will never be delivered")
    assert not (subscribed - dispatched), (
        f"subscribed with no handler: {sorted(subscribed - dispatched)} — deliveries would be "
        f"acknowledged and dropped")
    assert len(HANDLED_EVENT_TYPES) == len(subscribed), "duplicate entry in HANDLED_EVENT_TYPES"


def test_registration_never_logs_the_minted_secret(monkeypatch, capsys):
    """The readback logging added a new log line near the secret. Pin that it stayed clean."""
    from saas.payments_stripe import StripePaymentProvider
    captured: dict = {}
    _fake_create(monkeypatch, "2026-07-29.dahlia", captured)
    p = StripePaymentProvider(config=_cfg())
    p.register_webhook_endpoint("https://mcp.faultline.ca/payments/stripe-webhook")
    assert "whsec_minted_once" not in capsys.readouterr().err
