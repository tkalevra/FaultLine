"""ZEROEDGE — a DEGRADED harvest must not be reported as a clean one.

Every LLM helper in src/api/llm_calls.py answers a NON-ANSWER by RETURNING (a bare {}, or
the circuit breaker's {"error": "circuit_breaker_open", ...}). That is the right runtime
behaviour — an ingest lane must degrade, not crash — but it means /harvest-spans answered
a flat 200 {"edges": [...], "spans": N} whether the brain answered every call or none of
them, so the MCP / document worker / bench harness could not tell the two apart.

DOCLOSS-B closed exactly this hole for the OTHER statement door (/extract/rewrite's chunk
loop). These tests pin the same guarantee for the spine door.

MEASURED, on 37 LongMemEval gold turns through the identical seam and the bench's own
deterministic scorer, the ONLY variable being whether the brain was reachable:
    brain reachable    M1 gold-answer capture 9/14 = 64.3%,  content coverage 42.3%
    brain unreachable  M1 gold-answer capture 5/14 = 35.7%,  content coverage 32.0%
Neither arm carried any signal that anything was wrong. That is what this test guards.

No DB, no network, no model load.
"""
import asyncio

import pytest

from src.api import llm_calls as LC


# ── the ledger itself ────────────────────────────────────────────────────────────

def test_no_scope_open_is_a_noop():
    """With no scope open, recording is a no-op and the snapshot is None.

    This is what makes the observer safe to leave in the hot path: every existing caller
    of call_llm_with_retry_* runs with no scope and is therefore untouched.
    """
    LC._record_llm_nonanswer("EXTRACT", "circuit_breaker_open")
    assert LC.llm_degradation_snapshot() is None


def test_scope_records_per_operation_counts():
    tok = LC.begin_llm_degradation_scope()
    try:
        # A clean scope with no failures reports NOTHING (not an empty dict) — callers
        # must be able to test truthiness.
        assert LC.llm_degradation_snapshot() is None
        LC._record_llm_nonanswer("REFRAME", "circuit_breaker_open")
        LC._record_llm_nonanswer("EXTRACT", "circuit_breaker_open")
        LC._record_llm_nonanswer("EXTRACT", "unparseable_llm_response")
        snap = LC.llm_degradation_snapshot()
    finally:
        LC.reset_llm_degradation_scope(tok)
    assert snap["llm_calls_unanswered"] == 3
    assert snap["llm_unanswered_operations"] == {"REFRAME": 1, "EXTRACT": 2}
    assert snap["llm_first_unanswered_reason"] == "REFRAME:circuit_breaker_open"
    # Scope closed → back to no-op.
    assert LC.llm_degradation_snapshot() is None


def test_scope_does_not_leak_across_resets():
    tok = LC.begin_llm_degradation_scope()
    LC._record_llm_nonanswer("EXTRACT", "circuit_breaker_open")
    LC.reset_llm_degradation_scope(tok)
    tok2 = LC.begin_llm_degradation_scope()
    try:
        assert LC.llm_degradation_snapshot() is None
    finally:
        LC.reset_llm_degradation_scope(tok2)


def test_open_breaker_is_recorded_by_the_real_helper(monkeypatch):
    """The recording is wired into the REAL non-answer return site, not just the helper.

    Drives call_llm_with_retry_sync with the breaker forced OPEN — the exact condition
    that produced the measured 64.3% -> 35.7% collapse — and asserts the ledger saw it.
    """
    class _OpenBreaker:
        timeout_seconds = 30
        last_failure_time = LC.datetime.utcnow()

        def is_open(self):
            return True

    monkeypatch.setattr(LC, "_saas_brain_unavailable", lambda: False)
    monkeypatch.setattr(LC, "_apply_tenant_brain",
                        lambda model, max_tokens: (_OpenBreaker(), model, max_tokens, {}))
    tok = LC.begin_llm_degradation_scope()
    try:
        out = LC.call_llm_with_retry_sync(
            messages=[{"role": "user", "content": "x"}],
            model="test-model", operation="EXTRACT",
        )
        snap = LC.llm_degradation_snapshot()
    finally:
        LC.reset_llm_degradation_scope(tok)
    # Behaviour is UNCHANGED — the caller still gets the breaker dict, nothing raises.
    assert out.get("error") == "circuit_breaker_open"
    # ...but the run is now attributable.
    assert snap["llm_calls_unanswered"] == 1
    assert snap["llm_unanswered_operations"] == {"EXTRACT": 1}


# ── the /harvest-spans envelope ──────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Req:
    def __init__(self, user_id="04658f0e-c7b5-4725-9ccf-f426ff13c3e5"):
        self.user_id = user_id
        self.text = "irrelevant — the impl is stubbed"


def _harvest_module():
    from src.api import main as M
    return M


def test_clean_harvest_envelope_is_byte_identical():
    M = _harvest_module()
    baseline = {"edges": [{"subject": "user", "rel_type": "owns", "object": "car"}], "spans": 1}

    async def _impl(_req):
        return dict(baseline)

    orig = M._harvest_spans_impl
    M._harvest_spans_impl = _impl
    try:
        out = _run(M.harvest_spans(_Req()))
    finally:
        M._harvest_spans_impl = orig
    assert out == baseline, "a harvest with no unanswered LLM call must be unchanged"


def test_degraded_harvest_withholds_its_edges():
    """RULING CHANGED — this test previously asserted the OPPOSITE, and said so:
    "edges survive AND the degradation is reported … discarding the deterministic edges in
    order to report the failure would just be a smaller silent loss."

    That was wrong, and the shipped log line argued against itself: it reported
    `edges_returned_are_a_DEGRADED_harvest_not_a_clean_empty` and returned them anyway, to a
    caller whose job is to commit them. A later recall then served those edges back as user
    truth. The withheld-edge count is still reported, so nothing about the failure becomes
    quieter — only the writing stops.

    Note what is NOT claimed: that the withheld edges were bad. The ledger is per-TURN, so
    the endpoint cannot tell which edges needed the brain. Committing the ones it cannot
    vouch for is the guess this refuses to make. The turn itself survives in `episodic_log`
    for re-extraction, which is why withholding is not a drop.
    """
    M = _harvest_module()
    edges = [{"subject": "user", "rel_type": "owns", "object": "car"}]

    async def _impl(_req):
        LC._record_llm_nonanswer("REFRAME", "circuit_breaker_open")
        LC._record_llm_nonanswer("MERGE_RELATION", "circuit_breaker_open")
        return {"edges": list(edges), "spans": 1}

    orig = M._harvest_spans_impl
    M._harvest_spans_impl = _impl
    try:
        out = _run(M.harvest_spans(_Req()))
    finally:
        M._harvest_spans_impl = orig
    assert out["edges"] == []             # nothing enters memory from a degraded extraction
    assert out["edges_withheld"] == 1     # and the loss is COUNTED, not hidden
    assert out["status"] == "degraded"
    assert out["spans"] == 1
    assert out["extraction_degraded"] is True
    assert out["llm_calls_unanswered"] == 2
    assert out["llm_unanswered_operations"] == {"REFRAME": 1, "MERGE_RELATION": 1}


def test_no_brain_is_refused_at_the_door_and_stays_loud(monkeypatch):
    """An unavailable brain produces an honest nothing BEFORE the harvest runs.

    The harvest is not read-only — it registers entities and files Class-C residue itself —
    so a post-hoc withhold is too late to make "an unavailable brain writes nothing" true.

    The second half of the assertion is the one that matters: refusing early must not make a
    customer-facing condition QUIET. A self-serve seat that has not connected an endpoint is
    in a normal state, and the signals that tell its owner so are emitted by the very call
    being declined — so the refusal has to emit them itself.
    """
    M = _harvest_module()
    seen: dict = {}

    monkeypatch.setattr(LC, "_saas_brain_unavailable", lambda: True)
    monkeypatch.setattr(LC, "_emit_brain_nonanswer",
                        lambda uid, reason, exc=None: seen.setdefault("event", reason))
    monkeypatch.setattr(LC, "_health_note",
                        lambda uid, metric, operation="*", **kw: seen.setdefault("health", metric))

    out = M._harvest_no_brain_refusal("11111111-aaaa-4bbb-8ccc-111111111111")

    assert out is not None, "no brain must refuse, not proceed"
    assert out["edges"] == []
    assert out["status"] == "degraded"
    assert out["extraction_degraded"] is True
    # the customer-visible lane and the console-banner lane both still fire
    assert seen.get("event") == "saas_no_brain_failclosed"
    assert seen.get("health") == "no_brain"
    # and the operation reported is the one that actually happened — no REFRAME was attempted
    assert out["llm_first_unanswered_reason"].startswith("HARVEST:")


def test_brain_available_does_not_refuse(monkeypatch):
    """Fail direction: a healthy seat is never refused by the door check."""
    M = _harvest_module()
    monkeypatch.setattr(LC, "_saas_brain_unavailable", lambda: False)
    assert M._harvest_no_brain_refusal("11111111-aaaa-4bbb-8ccc-111111111111") is None


def test_flag_off_is_byte_identical_even_when_degraded(monkeypatch):
    M = _harvest_module()

    async def _impl(_req):
        LC._record_llm_nonanswer("REFRAME", "circuit_breaker_open")
        return {"edges": [], "spans": 0}

    monkeypatch.setattr(M, "HARVEST_FAILURE_SURFACED", False)
    orig = M._harvest_spans_impl
    M._harvest_spans_impl = _impl
    try:
        out = _run(M.harvest_spans(_Req()))
    finally:
        M._harvest_spans_impl = orig
    assert out == {"edges": [], "spans": 0}
    assert "extraction_degraded" not in out


def test_zero_edge_degraded_harvest_is_not_a_clean_empty():
    """The failure mode this whole change exists for.

    A brain outage produced `{"edges": [], "spans": 0}` — indistinguishable from a turn
    that genuinely held no facts. It must now be distinguishable.
    """
    M = _harvest_module()

    async def _impl(_req):
        LC._record_llm_nonanswer("REFRAME", "circuit_breaker_open")
        LC._record_llm_nonanswer("EXTRACT", "circuit_breaker_open")
        return {"edges": [], "spans": 0}

    orig = M._harvest_spans_impl
    M._harvest_spans_impl = _impl
    try:
        out = _run(M.harvest_spans(_Req()))
    finally:
        M._harvest_spans_impl = orig
    assert out["edges"] == []
    assert out["extraction_degraded"] is True
    assert out["llm_calls_unanswered"] == 2


def test_scope_is_reset_even_when_the_harvest_raises():
    M = _harvest_module()

    async def _impl(_req):
        LC._record_llm_nonanswer("REFRAME", "circuit_breaker_open")
        raise RuntimeError("boom")

    orig = M._harvest_spans_impl
    M._harvest_spans_impl = _impl
    try:
        with pytest.raises(RuntimeError):
            _run(M.harvest_spans(_Req()))
    finally:
        M._harvest_spans_impl = orig
    assert LC.llm_degradation_snapshot() is None, "the ledger scope leaked past a raise"
