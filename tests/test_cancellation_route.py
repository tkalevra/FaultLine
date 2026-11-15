"""SELF-SERVE CANCELLATION — the route, the webhook branch, and the sweep schedule.

``saas/term_acceleration.py`` shipped complete and DARK: 956 lines, 39 live-Stripe tests, and no
caller anywhere in the product, while the Team pricing card advertised self-serve cancellation and
there was no cancellation door on the console at all. This suite pins the three things that were
missing, and it pins them at the seams where they can actually be wrong:

  (a) THE ROUTE      — authorization (billing-gated, session-derived account, CSRF), a typed-name
                       signature, a READ-ONLY preview, and the amount-acknowledgement consent
                       check that refuses a stale figure instead of charging it.
  (b) THE WEBHOOK    — ``invoice.paid`` → settle and ``invoice.voided`` → resume, keyed on the
                       invoice's ``status`` and NEVER on an amount, replay-safe, and reached only
                       by our own metadata discriminator.
  (c) THE SCHEDULE   — both operator sweeps run on the existing ``_retention_worker`` loop, are
                       safe to re-run, and log "nothing to do".

🔴 WHY SO MUCH OF THIS IS ASSERTED ON SOURCE RATHER THAN BEHAVIOUR. Several properties here are
ORDERING properties on a money path — "the seats are off the subscription BEFORE any amount is
computed", "collection is stopped BEFORE the lump is invoiced". A behavioural test can only show
that the right thing happened on the happy path; it cannot show that the wrong order is
impossible, because both orders produce the same output when nothing fails. So the ordering is
pinned structurally AND exercised behaviourally, and the structural pins name what they protect.

⚠️ FALSE-GREEN GUARD: ``_DB_RAN`` is set by the DB fixture and asserted by the last test. A
DSN-gated suite that silently skips half of itself reports "N passed, M skipped" and looks green;
that has produced a false green in this repo repeatedly.

RUN IT (never raw pytest — owner rule):

    PYTHONPATH="src:<venv site-packages>" \\
    POSTGRES_DSN=postgresql://faultline:faultline@172.20.0.3:5432/faultline_fixaccel_test \\
    python3 tools/fltest.py --bug FIXACCEL --test tests/test_cancellation_route.py
"""

import inspect
import os
import sys
import textwrap
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SAAS_MODE", "1")
os.environ.setdefault("SAAS_SESSION_SECRET", "fixaccel-suite-secret-0123456789abcdef")

from saas import billing_sweeps as sweeps_mod          # noqa: E402
from saas import dashboard_auth as auth                # noqa: E402
from saas import db                                    # noqa: E402
from saas import stripe_webhooks as hooks              # noqa: E402
from saas import tenant_console as tc                  # noqa: E402
from saas import term_acceleration as accel            # noqa: E402

DEFAULT_TEST_DSN = ("postgresql://faultline:faultline@172.20.0.3:5432/"
                    "faultline_fixaccel_test")
TEST_DSN = os.environ.get("CANCEL_TEST_DSN", os.environ.get("POSTGRES_DSN", DEFAULT_TEST_DSN))

_DB_RAN = {"value": False}


def _src(fn) -> str:
    """Function source with the docstring REMOVED.

    A plain ``inspect.getsource`` grep sees the prose too, so a test that greps for a token passes
    on the docstring EXPLAINING the token while the code does the opposite. Paid for repeatedly in
    this repo — including once in this very feature, where a CSP comment containing the words
    "no inline script tag" tripped the CSP grep."""
    import ast
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = node.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    # ⚠️ ``ast.unparse`` NORMALIZES STRING QUOTING to single quotes, so a grep written with the
    # double quotes that appear in the real file silently never matches and the test passes for
    # the wrong reason. Normalize BOTH sides instead of writing the expectations in a quote style
    # that has nothing to do with the source.
    return "\n".join(ast.unparse(n) for n in body).replace('"', "'")


# ══════════════════════════════════════════════════════════════════════════════════
# PART A — the ROUTE exists, and is authorized the way a money route must be
# ══════════════════════════════════════════════════════════════════════════════════

def _routes():
    return {(r.path, tuple(sorted(m for m in r.methods if m != "HEAD")))
            for r in tc.router.routes}


def test_the_cancellation_door_exists_at_all():
    """The whole point of the exercise: before this, a customer who wanted to cancel had no door.

    Goes red if the routes are removed — which would silently return the product to advertising
    self-serve cancellation it does not offer."""
    paths = {p for p, _ in _routes()}
    assert "/tenant/cancel" in paths
    assert "/tenant/cancel/preview" in paths


def test_both_cancel_routes_are_billing_gated_and_session_scoped():
    """``require_billing_access`` on BOTH, and the account comes from the SESSION.

    Two properties in one assertion because they are one guarantee: the dependency chain is
    ``require_billing_access`` → ``require_dashboard_session`` → the signed cookie, which is what
    makes the account server-derived (no BOLA) and enforces CSRF on the POST. A route that took
    ``account_id`` from the body would let any billing admin cancel any tenant."""
    for fn in (tc.tenant_cancel_preview, tc.tenant_cancel):
        sig = inspect.signature(fn)
        dep = sig.parameters["account_id"].default
        assert getattr(dep, "dependency", None) is tc.require_billing_access, fn.__name__
        # No account/tenant identifier may be accepted from the caller.
        assert "account_id" not in {p for p in sig.parameters if p != "account_id"}
    body = inspect.signature(tc.tenant_cancel).parameters["body"].annotation
    assert "account_id" not in body.model_fields
    assert "user_id" not in body.model_fields


def test_no_session_is_401_and_no_csrf_is_403():
    """Fail-CLOSED at the transport, proven by driving it rather than by reading the dependency."""
    app = FastAPI()
    app.include_router(tc.router)
    c = TestClient(app)
    assert c.get("/tenant/cancel/preview").status_code == 401
    assert c.post("/tenant/cancel", json={"signature": "X"}).status_code == 401

    sub, acct = str(uuid.uuid4()), str(uuid.uuid4())
    c.cookies.set(auth.SESSION_COOKIE_NAME,
                  auth.issue_session(dashboard_user_id=sub, account_id=acct, username="u"))
    # A valid session with NO csrf header must still be refused on the unsafe method.
    assert c.post("/tenant/cancel", json={"signature": "X"}).status_code == 403


def test_a_signature_is_required_before_anything_can_move():
    """An unsigned confirm changes nothing — checked in ``cancel_account`` BEFORE the kill-switch,
    before the quote, and before any Stripe call, so there is no ordering in which an unsigned
    request reaches a write."""
    for bad in ("", "   ", "x"):
        r = accel.cancel_account("00000000-0000-0000-0000-000000000000", signature=bad)
        assert r["ok"] is False and r["status"] == "unsigned", bad
        assert r["signed"] is False
    # And the signature check precedes the flag read and the quote in the SOURCE, not just here.
    src = _src(accel.cancel_account)
    assert src.index("unsigned") < src.index("acceleration_enabled")
    assert src.index("unsigned") < src.index("quote(account_id)")


def test_the_status_map_never_blames_the_customer_for_our_fault():
    """Our faults are 5xx; only the customer's own input is 4xx.

    An unmapped status defaults to 502, never to a 400 — a route that answers "bad request" when
    Stripe is unreachable teaches the customer to retry a thing that cannot work."""
    m = tc._CANCEL_STATUS_HTTP
    for ours in ("stop_failed", "provider_unavailable", "stripe_unreadable", "unknown_item",
                 "claim_failed", "refused", "booked_failed", "not_quotable"):
        assert m[ours] >= 500, ours
    assert m["unsigned"] == 400
    for conflict in ("amount_changed", "already_open", "already_invoiced", "in_flight"):
        assert m[conflict] == 409, conflict
    assert m.get("nonexistent_status_xyz", 502) == 502


# ══════════════════════════════════════════════════════════════════════════════════
# PART B — CONSENT: the preview writes nothing, and the amount is held to
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_preview_has_no_write_in_it():
    """READ-ONLY, structurally. ``quote`` and its one helper must contain no INSERT/UPDATE/DELETE
    and must not reach any Stripe write seam. A preview with a side effect is a cancellation the
    customer never agreed to."""
    for fn in (accel.quote, accel._elastic_preview, accel.resolutions_text):
        s = _src(fn).upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "_WRITE_LEDGER", "_APPLY_ITEM_MUTATION",
                     "CREATE_ONE_OFF_INVOICE", "SCHEDULE_SUBSCRIPTION_CANCELLATION",
                     "VOID_INVOICE", "_CLAIM_CANCELLATION", "ACCELERATE("):
            assert verb not in s, f"{fn.__name__} contains {verb!r}"


def test_the_route_passes_the_acknowledged_amount_straight_through():
    """The console cannot opt out of the consent check by omitting the field — the route forwards
    whatever arrived, and the module decides. (A transport that defaulted a missing acknowledgement
    to "whatever it currently is" would turn the guard off for every caller.)"""
    src = _src(tc.tenant_cancel)
    assert "acknowledged_amount_cents=body.acknowledged_amount_cents" in src.replace(" ", "")


def test_a_stale_acknowledged_amount_is_refused_not_charged(monkeypatch):
    """THE CONSENT CHECK. The figure moved between preview and confirm ⇒ refuse and re-show.

    Teeth: the fake quote is quotable and complete in every respect EXCEPT that the acknowledged
    number differs, so nothing but the guard under test can produce the refusal."""
    monkeypatch.setattr(accel, "quote", lambda a: {
        "quotable": True, "status": "quotable", "amount_cents": 600000,
        "months_remaining": 4, "base_fee_cents": 150000, "currency": "CAD",
        "subscription_id": "sub_x"})
    called = {"n": 0}
    def _claim(*a, **k):
        called["n"] += 1
        return {"ok": False, "status": "claim_failed", "row_id": None, "reason": "stubbed"}
    monkeypatch.setattr(accel, "_claim_cancellation", _claim)
    r = accel.cancel_account("acct", signature="Real Name",
                             acknowledged_amount_cents=599999)
    assert r["ok"] is False and r["status"] == "amount_changed"
    assert called["n"] == 0, "a stale amount must not reach the claim, let alone Stripe"
    # The matching amount proceeds past the guard (it then stops at the stubbed claim).
    accel.cancel_account("acct", signature="Real Name", acknowledged_amount_cents=600000)
    assert called["n"] == 1


# ══════════════════════════════════════════════════════════════════════════════════
# PART C — ORDERING: stop before invoice, strip before amount
# ══════════════════════════════════════════════════════════════════════════════════

def test_collection_is_stopped_before_the_lump_is_invoiced():
    """THE ORDER THAT CANNOT OVER-BILL, pinned structurally.

    ``months_remaining`` counts the FUTURE monthly invoices. Invoice them as a lump while the
    monthly cycle still runs and the customer pays twice for the same months. Stop-then-invoice
    fails toward under-collecting (recoverable); invoice-then-stop fails toward an over-charge on
    a customer-visible surface (not recoverable)."""
    src = _src(accel.cancel_account)
    # ⚠️ rindex, NOT index. ``_stop_at_period_boundary`` is ALSO called earlier, in the
    # "nothing owed" branch, and an index-based pin therefore stayed GREEN under a mutation that
    # moved the real acceleration ahead of the real stop. A pin that the mutation cannot move is
    # not a pin. (Measured: mutation M2 passed this assertion before it was rindex'd.)
    assert src.rindex("_stop_at_period_boundary") < src.index("accelerate(account_id")


def test_a_failed_stop_refuses_and_invoices_nothing(monkeypatch):
    """The behavioural half of the ordering: if collection cannot be stopped, NOTHING is raised."""
    monkeypatch.setattr(accel, "quote", lambda a: {
        "quotable": True, "status": "quotable", "amount_cents": 600000,
        "months_remaining": 4, "base_fee_cents": 150000, "currency": "CAD",
        "subscription_id": "sub_x"})
    monkeypatch.setattr(accel, "_claim_cancellation",
                        lambda *a, **k: {"ok": True, "status": "claimed", "row_id": 1,
                                         "reason": None})
    monkeypatch.setattr(accel, "_fail_row", lambda *a, **k: None)
    monkeypatch.setattr(accel, "_stop_at_period_boundary",
                        lambda s: {"ok": False, "reason": "provider_unavailable"})
    boom = lambda *a, **k: pytest.fail("accelerate() ran after the stop FAILED")  # noqa: E731
    monkeypatch.setattr(accel, "accelerate", boom)
    r = accel.cancel_account("acct", signature="Real Name")
    assert r["ok"] is False and r["status"] == "stop_failed"


def test_the_stop_uses_stripes_own_boundary_and_never_computes_a_date():
    """MEASURED, not reasoned (module docstring probe D): a ``cancel_at`` even three days inside
    the current period drags ``items.current_period_end`` back to match it, truncating a month the
    customer already paid for. So the date must be Stripe's own ``current_period_end``, used
    verbatim, and an unreadable boundary must REFUSE rather than fall back to arithmetic."""
    src = _src(accel._stop_at_period_boundary)
    assert "snap.get('current_period_end')" in src
    assert "no_period_end" in src
    for computed in ("_add_months", "timedelta(", "term_end"):
        assert computed not in src, f"the stop date must not be computed ({computed})"


def test_strip_runs_before_the_amount_is_computed_and_gates_the_invoice():
    """The SAFETY property, unchanged by this wiring and re-pinned because the new door depends on
    it: if the seat items are not on the subscription, no arithmetic downstream can accelerate
    them. ``accelerate`` strips first and refuses unless ``strip_verified``."""
    src = _src(accel.accelerate)
    assert src.index("strip_elastic_items") < src.index("signed_base_fee(subscription_id)")
    assert src.index("strip_elastic_items") < src.index("months_remaining(now,")
    assert src.index("strip.get('verified')") < src.index("create_one_off_invoice")


def test_an_unverified_strip_blocks_the_invoice_outright(monkeypatch):
    """🔴 THE GUARD-COUNT LESSON, PAID AGAIN IN THIS SUITE. The structural pin above survived a
    mutation to ``if False and not strip.get('verified')`` — the token was still present and still
    ahead of the invoice call, so the assertion held while the gate was gone. A property that can
    be neutered without moving any token has to be DRIVEN.

    Here the strip reports 3 deletes issued but verification FAILED (Stripe could not be re-read),
    which is precisely the state where "I issued three deletes" and "one item remains" diverge —
    and where invoicing anyway could accelerate seats that are still on the subscription."""
    monkeypatch.setattr(accel, "acceleration_enabled", lambda: True)
    monkeypatch.setattr(accel._seat_sync, "_subscription_for", lambda a: ("sub_x", {}))
    monkeypatch.setattr(accel._seat_sync, "term_of_record", lambda a: {
        "ok": True, "in_term": True, "term_months": 6, "term_end": "2027-01-01",
        "term_end_dt": None})
    monkeypatch.setattr(accel, "_open_row", lambda cur, a: None)
    monkeypatch.setattr(accel, "_record_strip", lambda *a, **k: None)
    monkeypatch.setattr(accel, "_fail_row", lambda *a, **k: None)
    monkeypatch.setattr(accel, "strip_elastic_items", lambda *a, **k: {
        "ok": False, "stripped": 3, "remaining": 3, "verified": False,
        "base_present": True, "failures": [], "reason": "post_strip_unreadable"})
    monkeypatch.setattr(accel, "signed_base_fee",
                        lambda s: pytest.fail("the amount was computed on an UNVERIFIED strip"))
    monkeypatch.setattr(accel._seat_sync, "_provider",
                        lambda: pytest.fail("Stripe was touched after an unverified strip"))

    class _Conn:
        def cursor(self):
            return self
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **k):
            return None
        def fetchone(self):
            return (4242,)
        def commit(self):
            return None
    monkeypatch.setattr(accel.db, "connection", lambda: _Conn())

    r = accel.accelerate("acct", trigger=accel.TRIGGER_CANCELLATION)
    assert r["ok"] is False
    assert r["status"] == "refused"
    assert r["reason"] == "strip_post_strip_unreadable"


# ══════════════════════════════════════════════════════════════════════════════════
# PART D — the WEBHOOK: status, never an amount; ours, never anyone else's
# ══════════════════════════════════════════════════════════════════════════════════

def test_invoice_voided_is_registered_and_invoice_paid_still_is():
    assert hooks._HANDLERS["invoice.voided"] is hooks.handle_invoice_voided
    assert hooks._HANDLERS["invoice.paid"] is hooks.handle_invoice_paid


def test_resolution_keys_on_status_and_never_on_an_amount():
    """🔴 THE ONE THAT WOULD SURVIVE A GREEN SUITE. Measured live: a VOIDED Stripe invoice still
    reports ``total``/``amount_due``/``amount_remaining`` at their FULL original values
    (in_…P0t5hMY1: status='void', total=600000, amount_due=600000). "Zero-value for reporting" is
    about Stripe's reporting, not the API object. So an amount-keyed resolver reads a voided
    invoice as still owed and — with the opposite comparison — a paid one as resolved for the
    wrong reason. Nothing in either resolution path may look at an amount to DECIDE."""
    settle_src = _src(accel.settle)
    decide = settle_src.split("credit_cents")[0]      # everything up to the "how much" question
    assert "inv.get('status')" in decide
    assert "inv_status != 'paid'" in decide
    for amount_key in ("amount_due", "amount_remaining", "total_cents"):
        assert amount_key not in decide, f"settle decides on {amount_key}"
    resume_src = _src(accel.resume)
    for amount_key in ("amount_due", "amount_remaining", "amount_paid"):
        assert amount_key not in resume_src, f"resume decides on {amount_key}"


def test_only_our_own_metadata_marker_reaches_a_resolver():
    """The discriminator is metadata WE wrote, not a heuristic over the description or amount."""
    assert hooks._acceleration_ref({"id": "in_x"}) is None
    assert hooks._acceleration_ref({"id": "in_x", "metadata": {"faultline_kind": "other"}}) is None
    acct = str(uuid.uuid4())
    ref = hooks._acceleration_ref({"id": "in_x", "metadata": {
        "faultline_kind": "term_acceleration", "faultline_account_id": acct,
        "faultline_acceleration_id": "77"}})
    assert ref == {"account_id": acct, "acceleration_id": "77"}


def test_an_unattributable_acceleration_invoice_is_refused_not_guessed():
    """A malformed account id must REFUSE. Resolving the wrong account would settle or void
    somebody else's receivable — the one failure mode with no cheap remedy."""
    assert hooks._acceleration_ref({"id": "in_x", "metadata": {
        "faultline_kind": "term_acceleration", "faultline_account_id": "not-a-uuid"}}) is None
    assert hooks._acceleration_ref({"id": "in_x", "metadata": {
        "faultline_kind": "term_acceleration"}}) is None


def test_an_ordinary_renewal_invoice_is_untouched_by_the_acceleration_branch(monkeypatch):
    """A normal ``invoice.paid`` must still renew. The branch must not swallow the renewal path."""
    seen = {}
    monkeypatch.setattr(hooks._state, "record_charge", lambda p: seen.setdefault("charge", p))
    monkeypatch.setattr(hooks._state, "upsert_subscription", lambda p: seen.setdefault("sub", p))
    monkeypatch.setattr(hooks, "_existing_subscription", lambda s: {"account_id": None,
                                                                    "user_id": None,
                                                                    "plan_id": "enterprise",
                                                                    "price_id": None})
    monkeypatch.setattr(hooks, "_grant_entitlement",
                        lambda *a, **k: seen.setdefault("grant", True))
    monkeypatch.setattr(hooks, "_provision_account",
                        lambda *a, **k: seen.setdefault("provision", True))
    out = hooks.handle_invoice_paid({"data": {"object": {
        "id": "in_renewal", "subscription": "sub_1", "customer": "cus_1",
        "charge": "ch_1", "amount_paid": 150000, "currency": "cad", "metadata": {}}}})
    assert "acceleration" not in out
    assert seen.get("grant") and seen.get("provision") and seen.get("sub")


def test_an_acceleration_invoice_never_re_grants_or_re_provisions(monkeypatch):
    """A settled cancellation invoice is NOT a renewal. Re-granting an entitlement or
    re-provisioning an account that is closing would resurrect the very thing being cancelled —
    and ``_provision_account`` on the renewal path is documented to re-derive account state."""
    acct = str(uuid.uuid4())
    monkeypatch.setattr(accel, "settle", lambda a, **k: {"ok": True, "status": "settled"})
    monkeypatch.setattr(hooks._state, "record_charge", lambda p: None)
    for forbidden in ("_grant_entitlement", "_provision_account"):
        monkeypatch.setattr(hooks, forbidden,
                            lambda *a, **k: pytest.fail(f"{forbidden} ran for an acceleration"))
    out = hooks.handle_invoice_paid({"data": {"object": {
        "id": "in_accel", "customer": "cus_1", "charge": "ch_1", "amount_paid": 600000,
        "currency": "cad", "metadata": {"faultline_kind": "term_acceleration",
                                        "faultline_account_id": acct}}}})
    assert out["acceleration"]["status"] == "settled"
    assert out["acceleration"]["action"] == "settle"


def test_a_resolver_refusal_is_logged_not_raised(monkeypatch):
    """A refusal ("nothing open", "not actually paid") is a CONSIDERED outcome. Raising would mark
    the whole event 'error' and have the reprocessor retry a decision that was correct."""
    acct = str(uuid.uuid4())
    monkeypatch.setattr(accel, "resume",
                        lambda a, **k: {"ok": False, "status": "provider_unavailable"})
    out = hooks.handle_invoice_voided({"data": {"object": {
        "id": "in_v", "status": "void", "metadata": {
            "faultline_kind": "term_acceleration", "faultline_account_id": acct}}}})
    assert out["acceleration"]["ok"] is False


def test_a_void_on_someone_elses_invoice_is_acknowledged_without_action(monkeypatch):
    monkeypatch.setattr(accel, "resume",
                        lambda *a, **k: pytest.fail("resume ran for a foreign invoice"))
    out = hooks.handle_invoice_voided({"data": {"object": {"id": "in_other", "status": "void"}}})
    assert "acceleration" not in out


# ══════════════════════════════════════════════════════════════════════════════════
# PART E — the SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════════

def test_the_sweeps_ride_the_existing_retention_worker():
    """ONE scheduler, not a second one. A second loop is a second thing to fail silently."""
    main_src = open(os.path.join(os.path.dirname(__file__), "..",
                                 "src", "api", "main.py"), encoding="utf-8").read()
    assert "from saas import billing_sweeps as _sweeps" in main_src
    assert "_sweeps.run_sweeps()" in main_src
    worker = main_src.index("async def _retention_worker")
    assert worker < main_src.index("_sweeps.run_sweeps()") < main_src.index(
        "asyncio.create_task(_retention_worker())")


def test_both_sweeps_run_and_a_failure_in_one_does_not_stop_the_other(monkeypatch):
    """Fault isolation: the tenant whose reconciliation is skipped is not the tenant whose
    commitment check raised."""
    import saas.stripe_seat_sync as sync_mod
    monkeypatch.setattr(sweeps_mod, "sweeps_enabled", lambda: True)
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    ran = {}
    monkeypatch.setattr(sync_mod, "sweep_broken_commitments",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sync_mod, "reconcile",
                        lambda **k: ran.setdefault("rec", {"ok": True, "findings": []}))
    out = sweeps_mod.run_sweeps()
    assert out["ran"] is True
    assert out["ok"] is False                       # honest: something failed
    assert out["commitments"]["ok"] is False
    assert ran.get("rec") is not None, "the reconciler must still run"


def test_a_clean_sweep_says_nothing_to_do_out_loud(monkeypatch, capsys):
    """A sweep that is silent when clean cannot be told apart from a sweep that is not running —
    which is the exact defect this module was written to fix, recreated one level up."""
    import saas.stripe_seat_sync as sync_mod
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    monkeypatch.setattr(sync_mod, "sweep_broken_commitments",
                        lambda: {"ok": True, "broken": [], "count": 0})
    monkeypatch.setattr(sync_mod, "reconcile", lambda **k: {"ok": True, "findings": []})
    sweeps_mod.run_sweeps()
    err = capsys.readouterr().err
    assert err.count("nothing to do") == 2


def test_disabled_is_reported_distinctly_from_clean(monkeypatch):
    """"Ran and found nothing" and "did not run" must never collapse into one summary — only one
    of them is healthy."""
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True)
    monkeypatch.setenv("SAAS_BILLING_SWEEPS", "false")
    out = sweeps_mod.run_sweeps()
    assert out["ran"] is False and out["reason"] == "disabled"


def test_the_deep_reconcile_is_opt_in_and_the_sweeps_are_on_by_default(monkeypatch):
    """Unset is not off. The cheap pass runs everywhere; the one that asks Stripe about every
    account is opt-in."""
    monkeypatch.delenv("SAAS_BILLING_SWEEPS", raising=False)
    monkeypatch.delenv("SAAS_BILLING_SWEEP_DEEP", raising=False)
    assert sweeps_mod.sweeps_enabled() is True
    assert sweeps_mod.deep_reconcile_enabled() is False
    monkeypatch.setenv("SAAS_BILLING_SWEEP_DEEP", "true")
    assert sweeps_mod.deep_reconcile_enabled() is True


def test_the_sweeps_never_write_to_stripe():
    """Read-mostly by construction: the sweep module itself holds no Stripe write seam."""
    s = _src(sweeps_mod.run_sweeps)
    for write in ("Subscription.modify", "Invoice.", "sync_account", "_apply_item_mutation",
                  "accelerate(", "request_cancellation"):
        assert write not in s, write


# ══════════════════════════════════════════════════════════════════════════════════
# PART F — DB-backed: the claim is serialized, and one open acceleration per account
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def dsn():
    os.environ["POSTGRES_DSN"] = TEST_DSN
    if "test" not in TEST_DSN:
        pytest.fail(f"refusing to run against a DSN with no 'test' in its name: {TEST_DSN}")
    try:
        with db.connection() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('public.term_accelerations')")
                assert cur.fetchone()[0] is not None, "migration 254 is not applied"
            c.commit()
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"test DB unreachable ({exc}) — this suite must not silently skip")
    _DB_RAN["value"] = True
    return TEST_DSN


def _acct(dsn):
    aid = str(uuid.uuid4())
    with db.connection() as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO public.accounts (account_id, name, slug, status) "
                        "VALUES (%s,%s,%s,'active')", (aid, "FIXACCEL claim", f"fa-{aid[:8]}"))
        c.commit()
    return aid


def test_a_double_submit_claims_once_and_the_second_is_told_to_wait(dsn):
    """🔴 THE DEDUPE THAT THE UNIQUE INDEX ALONE DOES NOT GIVE.

    The DB's partial unique index stops two OPEN rows. It does NOT stop the real double-submit
    hazard, because ``accelerate`` REUSES a ``pending`` row: a second confirm arriving while the
    first is still inside its Stripe round trip would find that row, reuse it, and run the whole
    strip-and-invoice again. Stripe's idempotency key would absorb the duplicate INVOICE — but
    both requests would then write their own ``term_acceleration`` LEDGER row, double-counting one
    receivable on the operator's Billing card."""
    aid = _acct(dsn)
    first = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    assert first["ok"] is True and first["status"] == "claimed"
    second = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    assert second["ok"] is False and second["status"] == "in_flight"
    assert second["row_id"] == first["row_id"], "it must point at the claim already running"
    with db.connection() as c:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.term_accelerations WHERE account_id=%s",
                        (aid,))
            assert cur.fetchone()[0] == 1
        c.commit()


def test_a_stale_pending_row_is_reclaimed_rather_than_stranding_the_customer(dsn):
    """A genuinely FAILED earlier attempt must not lock the customer out forever."""
    aid = _acct(dsn)
    first = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    with db.connection() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE public.term_accelerations SET created_at = NOW() - "
                        "INTERVAL '1 hour' WHERE id = %s", (first["row_id"],))
        c.commit()
    again = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    assert again["ok"] is True and again["status"] == "reclaimed"
    assert again["row_id"] == first["row_id"]


def test_an_already_invoiced_acceleration_is_returned_not_re_raised(dsn):
    """Idempotent at the top: a customer who resubmits after the invoice exists gets THAT invoice,
    never a second one."""
    aid = _acct(dsn)
    first = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    with db.connection() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE public.term_accelerations SET status='invoiced', "
                        "  stripe_invoice_id='in_test', amount_cents=600000, "
                        "  months_remaining=4, base_fee_cents=150000 WHERE id=%s",
                        (first["row_id"],))
        c.commit()
    again = accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    assert again["ok"] is True and again["status"] == "already_invoiced"
    assert again["existing"]["stripe_invoice_id"] == "in_test"


def test_the_quote_refuses_while_an_acceleration_is_already_open(dsn):
    """Offering a fresh quote over an outstanding invoice invites a second one."""
    aid = _acct(dsn)
    accel._claim_cancellation(aid, subscription_id="sub_x", actor="t", actor_user="u")
    q = accel.quote(aid)
    assert q["quotable"] is False and q["status"] == "already_open"
    assert q["existing"] is not None


def test_the_advisory_key_is_deterministic_and_namespaced():
    """A lock key that changes across a PostgreSQL upgrade is a guard that silently stops guarding
    — which is why it is blake2b in Python and never ``hashtext()``."""
    a = str(uuid.uuid4())
    assert accel._advisory_key(a) == accel._advisory_key(a)
    assert accel._advisory_key(a) != accel._advisory_key(str(uuid.uuid4()))
    assert -(2 ** 63) <= accel._advisory_key(a) < 2 ** 63
    assert "hashtext" not in _src(accel._advisory_key)
    # A different namespace from the contracts guard, so the two never serialize against each
    # other for the same account.
    from saas import contracts as contracts_mod
    assert accel._ADVISORY_LOCK_NAMESPACE != contracts_mod._ADVISORY_LOCK_NAMESPACE


# ══════════════════════════════════════════════════════════════════════════════════
# PART G — the console surface
# ══════════════════════════════════════════════════════════════════════════════════

def _dash(name):
    return open(os.path.join(os.path.dirname(__file__), "..", "saas", "dashboard", name),
                encoding="utf-8").read()


def test_the_console_has_a_cancel_control_wired_without_inline_script():
    """CSP is ``script-src 'self'`` with no ``'unsafe-inline'`` — an inline-script build renders
    BLANK. Asserted as 0/0 over the whole page, not just over the new block."""
    html = _dash("tenant.html")
    assert "btn-cancel-review" in html and "cancel-quote" in html
    assert html.count("<script>") == 0
    import re
    assert len(re.findall(r"\son[a-z]+=", html)) == 0
    js = _dash("tenant.js")
    for fn in ("loadCancelPanel", "renderCancelQuote", "confirmCancel", "resetCancelPanel"):
        assert f"function {fn}" in js, fn
    assert "$('btn-cancel-review')" in js and "bcr.onclick = loadCancelPanel" in js


def test_the_console_confirms_with_a_signature_and_the_amount_it_displayed():
    """The UI half of the consent check: the confirm posts the number the customer just read."""
    js = _dash("tenant.js")
    block = js[js.index("function confirmCancel"):js.index("function renderCancelDone")]
    assert "signAction(" in block
    assert "'/tenant/cancel'" in block
    assert "acknowledged_amount_cents" in block
    assert "signature: name" in block


def test_the_confirm_button_only_exists_once_a_quote_is_on_screen():
    """No confirm without a displayed price — the button is MINTED by the renderer, so it cannot
    be reached before the amount has been fetched and shown."""
    js = _dash("tenant.js")
    assert "function cancelConfirmButton" in js
    render = js[js.index("function renderCancelQuote"):js.index("function ex_amount")]
    assert "cancelConfirmButton(" in render
    html = _dash("tenant.html")
    assert "btn-cancel-confirm" not in html, "the confirm button must not be static markup"


# ══════════════════════════════════════════════════════════════════════════════════
# FALSE-GREEN GUARD — must be LAST
# ══════════════════════════════════════════════════════════════════════════════════

def test_no_db_test_was_silently_skipped():
    """Turns "PASS | N passed, M skipped" into a RED. A DSN-gated suite that skips its DB half
    looks identical to one that passed it."""
    assert _DB_RAN["value"] is True, (
        "the DB-backed half never ran — set POSTGRES_DSN (or CANCEL_TEST_DSN) to a *_test "
        "database with migration 254 applied. A skipped half is not a pass.")
