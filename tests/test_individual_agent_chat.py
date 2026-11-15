"""SEAT-SCOPED chat-agent ("agent chat") add-on for the self-serve INDIVIDUAL (migration 250).

WHAT THIS SUITE IS FOR, IN ONE SENTENCE: an individual must be able to BUY agent chat, and
buying it must entitle THEM AND NOBODY ELSE — even though every self-serve individual is a
seat inside ONE shared house account.

THE PRIMARY FAILURE MODE, AND THE TEST THAT CATCHES IT. The pre-existing entitlement lives on
``public.accounts.agent_feature_enabled`` / ``agent_seat_bucket`` — an ACCOUNT-scoped pair.
That is right for a tenant (the account IS the customer) and wrong for an individual: the
``individual_house`` account is a single row shared by everyone who signed up self-serve.
Selling on that row would (a) enable the feature for every individual the moment one paid, and
(b) make the account-wide bucket count ``COUNT(active agent seats) <= bucket`` refuse both
buyers as soon as a second one qualified. ``test_buying_for_one_individual_does_not_entitle_a
_sibling_in_the_same_house`` is the guard, and ``test_teeth_*`` proves it has teeth by
reproducing the account-scoped grant and watching the sibling light up.

Everything DB-backed runs against a real migrated control plane. The ``_DB_RAN`` guard turns
the run RED rather than green-and-silent if the DB half is skipped.
"""

import os as _os
import uuid as _uuid

import pytest

from saas import agent_entitlement as _ae
from saas import entitlements as _ent
from saas import plans as _plans

# ── _DB_RAN GUARD ────────────────────────────────────────────────────────────────────
# A suite about a DB-backed entitlement that quietly skips its DB half is worse than no
# suite: it reports green while proving nothing. test_zzz_* turns that into a failure.
_DB_RAN = {"n": 0}
_DSN = _os.environ.get("POSTGRES_DSN")
_TAG = _uuid.uuid4().hex[:8]

_HOUSE_KIND = "individual_house"


# ══════════════════════════════════════════════════════════════════════════════════════
# PRICING — one source, tier-independent, and 0 is never a price
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_addon_price_comes_from_exactly_one_source():
    """The console and the checkout must both read ``entitlements.agent_addon_price_for_bucket``.

    A second copy of the number is how a displayed price and a charged price drift apart, so
    this pins that ``user_console._entitlements_addon_price`` is a thin pass-through of that
    one function and not a private table."""
    from saas import user_console as _uc
    for bucket in _ae.AGENT_SEAT_BUCKETS:
        assert _uc._entitlements_addon_price(bucket) == _ent.agent_addon_price_for_bucket(bucket)


def test_the_price_is_identical_at_every_tier_and_every_account_kind():
    """THE OWNER'S STATED RULE: same price at every tier.

    Proven by construction rather than by asserting a literal: the resolver's signature takes
    ONLY a bucket, so there is no argument through which a tier or an account kind could vary
    it. The literal card is checked too, so a silent re-pricing is still caught."""
    import inspect
    params = list(inspect.signature(_ent.agent_addon_price_for_bucket).parameters)
    assert params == ["bucket"], (
        "agent_addon_price_for_bucket grew a parameter — if that parameter is a tier or an "
        "account kind, the add-on is no longer tier-independent. Re-read the owner's rule.")
    assert _ent.agent_addon_price_for_bucket(1) == 40.0
    assert _ent.agent_addon_price_for_bucket(5) == 160.0
    assert _ent.agent_addon_price_for_bucket(10) == 320.0


def test_an_unresolvable_price_renders_unknown_and_never_zero():
    """A price that cannot resolve MUST be ``None`` ("unknown"), never 0.

    ``agent_addon_price_for_bucket`` returns 0.0 for an unknown bucket — a number that reads to
    a buyer as FREE. The console-facing resolver translates every non-positive result to None
    so the surface says "unknown" and the buy path refuses."""
    from saas import user_console as _uc
    assert _ent.agent_addon_price_for_bucket(7) == 0.0        # the raw source's 0
    assert _uc._entitlements_addon_price(7) is None           # never surfaced as a price


def test_an_individual_is_offered_bucket_one_only():
    """5 and 10 cover MULTIPLE agent seats; an individual holds exactly one and cannot mint
    another (seat creation is a tenant-admin/operator function). Offering them a $160 bucket
    would charge 4x for capacity that is physically unreachable."""
    assert _ae.INDIVIDUAL_ADDON_BUCKETS == (1,)
    assert set(_ae.INDIVIDUAL_ADDON_BUCKETS).issubset(set(_ae.AGENT_SEAT_BUCKETS))


def test_the_addon_price_id_resolves_by_lookup_key_not_a_hardcoded_id():
    """A ``price_…`` id belongs to ONE Stripe mode; a lookup key answers correctly in both.

    Pins that the three buckets carry lookup keys and that nothing hardcodes a live id as the
    fallback (these Prices are created under the lookup keys; the fallback is env-only)."""
    assert _plans.AGENT_ADDON_PRICE_LOOKUP_KEYS[1] == "faultline_agent_addon_1"
    assert _plans.AGENT_ADDON_PRICE_LOOKUP_KEYS[5] == "faultline_agent_addon_5"
    assert _plans.AGENT_ADDON_PRICE_LOOKUP_KEYS[10] == "faultline_agent_addon_10"
    for bucket, fallback in _plans.AGENT_ADDON_STRIPE_PRICE_IDS.items():
        assert fallback is None or str(fallback).startswith("price_"), bucket
    assert _plans.agent_addon_price_id(7) is None      # unknown bucket → refuse, never guess


# ══════════════════════════════════════════════════════════════════════════════════════
# THE METADATA CARRIER — plan_id is what survives the round-trip through Stripe
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_addon_plan_id_namespace_cannot_collide_with_a_seat_or_tenant_tier():
    """``payments_stripe.create_checkout`` forwards only {account_id,user_id,plan_id,contract_id},
    so plan_id is the only carrier available. It must not be mistakable for a tier."""
    assert _ae.addon_plan_id(1) == "agent_chat_addon_1"
    assert _ae.parse_addon_plan_id("agent_chat_addon_1") == 1
    assert _ae.parse_addon_plan_id("agent_chat_addon_10") == 10
    # Every real tier reads as NOT an add-on → the reconciler and the webhook no-op on them.
    for tier in ("standard", "advanced", "expert", "agent", "team", "enterprise"):
        assert _ae.parse_addon_plan_id(tier) is None
    # Junk in the namespace is refused rather than half-parsed.
    for junk in ("agent_chat_addon_", "agent_chat_addon_7", "agent_chat_addon_x", "", None):
        assert _ae.parse_addon_plan_id(junk) is None


def test_the_tenant_provisioner_does_not_claim_an_addon_purchase():
    """An add-on checkout must not be mistaken for a tenant-tier purchase — that would try to
    re-provision the shared house account as a Team/Enterprise."""
    from saas import tenant_provisioning as _prov
    assert _prov.resolve_tenant_tier({"plan_id": _ae.addon_plan_id(1)}) is None


# ══════════════════════════════════════════════════════════════════════════════════════
# THE GATE — pure-function branches (no DB): fail-closed and tenant-untouched
# ══════════════════════════════════════════════════════════════════════════════════════

def test_the_seat_lane_is_unreachable_on_a_tenant_account():
    """Containment. Even a seat row carrying an enabled add-on grants nothing unless the
    account is the individual house — so a tenant's gate decision is what it always was."""
    assert _ae._seat_addon_active(_HOUSE_KIND, True, 1) is True
    for kind in ("tenant", "team", "clinical", None, "", "INDIVIDUAL_HOUSEX"):
        assert _ae._seat_addon_active(kind, True, 1) is False, kind


def test_the_seat_lane_fails_closed_on_every_unreadable_axis():
    """A half-written row, a NULL bucket or a non-True flag must all refuse. This is the belt
    to the DB CHECK's braces, and it is what protects a PRE-MIGRATION database where both
    columns read NULL for every row."""
    assert _ae._seat_addon_active(_HOUSE_KIND, None, None) is False   # pre-migration shape
    assert _ae._seat_addon_active(_HOUSE_KIND, True, None) is False   # on, unprovisioned
    assert _ae._seat_addon_active(_HOUSE_KIND, True, 0) is False
    assert _ae._seat_addon_active(_HOUSE_KIND, False, 1) is False
    assert _ae._seat_addon_active(_HOUSE_KIND, "true", 1) is False    # truthy != True
    assert _ae._seat_addon_active(_HOUSE_KIND, True, "not-a-number") is False


def test_an_entitlement_read_that_fails_denies():
    """FAIL-CLOSED on the money path: an unreadable control plane must never grant."""
    import saas.db as _db_mod

    class _Boom:
        def __enter__(self):
            raise RuntimeError("control plane down")

        def __exit__(self, *a):
            return False

    _ae.invalidate_agent_serve_cache()
    orig = _db_mod.connection
    try:
        _db_mod.connection = lambda *a, **k: _Boom()
        allowed, refusal = _ae.serve_allowed(str(_uuid.uuid4()), str(_uuid.uuid4()))
        assert allowed is False
        assert refusal["reason_code"] == "unverifiable"
        # And the seat-state read used by the console denies too, marked unreadable so the
        # surface can say "we could not check" instead of a confident OFF.
        state = _ae.seat_addon_state(str(_uuid.uuid4()), str(_uuid.uuid4()))
        assert state["enabled"] is False and state["readable"] is False
    finally:
        _db_mod.connection = orig
        _ae.invalidate_agent_serve_cache()


def test_a_grant_is_refused_for_a_bucket_or_source_outside_the_ratified_set():
    """Validation happens BEFORE any DB work, so these need no database."""
    aid, uid = str(_uuid.uuid4()), str(_uuid.uuid4())
    for bad in (0, 2, 7, 11, None, "one"):
        res = _ae.grant_seat_agent_addon(aid, uid, bad, source="stripe")
        assert res["ok"] is False and res["status_code"] == 400, bad
    res = _ae.grant_seat_agent_addon(aid, uid, 1, source="redirect")
    assert res["ok"] is False and res["status_code"] == 400
    assert "source" in res["reason"]


# ══════════════════════════════════════════════════════════════════════════════════════
# DB-BACKED — the real control plane
# ══════════════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def real_db():
    """A real migrated control plane with ONE house account and THREE individuals in it.

    ⚠️ The DB NAME must contain "test" — the sibling suites use that as their own guard and a
    throwaway without it silently skips.

    THE THREE INDIVIDUALS ARE THE THREE CASES, and the seat tiers are chosen deliberately:
      A — Agent plan (``seat_type='agent'``), BUYS the add-on   → must serve
      B — Agent plan, does NOT buy the add-on                   → must be refused, and is the
          leak subject: B satisfies every clause of the ACCOUNT lane except the entitlement,
          so if the entitlement is ever attached to the shared house account, B lights up free
      C — Standard plan, add-on flag set on the row             → must STILL be refused: the
          add-on rides an Agent-plan seat, exactly as a tenant needs an agent seat first
    plus one tenant agent seat, to prove the tenant lane is untouched."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set")
    if "test" not in _DSN.rsplit("/", 1)[-1].lower():
        pytest.skip("refusing to run against a DB whose name does not contain 'test'")
    import psycopg2
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    cur = conn.cursor()

    house = str(_uuid.uuid4())
    tenant = str(_uuid.uuid4())
    seat_a = str(_uuid.uuid4())
    seat_b = str(_uuid.uuid4())
    seat_c = str(_uuid.uuid4())
    seat_t = str(_uuid.uuid4())
    # accounts.slug is NOT NULL — pass it explicitly rather than relying on a default.
    cur.execute(
        "INSERT INTO public.accounts (account_id, name, slug, account_kind, status, seat_limit) "
        "VALUES (%s,%s,%s,%s,'active',100000)",
        (house, f"Singles {_TAG}", f"singles-{_TAG}", _HOUSE_KIND))
    cur.execute(
        "INSERT INTO public.accounts (account_id, name, slug, account_kind, status, seat_limit) "
        "VALUES (%s,%s,%s,'tenant','active',100)",
        (tenant, f"Acme {_TAG}", f"acme-{_TAG}"))
    for aid, uid, st in ((house, seat_a, "agent"), (house, seat_b, "agent"),
                         (house, seat_c, "standard"), (tenant, seat_t, "agent")):
        cur.execute(
            "INSERT INTO public.account_users (account_id, user_id, status, seat_type) "
            "VALUES (%s,%s,'active',%s)", (aid, uid, st))
    yield {"cur": cur, "house": house, "tenant": tenant,
           "a": seat_a, "b": seat_b, "c": seat_c, "t": seat_t}
    cur.execute("DELETE FROM public.accounts WHERE account_id IN (%s,%s)", (house, tenant))
    cur.close()
    conn.close()


def _reset(db):
    # Covers A, B, C and the tenant seat — every seat in both fixture accounts.
    db["cur"].execute(
        "UPDATE public.account_users SET agent_addon_enabled = FALSE, agent_addon_bucket = NULL, "
        "agent_addon_source = NULL, agent_addon_reference = NULL "
        "WHERE account_id IN (%s,%s)", (db["house"], db["tenant"]))
    # Restore the fixture's seat tiers, so a test that re-tiers a seat cannot leak into the
    # next one (A/B are Agent-plan seats, C is Standard — that IS the fixture's contract).
    db["cur"].execute(
        "UPDATE public.account_users SET seat_type = 'agent' WHERE user_id IN (%s,%s)",
        (db["a"], db["b"]))
    db["cur"].execute(
        "UPDATE public.account_users SET seat_type = 'standard' WHERE user_id = %s", (db["c"],))
    db["cur"].execute(
        "UPDATE public.accounts SET agent_feature_enabled = FALSE, agent_seat_bucket = NULL "
        "WHERE account_id IN (%s,%s)", (db["house"], db["tenant"]))
    db["cur"].execute("DELETE FROM public.stripe_subscriptions WHERE metadata->>'tag' = %s",
                      (_TAG,))
    _ae.invalidate_agent_serve_cache()


# ── 🔴 THE ISOLATION TEST ─────────────────────────────────────────────────────────────

def test_buying_for_one_individual_does_not_entitle_a_sibling_in_the_same_house(real_db):
    """🔴 THE HEADLINE. Individual A buys agent chat. Individual B — a DIFFERENT person, in the
    SAME shared house account — must NOT be entitled.

    This is the single defect that made a naive implementation unshippable: the existing
    entitlement columns live on the account, and the house account is shared by every
    self-serve individual on the platform.

    B IS ON THE ``agent`` SEAT TIER, and that is what gives this test its force. An individual
    reaches ``seat_type='agent'`` simply by being on the $120 Agent PLAN, which is a memory
    quota and a voice and has NOTHING to do with buying chat. So B satisfies EVERY clause of
    the entitlement predicate except the add-on itself; if the entitlement is ever attached to
    the shared house account, B lights up for free. An earlier cut of this test left B on
    ``standard`` and passed under an account-scoped grant for the wrong reason — a negative
    isolation assertion must make the subject satisfy every clause but the one under test, or
    it guards nothing."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    res = _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1,
                                     source="stripe", reference=f"sub_{_TAG}_a")
    assert res["ok"] is True and res["changed"] is True

    _ae.invalidate_agent_serve_cache()
    allowed_a, _ = _ae.serve_allowed(real_db["house"], real_db["a"])
    assert allowed_a is True, "the individual who PAID is not entitled"

    allowed_b, refusal_b = _ae.serve_allowed(real_db["house"], real_db["b"])
    assert allowed_b is False, (
        "🔴 CROSS-SEAT LEAK: individual B did not buy agent chat and is entitled anyway. "
        "The entitlement has been attached to the shared house ACCOUNT instead of the "
        "SEAT, so one person's $40 just bought the feature for every self-serve "
        "individual on the platform.")
    assert refusal_b["reason_code"] == "agent_feature_off"

    # And the account row was never touched — the grant is entirely seat-local.
    real_db["cur"].execute(
        "SELECT agent_feature_enabled, agent_seat_bucket FROM public.accounts "
        "WHERE account_id = %s", (real_db["house"],))
    assert real_db["cur"].fetchone() == (False, None), (
        "the seat-scoped grant wrote the ACCOUNT-scoped columns — that is the leak")
    _reset(real_db)


def test_teeth_an_account_scoped_grant_DOES_leak_to_the_sibling(real_db):
    """PROOF THE ISOLATION TEST HAS TEETH.

    Reproduce what a naive implementation would do — set the entitlement on the shared house
    ACCOUNT, the way the tenant console does — and watch individual B become entitled without
    paying. If this test ever goes green-by-not-leaking, the account lane has changed and the
    isolation guarantee above needs re-deriving, not re-asserting."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    # The naive grant: exactly what PUT /tenant/agent-feature writes. B is already on the
    # Agent plan (seat_type='agent'), which is all the account lane demands of a seat — and an
    # individual reaches that tier by paying for the PLAN, not for chat.
    real_db["cur"].execute(
        "UPDATE public.accounts SET agent_feature_enabled = TRUE, agent_seat_bucket = 10 "
        "WHERE account_id = %s", (real_db["house"],))
    _ae.invalidate_agent_serve_cache()
    try:
        allowed_b, _ = _ae.serve_allowed(real_db["house"], real_db["b"])
        assert allowed_b is True, (
            "the account-scoped grant did NOT leak — the teeth check is no longer measuring "
            "the defect it was written for; re-derive the isolation guarantee.")
    finally:
        _reset(real_db)


def test_two_individuals_who_both_buy_do_not_cancel_each_other(real_db):
    """THE SECOND HALF OF THE SAME DEFECT, and the subtler one.

    The account lane refuses when COUNT(active agent seats of the account) exceeds the bucket.
    Applied across the shared house that is mutual destruction: a second buyer pushes the count
    past the first buyer's bucket of 1 and BOTH are refused. The seat lane counts nothing but
    itself, so two buyers coexist."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1,
                               source="stripe", reference=f"sub_{_TAG}_a")
    _ae.grant_seat_agent_addon(real_db["house"], real_db["b"], 1,
                               source="stripe", reference=f"sub_{_TAG}_b")
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True
    assert _ae.serve_allowed(real_db["house"], real_db["b"])[0] is True
    _reset(real_db)


# ── ELIGIBILITY: the add-on RIDES an Agent-plan seat (owner correction) ───────────────
#
# THE RULE, and the distinction that was got wrong once: the add-on's PRICE is static across
# tiers ($40/$160/$320 for a Team, an Enterprise or an individual). ELIGIBILITY is NOT. An
# individual must hold the Agent plan AND buy the add-on, exactly as a tenant pays for an
# agent SEAT ($100/$80) AND the add-on. Dropping the seat clause made a $15 Standard seat buy
# chat for $55 against a $120 Agent plan — a pricing hole, and an asymmetry with tenants.
#
# MUTATION-CHECKED: delete `seat_type == 'agent'` from the gate predicate and
# test_a_standard_individual_with_the_addon_flag_is_still_refused goes RED.

def test_a_standard_individual_with_the_addon_flag_is_still_refused(real_db):
    """🔴 (a) THE CORRECTION'S HEADLINE. Seat C is on the STANDARD plan and carries the add-on
    flag anyway (an operator slip, a stale row, a future bug — the flag being set must not be
    sufficient). The gate must REFUSE, and the purchase door must refuse too."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    real_db["cur"].execute("SELECT seat_type FROM public.account_users WHERE user_id = %s",
                           (real_db["c"],))
    assert real_db["cur"].fetchone()[0] == "standard"

    res = _ae.grant_seat_agent_addon(real_db["house"], real_db["c"], 1, source="operator")
    assert res["ok"] is True, "the write itself is allowed; it is the SERVE that must refuse"

    allowed, refusal = _ae.serve_allowed(real_db["house"], real_db["c"])
    assert allowed is False, (
        "🔴 a STANDARD individual is serving agent chat. The seat_type='agent' clause has been "
        "dropped from the predicate again — that undercuts the $120 Agent plan with a $55 "
        "Standard+add-on path and breaks symmetry with the tenant lane. Read the ⚠️ block in "
        "saas/agent_entitlement.py before 'fixing' this.")
    assert refusal["reason_code"] == "not_agent_seat"
    assert refusal["status"] == 403

    # …and the console read reports the shortfall in words, not as a silent False.
    state = _ae.seat_addon_state(real_db["house"], real_db["c"])
    assert state["eligible"] is False and state["reason"] == "not_agent_seat"
    _reset(real_db)


def test_an_agent_plan_individual_with_the_addon_serves(real_db):
    """(b) The happy path: Agent plan + add-on = chat."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1, source="stripe",
                               reference=f"sub_{_TAG}_a")
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True
    state = _ae.seat_addon_state(real_db["house"], real_db["a"])
    assert state["eligible"] is True and state["enabled"] is True and state["reason"] is None
    _reset(real_db)


def test_an_agent_plan_individual_without_the_addon_is_refused(real_db):
    """(c) THE OTHER HALF OF THE RULE, and the one that keeps the add-on a real product: the
    Agent PLAN does not imply the add-on. Seat B holds the $120 plan and has bought nothing.

    Note this seat is ELIGIBLE (it could buy) but NOT ENTITLED (it has not) — the two are
    reported separately so the console shows a live buy button rather than an explanation."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    allowed, refusal = _ae.serve_allowed(real_db["house"], real_db["b"])
    assert allowed is False, "the Agent PLAN alone must not grant the chat add-on"
    assert refusal["reason_code"] == "agent_feature_off"
    state = _ae.seat_addon_state(real_db["house"], real_db["b"])
    assert state["eligible"] is True and state["enabled"] is False
    _reset(real_db)


def test_the_purchase_door_refuses_exactly_what_the_serve_gate_refuses(real_db):
    """🔴 SELLING SOMETHING THE GATE WILL REFUSE IS WORSE THAN REFUSING THE SALE.

    Drives the REAL route function (not a re-implementation of its rule) for all three seats:
    the Standard seat is refused 403 with a remedy; the eligible-but-unentitled Agent seat is
    offered a live buy button; the already-entitled seat is refused 409. The two refusals must
    be DISTINGUISHABLE — the console routes one to an upgrade CTA and one to "you're all set"."""
    _DB_RAN["n"] += 1
    from fastapi import HTTPException
    from saas import user_console as _uc
    _reset(real_db)

    class _Req:            # only .base_url is read, and only past the eligibility gate
        base_url = "https://example.test/"

    def _ind(uid):
        return {"account_id": real_db["house"], "user_id": uid, "email": "x@example.test"}

    body = _uc.AgentChatCheckoutRequest(bucket=1)

    # (a) STANDARD seat → 403, and the message names the remedy rather than just refusing.
    with pytest.raises(HTTPException) as exc:
        _uc.me_agent_chat_checkout(body, _Req(), _ind(real_db["c"]))
    assert exc.value.status_code == 403
    assert "agent plan" in str(exc.value.detail).lower()

    # The GET says the same thing, and does NOT offer a purchasable bucket.
    view = _uc.me_agent_chat(_ind(real_db["c"]))
    assert view["eligible"] is False
    assert view["eligibility_reason"] == "not_agent_seat"
    assert view["buckets"][0]["purchasable"] is False
    assert view["eligibility_detail"], "an ineligible seat must be TOLD why"
    # The price is still reported, and is still the same number — eligibility is gated,
    # the PRICE is not tier-dependent.
    assert view["buckets"][0]["monthly_price"] == _ent.agent_addon_price_for_bucket(1)
    assert view["price_tier_independent"] is True

    # (b) AGENT-plan seat, not yet entitled → eligible, purchasable, no exception raised.
    view_b = _uc.me_agent_chat(_ind(real_db["b"]))
    assert view_b["eligible"] is True and view_b["entitled"] is False
    assert view_b["buckets"][0]["purchasable"] is True

    # (c) ALREADY entitled → 409, distinguishable from the 403 above.
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1, source="stripe",
                               reference=f"sub_{_TAG}_a")
    with pytest.raises(HTTPException) as exc:
        _uc.me_agent_chat_checkout(body, _Req(), _ind(real_db["a"]))
    assert exc.value.status_code == 409
    _reset(real_db)


def test_the_purchase_door_refuses_a_bucket_an_individual_may_not_buy(real_db):
    """Buckets 5 and 10 cover multiple agent seats; an individual holds one and cannot mint
    another. Refused BEFORE any eligibility or price work."""
    _DB_RAN["n"] += 1
    from fastapi import HTTPException
    from saas import user_console as _uc
    _reset(real_db)

    class _Req:
        base_url = "https://example.test/"

    for bad in (5, 10):
        with pytest.raises(HTTPException) as exc:
            _uc.me_agent_chat_checkout(
                _uc.AgentChatCheckoutRequest(bucket=bad), _Req(),
                {"account_id": real_db["house"], "user_id": real_db["a"], "email": "x@e.test"})
        assert exc.value.status_code == 400
    _reset(real_db)


def test_a_tenant_seat_is_completely_unaffected_by_the_seat_lane(real_db):
    """REGRESSION FIREWALL. A tenant agent seat with no account entitlement still refuses with
    the historical reason code, and a seat-scoped add-on row on a TENANT account does not
    entitle it (the lane is house-only)."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    allowed, refusal = _ae.serve_allowed(real_db["tenant"], real_db["t"])
    assert allowed is False and refusal["reason_code"] == "agent_feature_off"

    real_db["cur"].execute(
        "UPDATE public.account_users SET agent_addon_enabled = TRUE, agent_addon_bucket = 1, "
        "agent_addon_source = 'operator' WHERE user_id = %s", (real_db["t"],))
    _ae.invalidate_agent_serve_cache()
    allowed, refusal = _ae.serve_allowed(real_db["tenant"], real_db["t"])
    assert allowed is False, "the seat lane fired on a TENANT account — containment is broken"
    assert refusal["reason_code"] == "agent_feature_off"

    # The tenant's own account-scoped lane still works exactly as before.
    real_db["cur"].execute(
        "UPDATE public.accounts SET agent_feature_enabled = TRUE, agent_seat_bucket = 1 "
        "WHERE account_id = %s", (real_db["tenant"],))
    _ae.invalidate_agent_serve_cache()
    assert _ae.serve_allowed(real_db["tenant"], real_db["t"])[0] is True
    _reset(real_db)


def test_an_inactive_account_or_seat_still_refuses_even_with_the_addon(real_db):
    """The add-on buys the FEATURE, not an exemption from status. A suspended account and a
    deactivated seat both still refuse."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1, source="stripe",
                               reference=f"sub_{_TAG}_a")
    real_db["cur"].execute("UPDATE public.accounts SET status = 'suspended' WHERE account_id = %s",
                           (real_db["house"],))
    _ae.invalidate_agent_serve_cache()
    allowed, refusal = _ae.serve_allowed(real_db["house"], real_db["a"])
    assert allowed is False and refusal["reason_code"] == "account_inactive"

    real_db["cur"].execute("UPDATE public.accounts SET status = 'active' WHERE account_id = %s",
                           (real_db["house"],))
    real_db["cur"].execute("UPDATE public.account_users SET status = 'suspended' "
                           "WHERE user_id = %s", (real_db["a"],))
    _ae.invalidate_agent_serve_cache()
    allowed, refusal = _ae.serve_allowed(real_db["house"], real_db["a"])
    assert allowed is False and refusal["reason_code"] == "seat_inactive"
    real_db["cur"].execute("UPDATE public.account_users SET status = 'active' "
                           "WHERE user_id = %s", (real_db["a"],))
    _reset(real_db)


def test_the_grant_is_idempotent_across_stripe_redeliveries(real_db):
    """Stripe redelivers for up to three days and the reconciler sweeps on top of that, so one
    purchase reaches the grant many times. A repeat must be a reported no-op, not a re-write."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    ref = f"sub_{_TAG}_a"
    first = _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1,
                                       source="stripe", reference=ref)
    assert first["changed"] is True
    for _ in range(3):
        again = _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1,
                                           source="stripe", reference=ref)
        assert again["ok"] is True and again["changed"] is False
    _reset(real_db)


def test_the_db_refuses_an_enabled_addon_with_no_bucket(real_db):
    """Migration 250's CHECK. "On but unprovisioned" is a state the gate would have to guess
    about, so the database refuses to store it."""
    _DB_RAN["n"] += 1
    import psycopg2
    _reset(real_db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        real_db["cur"].execute(
            "UPDATE public.account_users SET agent_addon_enabled = TRUE, "
            "agent_addon_bucket = NULL WHERE user_id = %s", (real_db["a"],))
    _reset(real_db)


def test_the_db_refuses_a_bucket_outside_the_ratified_set(real_db):
    _DB_RAN["n"] += 1
    import psycopg2
    _reset(real_db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        real_db["cur"].execute(
            "UPDATE public.account_users SET agent_addon_enabled = TRUE, "
            "agent_addon_bucket = 3 WHERE user_id = %s", (real_db["a"],))
    _reset(real_db)


# ── THE GRANT TRIGGER: the signed webhook's snapshot, never the redirect ──────────────

def _sub_row(db, *, user_id, plan_id, status, sub_id):
    """Write the snapshot row EXACTLY as saas/stripe_state.upsert_subscription does when the
    signature-verified webhook processes a completed checkout."""
    db["cur"].execute("INSERT INTO public.stripe_customers (customer_id, livemode) "
                      "VALUES (%s, false) ON CONFLICT DO NOTHING", (f"cus_{_TAG}",))
    db["cur"].execute(
        "INSERT INTO public.stripe_subscriptions "
        "  (subscription_id, customer_id, account_id, user_id, status, plan_id, livemode, "
        "   metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,false,%s::jsonb) "
        "ON CONFLICT (subscription_id) DO UPDATE SET status = EXCLUDED.status, "
        "  plan_id = EXCLUDED.plan_id, updated_at = NOW()",
        (sub_id, f"cus_{_TAG}", db["house"], user_id, status, plan_id,
         '{"tag": "%s"}' % _TAG))


def test_the_redirect_grants_nothing_only_the_webhook_snapshot_does(real_db):
    """🔴 A REDIRECT PROVES ONLY THAT A BROWSER CAME BACK.

    Two halves. (1) With NO subscription snapshot in existence — i.e. someone who bounced off
    the Stripe page, or who simply loaded the success URL by hand — a full reconcile grants
    nothing. (2) Once the signature-verified webhook has written its snapshot row, the SAME
    reconcile grants. The row is the only difference, and nothing a browser does can write it."""
    _DB_RAN["n"] += 1
    _reset(real_db)

    out = _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"],
                                                user_id=real_db["a"])
    assert out["granted"] == 0
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is False, (
        "🔴 an entitlement appeared with NO signature-verified payment behind it")

    _sub_row(real_db, user_id=real_db["a"], plan_id=_ae.addon_plan_id(1),
             status="active", sub_id=f"sub_{_TAG}_hook")
    out = _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"],
                                                user_id=real_db["a"])
    assert out["granted"] == 1
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True
    # …and the sibling is STILL not entitled after a real payment landed for A.
    assert _ae.serve_allowed(real_db["house"], real_db["b"])[0] is False
    _reset(real_db)


def test_a_cancelled_subscription_revokes_the_seat(real_db):
    """The reconciler revokes as well as grants, so a cancellation stops serving without a
    second wire into the webhook module."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _sub_row(real_db, user_id=real_db["a"], plan_id=_ae.addon_plan_id(1),
             status="active", sub_id=f"sub_{_TAG}_c")
    _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"], user_id=real_db["a"])
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True

    real_db["cur"].execute("UPDATE public.stripe_subscriptions SET status = 'canceled' "
                           "WHERE subscription_id = %s", (f"sub_{_TAG}_c",))
    out = _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"],
                                                user_id=real_db["a"])
    assert out["revoked"] == 1
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is False
    _reset(real_db)


def test_the_reconciler_never_revokes_an_operator_comp(real_db):
    """A comp is a signed human decision; Stripe has no opinion on it. Only a ``stripe``-sourced
    grant may be revoked by the absence of a Stripe subscription."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1, source="operator")
    _sub_row(real_db, user_id=real_db["a"], plan_id=_ae.addon_plan_id(1),
             status="canceled", sub_id=f"sub_{_TAG}_comp")
    out = _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"],
                                                user_id=real_db["a"])
    assert out["revoked"] == 0
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True
    _reset(real_db)


def test_a_non_addon_subscription_is_ignored_by_the_reconciler(real_db):
    """An individual PLAN upgrade (plan_id='expert') must never be read as an add-on purchase."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _sub_row(real_db, user_id=real_db["a"], plan_id="expert", status="active",
             sub_id=f"sub_{_TAG}_plan")
    out = _ae.reconcile_seat_addons_from_stripe(account_id=real_db["house"],
                                                user_id=real_db["a"])
    assert out["granted"] == 0 and out["checked"] == 0
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is False
    _reset(real_db)


def test_apply_stripe_checkout_grants_from_verified_event_metadata(real_db):
    """The immediate, single-purchase form — for a caller that already holds the signed event.
    Non-add-on metadata is a clean no-op, which is what every tenant tier and every individual
    plan checkout produces."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    meta = {"account_id": real_db["house"], "user_id": real_db["a"],
            "plan_id": _ae.addon_plan_id(1)}
    out = _ae.apply_stripe_checkout(meta, subscription_id=f"sub_{_TAG}_apply")
    assert out["applied"] is True and out["bucket"] == 1
    assert _ae.serve_allowed(real_db["house"], real_db["a"])[0] is True
    assert _ae.serve_allowed(real_db["house"], real_db["b"])[0] is False
    _reset(real_db)

    for other in ("team", "enterprise", "expert", None):
        out = _ae.apply_stripe_checkout({"account_id": real_db["house"],
                                         "user_id": real_db["a"], "plan_id": other})
        assert out["applied"] is False and out["reason"] == "not_an_agent_chat_addon", other


def test_apply_stripe_checkout_never_raises(real_db):
    """It is called from a webhook handler that has already recorded a payment; a fault here
    must not be able to turn a successful paid grant into an errored event row."""
    _DB_RAN["n"] += 1
    for junk in (None, {}, {"plan_id": _ae.addon_plan_id(1)}, {"plan_id": object()}):
        out = _ae.apply_stripe_checkout(junk)          # type: ignore[arg-type]
        assert out["applied"] is False


def test_seat_addon_state_reports_the_seat_and_not_the_account(real_db):
    """The console read is per-seat: A sees active, B sees not-active, in the same house."""
    _DB_RAN["n"] += 1
    _reset(real_db)
    _ae.grant_seat_agent_addon(real_db["house"], real_db["a"], 1, source="stripe",
                               reference=f"sub_{_TAG}_a")
    a = _ae.seat_addon_state(real_db["house"], real_db["a"])
    b = _ae.seat_addon_state(real_db["house"], real_db["b"])
    assert a["enabled"] is True and a["bucket"] == 1 and a["readable"] is True
    assert b["enabled"] is False and b["readable"] is True
    _reset(real_db)


def test_zzz_no_db_test_was_silently_skipped():
    """If the DB half skipped, FAIL — a green run that proved nothing is the worst outcome for
    an entitlement suite. Threshold is the ACTUAL count of DB tests, not a guess."""
    if not _DSN:
        pytest.skip("POSTGRES_DSN not set — DB half intentionally not run")
    assert _DB_RAN["n"] >= 20, (
        f"only {_DB_RAN['n']} DB-backed tests ran; the isolation guarantee is UNPROVEN")
