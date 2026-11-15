"""SANDBOX ISOLATION / TEARDOWN — the self-test scope is ALWAYS destroyed (phase 2 boundary).

A self-serve benchmark mints an EPHEMERAL throwaway account/seat, runs, and MUST tear it down —
dropping BOTH per-seat schemas (the memory schema ``faultline_<uuid>`` AND the cortex schema
``flagent_<uuid>``) and BOTH per-seat Qdrant collections (``faultline-<uuid>`` + ``flagent-<uuid>``)
so nothing orphans. This test proves, WITHOUT live infra (psycopg2 / httpx / the account ops are
monkeypatched to RECORD, not perform):

  * _teardown_sandbox issues DROP SCHEMA for BOTH schema names and DELETE for BOTH collections,
    reports dropped_schemas=True, and NEVER raises;
  * the teardown runs even when the run body FAILS (run_selftest's finally), so a crashed run can
    never leak a schema/collection.

The placeholder POSTGRES_DSN below exists ONLY to satisfy a truthiness branch gate — psycopg2's
connect() is stubbed and nothing ever dials it. It is set per test (``_placeholder_dsn``) rather
than at module import: the import-time write was process-wide and permanent, so it replaced the
real DSN for every later module — turning the DB-backed suites' clean "no database → skip" into
``OperationalError: could not translate host name "unused"``.

tests/ is gitignored → add with ``git add -f``.

Run: python3 tools/fltest.py --bug BENCH-SANDBOX-TEARDOWN --test tests/test_saas_bench_selftest_teardown.py
"""

import asyncio

import pytest

from saas import bench_selftest


@pytest.fixture(autouse=True)
def _placeholder_dsn(monkeypatch):
    """Branch gate only — connect() is stubbed, so this DSN is never dialled."""
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://unused/teardown-test")


class _FakeCursor:
    def __init__(self, sink): self._sink = sink
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self._sink.append(sql)


class _FakeConn:
    def __init__(self, sink): self._sink = sink
    def cursor(self): return _FakeCursor(self._sink)
    def commit(self): pass
    def close(self): pass


def test_teardown_drops_both_schemas_and_both_collections(monkeypatch):
    seat = "11111111-2222-3333-4444-555555555555"
    fl_schema = "faultline_" + seat.replace("-", "_")
    ag_schema = "flagent_" + seat.replace("-", "_")

    sql_log: list[str] = []
    deleted_collections: list[str] = []

    # Account control-plane ops: pretend the throwaway account deleted cleanly.
    monkeypatch.setattr(bench_selftest._accounts, "set_account_status",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bench_selftest._accounts, "delete_account",
                        lambda *a, **k: {"ok": True})
    # DB: record every SQL statement instead of hitting Postgres.
    monkeypatch.setattr(bench_selftest.psycopg2, "connect", lambda dsn: _FakeConn(sql_log))

    class _FakeHttpx:
        @staticmethod
        def delete(url, timeout=None):
            deleted_collections.append(url)
        @staticmethod
        def post(url, json=None, timeout=None):
            pass
    monkeypatch.setattr(bench_selftest, "httpx", _FakeHttpx)

    out = bench_selftest._teardown_sandbox("acct-x", "slug-x", seat)

    assert out["dropped_schemas"] is True
    joined = " ".join(sql_log)
    assert f'DROP SCHEMA IF EXISTS "{fl_schema}" CASCADE' in joined, "memory schema not dropped"
    assert f'DROP SCHEMA IF EXISTS "{ag_schema}" CASCADE' in joined, "cortex schema not dropped"
    # Both per-seat collections deleted (memory + cortex, distinct prefixes).
    assert any(f"faultline-{seat}" in u for u in deleted_collections), "memory collection not deleted"
    assert any(f"flagent-{seat}" in u for u in deleted_collections), "cortex collection not deleted"


def test_teardown_never_raises_on_db_error(monkeypatch):
    monkeypatch.setattr(bench_selftest._accounts, "set_account_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bench_selftest._accounts, "delete_account",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bench_selftest.psycopg2, "connect",
                        lambda dsn: (_ for _ in ()).throw(RuntimeError("no db")))
    # Must NOT raise — a teardown that throws would leak the sandbox.
    out = bench_selftest._teardown_sandbox("acct-x", "slug-x",
                                           "99999999-8888-7777-6666-555555555555")
    assert isinstance(out, dict)


def test_run_selftest_tears_down_even_when_body_fails(monkeypatch):
    """If the run BODY raises (here: seat registration fails), the finally still tears the
    sandbox down — a crashed run cannot orphan a schema/collection."""
    monkeypatch.setattr(bench_selftest, "_load_claim_lib",
                        lambda: type("M", (), {"CLAIMS": [{"name": "c", "category": "x",
                                                           "setup_turns": [], "query": "q"}]})())
    monkeypatch.setattr(bench_selftest._accounts, "create_account",
                        lambda **k: {"account_id": "sandbox-acct", "slug": "sandbox-slug"})
    # register_seat FAILS → body raises inside the try.
    monkeypatch.setattr(bench_selftest._seats, "register_seat",
                        lambda *a, **k: {"ok": False, "reason": "boom"})

    torn: list = []
    monkeypatch.setattr(bench_selftest, "_teardown_sandbox",
                        lambda acct, slug, seat: torn.append((acct, slug)) or {"deleted": True})

    with pytest.raises(Exception):
        asyncio.run(bench_selftest.run_selftest("sandbox-acct",
                                                {"model": "m", "base_url": "http://x",
                                                 "backend_type": "openai"}))
    assert torn and torn[0][0] == "sandbox-acct", "teardown was not called on a failed run"


def test_accepted_run_records_consent_privacy_minimal(monkeypatch):
    """M1 (audit finding): an ACCEPTED run must persist a consent-ledger row (who/when/model),
    privacy-minimal — NO ip / user-agent. Proves record_consent is actually WIRED (was dead code)."""
    from saas import community_bench

    user = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    account = "99999999-8888-7777-6666-555555555555"

    # A configured brain → passes the cost-safe no-brain gate.
    # MOCK SEAM MOVED (migration 202): the gate now resolves through saas.seat_brain.resolve_brain
    # (seat row first for a house/individual account, the shared account row for a corporate one)
    # instead of resolve_account_llm_config directly — otherwise an individual's PERSONAL brain
    # would read as "no brain", and worse, the account lane could hand this run another seat's
    # credential to spend. The old mock fed a seam the code no longer reads (a DEAD MOCK), so it
    # is repointed rather than the assertion being weakened. What this test pins — an accepted run
    # records exactly one privacy-minimal consent row — is unchanged.
    from saas import seat_brain as _seat_brain
    monkeypatch.setattr(_seat_brain, "resolve_brain",
                        lambda uid, acct: {"model": "gemma-4-31b", "base_url": "http://x",
                                           "api_key": "k", "backend_type": "openai"})
    # Don't actually run the sandbox — the background runner is a no-op for this test.
    async def _noop_run(*a, **k):
        return {"claims_passed": 0, "claims_total": 0, "by_category": {}}
    monkeypatch.setattr(bench_selftest, "run_selftest", _noop_run)
    # Fresh run state so no cooldown / in-flight guard trips.
    monkeypatch.setattr(bench_selftest, "_RUNS", {})

    calls: list[tuple] = []
    def _spy(acct_id, uid, *, model_label, ip=None, user_agent=None):
        calls.append((acct_id, uid, model_label, ip, user_agent))
        return {"ok": True}
    monkeypatch.setattr(community_bench, "record_consent", _spy)

    res = asyncio.run(bench_selftest.start_run(user, account))
    assert res.get("ok") is True

    assert len(calls) == 1, "an accepted run must record exactly one consent event"
    acct_id, uid, model_label, ip, ua = calls[0]
    assert acct_id == account and uid == user
    assert model_label == "gemma-4-31b"          # server-derived model label
    assert ip is None and ua is None             # privacy-minimal: no ip / user-agent captured


def test_no_brain_run_records_no_consent(monkeypatch):
    """Cost-safe refusal (no brain) must NOT record consent — no run was accepted."""
    from saas import community_bench
    from saas import seat_brain as _seat_brain     # gate seam, see the test above
    monkeypatch.setattr(_seat_brain, "resolve_brain",
                        lambda uid, acct: None)     # no brain configured
    monkeypatch.setattr(bench_selftest, "_RUNS", {})
    calls: list = []
    monkeypatch.setattr(community_bench, "record_consent",
                        lambda *a, **k: calls.append(1) or {"ok": True})

    res = asyncio.run(bench_selftest.start_run("u-1", "a-1"))
    assert res.get("ok") is False and res.get("status_code") == 400
    assert calls == [], "a refused (no-brain) run must not record consent"
