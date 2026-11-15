"""The reasoning-overhead counter must be READ BACK non-zero out of a POPULATED window.

The structural pins (metric in METRICS, not in DEGRADATION_METRICS; keys present in
collect_seat_metrics output) cannot tell a live counter from a dead one — a counter
that silently never increments passes all of them. These tests drive the REAL
recording path (the same _audit_reasoning_leak → _health_note → record() seam the
live call path uses — verified live on pre-prod 2026-08-18: the z.ai glm-5.2 seat
holds 246 reasoning_overhead samples against 306 llm_calls in one 24h window), flush
through the REAL flush SQL, and read back through the REAL window aggregation. Only
the psycopg2 boundary is faked.

Each test notes the mutation that turns it red.
"""
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import src.api.llm_calls as C
import src.api.llm_client as CL
from saas import brain_health as bh

SEAT = "a9e77b5c-1111-4a11-8b11-a9e77b5c1111"   # the live pre-prod seat measured 246/306
SCHEMA = "faultline_a9e77b5c_1111_4a11_8b11_a9e77b5c1111"


class _HealthCursor:
    """Serves the three SQL shapes brain_health issues: provisioning lookup, sample
    upsert, window aggregation. Everything above psycopg2 is the REAL code."""

    def __init__(self, state):
        self._state = state
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self._state["log"].append((s, params))
        if "FROM public.user_provisioning" in s:
            self._rows = [(SCHEMA,)]
        elif "INSERT INTO brain_health_samples" in s:
            # params: (bucket_epoch, operation, metric, latency_le_ms, n)
            bucket, op, metric, lat, n = params
            key = (bucket, op, metric, lat)
            self._state["samples"][key] = self._state["samples"].get(key, 0) + int(n)
            self._rows = []
        elif "FROM brain_health_samples" in s and "GROUP BY" in s:
            agg = {}
            newest = {}
            for (bucket, op, metric, lat), n in self._state["samples"].items():
                k = (op, metric, lat)
                agg[k] = agg.get(k, 0) + n
                ts = datetime.fromtimestamp(bucket, tz=timezone.utc)
                if str(metric) not in newest or ts > newest[str(metric)]:
                    newest[str(metric)] = ts
            self._rows = [(op, metric, lat, agg[(op, metric, lat)], newest.get(str(metric)))
                          for (op, metric, lat) in agg]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _HealthConn:
    def __init__(self, state):
        self._state = state

    def cursor(self):
        return _HealthCursor(self._state)

    def commit(self):
        return None

    def rollback(self):
        return None


@pytest.fixture
def health_db(monkeypatch):
    """Fake ONLY the psycopg2 boundary; clear the schema cache so lookups hit it."""
    state = {"samples": {}, "log": []}

    @contextmanager
    def _conn():
        yield _HealthConn(state)

    import saas.db as saas_db
    monkeypatch.setattr(saas_db, "connection", _conn)
    bh._schema_cache.clear()
    monkeypatch.setattr(bh, "_buf", {})
    return state


def _audit_expected_call(user_id, model="glm-5.2"):
    """The REAL increment seam exactly as the three live call sites invoke it."""
    C._audit_reasoning_leak(
        {"choices": [{"message": {"content": "[]"}}],
         "usage": {"completion_tokens": 488,
                   "completion_tokens_details": {"reasoning_tokens": 440}}},
        "EXTRACT", user_id,
        expected=CL.resolve_reasoning_controls(
            "https://api.z.ai/api/coding/paas/v4", model)["can_disable"] is False,
        budget_padded=1024,
        model=model)


def _bind_zai(monkeypatch):
    monkeypatch.setattr(CL, "_tenant_brain",
                        lambda: {"base_url": "https://api.z.ai/api/coding/paas/v4",
                                 "backend_type": "openai"})


def test_counter_reads_back_nonzero_from_a_populated_window(monkeypatch, health_db):
    """THE BAR: record → flush → read-back through the real seam, and the number
    that comes out is the number that went in. RED if the increment line in
    _audit_reasoning_leak is removed, or if the read mapping breaks — the structural
    pins stay green through both, which is exactly the hole this closes."""
    _bind_zai(monkeypatch)
    for _ in range(3):
        _audit_expected_call(SEAT)
    for _ in range(10):
        bh.record(SEAT, bh.M_LLM_CALL, "EXTRACT")
    written = bh.flush_now()
    assert written >= 2, (  # cells, not calls: same-minute records aggregate into one upsert cell
        "the flush must have written the samples (one cell per metric, n aggregated)")
    out = bh.collect_seat_metrics(SEAT)
    assert out["available"] is True
    assert out["reasoning_overhead_calls"] == 3, (
        "a counter read back as zero from a window the seam just populated is the "
        "silent-dead-counter failure mode — every structural pin passes it")
    assert out["llm_calls"] == 10
    assert out["reasoning_overhead_share"] == pytest.approx(0.3, abs=1e-6)


def test_the_increment_seam_is_the_live_audit_path(monkeypatch, health_db):
    """END TO END above the DB: the same _audit_reasoning_leak the live call path
    runs (expected=True, model=tenant model, measured reasoning tokens in usage)
    leaves a reasoning_overhead cell in the buffer WITHOUT any mocking of
    _health_note or record. RED if the audit stops teeing the note."""
    _bind_zai(monkeypatch)
    _audit_expected_call(SEAT)
    assert any(metric == bh.M_REASONING_OVERHEAD
               for (_uid, _b, _op, metric, _l) in bh._buf), (
        "the audit tee did not reach the recording buffer")
    bh.flush_now()
    assert bh.collect_seat_metrics(SEAT)["reasoning_overhead_calls"] == 1


def test_overhead_is_never_an_active_signal_even_when_fresh(monkeypatch, health_db):
    """POPULATED-window proof of the not-an-alarm stance: with reasoning_overhead
    recorded RIGHT NOW it stays out of active_signals — while a degradation metric
    recorded in the same minute goes active. RED if someone adds M_REASONING_OVERHEAD
    to DEGRADATION_METRICS."""
    _bind_zai(monkeypatch)
    _audit_expected_call(SEAT)
    bh.record(SEAT, bh.M_LLM_FAILED, "EXTRACT")
    bh.flush_now()
    out = bh.collect_seat_metrics(SEAT)
    assert out["reasoning_overhead_calls"] == 1
    assert "reasoning_overhead" not in out["active_signals"], (
        "expected provider-class spend must never read as a fault")
    assert "llm_call_failed" in out["active_signals"], (
        "the contrast case: a real degradation metric in the same window DOES alarm")


def test_non_seat_uids_do_not_reach_the_window(monkeypatch, health_db):
    """Reachable-state honesty: background lanes call with user_id=re_embedder —
    those must NOT create samples (they never could resolve a schema). RED if the
    UUID door is removed (junk ids would burn flush cycles)."""
    _bind_zai(monkeypatch)
    _audit_expected_call("re_embedder")
    assert not bh._buf, "a non-UUID caller must not leave cells in the buffer"
    _ = uuid.uuid4()  # import exercised; keep the symbol honest