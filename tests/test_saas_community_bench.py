"""Community self-serve benchmark — anonymization boundary + rate-limit + report cadence.

Covers (SAAS_MODE):
  * ANONYMIZATION BOUNDARY (the load-bearing security property): a published result stores the
    owning account_id/user_id INTERNALLY, but the PUBLIC read path (list_public_results) and the
    PUBLIC HTTP endpoint (/leaderboard/data) return NEITHER — no identity column, and the raw
    identity values never appear anywhere in the public payload.
  * bench_selftest rate-limit / cost-safety: refuses with NO brain configured (cost-safe); refuses
    a concurrent run (409); refuses inside the cooldown (429).
  * bench_report cadence: an auto-report fires exactly once per cadence crossing (double-send
    guarded), and never sends without recipients.

DB-backed tests use POSTGRES_DSN (a scratch DB with migration 176 applied) — SKIPPED when there
is no REACHABLE control-plane DB, so the suite still runs where none is available. The guard is
the ``requires_postgres`` fixture (tests/conftest.py), which connects rather than trusting the
string: a sibling module used to plant ``postgresql://unused/...`` into os.environ at import,
which is process-wide, so a truthiness check saw a "DSN" and these five tests ERRORED with
``could not translate host name "unused"`` instead of skipping.

SAAS_MODE + the test secrets are the suite-wide baseline set once in tests/conftest.py.

tests/ is gitignored → add with ``git add -f``.
Run: python3 tools/fltest.py --bug BENCH-COMMUNITY --test tests/test_saas_community_bench.py
"""

import uuid

import pytest

_needs_db = pytest.mark.usefixtures("requires_postgres")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _wipe_results(dsn):
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE public.community_bench_results")
            cur.execute("UPDATE public.bench_report_config "
                        "SET recipients = ARRAY[]::text[], cadence_entries = 25, "
                        "last_reported_count = 0, last_report_at = NULL WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def clean_db(requires_postgres):
    _wipe_results(requires_postgres)
    yield requires_postgres
    _wipe_results(requires_postgres)


# ─────────────────────────────────────────────────────────────────────────────
# ANONYMIZATION BOUNDARY
# ─────────────────────────────────────────────────────────────────────────────

@_needs_db
def test_public_read_never_exposes_identity(clean_db):
    """publish_result stores account/user INTERNALLY; list_public_results exposes NEITHER."""
    from saas import community_bench

    acct = str(uuid.uuid4())
    user = str(uuid.uuid4())
    out = community_bench.publish_result(
        acct, user, model_label="qwen3.5-9b",
        scores={"claims_passed": 5, "claims_total": 7, "by_category": {"corrections": {"passed": 1, "total": 1}}},
        wall_time_ms=42000)
    assert out["ok"] and out["total_published"] == 1

    rows = community_bench.list_public_results()
    assert len(rows) == 1
    row = rows[0]
    # PUBLIC surface: only anonymized keys.
    assert set(row.keys()) == {
        "model_label", "claims_passed", "claims_total", "score_pct",
        "wall_time_ms", "by_category", "created_at"}
    assert "account_id" not in row and "user_id" not in row
    # The raw identity values must NOT appear ANYWHERE in the public payload.
    blob = repr(rows).lower()
    assert acct.lower() not in blob, "account_id leaked into public leaderboard payload"
    assert user.lower() not in blob, "user_id leaked into public leaderboard payload"
    # The anonymized content survives correctly.
    assert row["model_label"] == "qwen3.5-9b"
    assert row["claims_passed"] == 5 and row["claims_total"] == 7
    assert row["score_pct"] == pytest.approx(71.4, abs=0.1)


@_needs_db
def test_internal_columns_are_actually_stored(clean_db):
    """The identity columns ARE persisted (for rate-limit/dedupe) — the boundary is the READ."""
    import psycopg2
    from saas import community_bench
    acct, user = str(uuid.uuid4()), str(uuid.uuid4())
    community_bench.publish_result(acct, user, model_label="m", scores={"claims_passed": 1, "claims_total": 1}, wall_time_ms=1)
    conn = psycopg2.connect(clean_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT account_id::text, user_id::text FROM public.community_bench_results")
            got = cur.fetchone()
    finally:
        conn.close()
    assert got == (acct, user)


@_needs_db
def test_public_http_endpoint_is_anonymized(clean_db, monkeypatch):
    """The gate-free /leaderboard/data endpoint returns only anon columns (end-to-end)."""
    from fastapi.testclient import TestClient
    from saas import community_bench
    import saas.platform_settings as _ps
    import src.mcp.http_server as h

    # Neutralize the offline maintenance gate (the scratch DB lacks platform_settings; in prod
    # the gate reads a real not-offline state). This test targets the anonymization boundary.
    monkeypatch.setattr(_ps, "offline_state", lambda: {"offline": False, "message": ""})

    acct, user = str(uuid.uuid4()), str(uuid.uuid4())
    community_bench.publish_result(acct, user, model_label="secret-model",
                                   scores={"claims_passed": 3, "claims_total": 7}, wall_time_ms=10)
    client = TestClient(h.app, follow_redirects=False)
    r = client.get("/leaderboard/data")  # NO auth — it is public
    assert r.status_code == 200, (r.status_code, r.text[:200])
    body = r.json()
    assert "results" in body and len(body["results"]) == 1
    blob = r.text.lower()
    assert acct.lower() not in blob and user.lower() not in blob
    assert "account_id" not in blob and "user_id" not in blob
    assert body["results"][0]["model_label"] == "secret-model"


# ─────────────────────────────────────────────────────────────────────────────
# RATE-LIMIT / COST-SAFETY (bench_selftest)
# ─────────────────────────────────────────────────────────────────────────────

def test_start_run_refuses_without_brain(monkeypatch):
    """COST-SAFE: no configured brain → 400 refusal, never spends a global default."""
    import asyncio
    from saas import bench_selftest
    monkeypatch.setattr(bench_selftest._llm_config, "resolve_account_llm_config", lambda a: None)
    res = asyncio.run(bench_selftest.start_run(str(uuid.uuid4()), str(uuid.uuid4())))
    assert res["ok"] is False and res["status_code"] == 400
    assert "model" in res["reason"].lower()


def test_start_run_concurrency_and_cooldown(monkeypatch):
    """One run per user (409) + cooldown after a finished run (429). No live run is executed."""
    import asyncio
    import time
    from saas import bench_selftest

    monkeypatch.setattr(bench_selftest._llm_config, "resolve_account_llm_config",
                        lambda a: {"model": "m", "base_url": "http://x", "backend_type": "openai"})
    # Neutralize the actual background run so start_run only exercises the guard/registry.
    monkeypatch.setattr(bench_selftest, "run_selftest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))

    user = str(uuid.uuid4())
    # Simulate an in-flight run → concurrent start refused with 409.
    bench_selftest._RUNS[user] = {"status": "running", "started_at": time.time(),
                                  "finished_at": None, "progress": {}, "result": None,
                                  "model_label": "m", "published": False}
    res = asyncio.run(bench_selftest.start_run(user, str(uuid.uuid4())))
    assert res["status_code"] == 409

    # Simulate a just-FINISHED run → cooldown refuses with 429 + retry_after.
    bench_selftest._RUNS[user] = {"status": "done", "started_at": time.time() - 1,
                                  "finished_at": time.time(), "progress": {}, "result": {},
                                  "model_label": "m", "published": False}
    res = asyncio.run(bench_selftest.start_run(user, str(uuid.uuid4())))
    assert res["status_code"] == 429 and res.get("retry_after", 0) > 0
    bench_selftest._RUNS.pop(user, None)


def test_model_label_strips_path_and_extension():
    from saas import bench_selftest
    assert bench_selftest._model_label({"model": r"C:\Models\Qwen3.6-35B-A3B.gguf"}) == "Qwen3.6-35B-A3B"
    assert bench_selftest._model_label({"model": "qwen/qwen3.5-9b"}) == "qwen3.5-9b"
    assert bench_selftest._model_label({"model": ""}) == "unknown-model"


def test_claim_library_loads_from_harness():
    """The sandbox reuses the harness claim set verbatim (no drift, single source)."""
    from saas import bench_selftest
    lib = bench_selftest._load_claim_lib()
    assert isinstance(lib.CLAIMS, list) and len(lib.CLAIMS) >= 5
    # assess is a pure function present on the loaded module.
    v = lib.assess(lib.CLAIMS[0], "my favorite color is red")
    assert set(("passed", "checks", "expected")).issubset(v.keys())


# ─────────────────────────────────────────────────────────────────────────────
# REPORT CADENCE (bench_report)
# ─────────────────────────────────────────────────────────────────────────────

@_needs_db
def test_report_cadence_fires_once_per_crossing(clean_db, monkeypatch):
    """Auto-report sends exactly once per cadence multiple; guarded against double-send."""
    from saas import bench_report

    sent = []
    monkeypatch.setattr(bench_report._smtp, "send_email",
                        lambda to, *a, **k: (sent.append(to) or {"ok": True}))
    assert bench_report.set_cadence(3)["ok"]
    assert bench_report.add_recipient("admin@example.com")["ok"]

    # Below the first multiple → no send.
    assert bench_report.maybe_send_auto_report(2)["ok"] is False
    assert sent == []
    # Cross 3 → one send.
    r = bench_report.maybe_send_auto_report(3)
    assert r["ok"] and r["milestone"] == 3 and sent == ["admin@example.com"]
    # Same count again → NO double-send (guard).
    assert bench_report.maybe_send_auto_report(3)["ok"] is False
    assert sent == ["admin@example.com"]
    # Cross 6 → a second send.
    r2 = bench_report.maybe_send_auto_report(7)
    assert r2["ok"] and r2["milestone"] == 6
    assert sent == ["admin@example.com", "admin@example.com"]


@_needs_db
def test_report_never_sends_without_recipients(clean_db, monkeypatch):
    from saas import bench_report
    sent = []
    monkeypatch.setattr(bench_report._smtp, "send_email",
                        lambda to, *a, **k: (sent.append(to) or {"ok": True}))
    bench_report.set_cadence(1)
    # No recipients configured → advances the marker but sends nothing.
    r = bench_report.maybe_send_auto_report(1)
    assert r["ok"] is False and sent == []
