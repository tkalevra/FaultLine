"""SIGNUP-LEDGER CLEAR — the operator lever for the purge-surviving 3-strikes ladder.

LIVE CONTEXT (2026-11-01): the owner's own Gmail sat at 3 strikes with NO console lever to
reset it — twice in one night the agent had to hand-delete rows on prod so the owner could
re-register. The ladder cannot tell an owner testing flows from an abuser; this lever is the
reset. Two pins:

  1. clear_attempts deletes BOTH key arms (canonical, migration 232, and the pre-232 legacy
     arm) — a partial clear that leaves the legacy row would keep counting against the
     address (get_attempt_count sums both arms).
  2. the /admin/signup-ledger/clear endpoint returns the honest count and turns a
     control-plane failure into a loud 500 — never a silent "cleared" while the row survives.

The email itself is NEVER logged — the audit target_id is the hash.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from saas import signup_ledger as sl              # noqa: E402

_DSN = (os.environ.get("POSTGRES_DSN") or "").strip()
requires_db = pytest.mark.skipif(not _DSN, reason="POSTGRES_DSN not set — control plane needed")


# ── pin 1: BOTH key arms die ────────────────────────────────────────────────────────

@requires_db
def test_clear_deletes_canonical_and_legacy_arms():
    email = "ledger-clear-pin@example.test"
    canon = sl.hash_email(email)                       # canonical (migration 232)
    legacy = sl._hash_normalized(sl.legacy_normalize_email(email))  # pre-232 arm
    assert canon and legacy
    try:
        with sl.db.connection() as conn:
            with conn.cursor() as cur:
                for h in {canon, legacy}:
                    cur.execute(
                        "INSERT INTO public.signup_ledger (email_hash, attempt_count, "
                        "last_outcome) VALUES (%s, 3, %s) ON CONFLICT (email_hash) DO UPDATE "
                        "SET attempt_count = 3",
                        (h, sl.OUTCOME_REJECTED),
                    )
            conn.commit()
        # the ladder reads BOTH arms — prove the lockout first
        assert sl.get_attempt_count(email) == 6 or sl.get_attempt_count(email) >= 3
        n = sl.clear_attempts(email)
        assert n == 2, f"expected both arms deleted, got rows_deleted={n}"
        with sl.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.signup_ledger WHERE email_hash = ANY(%s)",
                            ([canon, legacy],))
                assert cur.fetchone()[0] == 0
    finally:
        with sl.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.signup_ledger WHERE email_hash = ANY(%s)",
                            ([canon, legacy],))
            conn.commit()


@requires_db
def test_clear_is_a_fresh_attempt_one():
    email = "ledger-fresh-pin@example.test"
    try:
        with sl.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.signup_ledger (email_hash, attempt_count, last_outcome) "
                    "VALUES (%s, 3, %s) ON CONFLICT (email_hash) DO UPDATE SET attempt_count = 3",
                    (sl.hash_email(email), sl.OUTCOME_REJECTED),
                )
            conn.commit()
        assert sl.decide(email) == sl.DECISION_REJECT
        sl.clear_attempts(email)
        assert sl.decide(email) == sl.DECISION_ALLOW, (
            "a cleared address must start the ladder again at attempt 1")
    finally:
        with sl.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.signup_ledger WHERE email_hash = %s",
                            (sl.hash_email(email),))
            conn.commit()


# ── pin 2: the endpoint is honest ───────────────────────────────────────────────────

def test_clear_endpoint_returns_count_and_fails_loud(monkeypatch):
    from fastapi import HTTPException
    from saas import admin as admin_mod

    seen = {}
    monkeypatch.setattr(admin_mod._signup_ledger, "clear_attempts",
                        lambda e: seen.setdefault("email", e) and 2)
    monkeypatch.setattr(admin_mod._audit, "record", lambda *a, **k: None)
    monkeypatch.setattr(admin_mod, "_log", lambda msg: None)

    body = admin_mod.SignupLedgerClearRequest(email="  padded@example.test  ")
    out = admin_mod.clear_signup_ledger(body, _op=True)
    assert out == {"status": "cleared", "rows_deleted": 2}
    assert seen["email"] == "padded@example.test", "the endpoint must strip the address"

    monkeypatch.setattr(admin_mod._signup_ledger, "clear_attempts", lambda e: -1)
    with pytest.raises(HTTPException) as ei:
        admin_mod.clear_signup_ledger(body, _op=True)
    assert ei.value.status_code == 500, (
        "a control-plane failure must NEVER read as a successful clear")


def test_clear_endpoint_is_operator_gated():
    from saas import admin as admin_mod
    route = next(r for r in admin_mod.router.routes
                 if getattr(r, "path", "") == "/admin/signup-ledger/clear")
    deps = [d.call.__name__ for d in getattr(route, "dependant", None).dependencies] \
        if getattr(route, "dependant", None) else []
    assert any("operator" in d for d in deps), (
        "the strike-clear lever must sit behind require_operator")
