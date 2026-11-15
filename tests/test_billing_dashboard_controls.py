"""Operator billing console — every offered control must actually DO something.

WHY THIS FILE EXISTS (2026-08-13, found while fixing the live webhook endpoint).

``⟳ register webhook`` was rendered into the billing console, un-hidden by the permissions
toggle, and NOTHING was ever bound to it. The ``POST /admin/billing/register-webhook`` route
existed, worked, and was simply unreachable from the UI. Clicking did nothing at all.

That is worse than an absent button. An operator who clicks an enabled control and sees no change
concludes THE ACTION FAILED — that Stripe rejected it, that the key is wrong — and starts
debugging the wrong system. It cost real time here, at the worst moment: mid-launch, with the
production webhook endpoint on an API version whose payloads our handlers cannot fully read, and
this button being the documented remedy.

It is also invisible to every other kind of test. The route has unit tests and they pass. The HTML
contains the button and any markup assertion passes. Only the JOIN between them was missing, and
nothing asserted the join.

So the invariant here is structural and covers the whole class: any button the console reveals
must have a click handler bound to it.
"""

import re
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[1] / "saas" / "dashboard" / "billing.js"

# The house wiring pattern is two adjacent statements:
#     var b = document.getElementById('sb-x-btn');
#     if (b && !b.dataset.wired) { b.dataset.wired = '1'; b.addEventListener('click', fn); }
# so the handler binding follows the lookup closely. Allow generous slack for formatting without
# letting an unrelated, far-away addEventListener count as wiring for this button.
_WIRING_WINDOW = 400


def _js() -> str:
    assert _JS.is_file(), f"missing {_JS}"
    return _JS.read_text(encoding="utf-8")


def _button_ids(src: str) -> set:
    """Every button the console renders.

    Matched on the <button> tag itself rather than an id convention: only 3 of the 9 controls
    carry the ``-btn`` suffix, so a convention-based sweep silently skipped two thirds of them —
    including whole feature areas. Scoping a guard by naming convention makes it as reliable as
    the convention, which is to say not."""
    return set(re.findall(r"<button[^>]*id=\"([a-z0-9-]+)\"", src))


def _is_wired(src: str, button_id: str) -> bool:
    """True iff a getElementById for this id is followed closely by an addEventListener."""
    for m in re.finditer(r"getElementById\(['\"]%s['\"]\)" % re.escape(button_id), src):
        if "addEventListener" in src[m.end():m.end() + _WIRING_WINDOW]:
            return True
    return False


def test_the_console_renders_buttons_at_all():
    """Guard the guard: if the id convention changes, the sweep below would silently pass over an
    empty set and assert nothing."""
    ids = _button_ids(_js())
    assert len(ids) >= 8, f"expected the console's full control set, found {sorted(ids)}"


def test_every_rendered_button_has_a_click_handler():
    """THE INVARIANT. A control that renders and does nothing sends the operator to debug the
    wrong system."""
    src = _js()
    dead = sorted(b for b in _button_ids(src) if not _is_wired(src, b))
    assert not dead, (
        f"these buttons render but have no click handler bound: {dead} — either wire them or "
        f"stop rendering them; an enabled control that does nothing reads as a FAILED ACTION")


def test_the_register_webhook_button_specifically_is_wired():
    """Names the actual bug, so a regression is legible rather than a list diff.

    This is the ONLY route to an endpoint pinned to the API version our handlers parse — the
    Stripe Dashboard's own create form offers only the account default and the current preview."""
    src = _js()
    assert "sb-register-webhook-btn" in src, "the button was removed rather than wired"
    assert _is_wired(src, "sb-register-webhook-btn"), (
        "⟳ register webhook renders but nothing is bound to it — this was the live bug")
    assert "register-webhook" in src, "no call to POST /admin/billing/register-webhook"


def test_registration_failure_to_store_the_secret_is_not_reported_as_success():
    """The route can return ok with secret_persisted=false: the endpoint EXISTS at Stripe but we
    cannot verify its deliveries. Reporting that as success would leave the operator believing
    webhooks are healthy while every delivery fails signature verification."""
    src = _js()
    assert "secret_persisted" in src, "the UI ignores whether the signing secret was stored"
    # The unhappy branch must be reachable BEFORE the success message is set.
    idx_check = src.index("secret_persisted")
    idx_ok = src.index("signing secret stored")
    assert idx_check < idx_ok, "success is reported before secret_persisted is checked"


# ── every element the JS reaches for must actually exist ──────────────────────────

# Ids the module deliberately does NOT render because it renders INTO them. Owned by
# index.html / billing.html per this module's docstring.
_EXTERNALLY_OWNED_IDS = {"stripe-billing-panel"}


def test_every_id_the_js_looks_up_is_rendered_somewhere():
    """THE INVARIANT THE WIRING TEST COULD NOT SEE, and the one that mattered.

    ``renderProviderHealth()`` opens with:

        var box = document.getElementById('sb-provider-health');
        if (!box) return;

    and EVERYTHING below it — the whole health card's values AND the click wiring for both
    ⚡ test connection and ⟳ register webhook — sat under that guard. The id was never in the
    markup, so the guard returned on every single call. The card read "—" for its entire life
    and both buttons rendered, enabled, and did nothing.

    Note what this defeats that the sibling wiring test does not: the wiring was PRESENT in the
    source and textually correct, so a source-reading check passes while the runtime never
    reaches the line. A presence guard on an element nobody renders is not a guard, it is an
    off switch — and it fails silently, because a missing element is indistinguishable from
    "this panel isn't on screen right now".
    """
    src = _js()
    looked_up = set(re.findall(r"getElementById\(['\"]([a-zA-Z0-9_-]+)['\"]\)", src))
    rendered = set(re.findall(r"id=\"([a-zA-Z0-9_-]+)\"", src))
    assert looked_up, "no getElementById calls found — the extraction broke, not the code"
    missing = sorted(looked_up - rendered - _EXTERNALLY_OWNED_IDS)
    assert not missing, (
        f"the JS looks up ids that this module never renders: {missing} — either add them to "
        f"shellHtml() or stop reaching for them. Any guarded early-return on one of these "
        f"silently disables everything below it.")


def test_the_provider_health_container_exists():
    """Names the specific defect so a regression is legible rather than a set difference."""
    src = _js()
    assert 'id="sb-provider-health"' in src, (
        "sb-provider-health is missing from shellHtml() — renderProviderHealth() returns early "
        "and BOTH buttons plus the whole health card go dead. This was the live bug.")
    # And the guard it feeds must still be the reason it matters.
    assert "getElementById('sb-provider-health')" in src


# ── permission parity ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("perms,allowed", [
    ({"can_register_webhook": True, "can_issue_refunds": False}, True),
    ({"can_register_webhook": False, "can_issue_refunds": True}, True),
    ({"can_register_webhook": True, "can_issue_refunds": True}, True),
    ({"can_register_webhook": False, "can_issue_refunds": False}, False),
])
def test_register_webhook_grant_matches_the_button_that_offers_it(monkeypatch, perms, allowed):
    """The console reveals the button on (can_register_webhook OR can_issue_refunds); the route
    used to require refunds ALONE. An operator holding only the dedicated grant was shown a
    control that answered 403 — the grant existed and could not be used.

    Refunds must keep working, or this 'fix' silently removes access from every operator who has
    it today."""
    from fastapi import HTTPException
    from saas import billing_dashboard_routes as r

    monkeypatch.setattr(r, "resolve_operator_perms", lambda *a, **k: perms)
    if allowed:
        assert r.require_can_register_webhook(None, None) == perms
    else:
        with pytest.raises(HTTPException) as ei:
            r.require_can_register_webhook(None, None)
        assert ei.value.status_code == 403


def test_the_route_uses_that_dependency():
    """Pin the wiring itself — a correct dependency that nothing depends ON is not a gate."""
    import inspect
    from saas import billing_dashboard_routes as r
    src = inspect.getsource(r.billing_register_webhook)
    assert "require_can_register_webhook" in src, (
        "billing_register_webhook no longer depends on require_can_register_webhook")
