"""Security-regression tests for the TIER-1 fixes from the first critic loop (2026-08-11).

These pin the FOUR gaps the harsh-critic pass found, so a future edit cannot silently
re-introduce them:

1. AUTHORIZATION BYPASS on /admin/billing-ops/refund + /admin/billing-ops/register-webhook —
   these action routes MUST enforce the SAME fine-grained ``can_issue_refunds`` gate the
   dashboard surface does. A control hidden in the UI must also be refused by the route. The
   fix imported ``require_can_issue_refunds`` from billing_dashboard_routes into stripe_routes.
2. LIVE-KEY REFUSE BY VALUE, not by field-name. Putting sk_live_… in the api_key column (vs
   api_secret) used to defeat the field-bound check. Now every credential-shaped resolved value
   is scanned.
3. PROVIDER-HEALTH uses payment_config.get_config_redacted() (the existing helper), not a
   hand-rolled SELECT against non-existent *_set columns.
4. VERIFY FAILURE with a secret configured is LOUD (log_crit, not log).

Run via: PYTHONPATH="src:.venv/lib64/python3.14/site-packages" python3 tools/fltest.py \\
         --bug BILLING-AUTHZ-REGRESSION --test tests/test_billing_ops_authz_regression.py
"""

from __future__ import annotations

import importlib
import inspect
from unittest import mock

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# FIX 1 — the action routes carry the fine-grained Depends (source-level proof)
# ──────────────────────────────────────────────────────────────────────────────

def _route_gates(route) -> set[str]:
    """Pull the set of dependency callable names attached to a FastAPI route.

    Walks BOTH ``route.dependant.dependencies`` (FastAPI's sub-dependants from Depends()) AND
    ``route.dependencies`` (router-level). Robust across FastAPI versions by checking both."""
    names: set[str] = set()
    main_dep = getattr(route, "dependant", None)
    for dep in (getattr(main_dep, "dependencies", None) or []):
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    for dep in (getattr(route, "dependencies", None) or []):
        call = getattr(dep, "dependency", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    return names


def _route_gate_names_from_source(route) -> set[str]:
    """Inspect the route handler's source for Depends(...) callables (version-robust fallback)."""
    handler = getattr(route, "endpoint", None) or getattr(route, "app", None)
    try:
        src = inspect.getsource(handler)
    except (OSError, TypeError):
        return set()
    import re
    return set(re.findall(r"Depends\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", src))


def test_refund_action_route_enforces_can_issue_refunds():
    """The /admin/billing-ops/refund POST MUST carry require_can_issue_refunds.

    Without it, any operator can curl a refund even when the dashboard hides the button —
    the permission regime is advisory on the dashboard surface and unenforced on the
    money-moving surface, which is exactly the bug the critic caught."""
    from saas.stripe_routes import router
    refund_routes = [r for r in router.routes
                     if getattr(r, "path", "") == "/admin/billing-ops/refund"
                     and "POST" in (getattr(r, "methods", None) or set())]
    assert refund_routes, "no POST /admin/billing-ops/refund route registered"
    dep_names = _route_gates(refund_routes[0]) | _route_gate_names_from_source(refund_routes[0])
    assert "require_can_issue_refunds" in dep_names, (
        f"/admin/billing-ops/refund is missing the fine-grained can_issue_refunds gate "
        f"(deps seen: {dep_names}). A control hidden in the UI MUST also be refused by the "
        f"route — re-introducing this gap is a money-moving authorization bypass.")


def test_register_webhook_action_route_enforces_can_issue_refunds():
    """The /admin/billing-ops/register-webhook POST MUST carry require_can_issue_refunds.

    Registering a webhook endpoint mints a new signing secret via the Stripe API — a
    credential-using operation. Riding the same destructive-verb tier as refunds is the
    documented gate; it must be enforced on the route, not just on the dashboard button."""
    from saas.stripe_routes import router
    routes = [r for r in router.routes
              if getattr(r, "path", "") == "/admin/billing-ops/register-webhook"
              and "POST" in (getattr(r, "methods", None) or set())]
    assert routes, "no POST /admin/billing-ops/register-webhook route registered"
    dep_names = _route_gates(routes[0]) | _route_gate_names_from_source(routes[0])
    assert "require_can_issue_refunds" in dep_names, (
        f"/admin/billing-ops/register-webhook is missing the fine-grained gate "
        f"(deps seen: {dep_names}).")


def test_the_two_routes_do_not_drift_apart_on_the_gate():
    """The action route + the dashboard route MUST import the SAME dependency callable.

    The lesson from this gap: splitting a domain across two routers lets the gates drift.
    Importing require_can_issue_refunds from billing_dashboard_routes into stripe_routes
    means a single definition serves both — drift is impossible. Assert the __module__ matches."""
    from saas import stripe_routes, billing_dashboard_routes
    assert (stripe_routes.require_can_issue_refunds
            is billing_dashboard_routes.require_can_issue_refunds), (
        "stripe_routes re-defined require_can_issue_refunds instead of importing it — "
        "the two surfaces can now drift. Import the single definition.")


# ──────────────────────────────────────────────────────────────────────────────
# FIX 2 — live-key refuse scans every credential-shaped value (defeats field swap)
# ──────────────────────────────────────────────────────────────────────────────

def _crit_stderr(capsys):
    return capsys.readouterr().err


def test_live_key_detection_is_by_value_across_every_field(capsys):
    """⚠️ INVERTED 2026-08-13, NOT DELETED — the same treatment the four sandbox-refuse tests in
    tests/test_stripe_adapter.py received, in the file that pass missed.

    This used to assert that a live credential RAISED ValueError. The owner deliberately lifted
    the sandbox-only refuse once the paid flow was finished, so the assertion was correctly red.
    Deleting it would have been wrong: the valuable half of this test was never the refusal, it
    was the BY-VALUE scan. The original bug was that detection was FIELD-NAME-BOUND and a swap
    into the 'wrong' column defeated it. That defect is still available to be re-introduced, and
    it now manifests as a live adapter coming up SILENTLY instead of announcing.

    So the subject is unchanged and the expectation flips: construct, and announce at CRIT.
    """
    from saas.payments_stripe import StripePaymentProvider
    config = {
        "provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
        "api_key": "pk_test_THISISAPUBLISHABLEKEY",   # publishable in api_key slot
        "api_secret": "sk_live_SECRETKEYHERE",        # LIVE secret in api_secret slot
        "webhook_secret": "whsec_test_xxx",
    }
    StripePaymentProvider(config=config)
    err = _crit_stderr(capsys)
    assert "LIVE MODE" in err, "a live credential must never come up quietly"
    assert "live_credential_present=True" in err
    assert "sk_live_SECRETKEYHERE" not in err, "the announce must not echo the key"


def test_a_restricted_live_key_is_detected_too(capsys):
    """rk_live_ is a LIVE credential and was missed by the first cut of the announce, which only
    matched sk_live_. Found on production: the owner's key is restricted, so the adapter reported
    itself as not-live while charging real cards. Cheap to regress, expensive to miss."""
    from saas.payments_stripe import StripePaymentProvider
    config = {
        "provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
        "api_key": "pk_test_PUBLISHABLE",
        "api_secret": "rk_live_RESTRICTEDLIVEKEY",
        "webhook_secret": "whsec_test_xxx",
    }
    StripePaymentProvider(config=config)
    err = _crit_stderr(capsys)
    assert "live_credential_present=True" in err, (
        "rk_live_ (restricted live key) must count as a live credential")
    assert "rk_live_RESTRICTEDLIVEKEY" not in err


def test_pure_test_keys_still_construct():
    """Sanity: all-test-key config still constructs cleanly (the scan does not over-block)."""
    from saas.payments_stripe import StripePaymentProvider
    config = {
        "provider": "stripe", "mode": "test", "configured": True, "currency": "CAD",
        "api_key": "pk_test_NORMALPUBLISHABLE",
        "api_secret": "sk_test_NORMALSECRET",
        "webhook_secret": "whsec_test_NORMALSECRET",
    }
    adapter = StripePaymentProvider(config=config)
    assert adapter.is_configured() is True, "pure test-key config should be fully configured"


# ──────────────────────────────────────────────────────────────────────────────
# FIX 3 — provider-health does NOT select non-existent *_set columns
# ──────────────────────────────────────────────────────────────────────────────

def test_provider_health_source_does_not_select_set_columns_directly():
    """The hand-rolled SELECT against api_key_set/webhook_secret_set was a silent fail.
    The fix uses payment_config.get_config_redacted(). Assert the source no longer mentions
    the broken column names in a SELECT against payment_config."""
    src = inspect.getsource(__import__("saas.billing_dashboard_routes", fromlist=["x"]).__dict__.get(
        "_provider_health", lambda: None))
    assert "api_key_set, webhook_secret_set" not in src, (
        "_provider_health still selects the non-existent api_key_set/webhook_secret_set "
        "columns — this raises UndefinedColumn, is swallowed by the bare except, and makes "
        "provider-health silently report 'not configured' forever.")
    assert "get_config_redacted" in src, (
        "_provider_health should read via payment_config.get_config_redacted() (the helper "
        "that returns the masked *_set booleans correctly).")


# ──────────────────────────────────────────────────────────────────────────────
# FIX 4 — verify failure with secret configured is LOUD (log_crit, not log)
# ──────────────────────────────────────────────────────────────────────────────

def test_verify_failure_with_secret_configured_logs_crit(monkeypatch, capsys):
    """A verify failure when a secret IS configured must log_crit (loud), not _log (info).

    The owner's rule: 'never silently drop a money event.' A forgery is not silent if it
    log_crits. The rotation-race case (valid event, old secret) is the one that matters —
    the log_crit is the visibility floor until secret-overlap rotation lands."""
    from saas import stripe_webhooks
    # Mock stripe.Webhook.construct_event to raise (simulating verify failure).
    class _FakeSignatureError(Exception):
        pass
    fake_stripe = mock.MagicMock()
    fake_stripe.Webhook.construct_event.side_effect = _FakeSignatureError("verify failed")
    fake_stripe.error = mock.MagicMock()
    monkeypatch.setattr(stripe_webhooks, "_stripe", fake_stripe)
    # Mock payment_config.resolve_config to return a configured secret.
    fake_pc = mock.MagicMock()
    fake_pc.resolve_config.return_value = {
        "provider": "stripe", "mode": "test", "configured": True,
        "webhook_secret": "whsec_test_configuredsecret",
    }
    monkeypatch.setitem(__import__("sys").modules, "saas.payment_config", fake_pc)
    # Patch the lazy import inside verify_and_record.
    monkeypatch.setattr("saas.payment_config", fake_pc, raising=False)

    ok, meta = stripe_webhooks.verify_and_record(b"{}", "t=1,v1=fake")
    assert ok is False, "verify failure must return (False, meta)"
    # verify_and_record now returns a meta dict (reason + http_status) so the route can map
    # the failure to the right HTTP status. A forgery → 400.
    assert isinstance(meta, dict) and meta.get("reason") == "forgery", f"expected forgery meta, got {meta}"
    assert meta.get("http_status") == 400
    captured = capsys.readouterr()
    assert "CRIT" in captured.err, (
        f"a verify failure with a secret configured must log_crit (loud), not _log (info). "
        f"stderr was: {captured.err!r}")
