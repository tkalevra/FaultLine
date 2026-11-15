"""The re-embedder must skip a REVOKED SEAT, not just a non-active ACCOUNT.

WHY THIS EXISTS. `_saas_suspended_user_ids` filtered on ``a.status <> 'active'`` — the ACCOUNT's
status — and never looked at ``au.status``, the SEAT's. So a revoked seat on an active account was
swept on every cycle forever. Measured on production the day it was found: **13 revoked seats vs 8
active**, i.e. 62% of the swept population was dead weight, and the largest graph on the box
(3 836 entities / 2 857 facts) belonged to a REVOKED seat. Sweeping it means re-extraction,
embedding and reconciliation over the whole graph, repeatedly, for a seat nobody can use.

The predicate is verified two ways: structurally (the SQL names both statuses) and behaviourally
(a real Postgres with a revoked seat on an ACTIVE account — the exact shape that used to slip
through). The DB arm is the one that would have caught the original bug; the structural arm keeps a
future refactor from silently dropping half the predicate while the DB arm is skipped for want of a
DSN.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest

from src.re_embedder import embedder as E

DSN = os.environ.get("WELD_GUARD_TEST_DSN") or os.environ.get("REEMBED_TEST_DSN")


# ── structural: the SQL must consider BOTH statuses ──────────────────────────────────────────────

def test_the_skip_predicate_names_both_the_account_and_the_seat_status():
    """The original defect was a predicate that named only the account. Pin both."""
    src = inspect.getsource(E._saas_suspended_user_ids)
    assert "a.status <> 'active'" in src, src
    assert "au.status <> 'active'" in src, (
        "the SEAT status is missing from the skip predicate — a revoked seat on an active "
        "account will be swept forever (this is the exact bug this test was written for)"
    )


def test_the_two_status_checks_are_ORed_not_ANDed():
    """OR = skip if EITHER is non-active. AND would skip almost nothing — it would require the
    account to be dead *and* the seat to be dead, which is strictly narrower than the old
    account-only behaviour and would be a silent regression."""
    src = inspect.getsource(E._saas_suspended_user_ids)
    i = src.find("a.status <> 'active'")
    j = src.find("au.status <> 'active'")
    assert i != -1 and j != -1
    between = src[min(i, j):max(i, j)]
    assert " OR " in between.upper(), f"expected the two status checks to be OR-ed, got: {between!r}"


# ── behavioural: a real DB, the exact shape that slipped through ─────────────────────────────────

@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
def test_db_a_revoked_seat_on_an_ACTIVE_account_is_skipped(monkeypatch):
    import psycopg2

    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True, raising=False)

    acct = str(uuid.uuid4())
    live, dead = str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS public.accounts "
                    "(account_id uuid PRIMARY KEY, status text)")
        cur.execute("CREATE TABLE IF NOT EXISTS public.account_users "
                    "(user_id uuid PRIMARY KEY, account_id uuid, status text)")
        cur.execute("INSERT INTO public.accounts (account_id, status) VALUES (%s,'active') "
                    "ON CONFLICT (account_id) DO UPDATE SET status='active'", (acct,))
        cur.execute("INSERT INTO public.account_users VALUES (%s,%s,'active') "
                    "ON CONFLICT (user_id) DO UPDATE SET status='active'", (live, acct))
        cur.execute("INSERT INTO public.account_users VALUES (%s,%s,'revoked') "
                    "ON CONFLICT (user_id) DO UPDATE SET status='revoked'", (dead, acct))
        c.commit()

    skipped = E._saas_suspended_user_ids(DSN, [live, dead])

    assert dead in skipped, (
        "a REVOKED seat on an ACTIVE account was not skipped — this is the production bug: "
        "13 such seats were being swept every cycle"
    )
    assert live not in skipped, "an ACTIVE seat on an ACTIVE account must still be processed"


@pytest.mark.skipif(not DSN, reason="set WELD_GUARD_TEST_DSN to a THROWAWAY postgres")
def test_db_fail_open_on_error_never_mass_skips(monkeypatch):
    """The fail direction is load-bearing: an error must process EVERYTHING, never skip everything.
    A mass-skip would silently stop all memory maintenance across the fleet."""
    monkeypatch.setattr("saas.config.saas_mode_enabled", lambda: True, raising=False)
    assert E._saas_suspended_user_ids("postgresql://nope:nope@127.0.0.1:1/nope",
                                      [str(uuid.uuid4())]) == set()
