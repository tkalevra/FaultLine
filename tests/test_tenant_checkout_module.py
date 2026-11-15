"""saas/tenant_checkout.py — the tenant terms-acceptance + Stripe-Checkout module.

WHAT THIS SUITE IS ACTUALLY GUARDING (the ORDER, not merely the happy path):

  * the tier catalog's SHAPE — the routes + frontend agents build to those exact keys, and the
    'enterprise' tier → account_kind 'tenant' mismatch is a real trap that a rename would hide;
  * an INVALID TIER writes NO contract row and makes NO Stripe call;
  * a FAILED record_acceptance makes NO Stripe call — this is THE gate;
  * NO ACTIVE TERMS refuses before either write;
  * a SUCCESSFUL flow writes the row with the exact terms snapshot, the signer facts and the
    agreed price BEFORE any checkout url comes back — asserted by ORDER OF CALLS, not just by
    the final state, because "the row exists afterwards" cannot distinguish before from after;
  * a Stripe failure leaves an HONEST ORPHAN and returns ok=False with no url;
  * a linkage failure still returns the url (contract_id rides in the metadata) and is loud.

Two layers, deliberately:
  * MODULE tests monkeypatch saas.contracts / saas.payments — no DB, no Stripe, always run.
  * DB tests (marked, POSTGRES_DSN-gated) apply migration 243 into a THROWAWAY schema and drive
    the REAL contracts.py SQL against real PostgreSQL — because the append-only trigger, the
    write-once linkage and the COALESCE false-success are properties of the DATABASE and a mock
    cannot falsify them.

NO real Stripe call is made anywhere in this file.
"""

import os
import re
import sys
import uuid
from pathlib import Path

import pytest

from saas import tenant_checkout as tc


REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "migrations" / "243_contract_infrastructure.sql"


# ══════════════════════════════════════════════════════════════════════════════════
# 1. TIER CATALOG SHAPE
# ══════════════════════════════════════════════════════════════════════════════════

_REQUIRED_KEYS = {"tier", "name", "amount_cents", "currency", "interval", "price_id",
                  "seats_included", "blurb", "features",
                  # SEAT ECONOMICS — resolved from the billing engine, rendered by subscribe.js.
                  # A customer must be able to see what a seat costs BEFORE signing.
                  "seat_cap", "seat_prices", "seat_currency"}


def test_catalog_is_ordered_team_then_enterprise():
    rows = tc.tier_catalog()
    assert [r["tier"] for r in rows] == ["team", "enterprise"]


def test_every_tier_carries_the_agreed_contract_keys():
    """The routes + frontend agents build to these key names. A rename breaks both."""
    for row in tc.tier_catalog():
        missing = _REQUIRED_KEYS - set(row)
        assert not missing, f"{row.get('tier')} is missing {sorted(missing)}"


def test_tier_prices_are_the_real_amounts():
    assert tc.get_tier("team")["amount_cents"] == 75000        # CA$750.00/mo
    assert tc.get_tier("enterprise")["amount_cents"] == 150000  # CA$1500.00/mo
    for row in tc.tier_catalog():
        assert row["currency"] == "CAD"
        assert row["interval"] == "month"


def test_seats_included_comes_from_the_BILLER_not_a_literal():
    """🔴 THE CARD AND THE INVOICE MUST AGREE — they did not, and this test asserted the wrong
    number so it stayed green while the page misled customers.

    The old assertion was ``team seats_included == 0`` with the comment "Team has NO base
    package". That is contradicted by the code that actually produces the bill:
    ``entitlements.TEAM_INCLUDED_STANDARD == 2`` and ``_team_billing_view`` charges Standard
    seats only BEYOND that allotment. So the pricing card told a customer that CA$750/mo bought
    them ZERO seats while the biller included two.

    The values are now READ from the billing engine rather than restated here, so this test
    cannot drift from the invoice again — if an operator re-prices via ``SAAS_PRICE_*``, both
    sides move together."""
    from saas import entitlements as ent
    assert tc.get_tier("team")["seats_included"] == ent.included_standard_for("team")
    assert tc.get_tier("enterprise")["seats_included"] == ent.included_standard_for("tenant")
    # …and the values are the real ones, not an accidental None == None.
    assert tc.get_tier("team")["seats_included"] == 2
    assert tc.get_tier("enterprise")["seats_included"] == 5


def test_the_price_of_an_extra_seat_is_disclosed_and_matches_the_biller():
    """A customer must not be able to read CA$750/mo as "covers my users". §2 of the agreement
    says "You choose a plan with a number of seats", so the per-seat cost has to be on the page.
    Every rate is the biller's own number (``entitlements.price_for``), never a literal here."""
    from saas import entitlements as ent
    for tier_key, kind in (("team", "team"), ("enterprise", "tenant")):
        row = tc.get_tier(tier_key)
        rates = {r["seat_type"]: r["unit_price"] for r in row["seat_prices"]}
        assert rates, f"{tier_key} discloses no per-seat price at all"
        for seat_type, price in rates.items():
            assert price == ent.price_for(seat_type, kind), (
                f"{tier_key}/{seat_type} card price {price} != biller "
                f"{ent.price_for(seat_type, kind)}")
        assert set(rates) == set(ent.SEAT_TYPES)
        assert row["seat_currency"] == ent.CURRENCY


def test_the_team_seat_ceiling_is_disclosed_and_enterprise_has_none():
    """The 15-seat hard cap is a TEAM property enforced by seats.py::register_seat. Enterprise
    has no fixed tier ceiling, so it must not claim one."""
    from saas import entitlements as ent
    assert tc.get_tier("team")["seat_cap"] == ent.TEAM_SEAT_CAP == 15
    assert tc.get_tier("enterprise")["seat_cap"] is None


def test_enterprise_tier_maps_to_account_kind_tenant():
    """⚠️ THE TRAP: the buyer-facing tier 'enterprise' is stored as account_kind 'tenant'.

    Deriving the kind from the tier string would create an Enterprise buyer as a 15-seat-capped
    Team account. If this goes red because account_kind was dropped or 'normalised' to match the
    tier key, read saas/accounts.py ACCOUNT_KINDS first — 'enterprise' is not a valid kind."""
    assert tc.get_tier("enterprise")["account_kind"] == "tenant"
    assert tc.get_tier("team")["account_kind"] == "team"


def test_price_ids_resolve_from_plans():
    from saas import plans
    for key in ("team", "enterprise"):
        assert tc.get_tier(key)["price_id"] == plans.TENANT_STRIPE_PRICE_IDS[key]
        assert str(tc.get_tier(key)["price_id"]).startswith("price_")


def test_features_are_concrete_checkable_strings():
    for row in tc.tier_catalog():
        assert isinstance(row["features"], list) and len(row["features"]) >= 3
        for f in row["features"]:
            assert isinstance(f, str) and f.strip()


def test_no_feature_string_claims_a_capability_that_does_not_EXIST():
    """🔴 THE PRICING CARD MUST NOT ADVERTISE SOMETHING THE CODEBASE CANNOT DO.

    The Team card shipped "Self-serve signup and cancellation". There is NO cancellation
    capability anywhere in saas/: zero cancel routes, zero Stripe cancel calls; every
    ``cancel_at_period_end`` occurrence is a READ of a webhook snapshot. And it contradicted
    ``subscribe.html`` ON THE SAME SCREEN, which correctly says "There is no self-serve cancel
    button in the console today."

    This test is a TRIPWIRE, not a proof of truth — it cannot verify prose. It re-checks the
    GROUND FACT (does a cancel write path exist?) and requires the copy to match it.

    ⚠️ UPDATED 2026-08-13 — CANCELLATION WAS BUILT, AND THE WIRE FIRED EXACTLY AS ITS AUTHOR
    INTENDED. The old text said "if cancellation is ever built, delete this test". Deleting it
    would have been the wrong move: the guard is not "don't claim cancellation", it is "the copy
    and the capability must agree", and that has TWO failure directions. Once the route existed
    (`POST /tenant/cancel`), the page still said "there is no self-serve cancel button in the
    console today" — understating the product and sending customers to email for something the
    console now does in one click. Same false-claim class, opposite sign. So the wire INVERTS
    rather than retiring, and it will fire again if either side moves without the other."""
    import re as _re
    from pathlib import Path as _Path

    features = [f.lower() for row in tc.tier_catalog() for f in row["features"]]

    # 1. The ground fact: is there a cancel WRITE path yet?
    saas_dir = _Path(tc.__file__).resolve().parent
    cancel_writes = []
    for py in sorted(saas_dir.glob("*.py")):
        for i, line in enumerate(py.read_text(errors="ignore").splitlines(), 1):
            if _re.search(r"@router\.(post|delete)\([^)]*cancel", line) or \
               _re.search(r"\bsubscriptions?\.(cancel|delete)\s*\(", line) or \
               _re.search(r"\bdef\s+\w*cancel_subscription\w*\s*\(", line):
                cancel_writes.append(f"{py.name}:{i}")

    # 2. The claim must not outrun the capability.
    #    A NEGATED mention ("there is NO self-serve cancel button") is the honest disclosure we
    #    want and must not trip the wire — only an AFFIRMATIVE claim of a self-serve cancel
    #    capability counts. This distinction is the whole point: the fix for the bad string was
    #    to tell the truth about cancelling, not to stop mentioning it.
    def _claims_self_serve_cancel(f: str) -> bool:
        negated = ("no self-serve cancel" in f or "there is no" in f)
        if negated:
            return False
        return bool(_re.search(r"self-serve[^.]*\bcancel", f)
                    or _re.search(r"\bcancel(lation)?\b[^.]*\bin the console\b", f)
                    or "self-serve cancellation" in f)

    claims_cancel = [f for f in features if _claims_self_serve_cancel(f)]
    page = (saas_dir / "dashboard" / "subscribe.html").read_text(errors="ignore").lower()
    disowns_console = "no self-serve cancel button in the console today" in page

    if not cancel_writes:
        # The original direction: the claim must not outrun the capability.
        assert not claims_cancel, (
            "a pricing card advertises self-serve cancellation but there is NO cancel write "
            f"path in saas/ — offending feature strings: {claims_cancel}")
        assert disowns_console, (
            "there is no cancel write path, so the page MUST still disown the console button")
    else:
        # The capability now exists. The wire does NOT become obsolete — it inverts, because the
        # opposite lie is just as damaging: a page that disowns a button the customer can see is
        # the same false-claim class pointing the other way, and it would send them to email for
        # something the console does in one click.
        assert not disowns_console, (
            f"a cancel write path EXISTS ({cancel_writes}) but subscribe.html still says there "
            "is no self-serve cancel button — the page now understates the product")
        for f in features:
            if "cancel" in f:
                assert "no self-serve" not in f, (
                    f"feature string {f!r} denies a capability that now exists at {cancel_writes}")


def test_catalog_returns_copies_not_the_live_catalog():
    """A caller mutating a returned row must not be able to edit the amount recorded on the
    NEXT signed contract."""
    row = tc.get_tier("team")
    row["amount_cents"] = 1
    row["features"].append("free ponies")
    assert tc.get_tier("team")["amount_cents"] == 75000
    assert "free ponies" not in tc.get_tier("team")["features"]


def test_unknown_tier_is_none():
    for bad in ("", None, "tenant", "TEAMS", "individual", "standard"):
        assert tc.get_tier(bad) is None


# ══════════════════════════════════════════════════════════════════════════════════
# 2. SIGNATURE VALIDATION
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", ["Christopher Thompson", "Ng", "Bo Li", "Zoë Müller",
                                  "Jean-Luc O'Brien", "李 明"])
def test_real_names_sign(name):
    ok, reason = tc.validate_signature(name)
    assert ok, f"{name!r} refused: {reason}"


@pytest.mark.parametrize("name", ["", "   ", None, "x", "...", "---", "___", "  .  ", "!!!"])
def test_non_signatures_are_refused(name):
    ok, _ = tc.validate_signature(name)
    assert not ok


def test_over_long_name_is_refused_not_silently_truncated():
    ok, _ = tc.validate_signature("a" * 201)
    assert not ok, "a signature we would truncate is not the signature they gave"


def test_brand_impersonation_reuses_the_existing_guard():
    """Delegated to saas/name_guard.check_display_name — not re-implemented here."""
    ok, _ = tc.validate_signature("Microsoft")
    assert not ok


# ══════════════════════════════════════════════════════════════════════════════════
# 3. THE FLOW — module level, contracts + payments monkeypatched
# ══════════════════════════════════════════════════════════════════════════════════

TERMS = {"id": 7, "version": "2026-08-12-v1", "title": "FaultLine Subscription Agreement",
         "body_markdown": "## Terms\n\nThe EXACT words shown.\n", "effective_at": None,
         "created_by": "system-seed"}
# The DOCUMENT FINGERPRINT (migration 252), derived with the SHIPPED function rather than written
# as a literal — a fixture that states its own opinion of the hash pins the fixture, not the code.
from saas.contracts import body_fingerprint as _fp   # noqa: E402
TERMS["body_sha256"] = _fp(TERMS["body_markdown"])


class _Recorder:
    """Records the ORDER of every call across both collaborators."""

    def __init__(self):
        self.calls: list[str] = []
        self.acceptances: list[dict] = []
        self.checkouts: list[dict] = []
        self.links: list[tuple] = []
        self.blocked: dict | None = None   # set to make the duplicate guard fire


class _FakeProvider:
    """Stand-in for the Stripe adapter. Never touches the network."""

    name = "stripe-fake"

    def __init__(self, rec, *, result=None, configured=True):
        self._rec = rec
        self._result = result
        self._configured = configured

    def is_configured(self):
        # The flow now checks this BEFORE writing a contract (see _provider_accepts / step 2),
        # so a double that omits it would refuse everything and every test would pass for the
        # wrong reason.
        self._rec.calls.append("is_configured")
        return self._configured

    def create_checkout(self, **kw):
        self._rec.calls.append("create_checkout")
        self._rec.checkouts.append(kw)
        if self._result is not None:
            return self._result
        return {"status": "checkout_created", "provider": self.name, "configured": True,
                "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_FAKE",
                "session_id": "cs_test_FAKE", "charged": False, "entitlement_changed": False}


@pytest.fixture
def wired(monkeypatch):
    """tenant_checkout wired to in-memory collaborators. Returns the recorder."""
    rec = _Recorder()

    def _get_current_terms():
        rec.calls.append("get_current_terms")
        return dict(TERMS)

    def _record_acceptance(**kw):
        rec.calls.append("record_acceptance")
        rec.acceptances.append(kw)
        return 4242

    def _record_acceptance_guarded(**kw):
        """The GUARDED writer the flow actually calls now. Returns the documented dict shape.

        ``rec.blocked`` lets a test make the duplicate guard fire without a database."""
        rec.calls.append("record_acceptance")
        rec.acceptances.append(kw)
        if rec.blocked is not None:
            return rec.blocked
        return {"ok": True, "contract_id": 4242, "blocked": False, "blocking": None,
                "status": "recorded", "reason": ""}

    def _link(cid, sid):
        rec.calls.append("link_checkout_session")
        rec.links.append((cid, sid))
        return True

    monkeypatch.setattr(tc._contracts, "get_current_terms", _get_current_terms)
    monkeypatch.setattr(tc._contracts, "record_acceptance", _record_acceptance)
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded", _record_acceptance_guarded)
    monkeypatch.setattr(tc._contracts, "link_checkout_session", _link)
    monkeypatch.setattr(tc._payments, "get_payment_provider",
                        lambda: _FakeProvider(rec))
    return rec


def _begin(**over):
    """A subscribe as a REAL CLIENT makes it — echoing the document it rendered.

    ⚠️ THE THREE BINDING TOKENS ARE IN THE DEFAULT ARGUMENTS ON PURPOSE. They used to be absent,
    which meant ~100 tests in this file exercised a flow that had opted out of the terms binding
    entirely, and the suite could not have noticed if the binding stopped applying to real
    traffic. ``begin_subscription`` now REFUSES a caller that cannot say which document it
    rendered (there is no opt-out), so a helper that omits them is not a lenient helper — it is a
    helper that tests nothing."""
    kw = dict(tier="team", signer_email="Buyer@Example.COM", legal_name="Christopher Thompson",
              signer_ip="203.0.113.9", signer_user_agent="Mozilla/5.0 (test)",
              expected_template_id=TERMS["id"], expected_version=TERMS["version"],
              expected_sha256=TERMS["body_sha256"])
    kw.update(over)
    return tc.begin_subscription(**kw)


# ── the return shape is a contract with the routes agent ──────────────────────────

_RETURN_KEYS = {"ok", "status", "contract_id", "checkout_url", "session_id", "tier",
                "amount_cents", "currency", "contract_version",
                # ADDED with migration 252's document binding. Populated ONLY on the two
                # stale-document refusals (terms_changed / terms_version_stale), where the page
                # must re-render the current agreement and must NOT go fetch it itself — a
                # client-issued re-GET is a third read of a mutable row and can land on yet
                # another version, i.e. the same race one turn later. Present-and-None everywhere
                # else so the shape stays identical on success and on every refusal, which is what
                # this constant is for.
                "terms",
                "reason",
                # ADDED with the honest-refusal work. ``signed`` answers the one question a
                # customer interrupted mid-signature actually has — does a permanent
                # signed_contracts row exist for this call — and it is DERIVED from contract_id
                # so the two can never disagree. ``charged`` is False on every return this
                # function can make (it returns at most a Checkout Session url; money moves
                # later, at Stripe, and is only recognised by the signed webhook).
                # ``provider_status`` carries the payment ADAPTER's own status word, which used
                # to be promoted to ``status`` and thereby fell through the transport's status
                # map into an accidental 502 — see the step-4 comment.
                "signed", "charged", "provider_status"}


def test_return_shape_is_identical_on_success_and_on_every_refusal(wired):
    out = _begin()
    assert set(out) == _RETURN_KEYS
    assert set(_begin(tier="nope")) == _RETURN_KEYS
    assert set(_begin(legal_name="")) == _RETURN_KEYS
    assert set(_begin(signer_email="nope")) == _RETURN_KEYS


# ── invalid tier: NO contract, NO Stripe ──────────────────────────────────────────

def test_bad_tier_writes_no_contract_and_calls_no_stripe(wired):
    out = _begin(tier="enterprize")
    assert out["ok"] is False and out["status"] == "invalid_tier"
    assert out["checkout_url"] is None and out["session_id"] is None
    assert out["contract_id"] is None
    assert wired.calls == [], f"nothing may be called for an unknown tier, got {wired.calls}"


def test_bad_signature_writes_no_contract_and_calls_no_stripe(wired):
    out = _begin(legal_name="   ")
    assert out["ok"] is False and out["status"] == "invalid_signature"
    assert wired.calls == []


def test_bad_email_writes_no_contract_and_calls_no_stripe(wired):
    out = _begin(signer_email="not-an-email")
    assert out["ok"] is False and out["status"] == "invalid_email"
    assert wired.calls == []


# ── THE GATE: acceptance write fails → no Stripe call at all ──────────────────────

def test_failed_acceptance_makes_no_stripe_call(wired, monkeypatch):
    """A customer who reaches Stripe Checkout without a signed contract row must get nothing."""
    def _fails(**kw):
        wired.calls.append("record_acceptance")
        return {"ok": False, "contract_id": None, "blocked": False, "blocking": None,
                "status": "write_failed", "reason": ""}
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded", _fails)

    out = _begin()
    assert out["ok"] is False
    assert out["status"] == "contract_write_failed"
    assert out["checkout_url"] is None and out["session_id"] is None
    assert "create_checkout" not in wired.calls, "THE GATE LEAKED — Stripe was called with no contract"


def test_acceptance_raising_is_caught_and_still_makes_no_stripe_call(wired, monkeypatch):
    def _boom(**kw):
        wired.calls.append("record_acceptance")
        raise RuntimeError("control plane down")
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded", _boom)

    out = _begin()
    assert out["ok"] is False and out["status"] == "contract_write_failed"
    assert "create_checkout" not in wired.calls


# ── no active terms: refuse before either write ───────────────────────────────────

def test_no_active_terms_refuses_before_any_write(wired, monkeypatch):
    monkeypatch.setattr(tc._contracts, "get_current_terms", lambda: None)
    out = _begin()
    assert out["ok"] is False and out["status"] == "no_active_terms"
    assert out["contract_id"] is None
    assert "record_acceptance" not in wired.calls
    assert "create_checkout" not in wired.calls


def test_no_price_id_configured_refuses_before_the_contract_write(wired, monkeypatch):
    from saas import plans
    monkeypatch.setitem(plans.TENANT_STRIPE_PRICE_IDS, "team", None)
    out = _begin(tier="team")
    assert out["ok"] is False and out["status"] == "invalid_tier"
    assert wired.calls == []


# ── M1: the provider is proven usable BEFORE a permanent row is written ───────────

def test_an_unconfigured_provider_refuses_BEFORE_writing_a_contract(wired, monkeypatch):
    """🔴 An unconfigured deployment used to mint a permanent orphan on EVERY attempt.

    The provider was resolved AFTER ``record_acceptance``, so the contract row already existed
    when the adapter turned out to be unusable. Since the row can never be deleted, junk
    accumulated in a financial audit table at exactly the rate customers tried to buy."""
    monkeypatch.setattr(tc._payments, "get_payment_provider",
                        lambda: _FakeProvider(wired, configured=False))
    out = _begin()
    assert out["ok"] is False and out["status"] == "payments_unavailable"
    assert out["contract_id"] is None and out["checkout_url"] is None
    assert "record_acceptance" not in wired.calls, "a contract was written for an unusable provider"
    assert wired.acceptances == []


def test_an_adapter_that_cannot_take_our_arguments_refuses_before_the_write(wired, monkeypatch):
    """The shipped default (``StubPaymentProvider``) has a NARROW signature — no
    ``customer_email`` / ``price_id`` / ``success_url`` / ``cancel_url`` / ``contract_id`` and no
    ``**kwargs`` — so calling it raises TypeError. That exception was caught downstream, AFTER
    the contract write, which is what produced the orphan. Measured before the fix:

        TypeError: StubPaymentProvider.create_checkout() got an unexpected keyword argument
                   'customer_email'
    """
    class _NarrowProvider:
        name = "narrow"

        def is_configured(self):
            return True

        def create_checkout(self, *, account_id, user_id, plan_id, amount,
                            currency="CAD", description=None):
            raise AssertionError("must never be called — the signature check should refuse first")

    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _NarrowProvider())
    out = _begin()
    assert out["ok"] is False and out["status"] == "payments_unavailable"
    assert wired.acceptances == [], "an orphan contract was written for an uncallable adapter"


def test_the_real_shipped_stub_is_refused_before_any_write(wired, monkeypatch):
    """Not a hand-built double — the ACTUAL default provider this repo ships with."""
    from saas import payments as _p
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _p.StubPaymentProvider())
    out = _begin()
    assert out["ok"] is False and out["status"] == "payments_unavailable"
    assert wired.acceptances == []


# ── S1: the binding between what was READ and what is SIGNED ──────────────────────

def test_a_matching_template_id_signs_normally(wired):
    """The binding must not break the happy path: the id the client rendered IS the active one."""
    out = _begin(expected_template_id=TERMS["id"], expected_version=TERMS["version"])
    assert out["ok"] is True and out["status"] == "checkout_created"
    assert wired.acceptances[0]["template_id"] == TERMS["id"]


def test_a_STALE_template_id_is_refused_and_writes_NOTHING(wired):
    """🔴 THE CRITIC'S DEMONSTRATION. The page rendered template 6; by POST time the active
    template is 7. The old code re-read ``get_current_terms()`` and cheerfully recorded the
    customer as having signed text they never saw. It must refuse, and write nothing."""
    out = _begin(expected_template_id=6, expected_version="2026-08-12-v0")
    assert out["ok"] is False and out["status"] == "terms_changed"
    assert out["contract_id"] is None and out["checkout_url"] is None
    assert wired.acceptances == [], "a contract was written against unseen terms"
    assert "create_checkout" not in wired.calls
    # The refusal must tell the customer nothing happened and to re-read.
    assert "nothing was signed" in out["reason"].lower()
    assert "reload" in out["reason"].lower()


@pytest.mark.parametrize("bad", ["not-an-int", None, "", 0, -1, 6.5])
def test_a_junk_template_id_never_passes_the_binding(wired, bad):
    """A non-matching id of ANY shape must refuse rather than fall through to "current".

    ⚠️ CHANGED — ``None`` NO LONGER PASSES, AND THE OLD BEHAVIOUR WAS THE HOLE.
    This test used to carve ``None`` out with the comment *"caller predates the binding — the
    module allows it; the HTTP route is what makes it mandatory"*. That is an optional binding,
    and an optional binding is not a binding: any caller that simply omitted the token got the
    pre-fix behaviour back, and the enforcement lived in a Pydantic schema (the transport) rather
    than in the module that owns the decision.

    Absence is now the STALE case — same cause, same fix, same answer as a mismatch — so it
    refuses with ``terms_version_stale`` and the customer is handed the current agreement to
    re-read. Enforcing it here is also what lets the transport answer a 409 with recoverable
    content instead of a bare 422 (see ARM 0 in ``begin_subscription``)."""
    out = _begin(expected_template_id=bad)
    assert out["ok"] is False, bad
    # None and "" are ABSENCE (the presence arm); every other junk value is a real
    # id that does not match (the id arm). Both refuse; they refuse for different
    # reasons and the status says which.
    absent = bad is None or (isinstance(bad, str) and not bad.strip())
    assert out["status"] == ("terms_version_stale" if absent else "terms_changed"), bad
    assert wired.acceptances == []


@pytest.mark.parametrize("omitted", ["expected_template_id", "expected_version",
                                     "expected_sha256"])
def test_omitting_ANY_binding_token_is_the_stale_case_and_writes_nothing(wired, omitted):
    """🔴 THE 422 DEAD END, CLOSED. A page that was already open when this shipped cannot echo
    tokens it was never served. Refusing it with a schema validation error names two fields the
    customer has never heard of and offers no way forward; refusing it as "your page is out of
    date, here is the current agreement" is the same refusal with a recovery path.

    Nothing is written on any of the three, and no Stripe call is made."""
    out = _begin(**{omitted: None})
    assert out["ok"] is False, omitted
    assert out["status"] == "terms_version_stale", (omitted, out)
    assert out["contract_id"] is None and out["checkout_url"] is None
    assert wired.acceptances == [], omitted
    assert "create_checkout" not in wired.calls
    # The refusal must carry the current agreement — that IS the recovery path.
    assert out["terms"] and out["terms"]["version"] == TERMS["version"]
    assert out["terms"]["body_markdown"] == TERMS["body_markdown"]
    assert "out of date" in out["reason"].lower()


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_BLANK_binding_token_is_treated_as_absent_not_as_a_value(wired, blank):
    """An empty string is not a fingerprint. Trimmed-empty counts as absent, or a client could
    satisfy the presence check with whitespace and then be compared against nothing."""
    out = _begin(expected_sha256=blank)
    assert out["ok"] is False and out["status"] == "terms_version_stale", blank
    assert wired.acceptances == []


# ── S3: the duplicate guard ───────────────────────────────────────────────────────

def test_the_dedupe_principal_is_the_ACCOUNT_when_there_is_one(wired):
    """Concurrent attempts must serialize on the ACCOUNT — the unit that gets billed."""
    _begin(account_id="acct-1", signer_user_id="user-1")
    assert wired.acceptances[0]["dedupe_principal"] == "acct-1"
    # 🔴 S1: ONE argument, written to the row AND used as the guard's account arm. It was two
    # (``dedupe_account_id`` guarded, nothing wrote the column) and the arm was therefore dead.
    assert wired.acceptances[0]["account_id"] == "acct-1"
    assert "dedupe_account_id" not in wired.acceptances[0], (
        "a second account parameter is back — the guard arm and the written column can drift "
        "apart again, which is exactly how S1 happened")


def test_the_dedupe_principal_falls_back_to_the_signer_before_an_account_exists(wired):
    """A tenant signup has no account until the webhook provisions one, so the signer is the
    only stable contention key at signature time."""
    _begin(account_id=None, signer_user_id="user-1")
    assert wired.acceptances[0]["dedupe_principal"] == "user-1"


@pytest.mark.parametrize("status,fragment", [
    ("already_subscribed", "active subscription"),
    ("duplicate_in_flight", "already open"),
])
def test_a_blocked_acceptance_returns_the_refusal_and_makes_NO_stripe_call(
        wired, status, fragment):
    """A duplicate must not reach Stripe. It also must not read as a failure of OUR system —
    the customer is told which agreement already covers them."""
    wired.blocked = {"ok": False, "contract_id": None, "blocked": True,
                     "blocking": {"id": 99, "status": "completed", "tier": "team",
                                  "block_kind": "subscribed"},
                     "status": status,
                     "reason": ("This account already has an active subscription (agreement "
                                "#99)." if status == "already_subscribed"
                                else "A checkout for this plan is already open (agreement #99).")}
    out = _begin()
    assert out["ok"] is False and out["status"] == status
    assert out["checkout_url"] is None and out["session_id"] is None
    assert out["contract_id"] == 99, "the blocking agreement number must reach the customer"
    assert fragment in out["reason"]
    assert "create_checkout" not in wired.calls


# ── the successful flow ───────────────────────────────────────────────────────────

def test_success_records_the_contract_BEFORE_returning_a_checkout_url(wired):
    out = _begin()
    assert out["ok"] is True and out["status"] == "checkout_created"
    assert out["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_FAKE"
    assert out["session_id"] == "cs_test_FAKE"
    assert out["contract_id"] == 4242
    assert out["tier"] == "team"
    assert out["amount_cents"] == 75000
    assert out["currency"] == "CAD"
    assert out["contract_version"] == "2026-08-12-v1"
    assert out["reason"] is None
    # THE ORDER IS THE PRODUCT REQUIREMENT. Final state cannot distinguish before from after.
    #
    # ``is_configured`` leads: the provider's usability is a READ-ONLY check that now happens
    # BEFORE anything permanent is written, so an unconfigured deployment refuses instead of
    # minting an undeletable orphan contract on every attempt. The load-bearing invariant is
    # UNCHANGED and still asserted below — record_acceptance strictly precedes create_checkout.
    assert wired.calls == ["is_configured", "get_current_terms", "record_acceptance",
                           "create_checkout", "link_checkout_session"]
    assert wired.calls.index("record_acceptance") < wired.calls.index("create_checkout"), \
        "THE GATE: the signature must be on disk before the payment surface exists"


def test_the_recorded_acceptance_carries_the_signer_facts_and_the_agreed_price(wired):
    _begin()
    acc = wired.acceptances[0]
    assert acc["template_id"] == 7                       # the ACTIVE template, snapshot source
    assert acc["signer_email"] == "buyer@example.com"    # normalised
    assert acc["signer_name"] == "Christopher Thompson"
    assert acc["signer_ip"] == "203.0.113.9"
    assert acc["signer_user_agent"] == "Mozilla/5.0 (test)"
    assert acc["tier"] == "team"
    assert acc["agreed_amount_cents"] == 75000
    assert acc["agreed_currency"] == "CAD"
    assert acc["agreed_interval"] == "month"
    assert acc["stripe_price_id"] == tc.get_tier("team")["price_id"]


def test_enterprise_flow_records_the_enterprise_price_and_price_id(wired):
    out = _begin(tier="enterprise")
    assert out["ok"] is True and out["amount_cents"] == 150000
    acc = wired.acceptances[0]
    assert acc["tier"] == "enterprise"
    assert acc["agreed_amount_cents"] == 150000
    assert acc["stripe_price_id"] == tc.get_tier("enterprise")["price_id"]


def test_contract_id_is_passed_into_the_checkout_call(wired):
    _begin()
    kw = wired.checkouts[0]
    assert kw["contract_id"] == 4242
    assert kw["price_id"] == tc.get_tier("team")["price_id"]
    assert kw["customer_email"] == "buyer@example.com"
    assert kw["plan_id"] == "team"


def test_client_reference_falls_back_to_the_contract_when_there_is_no_user_yet(wired):
    _begin()
    assert wired.checkouts[0]["user_id"] == "fl_contract_4242"
    wired.checkouts.clear()
    uid = str(uuid.uuid4())
    _begin(signer_user_id=uid)
    assert wired.checkouts[0]["user_id"] == uid


def test_the_session_is_linked_to_the_contract(wired):
    _begin()
    assert wired.links == [(4242, "cs_test_FAKE")]


# ── Stripe failure: the honest orphan ─────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"status": "checkout_failed", "checkout_url": None, "session_id": None,
     "message": "Stripe checkout could not be created. No charge was made."},
    {"status": "provider_not_configured", "checkout_url": None, "session_id": None,
     "message": "not configured"},
    {"status": "missing_price", "checkout_url": None, "session_id": None, "message": "no price"},
    # a url with no session id is NOT a usable checkout — treat it as a failure, never a success
    {"status": "checkout_created", "checkout_url": "https://x", "session_id": None},
])
def test_stripe_failure_leaves_an_honest_orphan_and_never_fakes_success(wired, monkeypatch, bad):
    monkeypatch.setattr(tc._payments, "get_payment_provider",
                        lambda: _FakeProvider(wired, result=bad))
    out = _begin()
    assert out["ok"] is False
    assert out["checkout_url"] is None and out["session_id"] is None
    # The contract row STANDS — it is evidence, and it is append-only. It is reported back so the
    # caller can show/audit it; it grants nothing on its own.
    assert out["contract_id"] == 4242
    # 🔴 THIS ASSERTION USED TO READ ``out["status"] == bad["status"] or ... "checkout_failed"``,
    # AND THAT WAS THE DEFECT IT WAS SUPPOSED TO GUARD. Accepting the ADAPTER's own status as
    # this module's status is exactly what let ``checkout_failed`` / ``invalid_return_url`` /
    # ``unconfigured`` reach the transport, miss its status map, and become a 502 nobody chose —
    # on the one refusal path that has already written a permanent contract row. The module now
    # reports ONE documented status for this case and preserves the adapter's word separately.
    assert out["status"] == "checkout_unavailable"
    assert out["provider_status"] == bad["status"], (
        "the adapter's own status must survive for the operator — it is the only thing that "
        "separates a Stripe outage from a return url misconfigured in THIS deployment")
    # The signature IS committed here, and the refusal must say so.
    assert out["signed"] is True and out["charged"] is False
    assert "link_checkout_session" not in wired.calls


def test_provider_raising_is_caught_and_never_fakes_success(wired, monkeypatch):
    class _Boom:
        name = "boom"

        def is_configured(self):
            return True

        def create_checkout(self, **kw):
            wired.calls.append("create_checkout")
            raise RuntimeError("network")

    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _Boom())
    out = _begin()
    assert out["ok"] is False and out["checkout_url"] is None
    # The provider RESOLVED and reported itself configured, so the contract was legitimately
    # written before the call; a mid-call network failure is the HONEST ORPHAN case and the row
    # must survive as evidence of a real acceptance.
    assert out["contract_id"] == 4242
    # An orphan is still a signature. The customer must be told it exists.
    assert out["signed"] is True and out["charged"] is False
    assert out["status"] == "checkout_unavailable"


# ── linkage failure: still return the url, loudly ─────────────────────────────────

def test_link_failure_still_returns_the_checkout_url_and_is_loud(wired, monkeypatch, capsys):
    monkeypatch.setattr(tc._contracts, "link_checkout_session",
                        lambda cid, sid: (wired.calls.append("link_checkout_session"), False)[1])
    out = _begin()
    assert out["ok"] is True
    assert out["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_FAKE"
    err = capsys.readouterr().err
    assert "CRIT" in err and "could NOT be linked" in err


# ── the module must import with SAAS off ──────────────────────────────────────────

def test_module_imports_with_saas_off_and_no_dsn(monkeypatch):
    monkeypatch.delenv("SAAS_MODE", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    import importlib
    mod = importlib.reload(tc)
    assert mod.get_tier("team")["amount_cents"] == 75000
    importlib.reload(tc)   # restore for any later test


def test_no_secret_or_key_literal_in_the_module():
    src = Path(tc.__file__).read_text()
    assert not re.search(r"\b(sk|pk|whsec)_[A-Za-z0-9]", src)


def test_no_fuzzy_matching_in_the_module():
    """Constraint: deterministic, not fuzzy. No ILIKE / LIKE '%' / similarity in this module."""
    src = Path(tc.__file__).read_text().lower()
    for banned in ("ilike", "like '%", 'like "%', "similarity(", "levenshtein"):
        assert banned not in src, f"fuzzy construct {banned!r} found in tenant_checkout.py"


# ══════════════════════════════════════════════════════════════════════════════════
# 3b. THE ADAPTER EDIT — contract_id must reach BOTH the session AND the subscription
# ══════════════════════════════════════════════════════════════════════════════════
#
# Per https://docs.stripe.com/api/checkout/sessions/create, session ``metadata`` attaches to the
# Checkout Session and NOWHERE else; ``subscription_data`` is "A subset of parameters to be passed
# to subscription creation". Session metadata does NOT propagate to the Subscription — so if
# contract_id only rode on the session, every later lifecycle event (invoice.paid,
# customer.subscription.*) would have no contract to point at. These pin both halves.

def _stripe_params(monkeypatch, **checkout_kw) -> dict:
    """Drive the REAL adapter with stripe.checkout.Session.create captured. No network."""
    from saas import payments_stripe as ps
    if ps._stripe is None:                                    # pragma: no cover
        pytest.skip("stripe library unavailable")
    captured: dict = {}

    class _Session:
        id = "cs_test_CAPTURED"
        url = "https://checkout.stripe.com/c/pay/cs_test_CAPTURED"

    def _create(**params):
        captured.update(params)
        return _Session()

    monkeypatch.setattr(ps._stripe.checkout.Session, "create", staticmethod(_create))
    adapter = ps.StripePaymentProvider(config={
        "provider": "stripe", "mode": "test", "currency": "CAD",
        "api_key": "pk_test_placeholder", "api_secret": "sk_test_placeholder",
        "webhook_secret": "whsec_placeholder"})
    kw = dict(account_id="acct-1", user_id="user-1", plan_id="team", amount=750.0,
              currency="CAD", price_id="price_TEST",
              # ABSOLUTE by requirement — the adapter now refuses a relative return url
              # because the redirect is issued by checkout.stripe.com and a relative path
              # would resolve against STRIPE'S origin, not ours.
              success_url="https://app.faultline.ca/dashboard/subscribe.html"
                          "?checkout=success&session_id={CHECKOUT_SESSION_ID}",
              cancel_url="https://app.faultline.ca/dashboard/subscribe.html?checkout=cancelled")
    kw.update(checkout_kw)
    out = adapter.create_checkout(**kw)
    assert out["status"] == "checkout_created", out
    return captured


@pytest.mark.parametrize("relative", [
    "/?checkout=success&session_id={CHECKOUT_SESSION_ID}",   # the OLD shipped default
    "/dashboard/subscribe.html",
    "dashboard/subscribe.html",
    "//evil.example/x",                                      # protocol-relative is not absolute
    "",
    None,
])
def test_adapter_REFUSES_a_non_absolute_return_url(monkeypatch, relative):
    """🔴 M4. The old default was ``"/?checkout=success&session_id={CHECKOUT_SESSION_ID}"`` and
    the tenant flow passed nothing, so it was always used.

    This is broken independently of Stripe's own validation, and that is the decisive point: the
    redirect is issued BY checkout.stripe.com, so a relative path resolves against STRIPE'S
    origin. A customer who had just paid CA$750/mo would land on
    ``https://checkout.stripe.com/?checkout=success`` — someone else's website. Every example in
    Stripe's docs is an absolute URL (docs.stripe.com/api/checkout/sessions/create →
    ``success_url=https://example.com/success``).

    Refusing is the money-path-correct behaviour: no session is created and nothing is charged,
    rather than a paid customer stranded off-site."""
    from saas import payments_stripe as ps
    if ps._stripe is None:                                    # pragma: no cover
        pytest.skip("stripe library unavailable")

    def _explode(**params):                                   # pragma: no cover
        raise AssertionError("Stripe must never be called with a non-absolute return url")

    monkeypatch.setattr(ps._stripe.checkout.Session, "create", staticmethod(_explode))
    adapter = ps.StripePaymentProvider(config={
        "provider": "stripe", "mode": "test", "currency": "CAD",
        "api_key": "pk_test_placeholder", "api_secret": "sk_test_placeholder",
        "webhook_secret": "whsec_placeholder"})
    out = adapter.create_checkout(
        account_id="a", user_id="u", plan_id="team", amount=750.0, currency="CAD",
        price_id="price_TEST", success_url=relative,
        cancel_url="https://app.faultline.ca/x")
    assert out["status"] == "invalid_return_url", out
    assert out["checkout_url"] is None and out["session_id"] is None
    assert out["charged"] is False


def test_adapter_keeps_the_session_id_placeholder_verbatim(monkeypatch):
    """Stripe: "{CHECKOUT_SESSION_ID} ... is a literal string and you must add it exactly as you
    see it here. Don't substitute it with a Checkout Session ID—this happens automatically after
    your customer pays" (docs.stripe.com/payments/checkout/custom-success-page)."""
    p = _stripe_params(monkeypatch)
    assert "{CHECKOUT_SESSION_ID}" in p["success_url"]
    assert p["success_url"].startswith("https://")
    assert p["cancel_url"].startswith("https://")


def test_adapter_puts_contract_id_on_the_session_AND_the_subscription(monkeypatch):
    p = _stripe_params(monkeypatch, contract_id=4242)
    assert p["metadata"]["contract_id"] == "4242"
    assert p["subscription_data"]["metadata"]["contract_id"] == "4242"
    assert p["mode"] == "subscription"


def test_adapter_body_is_byte_identical_when_no_contract_id(monkeypatch):
    """Existing behaviour must not shift for the individual-plan flow."""
    without = _stripe_params(monkeypatch)
    assert "contract_id" not in without["metadata"]
    assert "contract_id" not in without["subscription_data"]["metadata"]
    assert set(without["metadata"]) == {"account_id", "user_id", "plan_id"}
    explicit_none = _stripe_params(monkeypatch, contract_id=None)
    assert explicit_none["metadata"] == without["metadata"]
    assert explicit_none["subscription_data"] == without["subscription_data"]


def test_adapter_metadata_respects_the_documented_stripe_limits(monkeypatch):
    """https://docs.stripe.com/api/metadata — "up to 50 keys, with key names up to 40 characters
    long and values up to 500 characters long"; client_reference_id max 200."""
    p = _stripe_params(monkeypatch, contract_id="x" * 400, user_id="u" * 400)
    for scope in (p["metadata"], p["subscription_data"]["metadata"]):
        assert len(scope) <= 50
        for k, v in scope.items():
            assert len(k) <= 40 and "[" not in k and "]" not in k
            assert isinstance(v, str) and len(v) <= 500
    assert len(p["client_reference_id"]) <= 200


# ══════════════════════════════════════════════════════════════════════════════════
# 4. DB-BACKED — the real SQL against real PostgreSQL, in a THROWAWAY schema
# ══════════════════════════════════════════════════════════════════════════════════
#
# These prove the things a mock CANNOT: the append-only trigger, the write-once linkage columns,
# and the COALESCE false-success that link/advance must verify the read to catch.
#
# Gated on POSTGRES_DSN. ⚠️ A skipped DB test is NOT evidence — this repo has produced three false
# greens that way. When you run WITH a DSN, assert `skipped == 0` in the fltest output.

_DSN = os.environ.get("POSTGRES_DSN")
_SCHEMA = "tc243_" + uuid.uuid4().hex[:8]

try:
    import psycopg2 as _pg
except Exception:  # pragma: no cover
    _pg = None

needs_db = pytest.mark.skipif(
    not _DSN or _pg is None or not MIGRATION.exists(),
    reason="POSTGRES_DSN + psycopg2 + migration 243 required for the DB-backed lane")


@pytest.fixture(scope="module")
def throwaway_schema():
    """Apply migration 243 into a THROWAWAY schema. NEVER a real tenant, NEVER production."""
    if not _DSN or _pg is None:
        pytest.skip("no DSN")
    sql = MIGRATION.read_text()
    # The migration is written against public.*; retarget every reference at the throwaway schema
    # (the same substitution the orchestrator validated the migration with).
    sql = sql.replace("public.", f"{_SCHEMA}.")
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        cur.execute(sql)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    conn.close()


class _CurProxy:
    """Cursor wrapper that retargets ``public.`` at the throwaway schema.

    ⚠️ A WRAPPER, not attribute assignment: ``psycopg2.extensions.cursor.execute`` and
    ``connection.cursor`` are READ-ONLY on the C extension type ("object attribute 'execute' is
    read-only"). Assigning to them raises AttributeError INSIDE contracts.py's own
    ``except Exception`` and every function quietly returns None/False — which reads exactly like
    a product bug and is not one. Implements ``__enter__``/``__exit__`` because a psycopg2 cursor
    is its own context manager and every saas call site uses ``with conn.cursor() as cur:``."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, q, params=None):
        return self._cur.execute(q.replace("public.", f"{_SCHEMA}."), params)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **kw):
        return _CurProxy(self._conn.cursor(*a, **kw))

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def db_contracts(throwaway_schema, monkeypatch):
    """saas.contracts pointed at the throwaway schema.

    Retargets the module's ``public.`` SQL prefix at the cursor seam so the REAL statements in
    contracts.py execute verbatim against the sandbox — the SQL under test is the shipped SQL,
    not a copy."""
    from saas import contracts as _c
    import contextlib

    @contextlib.contextmanager
    def _conn():
        c = _pg.connect(_DSN)
        try:
            yield _ConnProxy(c)
        finally:
            c.close()

    monkeypatch.setattr(_c.db, "connection", _conn)
    return _c


def _template_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {_SCHEMA}.contract_templates "
                    f"WHERE retired_at IS NULL ORDER BY effective_at DESC LIMIT 1")
        return int(cur.fetchone()[0])


def _row(conn, cid):
    with conn.cursor() as cur:
        cur.execute(f"SELECT status, stripe_checkout_session_id, stripe_subscription_id, "
                    f"account_id, contract_version, agreed_amount_cents, agreed_currency, "
                    f"agreed_interval, contract_snapshot, signer_name, signer_email, signer_ip "
                    f"FROM {_SCHEMA}.signed_contracts WHERE id = %s", (cid,))
        return cur.fetchone()


@needs_db
def test_db_the_seeded_terms_version_is_active(throwaway_schema, db_contracts):
    terms = db_contracts.get_current_terms()
    assert terms and terms["version"] == "2026-08-12-v1"
    assert "FaultLine Subscription Agreement" in terms["body_markdown"]


@needs_db
def test_db_record_acceptance_writes_the_snapshot_version_and_agreed_price(
        throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="Buyer@Example.COM", signer_name="Christopher Thompson",
        signer_ip="203.0.113.9", signer_user_agent="UA/1.0", tier="team",
        stripe_price_id="price_TEST_TEAM", agreed_amount_cents=75000,
        agreed_currency="CAD", agreed_interval="month")
    assert cid
    r = _row(throwaway_schema, cid)
    assert r[0] == "accepted"
    assert r[4] == "2026-08-12-v1"                     # contract_version
    assert r[5] == 75000 and r[6] == "CAD" and r[7] == "month"
    assert "FaultLine Subscription Agreement" in r[8]  # the EXACT terms snapshot
    assert r[9] == "Christopher Thompson"
    assert r[10] == "buyer@example.com"
    assert r[11] == "203.0.113.9"


@needs_db
def test_db_a_bad_amount_refuses_the_write_rather_than_recording_a_wrong_price(
        throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    assert db_contracts.record_acceptance(
        template_id=tid, signer_email="x@example.com", signer_name="X Y", tier="team",
        agreed_amount_cents="seven hundred fifty") is None
    assert db_contracts.record_acceptance(
        template_id=tid, signer_email="x@example.com", signer_name="X Y", tier="team",
        agreed_amount_cents=-1) is None


@needs_db
def test_db_delete_is_always_blocked(throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="del@example.com", signer_name="D E", tier="team",
        agreed_amount_cents=75000)
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as exc:
                cur.execute(f"DELETE FROM {_SCHEMA}.signed_contracts WHERE id = %s", (cid,))
            assert "append-only" in str(exc.value)
    finally:
        conn.close()
    assert _row(throwaway_schema, cid) is not None


@needs_db
@pytest.mark.parametrize("col,val", [
    ("signer_email", "'attacker@example.com'"),
    ("signer_name", "'Someone Else'"),
    ("agreed_amount_cents", "1"),
    ("contract_snapshot", "'different terms'"),
    ("tier", "'enterprise'"),
])
def test_db_every_acceptance_fact_is_immutable(throwaway_schema, db_contracts, col, val):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="imm@example.com", signer_name="I M", tier="team",
        agreed_amount_cents=75000)
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as exc:
                cur.execute(f"UPDATE {_SCHEMA}.signed_contracts SET {col} = {val} WHERE id = %s",
                            (cid,))
            assert "append-only" in str(exc.value)
    finally:
        conn.close()


@needs_db
def test_db_link_checkout_session_is_write_once_and_idempotent(throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="link@example.com", signer_name="L K", tier="team",
        agreed_amount_cents=75000)
    assert db_contracts.link_checkout_session(cid, "cs_test_ONE") is True
    assert _row(throwaway_schema, cid)[1] == "cs_test_ONE"
    # same session again → idempotent True, still linked to the same session
    assert db_contracts.link_checkout_session(cid, "cs_test_ONE") is True
    assert _row(throwaway_schema, cid)[1] == "cs_test_ONE"
    # a DIFFERENT session → refused, and the row is NOT re-pointed.
    # This is the COALESCE false-success: the SQL function would return true here.
    assert db_contracts.link_checkout_session(cid, "cs_test_TWO") is False
    assert _row(throwaway_schema, cid)[1] == "cs_test_ONE"
    # linking never moves the lifecycle
    assert _row(throwaway_schema, cid)[0] == "accepted"


@needs_db
def test_db_advance_to_completed_and_its_replay(throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="adv@example.com", signer_name="A D", tier="enterprise",
        agreed_amount_cents=150000)
    db_contracts.link_checkout_session(cid, "cs_test_ADV")
    acct = str(uuid.uuid4())
    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id="sub_ADV", account_id=acct) is True
    r = _row(throwaway_schema, cid)
    assert r[0] == "completed" and r[2] == "sub_ADV" and str(r[3]) == acct
    # WEBHOOK REPLAY with the SAME subscription id → True, no corruption.
    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id="sub_ADV", account_id=acct) is True
    r = _row(throwaway_schema, cid)
    assert r[0] == "completed" and r[2] == "sub_ADV" and str(r[3]) == acct
    # A replay carrying a DIFFERENT subscription is REFUSED, not silently absorbed by COALESCE.
    assert db_contracts.advance_to_completed(cid, stripe_subscription_id="sub_OTHER") is False
    assert _row(throwaway_schema, cid)[2] == "sub_ADV"


@needs_db
def test_db_status_cannot_regress_from_completed(throwaway_schema, db_contracts):
    tid = _template_id(throwaway_schema)
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="reg@example.com", signer_name="R G", tier="team",
        agreed_amount_cents=75000)
    db_contracts.advance_to_completed(cid, stripe_subscription_id="sub_REG")
    conn = _pg.connect(_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as exc:
                cur.execute(f"SELECT {_SCHEMA}.advance_signed_contract_status(%s,'accepted')",
                            (cid,))
            assert "regress" in str(exc.value)
    finally:
        conn.close()
    assert _row(throwaway_schema, cid)[0] == "completed"
    # …and linking AFTER completion still does not regress the status (it passes the row's own
    # current status back into the advance function).
    assert db_contracts.link_checkout_session(cid, "cs_test_LATE") is True
    assert _row(throwaway_schema, cid)[0] == "completed"


@needs_db
def test_db_the_full_flow_writes_the_row_before_any_checkout_url(
        throwaway_schema, db_contracts, monkeypatch):
    """END TO END against real PostgreSQL: begin_subscription with the REAL contracts.py SQL and
    a mocked Stripe adapter. Asserts the row is committed and complete BEFORE the url exists."""
    seen: dict = {}

    class _Provider:
        name = "fake"

        def is_configured(self):
            return True

        def create_checkout(self, **kw):
            # At the MOMENT Stripe would be called, the contract must already be on disk.
            seen["row_at_checkout_time"] = _row(throwaway_schema, kw["contract_id"])
            return {"status": "checkout_created", "checkout_url": "https://cs/url",
                    "session_id": "cs_test_E2E"}

    monkeypatch.setattr(tc._contracts, "get_current_terms", db_contracts.get_current_terms)
    monkeypatch.setattr(tc._contracts, "record_acceptance", db_contracts.record_acceptance)
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded",
                        db_contracts.record_acceptance_guarded)
    monkeypatch.setattr(tc._contracts, "link_checkout_session", db_contracts.link_checkout_session)
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _Provider())

    _shown = db_contracts.get_current_terms()
    out = tc.begin_subscription(
        tier="enterprise", signer_email="E2E@Example.com", legal_name="Christopher Thompson",
        signer_ip="198.51.100.4", signer_user_agent="UA/e2e",
        # The binding is MANDATORY in the brain (ARM 0) — an end-to-end test that omitted these
        # would be testing the refusal path, not the flow it names.
        expected_template_id=_shown["id"], expected_version=_shown["version"],
        expected_sha256=_shown["body_sha256"])
    assert out["ok"] is True and out["checkout_url"] == "https://cs/url"

    pre = seen["row_at_checkout_time"]
    assert pre is not None, "the contract row did NOT exist when Stripe was called"
    assert pre[0] == "accepted"
    assert pre[5] == 150000 and pre[6] == "CAD" and pre[7] == "month"
    assert pre[4] == "2026-08-12-v1"
    assert "FaultLine Subscription Agreement" in pre[8]
    assert pre[10] == "e2e@example.com"
    assert pre[1] is None, "the session must not be linked until AFTER Stripe returns one"

    post = _row(throwaway_schema, out["contract_id"])
    assert post[1] == "cs_test_E2E"

    # the read helpers surface the new columns
    got = db_contracts.get_signed_contract(out["contract_id"])
    assert got["agreed_amount_cents"] == 150000 and got["contract_version"] == "2026-08-12-v1"
    assert got["agreed_currency"] == "CAD" and got["agreed_interval"] == "month"
    ver = db_contracts.verify_acceptance_for_session("cs_test_E2E")
    assert ver["id"] == out["contract_id"] and ver["status"] == "accepted"
    assert ver["contract_snapshot_length"] > 0 and ver["agreed_amount_cents"] == 150000
    listed = db_contracts.list_signed_contracts(limit=100)
    assert any(x["id"] == out["contract_id"] and x["contract_version"] == "2026-08-12-v1"
               for x in listed)


# ══════════════════════════════════════════════════════════════════════════════════
# 4b. S1 / S3 / S4 — against the REAL database, because they are database properties
# ══════════════════════════════════════════════════════════════════════════════════

def _count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.signed_contracts")
        return int(cur.fetchone()[0])


@needs_db
def test_db_S1_terms_changed_writes_NO_contract_row(throwaway_schema, db_contracts, monkeypatch):
    """🔴 THE CRITIC'S EXACT DEMONSTRATION, end-to-end, against real PostgreSQL.

    Publish v2 with a DIFFERENT governing law between the page's GET and the customer's POST.
    Before the binding this recorded the customer as having agreed to Delaware law and a
    class-action waiver they never saw::

        PAGE SHOWED  : 2026-08-12-v1 -> '...laws of the Province of Ontario.'
        ROW RECORDED : 2026-08-12-v2 -> '...laws of Delaware, USA. You waive all class-action
                                         rights.'
        snapshot == what was displayed? False

    PROVING THIS TEST HAS TEETH: delete the ``if expected_template_id is not None:`` block in
    ``tenant_checkout.begin_subscription`` and this goes RED — a row IS written and its snapshot
    is the Delaware text. (Verified by doing exactly that; see the task report.)"""
    ontario = "This agreement is governed by the laws of the Province of Ontario."
    delaware = ("This agreement is governed by the laws of Delaware, USA. "
                "You waive all class-action rights.")

    # ⚠️ change_summary is REQUIRED when superseding an incumbent (migration 256): publishing
    # revised terms without stating what changed is not sufficient notice. These probes are
    # publishing over the seeded version, so they must supply one like any real publisher.
    v1 = db_contracts.publish_terms_version(
        version=f"s1-{uuid.uuid4().hex[:8]}-v1", title="Probe", body_markdown=ontario,
        change_summary="probe: governing law set to Ontario")
    assert v1["ok"], v1
    shown = db_contracts.get_current_terms()          # what the PAGE rendered
    assert shown["body_markdown"] == ontario

    # …the operator publishes a new version while the customer is reading.
    v2 = db_contracts.publish_terms_version(
        version=f"s1-{uuid.uuid4().hex[:8]}-v2", title="Probe", body_markdown=delaware,
        change_summary="probe: governing law moved to Delaware; class-action waiver added")
    assert v2["ok"], v2
    assert db_contracts.get_current_terms()["body_markdown"] == delaware

    class _Provider:
        name = "fake"

        def is_configured(self):
            return True

        def create_checkout(self, **kw):              # pragma: no cover
            raise AssertionError("Stripe must never be reached on a terms mismatch")

    monkeypatch.setattr(tc._contracts, "get_current_terms", db_contracts.get_current_terms)
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded",
                        db_contracts.record_acceptance_guarded)
    monkeypatch.setattr(tc._contracts, "link_checkout_session", db_contracts.link_checkout_session)
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _Provider())

    before = _count(throwaway_schema)
    out = tc.begin_subscription(
        tier="team", signer_email="s1@example.com", legal_name="Christopher Thompson",
        signer_user_id=str(uuid.uuid4()),
        # THE BINDING TOKEN: the template the page actually rendered.
        expected_template_id=shown["id"], expected_version=shown["version"],
        expected_sha256=shown["body_sha256"])

    assert out["ok"] is False and out["status"] == "terms_changed", out
    assert out["contract_id"] is None and out["checkout_url"] is None
    assert _count(throwaway_schema) == before, "A CONTRACT ROW WAS WRITTEN AGAINST UNSEEN TERMS"
    # And nothing anywhere in the table quotes the text the customer never saw.
    with throwaway_schema.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.signed_contracts "
                    f"WHERE contract_snapshot = %s", (delaware,))
        assert int(cur.fetchone()[0]) == 0


@needs_db
def test_db_S1_a_matching_id_still_signs(throwaway_schema, db_contracts, monkeypatch):
    """The binding must refuse a STALE id without breaking the ordinary case."""
    class _Provider:
        name = "fake"

        def is_configured(self):
            return True

        def create_checkout(self, **kw):
            return {"status": "checkout_created", "checkout_url": "https://cs/ok",
                    "session_id": f"cs_ok_{uuid.uuid4().hex[:12]}"}

    monkeypatch.setattr(tc._contracts, "get_current_terms", db_contracts.get_current_terms)
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded",
                        db_contracts.record_acceptance_guarded)
    monkeypatch.setattr(tc._contracts, "link_checkout_session", db_contracts.link_checkout_session)
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _Provider())

    cur_terms = db_contracts.get_current_terms()
    out = tc.begin_subscription(
        tier="team", signer_email="s1ok@example.com", legal_name="Christopher Thompson",
        signer_user_id=str(uuid.uuid4()),
        expected_template_id=cur_terms["id"], expected_version=cur_terms["version"],
        expected_sha256=cur_terms["body_sha256"])
    assert out["ok"] is True, out
    assert _row(throwaway_schema, out["contract_id"])[8] == cur_terms["body_markdown"]


@needs_db
def test_db_S3_five_concurrent_subscribes_write_ONE_row(throwaway_schema, db_contracts,
                                                        monkeypatch):
    """🔴 MEASURED 5 → 5 before the guard, 5 → 1 after.

    Every row is permanent and undeletable, so five concurrent POSTs meant five permanent
    financial records and five checkout URLs for one buying intent. A check-then-insert across
    separate connections cannot prevent this; the guard takes a transaction-scoped ADVISORY LOCK
    on the principal and re-checks INSIDE it.

    The threads share nothing but the database — which is the point: this is the same shape as
    five browser tabs or a double-click on a slow connection."""
    import threading

    class _Provider:
        name = "fake"

        def is_configured(self):
            return True

        def create_checkout(self, **kw):
            return {"status": "checkout_created", "checkout_url": "https://cs/x",
                    "session_id": f"cs_{uuid.uuid4().hex[:16]}"}

    monkeypatch.setattr(tc._contracts, "get_current_terms", db_contracts.get_current_terms)
    monkeypatch.setattr(tc._contracts, "record_acceptance_guarded",
                        db_contracts.record_acceptance_guarded)
    monkeypatch.setattr(tc._contracts, "link_checkout_session", db_contracts.link_checkout_session)
    monkeypatch.setattr(tc._payments, "get_payment_provider", lambda: _Provider())

    acct = str(uuid.uuid4())
    usr = str(uuid.uuid4())
    _shown = db_contracts.get_current_terms()
    before = _count(throwaway_schema)
    results: list = []
    lock = threading.Lock()

    def _go():
        r = tc.begin_subscription(tier="team", signer_email="dup@example.com",
                                  legal_name="Christopher Thompson",
                                  account_id=acct, signer_user_id=usr,
                                  expected_template_id=_shown["id"],
                                  expected_version=_shown["version"],
                                  expected_sha256=_shown["body_sha256"])
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    written = _count(throwaway_schema) - before
    assert written == 1, f"{written} permanent rows written for ONE buying intent"
    assert sum(1 for r in results if r.get("ok")) == 1
    blocked = [r for r in results if r.get("status") == "duplicate_in_flight"]
    assert len(blocked) == 4
    # The refusal must name the agreement that already covers them, not just say "no".
    assert all(r.get("contract_id") for r in blocked)


@needs_db
def test_db_S3_a_completed_subscription_blocks_a_second_one(throwaway_schema, db_contracts):
    """Parallel double-billing is the failure to prevent. There is no self-serve plan-change
    path, so "sign again to upgrade" would mean paying for two subscriptions."""
    tid = _template_id(throwaway_schema)
    acct = str(uuid.uuid4())
    usr = str(uuid.uuid4())
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="done@example.com", signer_name="D N", tier="team",
        signer_user_id=usr, agreed_amount_cents=75000)
    db_contracts.link_checkout_session(cid, f"cs_{uuid.uuid4().hex[:16]}")
    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id=f"sub_{uuid.uuid4().hex[:12]}", account_id=acct) is True

    out = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="done@example.com", signer_name="D N", tier="enterprise",
        signer_user_id=usr, agreed_amount_cents=150000,
        dedupe_principal=acct, account_id=acct)
    assert out["blocked"] is True and out["status"] == "already_subscribed", out
    assert out["contract_id"] is None
    assert out["blocking"]["id"] == cid


@needs_db
def test_db_S3_an_honest_orphan_does_NOT_block_a_retry_forever(throwaway_schema, db_contracts):
    """DELIBERATELY NOT BLOCKED: an ``accepted`` row with no checkout session can never be
    billed against (Stripe failed after the write). Blocking on it would strand the customer
    permanently behind a row that grants nothing. Only the short concurrency CLAIM window covers
    it, so a retry a few minutes later must succeed."""
    tid = _template_id(throwaway_schema)
    usr = str(uuid.uuid4())
    orphan = db_contracts.record_acceptance(
        template_id=tid, signer_email="orphan@example.com", signer_name="O R", tier="team",
        signer_user_id=usr, agreed_amount_cents=75000)
    assert orphan and _row(throwaway_schema, orphan)[1] is None    # no session — the orphan

    # Inside the claim window it DOES block (that is what closes the concurrency race)…
    inside = db_contracts.find_blocking_contract(
        account_id=None, signer_user_id=usr, claim_seconds=120)
    assert inside and inside["block_kind"] == "just_signed"

    # …and once the claim window has passed, the orphan must NOT block a genuine retry.
    outside = db_contracts.find_blocking_contract(
        account_id=None, signer_user_id=usr, claim_seconds=0, inflight_seconds=1800)
    assert outside is None, "an unbillable orphan is stranding the customer's retry"


# ══════════════════════════════════════════════════════════════════════════════════
# S1 — TWO DIFFERENT ADMIN LOGINS, ONE ACCOUNT, ONE CONTRACT
# ══════════════════════════════════════════════════════════════════════════════════
#
# The 5-concurrent proof above uses a SINGLE principal, so it exercises only the guard's
# ``signer_user_id`` arm. The ``account_id`` arm was dead — the INSERT never wrote the column —
# and a second billing admin therefore sailed straight past the guard. MEASURED before the fix::
#
#     Admin A1 signs contract #1 ; row account_id: None
#     Admin A1 retries                        -> blocked? True   (just_signed)
#     Admin A2, SAME ACCOUNT, different login -> blocked? False  <-- 2 permanent rows
#
# Two admins → two undeletable contracts → two Stripe subscriptions → CA$750–1500/mo twice.

@needs_db
def test_db_S1_the_account_is_written_AT_INSERT_not_only_by_the_webhook(
        throwaway_schema, db_contracts):
    """The premise of the whole fix, asserted on the ROW rather than on the argument.

    A guard arm that ORs over a column the writer leaves NULL is not an arm. Reading the row is
    the only way to know which it is — the call signature looked correct throughout."""
    tid = _template_id(throwaway_schema)
    acct = str(uuid.uuid4())
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="fresh@example.com", signer_name="F R", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id=acct, agreed_amount_cents=75000)
    assert cid
    row = _row(throwaway_schema, cid)
    assert str(row[3]) == acct, ("account_id is NULL on a freshly-signed contract — the dedupe "
                                 "guard's account arm cannot fire and a second admin will "
                                 "sail past it (S1)")
    assert row[0] == "accepted" and row[2] is None      # still unpaid, nothing linked


@needs_db
def test_db_S1_a_SECOND_admin_of_the_SAME_account_is_BLOCKED(throwaway_schema, db_contracts):
    """🔴 THE CRITIC'S EXACT MEASUREMENT. Two different logins, one account, ONE contract."""
    tid = _template_id(throwaway_schema)
    acct = str(uuid.uuid4())
    admin_1, admin_2 = str(uuid.uuid4()), str(uuid.uuid4())
    before = _count(throwaway_schema)

    first = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="admin1@tenant-a.example", signer_name="Admin One",
        tier="team", signer_user_id=admin_1, account_id=acct, agreed_amount_cents=75000,
        dedupe_principal=acct)
    assert first["status"] == "recorded" and first["contract_id"]

    # The SAME admin retrying is blocked (this arm always worked — kept so a regression that
    # trades one arm for the other is visible).
    retry = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="admin1@tenant-a.example", signer_name="Admin One",
        tier="team", signer_user_id=admin_1, account_id=acct, agreed_amount_cents=75000,
        dedupe_principal=acct)
    assert retry["blocked"] is True

    # A DIFFERENT billing admin of the SAME account — the arm that was dead.
    second = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="admin2@tenant-a.example", signer_name="Admin Two",
        tier="enterprise", signer_user_id=admin_2, account_id=acct,
        agreed_amount_cents=150000, dedupe_principal=acct)
    assert second["blocked"] is True, (
        "a SECOND billing admin of the same account signed a SECOND permanent contract — that "
        "is a second Stripe subscription and double billing at CA$750–1500/mo (S1)")
    assert second["contract_id"] is None
    assert second["blocking"]["id"] == first["contract_id"]

    assert _count(throwaway_schema) - before == 1, "more than ONE permanent row for one account"


@needs_db
def test_db_S1_a_different_ACCOUNT_is_never_blocked_by_ours(throwaway_schema, db_contracts):
    """The guard must not over-reach: another tenant signing at the same moment is unrelated."""
    tid = _template_id(throwaway_schema)
    ours, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    a = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="us@a.example", signer_name="U S", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id=ours, agreed_amount_cents=75000,
        dedupe_principal=ours)
    b = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="them@b.example", signer_name="T M", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id=theirs, agreed_amount_cents=75000,
        dedupe_principal=theirs)
    assert a["status"] == "recorded" and b["status"] == "recorded"
    assert a["contract_id"] != b["contract_id"]


@needs_db
def test_db_S1_writing_the_account_early_does_NOT_break_the_webhooks_later_link(
        throwaway_schema, db_contracts):
    """🔴 THE PRE-FLIGHT THIS FIX HAD TO PASS, RUN AS A TEST RATHER THAN ARGUED.

    ``account_id`` is one of migration 243's WRITE-ONCE linkage columns. Setting it at INSERT is
    only safe if the webhook's later ``advance_to_completed(account_id=<same>)`` still succeeds.
    The SQL function COALESCEs and its conflict check refuses only a DIFFERENT non-null value —
    measured directly on the shipped trigger, with it provably enabled."""
    tid = _template_id(throwaway_schema)
    acct = str(uuid.uuid4())
    cid = db_contracts.record_acceptance(
        template_id=tid, signer_email="paid@example.com", signer_name="P D", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id=acct, agreed_amount_cents=75000)
    sub = f"sub_{uuid.uuid4().hex[:12]}"

    # PRECONDITION: the append-only trigger is ENABLED, so a pass here is not a disabled guard.
    with throwaway_schema.cursor() as cur:
        cur.execute("SELECT tgenabled FROM pg_trigger WHERE tgname = "
                    "'trg_signed_contracts_immutable' AND tgrelid = "
                    f"'{_SCHEMA}.signed_contracts'::regclass")
        assert cur.fetchone()[0] == "O", "the append-only trigger is DISABLED — result is void"

    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id=sub, account_id=acct) is True, (
        "the webhook could not complete a contract whose account_id was set at INSERT")
    row = _row(throwaway_schema, cid)
    assert row[0] == "completed" and row[2] == sub and str(row[3]) == acct

    # REPLAY (Stripe redelivers for up to 3 days) — still true, still unchanged.
    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id=sub, account_id=acct) is True
    assert _row(throwaway_schema, cid)[3:4] == row[3:4]

    # HOSTILE: a webhook naming a DIFFERENT account must be REFUSED, not silently absorbed.
    # This case got STRICTER with the fix — previously the column was NULL and any value stuck.
    assert db_contracts.advance_to_completed(
        cid, stripe_subscription_id=sub, account_id=str(uuid.uuid4())) is False
    assert str(_row(throwaway_schema, cid)[3]) == acct


@needs_db
def test_db_S1_an_unparseable_account_is_REFUSED_not_silently_dropped(
        throwaway_schema, db_contracts):
    """Dropping a bad account_id would put the guard straight back into the single-armed state.

    Fail-CLOSED on the money path: no contract row, an explicit ``invalid_account`` status."""
    tid = _template_id(throwaway_schema)
    before = _count(throwaway_schema)
    out = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="bad@example.com", signer_name="B D", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id="not-a-uuid", agreed_amount_cents=75000,
        dedupe_principal="not-a-uuid")
    assert out["ok"] is False and out["status"] == "invalid_account"
    assert out["contract_id"] is None
    assert _count(throwaway_schema) == before, "a permanent row was written on a refusal"
    # …while a genuinely ABSENT account (an individual signature) still signs normally.
    ok = db_contracts.record_acceptance_guarded(
        template_id=tid, signer_email="solo@example.com", signer_name="S O", tier="team",
        signer_user_id=str(uuid.uuid4()), account_id=None, agreed_amount_cents=75000,
        dedupe_principal="solo@example.com")
    assert ok["status"] == "recorded" and ok["contract_id"]


@needs_db
def test_db_S4_republishing_a_version_is_REFUSED_and_leaves_terms_ACTIVE(
        throwaway_schema, db_contracts):
    """🔴 TWO BUGS, ONE CALL. Measured before the fix::

        republish same version returned: {'ok': True, 'id': 5, 'version': 'probe-v1'}
        active after republish     : None   <-- whole paid surface 503s
        body mutated in place?     : True

    (a) ``ON CONFLICT (version) DO UPDATE SET body_markdown=…`` made a *versioned immutable*
    template mutable in place — rewriting text customers may already have signed, on a table
    with no append-only trigger. (b) The unconditional retire ran first and the DO UPDATE branch
    never cleared ``retired_at``, so the platform was left with ZERO active terms while the
    function reported success."""
    ver = f"s4-{uuid.uuid4().hex[:8]}"
    first = db_contracts.publish_terms_version(
        version=ver, title="Original", body_markdown="ORIGINAL TEXT",
        change_summary="probe: the original text")
    assert first["ok"], first
    assert db_contracts.get_current_terms()["version"] == ver

    second = db_contracts.publish_terms_version(
        version=ver, title="Rewritten", body_markdown="REWRITTEN TEXT",
        change_summary="probe: an attempted in-place rewrite")
    assert second["ok"] is False, "a published version was allowed to be rewritten"
    assert "already been published" in second["reason"]

    # (b) THE OUTAGE: there must STILL be exactly one active version, and it must be ours.
    still = db_contracts.get_current_terms()
    assert still is not None, "republishing left the platform with NO active terms"
    assert still["version"] == ver
    # (a) THE MUTATION: the text customers signed is untouched.
    assert still["body_markdown"] == "ORIGINAL TEXT"
    assert still["title"] == "Original"
    with throwaway_schema.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.contract_templates "
                    f"WHERE retired_at IS NULL")
        assert int(cur.fetchone()[0]) == 1


@needs_db
def test_db_S4_publishing_a_NEW_version_retires_the_incumbent_atomically(
        throwaway_schema, db_contracts):
    """The legitimate path must still work, and must leave exactly one active row."""
    a = f"s4a-{uuid.uuid4().hex[:8]}"
    b = f"s4b-{uuid.uuid4().hex[:8]}"
    assert db_contracts.publish_terms_version(
        version=a, title="A", body_markdown="AAA", change_summary="probe A")["ok"]
    assert db_contracts.publish_terms_version(
        version=b, title="B", body_markdown="BBB", change_summary="probe B")["ok"]
    cur_terms = db_contracts.get_current_terms()
    assert cur_terms["version"] == b and cur_terms["body_markdown"] == "BBB"
    with throwaway_schema.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_SCHEMA}.contract_templates WHERE retired_at IS NULL")
        assert int(cur.fetchone()[0]) == 1
        # the incumbent is retired, not deleted — history is preserved
        cur.execute(f"SELECT retired_at IS NOT NULL FROM {_SCHEMA}.contract_templates "
                    f"WHERE version = %s", (a,))
        assert cur.fetchone()[0] is True


# ══════════════════════════════════════════════════════════════════════════════════
# 5. THE FALSE-GREEN GUARD — a skipped DB half must turn the run RED
# ══════════════════════════════════════════════════════════════════════════════════
#
# ⚠️ DSN-GATED SUITES IN THIS REPO HAVE PRODUCED FALSE GREENS REPEATEDLY. "PASS | N passed,
# M skipped" is not evidence that the database properties above were checked — and S1/S3/S4 are
# ALL database properties (an advisory lock, a transaction boundary, an append-only trigger).
# This mirrors the guard in tests/test_tenant_checkout_routes.py.

_DB_RAN = {"value": False}


@pytest.fixture(autouse=True)
def _mark_db_ran(request):
    """Records that at least one DB-backed test actually EXECUTED (not skipped)."""
    yield
    if "throwaway_schema" in request.fixturenames and not request.node.own_markers:
        pass
    # A test only reaches teardown having used the schema fixture if it really ran.
    if "throwaway_schema" in request.fixturenames:
        _DB_RAN["value"] = True


def test_zzz_no_db_test_was_silently_skipped():
    """MUST BE LAST (hence the zzz). Fails the run if the DB-backed half never executed.

    If this is the only failure you see, the DB lane did not run: check POSTGRES_DSN points at a
    THROWAWAY database reachable from here, and that migrations/243 exists. Do NOT 'fix' this by
    deleting it — it is the only thing standing between this suite and a green that proves
    nothing about S1, S3 or S4."""
    assert _DB_RAN["value"], (
        "the DB-backed half of this suite never ran (POSTGRES_DSN unset/unreachable, psycopg2 "
        f"missing, or migration 243 absent) — POSTGRES_DSN={_DSN!r}. A green without it is a "
        "FALSE GREEN: S1 (no row on a terms mismatch), S3 (5 concurrent → 1 row) and S4 "
        "(republish refused, terms stay active) are all DATABASE properties.")
