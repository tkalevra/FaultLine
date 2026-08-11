"""A call that never happened must not be cached as an answer.

Every LLM helper in this codebase returns ``None`` on failure, which collapses two very
different facts into one value: "the model was asked and had no answer" (real evidence) and
"the model was never reached" (no evidence at all).

That distinction is load-bearing in the re-embedder's ontology-growth lanes. They ask the
model to classify a concept and, on ``None``, write a verdict into ``climb_state`` — which
``_climb_state_should_skip`` then HONOURS on an unchanged fingerprint, so the concept is
never re-attempted. A transient outage therefore writes PERMANENT conclusions the model
never supplied, and burns each concept's attempt budget doing it.

``LLMUnavailable`` (opt-in, ``raise_on_unavailable=True``) makes the didn't-happen case
impossible to mistake for an answer.

Run: python3 -m pytest tests/test_llm_unavailable_not_a_verdict.py -q
"""

import httpx
import pytest

import src.api.llm_calls as rl
from src.api import llm_lane as lane
from src.re_embedder import embedder as emb


def _seam(monkeypatch, responder):
    """Wire the real sync call path to an in-process responder (no network)."""
    monkeypatch.setattr(rl, "_get_endpoint_list", lambda: ["http://brain:8080/v1/chat/completions"])
    monkeypatch.setattr(rl.llm_source_ip, "post_sync", responder)


def _down(_client, url, **kw):
    raise httpx.ConnectError("brain unreachable")


# ── the call layer ───────────────────────────────────────────────────────────────────

def test_exhausted_retries_raise_unavailable_when_opted_in(monkeypatch):
    _seam(monkeypatch, _down)
    rl._llm_circuit_breaker.reset()
    try:
        with pytest.raises(rl.LLMUnavailable) as ei:
            rl.call_llm_with_retry_sync(
                messages=[{"role": "user", "content": "classify this"}],
                model="m", user_id="re_embedder", operation="CLASSIFY_CHAIN",
                max_retries=1, timeout=0.1, raise_on_unavailable=True,
            )
    finally:
        rl._llm_circuit_breaker.reset()
    assert ei.value.reason == "retries_exhausted"
    assert ei.value.operation == "CLASSIFY_CHAIN"


def test_existing_callers_are_unchanged(monkeypatch):
    """The flag defaults False, so every pre-existing caller keeps its exact contract."""
    _seam(monkeypatch, _down)
    rl._llm_circuit_breaker.reset()
    try:
        with pytest.raises(httpx.ConnectError):
            rl.call_llm_with_retry_sync(
                messages=[{"role": "user", "content": "hi"}],
                model="m", user_id="u", operation="EXTRACT",
                max_retries=1, timeout=0.1,
            )
    finally:
        rl._llm_circuit_breaker.reset()


# ── the consumer: verify the READ, not just the write ────────────────────────────────

def test_unavailable_is_not_none_at_the_sweep_boundary():
    """``_ask_brain`` must return a sentinel that is NOT None.

    If it collapsed to None the caller could not tell it apart from a genuine "no
    classification" answer, and would cache a verdict — the exact bug this closes.
    """
    stats = {}

    def _raises():
        raise lane.LLMUnavailable("retries_exhausted", "CLASSIFY_CHAIN")

    got = emb._ask_brain(_raises, stats=stats, what="unit")
    assert got is emb._BRAIN_UNAVAILABLE
    assert got is not None, "the sentinel must be distinguishable from a negative answer"
    assert stats["brain_unavailable"] == 1


def test_a_real_negative_answer_still_passes_through():
    """The guard must not swallow a genuine None — that IS evidence and is cached normally."""
    stats = {}
    assert emb._ask_brain(lambda: None, stats=stats, what="unit") is None
    assert "brain_unavailable" not in stats


def test_an_unrelated_error_is_not_treated_as_unavailable():
    """Only LLMUnavailable means "never asked"; anything else keeps its own handling."""
    with pytest.raises(ValueError):
        emb._ask_brain(lambda: (_ for _ in ()).throw(ValueError("bad parse")), what="unit")
