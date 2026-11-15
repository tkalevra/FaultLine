"""NIMRATE — a turn the brain was never ASKED must not be recorded as a turn it answered.

THE DEFECT. ``apply_rate_limit_sync``/``_async`` return False for a BACKGROUND-lane call with
no rate capacity, and the LLM helper then returns the marker ``{"error": "rate_deferred"}``.
That marker exists precisely so a merely-PACED call is distinguishable from "the model
answered nothing" (llm_calls: *"a bare {} is byte-identical to 'the model answered nothing'
downstream … The error marker lets callers that can wait (documents, reextract) re-pend
instead of fail"*). ``/harvest-spans`` surfaces it upstream as a degradation envelope carrying
``llm_first_unanswered_reason="rate_deferred"``.

``_statement_defer_worker`` did not honour that distinction. Its retry loop retried only on an
EXCEPTION, under the rule *"a clean (non-exception) return is a REAL answer — do not retry
it"*. A paced turn returns cleanly, so the worker broke out of the loop and tallied the turn
``statement_defer_no_edges`` — DROPPED, with nothing raised, and indistinguishable in the log
from a turn that legitimately carried no facts.

MEASURED CONSEQUENCE (local stack, NVIDIA NIM brain pinned at the 1-rpm back-off floor): a turn
stating a MAC address produced no ``has_mac`` row at all, and a correction turn left the OLD IP
in place. The user's stated fact was simply absent afterwards.

WHY ``episodic_log`` IS NOT AN ADEQUATE BACKSTOP HERE, and why this is a data-loss pin rather
than a latency one: ``reextract_episodic`` is gated behind a ONE-HOUR age floor and re-ingests
with ``source="reextract"``, which the /ingest provenance router maps to ``llm_inferred``. So a
fact the user STATED returns an hour later DEMOTED out of Class-A ``user_stated``. Retention of
the raw turn is real, but it does not restore the fact, and it does not restore its authorship.

PRIMARY SOURCE for the distinction being pinned: M. Nygard, *Release It!* (2nd ed., Pragmatic
Bookshelf, 2018), ch. 5 "Stability Patterns" — a load-shed/fast-fail rejection must be
DISTINGUISHABLE from a successful empty answer, or a transient capacity event becomes
permanent data loss. Same rule the document lane already pins in test_document_ingest_loss.py.
"""

import pytest

from src.mcp import server


def _degraded(reason):
    """The envelope /harvest-spans returns when an LLM call went unanswered."""
    return {
        "status": "extraction_degraded", "committed": 0, "staged": 0,
        "isError": True, "extraction_degraded": True, "edges_withheld": 0,
        "llm_calls_unanswered": 1, "llm_unanswered_operations": "REFRAME",
        "llm_first_unanswered_reason": reason, "retained_for_reextraction": True,
    }


def test_a_rate_deferred_degradation_is_recognised_as_never_asked():
    """THE BAR. The turn was paced, not answered — the worker must be able to tell."""
    assert server._result_is_paced_not_answered(_degraded("rate_deferred")) is True


def test_the_operation_prefixed_form_is_recognised_too():
    """The reason is emitted bare AND ``OP:reason``-prefixed depending on the seam that
    raised it, so the match is a substring — the same precedent the re_embedder uses
    (``_paced = "rate_deferred" in _reason``). A prefix must not defeat recognition."""
    assert server._result_is_paced_not_answered(_degraded("REFRAME:rate_deferred")) is True
    assert server._result_is_paced_not_answered(
        {"extraction_degraded": True, "llm_unanswered_operations": "EXTRACT:rate_deferred",
         "llm_first_unanswered_reason": ""}) is True


@pytest.mark.parametrize("reason", [
    "REFRAME:all_retries_exhausted:HTTPStatusError",   # the brain WAS asked and failed
    "circuit_breaker_open",                            # a real outage verdict
    "empty_llm_response",                              # the brain answered, with nothing
    "",
])
def test_a_genuine_non_answer_is_not_mistaken_for_pacing(reason):
    """FAIL-SAFE DIRECTION. Only PACING re-pends. Everything else keeps today's behaviour, so
    this cannot turn a real brain outage into an unbounded retry loop."""
    assert server._result_is_paced_not_answered(_degraded(reason)) is False


@pytest.mark.parametrize("result", [
    None,
    {},
    {"committed": 1, "staged": 0},                       # a clean, successful ingest
    {"status": "ok", "llm_first_unanswered_reason": "rate_deferred"},   # not degraded
    "rate_deferred",                                     # not a dict at all
])
def test_anything_unrecognised_falls_through_to_todays_behaviour(result):
    """A predicate that gates a retry must fail toward NOT retrying on anything it does not
    understand. Note the fourth case: the marker alone is not enough — the result must
    actually BE a degradation, or a successful turn carrying a stale reason would re-pend."""
    assert server._result_is_paced_not_answered(result) is False


def test_the_worker_retries_a_paced_turn_and_gives_up_loudly(monkeypatch):
    """END TO END over the real worker loop: a paced turn must be RETRIED, and when the retry
    budget is exhausted while still only paced it must be logged as a CRITICAL loss — not as
    an ordinary 'no edges' turn, which is what made this invisible in production."""
    import asyncio

    calls = {"n": 0}
    crits, notes = [], []

    async def _fake_spine(text, user_id, **kw):
        calls["n"] += 1
        return _degraded("rate_deferred")

    monkeypatch.setattr(server, "_ingest_statement_via_spine", _fake_spine)
    async def _route(uid):
        return "spine"
    monkeypatch.setattr(server, "_statement_extractor_route", _route)
    monkeypatch.setattr(server, "_log_crit", lambda k, m: crits.append((k, m)))
    monkeypatch.setattr(server, "_log", lambda m: notes.append(m))
    monkeypatch.setattr(server, "_STATEMENT_DEFER_BACKOFF_S", 0.0)

    async def _drive():
        # The worker is an endless consumer (its None sentinel `continue`s, it never
        # returns), so drive it as a TASK, wait for the queue to drain, then cancel it.
        server._statement_defer_queue = asyncio.Queue()
        server._statement_defer_queue.put_nowait(("my MAC is 00:1a:2b:3c:4d:5e", "u1", "mcp"))
        task = asyncio.get_running_loop().create_task(server._statement_defer_worker())
        try:
            await asyncio.wait_for(server._statement_defer_queue.join(), timeout=20)
        finally:
            task.cancel()

    asyncio.run(_drive())

    assert calls["n"] == server._STATEMENT_DEFER_EXTRACTION_ATTEMPTS, \
        "a paced turn must consume the whole retry budget, not break on the first defer"
    assert any(k == "statement_defer_rate_exhausted" for k, _ in crits), \
        "giving up on a paced turn must be LOUD — it is a lost user-stated fact"
    assert not any("statement_defer_no_edges" in n for n in notes), \
        "a paced turn must never be reported as a turn that simply carried no facts"


