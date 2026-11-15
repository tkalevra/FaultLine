"""
Centralized LLM calling module with retry, circuit breaker, and timeout management.

This module provides a unified interface for all LLM calls across FaultLine components:
- main.py (API endpoints, extraction, validation)
- gate.py (WGM ontology validation)
- re_embedder.py (background enrichment/promotion)
- filter.py (OpenWebUI inlet/outlet)

All LLM calls must use one of these three functions:
1. call_llm_with_retry_sync() — blocking calls with retry + circuit breaker
2. call_llm_with_retry_async() — async calls with retry + circuit breaker
3. call_llm_no_retry_sync() — single attempt, graceful failure (non-critical paths)

Endpoint Resolution:
  Follows centralized priority chain: env vars > auto-detected > hardcoded fallbacks.
  All endpoints must be reachable by callers (no circular dependencies on main.py).

Circuit Breaker:
  Tracks LLM endpoint failures across all callers. When threshold exceeded, returns
  safe default without attempting new calls. Resets automatically after timeout.

Timeout Configuration:
  Per-operation timeouts via LLMTimeouts class. Prevents long-tail requests from
  blocking ingest pipeline. All timeouts configurable via environment variables.

Logging & Error Handling:
  All failures logged with full context (attempt#, endpoint, error type, user_id).
  Exceptions never silently caught. Circuit breaker state changes logged.

HARD CONSTRAINTS (violations will cause Phase 2 rejection):
  1. NO hardcoded endpoint strings in main logic (only in _get_endpoint_list helper)
  2. NO rel_type constants or database schema assumptions
  3. NO import-time database queries (lazy imports only)
  4. NO timeout values in conditional logic (all from LLMTimeouts class)
  5. NO silent exception handlers (FAIL LOUD principle)
  6. NO circular imports from main.py (lazy imports inside functions)
"""

import asyncio
import contextvars
import hashlib
import json
import os
import re as _re
import structlog
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import httpx

from src.api import errors as _errors  # THE ONE ERROR SEAM (redact_text)
from src.api import llm_source_ip
from src.api import llm_lane
from src.api.llm_lane import LLMUnavailable  # re-exported: callers import it from here

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# LLM NON-ANSWER LEDGER (request-scoped) — "FAIL LOUD, NEVER SILENT"
# ──────────────────────────────────────────────────────────────────────────────
# Every function in this module answers a NON-ANSWER the same way it answers a real
# one: by RETURNING (a bare {}, or the circuit-breaker's {"error": ...} dict). That is
# deliberate — an ingest lane must degrade, not crash. But it also means a caller that
# only reads the returned edges CANNOT TELL "the brain said there is nothing here" from
# "the brain never answered", and so reports a degraded run as a clean one.
#
# DOCLOSS-B closed exactly this hole for ONE caller — the /extract/rewrite chunk loop —
# by hand, with a per-chunk failure list local to that endpoint. Every OTHER LLM-bearing
# ingest lane (/harvest-spans and the spine deriver it drives, the merge-brain relation
# fill, the atomizer) still has no signal at all.
#
# This ledger generalises the DOCLOSS-B idea to the ONE place every lane already funnels
# through. It is a pure OBSERVER: a ContextVar that is None unless a caller explicitly
# opens a scope, so with no scope open the recording call is a single dict-get and NOTHING
# about any existing code path changes. A caller that opens a scope can then report, on its
# own response envelope, exactly which operations went unanswered while it ran.
#
# EVIDENCE. Martin Fowler, "CircuitBreaker" (martinfowler.com/bliki/CircuitBreaker.html),
# verbatim: "Any change in breaker state should be logged and breakers should reveal
# details of their state for deeper monitoring." and "Breaker behavior is often a good
# source of warnings about deeper troubles in the environment." REVEALING the state is
# half the pattern, and it is the half a `return {}` silently drops: our breaker logs its
# own trip, but the ingest RESPONSE never carried it, so the operator-facing signal died
# at the process boundary. (Same principle Nygard's *Release It!* circuit-breaker chapter
# is invoked for at src/re_embedder/embedder.py::_document_chunk_edges — this generalises
# that one hand-rolled ledger to every LLM-bearing lane.)
_LLM_NONANSWER_LEDGER: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "_llm_nonanswer_ledger", default=None)


def begin_llm_degradation_scope():
    """Open a request-scoped LLM non-answer ledger. Returns a token for reset."""
    return _LLM_NONANSWER_LEDGER.set(
        {"total": 0, "by_operation": {}, "reasons": [], "rate_deferred": False})


def reset_llm_degradation_scope(token) -> None:
    """Close the scope opened by begin_llm_degradation_scope(). Never raises."""
    try:
        _LLM_NONANSWER_LEDGER.reset(token)
    except Exception:  # noqa: BLE001 — an observer must never break its host
        pass


def llm_degradation_snapshot() -> Optional[dict]:
    """The current scope's ledger, or None when no scope is open / nothing failed."""
    ledger = _LLM_NONANSWER_LEDGER.get()
    if not ledger or not ledger.get("total"):
        return None
    return {
        "llm_calls_unanswered": ledger["total"],
        "llm_unanswered_operations": dict(ledger["by_operation"]),
        "llm_first_unanswered_reason": ledger["reasons"][0] if ledger["reasons"] else None,
        # BUG-005 typed outcome: at least one op was RATE-DEFERRED (the limiter never put
        # the request on the wire — the brain was merely PACED, not down). ADDITIVE key —
        # present (False) even when no op was paced, so every consumer can branch on the
        # typed signal instead of substring-matching reason strings.
        "llm_rate_deferred": bool(ledger.get("rate_deferred")),
    }


def llm_scope_rate_deferred() -> bool:
    """True when the OPEN scope has recorded a rate_deferred non-answer.

    THE PACED-TURN ABORT SIGNAL (BUG-005). A rate defer means the limiter never put that
    request on the wire — every further LLM op this turn would issue waits against (and
    consume slots from) the very budget that is already starved. A harvest turn consults
    this between its LLM ops and SKIPS the remaining ones, so a paced turn degrades
    immediately instead of grinding its ladder. No scope ⇒ False (today's behaviour);
    a non-rate failure ⇒ False (only a PACED defer aborts the turn). Never raises."""
    try:
        ledger = _LLM_NONANSWER_LEDGER.get()
        return bool(ledger and ledger.get("rate_deferred"))
    except Exception:  # noqa: BLE001 — an observer must never break its host
        return False


# ──────────────────────────────────────────────────────────────────────────────
# BYO-BRAIN HEALTH TELEMETRY TEE (SaaS increment 1)
# ──────────────────────────────────────────────────────────────────────────────
# The signals that diagnosed the 2026-07-31 BYO-brain incident (per-attempt latency,
# attempt failures, circuit-breaker refusals, the fail-closed no-brain path) are already
# emitted here as structured logs the TENANT CANNOT READ. `_health_note` tees the SAME
# signal, at the SAME sites, into the per-seat health aggregate so a console card can
# show it. See DEV/DESIGN-byo-brain-health-and-alerting.md and saas/brain_health.py.
#
# ⛔ TELEMETRY, NEVER AN OUTAGE: this is total — SAAS off / saas package absent / any
# error → silent no-op. It performs no I/O inline (the recorder buffers in memory and
# flushes on a daemon thread). A monitoring bug must not become an ingest failure.
_HEALTH_SINK_MISSING = False


def _health_note(user_id: Optional[str], metric: str, operation: str = "*",
                 elapsed_seconds: Optional[float] = None,
                 endpoint: Optional[str] = None,
                 budget_seconds: Optional[float] = None,
                 detail: Optional[str] = None,
                 exc: Optional[BaseException] = None,
                 response_body: Optional[str] = None) -> None:
    """Tee one LLM-lane signal to the per-seat brain-health aggregate. Never raises.

    TWO SINKS, deliberately, because they answer two different questions:
      • ``record()``      → the rolling WINDOW aggregate: "how often lately" (trend/diagnosis).
      • ``probe_event()`` → the ALARM STREAM: "is it broken right now". One event with an
        ``outcome`` field, last write wins, so a success silently clears a prior failure with
        no TTL or decay job. ⚠️ THAT ONLY WORKS IF THE SUCCESS PATH EMITS TOO — a lane that
        reports only its errors keeps its last error as "current state" forever. ``llm_call``
        (success) is therefore mapped just as deliberately as ``llm_call_failed``.

    ⚠️ ``exc`` / ``response_body`` ARE THE DIAGNOSIS, AND DROPPING THEM WAS THE BUG.
    Every failure call site already holds the exception and — for an HTTP error — the body it
    came with; the ``llm_call.attempt_failed`` log line right above each one prints both. That
    evidence used to stop at the log, and the tenant-facing notice, having only "something
    failed", described every failure as a timeout and told everybody to check a firewall. A
    production tenant whose endpoint answered ``402 Payment Required`` in 90ms with "Visit your
    billing tab" was sent to configure their network. Handing the exception through here is what
    lets ``saas.brain_fault.describe`` say what actually happened.

    The derivation is done INSIDE the saas block on purpose: ``src/`` stays free of it, so an
    open-core deployment with no ``saas/`` package is byte-identical and pays nothing.
    """
    global _HEALTH_SINK_MISSING
    if _HEALTH_SINK_MISSING:
        return
    try:
        from saas import brain_health
        brain_health.record(user_id, metric, operation=operation,
                            elapsed_seconds=elapsed_seconds)
        _fault = None
        if exc is not None:
            try:
                from saas import brain_fault
                # ⛔ THE CREDENTIALS THIS CALL ACTUALLY SENT. A provider echoing the key back
                # in its own error message is the realistic leak (OpenAI and AWS both do it),
                # and it is the one case that needs no heuristic: the value is in hand here.
                # Resolved fresh from the bound tenant config, never cached, never logged.
                _secrets = []
                try:
                    from saas.llm_context import current_llm_config
                    _cfg = current_llm_config() or {}
                    # ⚠️ `embedding_api_key` — `_build_config` emits that name
                    # (saas/llm_config.py) and one builder serves both the account and seat
                    # resolvers, so the old `embedding_key` lookup was None on every single
                    # call. "embedding_key" is only ever an admin UPDATE-INPUT name. Half of
                    # the one exact defence was dead and looked alive.
                    _secrets = [_cfg.get("api_key"), _cfg.get("embedding_api_key")]
                except Exception:  # noqa: BLE001
                    _secrets = []
                _fault = brain_fault.describe(exc, response_body, _secrets)
            except Exception:  # noqa: BLE001 — a classifier is never load-bearing
                _fault = None
        _outcome = {
            "llm_call": brain_health.OUTCOME_OK,            # ← the ok side; do not drop it
            "llm_call_failed": brain_health.OUTCOME_FAIL,
            "breaker_open": brain_health.OUTCOME_FAIL,
            "no_brain": brain_health.OUTCOME_FAIL,
            # A SAVED endpoint whose stored credential will not decrypt. Deliberately its own
            # metric rather than another `no_brain`: the two need different words and
            # different remedies, and `no_brain` is a SETUP state that is intentionally never
            # emailed — routing a real, actionable fault through it is how three accounts sat
            # broken for days with the engine shouting into a log nobody reads.
            "key_undecryptable": brain_health.OUTCOME_FAIL,
            # A saved endpoint whose HOST cannot be resolved or is refused by the egress
            # policy. Same zero-spend refusal, a third set of words: nothing is unconfigured
            # and nothing is unreadable — the address itself is the problem.
            "egress_blocked": brain_health.OUTCOME_FAIL,
        }.get(metric)
        if _outcome is not None:
            brain_health.probe_event(
                user_id, str(operation or "*").upper(), _outcome,
                operation=operation, elapsed_s=elapsed_seconds,
                budget_s=budget_seconds, endpoint=endpoint,
                reason=(None if _outcome == brain_health.OUTCOME_OK else metric),
                detail=detail, fault=_fault)
    except ImportError:
        # Open-core deployment with no saas/ package — remember, stop trying (hot path).
        _HEALTH_SINK_MISSING = True
    except Exception:  # noqa: BLE001 — an observer must never break its host
        pass


# The fail-closed refusals that are NOT "you have not set this up yet". Each is a fault the
# tenant can act on, and each needs different words; routing them all through `no_brain` is
# what tells somebody who HAS configured an endpoint to go and configure one.
_NO_BRAIN_REASON_METRICS = {
    "key_undecryptable": "key_undecryptable",
    "egress_blocked": "egress_blocked",
}

# ...and the customer-facing event-log reason for each. One mapping instead of a conditional
# repeated at three call sites, because the previous shape — a ternary naming ONE of the
# special cases — is why the second one was missed at all three of them.
_NO_BRAIN_EVENT_REASONS = {
    "key_undecryptable": "saas_key_undecryptable_failclosed",
    "egress_blocked": "tenant_egress_blocked",
    "no_brain": "saas_no_brain_failclosed",
}


def no_brain_metric() -> str:
    """WHICH fail-closed condition this is — not merely THAT one happened.

    The sentinel bound by `llm_context._no_brain_sentinel` carries the resolver's own refusal
    reason when it knows one. Reading it here is what keeps the distinction from being lost at
    the last hop — every guard below fails closed identically, and without this they would all
    report the generic state.

    ⚠️ ``egress_blocked`` was the SECOND condition to be swallowed this way, and it survived
    the fix for the first. `saas.llm_config` has named it since `explain_unresolvable` was
    written, and nothing carried it: a seat whose base_url host cannot be resolved, or is
    refused by the egress policy, was told "No model endpoint is connected to FaultLine yet".
    Measured on pre-prod 2026-08-18.

    Fail-safe: anything unknown → ``no_brain``, i.e. today's behaviour.
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config() or {}
        return _NO_BRAIN_REASON_METRICS.get(
            str(cfg.get("no_brain_reason") or ""), "no_brain")
    except Exception:  # noqa: BLE001
        pass
    return "no_brain"


def no_brain_detail() -> str:
    """The resolver's OWN sentence about its refusal, or None. Never raises."""
    try:
        from saas.llm_context import current_llm_config
        return (current_llm_config() or {}).get("no_brain_detail") or None
    except Exception:  # noqa: BLE001
        return None


def _llm_input_echo(messages, limit: int = 120) -> Optional[str]:
    """The user's OWN words from this call, truncated — so the failure message can say WHAT
    timed out instead of "an error occurred".

    ⚠️ THE DOCSTRING USED TO CLAIM the last ``role == "user"`` turn "on every lane here is the
    tenant's text". IT IS NOT, and the notice printed the difference to tenants. MEASURED on
    pre-prod 2026-08-18: a seat read *The most recent turn affected was "[FaultLine-Internal]
    You are an ontology classifier. Classify the CONCEPT below into exactly ONE general type
    and (opti…"*. Only the lanes that hand the model the tenant's sentence VERBATIM (REFRAME,
    and the TURN lane whose detail is written by the code that holds the turn) satisfy the
    premise; every lane that BUILDS a prompt — the classifier, the subject resolver, the
    natural-language fill — puts our own instructions in the ``user`` role.

    This can only be decided by the caller, so the value stays as a stored diagnostic and the
    tenant-facing renderer prints it only from the lane that owns the tenant's words
    (``brain_health.degradation_notice``). What IS decided here is the one unambiguous case:
    content carrying the ``[FaultLine-Internal]`` marker is ours by construction and is never
    echoed anywhere.

    Newlines collapsed, hard-truncated with an ellipsis. Never raises.
    """
    try:
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                txt = " ".join(str(m.get("content") or "").split())
                if not txt or "[FaultLine-Internal]" in txt:
                    return None
                return txt if len(txt) <= limit else txt[:limit - 1].rstrip() + "…"
    except Exception:
        pass
    return None


def _record_llm_nonanswer(operation: str, reason: str) -> None:
    """Record ONE non-answer against the open scope. No scope ⇒ no-op, no raise."""
    ledger = _LLM_NONANSWER_LEDGER.get()
    if ledger is None:
        return
    try:
        ledger["total"] += 1
        op = str(operation or "UNKNOWN")
        ledger["by_operation"][op] = ledger["by_operation"].get(op, 0) + 1
        if "rate_deferred" in reason:
            # BUG-005: one paced defer turns the WHOLE turn into a typed paced outcome —
            # every later op this turn would burn against the same starved budget.
            # Substring (not ==) so a variant reason spelling still classifies; the
            # authored reasons are literal "rate_deferred" today.
            ledger["rate_deferred"] = True
        if len(ledger["reasons"]) < 10:
            ledger["reasons"].append(f"{op}:{reason}")
    except Exception:  # noqa: BLE001 — an observer must never break its host
        pass


# ──────────────────────────────────────────────────────────────────────────────
# CUSTOMER-FACING BRAIN NON-ANSWER EVENT LOG (SaaS increment)
# ──────────────────────────────────────────────────────────────────────────────
# The signals above feed the per-REQUEST ledger (the ingest RESPONSE). This feeds the TENANT
# event log: a not-our-fault brain non-answer becomes a clear, actionable line the customer can
# read in their console ("your model endpoint didn't respond", "your API key was rejected",
# "this account is out of credit", "rate-limited, backing off", "no model configured", …).
# Crucially a FaultLine-side failure produces NO customer line — never gaslight. See
# saas.brain_events for the their-fault ↔ our-fault classification + spam control.
#
# ⛔ SAME POSTURE AS _health_note: total — SAAS off / saas package absent / any error → no-op.
# One inline Redis SETNX (dedup, sub-ms) on the non-answer path, then a daemon-thread DB write.
# An observability tee must never slow or break a live LLM call.
def _emit_brain_nonanswer(user_id, reason: str, exc: Optional[Exception] = None) -> None:
    """Tee ONE customer-facing brain non-answer to the SaaS event log. Never raises/blocks.

    ``reason`` is the same token passed to ``_record_llm_nonanswer``; ``exc`` is the underlying
    exception (consulted only for ``all_retries_exhausted`` to read the HTTP status). Scope and
    cfg are resolved via the SAME resolvers the rate budget / breaker / cache key on, so the
    event is stamped with the brain's true owner (the house-account leak fix).
    """
    try:
        from saas import brain_events
        brain_events.emit_brain_nonanswer(
            user_id, reason, exc=exc,
            scope=_resolve_brain_scope(), cfg=_current_llm_config_or_none())
    except Exception:  # noqa: BLE001 — observability must never break or slow a call
        pass


def _bump_429_counter() -> None:
    """Count ONE provider rate-limit response (HTTP 429) against this brain. Never raises.

    THE ONE CHOKEPOINT for the 429 count: ``_observe_rate_headers`` is the single function that
    sees every response (success + failure, sync + async), so a 429 is counted exactly once per
    received response here — never double-counted by the retry/fallback paths that call into it.
    Bounded by the brain scope (brain-scoped Redis counter); no scope → no-op.
    """
    try:
        from src.api import brain_meter
        scope = _resolve_brain_scope()
        if scope is None:
            return
        brain_meter.record(scope, brain_meter.KIND_RATE429)
    except Exception:  # noqa: BLE001 — instrumentation must never break a call
        pass


# ──────────────────────────────────────────────────────────────────────────────
# THINK-TAG STRIPPING: Qwen3 models wrap reasoning in <think>...</think> tags
# before the actual JSON output.  Strip these before JSON parsing.
# Defense-in-depth Layer 2 — Layer 1 (chat_template_kwargs in llm_client.py)
# prevents thinking at the request level; this catches leakage from weak models.
# ──────────────────────────────────────────────────────────────────────────────
_THINK_RE = _re.compile(r'<think>.*?</think>', _re.DOTALL)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from LLM output.

    ⚠️ THIS IS COSMETIC. It cleans the TEXT; it does NOT prevent reasoning. By the time a
    ``<think>`` block reaches us the tokens were generated, billed, and the latency paid —
    and if the model spent its budget reasoning, the JSON may be truncated so there is
    nothing left to salvage. Actual suppression happens at REQUEST time
    (``llm_client.resolve_reasoning_controls``); this remains only as the last-resort net
    for a self-hosted model whose off-switch we could not resolve.
    """
    return _THINK_RE.sub('', text).strip()


_FENCE_RE = _re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", _re.DOTALL)


# ── LAST-CALL USAGE ACCOUNTING (dprompt-156) ────────────────────────────────────────────
# The retry wrappers return the PARSED CONTENT only; the response's usage block is consumed
# by the reasoning audit and discarded. A caller that needs the accounting (the brain Test &
# calibrate probe, which must show a cannot-disable model's measured reasoning spend as
# ordinary output) has no way to reach it. This thread-local is that seam: each call records
# the usage it saw, same-thread read-back, no cross-thread races. Fail-safe everywhere.

_last_usage = threading.local()


def last_call_usage() -> Optional[dict]:
    """Usage block of the most recent LLM call made ON THIS THREAD, or None.

    Shape: ``{"completion_tokens": int|None, "reasoning_tokens": int|None, "operation": str}``
    (reasoning_tokens from the provider's reporting path when it sends one). Deliberately
    thread-scoped: the probe reads back its OWN call's accounting, never another caller's.
    """
    return getattr(_last_usage, "usage", None)


def _record_call_usage(result: dict, operation: str) -> None:
    """Stash the usage numbers of THIS call for same-thread read-back. Never raises."""
    try:
        comp = _dig(result, "usage.completion_tokens")
        reasoning = None
        from src.api.llm_client import _REASONING_USAGE_PATHS
        for _p in _REASONING_USAGE_PATHS:
            _v = _dig(result, _p)
            if isinstance(_v, (int, float)) and _v > 0:
                reasoning = int(_v)
                break
        _last_usage.usage = {
            "completion_tokens": int(comp) if isinstance(comp, (int, float)) else None,
            "reasoning_tokens": reasoning,
            "operation": str(operation or "*").upper(),
        }
    except Exception:
        _last_usage.usage = None



def _unfence(text: str) -> str:
    """Strip a markdown code fence around a JSON payload.

    Models routinely return ```` ```json\\n[...]\\n``` ```` even when told to emit bare JSON —
    DeepSeek, GPT, Claude and Qwen all do it intermittently. ``json.loads`` fails on the
    fenced form, and the object-only regex fallback below cannot rescue an ARRAY, so a fenced
    response was being recorded as ``no_json_in_response`` — a hard "the model gave us
    nothing" verdict for a response that was actually perfectly well-formed.

    Measured 2026-07-30: this is what stalled document ingest. A chunk containing no
    extractable facts legitimately returns ``[]``; fenced, that read as a FAILURE, was
    retried to exhaustion, and fed the circuit breaker — so a run of fact-free chunks tripped
    the breaker OPEN and every good chunk after it was rejected. 53 breaker-open events and
    18 chunk timeouts in four minutes, on a document whose chunks were mostly fine.
    """
    if not text:
        return text
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


def _text_of(value) -> str:
    """A provider text field as a str. ``None`` / non-text → ``""``; never raises.

    OpenAI's multipart shape (``[{"type": "text", "text": "..."}]``) is joined. Anything else
    that is not a string is NOT stringified — ``str(None)`` would hand the JSON parser "None".
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for p in value:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "".join(parts)
    return ""


def response_message_text(result, operation: str = "", user_id: Optional[str] = None) -> str:
    """The answer text of an OpenAI-shaped chat completion. ALWAYS a ``str``; never raises.

    ⚠️ Why this exists (issue #107): every caller read
    ``result["choices"][0]["message"].get("content", "").strip()``. ``.get(k, "")`` only
    defaults a MISSING key — OpenAI-compatible servers (vLLM behind NVIDIA NIM, measured
    2026-10-01 on google/diffusiongemma-26b-a4b-it) send the key PRESENT with JSON ``null``
    when the model spent the whole completion budget in its reasoning channel
    (``finish_reason="length"``, ``"content": null``, ``"reasoning": "<chain of thought>"``).
    ``None.strip()`` raised AttributeError AFTER a successful HTTP call; the retry loop read
    it as an endpoint failure, retried the same request, fed the circuit breaker and every
    turn was withheld (``extraction_degraded``).

    Channels, in order — the first one carrying text wins:
      1. ``message.content`` (str, or OpenAI multipart list);
      2. ``message.tool_calls[0].function.arguments`` (a provider that answers structured
         output as a tool call);
      3. ``message.reasoning`` / ``message.reasoning_content`` — ONLY when the generation
         FINISHED (``finish_reason != "length"``). A server whose reasoning parser never saw
         the end-of-thought marker files the whole completed output there; but a reasoning
         channel cut off by the token budget is an unfinished thought, never an answer, and
         salvaging JSON out of it would file facts the model did not conclude.

    Nothing usable → ``""`` and ONE loud warning naming the finish_reason and the channel
    that ate the budget, so "empty content" is never silent and never an exception.
    """
    try:
        choice = ((result or {}).get("choices") or [{}])[0] or {}
        msg = choice.get("message") or {}
    except Exception:  # noqa: BLE001 — a malformed body is "no text", not a crash
        return ""
    if not isinstance(msg, dict):
        return ""
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None

    text = _text_of(msg.get("content")).strip()
    if text:
        return text

    for call in (msg.get("tool_calls") or []):
        fn = call.get("function") if isinstance(call, dict) else None
        args = _text_of((fn or {}).get("arguments")).strip() if isinstance(fn, dict) else ""
        if args:
            log.warning("llm_response.content_from_tool_call", operation=operation,
                        user_id=user_id, finish_reason=finish)
            return args

    reasoning_field = next((k for k in ("reasoning_content", "reasoning")
                            if _text_of(msg.get(k)).strip()), None)
    if reasoning_field and finish != "length":
        log.warning("llm_response.content_from_reasoning_channel", operation=operation,
                    user_id=user_id, finish_reason=finish, field=reasoning_field,
                    note="content empty but the generation finished; reading the answer "
                         "from the reasoning channel (server reasoning-parser misfile)")
        return _text_of(msg.get(reasoning_field)).strip()

    log.warning("llm_response.no_usable_content", operation=operation, user_id=user_id,
                finish_reason=finish,
                content_type=type(msg.get("content")).__name__,
                reasoning_field=reasoning_field,
                reasoning_chars=len(_text_of(msg.get(reasoning_field))) if reasoning_field else 0,
                note=("the completion budget was spent in the reasoning channel before any "
                      "answer was written" if reasoning_field and finish == "length"
                      else "the response carried no answer text in any known channel"))
    return ""


def _dig(obj, dotted: str):
    """Fetch a dotted path out of nested dicts, or None. Never raises."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _audit_reasoning_leak(result: dict, operation: str, user_id: Optional[str] = None,
                          expected: bool = False,
                          budget_padded: Optional[int] = None,
                          model: Optional[str] = None) -> None:
    """VERIFY that reasoning suppression actually took effect, from the response.

    We cannot trust a 200. Measured 2026-07-30: DeepSeek accepted a suppression field it did
    not recognize (enable_thinking, a Qwen/vLLM convention) and reasoned anyway — no error
    to catch. So the only honest check is to read a reasoning-token count back out of the
    response and complain when it is non-zero.

    Providers report this under different paths (and some not at all) — see
    llm_client._REASONING_USAGE_PATHS. A miss is not evidence of success, merely absence
    of evidence, so this NEVER asserts "suppression worked"; it only fires when it provably
    did NOT. Also records token usage, which we previously discarded entirely — which is why
    ~1300 wasted reasoning tokens per call were invisible in production.

    expected=True means the resolved model CANNOT disable reasoning (can_disable=False),
    so a non-zero reasoning count is EXPECTED, not a bug — log at info, do NOT fire the
    suppression_ineffective CRIT (that lane is reserved for a field we SENT being ignored).

    budget_padded is the headroom ACTUALLY applied to this call's completion budget (0/None
    = none). The expected-case note states it as measured fact, never as an assumption —
    "the budget was padded" is only honest when padding really happened (LLM_REASONING_TOKEN_
    HEADROOM=0 or a resolution failure both leave it unpadded, and the note must say so).

    model, when given, lets the note state WHY reasoning was expected at its true strength.
    There are two different facts that both arrive here as expected=True:
      (a) GENUINE cannot-disable — the endpoint host is KNOWN and its recorded spec says
          reasoning cannot be turned off (z.ai coding surface, Cerebras gpt-oss, Grok).
      (b) RESOLUTION FAILURE — the host is NOT recognized, so no suppression field could
          be resolved or sent at all; the model name says it reasons, so the budget was
          padded as a PRECAUTION (measured live: LAN llama.cpp brains serving Qwen/GLM
          weights resolve unknown and pad). The provider may well be able to disable
          reasoning — WE failed to find the switch, and "this model cannot disable
          reasoning" would claim a provider capability nobody established. The note for
          (b) says exactly that, at info strength (the pad is the safe direction; this
          is visibility, not an alarm).

    Fail-safe: never raises, never alters the result.
    """
    try:
        from src.api.llm_client import _REASONING_USAGE_PATHS
        reasoning = None
        found_path = None
        for path in _REASONING_USAGE_PATHS:
            v = _dig(result, path)
            if isinstance(v, (int, float)) and v > 0:
                reasoning, found_path = int(v), path
                break
        if not reasoning:
            return
        if expected:
            _padded = int(budget_padded or 0)
            # WHICH kind of "expected" is this? A KNOWN host cannot-disable verdict is a
            # recorded provider capability; an UNKNOWN host means resolution FAILED and the
            # pad was a precaution — the note must not claim a capability nobody
            # established. Fail-safe default: known=True (the genuine reading).
            _known = True
            _source = "unresolved"
            if model:
                try:
                    from src.api.llm_client import (resolve_reasoning_controls,
                                                    _reasoning_base_url)
                    _res = resolve_reasoning_controls(_reasoning_base_url(), model)
                    _known = bool(_res.get("known"))
                    _source = str(_res.get("source") or "unresolved")
                except Exception:
                    _known = True
            if not _known:
                if _padded > 0:
                    _note = (f"reasoning measured and suppression UNRESOLVED — the "
                             f"reasoning controls for endpoint '{_source}' could not be "
                             f"resolved (unrecognized provider), so no suppression field "
                             f"was sent and the completion budget was padded by {_padded} "
                             f"tokens as a PRECAUTION. Whether this provider can disable "
                             f"reasoning is UNRESOLVED, not established; recording its "
                             f"control field in llm_client._REASONING_CONTROLS would let "
                             f"the field be sent and the pad retired if one exists.")
                else:
                    _note = (f"reasoning measured and suppression UNRESOLVED — the "
                             f"reasoning controls for endpoint '{_source}' could not be "
                             f"resolved (unrecognized provider), no suppression field was "
                             f"sent, and the completion budget was NOT padded "
                             f"(LLM_REASONING_TOKEN_HEADROOM=0) — capture may truncate "
                             f"on long turns")
            elif _padded > 0:
                _note = (f"reasoning expected — this model cannot disable reasoning "
                         f"(can_disable=False); the completion budget was padded by "
                         f"{_padded} tokens so capture stays intact. This spend is "
                         f"UNAVOIDABLE on this provider: it is charged on every call and "
                         f"is NOT a solved problem.")
            else:
                _note = ("reasoning expected — this model cannot disable reasoning "
                         "(can_disable=False); the completion budget was NOT padded "
                         "(LLM_REASONING_TOKEN_HEADROOM=0 or resolution failed) — capture "
                         "may truncate on long turns")
            log.info("llm_reasoning.expected",
                     operation=operation, user_id=user_id,
                     reasoning_tokens=reasoning, usage_path=found_path,
                     budget_padded=_padded,
                     # the machine-readable half of the distinction: "unresolved" = the
                     # pad is a precaution after a resolution failure, "known" = a
                     # recorded provider capability. Ordinary output can tell the two
                     # apart without parsing prose.
                     resolution=("unresolved" if not _known else "known"),
                     resolution_source=_source,
                     note=_note)
            # Tee the measured overhead to the brain-health window so the cost of a
            # cannot-disable brain is a first-class metric, not something inferable only
            # from raw token counts. Count-only by design (the samples table has no value
            # column); the MAGNITUDE lives in this log line and the brain-test surface.
            _health_note(user_id, "reasoning_overhead", operation)
            return

        from src.api.logging_config import log_crit
        if reasoning <= _REASONING_FLOOR_NOISE:
            log.debug("llm_reasoning.floor_noise",
                      operation=operation, user_id=user_id,
                      reasoning_tokens=reasoning,
                      note="reasoning count at/below the noise floor — not a suppression "
                           "failure signal")
            return
        log_crit(log, "llm_reasoning.suppression_ineffective",
                 operation=operation, user_id=user_id,
                 reasoning_tokens=reasoning, usage_path=found_path,
                 note="the model REASONED despite suppression — the field we send for this "
                      "host/model is wrong, ignored, or unsupported. Tokens and latency were "
                      "spent, and extraction JSON may be truncated. Fix the entry in "
                      "llm_client._REASONING_CONTROLS; do NOT rely on _strip_think_tags.")
    except Exception:
        return  # observability must never break a call

# Below this count, a reasoning-tokens reading is floor/transport noise rather than
# evidence of a suppression failure (suppressed medians measure 5-10; failures measure
# 145-500). Real leaks stay far above.
_REASONING_FLOOR_NOISE = 50

# Module-level sync HTTP client — initialized at import time so it is available
# in both the FastAPI process and the re_embedder subprocess without depending
# on the FastAPI lifespan coroutine.
_llm_http_client: httpx.Client = httpx.Client(
    timeout=httpx.Timeout(30.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)


# ──────────────────────────────────────────────────────────────────────────────
# BREAKER EVIDENCE MODEL — what counts as evidence that the DEPENDENCY is unavailable
# ──────────────────────────────────────────────────────────────────────────────
# A circuit breaker answers exactly one question: **"is the dependency unavailable?"** It
# must NOT be tripped by "this particular request is unacceptable". Those are different
# facts and the old counter could not tell them apart — so a single well-grown tenant whose
# prompts exceeded the served window produced `400 exceed_context_size_error`, and THREE of
# them opened the breaker for EVERY seat on the account (the breaker is keyed per-account).
# The context guard now short-circuits that one error before it reaches the counter, but
# that fixed a symptom; this fixes the principle.
#
# THE RULE: the same request failing N times is ONE fact observed N times, not N facts.
# Diversity of failing requests is the actual evidence of unavailability.
#
# TWO CLASSES, DELIBERATELY UNEQUAL WEIGHT:
#
#   CLASS_DEPENDENCY — a connect/read timeout, a transport error, 5xx, 429. The dependency
#       did not give a settled answer about our request; it gave evidence about ITSELF.
#       **Counted in full, every occurrence, never de-duplicated.** This is what preserves
#       fast tripping during a real outage, and it is why an outage cannot be masked: when
#       the endpoint is down, EVERY call fails this way, including retries of one turn.
#
#   CLASS_REQUEST — 400/404/413/422 and friends. The dependency answered, promptly and
#       definitively: *this payload is unacceptable.* That is a verdict on OUR request. It
#       is evidence the endpoint is UP (it parsed and judged us). Counted **once per
#       distinct signature per window** — a thousand oversized documents are one fact.
#
#   UNKNOWN → CLASS_DEPENDENCY. Fail-safe: anything unclassified behaves exactly as it does
#       today (counted in full). New failure modes can only ever be as sensitive as legacy,
#       never less.
#
# WHY THE FINGERPRINT CONTAINS NO PAYLOAD — this rejects the first-instinct design:
#   Fingerprinting the request body would make 500 different oversized documents look like
#   500 independent facts and trip the breaker anyway — the exact bug, re-created. The
#   signature is the CLASS of rejection, not the instance: (scope, brain, operation, status,
#   error token). Five hundred oversized docs collapse to one signature. Conversely the
#   retry-storm worry ("every retry of one turn looks identical, masking a real outage")
#   does not arise, because a real outage is CLASS_DEPENDENCY and is never de-duplicated at
#   all. Payload hashing turns out to be wrong in both directions, so it is absent.
#
# WHY `operation` IS in the signature: an endpoint that rejects EVERY operation shape is far
# more likely genuinely misconfigured than one rejecting a single shape, so distinct
# operations legitimately accumulate distinct evidence. An endpoint that rejects one
# operation is our sizing/prompt bug and stays at one unit.
#
# EVIDENCE: Nygard, *Release It!* (2nd ed.) ch. "Circuit Breaker" — the breaker guards
# against *integration point* failure; and Fowler, "CircuitBreaker" (martinfowler.com) —
# state changes must be logged and observable, which the suppression path does explicitly.

CLASS_DEPENDENCY = "dependency"
CLASS_REQUEST = "request"

# Status codes where the server is telling us about ITSELF, not about our payload.
_DEPENDENCY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 598, 599})
# Status codes that are a settled verdict ON THIS REQUEST. The endpoint is demonstrably up.
_REQUEST_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 409, 411, 413, 414, 415, 422, 431})

# An `error.type` / `error.code` token from a JSON error body must look like an identifier
# before it enters a signature — free-text messages carry per-request numbers ("prompt is
# 9001 tokens") and would make every rejection unique, defeating the de-duplication.
_ERR_TOKEN_OK = _re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def breaker_evidence_enabled() -> bool:
    """Evidence-weighted breaker counting. OFF → byte-for-byte the legacy counter.

    ROLLBACK LEVER: ``LLM_BREAKER_EVIDENCE=false``.
    """
    return os.environ.get("LLM_BREAKER_EVIDENCE", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _error_body_token(exc: BaseException) -> str:
    """A short, stable identifier from a structured error body ('' when there isn't one)."""
    try:
        resp = getattr(exc, "response", None)
        if resp is None:
            return ""
        body = resp.text
        if not body or len(body) > 8192:
            return ""
        data = json.loads(body)
    except Exception:  # noqa: BLE001 — a non-JSON body simply contributes no token
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, str):
        cand = err
    elif isinstance(err, dict):
        cand = err.get("type") or err.get("code") or ""
    else:
        cand = (data.get("type") or data.get("code") or "") if isinstance(data, dict) else ""
    cand = str(cand or "").strip()
    return cand if _ERR_TOKEN_OK.match(cand) else ""


def classify_failure(exc: Optional[BaseException]) -> tuple[str, str]:
    """(failure_class, token) for one exhausted LLM call.

    Fail-safe by construction: anything not positively identified as a settled verdict on
    our request is CLASS_DEPENDENCY, i.e. counted exactly as it is today.
    """
    if exc is None:
        # No exception, yet the call produced nothing usable (e.g. every attempt parsed
        # empty). We cannot show the endpoint judged our payload → treat as dependency.
        return CLASS_DEPENDENCY, "no_exception"

    status = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)

    if isinstance(status, int):
        token = _error_body_token(exc)
        sig = f"http_{status}" + (f":{token}" if token else "")
        if status in _REQUEST_STATUSES:
            return CLASS_REQUEST, sig
        if status in _DEPENDENCY_STATUSES:
            return CLASS_DEPENDENCY, sig
        # Unlisted: 4xx is a verdict on us, 5xx (and anything else) is about them.
        if 400 <= status < 500:
            return CLASS_REQUEST, sig
        return CLASS_DEPENDENCY, sig

    return CLASS_DEPENDENCY, type(exc).__name__


def is_connect_phase_failure(exc: Optional[BaseException]) -> bool:
    """True when the failure happened in the TCP CONNECT phase (the host never answered).

    httpx raises ``ConnectTimeout``/``ConnectError`` ONLY from the connect step — under the
    old uniform budget a connect-phase timeout raised the same class, only the elapsed wall
    differed (see ``split_connect_timeout``), and a socket-level refusal during dial maps to
    ``ConnectError``. A READ failure (``ReadTimeout``/``ReadError``) means the endpoint
    ACCEPTED the connection — the host is up — and is deliberately NOT counted here.
    """
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))


def connect_trip_n() -> int:
    """Consecutive connect-phase exhausted calls that open the breaker on their own.

    ``LLM_BREAKER_CONNECT_TRIP`` (default 2, 0 disables → byte-for-byte legacy). Motivation
    (BUG-brain-connect-timeout-burns-full-budget-20261026): the per-(lane × error-class)
    breaker needed threshold=25 failures to open — ~37 minutes of dead-brain traffic at
    90s apiece, half-open probes re-paying the full budget. A host that answers NOTHING at
    the connect phase is decisive evidence about the DEPENDENCY (the bug file's fix
    direction: "trip at N consecutive connect-fails rather than 25 mixed failures"); two
    such calls in a row open the circuit, and every half-open probe re-checks it cheaply
    (a connect budget, not a read budget). The streak RESETS on a success, on any
    non-connect failure (the host demonstrably ACCEPTED a connection), and on reset().
    """
    raw = os.environ.get("LLM_BREAKER_CONNECT_TRIP", "2")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = 2
    return max(0, v)


def _resolve_brain_scope():
    """The resolved brain's ``redis_coord.Scope``, or None.

    ONE source of truth for the scope the rate budget, the circuit breaker, and the ingest
    cache key on. Reads the per-request tenant brain cfg (``saas.llm_context``) and maps it
    through ``redis_coord.scope_for_brain`` — which discriminates SEAT-owned vs ACCOUNT-owned
    brains and folds in the api-key digest, so the scope follows the BRAIN, not the account
    row. This is the fix for the house-account leak: two individuals on the "Singles" account
    with their OWN brains resolved to the SAME ``account:`` scope before, and therefore SHARED
    a rate bucket and a circuit breaker. They now resolve to two brain scopes.

    Returns None when SAAS is off / no cfg / the no-brain sentinel / any field missing — the
    caller falls back to the env ``url:`` path (byte-for-byte FOSS / unbound-tenant behaviour).
    """
    try:
        from src.api import redis_coord
    except Exception:  # noqa: BLE001
        return None
    try:
        return redis_coord.scope_for_brain(_current_llm_config_or_none())
    except Exception:  # noqa: BLE001 — scope resolution must never break a call
        return None


def _breaker_scope():
    """The ``redis_coord.Scope`` this breaker's shared state lives under, or None.

    Brain-scoped first (so two seats on one corporate brain SHARE breaker state while two
    seats with their OWN brains on a house account do NOT), else the resolved env endpoint,
    else the explicit single-tenant scope. Never an empty/implicit scope — ``redis_coord``
    would refuse to build a key from one.
    """
    try:
        from src.api import redis_coord
    except Exception:  # noqa: BLE001
        return None
    try:
        brain = _resolve_brain_scope()
        if brain is not None:
            return brain
        base = os.environ.get("LLM_BASE_URL", "").strip()
        if base:
            return redis_coord.scope_for_endpoint(base, os.environ.get("WGM_LLM_MODEL", ""))
        return redis_coord.scope_single_tenant()
    except Exception:  # noqa: BLE001 — scope resolution must never break a call
        return None


def _failure_signature(failure_class: str, token: str, operation: str) -> str:
    """The de-duplication key for a CLASS_REQUEST failure. Scope+brain come from the
    breaker instance itself (there is one breaker per account), so they are implicit."""
    return f"{failure_class}|{operation}|{token}"


# ──────────────────────────────────────────────────────────────────────────────
# CLASS: CircuitBreakerState
# ──────────────────────────────────────────────────────────────────────────────

class CircuitBreakerState:
    """
    Circuit breaker to prevent cascading LLM endpoint failures.

    When LLM endpoint fails repeatedly, the circuit opens and returns safe defaults
    without attempting new calls. Circuit resets automatically after timeout.

    This prevents retry loops from exhausting connection pools and timeouts during
    widespread LLM outages.

    Attributes:
        failure_threshold (int): Number of failures before circuit opens (default 3)
        timeout_seconds (int): Seconds before attempting reset (default 60)
        failures (int): Current failure count
        last_failure_time (Optional[datetime]): Timestamp of most recent failure
    """

    def __init__(self, failure_threshold: int = 3, timeout_seconds: int = 60):
        """
        Initialize circuit breaker state.

        Args:
            failure_threshold: Open circuit after N failures (default 3)
            timeout_seconds: Reset attempt after N seconds (default 60)
        """
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.failures = 0
        self.last_failure_time: Optional[datetime] = None
        # CLASS_REQUEST signatures already counted in the CURRENT window → sig: repeat_count.
        # Bounded (see _MAX_SIGNATURES): a pathological spread of signatures degrades to the
        # legacy counter rather than growing without limit.
        self.signatures: dict[str, int] = {}
        self.suppressed: int = 0   # observability: failures that were NOT independent evidence
        # Consecutive CONNECT-phase exhausted calls (see is_connect_phase_failure /
        # connect_trip_n): the dead-host trip short of the legacy threshold. NOT part of
        # the legacy counter — failures keeps its exact legacy semantics.
        self.consecutive_connect_failures: int = 0

    _MAX_SIGNATURES = 64

    def _prune_window(self) -> None:
        """Drop remembered signatures once this failure window has closed.

        The window is the breaker's own: it closes on a success/reset (``last_failure_time``
        is None) or once ``timeout_seconds`` have elapsed with no failure. ``self.failures``
        is deliberately NOT reset here — that would change legacy decay behaviour.
        """
        if self.last_failure_time is None:
            self.signatures.clear()
            return
        if (datetime.utcnow() - self.last_failure_time).total_seconds() >= self.timeout_seconds:
            self.signatures.clear()

    def is_open(self) -> bool:
        """
        Check if circuit is open (failing, should skip new requests).

        Circuit is open if:
        - failure count >= threshold, AND
        - timeout has not yet elapsed since last failure

        Returns:
            True if circuit is open (fail fast), False if closed (allow retries)
        """
        # CONNECT-PHASE TRIP (BUG-brain-connect-timeout-burns-full-budget-20261026):
        # consecutive failures where the host answered NOTHING at the TCP connect phase
        # open the circuit on their own, short of the legacy threshold — but under the
        # SAME reset-window discipline as the legacy counter below, so the half-open probe
        # that may discover recovery still fires (and now costs a connect budget, not a
        # read budget — see split_connect_timeout).
        _trip = connect_trip_n()
        if (_trip > 0
                and self.consecutive_connect_failures >= _trip
                and self.last_failure_time is not None
                and (datetime.utcnow() - self.last_failure_time).total_seconds() < self.timeout_seconds):
            return True

        if self.failures < self.failure_threshold:
            return False

        if self.last_failure_time is None:
            return False

        time_since_failure = datetime.utcnow() - self.last_failure_time
        if time_since_failure.total_seconds() >= self.timeout_seconds:
            # Timeout elapsed, attempt reset
            return False

        # Still in timeout window and failures threshold exceeded
        return True

    def record_failure(self, failure_class: str = CLASS_DEPENDENCY,
                       signature: Optional[str] = None,
                       connect_phase: bool = False):
        """Record a failure and update circuit state.

        Defaults reproduce the legacy counter exactly, so any caller that has not been
        taught the evidence model — and every call while ``LLM_BREAKER_EVIDENCE=false`` —
        behaves byte-for-byte as before.

        A CLASS_REQUEST failure whose signature was ALREADY seen in this window is recorded
        (timestamp, repeat count, log) but does NOT advance the trip counter: it is the same
        fact observed again, and the endpoint answering it proves the endpoint is up.

        ``connect_phase`` (BUG-brain-connect-timeout-burns-full-budget-20261026) marks a
        failure where the host never answered the TCP connect: it advances the
        consecutive-connect streak (which trips via ``is_open`` at ``connect_trip_n()``)
        and does NOT touch the legacy ``failures`` semantics. Any non-connect failure —
        including a read timeout, which proves the host ACCEPTED a connection — restarts
        the streak at zero.
        """
        self._prune_window()
        if connect_phase:
            self.consecutive_connect_failures += 1
        else:
            self.consecutive_connect_failures = 0

        counted = True
        if (breaker_evidence_enabled()
                and failure_class == CLASS_REQUEST
                and signature
                and len(self.signatures) < self._MAX_SIGNATURES):
            seen = self.signatures.get(signature)
            self.signatures[signature] = (seen or 0) + 1
            counted = seen is None

        if counted:
            self.failures += 1
        else:
            self.suppressed += 1

        self.last_failure_time = datetime.utcnow()

        if counted:
            log.warning("circuit_breaker.failure_recorded",
                        failures=self.failures,
                        threshold=self.failure_threshold,
                        failure_class=failure_class,
                        signature=signature,
                        connect_phase_streak=self.consecutive_connect_failures,
                        is_open=self.is_open())
        else:
            # Fowler: "Any change in breaker state should be logged and breakers should
            # reveal details of their state." A suppressed failure is a decision, so it is
            # visible — never a silent swallow.
            log.warning("circuit_breaker.failure_not_independent_evidence",
                        failures=self.failures,
                        threshold=self.failure_threshold,
                        failure_class=failure_class,
                        signature=signature,
                        repeat_count=self.signatures.get(signature, 0),
                        suppressed_total=self.suppressed,
                        note="the endpoint ANSWERED and rejected this request shape — that is "
                             "a verdict on our payload, not evidence the dependency is down. "
                             "Same signature already counted this window; not counted again.")

    def record_success(self):
        """Record a success and reset circuit state."""
        if self.failures > 0 or self.suppressed > 0:
            log.info("circuit_breaker.success_reset",
                    previous_failures=self.failures,
                    previous_suppressed=self.suppressed)
        self.failures = 0
        self.last_failure_time = None
        self.signatures.clear()
        self.suppressed = 0
        self.consecutive_connect_failures = 0

    def reset(self):
        """Force circuit closed (emergency recovery)."""
        log.warning("circuit_breaker.force_reset",
                   previous_failures=self.failures)
        self.failures = 0
        self.last_failure_time = None
        self.signatures.clear()
        self.suppressed = 0
        self.consecutive_connect_failures = 0


# ──────────────────────────────────────────────────────────────────────────────
# FUNCTION: Parse LLM Response Robustly (dBug-016 Handling)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_anthropic_response(data: dict) -> dict:
    """
    Parse Anthropic /v1/messages response format into a normalized dict.

    Anthropic returns: {"content": [{"type": "text", "text": "..."}], ...}
    This extracts the text and wraps it in a pseudo-choices structure so
    callers can use the same content extraction pattern.

    Args:
        data: Parsed JSON response dict from Anthropic API.

    Returns:
        dict with "choices" key for uniform downstream handling, or {} on failure.
    """
    content_blocks = data.get("content", [])
    if not content_blocks:
        log.warning("parse_anthropic_response.empty_content", data_keys=list(data.keys()))
        return {}
    text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
    text = "\n".join(text_parts).strip()
    if not text:
        log.warning("parse_anthropic_response.no_text_blocks", block_count=len(content_blocks))
        return {}
    # Normalize to OpenAI-compat shape so all callers work without changes
    return {"choices": [{"message": {"content": text}}]}


def _parse_llm_response_robust(response) -> dict:
    """
    Parse LLM HTTP response robustly, handling dBug-016 corruption.

    Dispatches to backend-specific parser based on LLM_BACKEND_TYPE:
    - anthropic: content[0].text (Anthropic /v1/messages format)
    - all others: choices[0].message.content (OpenAI-compat format)

    For OpenAI-compat backends, attempts three strategies:
    1. Standard JSON parsing (happy path)
    2. Line-delimited JSON (streaming format)
    3. Brace-counter extraction from corrupted JSON (dBug-016 case)

    Args:
        response: httpx.Response object from LLM endpoint

    Returns:
        dict: Parsed response with "choices" key, or {} on failure
    """
    from src.api.llm_client import get_backend_response_format

    # Anthropic dispatch: parse content[0].text before touching response.text
    if get_backend_response_format() == "anthropic":
        try:
            data = response.json()
            if isinstance(data, dict):
                return _parse_anthropic_response(data)
        except json.JSONDecodeError:
            log.warning("parse_llm_response.anthropic_json_decode_failed",
                       response_preview=response.text[:200])
        return {}

    # OpenAI-compat strategies (all non-Anthropic backends) — existing logic below
    try:
        # Strategy 1: Standard JSON parsing (most common)
        result = response.json()
        if isinstance(result, dict) and "choices" in result:
            return result
    except json.JSONDecodeError:
        log.debug("parse_llm_response.json_decode_failed")

    # Strategy 2: Line-delimited JSON (streaming format)
    # Some LLM endpoints return one JSON object per line
    try:
        lines = response.text.strip().split('\n')
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and "choices" in parsed:
                    log.debug("parse_llm_response.recovered_line_delimited")
                    return parsed
            except json.JSONDecodeError as e:
                log.warning("parse_llm_response.json_decode_failed", error=str(e), line_preview=line[:100])
                continue
    except Exception as e:
        log.error("parse_llm_response.line_delimited_parse_failed", error=str(e), response_preview=response.text[:200])

    # Strategy 3: Brace-counter extraction — handles arbitrary nesting depth.
    # Scans for the first '{' and its matching '}', validates against "choices" key.
    # No regex — works for any JSON regardless of nesting.
    try:
        text = response.text
        start = text.find('{')
        while start != -1:
            depth = 0
            end = start
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                candidate = text[start:end]
                parsed = json.loads(candidate)
                if isinstance(parsed, dict) and "choices" in parsed:
                    log.debug("parse_llm_response.recovered_brace_extraction")
                    return parsed
            except json.JSONDecodeError:
                pass
            start = text.find('{', end)
    except Exception as e:
        log.error("parse_llm_response.brace_extraction_failed", error=str(e), response_preview=response.text[:200])

    # All strategies failed
    log.warning("parse_llm_response.all_strategies_failed",
               response_text_preview=response.text[:300],
               response_status=response.status_code)
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# PER-TENANT LATENCY-PROFILE TIMEOUT FLOOR (SaaS increment 2)
# ──────────────────────────────────────────────────────────────────────────────
# Read by LLMTimeouts.get(). Total + cheap: an open-core deployment (no saas/ package)
# short-circuits permanently after the first ImportError; with SAAS_MODE off,
# current_llm_config() returns None with zero DB access. Any error → None → unchanged.
_PROFILE_SINK_MISSING = False


def _tenant_timeout_floor(operation: str) -> Optional[float]:
    """The active tenant brain's profile FLOOR (seconds) for `operation`, or None."""
    global _PROFILE_SINK_MISSING
    if _PROFILE_SINK_MISSING:
        return None
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
        if not cfg:
            return None
        from saas.brain_profile import profile_timeout
        return profile_timeout(cfg.get("latency_profile"), operation)
    except ImportError:
        _PROFILE_SINK_MISSING = True
        return None
    except Exception:  # noqa: BLE001 — never break a call over a config read
        return None


# ── BRAIN AUTO-POSTURE CEILING (SaaS, issue #229) — the ACTING half's binding seam ────
# Read by LLMTimeouts.get(). This is the ONE place a measured-degraded brain tightens an
# op's effective budget — no call site is ever touched (the constraint the wound taught:
# the manual fix was two env edits at 2am; the automatic one must be central or it will be
# missed by the next op class). Total + cheap: ImportError → permanent short-circuit (open
# core); SAAS off / no brain bound → None with zero DB access; ANY error → None → the static
# budget, byte-for-byte. Fail-open-first: a posture error can never tighten below the floor
# nor block a call — returning None here IS the fail-open posture.
_POSTURE_SINK_MISSING = False


def _posture_timeout_ceiling(operation: str) -> Optional[float]:
    """The auto-posture CEILING (seconds) for `operation` on the bound brain, or None.

    None = no opinion → the caller keeps its static budget. A value is returned ONLY while
    that (brain, op) is inside a measured tighten window (see saas/brain_posture.py), and is
    always >= the documented floor — `get()` applies it as a MINIMUM-ONLY cap: an operator's
    deliberately-lower budget is never raised by the posture either.
    """
    global _POSTURE_SINK_MISSING
    if _POSTURE_SINK_MISSING:
        return None
    try:
        from saas import brain_posture
        return brain_posture.timeout_ceiling(operation)
    except ImportError:
        # Open-core deployment with no saas/ package — remember, stop trying (hot path).
        _POSTURE_SINK_MISSING = True
        return None
    except Exception:  # noqa: BLE001 — never break a call over a posture read
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CLASS: LLMTimeouts
# ──────────────────────────────────────────────────────────────────────────────

class LLMTimeouts:
    """
    Centralized timeout configuration for different LLM operations.

    All timeout values are configurable via environment variables.
    Never hardcode timeout values in logic — always use this class.

    Operations:
    - INTENT_CLASSIFICATION: Fast intent detection (no context needed)
    - EXTRACTION: Entity/relationship extraction from conversation
    - VALIDATION: WGM ontology validation
    - ENRICHMENT: Metadata inference for novel rel_types
    - CORRECTION: User correction handling
    - EMBEDDING: Text embedding calls
    - DEFAULT: Fallback for unknown operations

    All values in seconds. Can be overridden via environment:
    - LLM_TIMEOUT_INTENT_CLASSIFICATION=5.0
    - LLM_TIMEOUT_EXTRACTION=30.0
    - etc.
    """

    # Default timeouts (in seconds) for each operation type
    # These are the fallbacks used if environment variables not set
    _DEFAULTS = {
        "INTENT_CLASSIFICATION": 5.0,    # Fast — no context needed
        "INTENT_ADJUDICATION": 4.0,      # Sub-gate binary CORRECTION/STATEMENT — fail open fast to STATEMENT
        "INTENT_PRECLASSIFY": 4.0,       # Confident-GLiNER2 DB-weighted correction/omission supersede — fail open fast
        "OCCURRENCE_CLASSIFY": 4.0,      # Dateful-but-hostless residue → "what occurred here?" verbatim-span classify — fail open fast
        "SPAN_DETECT": 6.0,              # Buried fact-bearing clause DETECTION — return verbatim spans that assert a user fact; fail open fast to deterministic-only span-finding
        "REFRAME": 6.0,                  # De-ramble/atomize one short turn before segmentation — cheap restructuring, fail open fast
        "EXTRACTION": 30.0,              # Standard extraction calls
        # "EXTRACT" is a REAL operation name used by the chunked relation extractor, but it was
        # absent from this map and silently resolved to DEFAULT. It happened to match (both
        # 30.0), so the bug was invisible — but the two would have diverged the moment anyone
        # tuned DEFAULT, and an operation whose timeout is set by accident is not configured.
        # Named explicitly so it resolves on purpose. (Same name pairing as LLMMaxTokens,
        # which already carries both EXTRACT and EXTRACTION.)
        "EXTRACT": 30.0,                 # Chunked relation extraction (per sub-chunk)
        "VALIDATION": 20.0,              # WGM ontology validation
        "ENRICHMENT": 15.0,              # Metadata inference for new rel_types
        "CLASSIFY_CHAIN": 20.0,          # One-shot FULL is-a ladder for one concept — a few rungs of JSON, longer than a single ENRICHMENT rung
        "CORRECTION": 25.0,              # User correction extraction
        "CORRECTION_EXTRACTION": 60.0,  # Correction LLM re-extraction (main.py `operation="correction_extraction"`).
                                        # ⚠️ THIS KEY WAS ABSENT AND THE OPERATION SILENTLY TOOK DEFAULT=30.0 — the
                                        # THIRD instance of the bug class already documented for "EXTRACT" above
                                        # ("an operation whose timeout is set by accident is not configured").
                                        # It is the same shape of work as RETRACTION_EXTRACTION below — a
                                        # server-side LLM re-extraction that loads context — so it gets the same
                                        # 60.0, not the generic fallback. Measured consequence of the 30.0: a
                                        # correction-heavy arm returned 22/85 brain-degraded runs where the
                                        # identical arm with a raised budget returned 0/85, which silently
                                        # DEFLATED that arm's corruption counts (a timed-out brain writes
                                        # nothing, so the broken arm scores CLEANER). Note the sibling
                                        # "CORRECTION": 25.0 is a DIFFERENT key that this call site never hits.
        "RETRACTION_EXTRACTION": 60.0,   # Retraction LLM extraction — longer due to context loading
        "EMBEDDING": 10.0,               # Text embedding operations
        "TAXONOMY_DISCOVERY": 20.0,      # Discover new taxonomies
        "NATURAL_LANGUAGE_FILL": 10.0,   # Ontology phrasing (3p+2p) for a grown rel_type — short single-rel JSON
        "NATURAL_LANGUAGE_FILL_BATCH": 20.0,  # ONE call phrasing every grown rel_type minted in a single ingest turn (replaces N serial NATURAL_LANGUAGE_FILL round-trips on the shared LLM slot)
        "DEFAULT": 30.0,                 # Fallback for unknown operations
    }

    @classmethod
    def static(cls, operation: str = "DEFAULT") -> float:
        """The op's budget WITHOUT any posture modulation: env → default → profile floor.

        This is exactly what ``get()`` resolved before the auto-posture existed — the budget
        the measured calls actually ran with, and therefore the baseline the posture's p95
        trigger is evaluated against (see saas/brain_posture.py for why comparing against the
        POSTURED budget would be a feedback loop with no recovery). Same failure modes as the
        old ``get()``: env parse error → default; profile read error → ignored.
        """
        operation = operation.upper()
        env_var = f"LLM_TIMEOUT_{operation}"
        env_value = os.environ.get(env_var)

        resolved = None
        if env_value is not None:
            try:
                resolved = float(env_value)
            except ValueError:
                log.warning("llm_timeout.invalid_env_value",
                           env_var=env_var,
                           value=env_value,
                           using_default=True)
        if resolved is None:
            resolved = cls._DEFAULTS.get(operation, cls._DEFAULTS["DEFAULT"])

        # ── Per-tenant BYO-brain LATENCY PROFILE (SaaS) — the binding seam ────────────
        # This is where a calibrated profile actually TAKES EFFECT. A profile supplies a
        # per-operation FLOOR; the effective budget is max(env-or-default, floor), so:
        #   * SAAS off / no profile / any error → `resolved`, byte-for-byte unchanged;
        #   * 'hosted-fast' declares no floors → also unchanged;
        #   * an operator's explicit LLM_TIMEOUT_<OP> is never narrowed by a profile;
        #   * a mis-set profile can only make the engine WAIT LONGER, never give up sooner.
        # Root cause it exists for: one global 6.0s REFRAME budget, tuned for a hosted API,
        # guillotined a self-hosted brain and silently degraded capture (2026-07-31).
        floor = _tenant_timeout_floor(operation)
        if floor is not None and floor > resolved:
            return floor
        return resolved

    @classmethod
    def get(cls, operation: str = "DEFAULT") -> float:
        """
        Get timeout for a specific operation.

        The op's STATIC budget (env → default → calibrated profile floor — see ``static()``),
        then the BRAIN AUTO-POSTURE CEILING (#229): while the bound brain's measured recent
        p95 for this op-class is over its budget, the effective value is capped at the
        fail-open floor for a cooldown window. The ceiling applies as a MINIMUM-ONLY cap:

          * no posture / kill switch off / any error  → static, byte-for-byte;
          * static already at/below the floor          → static (the posture never tightens
            below the documented floor and never RAISES a deliberate operator budget);
          * tightened                                  → the floor (deterministic fail-open).

        This is the ONLY seam that modulates timeouts — call sites never change (the wound's
        manual fix was two env edits; the automatic one must be central or the next op class
        misses it).

        Args:
            operation: Operation type (e.g., "EXTRACTION", "VALIDATION")
                      Case-insensitive

        Returns:
            Timeout in seconds (float)
        """
        resolved = cls.static(operation)
        ceiling = _posture_timeout_ceiling(operation)
        if ceiling is not None and ceiling < resolved:
            return ceiling
        return resolved


# ──────────────────────────────────────────────────────────────────────────────
# CLASS: LLMMaxTokens
# ──────────────────────────────────────────────────────────────────────────────

class LLMMaxTokens:
    """
    Centralized max_tokens (completion budget) configuration for LLM operations.

    Mirrors LLMTimeouts. All values are configurable via environment variables.
    Never hardcode max_tokens values in logic — always use this class.

    Why EXTRACT/EXTRACTION default to 2048:
    The 2048 budget was deliberately chosen (e0e78c0 / dprompt-139) so that
    qwen3.5-9b's extraction output is not truncated mid-array — dense passages
    yield many triples and a 500-token cap silently cuts the JSON, collapsing
    rich passages to ~1 triple. Regression bf699ae dropped this back to the
    500 default, re-introducing the truncation. This class restores 2048 for
    extraction while leaving every other operation at the safe 500 default.

    Operations:
    - EXTRACT / EXTRACTION: Entity/relationship extraction — needs large budget
    - DEFAULT: Fallback for all other operations (unchanged 500)

    Override via environment, e.g.:
    - LLM_MAX_TOKENS_EXTRACT=2048
    - LLM_MAX_TOKENS_EXTRACTION=2048
    """

    # Default completion budgets (in tokens) for each operation type.
    # Only EXTRACT/EXTRACTION deviate from the safe 500 default — do NOT
    # regress other operations by raising their budgets here.
    _DEFAULTS = {
        "EXTRACT": 2048,      # Dense triple extraction — must not truncate JSON array
        "EXTRACTION": 2048,   # Alias of EXTRACT for callers using the long form
        "INTENT_ADJUDICATION": 16,  # Tiny JSON route answer e.g. {"route":"STATEMENT"} — keep latency/cost bounded
        "INTENT_PRECLASSIFY": 32,   # JSON route+confidence e.g. {"route":"CORRECTION","confidence":0.93} — bounded
        "OCCURRENCE_CLASSIFY": 48,  # Tiny JSON {"occurrence":"<verbatim noun phrase>"} | {"occurrence":"none"} — verbatim span only, NOT a triple array
        "SPAN_DETECT": 384,         # JSON list of VERBATIM fact-bearing sentences copied from the turn (segmentation/flagging, NOT triples) — a few clauses, never a dense triple array
        "CLASSIFY_CHAIN": 256,      # One-shot FULL is-a ladder: short JSON {entity_type, chain:[...]} of snake_case tokens up to a general root — bigger than a single rung, far smaller than dense EXTRACT
        "REFRAME": 256,             # Short JSON list of short atomic statements — restructuring, NOT dense triple generation; do NOT reuse 2048 EXTRACT budget
        "NATURAL_LANGUAGE_FILL": 200,  # Small JSON {natural_language, natural_language_2p} for ONE rel_type
        "NATURAL_LANGUAGE_FILL_BATCH": 900,  # JSON MAP {rel_type: {natural_language, natural_language_2p}} for EVERY novel rel in one turn — a handful of short rows, never a dense triple array
        "DEFAULT": 500,       # Safe fallback for every other operation
    }

    @classmethod
    def get(cls, operation: str = "DEFAULT") -> int:
        """
        Get max_tokens budget for a specific operation.

        Checks environment variables first (LLM_MAX_TOKENS_<OPERATION>),
        then falls back to defaults.

        Args:
            operation: Operation type (e.g., "EXTRACT", "EXTRACTION")
                      Case-insensitive

        Returns:
            max_tokens budget (int)
        """
        operation = operation.upper()
        env_var = f"LLM_MAX_TOKENS_{operation}"
        env_value = os.environ.get(env_var)

        if env_value is not None:
            try:
                return int(env_value)
            except ValueError:
                log.warning("llm_max_tokens.invalid_env_value",
                           env_var=env_var,
                           value=env_value,
                           using_default=True)

        return cls._DEFAULTS.get(operation, cls._DEFAULTS["DEFAULT"])


# ──────────────────────────────────────────────────────────────────────────────
# REASONING TOKEN HEADROOM (dprompt-156) — keep capture intact when reasoning can't be off
# ──────────────────────────────────────────────────────────────────────────────
# A model that cannot disable reasoning (resolve_reasoning_controls(...)["can_disable"] is
# False) spends reasoning tokens INSIDE the completion budget — measured live on Cerebras
# gpt-oss-120b: usage.completion_tokens = 49 reasoning + 55 content, i.e. reasoning counts
# against max_tokens. On a long fact-dense turn that truncates the extraction JSON to empty
# ("REFRAME:empty_llm_content"). Adding a headroom lets the atoms complete. Env-overridable
# (constraint 6 — never hardcode a max_tokens budget); 0 disables the padding.

def _reasoning_token_headroom() -> int:
    """Reasoning-token headroom added when the resolved model cannot disable reasoning.

    Single source of truth, read live from env (LLM_REASONING_TOKEN_HEADROOM). Default 1024 —
    sized above the measured ~49-token short-turn overhead so a long multi-fact turn has room
    for its CoT without ballooning the bill on a short turn. 0 disables.
    """
    try:
        v = int(os.environ.get("LLM_REASONING_TOKEN_HEADROOM", "1024"))
        return v if v >= 0 else 1024
    except (ValueError, TypeError):
        return 1024


def _apply_reasoning_headroom(model: str, max_tokens: int) -> int:
    """Add reasoning headroom to the completion budget when the resolved brain's model cannot
    disable reasoning.

    Gates on ``inflate_budget`` — the flag whose NAME is this contract ("the completion budget
    must be padded"). It is derived from ``can_disable`` (inflate_budget = not can_disable) so
    the two agree today, but the CONSUMER reads the flag that names the behaviour: a future
    entry that needs padding without a can_disable=False verdict (or vice versa) must not have
    to hunt for a private coincidence two modules away.

    Pure and fail-safe (any error → unchanged budget). Computed ONCE per logical call, AFTER the
    tenant-brain override resolves the real model and BEFORE _context_preflight, so the window
    check sees the full budget and can degrade honestly rather than silently re-shrinking it.
    """
    headroom = _reasoning_token_headroom()
    if headroom <= 0:
        return max_tokens
    try:
        from src.api.llm_client import resolve_reasoning_controls, _reasoning_base_url
        controls = resolve_reasoning_controls(_reasoning_base_url(), model)
    except Exception:
        return max_tokens
    if controls.get("inflate_budget") is True:
        return max_tokens + headroom
    return max_tokens


def _reasoning_expected(model: str) -> bool:
    """True when the resolved model cannot disable reasoning (reasoning tokens are expected).

    Pure and fail-safe. Used to mark the reasoning-leak audit's expected=True case so a
    cannot-disable model's reasoning tokens are logged at info, never the suppression_ineffective
    CRIT (that lane is for a field we SENT being ignored).
    """
    try:
        from src.api.llm_client import resolve_reasoning_controls, _reasoning_base_url
        return resolve_reasoning_controls(_reasoning_base_url(), model).get("can_disable") is False
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# CLASS: LLMModels
# ──────────────────────────────────────────────────────────────────────────────

class LLMModels:
    """
    Centralized MODEL-NAME resolution for LLM operations.

    Mirrors LLMTimeouts / LLMMaxTokens EXACTLY: a per-operation map points each
    operation at the ENV VAR that supplies its model name. Model identity is pure
    configuration — it lives ONLY in the environment (.env), NEVER as a literal in
    code. There is intentionally NO hardcoded model-name fallback: an unset model
    var is a LOUD config error (log_crit + raise), never a silent guess.

    This is the model analogue of the timeout/max_tokens resolvers. Before this
    class, ~21 call sites each hardcoded the model env read with an inline
    fallback literal, and those literals DISAGREED (most pointed at one model, a
    stray few at another, for the SAME env var) — so the same operation could
    silently split across two models if the var was unset. One resolver → one
    answer per operation.

    Resolution order for an operation:
      1. Per-op override env  LLM_MODEL_<OPERATION>   (parallel to LLM_TIMEOUT_<OP>)
      2. The operation's mapped base env var (_OP_ENV_VAR, default WGM_LLM_MODEL)
      3. If neither is set → log_crit + raise RuntimeError (fail loud, no literal)

    Env vars (VALUES live in .env, documented in .env.example):
    - WGM_LLM_MODEL          : base model for all standard operations
    - PATTERN_EXTRACTION_MODEL: model for the deterministic pattern-extraction op
                               (formerly the misnamed CATEGORY_LLM_MODEL); falls
                               back to WGM_LLM_MODEL when its own var is unset.
    - LLM_MODEL_<OPERATION>  : optional per-operation override
    """

    # Base env var every operation reads unless mapped otherwise.
    _BASE_ENV_VAR = "WGM_LLM_MODEL"

    # Operations that read a DIFFERENT base env var than WGM_LLM_MODEL.
    # Only the pattern-extraction op is distinct; it still falls back to the base
    # var when its own var is unset (so a single WGM_LLM_MODEL drives everything).
    _OP_ENV_VAR = {
        "PATTERN_EXTRACTION": "PATTERN_EXTRACTION_MODEL",
    }

    @classmethod
    def get(cls, operation: str = "DEFAULT") -> str:
        """
        Resolve the model name for a specific operation.

        Checks the per-op override (LLM_MODEL_<OPERATION>) first, then the
        operation's mapped base env var (default WGM_LLM_MODEL, with a final
        fallback to WGM_LLM_MODEL for ops that map to their own var). If nothing
        resolves to a non-empty value → log_crit + raise (fail loud, NO literal).

        Args:
            operation: Operation type (e.g., "EXTRACTION", "PATTERN_EXTRACTION").
                      Case-insensitive.

        Returns:
            Model name string from the environment.

        Raises:
            RuntimeError: if no model env var is set for the operation.
        """
        operation = operation.upper()

        # 1. Per-operation override.
        override = os.environ.get(f"LLM_MODEL_{operation}")
        if override and override.strip():
            return override.strip()

        # 2. Mapped base env var (op-specific var, then WGM_LLM_MODEL).
        primary_var = cls._OP_ENV_VAR.get(operation, cls._BASE_ENV_VAR)
        value = os.environ.get(primary_var)
        if (not value or not value.strip()) and primary_var != cls._BASE_ENV_VAR:
            # Op-specific var unset → fall back to the base model var.
            value = os.environ.get(cls._BASE_ENV_VAR)
        if value and value.strip():
            return value.strip()

        # 3. Fail loud — model identity is config; an unset model is never guessed.
        from src.api.logging_config import log_crit
        log_crit(
            log,
            "llm_model.unresolved",
            operation=operation,
            checked_env_vars=[f"LLM_MODEL_{operation}", primary_var,
                              cls._BASE_ENV_VAR],
            remediation="set WGM_LLM_MODEL (and/or the per-op LLM_MODEL_<OP> / "
                        "PATTERN_EXTRACTION_MODEL) in the environment",
        )
        raise RuntimeError(
            f"LLMModels.get({operation!r}): no model env var set — refusing to "
            f"guess a model name. Set WGM_LLM_MODEL in the environment."
        )


# ──────────────────────────────────────────────────────────────────────────────
# SHARED HELPER: generate_rel_type_phrasing()
# ──────────────────────────────────────────────────────────────────────────────

def generate_rel_type_phrasing(rel_type: str,
                               label: Optional[str] = None,
                               user_id: str = "engine") -> dict:
    """Generate 3p + 2p natural-language render templates for a grown rel_type.

    SINGLE SOURCE OF TRUTH for ontology render-phrasing generation. Both the
    ingest orphan-stub mint (strong-ingest, in-flow) and the re_embedder Job 7
    backfill (async safety-net) call THIS function so the prompt + validation
    live in exactly one place (transport-parity — no divergent copies).

    This is purely an LLM ontology-phrasing task (call_llm_with_retry_sync,
    operation NATURAL_LANGUAGE_FILL). It NEVER touches GLiNER2 and never inspects
    or alters any GLiNER2 label — it only asks the centralized LLM for human
    render strings for an already-decided rel_type.

    Returns a dict with whichever of these keys validated:
      {"natural_language": "...", "natural_language_2p": "..."}
    Empty / missing keys mean "could not generate cleanly" — callers MUST treat
    that as fail-safe (keep the NULL-template stub; backfill later). This helper
    NEVER raises; on any error it returns {} so ingest is never blocked.

    Placeholder contract (enforced here, same as the original Job 7 guard):
      * natural_language: third-person, MUST contain X (subject placeholder).
      * natural_language_2p: second-person, subject baked as "you"/"your",
        MUST contain Y and MUST NOT contain X.
    A value failing its contract is dropped (key omitted) — never returned broken.
    """
    rt = (rel_type or "").strip()
    if not rt:
        return {}

    try:
        messages = [
            {"role": "system", "content":
             "You are an ontology expert. Respond with ONLY a JSON object, no markdown."},
            {"role": "user", "content":
             f'Generate human-readable phrases for the relationship type "{rt}".\n'
             f'1. "natural_language": third-person, use X for subject and Y for object.\n'
             f'   Example: "parent_of" → "X is the parent of Y", "has_ip" → "X has IP address Y", "spouse" → "X and Y are spouses"\n'
             f'2. "natural_language_2p": SECOND-PERSON, subject baked in as "you"/"your", keep ONLY Y for the object. MUST contain Y, MUST NOT contain X.\n'
             f'   Example: "parent_of" → "You are the parent of Y", "has_ip" → "You have IP address Y", "spouse" → "You and Y are spouses"\n'
             f'Respond with ONLY: {{"natural_language": "...", "natural_language_2p": "..."}}'},
        ]
        result = call_llm_with_retry_sync(
            messages=messages,
            model=LLMModels.get("NATURAL_LANGUAGE_FILL"),
            user_id=user_id,
            operation="NATURAL_LANGUAGE_FILL",
        )
    except Exception as e:
        # Fail-safe: phrasing generation must NEVER block/abort the caller.
        log.warning("rel_type_phrasing.generation_failed",
                    rel_type=rt, error=str(e)[:200])
        return {}

    nl = (result.get("natural_language") or "").strip() if result else ""
    nl_2p = (result.get("natural_language_2p") or "").strip() if result else ""

    out: dict = {}
    # natural_language MUST contain the X subject placeholder — never store broken.
    if nl:
        if "X" in nl:
            out["natural_language"] = nl
        else:
            log.warning("rel_type_phrasing.natural_language_missing_placeholder",
                        rel_type=rt, generated_value=nl,
                        reason="LLM response missing X subject placeholder — dropping")
    # 2p must keep Y and must NOT reintroduce X.
    if nl_2p:
        if "Y" in nl_2p and "X" not in nl_2p:
            out["natural_language_2p"] = nl_2p
        else:
            log.warning("rel_type_phrasing.natural_language_2p_invalid",
                        rel_type=rt, generated_value=nl_2p,
                        reason="2p missing Y or contains X — dropping")
    return out


def _validate_rel_phrasing_pair(rt: str, nl: str, nl_2p: str) -> dict:
    """Apply the SAME placeholder contract generate_rel_type_phrasing enforces to ONE
    (natural_language, natural_language_2p) pair. Factored out so the single-rel and the
    batch generators validate byte-identically (no divergent copy). Returns whichever key
    validated; a broken value is dropped (never stored)."""
    out: dict = {}
    nl = (nl or "").strip()
    nl_2p = (nl_2p or "").strip()
    if nl:
        if "X" in nl:
            out["natural_language"] = nl
        else:
            log.warning("rel_type_phrasing.natural_language_missing_placeholder",
                        rel_type=rt, generated_value=nl,
                        reason="LLM response missing X subject placeholder — dropping")
    if nl_2p:
        if "Y" in nl_2p and "X" not in nl_2p:
            out["natural_language_2p"] = nl_2p
        else:
            log.warning("rel_type_phrasing.natural_language_2p_invalid",
                        rel_type=rt, generated_value=nl_2p,
                        reason="2p missing Y or contains X — dropping")
    return out


def generate_rel_type_phrasing_batch(rel_types: list,
                                     user_id: str = "engine") -> dict:
    """Phrase EVERY grown rel_type minted in one ingest turn in a SINGLE LLM round-trip.

    SPEED / SERIALIZATION FIX (owner: "LLM only where needed; stop N serial round-trips on
    the one shared LLM slot"). The per-rel ``generate_rel_type_phrasing`` fires ONE blocking
    NATURAL_LANGUAGE_FILL call per novel rel_type at ingest — a dense turn that mints
    fly_to / tour / purchase / present / present_at pays FIVE serial calls for what is one
    tiny ontology-phrasing task. This asks for all of them in ONE JSON map, so a turn's novel
    rels cost ONE call instead of N. The DECISION (which rels are novel) is unchanged and
    deterministic; only the number of LLM round-trips drops. Phrasing output is byte-identical
    per rel (same prompt shape, same placeholder contract) → ZERO capture / render change.

    Returns ``{rel_type: {natural_language?, natural_language_2p?}}`` for whichever rels/keys
    validated. A rel absent from the result (LLM omitted it / failed its contract) is simply
    not cached — the caller falls back to the per-rel generator / the re_embedder Job-7 async
    backfill, exactly as today. NEVER raises; any error → ``{}`` (caller keeps today's serial
    path). Purely an LLM ontology-phrasing task — it NEVER touches GLiNER2.
    """
    rts = []
    _seen_rt: set = set()
    for r in (rel_types or []):
        r = (r or "").strip().lower()
        if r and r not in _seen_rt:
            _seen_rt.add(r)
            rts.append(r)
    if not rts:
        return {}
    # A single novel rel is cheaper via the single-rel path (smaller prompt/budget) — the
    # batch is only worth a round-trip when there are >=2 rels to phrase.
    if len(rts) == 1:
        one = generate_rel_type_phrasing(rts[0], user_id=user_id)
        return {rts[0]: one} if one else {}

    _rt_lines = "\n".join(f'  - "{r}"' for r in rts)
    try:
        messages = [
            {"role": "system", "content":
             "You are an ontology expert. Respond with ONLY a JSON object, no markdown."},
            {"role": "user", "content":
             f'Generate human-readable phrases for EACH of these relationship types:\n'
             f'{_rt_lines}\n\n'
             f'For every relationship type produce TWO phrasings:\n'
             f'1. "natural_language": third-person, use X for subject and Y for object.\n'
             f'   Example: "parent_of" -> "X is the parent of Y", "has_ip" -> "X has IP address Y".\n'
             f'2. "natural_language_2p": SECOND-PERSON, subject baked in as "you"/"your", keep ONLY Y for the object. MUST contain Y, MUST NOT contain X.\n'
             f'   Example: "parent_of" -> "You are the parent of Y", "has_ip" -> "You have IP address Y".\n\n'
             f'Respond with ONLY a JSON object mapping each relationship type to its phrasings, e.g.:\n'
             f'{{"parent_of": {{"natural_language": "X is the parent of Y", "natural_language_2p": "You are the parent of Y"}}, ...}}'},
        ]
        result = call_llm_with_retry_sync(
            messages=messages,
            model=LLMModels.get("NATURAL_LANGUAGE_FILL_BATCH"),
            user_id=user_id,
            operation="NATURAL_LANGUAGE_FILL_BATCH",
        )
    except Exception as e:
        log.warning("rel_type_phrasing.batch_generation_failed",
                    rel_count=len(rts), error=str(e)[:200])
        return {}
    if not isinstance(result, dict):
        return {}
    out: dict = {}
    for r in rts:
        row = result.get(r)
        if not isinstance(row, dict):
            continue
        validated = _validate_rel_phrasing_pair(
            r, row.get("natural_language") or "", row.get("natural_language_2p") or "")
        if validated:
            out[r] = validated
    log.info("rel_type_phrasing.batch_generated",
             requested=len(rts), phrased=len(out))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# GLOBALS
# ──────────────────────────────────────────────────────────────────────────────

# Global circuit breaker instance (shared across all LLM calls)
_llm_circuit_breaker = CircuitBreakerState(
    failure_threshold=int(os.environ.get("LLM_CIRCUIT_BREAKER_THRESHOLD", "5")),
    timeout_seconds=int(os.environ.get("LLM_CIRCUIT_BREAKER_TIMEOUT", "30")),
)

# ── Per-tenant circuit breakers (SaaS BYO-LLM) ──────────────────────────────────
# The breaker above is process-GLOBAL: one tenant's BYO endpoint outage would trip it
# for EVERYONE. In SaaS mode we key a breaker per account_id so a bad tenant endpoint
# fails fast for ONLY that tenant. account_id None → the shared global breaker (the
# SAAS-off path is byte-for-byte unchanged — same object, same behavior).
_tenant_circuit_breakers: dict[str, CircuitBreakerState] = {}


def _breaker_for(account_id: Optional[str]) -> CircuitBreakerState:
    """Return the circuit breaker for an account (or the global one when account_id is None)."""
    if not account_id:
        return _llm_circuit_breaker
    breaker = _tenant_circuit_breakers.get(account_id)
    if breaker is None:
        breaker = CircuitBreakerState(
            failure_threshold=int(os.environ.get("LLM_CIRCUIT_BREAKER_THRESHOLD", "5")),
            timeout_seconds=int(os.environ.get("LLM_CIRCUIT_BREAKER_TIMEOUT", "30")),
        )
        _tenant_circuit_breakers[account_id] = breaker
    return breaker


# ── Cross-process breaker state (OPTIONAL, off by default) ──────────────────────
# Today every API worker holds its OWN opinion about the same brain: worker A opening its
# breaker protects nobody else, and the recovery probe fires once per worker at an endpoint
# that is already struggling. `/health` reports only the process that answered, so the
# health surface is not truthful either.
#
# The shared view is an OVERLAY, deliberately ONE-WAY: it can only ADD an open verdict the
# local breaker did not have. It can never force a locally-open breaker closed, and it can
# never suppress a local failure count. So the worst case for a stale/wrong shared value is
# "we fail fast for up to `timeout_seconds`" — the same thing a local trip already does.
# Redis absent/down/disabled → both helpers no-op and behaviour is exactly in-process.

def _breaker_is_open(breaker: "CircuitBreakerState") -> bool:
    """Local verdict OR the shared verdict. Never raises."""
    if breaker.is_open():
        return True
    try:
        from src.api import redis_coord
        if not redis_coord.breaker_enabled():
            return False
        scope = _breaker_scope()
        if scope is None:
            return False
        until = redis_coord.shared_open_until(scope)
        if until is None:
            return False
        # Adopt the shared verdict locally so `time_until_reset` and `/health` agree with it.
        breaker.last_failure_time = datetime.utcnow()
        breaker.failures = max(breaker.failures, breaker.failure_threshold)
        log.warning("circuit_breaker.adopted_shared_open",
                    seconds_remaining=max(1, int(until - time.time())),
                    note="another process reported this brain out; failing fast here too")
        return True
    except Exception:  # noqa: BLE001 — coordination must never break a call
        return False


def _publish_breaker_state(breaker: "CircuitBreakerState") -> None:
    """Tell the other processes when THIS one trips. Best-effort, never raises."""
    try:
        from src.api import redis_coord
        if not redis_coord.breaker_enabled() or not breaker.is_open():
            return
        scope = _breaker_scope()
        if scope is not None:
            redis_coord.publish_open(scope, float(breaker.timeout_seconds))
    except Exception:  # noqa: BLE001
        pass


def _clear_shared_breaker() -> None:
    """A success anywhere clears the shared verdict — recovery must not wait out the TTL."""
    try:
        from src.api import redis_coord
        if not redis_coord.breaker_enabled():
            return
        scope = _breaker_scope()
        if scope is not None:
            redis_coord.clear_shared_open(scope)
    except Exception:  # noqa: BLE001
        pass


# ──────────────────────────────────────────────────────────────────────────────
# LLM CONCURRENCY CEILING — one seam, two layers (process, then fleet)
# ──────────────────────────────────────────────────────────────────────────────
# MEASURED DEFECT: `_EXTRACT_SEMAPHORE = asyncio.Semaphore(8)` is constructed INSIDE
# `extract_rewrite()` (main.py), so it bounds ONE request to 8 in-flight calls and bounds N
# concurrent requests to **8N** — there is no global cap at all, against a llama.cpp server
# with 4 slots. Measured: 11 in-flight on one lightly-loaded seat; 316 ConnectTimeouts at
# 3 ms host-level connect latency.
#
# That matters here and not only in main.py: a ConnectTimeout is CLASS_DEPENDENCY, counted
# in full. So self-inflicted over-subscription is precisely the thing that opens the breaker
# for every seat on the account. Capping concurrency is not a throughput tweak — it removes
# a manufactured source of "the dependency is unavailable" evidence.
#
# LAYER 1 (no Redis, no infra): a module-level semaphore — per PROCESS instead of per
#   REQUEST. Turns 8N into 8. This alone fixes the unbounded term.
# LAYER 2 (optional, Redis): a leased slot shared across processes, so N workers cannot each
#   run their own 8 at a 4-slot server.
#
# Both are FAIL-OPEN and both are OFF by default: unset flags → this is a bare `yield` and
# the caller's own semaphore is the only bound, byte-for-byte today.

from contextlib import asynccontextmanager as _asynccontextmanager

_proc_llm_semaphore: Optional[asyncio.Semaphore] = None
_proc_llm_sem_limit: int = 0
_proc_llm_sem_lock = threading.Lock()


def process_llm_concurrency_limit() -> int:
    """Per-PROCESS in-flight LLM ceiling. 0/unset → no process-level cap (today's behaviour)."""
    try:
        return max(0, int(os.environ.get("LLM_PROCESS_CONCURRENCY", "0")))
    except (TypeError, ValueError):
        return 0


def _get_proc_semaphore() -> Optional[asyncio.Semaphore]:
    global _proc_llm_semaphore, _proc_llm_sem_limit
    limit = process_llm_concurrency_limit()
    if limit <= 0:
        return None
    with _proc_llm_sem_lock:
        if _proc_llm_semaphore is None or _proc_llm_sem_limit != limit:
            _proc_llm_semaphore = asyncio.Semaphore(limit)
            _proc_llm_sem_limit = limit
        return _proc_llm_semaphore


def reset_llm_concurrency() -> None:
    """Test/diagnostic hook — rebuild the process semaphore so a new limit takes effect."""
    global _proc_llm_semaphore, _proc_llm_sem_limit
    with _proc_llm_sem_lock:
        _proc_llm_semaphore = None
        _proc_llm_sem_limit = 0


def _fleet_slot_max_wait() -> float:
    try:
        return float(os.environ.get("LLM_GLOBAL_CONCURRENCY_MAX_WAIT", "30"))
    except (TypeError, ValueError):
        return 30.0


@_asynccontextmanager
async def llm_concurrency_slot(operation: str = "DEFAULT"):
    """Hold one in-flight LLM slot for the duration of the block.

    Wrap any parallel fan-out of LLM calls in this instead of a locally-constructed
    ``asyncio.Semaphore``. Never raises, never refuses: if the fleet ceiling cannot be
    obtained within ``LLM_GLOBAL_CONCURRENCY_MAX_WAIT`` it PROCEEDS (fail-open) — dropping a
    call to protect a throughput number would be losing user data to save a metric.
    """
    sem = _get_proc_semaphore()
    if sem is not None:
        await sem.acquire()
    holder = None
    scope = None
    try:
        try:
            from src.api import redis_coord
            if redis_coord.semaphore_enabled():
                scope = _breaker_scope()
                if scope is not None:
                    holder = f"{os.getpid()}:{operation}:{time.time_ns()}"
                    deadline = time.time() + _fleet_slot_max_wait()
                    delay = 0.02
                    while True:
                        # Redis I/O off the event loop: a wedged Redis must never stall it.
                        got = await asyncio.to_thread(
                            redis_coord.try_acquire_slot, scope, holder)
                        if got or time.time() >= deadline:
                            if not got:
                                log.warning("llm_concurrency.fleet_slot_wait_exhausted",
                                            operation=operation,
                                            waited_seconds=_fleet_slot_max_wait(),
                                            note="proceeding WITHOUT a fleet slot — "
                                                 "fail-open: never lose a call to the ceiling")
                                holder = None
                            break
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 0.5)
        except Exception:  # noqa: BLE001 — coordination must never break a call
            holder = None
        yield
    finally:
        if holder is not None and scope is not None:
            try:
                from src.api import redis_coord
                await asyncio.to_thread(redis_coord.release_slot, scope, holder)
            except Exception:  # noqa: BLE001
                pass
        if sem is not None:
            sem.release()


# ──────────────────────────────────────────────────────────────────────────────
# RESPECTFUL ACCOUNT-WIDE CLIENT-SIDE RATE LIMITER (outbound LLM pacing)
# ──────────────────────────────────────────────────────────────────────────────
#
# WHY: a metered 3rd-party LLM API (e.g. the bench brain, dashscope qwen-turbo:
# 600 RPM / 5M TPM PER ALIBABA-CLOUD-ACCOUNT on the Singapore endpoint) is SHARED
# by every seat under that account. Bursting past the published limit triggers the
# provider's burst-protection — which escalated to DROPPING our connections and
# cost two days. This limiter PACES (never rejects) outbound calls so we stay
# comfortably under the published per-account rate.
#
# NOT to be confused with saas/ratelimit.py — that is the Redis-backed INBOUND
# request reject-limiter on the MCP door. THIS is OUTBOUND LLM-call pacing that
# BLOCKS the caller briefly so extraction is never dropped, only smoothed.
#
# BUCKET KEY CHOICE (documented, per the spec):
#   * SAAS_MODE + resolved tenant config with an account_id → key = "account:<id>".
#     This is exactly right: Alibaba (and every per-account-metered provider) meters
#     PER ACCOUNT, so ALL seats of one account correctly SHARE ONE bucket, and one
#     tenant's load can NEVER throttle another (independent buckets per account).
#   * tenant config with a base_url but no account_id → key = "url:<base_url>".
#   * no tenant config (SAAS off / single-tenant / homelab) → the resolved global
#     endpoint: LLM_BASE_URL, else the first legacy endpoint → key = "url:<base_url>".
#   * nothing resolvable → key = None → the gate is a NO-OP (fail-safe: never lose a
#     call, never pace on an unknown key).
#
# CONCURRENCY: one _TokenBucket per key. The token arithmetic runs under a tiny
# threading.Lock held for microseconds (never across a sleep), so the SAME bucket is
# consistent whether acquired from the sync path or an async task — async callers
# sleep with asyncio.sleep OUTSIDE the lock. time.monotonic() is used deliberately:
# this is runtime PACING, not extraction logic, so a monotonic wall-independent clock
# is correct here (and immune to system-clock jumps).
#
# ENV KNOBS (all conservative defaults; disabled or no-key → byte-for-byte current path):
#   LLM_RATE_LIMIT_ENABLED  default "1"/on. OFF → pure no-op.
#   LLM_MAX_RPM             default 480 (= 8 tokens/sec, comfortably under 600 RPM).
#   LLM_RATE_BURST          burst capacity (tokens). Default = 1s worth of tokens
#                           (i.e. ceil(rate)), min 1 — a small burst allowance.
#   LLM_RATE_MAX_WAIT_S     default 30.0. Fail-safe cap: a genuinely wedged limiter
#                           can never hang a call forever — after this bounded wait
#                           the call PROCEEDS (log_crit), never lost. A provider-
#                           PAUSED bucket EXTENDS the cap to wait out the window reset
#                           (bounded at LLM_RATE_PAUSE_EXTENSION_S, default 300) so a
#                           paused caller never fires uncapped into an exhausted window.
#   LLM_RATE_HEADER_LEARNING default "1"/on. LAYER 2 (adaptive): re-paces the account
#                           bucket from the provider's own x-ratelimit-* headers off
#                           EVERY response; remaining<=1 → HARD PAUSE until the window
#                           reset. Tighten-only vs the effective clamp — never loosens
#                           past operator intent, recovers when a tier/key improves.
#                           The learned rate is persisted as a LEARNER FLOOR on the
#                           bucket so the acquire-time re-clamp cannot overwrite it.


_TRUE_SET = {"1", "true", "yes", "on"}


def _rate_limit_enabled() -> bool:
    """Read live so a test/tenant toggle needs no reload. Default ON."""
    return os.environ.get("LLM_RATE_LIMIT_ENABLED", "1").strip().lower() in _TRUE_SET


def _rate_header_learning_enabled() -> bool:
    """Live read for LAYER 2 (adaptive header learning). Default ON, tighten-only."""
    return os.environ.get("LLM_RATE_HEADER_LEARNING", "1").strip().lower() in _TRUE_SET


def _rate_tokens_per_sec() -> float:
    """Refill rate in tokens/sec, derived from LLM_MAX_RPM (per-account RPM). Fail-safe default."""
    try:
        rpm = float(os.environ.get("LLM_MAX_RPM", "480"))
        if rpm <= 0:
            return 8.0
        return rpm / 60.0
    except (ValueError, TypeError):
        return 8.0


def _rate_learn_pace_window_max_s() -> float:
    """The largest header window that may drive the INSTANTANEOUS pace (rate + burst).

    A token bucket's contract is bursts up to capacity while constraining the AVERAGE.
    Flattening a long-window quota into a per-second rate destroys the burst half: a
    2400/day allowance ÷ 86400s ≈ 0.028 rps, and with capacity ~1s-of-tokens the bucket
    becomes a metronome (1 call / 36-40s) that no multi-call conversational turn can
    satisfy — far STRICTER short-term than the provider's own policy (Cerebras permits
    all 5 minute-requests in the first second of the minute). Windows LONGER than this
    cutoff are quota-shaped, not pace-shaped: they belong to the pause mechanism and the
    paced daily budget controller (src/api/daily_budget.py), never to the per-second
    min(). Env: FL_RATE_LEARN_PACE_WINDOW_MAX_S (seconds; default 600 = minute-class).
    """
    try:
        v = float(os.environ.get("FL_RATE_LEARN_PACE_WINDOW_MAX_S", "600"))
        return v if v > 0 else 600.0
    except (ValueError, TypeError):
        return 600.0


def _rate_burst_capacity(rate: float, allowance: Optional[float] = None) -> float:
    """Burst allowance (bucket capacity). Env override; learned allowance; else ~1s of tokens.

    ``allowance`` is the provider's OWN per-window request allowance (e.g. Cerebras'
    5-per-minute), learned from headers: capacity = that allowance, so a turn of N ≤
    allowance calls draws N permits INSTANTLY — within provider policy — and the window
    refills over its full span. When unknown, the legacy default (one second's worth of
    tokens, at least 1) applies. Env LLM_RATE_BURST still outranks both (operator knob).
    """
    raw = os.environ.get("LLM_RATE_BURST")
    if raw is not None:
        try:
            cap = float(raw)
            if cap >= 1.0:
                return cap
        except (ValueError, TypeError):
            pass
    if allowance is not None:
        try:
            a = float(allowance)
            if a > 0:
                # max(1, ceil(allowance)) — the provider's window allowance IS the burst.
                import math
                return max(1.0, float(math.ceil(a)))
        except (ValueError, TypeError):
            pass
    # default: one second's worth of tokens, at least 1 (a small burst)
    import math
    return max(1.0, float(math.ceil(rate)))


def _rate_max_wait_s() -> float:
    """Fail-safe max total wait per acquire before proceeding anyway. Default 30s."""
    try:
        v = float(os.environ.get("LLM_RATE_MAX_WAIT_S", "30"))
        return v if v > 0 else 30.0
    except (ValueError, TypeError):
        return 30.0


def _rate_headroom() -> float:
    """Fraction of a provider's published max we allow ourselves (stay comfortably under).

    Default 0.9 → we clamp to 90% of the published limit. Env: LLM_RATE_HEADROOM.
    """
    try:
        h = float(os.environ.get("LLM_RATE_HEADROOM", "0.9"))
        return h if 0.0 < h <= 1.0 else 0.9
    except (ValueError, TypeError):
        return 0.9


# ──────────────────────────────────────────────────────────────────────────────
# ENDPOINT-DRIVEN PUBLISHED-LIMITS REGISTRY ("quick-set")
# ──────────────────────────────────────────────────────────────────────────────
#
# Keyed by the brain's base-URL HOST (+ model). Setting an endpoint auto-fills the
# provider's PUBLISHED account-wide rate limits, which the clamp (below) uses to make
# it STRUCTURALLY IMPOSSIBLE to burst past a provider's published ceiling on any model.
#
# Values are CONSERVATIVE published-tier numbers. Erring LOW is intentionally safe:
# the clamp only ever takes min(configured, provider_max*headroom), so an underestimate
# merely makes us gentler, never more abusive. Real limits are often per-org/tier and
# change — these are safe floors, not contractual truth. UPDATE from provider docs.
#
# KNOWN vs UNKNOWN (load-bearing for homelab/self-host zero-impact):
#   * KNOWN host (a metered public provider we recognize) → the hard clamp APPLIES.
#   * UNKNOWN host (a self-hosted / LAN / private brain — no published limit) → NO hard
#     clamp; the configured env pace still applies but never throttles below what the
#     operator asked for. This is why a homelab LLM_BASE_URL is unaffected.
_PROVIDER_LIMITS: dict[str, dict] = {
    # ── DeepSeek — publishes NO RPM/TPM. It meters CONCURRENCY (simultaneous in-flight
    # requests), account-wide across all keys, and returns 429 when exceeded. Because
    # concurrency and requests/minute are not inter-convertible without knowing per-request
    # latency, we record rpm=None (→ no clamp) rather than inventing an RPM: a fabricated
    # ceiling would throttle a provider that never asked us to. The concurrency figures are
    # kept on their own axis so the tenant console can show the real limit.
    # Source: https://api-docs.deepseek.com/quick_start/rate_limit/ (fetched 2026-07-30)
    # NB: no x-ratelimit-* headers are documented or returned (confirmed empirically).
    "api.deepseek.com": {
        "models": {
            "deepseek-v4-pro": {"rpm": None, "tpm": None, "concurrency": 500},
            "deepseek-v4-flash": {"rpm": None, "tpm": None, "concurrency": 2500},
        },
        "default": {"rpm": None, "tpm": None, "concurrency": 500},
    },
    # ── Cerebras — the published ceiling differs by ~100-200x between the Free Trial
    # ($5 credits, 30-day expiry) and the paid Developer tier, on the SAME host+model:
    #     Free Trial:  5 RPM /  30,000 TPM / 1M TPH / 1M TPD  (all current models)
    #     Developer:  gpt-oss-120b 1000 RPM/1M TPM · zai-glm-4.7 500/500k · gemma-4-31b 300/500k
    # We cannot tell which tier a tenant's key is on from the hostname, and our clamp takes
    # min(...) — so hardcoding the Free-Trial floor would PERMANENTLY cap a paying tenant at
    # 4 RPM with no way to raise it (tenant max_rpm only tightens). Clamping to the Developer
    # figures instead means a Free-Trial key sees 429s, which our retry/backoff already
    # handles, and which the tenant can fix by setting their own max_rpm=5. Choosing the
    # recoverable failure over the unrecoverable one.
    # Source: https://inference-docs.cerebras.ai/support/rate-limits (fetched 2026-07-30)
    "api.cerebras.ai": {
        "models": {
            "gpt-oss": {"rpm": 1000, "tpm": 1_000_000},
            "zai-glm": {"rpm": 500, "tpm": 500_000},
            "gemma": {"rpm": 300, "tpm": 500_000},
        },
        "default": {"rpm": 300, "tpm": 500_000},   # tightest of the paid figures
    },
    # Alibaba Cloud Model Studio (DashScope) — Singapore/intl endpoint. Limits are
    # PER ALIBABA-CLOUD-ACCOUNT (aggregate across ALL seats/keys) — exactly our
    # per-account bucket grain. qwen-turbo = 600 RPM / 5,000,000 TPM.
    # Source: https://www.alibabacloud.com/help/en/model-studio/rate-limit  (verified 2026-07)
    "dashscope-intl.aliyuncs.com": {
        "models": {
            "qwen-turbo": {"rpm": 600, "tpm": 5_000_000},
            "qwen-plus": {"rpm": 600, "tpm": 1_000_000},
            "qwen-max": {"rpm": 600, "tpm": 1_000_000},
        },
        "default": {"rpm": 600, "tpm": 1_000_000},
    },
    # DashScope mainland endpoint — same published per-account RPM shape.
    # NOTE (2026-07-30, not changed): a secondary report puts the MAINLAND qwen-turbo ceiling at
    # 1,200 RPM (vs 600 on the international endpoint). Not applied because I did not verify it
    # against the doc myself, and the failure direction of leaving it is under-pacing (safe).
    # Raise to 1_200 once confirmed at https://www.alibabacloud.com/help/en/model-studio/rate-limit
    "dashscope.aliyuncs.com": {
        "models": {"qwen-turbo": {"rpm": 600, "tpm": 5_000_000}},
        "default": {"rpm": 600, "tpm": 1_000_000},
    },
    # OpenAI — limits are PER-ORG and vary by usage TIER; these are conservative
    # Tier-1 figures. Source: https://platform.openai.com/docs/guides/rate-limits
    # ⚠️ UNVERIFIED / SUSPECTED STALE (flagged 2026-07-30, NOT changed — see below).
    # Two problems, neither safe to "fix" without a source: (a) OpenAI no longer publishes a
    # static per-model RPM/TPM table — the guide now points at the account console, so these
    # figures cannot be re-derived from public docs; (b) the model ids below (gpt-4o,
    # gpt-4-turbo, gpt-3.5-turbo) are sunset or sunsetting, so they may never match a live
    # tenant's model string, silently falling through to `default`. Left AS-IS deliberately:
    # the failure direction is under-pacing (429-free but slower), and inventing a number
    # would be worse than an honest stale marker. Re-key to current model ids when a tenant
    # actually configures OpenAI, ideally from their own console values.
    "api.openai.com": {
        "models": {
            "gpt-4o": {"rpm": 500, "tpm": 30_000},
            "gpt-4o-mini": {"rpm": 500, "tpm": 200_000},
            "gpt-4-turbo": {"rpm": 500, "tpm": 30_000},
            "gpt-3.5-turbo": {"rpm": 500, "tpm": 200_000},
        },
        "default": {"rpm": 500, "tpm": 30_000},
    },
    # Anthropic (Claude) — limits are PER-ORG and TIER-DEPENDENT. ⚠️ CORRECTED 2026-07-30:
    # the previous entry claimed "Tier 1 = 50 RPM / 30k ITPM", which was ~20x/65x STALE — those
    # were Claude-3-era figures. Anthropic has since RENAMED the tiers (Tier 1..4 → Start /
    # Build / Scale / Custom) and raised the entry tier substantially. Under-clamping is not
    # "safely conservative" here: because the clamp is min(...) and a tenant's own max_rpm can
    # only TIGHTEN it, a stale-low ceiling permanently throttled a paying tenant to 45 RPM with
    # no way to raise it. Verified by fetching the doc directly (not a secondary report).
    #
    # STANDARD START-TIER figures (the entry tier), per model family:
    #   Opus 5 / Opus 4.x / Sonnet 5 / Sonnet 4.x / Haiku 4.5 → 1,000 RPM, 2M ITPM, 400k OTPM
    #   Fable 5                                               → 1,000 RPM, 500k ITPM, 100k OTPM
    # (Build: 5,000 RPM / 5M ITPM · Scale: 10,000 RPM / 10M ITPM.)
    #
    # Caveat kept deliberately: the doc notes a BRAND-NEW org "may start with limits below the
    # standard limits shown" while history is established, so a fresh key can still 429 — that
    # is retried/backed-off, and such a tenant can set its own max_rpm. We pace to the published
    # standard rather than to an unpublished probationary value we cannot see.
    #
    # Anthropic meters THREE independent buckets per model: RPM, ITPM and OTPM, and only
    # UNCACHED input counts toward ITPM. Our clamp is RPM-driven; tpm below is the ITPM figure
    # (the binding bucket for input-heavy extraction) and is informational.
    # Headers (documented): anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-
    # {limit,remaining,reset} plus retry-after.
    # Source: https://platform.claude.com/docs/en/api/rate-limits (Start tier, fetched 2026-07-30)
    "api.anthropic.com": {
        "models": {
            # Ordered: more specific ids first, since matching is substring-based and
            # "opus"/"sonnet" would otherwise shadow the family entries below.
            "claude-fable": {"rpm": 1_000, "tpm": 500_000},
            "claude-opus": {"rpm": 1_000, "tpm": 2_000_000},
            "claude-sonnet": {"rpm": 1_000, "tpm": 2_000_000},
            # Haiku 3.5 (retired except on Bedrock/GCP) has its OWN lower ITPM — 100k — and is
            # the one family where cache_read tokens DO count toward ITPM. Listed before the
            # general claude-haiku entry because matching is substring-based.
            "claude-haiku-3-5": {"rpm": 1_000, "tpm": 100_000},
            "claude-haiku": {"rpm": 1_000, "tpm": 2_000_000},
            # Legacy dotted ids (claude-3-5-sonnet / claude-3-opus).
            "sonnet": {"rpm": 1_000, "tpm": 2_000_000},
            "opus": {"rpm": 1_000, "tpm": 2_000_000},
            "haiku": {"rpm": 1_000, "tpm": 2_000_000},
        },
        "default": {"rpm": 1_000, "tpm": 2_000_000},
    },
    # Mistral La Plateforme — conservative published default.
    # Source: https://docs.mistral.ai/deployment/laplateforme/rate-limit/
    # ⚠️ UNVERIFIED (flagged 2026-07-30): Mistral no longer publishes a static tier table —
    # its docs direct you to Admin Panel → Limits for per-model values, so this figure cannot
    # be corroborated from public docs. Kept as a conservative default (under-paces rather
    # than over-paces); do not cite it as "official".
    "api.mistral.ai": {
        "default": {"rpm": 300, "tpm": 500_000},
    },
    # Groq — free/dev tier is low; conservative. Source: https://console.groq.com/docs/rate-limits
    "api.groq.com": {
        "default": {"rpm": 30, "tpm": 15_000},
    },
}

# Advisory (NOT a hard clamp) RPM to report for an unrecognized host. Informational only —
# the clamp never applies to unknown hosts, so this value can't throttle a self-hosted brain.
_UNKNOWN_ADVISORY_RPM = 600


# Plain-language transparency disclaimer surfaced next to the per-brain rate-limit control.
# The configurable per-brain max only goes UP TO the provider's published maximum for the
# bound endpoint — we match each provider's published limits (Anthropic/Claude, OpenAI,
# DashScope/Qwen, …) and pace UNDER their required rate. Single source of truth so every
# console door renders identical wording.
_RATE_LIMIT_DISCLAIMER = (
    "Rate limits are capped at each provider's published maximum for this endpoint — "
    "you can set a lower limit, never higher. We stay under the provider's required rate."
)


def rate_limit_disclaimer() -> str:
    """Return the transparency disclaimer for the per-brain rate-limit control (UI copy)."""
    return _RATE_LIMIT_DISCLAIMER


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 3 — 429 FALLBACK (a bare 429 is EVIDENCE; stop discarding it)
# ──────────────────────────────────────────────────────────────────────────────
#
# Layer 2 learns the real ceiling from the provider's own x-ratelimit-* headers. Some
# providers send NONE — measured on production 2026-08-13, api.z.ai returned ZERO
# x-ratelimit-*, ZERO ratelimit-*, ZERO retry-after across every response, so Layer 2 had
# nothing to learn from, the bucket kept its configured rate, and we walked into the wall
# repeatedly (241 x 429 in 48h, 362 breaker-open events).
#
# Being told "too many requests" IS evidence the current estimate is too high, even when
# nothing tells you the true number. This layer converts that bare signal into a pace.
#
# ── WHY THIS IS A SECOND TABLE AND NOT MORE ROWS IN _PROVIDER_LIMITS ──────────
# The two tables answer OPPOSITE questions and must not be merged:
#   * _PROVIDER_LIMITS is the OPTIMISTIC config-time clamp. Its documented policy is to
#     record the PAID/highest published tier, precisely so a paying tenant is never
#     permanently capped at a free-tier figure they cannot raise (see the Cerebras note:
#     "hardcoding the Free-Trial floor would PERMANENTLY cap a paying tenant at 4 RPM").
#     It accepts that a free-tier key will therefore 429.
#   * _RATE_429_FALLBACK is the CONSERVATIVE post-429 figure — the ENTRY/free tier. It is
#     read ONLY after the provider has actually refused us, i.e. after the optimistic
#     clamp has been PROVEN wrong for this key, and it is discarded the instant a real
#     header arrives. This table is the mechanism that resolves the trade-off the
#     _PROVIDER_LIMITS comment documents but could not fix.
#
# ── THE TABLE IS DATA, NOT CONTROL FLOW ──────────────────────────────────────
# This repo forbids provider-name branching in logic. There is NO `if "z.ai" in url`
# anywhere in the decision path: the lookup is the same generic host match
# `resolve_provider_limits` already uses, and every provider-specific fact is a VALUE in
# this dict. A lookup table of published limits is legitimate data because there is no
# structural way to know a given tier's RPM — but it must be exactly that, so every entry
# carries the value, the TIER it applies to, the SOURCE url and the DATE it was recorded.
# Published limits go stale; the date and source are what let a human check it later, so
# they are NOT optional. An entry without them is a bug.
#
# ``rpm=None`` is a FIRST-CLASS, deliberate value: it means "we recognise this provider AND
# it publishes no requests/minute figure at all". It is NOT the same as an absent entry.
# Do NOT substitute an invented number — a fabricated ceiling throttles a provider that
# never asked us to. An rpm=None entry falls through to the same conservative blind
# back-off an unknown host gets, which is the honest answer.
#
# ── WHAT EACH ENTRY RECORDS ───────────────────────────────────────────────────
# These are PUBLISHED FIGURES CAPTURED ON A DATE. They are NOT measurements, and anything
# learned from a provider's own response headers SUPERSEDES them immediately and permanently
# (see _FLOOR_RANK: measured outranks assumed, always).
#
# Rate limits are almost always PER TIER, not per provider — a free tier and a paid tier can
# differ by two orders of magnitude — so every entry NAMES the tier it describes, and where
# tiers differ we record the MOST CONSERVATIVE one. That asymmetry is deliberate: guessing
# high produces the very 429 this layer exists to avoid, and being needlessly slow costs a
# little latency while being throttled costs a retry. Those are not the same price.
#
# Limits are ALSO NOT ALWAYS RPM. Several providers publish tokens-per-minute, concurrent
# request caps, or daily quotas instead of (or alongside) a request rate. Each entry records
# what the vendor ACTUALLY publishes — ``rpm``/``rps``/``tpm``/``rpd``/``concurrency`` — with
# None where the vendor publishes nothing on that axis. We do NOT coerce everything into an
# RPM column: only ``rpm``, or ``rps`` (an exact x60 unit conversion), can become a pace.
# TPM and concurrency are recorded for the console and for humans, and are DELIBERATELY not
# convertible — concurrency→RPM needs per-request latency and TPM→RPM needs tokens-per-request,
# neither of which we know. An entry with no convertible figure falls through to the same
# conservative back-off an unknown host gets. See ``_pacing_rpm_from``.
#
# ``sends_rate_headers`` records whether the vendor documents x-ratelimit-*/retry-after. For a
# provider that DOES, this table is only a pre-header safety net — the first real response
# replaces it. It is recorded because it tells a maintainer how much this row can ever matter.
#
# A provider with NO published limit gets NO ENTRY and falls through to the back-off. An empty
# cell is an honest answer; a plausible-looking invented number is not — it would be trusted,
# it would be wrong, and nobody would know when it went stale. The providers that publish
# nothing are recorded in ``_NO_PUBLISHED_LIMIT`` below so the negative result is not silently
# re-researched every time someone wonders why they are missing.
_RATE_429_FALLBACK: dict[str, dict] = {
    # Cerebras — Free Trial tier ("$5 in free credits", expire 30 days). The tier
    # _PROVIDER_LIMITS deliberately does NOT clamp to, because that table records the paid
    # Developer figures so a paying tenant is never capped. Independently corroborated
    # against a live HelpDeskPro key whose failure profile matched 5 RPM exactly.
    "api.cerebras.ai": {
        "tier": "Free Trial (lowest published; Developer = 300-1000 RPM)",
        "rpm": 5, "rps": None, "tpm": 30_000, "rpd": None, "concurrency": None,
        "sends_rate_headers": True,
        "source": "https://inference-docs.cerebras.ai/support/rate-limits",
        "recorded": "2026-08-13",
        "note": "also 1M tokens/hour and 1M tokens/day on Free Trial; hourly and daily "
                "restrictions do not apply on the paid Developer tier",
    },
    # Groq — free tier is published PER MODEL and spans 10-30 RPM across models
    # (llama-3.1-8b-instant = 30 RPM / 14.4K RPD / 6K TPM / 500K TPD). 10 is the conservative
    # floor of the published range, per the most-conservative-tier rule.
    # NVIDIA NIM / hosted API Catalog (build.nvidia.com). RESEARCHED 2026-08-26.
    #
    # PROVENANCE, STATED HONESTLY BECAUSE IT IS WEAKER THAN EVERY OTHER ROW HERE. NVIDIA does
    # NOT publish this number, and says so in writing — NVIDIA moderator, 2025-09-10: "The rate
    # limits vary for each model, and we do not publish those. However, you can see your maximum
    # rate limit in the top right of build.nvidia.com". The 40 RPM figure therefore comes from
    # (a) multiple users on NVIDIA's OWN developer forum naming it as the DEFAULT while asking
    # for an increase, and (b) one NVIDIA admin acknowledging it directly: "the team is aware of
    # the implications of the 40 rpm rate limit" (forum thread 368420, 2026-05-13). It is the
    # free/developer-tier default, NOT a universal constant.
    #
    # WHY IT IS STILL THE RIGHT VALUE TO RECORD. This table is the CONSERVATIVE post-429 figure
    # (the entry tier), not the optimistic config-time clamp — it is consulted only AFTER the
    # provider has already refused us, and it is labelled `assumed_table` with this source in
    # the tenant-facing disclaimer. An account granted an increase is paced BELOW its real
    # allowance, which is the safe direction; the AIMD recovery
    # (`_rate_fallback_recovery_s`) walks such an account back up, and a further 429 re-tightens
    # it. Do NOT promote this number to _PROVIDER_LIMITS: that table clamps BEFORE any refusal
    # and its contract is a figure the vendor publishes, which this is not.
    #
    # ACCOUNT-WIDE, NOT PER MODEL. Verified against the pacer's own scope: the bucket key is
    # `brain:<ident>` or `url:<base_url>` (`_resolve_rate_limit_key`), never per-model and never
    # per-operation, so one bucket budgets every model and every lane on this host — which is
    # what an account-wide cap requires.
    #
    # MEASURED against the live endpoint the same day: NEITHER a 200 NOR a 429 carries any
    # rate-limit header (no x-ratelimit-*, no Retry-After); the whole 429 body is
    # {"status":429,"title":"Too Many Requests"}. So Layer 2 can never learn a MEASURED floor
    # here and this row is the only ceiling the pacer will ever have. That is exactly why the
    # missing row let the blind back-off ratchet to 1 rpm.
    "integrate.api.nvidia.com": {
        "tier": "Free / developer default (NOT published by NVIDIA; see the provenance note "
                "above — an account with an approved increase is paced below its allowance)",
        "rpm": 40, "rps": None, "tpm": None, "rpd": None, "concurrency": None,
        "source": "https://forums.developer.nvidia.com/t/"
                  "api-rate-limit-increase-for-nvidia-nim/366043",
        "recorded": "2026-08-26",
        "note": "NVIDIA states limits vary per model, per use-case and with ambient traffic, "
                "and are visible only in the account UI. Reported account-wide rather than "
                "per-model. Sends no rate-limit headers on any response, 200 or 429.",
        "sends_rate_headers": False,
    },

    "api.groq.com": {
        "tier": "Free (conservative floor of the published 10-30 RPM per-model range)",
        "rpm": 10, "rps": None, "tpm": 1_200, "rpd": 100, "concurrency": None,
        "sends_rate_headers": True,
        "source": "https://console.groq.com/docs/rate-limits",
        "recorded": "2026-08-13",
        "note": "free tier published as ranges: RPM 10-30, RPD 100-14,400, TPM 1.2K-15K, "
                "TPD 3.6K-500K. The floor of each range is recorded. Groq sends "
                "x-ratelimit-* and retry-after, so headers normally win.",
    },
    # OpenRouter — the published caps apply to FREE model variants (ids ending ':free').
    # Paid variants carry no platform-level request cap (only Cloudflare DDoS protection,
    # whose thresholds are not published). Most conservative = no credits ever purchased.
    "openrouter.ai": {
        "tier": "Free variants (':free'), no credits ever purchased",
        "rpm": 20, "rps": None, "tpm": None, "rpd": 50, "concurrency": None,
        "sends_rate_headers": True,
        "source": "https://openrouter.ai/docs/api-reference/limits",
        "recorded": "2026-08-13",
        "note": "RPD rises to 1000 with >=$10 lifetime credits. Paid model variants have no "
                "published platform request cap, so this row over-tightens them — which is "
                "the safe direction, and a real header replaces it.",
    },
    # Fireworks — account-wide request ceiling, NOT tiered by spend. Spending tiers control
    # TPM, not RPM. Most conservative = no payment method / no credits.
    "api.fireworks.ai": {
        "tier": "No payment method or no credits (with payment + credits: 6,000 RPM)",
        "rpm": 10, "rps": None, "tpm": None, "rpd": None, "concurrency": None,
        # True: Fireworks serverless responses DO carry rate headers — but TOKEN-level only
        # (X-Ratelimit-Limit-Tokens-Prompt / -Cache-Adjusted-Prompt / -Generated), never
        # RPM/remaining-requests. The learner paces REQUESTS not tokens, so these are not
        # learnable today; the flag is documented-accurate for anyone extending token pacing.
        "sends_rate_headers": True,
        "source": "https://docs.fireworks.ai/guides/quotas_usage/account-quotas",
        "recorded": "2026-08-13",
        "note": "the 6,000 RPM cap is account-wide and fixed, not adaptive; spend tiers move "
                "TPM ceilings rather than the request ceiling. Response headers are token-only.",
    },
    # Alibaba Cloud Model Studio (DashScope), Singapore/international endpoint. Limits are
    # applied at the ROOT ACCOUNT level — "the usage of all RAM users, workspaces, and API
    # keys under the account is combined" — which is exactly our per-brain/account bucket grain.
    "dashscope-intl.aliyuncs.com": {
        "tier": "Standard (Singapore/international region)",
        "rpm": 600, "rps": None, "tpm": 1_000_000, "rpd": None, "concurrency": None,
        "sends_rate_headers": False,
        "source": "https://www.alibabacloud.com/help/en/model-studio/rate-limit",
        "recorded": "2026-08-13",
        "note": "per-model: qwen-turbo 600 RPM/5M TPM, qwen-plus 600/1M, qwen-max 600/1M. "
                "RPM is uniform at 600 so a single host-level row is exact; the TPM recorded "
                "is the tightest (1M). Vendor also enforces per-second RPM/60.",
    },
    # DashScope mainland (Beijing). ⚠️ This RESOLVES a standing "unverified" note in
    # _PROVIDER_LIMITS, which recorded 600 and flagged a secondary report of 1,200 that no
    # one had confirmed. Confirmed 2026-08-13 against the vendor page: mainland qwen-turbo
    # IS 1,200 RPM. Recorded here; _PROVIDER_LIMITS left alone (different table, different job).
    "dashscope.aliyuncs.com": {
        "tier": "Standard (China/Beijing region)",
        "rpm": 1_200, "rps": None, "tpm": 1_000_000, "rpd": None, "concurrency": None,
        "sends_rate_headers": False,
        "source": "https://www.alibabacloud.com/help/en/model-studio/rate-limit",
        "recorded": "2026-08-13",
        "note": "per-model: qwen-turbo 1,200 RPM/5M TPM, qwen-max 1,200/1M, qwen-plus "
                "30,000/5M. 1,200 is the conservative figure across the three.",
    },
    # Anthropic — Start is the LOWEST tier with published numbers, and RPM is uniform at
    # 1,000 across every model in it. Caveat kept deliberately: the doc states new orgs "may
    # start in the Evaluation tier, with limits below the standard limits shown on this page"
    # — those Evaluation figures are NOT published, so a brand-new key can 429 under this row.
    # That is safe here: Anthropic always sends anthropic-ratelimit-* AND retry-after, so the
    # measured lane takes over on the first response.
    "api.anthropic.com": {
        "tier": "Start (lowest PUBLISHED tier; new orgs may begin on the unpublished "
                "Evaluation tier, which is lower)",
        "rpm": 1_000, "rps": None, "tpm": 500_000, "rpd": None, "concurrency": None,
        "sends_rate_headers": True,
        "source": "https://platform.claude.com/docs/en/api/rate-limits",
        "recorded": "2026-08-13",
        "note": "Start RPM is 1,000 for every model. TPM recorded is the tightest published "
                "ITPM at Start (Fable 5 = 500k; other families 2M). Only UNCACHED input "
                "counts toward ITPM. Anthropic meters RPM/ITPM/OTPM as three buckets.",
    },
    # xAI — Tier 0 is the default at $0 cumulative spend. Published in REQUESTS PER SECOND,
    # not per minute; rps is recorded as published and converted only by the exact x60.
    # ⚠️ Read for grok-4.6 specifically; xAI publishes a per-model table and other models
    # differ. At 150 RPS (= 9,000 RPM) this row is far above any pace we would run, so it
    # will essentially never bind — a 429 from xAI will therefore take the blind back-off via
    # the "does not explain the 429" rule below, which is the correct outcome.
    "api.x.ai": {
        "tier": "Tier 0 (default, $0 cumulative spend)",
        "rpm": None, "rps": 150, "tpm": 50_000_000, "rpd": None, "concurrency": None,
        "sends_rate_headers": False,
        "source": "https://docs.x.ai/developers/rate-limits",
        "recorded": "2026-08-13",
        "note": "figure read for grok-4.6; per-model table, other models differ. xAI "
                "documents no rate-limit response headers, only the 429 itself.",
    },
    # DeepSeek — publishes NO RPM and NO TPM at all. It meters CONCURRENCY per account
    # ("a request counts as one concurrent connection from the time it is sent until the model
    # response is complete") and returns 429 when exceeded. Concurrency is recorded on its own
    # axis and is deliberately NOT converted into a pace: that conversion needs per-request
    # latency, which we do not have. This row therefore yields no pacing figure BY DESIGN and
    # falls through to the conservative back-off.
    "api.deepseek.com": {
        "tier": "All accounts (meters concurrency; publishes no RPM/TPM)",
        "rpm": None, "rps": None, "tpm": None, "rpd": None, "concurrency": 500,
        "sends_rate_headers": False,
        "source": "https://api-docs.deepseek.com/quick_start/rate_limit/",
        "recorded": "2026-08-13",
        "note": "deepseek-v4-pro 500 concurrent, deepseek-v4-flash 2500; 500 is the "
                "conservative figure. Under load DeepSeek holds the connection open with "
                "empty lines / SSE ': keep-alive' rather than 429ing immediately.",
    },
}

# ── PROVIDERS THAT PUBLISH NOTHING — a RECORDED NEGATIVE, not an oversight ────
# Each of these was researched against the vendor's own documentation on the date shown and
# found to publish NO static numeric limit. They deliberately get NO row in the table above,
# so they fall through to the conservative back-off. This dict is documentation ONLY — it is
# never consulted by the decision path (see the test that pins that) — and exists so the next
# person does not spend an afternoon re-discovering the same absence.
_NO_PUBLISHED_LIMIT: dict[str, dict] = {
    "api.openai.com": {
        "reason": "No static per-model numeric table. The guide now says: 'You can view the "
                  "rate and usage limits for your organization under the limits section of "
                  "your account settings.' Tiers (Free, 1-5) are published as monthly SPEND "
                  "qualifications only, not as RPM/TPM.",
        "source": "https://developers.openai.com/api/docs/guides/rate-limits",
        "recorded": "2026-08-13",
        "sends_rate_headers": True,
    },
    "api.mistral.ai": {
        "reason": "No numbers published. Docs direct to 'Admin Panel > API > Limits' for the "
                  "per-model tokens-per-minute and requests-per-second values.",
        "source": "https://docs.mistral.ai/admin/billing-usage/usage-limits",
        "recorded": "2026-08-13",
        "sends_rate_headers": False,
    },
    "api.together.xyz": {
        "reason": "Explicitly dynamic by design: 'Dynamic rate limits adjust with usage, so "
                  "there are no fixed per-model limits published.' A fixed limit requires a "
                  "dedicated endpoint.",
        "source": "https://docs.together.ai/docs/rate-limits",
        "recorded": "2026-08-13",
        "sends_rate_headers": True,
    },
    "api.z.ai": {
        "reason": "THE LIVE CASE. Publishes no numeric RPM/TPM/concurrency anywhere — the GLM "
                  "Coding Plan docs give only a qualitative ordering ('Max > Pro > Lite') and "
                  "a recommended concurrent-PROJECT count. Sends no rate headers either "
                  "(0 x-ratelimit-*/retry-after across 96h of production logs), so z.ai is "
                  "the pure blind-back-off case in both directions.",
        "source": "https://docs.z.ai/devpack/usage-policy",
        "recorded": "2026-08-13",
        "sends_rate_headers": False,
    },
}

# ── 429s THAT ARE NOT ABOUT RATE ─────────────────────────────────────────────
# A 429 does not always mean "too many requests". z.ai returns HTTP 429 for an EXHAUSTED
# ACCOUNT BALANCE. Measured on production 2026-08-13, 100% of sampled 429 bodies were non-rate
# verdicts (66 x code 1113 "Insufficient balance or no resource package. Please recharge.",
# 24 x code 1214 "modelCode: does not exist"); z.ai's OWN documented rate-limit code, 1302,
# appeared ZERO times. No pace is low enough to fix an empty wallet or a wrong model id, so
# adopting a rate assumption there would throttle the tenant forever for a condition throttling
# cannot touch — and would show them a fabricated DIAGNOSIS on top of a fabricated NUMBER.
#
# Kept SEPARATE from the limits table on purpose: these are documented error-code semantics,
# not published limits, so a provider can appear here while publishing no limits at all (z.ai
# does exactly that). Codes are the vendor's own, cited below.
_NON_RATE_429_CODES: dict[str, dict] = {
    # Source: https://docs.z.ai/api-reference/api-code (fetched 2026-08-13). Deliberately
    # EXCLUDES 1302 "Rate limit reached for requests" and 1305 "service temporarily
    # overloaded" — those ARE genuine rate evidence and must reach the pacer. Also excludes
    # the usage-WINDOW codes (1308/1310/1316-1321, "Usage limit reached for N unit, resets
    # at ..."): a quota window is not an RPM we can pace to, but slowing down is at least
    # directionally right, so they stay rate evidence.
    "api.z.ai": {
        "source": "https://docs.z.ai/api-reference/api-code",
        "recorded": "2026-08-13",
        "codes": {
            "1113",  # Insufficient balance or no resource package. Please recharge.
            "1214",  # Parameter is invalid / modelCode does not exist.
            "1309",  # GLM Coding Plan package expired.
            "1311",  # Subscription does not include access to this model.
            "1314",  # Enterprise package expired.
            "1315",  # API key limited to enterprise coding package scenarios.
        },
    },
}

# Provider-INDEPENDENT markers for a 429 that is a settled account verdict rather than a
# pacing signal. Used ONLY as a fail-safe for hosts with no entry in _NON_RATE_429_CODES.
# Deliberately narrow: every phrase here names a condition that slowing down provably cannot
# fix. "quota"/"limit"/"rate" are NOT here — those are exactly the pacing signals we want.
_GENERIC_NON_RATE_429_MARKERS = (
    "insufficient balance",
    "insufficient_quota",
    "no resource package",
    "please recharge",
    "billing",
    "payment required",
    "does not exist",
    "has expired",
    "expired.",
)


# Provenance ranks for the bucket's learned floor. STRICT and NON-INVERTIBLE: a write is
# accepted only when its rank is >= the rank already in force, so an ASSUMPTION can never
# overwrite a MEASUREMENT. See _TokenBucket.set_learner_floor.
#
# The two ASSUMED kinds share rank 1 DELIBERATELY. They are different LABELS (the disclaimer
# must name a published source when there is one, and admit there is none when there is not)
# but they are the same GRADE of evidence, so either may tighten the other. That matters for
# the real case: we adopt a table figure, the provider 429s anyway — the published number is
# wrong for this key's tier, and the blind back-off has to be able to take over from it. If
# TABLE outranked BACKOFF the pace would be frozen at a figure already proven wrong.
# Only MEASURED is privileged, because only MEASURED is a fact — and CALIBRATED outranks it:
# a user-driven Detect-cap probe deliberately pushed the provider until it refused, which is
# a measurement taken ON PURPOSE and must not be silently overwritten by the next response
# header that happens to carry a looser number.
FLOOR_NONE = ""
FLOOR_BACKOFF = "assumed_backoff"   # bare 429, no header, no table entry → blind back-off
FLOOR_TABLE = "assumed_table"       # bare 429, no header, table entry → published figure
FLOOR_MEASURED = "measured"         # the provider's own headers said so
FLOOR_CALIBRATED = "calibrated"     # a user-driven capacity probe measured it directly
_FLOOR_RANK = {FLOOR_NONE: 0, FLOOR_BACKOFF: 1, FLOOR_TABLE: 1, FLOOR_MEASURED: 2,
               FLOOR_CALIBRATED: 3}
_ASSUMED_PROVENANCES = (FLOOR_TABLE, FLOOR_BACKOFF)


def _rate_fallback_decay() -> float:
    """Multiplier applied to the CURRENT pace on a bare 429 we cannot price. Default 0.5."""
    try:
        v = float(os.environ.get("LLM_RATE_429_DECAY", "0.5"))
        return v if 0.0 < v < 1.0 else 0.5
    except (ValueError, TypeError):
        return 0.5


def _rate_fallback_min_rpm() -> float:
    """Floor under the blind back-off so decay can never converge on zero (a stopped brain).

    Default 1 request/minute — slow, but a rate a tenant can still make progress at, and
    one a single real header immediately replaces.
    """
    try:
        v = float(os.environ.get("LLM_RATE_429_MIN_RPM", "1"))
        return v if v > 0 else 1.0
    except (ValueError, TypeError):
        return 1.0


def _rate_fallback_recovery_s() -> float:
    """Quiet (429-free) seconds an ASSUMED floor must hold before it may ease back up.

    THE DEFECT THIS EXISTS FOR. ``_rate_fallback_min_rpm``'s docstring says the blind
    back-off floor is "one a single real header immediately replaces" — and for a provider
    that SENDS rate-limit headers that is true (``_observe_rate_headers`` overwrites the
    floor with a MEASURED one on the very next response). For a provider that 429s BARE it
    is false, and the consequence is not a slow lane but a dead one: the blind back-off only
    ever multiplies the pace DOWN (``current * _rate_fallback_decay()``), nothing anywhere
    multiplies it back up, so the floor is a ONE-WAY RATCHET that converges on
    ``LLM_RATE_429_MIN_RPM`` (1 rpm) and stays there for the life of the process. Measured
    on NVIDIA NIM: 480 → 240 → ... → 7.5 → 3.75 → 1.88 → 1.0 rpm, then 500 consecutive
    clean 200s left it at 1.0.

    This restores the other half of AIMD (Chiu & Jain 1989; the shape TCP congestion control
    and AWS/Google client-side adaptive throttling both use): multiplicative DEcrease on
    evidence of refusal, multiplicative INcrease on evidence of sustained acceptance. The
    increase is deliberately smaller than the decrease is large (1.5x up vs 0.5x down), so
    one 429 undoes roughly two recovery steps and sustained pressure still converges DOWN —
    the fail-closed direction. The recovery can never exceed the already-clamped effective
    rate (operator ``LLM_MAX_RPM`` / per-brain max / published provider clamp), so it can
    only ever undo an ASSUMPTION we invented, never raise a ceiling anybody documented.

    ``<= 0`` disables recovery → byte-for-byte the pre-fix ratchet. Default 120s.
    """
    try:
        v = float(os.environ.get("LLM_RATE_429_RECOVERY_S", "120"))
        return v if v > 0 else 0.0
    except (ValueError, TypeError):
        return 120.0


def _rate_fallback_recovery_factor() -> float:
    """Multiplier applied to an ASSUMED floor per quiet interval. Default 1.5.

    Must be > 1 (an increase) and is deliberately well under 1/``_rate_fallback_decay()``
    (2.0) so recovery is slower than back-off. Out-of-range → the safe default.
    """
    try:
        v = float(os.environ.get("LLM_RATE_429_RECOVERY_FACTOR", "1.5"))
        return v if 1.0 < v <= 2.0 else 1.5
    except (ValueError, TypeError):
        return 1.5


def _rate_429_fallback_enabled() -> bool:
    """Live read. OFF → a bare 429 is discarded exactly as it was before this layer."""
    return os.environ.get("LLM_RATE_429_FALLBACK", "1").strip().lower() in _TRUE_SET


def _lookup_by_host(table: dict, base_url: Optional[str]) -> Optional[dict]:
    """Generic host lookup into ANY of the keyed data tables. Same match as the registry.

    No provider literal is tested here — the host is parsed from the URL and looked up in the
    dict, so adding a provider is a DATA edit and never a code edit. Fail-safe: any parse
    error → None (treated as an unknown host → blind back-off).
    """
    host = ""
    try:
        from urllib.parse import urlparse
        if base_url:
            host = (urlparse(str(base_url)).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    for known_host, entry in table.items():
        if host == known_host or host.endswith("." + known_host):
            return entry
    return None


def _fallback_entry_for(base_url: Optional[str]) -> Optional[dict]:
    """Published-limits entry for this endpoint's host, or None (→ conservative back-off)."""
    return _lookup_by_host(_RATE_429_FALLBACK, base_url)


def _non_rate_codes_for(base_url: Optional[str]) -> frozenset:
    """Documented 429-bearing error codes for this host that are NOT about request volume."""
    entry = _lookup_by_host(_NON_RATE_429_CODES, base_url)
    return frozenset((entry or {}).get("codes") or ())


def _pacing_rpm_from(entry: Optional[dict]) -> Optional[float]:
    """The requests/minute figure a published entry can legitimately become, or None.

    ONLY exactly-convertible units qualify. ``rpm`` is used directly; ``rps`` is multiplied
    by 60, which is arithmetic, not estimation. ``tpm``, ``rpd`` and ``concurrency`` are
    recorded on the entry for humans and the console but are DELIBERATELY never converted:
    concurrency→RPM needs per-request latency and TPM→RPM needs tokens-per-request, and we
    know neither. An entry with no convertible figure returns None and the caller falls
    through to the conservative back-off — which is the honest answer, not a gap.
    """
    if not entry:
        return None
    rpm = entry.get("rpm")
    if rpm:
        return float(rpm)
    rps = entry.get("rps")
    if rps:
        return float(rps) * 60.0
    return None


def _response_body_text(resp: Any) -> str:
    """Best-effort lowercase body text of a response. Never raises; unreadable → ""."""
    try:
        text = getattr(resp, "text", None)
        if text is None:
            return ""
        return str(text)[:2000].lower()
    except Exception:  # noqa: BLE001
        return ""


def _429_is_rate_evidence(resp: Any, codes: Any = None) -> tuple[bool, str]:
    """Is this 429 evidence about PACING, or a settled verdict pacing cannot fix?

    Returns ``(is_rate_evidence, reason)``. The host's documented ``codes`` (from
    ``_NON_RATE_429_CODES``) are authoritative; the generic markers are the fail-safe for a
    host with no documented codes.

    FAIL-SAFE DIRECTION IS **TRUE** (treat as rate evidence): an unreadable or unrecognised
    body must still cause a back-off, because the whole point of this layer is that an
    unknown provider cannot be left hammering. The narrow, explicitly-listed non-rate cases
    are the only ones that opt out.
    """
    body = _response_body_text(resp)
    if not body:
        return True, "body_unreadable"
    for code in (codes or ()):
        # Match the code as it appears in a JSON error envelope, not as a bare substring —
        # a bare "1113" could collide with an id or a token count elsewhere in the body.
        if f'"code":"{code}"' in body or f'"code": "{code}"' in body:
            return False, f"non_rate_code:{code}"
    for marker in _GENERIC_NON_RATE_429_MARKERS:
        if marker in body:
            return False, f"non_rate_marker:{marker}"
    return True, "rate_evidence"


def rate_assumption_disclaimer(rpm: float, provenance: str,
                               entry: Optional[dict] = None) -> str:
    """THE disclaimer. ONE definition, rendered identically in the logs and in the console.

    A guessed number presented as a measurement is worse than no number, so this string
    always says three things: that the figure is an ASSUMPTION, WHERE it came from (a named
    published source with the date it was recorded, or an explicit "we had nothing"), and
    that a real header REPLACES it.
    """
    shown = int(rpm) if float(rpm).is_integer() else round(rpm, 1)
    if provenance == FLOOR_TABLE and entry:
        return (
            f"Assumed limit: {shown} requests/min. This is NOT a measurement — this provider "
            f"returned 429 without any rate-limit headers for us to read, so we applied their "
            f"published limit for this tier: {entry.get('tier', 'entry tier')}. "
            f"Source: {entry.get('source', 'an unrecorded source')}, "
            f"read {entry.get('recorded', 'on an unrecorded date')}. "
            f"Published limits go stale and your plan may differ. We will replace this with a "
            f"real measured value the moment this provider sends a rate-limit header."
        )
    return (
        f"Assumed limit: {shown} requests/min. This is NOT a measurement — this provider "
        f"returned 429 without any rate-limit headers for us to read, and we have no "
        f"published limits on file for it, so we slowed down rather than keep hammering. "
        f"We will replace this with a real measured value the moment this provider sends a "
        f"rate-limit header."
    )


def rate_refusal_disclaimer(reason: str) -> str:
    """Tenant-facing note for a 429 that pacing cannot fix (balance/entitlement/config).

    Stating this plainly is the whole point: throttling an out-of-credit account makes it
    slower AND still broken, and reporting it as a rate limit sends the tenant to the wrong
    control entirely.
    """
    return (
        "This endpoint returned 429, but its response says the cause is not request volume "
        f"({reason}) — so we did NOT change your rate limit, because slowing down cannot fix "
        "it. Check the account's balance, plan and model name with the provider."
    )


def _global_env_max_rpm() -> float:
    """The operator's configured global LLM_MAX_RPM (requests/min). Fail-safe default 480."""
    try:
        v = float(os.environ.get("LLM_MAX_RPM", "480"))
        return v if v > 0 else 480.0
    except (ValueError, TypeError):
        return 480.0


def effective_rpm_for(base_url: Optional[str], model: Optional[str],
                      brain_max_rpm: Optional[int] = None) -> dict:
    """PURE, side-effect-free compute of the effective per-account RPM from EXPLICIT inputs.

    Shared display/reporting core for the console rate-limit surface (the console does NOT have
    the tenant ContextVar bound, so it passes the stored per-brain value in explicitly). Mirrors
    the TIGHTEST-OF-THREE clamp the live pacer applies in ``_effective_rate_per_sec``:

        effective_rpm = min(
            brain_max_rpm                      (if set),
            global env LLM_MAX_RPM,
            floor(provider_max_rpm * headroom) (KNOWN host only),
        )

    ``provider_max_rpm`` in the returned dict is the RAW published ceiling (the "provider max is Y"
    the UI shows); the effective value additionally applies the safety headroom. For an UNKNOWN
    (self-hosted) host there is no published max, so provider_max_rpm is None and no provider clamp
    applies. Returns a JSON-safe dict; includes the transparency disclaimer.
    """
    import math
    global_env_rpm = _global_env_max_rpm()
    candidates = [global_env_rpm]

    configured = None
    if brain_max_rpm:
        try:
            configured = int(brain_max_rpm)
            if configured > 0:
                candidates.append(float(configured))
            else:
                configured = None
        except (ValueError, TypeError):
            configured = None

    limits = resolve_provider_limits(base_url, model)
    provider_known = bool(limits.get("known"))
    provider_max_rpm = int(limits["rpm"]) if limits.get("rpm") is not None else None
    if provider_known and provider_max_rpm:
        provider_cap_rpm = math.floor(provider_max_rpm * _rate_headroom())
        if provider_cap_rpm <= 0:
            provider_cap_rpm = provider_max_rpm
        candidates.append(float(provider_cap_rpm))

    effective_rpm = int(min(candidates))
    return {
        "configured_max_rpm": configured,
        "global_env_rpm": int(global_env_rpm),
        "provider_max_rpm": provider_max_rpm if provider_known else None,
        "provider_known": provider_known,
        "provider_source": limits.get("source"),
        "headroom": _rate_headroom(),
        "effective_rpm": effective_rpm,
        "disclaimer": _RATE_LIMIT_DISCLAIMER,
    }


def resolve_provider_limits(base_url: Optional[str], model: Optional[str]) -> dict:
    """Resolve published account-wide limits for an endpoint host + model.

    Returns ``{"rpm": int, "tpm": Optional[int], "source": str, "known": bool}``.
      * ``known=True``  → a metered provider we recognize; the hard clamp APPLIES.
      * ``known=False`` → unrecognized host (self-hosted / LAN / unknown provider):
        an advisory conservative default, and the clamp does NOT apply (so a homelab /
        BYO self-hosted endpoint is never throttled below the operator's configured rate).
    Fail-safe: any parse error → treated as unknown (no clamp).
    """
    host = ""
    try:
        from urllib.parse import urlparse
        if base_url:
            host = (urlparse(str(base_url)).hostname or "").lower()
    except Exception:
        host = ""

    entry = None
    for known_host, e in _PROVIDER_LIMITS.items():
        if host == known_host or host.endswith("." + known_host):
            entry = e
            break
    if entry is None:
        return {"rpm": _UNKNOWN_ADVISORY_RPM, "tpm": None,
                "source": host or "unknown", "known": False}

    limits = None
    m = (model or "").lower()
    for model_key, model_limits in entry.get("models", {}).items():
        if model_key in m:
            limits = model_limits
            break
    if limits is None:
        limits = entry.get("default", {"rpm": _UNKNOWN_ADVISORY_RPM, "tpm": None})
    # rpm may be None for a RECOGNIZED provider that publishes no requests/minute figure at
    # all (DeepSeek meters CONCURRENCY instead). known=True + rpm=None means "we know this
    # provider and it has no RPM ceiling to clamp against" — the caller skips the clamp
    # (`if provider_known and provider_max_rpm`). Do NOT substitute an invented RPM here:
    # a made-up ceiling silently throttles a provider that never asked us to.
    _rpm = limits.get("rpm")
    return {"rpm": int(_rpm) if _rpm is not None else None,
            "tpm": limits.get("tpm"),
            "concurrency": limits.get("concurrency"),
            "source": host, "known": True}


def _configured_brain_max_rpm() -> Optional[int]:
    """The PER-BRAIN configured max RPM stored on the bound tenant config, or None.

    Read fail-safe from the resolved tenant brain config (``account_llm_config.max_rpm``).
    None / unset / non-positive / unparseable → None (→ no per-brain tightening; the global
    env + provider-registry behavior is byte-for-byte unchanged). SAAS off / no tenant cfg →
    always None (``_current_llm_config_or_none`` returns None instantly).
    """
    cfg = _current_llm_config_or_none()
    if not cfg:
        return None
    try:
        v = cfg.get("max_rpm")
        if v is None:
            return None
        v = int(v)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _effective_rate_per_sec(base_url: Optional[str], model: Optional[str]) -> tuple[float, dict]:
    """The token-bucket refill rate (tokens/sec) AFTER the TIGHTEST-OF-THREE clamp.

    effective_rps = min(
        per_brain_max_rpm / 60          (if the bound tenant config set one),
        global env LLM_MAX_RPM / 60     (the operator's configured pace),
        floor(provider_max_rpm * headroom) / 60   (KNOWN host only),
    )

    The per-brain value TIGHTENS but never LOOSENS: because it enters a ``min`` against the
    provider cap, a tenant that stores 5000 RPM for a 600-RPM endpoint is RE-CLAMPED here to
    the provider ceiling (defense-in-depth on top of the write-time clamp in
    ``saas.llm_config.set_account_max_rpm``) — STRUCTURALLY unable to exceed the published max.
    An UNKNOWN (self-hosted / LAN) host has no published max, so only the configured + per-brain
    values apply (the per-brain value can only make a homelab endpoint gentler, never faster
    than the operator asked). Returns (effective_rps, limits_dict) — the limits dict drives the
    once-per-key log.
    """
    import math
    configured = _rate_tokens_per_sec()
    # Per-brain override tightens the configured pace (never loosens it).
    brain_max_rpm = _configured_brain_max_rpm()
    if brain_max_rpm:
        configured = min(configured, brain_max_rpm / 60.0)
    limits = resolve_provider_limits(base_url, model)
    if not limits.get("known"):
        return configured, limits
    # A RECOGNIZED provider may publish no RPM at all (DeepSeek meters concurrency), in which
    # case there is nothing to clamp against — pace at the configured/per-brain rate only.
    if limits.get("rpm") is None:
        return configured, limits
    provider_cap_rps = math.floor(limits["rpm"] * _rate_headroom()) / 60.0
    if provider_cap_rps <= 0:
        provider_cap_rps = limits["rpm"] / 60.0  # headroom rounded to zero — use raw max
    return min(configured, provider_cap_rps), limits


def _resolve_endpoint_and_model() -> tuple[Optional[str], Optional[str]]:
    """Resolve the (base_url, model) THIS call will hit, for the clamp lookup.

    Tenant BYO cfg wins (its own brain); else the global env brain. Fail-safe → (None, None).
    """
    cfg = _current_llm_config_or_none()
    base = model = None
    if cfg:
        base = cfg.get("base_url")
        model = cfg.get("model")
    if not base:
        base = os.environ.get("LLM_BASE_URL") or None
    if not model:
        model = os.environ.get("WGM_LLM_MODEL") or None
    return base, model


# Smallest sleep slice while pacing — keeps the loop responsive without busy-spinning.
_RATE_MIN_SLICE = 0.001
_RATE_MAX_SLICE = 0.25
# Bound on how far a provider hard-pause can extend the acquire wait past LLM_RATE_MAX_WAIT_S.
# A caller waits out a normal window reset (~60s) but a pathological long pause (an hour
# bucket at remaining<=1) still fail-opens after ~300s — it can never park a caller forever.
def _park_past_budget() -> bool:
    """Restore the pre-fix behaviour: let a provider pause park a caller past its own budget.

    Default OFF. Kept as an escape hatch for an operator who would rather a caller wait out a
    long provider pause than fail fast, but the default is now "never wait longer than the
    caller can afford", because a wait that outlives its caller is not patience, it is a hang.
    """
    return str(os.environ.get("LLM_RATE_PARK_PAST_BUDGET", "")).strip().lower() in (
        "1", "true", "yes", "on")


_RATE_PAUSE_EXTENSION_S = 300.0
# Bound on how far a BRAIN MAINTENANCE window (user-driven Detect-cap probe) may extend the
# acquire wait. The probe is short and bounded (~a minute of burst), and the modal that starts
# it PROMISES "this test will pause all other LLM actions in progress" — so an interactive
# caller holds for the probe rather than failing open into it. Still bounded (never park a
# caller forever) and env-overridable for an operator who wants a tighter leash.
_RATE_MAINT_EXTENSION_S = float(os.environ.get("LLM_RATE_MAINT_EXTENSION_S", "300")
                                or 300.0)


class _TokenBucket:
    """A monotonic-clock token bucket shared by sync + async callers for ONE key.

    Refills at ``rate`` tokens/sec up to ``capacity``. Acquiring a token when empty
    BLOCKS (paces) rather than rejecting — the respectful behavior for a metered API.
    Token accounting is guarded by a threading.Lock held only for the arithmetic
    (never across a sleep), so sync and async callers see one consistent bucket.

    ``pause_until`` (monotonic) implements the LAYER-2 HARD PAUSE: a provider that
    reported the budget exhausted hands out NO tokens until its window resets.
    ``learner_floor_rps`` persists the LAYER-2 learned rate as a ceiling the
    acquire-time re-clamp (``_get_rate_bucket``) must NOT exceed — without it, a
    learned rate would be bumped back to the registry clamp on the very next acquire.
    """

    __slots__ = ("rate", "capacity", "tokens", "last", "pause_until",
                 "learner_floor_rps", "learner_source", "floor_provenance", "floor_note",
                 "floor_capacity", "floor_set_at",
                 "coord_scope", "maint_until", "_lock")

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity  # start full — a cold/idle system is never penalized
        self.last = time.monotonic()
        self.pause_until = 0.0  # monotonic; > now → provider said "wait until window reset"
        # Monotonic MIRROR of the shared maintenance window (Detect-cap probe in flight).
        # Refreshed from the shared take on every acquire and CLEARED the moment the shared
        # read says no window is in force — so a Redis-side release takes effect on this
        # process's very next acquire attempt. Used ONLY to extend the wait cap (holding,
        # not pacing); it is deliberately NOT the provider pause, whose release clock is
        # the provider's, not ours.
        self.maint_until = 0.0
        self.learner_floor_rps = 0.0  # 0.0 = no floor; >0 → acquire-time resync must not exceed
        self.learner_source = ""      # endpoint tag the floor was learned from ("" = unset)
        # WHICH kind of evidence produced the floor in force (FLOOR_* / _FLOOR_RANK). The
        # disclaimer and the health bar both depend on this, and so does precedence: an
        # ASSUMPTION may never overwrite a MEASUREMENT.
        self.floor_provenance = FLOOR_NONE
        self.floor_note = ""          # human-readable basis (published source + date)
        # Burst allowance learned WITH the floor (the provider's per-window request count,
        # e.g. 5 for a 5-per-minute ceiling). 0.0 = not learned → legacy ~1s-of-tokens cap.
        self.floor_capacity = 0.0
        # Monotonic stamp of the last floor WRITE. For an ASSUMED floor every write is a
        # TIGHTENING caused by a 429, so this doubles as "when this provider last refused
        # us" — which is exactly the clock the AIMD recovery needs (see relax_assumed_floor).
        self.floor_set_at = 0.0
        # redis_coord.Scope for the ACCOUNT-WIDE shared budget; None → local bucket only.
        self.coord_scope: Any = None
        self._lock = threading.Lock()

    def _shared_budget_wait(self) -> Optional[float]:
        """Seconds to wait per the CROSS-PROCESS budget, or None for "no shared opinion".

        0.0 means a token was actually SPENT in the shared bucket — so this must be called
        exactly once per attempted request, which is why it lives inside ``_try_acquire``
        and not in the acquire loop's wait path.

        A shared HARD PAUSE (any process saw a 429 with the budget exhausted) is ADOPTED
        into the local ``pause_until`` so the caller's wait cap extends exactly as if THIS
        process had seen the 429 — the bounded pause-extension + "provider_paused" fail-open
        semantics then apply uniformly. Called under ``self._lock`` → assigns directly;
        ``pause_for`` (which takes the lock) would deadlock here.

        A shared MAINTENANCE window (user-driven Detect-cap probe) is mirrored into the
        local ``maint_until`` the same way — refreshed every acquire, cleared the moment
        the shared read reports no window — but is deliberately NOT folded into
        ``pause_until``: maintenance is released by US (the probe's finally), and a local
        pause adopted from it would keep throttling this process after the release.
        """
        scope = self.coord_scope
        if scope is None:
            return None
        try:
            from src.api import redis_coord
            # self.capacity ALREADY encodes the resolved allowance and the operator's
            # knobs, so it is the only thing the shared path needs. An earlier revision
            # also passed window_allowance so the Lua could widen burst under a bound
            # floor; that was wrong -- a floor may only ever tighten -- and the parameter
            # is gone rather than left sitting there looking load-bearing.
            st = redis_coord.take_token_state(scope, self.rate, self.capacity)
            if st is None:
                return None
            if st.get("maintenance"):
                wait_s = float(st.get("wait_s") or 0.0)
                self.maint_until = max(self.maint_until,
                                       time.monotonic() + max(0.0, wait_s))
                return wait_s
            # No maintenance in force per the shared truth → drop any stale local mirror.
            self.maint_until = 0.0
            if st.get("paused"):
                wait_s = float(st.get("wait_s") or 0.0)
                target = time.monotonic() + wait_s
                if target > self.pause_until:
                    self.pause_until = target
                return wait_s
            return float(st.get("wait_s") or 0.0)
        except Exception:  # noqa: BLE001 — fail-OPEN to the local bucket
            return None

    def _try_acquire(self) -> tuple[bool, float]:
        """Refill then attempt to take one token. Returns (acquired, seconds_until_next)."""
        with self._lock:
            now = time.monotonic()
            # LAYER 2 HARD PAUSE: a provider that reported the request budget exhausted
            # (remaining <= 1) hands out NO tokens until its window resets. On expiry we
            # grant a single burst token — we know the reset, not the fresh grant, so one
            # is the safe amount (the next response's headers re-derive the real budget).
            if self.pause_until and now < self.pause_until:
                return False, min(self.pause_until - now, _RATE_MAX_SLICE)
            if self.pause_until:
                self.pause_until = 0.0
                self.last = now
                self.tokens = min(self.capacity, max(self.tokens, 1.0))
            # ACCOUNT-WIDE BUDGET (cross-process). This bucket is per-PROCESS, and the API
            # container runs uvicorn AND the re_embedder against the same account — so the
            # local balance alone under-counts the account's real outbound by the number of
            # processes. When the shared budget has an opinion it is AUTHORITATIVE, because
            # it is the only view that sees every process. None = no opinion (disabled /
            # Redis down) → fall through to the local balance, i.e. today's behaviour.
            shared_wait = self._shared_budget_wait()
            if shared_wait is not None:
                if shared_wait <= 0.0:
                    return True, 0.0
                return False, min(shared_wait, _RATE_MAX_SLICE)
            elapsed = now - self.last
            if elapsed > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            deficit = 1.0 - self.tokens
            wait = deficit / self.rate if self.rate > 0 else _RATE_MAX_SLICE
            return False, wait

    def pause_for(self, seconds: float) -> None:
        """Hold the bucket (no tokens at all) for ``seconds`` from now (monotonic).

        ``max`` semantics so repeated observations only ever EXTEND the pause, never cut
        it short (a second exhausted-window report cannot let an earlier pause expire).

        WRITE-THROUGH: a pause learned here is also published to the shared Redis bucket
        for this brain, so EVERY process is throttled by it — not just the one that saw
        the 429. Publish happens AFTER the lock (never hold the pacing lock across a
        network round trip) and is swallowed on any failure: a pause that cannot be
        shared still applies locally, and a live call is never lost to it.
        """
        with self._lock:
            target = time.monotonic() + max(seconds, 0.0)
            self.pause_until = max(self.pause_until, target)
        self._publish_shared_pause(seconds)

    def _publish_shared_pause(self, seconds: float) -> None:
        """Mirror a hard pause into the shared bucket hash. Never raises."""
        scope = self.coord_scope
        if scope is None:
            return
        try:
            from src.api import redis_coord
            redis_coord.publish_brain_pause(scope, seconds)
        except Exception:  # noqa: BLE001 — sharing a pause must never break a live call
            pass

    def clear_pause(self) -> None:
        """Drop any provider hard-pause. Used on an endpoint/brain switch: a pause was
        learned from the OLD provider's exhausted budget and must not delay the NEW
        provider's first call (their budgets are unrelated)."""
        with self._lock:
            self.pause_until = 0.0

    def set_learner_floor(self, rps: float, source: str = "",
                          provenance: str = FLOOR_MEASURED, note: str = "",
                          capacity: float = 0.0) -> bool:
        """Persist the learned/assumed ceiling this bucket may NOT exceed on acquire.

        Each ``_observe_rate_headers`` call OVERWRITES the floor (that is what lets the
        learner recover when a tier/key improves — new headers raise the floor). ``0.0``
        means no floor (nothing learned yet). The floor is what survives the acquire-time
        re-clamp in ``_get_rate_bucket``: without it a learned 0.025 rps would be bumped
        straight back to the 4.5 rps registry clamp on the next acquire.

        ``source`` tags WHICH provider endpoint the floor was learned from (the endpoint
        tag, e.g. the base_url). ``_get_rate_bucket`` clears a floor whose source no longer
        matches the endpoint THIS call will hit — a brain switch must not inherit a
        provider's Free-Trial floor (see the module doc on stale clamps).

        PRECEDENCE (strict, non-invertible). ``provenance`` ranks the EVIDENCE behind the
        value (_FLOOR_RANK): measured > assumed-from-table > assumed-blind-backoff > none.
        A write is REFUSED when its rank is BELOW the rank already in force, which is what
        makes "a measured value always beats the table, and the table never overwrites
        something learned" a property of the code rather than a convention about call order.
        A same-rank write is accepted (that is how the learner recovers on a tier upgrade,
        and how a repeated 429 tightens an assumption further).

        Returns True when the floor was written, False when it was refused.

        WRITE-THROUGH: an accepted floor is also published to the shared Redis bucket for
        this brain (with the SAME rank, so the Lua refuses an assumption-over-measurement
        overwrite even when two processes race). Publish happens AFTER the lock and is
        swallowed on any failure — the local floor still paces THIS process, and the take
        script re-clamps everyone as soon as sharing works again.
        """
        written = False
        candidate = 0.0
        with self._lock:
            try:
                candidate = max(0.0, float(rps))
            except (ValueError, TypeError):
                candidate = 0.0
            # A CLEAR (rps<=0) is unconditional: it is how an endpoint switch drops a stale
            # floor, and a stale MEASURED floor from the previous provider must not become
            # un-clearable just because it outranks everything.
            if candidate <= 0.0:
                self.learner_floor_rps = 0.0
                self.learner_source = ""
                self.floor_provenance = FLOOR_NONE
                self.floor_note = ""
                self.floor_capacity = 0.0
                self.floor_set_at = 0.0
                written = True
            else:
                incoming = _FLOOR_RANK.get(provenance, 0)
                current = _FLOOR_RANK.get(self.floor_provenance, 0)
                if incoming < current:
                    written = False
                else:
                    self.learner_floor_rps = candidate
                    self.learner_source = str(source) if source else ""
                    self.floor_provenance = provenance
                    self.floor_note = str(note or "")
                    # The burst allowance learned WITH this floor (the provider's
                    # per-window request count, e.g. 5-per-minute). 0 = not learned —
                    # capacity then falls back to the legacy ~1s-of-tokens default.
                    try:
                        self.floor_capacity = max(0.0, float(capacity or 0.0))
                    except (ValueError, TypeError):
                        self.floor_capacity = 0.0
                    self.floor_set_at = time.monotonic()
                    written = True
        if written:
            self._publish_shared_floor(candidate, provenance)
        return written

    def relax_assumed_floor(self, ceiling_rps: float) -> Optional[tuple]:
        """AIMD recovery: ease an ASSUMED floor back up after a quiet, 429-free interval.

        Returns ``(prev_rps, new_rps, cleared)`` when the floor moved, else None.

        SAFETY, in the order it matters:
          * ONLY ``_ASSUMED_PROVENANCES`` (blind back-off / published-table) relax. A
            MEASURED or CALIBRATED floor is a FACT and is never touched here — the
            precedence contract in ``set_learner_floor`` is unchanged and uninverted.
          * ``ceiling_rps`` is the ALREADY-CLAMPED effective rate the caller resolved
            (per-brain max ∩ ``LLM_MAX_RPM`` ∩ published provider clamp × headroom), so
            recovery can only ever walk back an assumption WE invented. It can never raise
            the pace past a documented provider figure or past what the operator allowed.
          * The clock is the last floor WRITE, and for an assumed floor every write is a
            429-driven tightening — so the interval is genuinely "seconds since this
            provider last refused us", not merely "seconds since we last looked".
          * Reaching the ceiling CLEARS the floor rather than pinning it there: with no
            assumption left in force the tenant-facing disclaimer stops claiming one, and
            the next 429 starts a fresh back-off from the real effective rate.
        """
        interval = _rate_fallback_recovery_s()
        if interval <= 0.0:
            return None
        try:
            ceiling = float(ceiling_rps)
        except (ValueError, TypeError):
            return None
        if ceiling <= 0.0:
            return None
        # Snapshot under the lock, then RELEASE it — set_learner_floor takes the same
        # non-reentrant lock, so calling it while held would deadlock the acquire path.
        with self._lock:
            if self.floor_provenance not in _ASSUMED_PROVENANCES:
                return None
            prev = self.learner_floor_rps
            if prev <= 0.0 or not self.floor_set_at:
                return None
            if (time.monotonic() - self.floor_set_at) < interval:
                return None
            provenance = self.floor_provenance
            source = self.learner_source
            capacity = self.floor_capacity
        candidate = prev * _rate_fallback_recovery_factor()
        if candidate >= ceiling:
            # Fully recovered — drop the assumption entirely (see the docstring).
            self.set_learner_floor(0.0, "")
            return (prev, ceiling, True)
        note = rate_assumption_disclaimer(candidate * 60.0, provenance, None)
        if not self.set_learner_floor(candidate, source, provenance=provenance,
                                      note=note, capacity=capacity):
            return None
        return (prev, candidate, False)

    def _publish_shared_floor(self, rps: float, provenance: str) -> None:
        """Mirror the learned/assumed ceiling (and its burst allowance) into the shared
        bucket hash, so the other process in this container adopts the same burst shape.
        Never raises."""
        scope = self.coord_scope
        if scope is None:
            return
        try:
            from src.api import redis_coord
            redis_coord.publish_brain_floor(
                scope, rps, _FLOOR_RANK.get(provenance, 0), provenance,
                capacity=self.floor_capacity, source_tag=self.learner_source)
        except Exception:  # noqa: BLE001 — sharing a floor must never break a live call
            pass

    def _pause_remaining(self) -> float:
        """Seconds left in a provider hard-pause (0.0 when not paused)."""
        with self._lock:
            if self.pause_until:
                now = time.monotonic()
                if now < self.pause_until:
                    return self.pause_until - now
            return 0.0

    def _maint_remaining(self) -> float:
        """Seconds left in the mirrored maintenance window (0.0 when none)."""
        with self._lock:
            if self.maint_until:
                now = time.monotonic()
                if now < self.maint_until:
                    return self.maint_until - now
            return 0.0

    def _extend_cap_for_hold(self, max_wait: float, cap: float) -> float:
        """ONE-SHOT cap extension for a hold (provider pause / maintenance window).

        The extension is the hold's FULL remaining at the moment it is first observed,
        clamped by the per-kind bound, and is then FROZEN for the rest of this acquire.
        Freezing is the whole point: a remaining-based cap recomputed every iteration
        shrinks as the hold runs down while ``waited`` only grows, so the fail-open would
        fire at the hold's MIDPOINT — measured, not theorized. A hold that ends early is
        detected by the very next take (granted → return), so the freeze never delays a
        released brain; a hold that GROWS past its adoption snapshot still fail-opens at
        the bounded cap, which is what the bound is for.
        """
        if cap != max_wait:
            return cap                      # already extended for this acquire
        extension = 0.0
        remaining = self._pause_remaining()
        if remaining > 0:
            # A PROVIDER PAUSE MAY NOT EXTEND PAST WHAT THE CALLER SAID IT COULD WAIT.
            # The caller passes `max_wait` as its budget; extending beyond it spends time the
            # caller does not have. Measured on production: the MCP recall path holds a 60s HTTP
            # timeout, this extension is bounded at 300s, and every recall parked ~70s and died
            # on the client timeout — the wait could never have paid off. The MAINTENANCE arm
            # below is deliberately NOT bounded this way: that hold is user-initiated, short,
            # and the UI that starts it promises other work will pause, so waiting it out is the
            # promised behaviour rather than a futile bet on a provider.
            _pause_ceiling = _RATE_PAUSE_EXTENSION_S if _park_past_budget() else max_wait
            extension = max(extension, min(remaining, _RATE_PAUSE_EXTENSION_S, _pause_ceiling))
        maint = self._maint_remaining()
        if maint > 0:
            extension = max(extension, min(maint, _RATE_MAINT_EXTENSION_S))
        return cap if extension <= 0 else max(max_wait, extension)

    def resync_rate(self, rate: float, capacity: float) -> bool:
        """Adapt the bucket to a NEW effective rate/capacity in place.

        Returns True when the rate actually changed (the caller logs it). The token
        balance is preserved but CLAMPED to the new capacity, so a live endpoint switch
        can only tighten a partly-drained bucket, never grant it extra burst. This is
        what makes a brain swap to a metered provider re-apply the recorded clamp
        WITHOUT a process restart — the per-key bucket no longer freezes its rate at
        creation time.
        """
        with self._lock:
            if abs(self.rate - rate) < 1e-9 and self.capacity == capacity:
                return False
            self.rate = rate
            self.capacity = capacity
            self.tokens = min(self.tokens, capacity)
            return True

    def _pause_outlasts_budget(self, cap: float, waited: float) -> bool:
        """True when a PROVIDER pause cannot possibly lift inside this acquire's remaining budget.

        Sleeping in 0.25s slices toward a deadline you can already prove you will not meet is not
        pacing, it is a hang with extra steps. When the pause has longer left than the budget has,
        the outcome is decided before the first sleep: the caller will reach its cap and either
        fail-open or defer. Doing that NOW returns the same answer without burning the caller's
        entire budget first — which on the production recall path was ~70s per call.

        MAINTENANCE IS EXEMPT ON PURPOSE. That hold is user-initiated, short and bounded, and the
        modal that starts it promises other LLM work will pause; a caller SHOULD wait it out. Only
        a provider pause — whose duration is the provider's to set, and which `pause_for` extends
        with max() semantics on every fresh 429 — is the unbounded bet.
        """
        if _park_past_budget():
            return False
        if self._maint_remaining() > 0:
            return False
        remaining = self._pause_remaining()
        if remaining <= 0:
            return False
        return remaining > max(0.0, cap - waited)

    def acquire_sync(self, max_wait: float, deferrable: bool = False) -> bool:
        """Take one token. Returns True when the caller may proceed.

        ``deferrable`` (BACKGROUND lane) inverts the cap behaviour: instead of failing
        open into a provider that is already refusing us, the acquire returns False and
        the caller SKIPS the work. Interactive callers are unchanged — they still
        fail-open so a user's request is never lost.
        """
        waited = 0.0
        cap = max_wait
        while True:
            ok, wait = self._try_acquire()
            if ok:
                return True
            # A hold (provider pause / maintenance window) may be adopted INSIDE
            # _try_acquire, i.e. after this loop started — extend the cap one-shot when
            # first observed (see _extend_cap_for_hold for why the extension is frozen).
            cap = self._extend_cap_for_hold(max_wait, cap)
            if waited >= cap or self._pause_outlasts_budget(cap, waited):
                reason = ("brain_maintenance" if self._maint_remaining() > 0
                          else "provider_paused" if self._pause_remaining() > 0
                          else "wedged")
                if deferrable:
                    _rate_background_deferred(waited, reason=reason)
                    return False
                _rate_wedged_failopen(waited, reason=reason)
                return True
            slice_s = min(wait, cap - waited, _RATE_MAX_SLICE)
            slice_s = max(slice_s, _RATE_MIN_SLICE)
            time.sleep(slice_s)
            waited += slice_s

    async def acquire_async(self, max_wait: float, deferrable: bool = False) -> bool:
        waited = 0.0
        cap = max_wait
        while True:
            ok, wait = self._try_acquire()
            if ok:
                return True
            cap = self._extend_cap_for_hold(max_wait, cap)   # same freeze as sync
            if waited >= cap or self._pause_outlasts_budget(cap, waited):
                reason = ("brain_maintenance" if self._maint_remaining() > 0
                          else "provider_paused" if self._pause_remaining() > 0
                          else "wedged")
                if deferrable:
                    _rate_background_deferred(waited, reason=reason)
                    return False
                _rate_wedged_failopen(waited, reason=reason)
                return True
            slice_s = min(wait, cap - waited, _RATE_MAX_SLICE)
            slice_s = max(slice_s, _RATE_MIN_SLICE)
            await asyncio.sleep(slice_s)
            waited += slice_s


def _rate_background_deferred(waited: float, reason: str = "wedged") -> None:
    """A BACKGROUND (deferrable) call yielded instead of firing uncapped.

    This is the healthy counterpart of ``_rate_wedged_failopen``: no user is waiting,
    so the correct answer to "no capacity" is to not make the request. Logged at INFO,
    not log_crit — a deferral is the system working, whereas a fail-open is the system
    knowingly exceeding a provider's pace to protect a user's request.
    """
    try:
        log.info("llm_rate_limit.background_deferred",
                 waited_seconds=round(waited, 2),
                 reason=reason,
                 note="deferrable upkeep yielded to the account's rate budget; it will "
                      "be retried on a later sweep. Interactive calls are unaffected.")
    except Exception:
        pass


def _rate_wedged_failopen(waited: float, reason: str = "wedged") -> None:
    """Fail-safe: a wedged limiter must never hang a call forever. Proceed + log_crit.

    ``reason`` distinguishes a genuinely wedged bucket (hit ``max_wait`` with no pause —
    the operator's cap fired) from a provider-PAUSED bucket whose wait exceeded even the
    bounded pause extension (``reason="provider_paused"`` — the pause itself was the cause,
    not a limiter malfunction) and from a MAINTENANCE window that outlasted its bounded
    extension (``reason="brain_maintenance"`` — a Detect-cap probe ran long; the caller
    proceeds uncapped rather than hang, never lost). Either way the call PROCEEDS uncapped,
    never lost.
    """
    try:
        from src.api.logging_config import log_crit
        if reason == "provider_paused":
            remediation = ("provider hard-pause exceeded the bounded pause-extension cap — "
                           "proceeding uncapped to avoid losing the call; check the "
                           "x-ratelimit reset/window headers the pause was derived from")
        elif reason == "brain_maintenance":
            remediation = ("brain maintenance window (detect-cap probe) exceeded the "
                           "bounded maintenance-extension cap — proceeding uncapped to "
                           "avoid losing the call; the window self-expires at its "
                           "timestamp if the probe's release never landed")
        else:
            remediation = ("rate limiter exceeded LLM_RATE_MAX_WAIT_S — proceeding "
                           "uncapped to avoid losing the call; check LLM_MAX_RPM / clock")
        log_crit(log, "llm_rate_limit.wedged_failopen",
                 waited_seconds=round(waited, 2),
                 reason=reason,
                 remediation=remediation)
    except Exception:
        pass


# Registry of per-key buckets (rate/capacity captured at creation from live env).
_rate_buckets: dict[str, _TokenBucket] = {}
_rate_buckets_lock = threading.Lock()


def _coord_scope_for_rate_key(rate_key: str):
    """Map this module's bucket key onto a ``redis_coord.Scope`` for the shared budget.

    Three key shapes today: ``brain:<digest>`` (the BRAIN-scoped identity from
    ``_resolve_rate_limit_key`` — the load-bearing one), ``account:<uuid>`` (legacy SaaS,
    retained for back-compat with buckets created before the brain re-key), and
    ``url:<base_url>`` (single-tenant / env-path). Anything unrecognised, or any construction
    error, yields None → local bucket only, never a crash and never an unscoped Redis key.
    """
    try:
        from src.api import redis_coord
        raw = str(rate_key or "")
        if raw.startswith("brain:"):
            # Re-derive the Scope from the ident in the key (same module → token access is fine).
            # The brain scope is ALSO recomputable live via _resolve_brain_scope(); reconstructing
            # from the key keeps the in-process bucket identity and the Redis scope in lockstep
            # even if the ContextVar has cycled between the key-resolve and the coord-resolve.
            return redis_coord.Scope("brain", raw.split(":", 1)[1], redis_coord._SCOPE_TOKEN)
        if raw.startswith("account:"):
            return redis_coord.scope_for_account(raw.split(":", 1)[1])
        if raw.startswith("url:"):
            return redis_coord.scope_for_endpoint(raw.split(":", 1)[1])
    except Exception:  # noqa: BLE001 — an unscopable key must not break pacing
        return None
    return None


def _known_window_allowance(limits: Optional[dict]) -> Optional[float]:
    """The TIGHTEST per-minute request allowance we actually KNOW, or None when we know none.

    Two sources count as knowledge, and neither is a guess:
      * the per-brain ``max_rpm`` the tenant configured on their own brain, and
      * the provider registry's published rpm, for a RECOGNISED host.

    An unknown (self-hosted / LAN) host with no configured rpm returns None, so the caller
    keeps the legacy "~1s of tokens" default and nothing about that path changes.

    WHY THIS EXISTS. ``_rate_burst_capacity`` was written to take an allowance and use it as
    the bucket capacity, but the only callers that had one were the header-learning paths.
    A provider that answers 429 WITHOUT rate headers — z.ai does — never teaches us an
    allowance, so capacity fell to ``max(1, ceil(rate))`` = 1 while the rate itself was known
    perfectly well from the tenant's configured 5/min. Capacity 1 with a 12-second refill is
    not a token bucket, it is a metronome: it keeps the average and destroys the burst half.
    A conversational turn (REFRAME + N EXTRACT chunks) then cannot proceed as a unit, and the
    background growth sweep is `rate_deferred` on every pass — measured live, the classify
    climb never ran once.

    THIS DOES NOT WIDEN ANY LIMIT. The refill rate is untouched: five per minute stays five
    per minute, exactly as configured. What is restored is the burst the provider's own policy
    already permits — "5 requests per minute" allows all five in the first second. Same policy,
    correct shape. No headroom is added and no provider is special-cased.
    """
    vals: list[float] = []
    brain_rpm = _configured_brain_max_rpm()
    if brain_rpm:
        vals.append(float(brain_rpm))

    if limits and limits.get("known") and limits.get("rpm"):
        try:
            _r = float(limits["rpm"])
            if _r > 0:
                vals.append(_r)
        except (ValueError, TypeError):
            pass
    if not vals:
        return None
    # The OPERATOR's own ceiling BOUNDS an allowance we already have; it never CREATES one.
    # Omitting it was a real over-widening -- the registry publishes rpm 1000 for
    # Anthropic/Cerebras, so a deployment configured to 480 rpm was handed a 1000-permit
    # burst, refill obeying the operator while burst ignored him. But adding it as a source
    # is equally wrong: an unknown self-hosted host with no configured rpm must still return
    # None so the legacy ~1s-of-tokens default applies, exactly as this docstring promises.
    env_rpm = _global_env_max_rpm()
    if env_rpm and env_rpm > 0:
        vals.append(float(env_rpm))
    return min(vals)


def _get_rate_bucket(key: str, rate: float, limits: Optional[dict] = None,
                     endpoint_tag: str = "") -> _TokenBucket:
    """Fetch (or create) the per-key bucket. Rate is the ALREADY-CLAMPED effective rate.

    Logs once at creation with the resolved provider limits (and whether the rate was a
    conservative UNKNOWN-host fallback), per the registry spec.

    LIVE RE-SYNC: a pre-existing bucket whose rate differs from the CURRENT effective
    rate is adapted in place (``resync_rate``) instead of frozen at its creation-time
    clamp. CAPACITY re-syncs the same way, and must: capacity used to be decided ONLY at
    creation, so a bucket built before the tenant's brain was bound had no knowable
    allowance, took capacity=1, and kept that metronome for the whole process lifetime
    even after the allowance became knowable. The allowance is therefore re-resolved on
    every resync — learned burst first, else the configured/registry allowance. Without this, a brain/endpoint switch (e.g. a homelab → Cerebras move) leaves
    the old unknown-host rate in force and the provider's published limit never applies —
    the exact stale-clamp failure that produced per-minute 429s.

    LEARNER FLOOR: when the bucket carries a LAYER-2 learned floor (``learner_floor_rps``
    > 0 — set by ``_observe_rate_headers``), the acquire-time resync targets
    ``min(effective_rate, floor)`` instead of the raw effective rate. This is what makes
    the learned rate SURVIVE the next acquire: without it, a learned 0.025 rps would be
    bumped straight back to the 4.5 rps registry clamp, and the rate-learning half of
    Layer 2 would be dead in the live path.

    STALE-FLOOR CLEAR: a floor is tagged with the endpoint it was learned from
    (``learner_source``). When ``endpoint_tag`` differs from that source, the brain has
    switched providers (e.g. HelpDeskPro → DashScope) — the old floor is CLEARED so the
    new provider is not permanently under-paced by the previous provider's tier. The new
    endpoint re-learns its own profile from its first response.
    """
    with _rate_buckets_lock:
        bucket = _rate_buckets.get(key)
        if bucket is None:
            # Creation used to be deliberately 1-arg, on the reasoning that the allowance is
            # learned from headers later. That holds only for providers that SEND headers; one
            # that 429s bare never teaches us, and the bucket stays a metronome forever even
            # though the tenant told us their limit. Pass the allowance we already know.
            bucket = _TokenBucket(rate, _rate_burst_capacity(
                rate, _known_window_allowance(limits)))
            bucket.coord_scope = _coord_scope_for_rate_key(key)
            # SEED from the shared floor (cross-process adoption): the other process in
            # this container may already have learned this brain's rate AND burst
            # allowance. Without this, a restarted process re-metronomes to capacity=1
            # until its own first response teaches it — and on a paced brain that first
            # response can be a long time coming. Fail-safe: unreadable/absent → no seed.
            try:
                if bucket.coord_scope is not None:
                    from src.api import redis_coord as _rc
                    _shared = _rc.brain_floor_state(bucket.coord_scope)
                    _fsrc = str(_shared.get("floor_src") or "") if _shared else ""
                    # Seed ONLY when the floor's own endpoint tag (floor_src, persisted
                    # verbatim by the writer) MATCHES the endpoint being called now.
                    # Re-tagging with the CURRENT endpoint (the original seed bug) makes
                    # learner_source always match, so the stale-floor clear can NEVER
                    # fire and a brain switch + restart permanently under-paces the new
                    # provider (critic 2026-08-15). An UNTAGGED floor (old-build write,
                    # or a mid-publish read between the Lua floor write and the floor_src
                    # hset) also does NOT seed: with learner_source="" the clear's
                    # non-empty guard can never fire and the stale floor would be
                    # immortal — the same blast radius (critic confirmation pass). An
                    # unknown endpoint_tag (no endpoint resolvable) is the one exception:
                    # nothing to mismatch against, adopt and let the clear's own guard
                    # (which also requires a non-empty tag) arbitrate later.
                    if (_shared and _shared.get("floor_rps") and endpoint_tag
                            and _fsrc and _fsrc == endpoint_tag):
                        bucket.learner_floor_rps = float(_shared["floor_rps"])
                        bucket.learner_source = _fsrc
                        bucket.floor_provenance = _shared.get("provenance") or FLOOR_MEASURED
                        _fc = _shared.get("floor_cap")
                        bucket.floor_capacity = float(_fc) if _fc else 0.0
                        _seed_rate = min(rate, bucket.learner_floor_rps)
                        bucket.rate = _seed_rate
                        bucket.capacity = _rate_burst_capacity(
                            _seed_rate, bucket.floor_capacity or None)
                        bucket.tokens = min(bucket.tokens, bucket.capacity)
                    elif _shared and _shared.get("floor_rps"):
                        # A FLOOR EXISTS THAT WE MAY NOT ADOPT -- and capacity must still
                        # respect it. The seed above is deliberately conservative: it only
                        # adopts a floor whose floor_src matches this endpoint, so a stale
                        # provider's floor cannot under-pace a switched-to one. But the
                        # CREATE path had already resolved capacity from the pre-floor
                        # config/registry allowance, and nothing downstream revisited it --
                        # so a bucket built while a floor was in force started with the full
                        # allowance as its burst. Worse, floor_src is only ever written
                        # alongside a floor_cap, so a bare-429 back-off floor can NEVER
                        # match, which makes this the common path rather than the rare one.
                        #
                        # We do not adopt the rate (that would be the under-pacing bug this
                        # guard exists to prevent), but we do refuse to hand out a burst the
                        # allowance no longer justifies: something out there is being
                        # refused. Same invariant as every other capacity site -- once a
                        # floor is in force, only a burst learned WITH a floor may justify
                        # one, and here we have none.
                        bucket.capacity = _rate_burst_capacity(bucket.rate, None)
                        bucket.tokens = min(bucket.tokens, bucket.capacity)
            except Exception:  # noqa: BLE001 — seeding must never break bucket creation
                pass
            _rate_buckets[key] = bucket
            log.info("llm_rate_limit.bucket_created",
                     key=key,
                     effective_rps=round(rate, 3),
                     capacity=round(bucket.capacity, 3),
                     window_allowance=_known_window_allowance(limits),
                     provider_source=(limits or {}).get("source"),
                     provider_rpm=(limits or {}).get("rpm"),
                     provider_known=(limits or {}).get("known"),
                     note=("provider-clamped" if (limits or {}).get("known")
                           else "unknown-host: no hard clamp (self-host/homelab), "
                                "configured pace only"))
        else:
            prev_rate = bucket.rate
            if (endpoint_tag and bucket.learner_source
                    and endpoint_tag != bucket.learner_source):
                prev_source = bucket.learner_source
                bucket.set_learner_floor(0.0, "")
                bucket.clear_pause()
                log.info("llm_rate_limit.learner_floor_cleared",
                         key=key,
                         prev_learner_source=prev_source,
                         current_endpoint=endpoint_tag,
                         note="brain/endpoint switch — stale learned floor AND any pause "
                              "dropped; the new provider re-learns its own profile")
            # AIMD RECOVERY (see relax_assumed_floor). A blind back-off adopted against a
            # provider that 429s WITHOUT headers has no other way back: nothing measures it,
            # so nothing replaces it, and the pace stays pinned at LLM_RATE_429_MIN_RPM
            # forever. Bounded by `rate` — the already-clamped effective rate — so this can
            # only ever undo an assumption, never exceed a documented ceiling.
            _relaxed = bucket.relax_assumed_floor(rate)
            if _relaxed:
                _prev_rps, _new_rps, _cleared = _relaxed
                log.info("llm_rate_limit.assumed_floor_relaxed",
                         key=key,
                         prev_rpm=round(_prev_rps * 60.0, 2),
                         new_rpm=round(_new_rps * 60.0, 2),
                         cleared=_cleared,
                         quiet_seconds=_rate_fallback_recovery_s(),
                         note=("no 429 for the quiet interval — assumed ceiling fully "
                               "recovered to the configured effective rate and dropped"
                               if _cleared else
                               "no 429 for the quiet interval — easing the ASSUMED "
                               "ceiling back up (a 429 re-tightens it immediately)"))
            target = rate
            floor = bucket.learner_floor_rps
            floor_binding = bool(floor > 0 and floor < rate)
            if floor > 0:
                target = min(rate, floor)
            # floor_capacity: a burst allowance learned WITH the floor (provider's
            # per-window count). Without this the acquire-time resync would clobber the
            # learned burst (5) back to the ~1s-of-tokens default (1) — the metronome.
            # A BOUND FLOOR MAKES THE CONFIGURED ALLOWANCE STALE. My own retraction
            # commit stated that invariant and then applied it only to the shared Lua --
            # which, it turns out, never executes: the Lua clamp is guarded on
            # `rate > floor`, and this very function sets target = min(rate, floor), so by
            # the time _shared_budget_wait forwards self.rate the two are EQUAL and the
            # whole block is skipped. The capacity that actually reaches the provider is
            # the one computed here. Measured end-to-end: a learned 4.5-req/MINUTE ceiling
            # still granting 480 instant permits.
            #
            # So the invariant belongs here, where it bites: once a floor is in force only
            # a burst learned WITH that floor (floor_capacity) may justify one. The
            # pre-floor config/registry allowance is evidence from before the provider
            # started refusing us, and it is not admissible after.
            _burst = bucket.floor_capacity or (
                None if floor > 0 else _known_window_allowance(limits))
            if bucket.resync_rate(target, _rate_burst_capacity(target, _burst)):
                log.info("llm_rate_limit.bucket_resynced",
                         key=key,
                         prev_rps=round(prev_rate, 3),
                         effective_rps=round(target, 3),
                         capacity=round(bucket.capacity, 3),
                         window_allowance=_known_window_allowance(limits),
                         learner_floor_rps=round(floor, 3),
                         floor_binding=floor_binding,
                         provider_source=(limits or {}).get("source"),
                         provider_rpm=(limits or {}).get("rpm"),
                         provider_known=(limits or {}).get("known"),
                         note=("provider-clamped" if (limits or {}).get("known")
                               else "unknown-host: configured pace only — endpoint "
                                    "changed, clamp re-derived"))
        return bucket


def _rate_endpoint_tag(base_url: Optional[str]) -> str:
    """Stable tag for the rate-limit PROFILE an endpoint (base_url) exposes.

    The learner floor is tagged with this so a brain switch to a DIFFERENT provider
    endpoint cannot inherit the previous provider's learned floor (see the stale-floor
    clear in ``_get_rate_bucket``). Normalized (stripped, trailing slash removed); empty
    when the endpoint is unknown → the acquire path then never clears a floor (fail-safe:
    unknown endpoints keep whatever they learned).
    """
    if not base_url:
        return ""
    try:
        return str(base_url).strip().rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _current_llm_config_or_none() -> Optional[dict]:
    """Lazy, fail-safe read of the per-request tenant brain config (SaaS).

    Isolated behind this indirection so (a) the SaaS import stays lazy and (b) tests
    can monkeypatch the key source without importing the SaaS stack.
    SAAS off / import fault → None (→ the global env key path).
    """
    try:
        from saas.llm_context import current_llm_config
        return current_llm_config()
    except Exception:
        return None


def _resolve_rate_limit_key() -> Optional[str]:
    """Derive the stable per-BRAIN bucket key for THIS call (see module doc).

    Brain-scoped via ``_resolve_brain_scope()`` so the key follows the BRAIN the call is
    routed to, not the account row. This is the fix for the house-account leak: two
    individuals on the "Singles" account with their OWN brains used to resolve to the same
    ``account:<acct>`` key and SHARE a budget; they now resolve to two ``brain:`` keys.

    Returns None when no key can be resolved → the gate no-ops (fail-safe).
    """
    brain = _resolve_brain_scope()
    if brain is not None:
        return f"brain:{brain.ident}"
    # Global env path (SAAS off / no tenant cfg / no-brain sentinel): key by the endpoint host.
    base = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"url:{base}"
    try:
        eps = _get_endpoint_list()
        if eps and eps[0]:
            return f"url:{eps[0].rstrip('/')}"
    except Exception:
        pass
    return None


def apply_rate_limit_sync(operation: str = "DEFAULT") -> bool:
    """Pace one outbound sync LLM REQUEST against its account bucket.

    Returns True when the caller may proceed, False ONLY when a BACKGROUND (deferrable)
    call found no capacity within the cap — see ``src.api.llm_lane``. Interactive callers
    always get True, preserving the never-lose-a-user's-call fail-open.

    Disabled, or no resolvable key → True (byte-for-byte current path).
    Any limiter error → True (never lose a call because the limiter itself broke).

    NOTE: this paces ONE HTTP REQUEST, not one logical call. It is invoked per ATTEMPT
    inside the retry loop — a call that retries three times sends three requests and must
    therefore spend three tokens, or the bucket silently under-counts real outbound load
    by the retry factor.
    """
    if not _rate_limit_enabled():
        return True
    try:
        key = _resolve_rate_limit_key()
        if not key:
            return True
        base_url, model = _resolve_endpoint_and_model()
        # BACKGROUND daily budget, probe BEFORE the (possibly 30s) minute-token wait:
        # an exhausted day defers instantly instead of wedging then failing open.
        if llm_lane.is_background() and not _daily_gate(key, base_url, spend=False):
            return False
        rate, limits = _effective_rate_per_sec(base_url, model)
        granted = _get_rate_bucket(key, rate, limits,
                                   _rate_endpoint_tag(base_url)).acquire_sync(
                                       _rate_max_wait_s(), deferrable=llm_lane.is_background())
        if granted:
            # Counts for BOTH lanes (the daily arithmetic needs the real usage);
            # defers only when the caller is background.
            return _daily_gate(key, base_url, spend=True)
        return granted
    except Exception as e:
        log.warning("llm_rate_limit.error_failopen",
                    operation=operation, mode="sync", error=str(e)[:200])
        return True


async def apply_rate_limit_async(operation: str = "DEFAULT") -> bool:
    """Pace one outbound async LLM REQUEST against its account bucket. See the sync twin."""
    if not _rate_limit_enabled():
        return True
    try:
        key = _resolve_rate_limit_key()
        if not key:
            return True
        base_url, model = _resolve_endpoint_and_model()
        # BACKGROUND daily budget, probe BEFORE the (possibly 30s) minute-token wait —
        # same instant-defer requirement as the sync twin.
        if llm_lane.is_background() and not _daily_gate(key, base_url, spend=False):
            return False
        rate, limits = _effective_rate_per_sec(base_url, model)
        granted = await _get_rate_bucket(key, rate, limits,
                                         _rate_endpoint_tag(base_url)).acquire_async(
                                             _rate_max_wait_s(),
                                             deferrable=llm_lane.is_background())
        if granted:
            # Counts for BOTH lanes (the daily arithmetic needs the real usage);
            # defers only when the caller is background.
            return _daily_gate(key, base_url, spend=True)
        return granted
    except Exception as e:
        log.warning("llm_rate_limit.error_failopen",
                    operation=operation, mode="async", error=str(e)[:200])
        return True


def reset_rate_limiter() -> None:
    """Clear all buckets (TEST/diagnostic hook — lets a new rate/env take effect)."""
    with _rate_buckets_lock:
        _rate_buckets.clear()


def _daily_gate(key: str, base_url: Optional[str], spend: bool) -> bool:
    """Daily-request budget: BACKGROUND defers against it; INTERACTIVE only COUNTS.

    The minute token bucket (with its headroom) is BURST-SMOOTHING — right for RPM/TPM.
    Applied to a daily quota it only moves the wall from 17:00 to 16:00 and then stalls
    hard. A background sweep against a free tier (50–1,000 RPD class) must be PACED:
    a Redis-backed budget that the background lane defers against, all day, so the sweep
    is never refused by the provider and never fires uncapped (dprompt-155 §2.3; the
    module is src/api/daily_budget.py).

    Cap provenance, metadata-driven (constraint 5): the ``rpd`` VALUE on this host's
    ``_RATE_429_FALLBACK`` entry — the free-tier table, whose doctrine is most-conservative
    — overridable by the operator via ``FL_DAILY_BUDGET_REQ``. A provider that publishes no
    daily request figure (e.g. the Cerebras trial, which caps tokens/day, not requests)
    gets NO daily gate: unknown stays unknown, never an invented number. Over-tightening a
    paid tier's BACKGROUND work is the safe direction (headers/env can correct it); the
    INTERACTIVE lane is exempt entirely — a user is never refused (that is the lane split's
    one inviolable rule).

    ``spend=False`` is the pre-wait PROBE (peek, no spend): a background call against an
    exhausted day must defer IMMEDIATELY, not sleep out LLM_RATE_MAX_WAIT_S first — the
    30s-then-fail-open wedge is the exact defect this gate closes. ``spend=True`` takes
    atomically AFTER the minute token granted, so a request that never fires (minute-defer)
    does not burn daily budget; the rare lost race (another process takes the last daily
    slot between probe and spend) defers, which is the safe direction.
    """
    try:
        from src.api import daily_budget as _DB
        entry = _fallback_entry_for(base_url)
        # daily_cap_from_limits applies the precedence env(FL_DAILY_BUDGET_REQ) > table rpd.
        cap = _DB.daily_cap_from_limits({"rpd": (entry or {}).get("rpd")})
        scope = _coord_scope_for_rate_key(key)
        if scope is None:
            return True
        window_s = _DB.daily_window_s()
        # LEARNED day cap (the provider's own day-window header, e.g.
        # x-ratelimit-limit-requests-day): a MEASUREMENT outranks the table value but
        # NOT the operator env knob — precedence env > learned > table, same rank
        # doctrine as the minute floor. The learned window rides along (a provider
        # whose day window isn't 86400s must not be forced onto one).
        if not os.environ.get("FL_DAILY_BUDGET_REQ"):
            _learned = _DB.learned_day_cap(scope)
            if _learned and _learned.get("day_cap"):
                cap = int(_learned["day_cap"])
                _w = int(_learned.get("day_window_s") or 0)
                if _w > 0:
                    window_s = _w
        if not cap:
            return True
        if not spend:
            peek = _DB.peek_daily(scope)
            # Defer ONLY while the stored window is live AND the CURRENT cap is spent.
            # Comparing peek["remaining"] (computed from the STORED cap) would ignore an
            # operator cap RAISE for up to a whole window (critic pass 2: FL_DAILY_BUDGET_REQ
            # 50→200 at used=50 still deferred until reset) — `used >= cap` with the
            # caller's CURRENT cap lets a raised cap take effect on the very next call,
            # and the atomic take re-persists it. Once now >= reset_ms the call falls
            # through to the minute acquire and the atomic take, whose Lua is the ONLY
            # code that rolls the window (pass 1: gating on a stale `remaining` alone
            # deadlocked the budget at 0 until the 2×-window Redis TTL expired).
            if (peek and int(peek.get("used", 0)) >= int(cap)
                    and int(time.time() * 1000) < int(peek.get("reset_ms", 0))):
                _rate_background_deferred(0.0, reason="daily_budget_exhausted")
                return False
            return True
        # force=not background (owner directive 2026-08-15): an INTERACTIVE spend COUNTS
        # into the daily hash even past cap (used may exceed cap) and NEVER deflects the
        # call — without the force flag the Lua refuses over-cap takes without
        # incrementing, so interactive traffic on an exhausted day was invisible to the
        # shared arithmetic, under-reporting the account and letting background pace
        # into a wall interactive had already spent (joint breach → provider day-window
        # 429 → pause_for(86400) on every lane). Background keeps force=False: a refused
        # take defers, which is the pacer working.
        take = _DB.try_take_daily(scope, cap, window_s,
                                  force=not llm_lane.is_background())
        if not take.get("ok") and llm_lane.is_background():
            _rate_background_deferred(0.0, reason="daily_budget_exhausted")
            return False
        return True
    except Exception:  # noqa: BLE001 — the daily gate must never break a live call
        return True


def _apply_tenant_brain(model: str, max_tokens: int):
    """Resolve any per-tenant BYO engine-brain override for this call.

    Returns ``(breaker, model, max_tokens, extra_params)``:
    - SAAS off / no override → ``(_llm_circuit_breaker, model, max_tokens, {})`` — the
      global path, byte-for-byte unchanged (``current_llm_config`` returns None instantly
      when SAAS_MODE is off; zero DB, zero behavior change).
    - override present → the tenant's model, its per-account breaker, and its advanced
      ``extra_params`` (temperature/top_p/top_k/penalties). The EXTRACT max_tokens FLOOR is
      preserved: ``extra_params.max_tokens`` may CLAMP UP the op budget but never below it.
      ``stream`` can never be flipped (stream=false is load-bearing).
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
    except Exception:
        cfg = None
    if not cfg:
        return _llm_circuit_breaker, model, max_tokens, {}

    breaker = _breaker_for(cfg.get("account_id"))
    tenant_model = cfg.get("model") or model
    extra = dict(cfg.get("extra_params") or {})

    # max_tokens floor — clamp UP only, never below the operation's resolved budget.
    ep_max = extra.pop("max_tokens", None)
    if ep_max is not None:
        try:
            max_tokens = max(int(max_tokens), int(ep_max))
        except Exception:
            pass
    extra.pop("stream", None)  # stream=false is CRITICAL — callers/params cannot flip it
    return breaker, tenant_model, max_tokens, extra


def _saas_brain_blocked() -> bool:
    """True iff a COST-SAFE FAIL-CLOSED no-brain sentinel is bound for this request/tenant.

    SaaS (Gap-B) cost guard: when SAAS_MODE is on and the resolved tenant account has NO brain
    configured, the request seam binds a ``{"no_brain": True}`` sentinel. Every LLM entry point
    checks this and returns its SAFE EMPTY DEFAULT instead of spending on the process-global
    env/cloud brain — zero external spend on an unconfigured tenant (critical on the CPU-only
    SaaS host). SAAS off → no sentinel is ever bound → always False → byte-for-byte unchanged.
    Fail-safe: any error → False (never blocks a call spuriously).
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
        return bool(cfg and cfg.get("no_brain"))
    except Exception:
        return False


def _saas_brain_unavailable() -> bool:
    """True iff, in SaaS mode, NO usable tenant brain resolves for THIS call.

    Superset of ``_saas_brain_blocked``: fail-closed on BOTH
      (a) the explicit ``{"no_brain": True}`` sentinel (a real account with no brain), AND
      (b) a ``None`` snapshot — the tenant ContextVar/schema was LOST or never bound
          (typically across an async boundary or a detached background task).

    Case (b) is the divergence bug: on a ``None`` snapshot ``get_backend_endpoint`` and
    ``get_llm_headers`` INDEPENDENTLY fall through to the process-global env brain
    (``LLM_BASE_URL`` / ``LLM_API_KEY``). On the CPU-only SaaS host those env values are a
    MISMATCHED pair (a plausible base_url, the wrong/empty key) → a 401 retry-storm that
    hangs the pipeline — and, worse, in multi-tenant it could spend on the WRONG brain.
    The documented SaaS contract is absolute: every LLM op rides the tenant's OWN brain or
    it does not run — there is NO legitimate global default in SaaS mode. So a ``None``
    snapshot here means "tenant context is not established" → fail-closed (safe empty
    default, ZERO network call), never the env brain.

    SAAS off → always False (byte-for-byte today's env path). Fail-safe: any error → False
    (an import/DB fault must not spuriously block a non-SaaS call).
    """
    try:
        from saas.config import saas_mode_enabled
        if not saas_mode_enabled():
            return False
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
        # None (lost/absent tenant context) OR the explicit no-brain sentinel → fail-closed.
        return cfg is None or bool(cfg.get("no_brain"))
    except Exception:
        return False


def _tenant_egress_blocked() -> bool:
    """SSRF re-check: True iff a tenant BYO-brain endpoint is bound and FAILS the egress guard.

    Closes the DNS-rebinding TOCTOU gap (security audit HIGH #2). The tenant endpoint URL is
    egress-validated at save/resolve time, but the stored endpoint was previously used at
    inference with NO re-check — a tenant could save a benign public URL, then re-point its DNS
    to ``169.254.169.254`` / an RFC1918 internal service (redis/qdrant/postgres/the unauth
    ``api:8000`` write path) for the real request. This re-resolves + re-vets the tenant
    endpoint immediately before EVERY outbound call, so validation and connect see (near-)the
    same DNS state — shrinking the rebind window from "hours since save" to "milliseconds
    before connect".

    ONLY the per-tenant BYO endpoint is gated — the operator's own global env brain
    (``LLM_BASE_URL`` with no tenant cfg) is trusted and may legitimately be a LAN/internal host,
    so it is NEVER egress-checked here (checking it would break the core deployment). SAAS off /
    no tenant cfg / no-brain sentinel → always False (byte-for-byte today's path).

    Fail behavior: a genuine ``EgressBlocked`` (rebound-to-private / unresolvable) → True
    (fail-closed, abort the call). An import failure of the SaaS egress module → False (never
    spuriously block a non-SaaS call — the module is only present in SaaS builds).
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
    except Exception:
        return False
    if not cfg or cfg.get("no_brain"):
        return False
    base_url = cfg.get("base_url")
    if not base_url:
        return False
    try:
        from saas.llm_config import validate_egress_url
    except Exception:
        # SaaS egress module unavailable (public core / import fault) → do not spuriously block.
        return False
    try:
        validate_egress_url(base_url)
        emb = cfg.get("embedding_url")
        if emb and str(emb).strip():
            validate_egress_url(emb)
        return False
    except Exception:
        # EgressBlocked (rebound to a private/reserved IP) or unresolvable → fail-closed.
        return True


# ──────────────────────────────────────────────────────────────────────────────
# HELPER: _get_endpoint_list()
# ──────────────────────────────────────────────────────────────────────────────

def _tenant_byo_cfg() -> Optional[dict]:
    """The bound tenant BYO-brain config, or None (SaaS off / no tenant cfg / no-brain).

    This is the discriminator for WHICH client carries an outbound LLM request: a tenant
    BYO endpoint is attacker-controllable input and MUST ride the pinned guarded transport
    (``saas.llm_config.shared_guarded_client``); the operator's own env brain (no tenant cfg)
    keeps the legacy client byte-for-byte, because it may legitimately be a LAN host.
    """
    try:
        from saas.llm_context import current_llm_config
        cfg = current_llm_config()
    except Exception:
        return None
    if not cfg or cfg.get("no_brain") or not cfg.get("base_url"):
        return None
    return cfg


def _is_egress_blocked(exc: BaseException) -> bool:
    """True iff ``exc`` is the SaaS egress refusal (``saas.llm_config.EgressBlocked``)."""
    try:
        from saas.llm_config import EgressBlocked
    except Exception:
        return False
    return isinstance(exc, EgressBlocked)


# ── CONNECT/READ SPLIT (BUG-brain-connect-timeout-burns-full-budget-20261026) ────────
# A per-request timeout that arrives at httpx as a bare number sets the CONNECT phase to
# the FULL read budget, so a host that will never answer a SYN was waited on for the whole
# per-op budget (90s REFRAME / 120s NL_FILL_BATCH / 180s atomic_subject_resolution measured
# live against a SYN-blackholed brain in the bug; at today's 30s EXTRACT default that is
# 30s per attempt × 3 retries ≈ 93s per call — measured), and the per-lane breaker needed
# threshold=25 failures (~37 min) to open. A CONNECTED-but-slow brain still owns the full
# read budget below; only the dial-up phase is shortened. Rollback lever: set
# LLM_CONNECT_TIMEOUT_S high (clamp 60s) — or split_connect_timeout is skipped only for
# None (a caller that explicitly asked for no timeout keeps exactly that).


def llm_connect_timeout_s() -> float:
    """The connect-phase budget in seconds: env ``LLM_CONNECT_TIMEOUT_S``, default 5.0.

    The bug file's fix direction measures a dead host as detectable in ~3-5s; 5s also
    absorbs a TLS handshake on a slow-but-alive brain. Clamped to [0.5, 60] — a nonsense
    env value must neither kill the floor nor quietly disable the split (the
    ``LLMTimeouts`` rule: env parse error → default).
    """
    raw = os.environ.get("LLM_CONNECT_TIMEOUT_S", "5.0")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 5.0
    return min(max(v, 0.5), 60.0)


def split_connect_timeout(timeout):
    """Normalize a per-request LLM timeout into an ``httpx.Timeout`` with a SHORT connect.

    - ``None`` passes through (the caller explicitly asked for no timeout).
    - An ``httpx.Timeout`` keeps its read/write/pool phases verbatim; its connect phase is
      shortened to :func:`llm_connect_timeout_s` but NEVER WIDENED — a caller that asked
      for a tighter connect (the doc-lane probe at ``embedder.py`` ``connect=2.0``) keeps it.
    - A bare number stays the read/write/pool budget; connect is clamped to
      ``min(knob, budget)`` (a 0.01s test budget must not grow a 5s connect).
    """
    if timeout is None:
        return None
    _connect = llm_connect_timeout_s()
    if isinstance(timeout, httpx.Timeout):
        # httpx stores an unset phase as None (read=None means unbounded) — an unset
        # connect phase is brought DOWN to the knob, never left unbounded.
        _existing = timeout.connect
        return httpx.Timeout(timeout.read, write=timeout.write, pool=timeout.pool,
                             connect=_connect if _existing is None else min(_connect, _existing))
    _budget = float(timeout)
    return httpx.Timeout(_budget, connect=min(_connect, _budget))


def _post_llm_sync(default_client, url: str, *, json, headers, timeout):
    """THE sync POST seam for every outbound LLM request.

    Tenant BYO endpoint bound → the resolve→vet→PIN guarded client (call-time SSRF guard:
    every A/AAAA answer vetted against ``SAAS_LLM_ALLOWED_EGRESS`` + the private/link-local/
    loopback/metadata set, the connection dialled on the vetted address, redirects refused —
    see ``saas/llm_config.py`` "CALL-TIME egress"). A refusal raises ``EgressBlocked`` before
    any socket opens; the call sites turn it into a loud non-answer, never a retry.

    No tenant cfg → the legacy path (``llm_source_ip.post_sync`` over ``default_client``),
    unchanged. Source-IP spreading (``LLM_SOURCE_IPS``) applies to the legacy path only: the
    pinned transport dials a vetted DESTINATION and does not rotate the SOURCE.

    CONNECT/READ SPLIT (2026-10-26): ``timeout`` is normalized by
    :func:`split_connect_timeout` before it reaches ANY branch — bare-number budgets used
    to make a SYN-blackholed host burn the whole read budget in the connect phase.
    """
    timeout = split_connect_timeout(timeout)
    if _tenant_byo_cfg() is not None:
        from saas.llm_config import shared_guarded_client
        return shared_guarded_client().post(url, json=json, headers=headers, timeout=timeout)
    return llm_source_ip.post_sync(default_client, url, json=json, headers=headers,
                                   timeout=timeout)


async def _post_llm_async(default_client, url: str, *, json, headers, timeout):
    """Async twin of :func:`_post_llm_sync` — same discriminator, same guarded transport,
    same connect/read split."""
    timeout = split_connect_timeout(timeout)
    if _tenant_byo_cfg() is not None:
        from saas.llm_config import shared_guarded_async_client
        return await shared_guarded_async_client().post(url, json=json, headers=headers,
                                                        timeout=timeout)
    return await llm_source_ip.post_async(default_client, url, json=json, headers=headers,
                                          timeout=timeout)


def _egress_refusal(exc: BaseException, operation: str, user_id: str, mode: str) -> None:
    """Loud, non-retried book-keeping for a call-time egress refusal (fail-closed)."""
    log.critical("llm_call.tenant_egress_blocked_at_call", user_id=user_id,
                 operation=operation, mode=mode, reason=str(exc)[:200])
    _record_llm_nonanswer(operation, "tenant_egress_blocked")
    _emit_brain_nonanswer(user_id, "tenant_egress_blocked")


def _get_endpoint_list() -> list[str]:
    """
    Priority-ordered list of LLM endpoint URLs.

    When LLM_BACKEND_TYPE + LLM_BASE_URL are both set, the type-resolved
    endpoint is authoritative and returned alone (no legacy fallback chain).
    Otherwise, delegates to src.api.llm_client.get_endpoint_list() for the
    legacy priority chain (OPENWEBUI_INTERNAL_URL → OPENWEBUI_URL → QWEN_API_URL
    → hardcoded fallbacks).

    Returns:
        list[str] — ordered endpoints, ready to POST to.
    """
    from src.api.llm_client import get_backend_endpoint, get_endpoint_list
    typed = get_backend_endpoint()
    if typed:
        # LLM_BASE_URL is set — type-resolved endpoint is authoritative
        return [typed]
    # Legacy fallback chain (full backward compat when LLM_BASE_URL not set)
    return get_endpoint_list()


# ──────────────────────────────────────────────────────────────────────────────
# CONTEXT-WINDOW GUARD (token-budget seam) — never knowingly send an over-window request
# ──────────────────────────────────────────────────────────────────────────────
# A request larger than the served model's context window is REJECTED, not inferred.
# llama.cpp returns HTTP 400 (`ERROR_TYPE_EXCEED_CONTEXT_SIZE` → `code = 400`), which this
# stack could not tell apart from any other transport failure: it retried a request that
# can only ever fail, then fed the CIRCUIT BREAKER — so three oversized turns opened the
# breaker and degraded every well-sized turn behind them. Callers saw the generic failure
# and fell back silently (the atomizer's `used_llm=False` → `segment_clauses`), so a
# rejected request scored as an engine capture MISS.
#
# Two seams, both pure token-budget logic (no endpoint resolution, no payload shape):
#   * `_context_preflight`  — BEFORE the call: estimate, clamp the completion budget, or
#     refuse LOUDLY with the measured numbers.
#   * `_context_rejection`  — AFTER a failed attempt: recognise the server's own refusal,
#     LEARN the authoritative `n_ctx` from its body, and stop retrying a deterministic
#     failure (and do NOT let it open the breaker — it is our sizing bug, not their outage).
# `LLM_CONTEXT_GUARD=false` makes both no-ops; the module short-circuits with zero probing.

_CONTEXT_SINK_MISSING = False


def _ctx_mod():
    """The context-window module, or None (import failure must never break a call)."""
    global _CONTEXT_SINK_MISSING
    if _CONTEXT_SINK_MISSING:
        return None
    try:
        from src.api import context_window
        return context_window
    except Exception:  # noqa: BLE001
        _CONTEXT_SINK_MISSING = True
        return None


def _context_preflight(messages: list, max_tokens: int, operation: str,
                       user_id: Optional[str], mode: str):
    """(effective_max_tokens, block_result_or_None) for a prospective call.

    `block_result_or_None` non-None ⇒ DO NOT SEND. The returned dict carries
    `error="context_overrun"` plus the measured numbers so a caller that can split (the
    atomizer) can act on it, and every other caller fails exactly as loudly as before —
    but now COUNTED and named, instead of silently degrading.
    """
    cw = _ctx_mod()
    if cw is None or not cw.guard_enabled():
        return max_tokens, None
    try:
        plan = cw.plan_call(messages, max_tokens, operation)
    except Exception:  # noqa: BLE001 — guard must never break a call
        return max_tokens, None
    if plan.fits:
        if plan.clamped:
            cw.count(cw.EV_COMPLETION_CLAMPED, operation)
            # `effective_max_tokens` comes ONLY from plan.as_log(). Passing it as an
            # explicit kwarg TOO is a duplicate-keyword TypeError that CRASHED every
            # clamped call -- the graceful half of this guard, unable to run. Any key in
            # as_log() must be spelled ONCE; `requested_max_tokens` is a deliberately
            # distinct name for plan.max_tokens and does not collide.
            #
            # WHY IT STAYED LATENT, and why both halves of that matter: the branch was
            # effectively unreachable for small operations until the operation-relative
            # floor landed, so the crash sat dormant until a clamped call actually
            # happened -- which it then did, as a brain-probe failure on a tenant whose
            # prompt fit while its completion budget did not, i.e. precisely the case
            # this branch exists to rescue. Found twice independently; same fix.
            log.warning("llm_call.context_completion_clamped",
                        user_id=(user_id or "?")[:8], operation=operation, mode=mode,
                        requested_max_tokens=plan.max_tokens,
                        note="prompt fits but the completion budget did not; clamped so the "
                             "answer is not truncated mid-JSON (which reads as a parse failure)",
                        **plan.as_log())
            return plan.effective_max_tokens, None
        return max_tokens, None

    cw.count(cw.EV_PREFLIGHT_BLOCKED, operation)
    from src.api.logging_config import log_crit
    log_crit(
        log,
        "llm_call.context_overrun_preflight",
        user_id=(user_id or "?")[:8],
        operation=operation,
        mode=mode,
        remediation=(
            "This request was NOT sent — the endpoint would have refused it and the failure "
            "would have looked like a capture miss. Either shrink the prompt for this "
            "operation or raise the brain's per-request window. For llama.cpp the usable "
            "window is n_ctx / n_parallel (kv_unified off), NOT the -c value."
        ),
        **plan.as_log(),
    )
    _health_note(user_id, "context_overrun", operation)
    _record_llm_nonanswer(operation, "context_overrun_preflight")
    return max_tokens, {
        "error": "context_overrun",
        "message": (f"Request ~{plan.prompt_tokens_est} tokens (estimated) exceeds the "
                    f"brain's usable context window of {plan.window} tokens."),
        "context_overrun": {
            "stage": "preflight",
            "prompt_tokens_est": plan.prompt_tokens_est,
            "window": plan.window,
            "window_source": plan.source,
            "max_tokens": plan.max_tokens,
            "prompt_budget_tokens": plan.prompt_budget,
            "operation": operation,
        },
    }


def _context_rejection(exc: Exception, operation: str, user_id: Optional[str], mode: str):
    """Recognise the SERVER refusing an over-window request. Returns an error dict or None.

    Non-None ⇒ stop: retrying is guaranteed to fail again, and it must not open the breaker.
    """
    cw = _ctx_mod()
    if cw is None or not cw.guard_enabled():
        return None
    status = None
    body = None
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
            try:
                body = resp.text
            except Exception:  # noqa: BLE001
                body = None
    except Exception:  # noqa: BLE001
        return None
    if body is None:
        return None
    try:
        info = cw.classify_rejection(status, body)
    except Exception:  # noqa: BLE001
        return None
    if not info:
        return None
    learned = cw.learn_from_rejection(info)
    cw.count(cw.EV_SERVER_REJECTED, operation)
    from src.api.logging_config import log_crit
    log_crit(
        log,
        "llm_call.context_overrun_rejected_by_server",
        user_id=(user_id or "?")[:8],
        operation=operation,
        mode=mode,
        status_code=status,
        n_prompt_tokens=info.get("n_prompt_tokens"),
        n_ctx=info.get("n_ctx"),
        learned_window=learned,
        server_message=info.get("message"),
        remediation=(
            "The endpoint REFUSED this request — it was never inferred, so any downstream "
            "fallback is a capture LOSS, not an engine result. The window is now learned "
            "and later calls are pre-flighted against it. For llama.cpp raise -c or lower "
            "-np: the usable per-slot window is n_ctx / n_parallel."
        ),
    )
    _health_note(user_id, "context_overrun", operation)
    _record_llm_nonanswer(operation, "context_overrun_server")
    return {
        "error": "context_overrun",
        # the provider's OWN sentence — through the installed redactor (src/api/errors.py) so a
        # gateway that echoes the request (URL, key) cannot ride into a caller's body
        "message": _errors.redact_text(info.get("message")) or "request exceeds the available context size",
        "context_overrun": {
            "stage": "server_rejection",
            "prompt_tokens": info.get("n_prompt_tokens"),
            "window": info.get("n_ctx") or learned,
            "window_source": "server_rejection",
            "operation": operation,
        },
    }


def _context_observe(messages: list, result: dict) -> None:
    """Free calibration off `usage.prompt_tokens` on a successful response."""
    cw = _ctx_mod()
    if cw is None:
        return
    try:
        cw.observe_usage(messages, result)
    except Exception:  # noqa: BLE001
        pass


def _reset_value_to_seconds(value: Any) -> Optional[float]:
    """Normalize a rate-limit reset header to seconds-until-reset.

    Numeric magnitude heuristic first (unchanged): epoch-ms (> 1e12) / epoch-s (> 1e9) /
    offset-seconds, each floored at 0. Real providers then get their string shapes:
    OpenAI sends ``"4s"`` / ``"1.5s"`` / ``"2m30s"`` compact durations, some proxies send
    ISO-8601 durations (``"PT1.5S"`` / ``"PT1M30S"`` / ``"PT1H"``), and Anthropic sends an
    RFC 3339 timestamp (``"2026-04-21T14:38:25Z"`` — the instant the window replenishes).
    Malformed → None (fail-safe, never an exception). Shared by the retry backoff and the
    LAYER-2 header learner so the magnitude heuristic lives in exactly one place.
    """
    raw = str(value).strip()
    try:
        v = float(raw)
    except (ValueError, TypeError):
        v = None
    if v is not None:
        if v > 1e12:
            return max(0.0, v / 1000.0 - time.time())  # epoch milliseconds
        if v > 1e9:
            return max(0.0, v - time.time())            # epoch seconds
        return max(0.0, v)                               # offset seconds
    dur = _parse_compact_duration(raw)
    if dur is not None:
        return max(0.0, dur)
    dur = _parse_iso8601_duration(raw)
    if dur is not None:
        return max(0.0, dur)
    ts = _parse_rfc3339_timestamp(raw)
    if ts is not None:
        return max(0.0, ts)
    return None


def _parse_rfc3339_timestamp(value: str) -> Optional[float]:
    """Anthropic-style reset ``"2026-04-21T14:38:25Z"`` → seconds-until-reset.

    ``anthropic-ratelimit-requests-reset`` is an RFC 3339 / ISO-8601 datetime instant
    (the exact moment the window replenishes), NOT a duration — the previous "ISO-8601
    durations" reading of that header was a fictional contract (real capture at
    DEV/ratelimit). ``Z`` / ``+00:00`` / fractional seconds and other offsets are all
    accepted; a naive datetime is treated as UTC. Malformed / non-timestamp → None.
    """
    if not value or "T" not in value.upper():
        return None
    try:
        norm = value.strip()
        if norm.upper().endswith("Z"):
            norm = norm[:-1] + "+00:00"
        parsed = datetime.fromisoformat(norm)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() - time.time()


# One or more "<magnitude><unit>" components; h/m/s accepted (case-insensitive).
_COMPACT_DURATION_RE = _re.compile(r"(\d+(?:\.\d+)?)([hms])", _re.IGNORECASE)
_COMPACT_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def _parse_compact_duration(value: str) -> Optional[float]:
    """OpenAI-style reset ``"4s"`` / ``"1.5s"`` / ``"2m30s"`` → seconds. Fail-safe → None."""
    if not value:
        return None
    total = 0.0
    pos = 0
    for m in _COMPACT_DURATION_RE.finditer(value):
        if m.start() != pos:
            return None  # non-duration garbage between components ("5s and then")
        total += float(m.group(1)) * _COMPACT_DURATION_UNITS[m.group(2).lower()]
        pos = m.end()
    if pos == 0 or pos != len(value):
        return None
    return total


_ISO8601_DURATION_RE = _re.compile(
    r"P(?:[0-9]+(?:\.[0-9]+)?Y)?(?:[0-9]+(?:\.[0-9]+)?M)?"
    r"(?:[0-9]+(?:\.[0-9]+)?W)?(?:[0-9]+(?:\.[0-9]+)?D)?"
    r"T?(?:([0-9]+(?:\.[0-9]+)?)H)?(?:([0-9]+(?:\.[0-9]+)?)M)?"
    r"(?:([0-9]+(?:\.[0-9]+)?)S)?$",
    _re.IGNORECASE)


def _parse_iso8601_duration(value: str) -> Optional[float]:
    """ISO-8601 DURATION ``"PT1.5S"`` / ``"PT1M30S"`` / ``"PT1H"`` → seconds.

    Some OpenAI-compatible proxies send reset durations in this shape (Anthropic's real
    reset header is an RFC 3339 TIMESTAMP — see ``_parse_rfc3339_timestamp``).
    Only the time (H/M/S) components contribute; date components (P1D, months, years) have
    no fixed length and are ignored (the whole string must still be a well-formed duration
    or the parse fails closed to None).
    """
    if not value.upper().startswith("P"):
        return None
    m = _ISO8601_DURATION_RE.match(value)
    if not m:
        return None
    # Date-only durations (P1D, P1M, P1Y — no T-time section) have no fixed length in
    # seconds → fail safe to None rather than inventing 0.0.
    if m.group(1) is None and m.group(2) is None and m.group(3) is None:
        return None
    total = 0.0
    if m.group(1):
        total += float(m.group(1)) * 3600.0
    if m.group(2):
        total += float(m.group(2)) * 60.0
    if m.group(3):
        total += float(m.group(3))
    return total


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """The provider's own 'wait before you retry' for a rate-limit response, or None.

    Reads ``Retry-After`` (RFC 7231 — integer/float seconds, or an HTTP-date) and, as a
    fallback, the ``x-ratelimit-reset`` / ``x-ratelimit-reset-requests`` family (Cerebras
    documents ``x-ratelimit-reset-requests`` as the UTC timestamp when the window
    replenishes), interpreted as epoch-ms/epoch-s/offset-seconds by magnitude. Returns
    None when the error carries no response or no usable header, so the caller keeps its
    normal exponential backoff. The whole parse is fail-safe: any malformed header →
    None, never an exception, never a hang.
    """
    resp = getattr(exc, "response", None)
    if resp is None or not hasattr(resp, "headers"):
        return None
    headers = getattr(resp, "headers", {}) or {}
    try:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra is not None:
            try:
                return max(0.0, float(str(ra).strip()))
            except (ValueError, TypeError):
                try:
                    # RFC 7231 HTTP-date form ("Wed, 21 Oct 2015 07:28:00 GMT").
                    parsed = datetime.strptime(str(ra).strip(), "%a, %d %b %Y %H:%M:%S GMT")
                    return max(0.0, (parsed - datetime.utcnow()).total_seconds())
                except (ValueError, TypeError):
                    return None
        reset = (headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
                 or headers.get("x-ratelimit-reset-requests") or headers.get("X-RateLimit-Reset-Requests"))
        if reset is not None:
            return _reset_value_to_seconds(reset)
    except (ValueError, TypeError):
        return None
    return None


# ──────────────────────────────────────────────────────────────────────────────
# LAYER 2 — ADAPTIVE HEADER LEARNING (per-response, zero extra network calls)
# ──────────────────────────────────────────────────────────────────────────────
# The static registry clamps to PUBLISHED limits, but a tenant's real key sits on a
# TIER we cannot see from the hostname (Cerebras Free-Trial = 5 RPM vs Developer =
# 300 RPM on the same host+model). Every provider response carries its own budget in
# x-ratelimit-* (OpenAI/Cerebras) or anthropic-ratelimit-* (Anthropic) headers — so we
# LEARN the real per-account rate for free: re-pace the account bucket off every
# response, and HARD-PAUSE when the provider reports the budget exhausted (remaining<=1).
#
# Recognized request-budget families: (family, limit_keys, remaining_keys, reset_keys).
# Tokens-budget headers (x-ratelimit-*-tokens) are intentionally NOT read — our limiter
# paces REQUESTS, not tokens; learning from them would conflate two axes.

_RATE_HEADER_FAMILIES = (
    # BARE form — OpenRouter (on 429 only) and some OpenAI-compatible proxies emit the
    # UNSUFFIXED X-RateLimit-{Limit,Remaining,Reset}. OpenAI/Cerebras send the SUFFIXED
    # -requests keys, so this family is inert for them. Kept as its own family because the
    # bare reset key collides with the suffixed family's reset fallback tuple — without a
    # dedicated family, an OpenRouter 429 partially matched the suffixed family (limit/
    # remaining=None, reset=value), no-oped the learner, and (parsed being non-None) never
    # reached the 429 fallback. With the limit-or-remaining match rule below, bare headers
    # resolve here as a full measurement.
    ("openai-compatible/bare",
     ("x-ratelimit-limit",),
     ("x-ratelimit-remaining",),
     ("x-ratelimit-reset",)),
    ("openai/cerebras",
     ("x-ratelimit-limit-requests",),
     ("x-ratelimit-remaining-requests",),
     ("x-ratelimit-reset-requests", "x-ratelimit-reset")),
    ("anthropic",
     ("anthropic-ratelimit-requests-limit",),
     ("anthropic-ratelimit-requests-remaining",),
     ("anthropic-ratelimit-requests-reset",)),
)

# Cerebras (and OpenAI-compatible proxies) also send SUFFIXED fixed-window buckets:
# x-ratelimit-{limit,remaining}-requests-{minute,hour,day} — and NO reset header at all, so
# the suffix IS the window (minute → 60s, hour → 3600s, day → 86400s). Scan in ascending
# window order so the TIGHTEST present bucket wins: a Free-Trial key (5/minute) must be paced
# off the minute window, not the loose 2400/day cap that would no-op the learner. Tokens-budget
# variants (-tokens-*) stay intentionally unread — we pace REQUESTS, not tokens.
_WINDOW_SUFFIXES = (("minute", 60.0), ("hour", 3600.0), ("day", 86400.0))


def _safe_rate_int(value: Any) -> Optional[int]:
    """Fail-safe int parse for a rate-limit header; garbage → None, never raises."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _first_header(headers: Any, keys: tuple) -> Any:
    for k in keys:
        v = headers.get(k)
        if v is not None:
            return v
    return None


def _match_windowed_request_bucket(headers: Any) -> Optional[dict]:
    """Scan for SUFFIXED fixed-window request buckets (Cerebras' actual header shape).

    Returns a normalized openai/cerebras dict when any ``x-ratelimit-*-requests-<window>``
    key is present, else None. ``window_seconds`` is the PAUSE window's duration (the
    tightest present window with ``remaining <= 1``, else the tightest present window —
    minute=60 / hour=3600 / day=86400) — it stands in for the reset header Cerebras never
    sends. ``reset_seconds`` stays None (no reset key in this shape). ``windows`` carries
    EVERY present window (ascending) so the OBSERVER can compute the binding SUSTAINED cap
    as the min over all windows of limit/window_seconds — learning off the tightest window
    alone (5/min) would over-pace a Free-Trial key past the real 150/hr + 2400/day caps.
    """
    if not headers:
        return None
    windows = []
    for suffix, window in _WINDOW_SUFFIXES:
        limit = _safe_rate_int(headers.get(f"x-ratelimit-limit-requests-{suffix}"))
        remaining = _safe_rate_int(headers.get(f"x-ratelimit-remaining-requests-{suffix}"))
        reset_raw = headers.get(f"x-ratelimit-reset-requests-{suffix}")
        reset_seconds = _reset_value_to_seconds(reset_raw) if reset_raw is not None else None
        if limit is None and remaining is None and reset_seconds is None:
            continue
        windows.append({"window_seconds": window,
                        "limit_requests": limit,
                        "remaining_requests": remaining,
                        "reset_seconds": reset_seconds})
    if not windows:
        return None
    windows.sort(key=lambda w: w["window_seconds"])  # tightest first
    # The PAUSE window: the tightest present window whose remaining is exhausted (<=1) —
    # an exhausted hour bucket must pause for ITS window, not the minute's. Else the
    # tightest present window drives both the pause length and the back-compat fields.
    pause_window = next((w for w in windows
                         if w["remaining_requests"] is not None and w["remaining_requests"] <= 1),
                        windows[0])
    return {"source": "openai/cerebras",
            "limit_requests": windows[0]["limit_requests"],
            "remaining_requests": pause_window["remaining_requests"],
            "reset_seconds": windows[0]["reset_seconds"],
            "window_seconds": pause_window["window_seconds"],
            "windows": windows}


def _parse_rate_limit_headers(headers: Any) -> Optional[dict]:
    """Extract a recognized request-budget header family into a normalized dict.

    Returns None when NO recognized family is present (the common case for self-hosted /
    LAN endpoints — nothing to learn, caller no-ops). Recognized fields parse fail-safe
    (garbage → None for that field); ``reset_seconds`` is seconds-until-reset by
    magnitude (epoch-ms / epoch-s / offset) and ``window_seconds`` is the fixed window's
    duration when the bucket is SUFFIXED (Cerebras' shape, which carries no reset header).
    """
    if not headers:
        return None
    for family, lkeys, rkeys, rkeys_reset in _RATE_HEADER_FAMILIES:
        limit = _safe_rate_int(_first_header(headers, lkeys))
        remaining = _safe_rate_int(_first_header(headers, rkeys))
        reset_raw = _first_header(headers, rkeys_reset)
        reset_seconds = _reset_value_to_seconds(reset_raw) if reset_raw is not None else None
        # A family matches only when it carries a LIMIT or REMAINING (an identifiable
        # request budget). A LONE reset is ambiguous — the suffixed family's reset tuple
        # also lists the bare reset key, so matching on reset alone let an OpenRouter 429
        # (bare reset, no suffixed limit/remaining) partial-match the suffixed family and
        # return non-None with nothing learnable, which then no-oped the learner AND (parsed
        # being non-None) skipped the 429 fallback. Requiring limit-or-remaining sends a
        # reset-only response to None → the observer routes a 429 to the fallback.
        if limit is None and remaining is None:
            continue
        return {"source": family,
                "limit_requests": limit,
                "remaining_requests": remaining,
                "reset_seconds": reset_seconds,
                "window_seconds": None}
    return _match_windowed_request_bucket(headers)


def _publish_rate_state(payload: Optional[dict]) -> None:
    """Publish (or clear) THIS brain's rate-ceiling assumption for the console. Never raises.

    Scope-bound by construction: ``_resolve_brain_scope()`` is the same resolver the rate
    bucket, the breaker and the cache key on, so an assumption can only ever be recorded
    against the brain that produced it. No scope → nothing published (a single-tenant/FOSS
    deployment has no console to tell).
    """
    try:
        from src.api import redis_coord
        scope = _resolve_brain_scope()
        if scope is None:
            return
        if payload is None:
            redis_coord.clear_rate_assumption(scope)
        else:
            redis_coord.publish_rate_assumption(scope, payload)
    except Exception:  # noqa: BLE001 — transparency must never break a live call
        return


def _adopt_rate_fallback_on_429(resp: Any, key: str, bucket: "_TokenBucket",
                                operation: str = "DEFAULT") -> None:
    """LAYER 3: a 429 that taught us nothing still has to change the pace.

    Reached only when the response carried NO recognised rate-limit header family, so there
    is nothing to measure. Precedence is enforced here and in ``set_learner_floor``:

      1. MEASURED (provider headers)  — never touched by this function. It returns early.
      2. CONFIGURED (brain max_rpm / LLM_MAX_RPM / published clamp) — an upper bound, always.
         The floor only ever TIGHTENS (``min(effective, floor)`` at acquire), so an
         assumption can slow us down and can never speed us past what the operator allowed.
      3. TABLE (published entry-tier figure for this host).
      4. BLIND BACK-OFF (no entry, or an entry that publishes no RPM).

    A 429 the provider's own body says is NOT about request volume changes nothing at all —
    see ``_429_is_rate_evidence``.
    """
    if not _rate_429_fallback_enabled():
        return
    if getattr(resp, "status_code", None) != 429:
        return

    from src.api.logging_config import log_crit
    base_url, model = _resolve_endpoint_and_model()
    entry = _fallback_entry_for(base_url)
    is_rate, reason = _429_is_rate_evidence(resp, _non_rate_codes_for(base_url))
    if not is_rate:
        # NOT a pacing problem. Throttling an out-of-credit or misconfigured account makes
        # it slower AND still broken, so we refuse to invent a ceiling and say why instead.
        log_crit(log, "llm_rate_limit.fallback_declined_not_rate",
                 key=key, operation=operation, reason=reason,
                 endpoint_host=(entry or {}).get("source", "") if entry else "",
                 note=rate_refusal_disclaimer(reason))
        _publish_rate_state({"state": "not_rate_limited", "reason": reason,
                             "disclaimer": rate_refusal_disclaimer(reason),
                             "observed_at": str(int(time.time()))})
        return

    # PRECEDENCE GUARD. A measured value always beats the table — including a measured value
    # learned earlier in this same window. We do NOT tighten it here; the retry_after/backoff
    # path already handles the immediate wait, and the next response's headers re-derive the
    # truth. Silently degrading a measurement to an assumption is the one inversion that
    # would make the disclaimer a lie.
    if bucket.floor_provenance == FLOOR_MEASURED:
        log.info("llm_rate_limit.fallback_declined_measured",
                 key=key, operation=operation,
                 measured_floor_rps=round(bucket.learner_floor_rps, 4),
                 note="429 carried no headers, but this brain already has a MEASURED "
                      "ceiling — an assumption may never overwrite a measurement")
        return

    effective_rps, _limits = _effective_rate_per_sec(base_url, model)
    existing = bucket.learner_floor_rps if bucket.learner_floor_rps > 0 else 0.0
    current = existing or bucket.rate or effective_rps
    min_rps = _rate_fallback_min_rpm() / 60.0

    table_rpm = _pacing_rpm_from(entry)
    if table_rpm:
        candidate = (table_rpm * _rate_headroom()) / 60.0
        provenance = FLOOR_TABLE
    else:
        # Unknown provider, OR a recognised one whose published limit is not a request rate
        # we can convert (DeepSeek's concurrency, a TPM-only figure) — see _pacing_rpm_from.
        # A bare 429 is evidence regardless of whether we can name the provider, so back off
        # conservatively rather than leave it hammering.
        candidate = current * _rate_fallback_decay()
        provenance = FLOOR_BACKOFF

    if candidate >= current:
        # THE PUBLISHED FIGURE DOES NOT EXPLAIN THIS 429. Either it is looser than the pace we
        # are already running (so adopting it would change nothing and leave us hammering), or
        # we adopted it earlier and got refused anyway — in which case it is simply wrong for
        # this key's tier. Both mean the table has nothing useful to say here, so decay from
        # what is actually in force and STOP claiming a published source for a value that is
        # no longer one. Without this, a provider with a high published ceiling (xAI Tier 0 at
        # 150 RPS, Anthropic Start at 1,000 RPM) would 429 and we would not slow down at all.
        candidate = current * _rate_fallback_decay()
        provenance = FLOOR_BACKOFF
        entry = None

    candidate = max(candidate, min_rps)
    disclaimer = rate_assumption_disclaimer(candidate * 60.0, provenance, entry)
    if not bucket.set_learner_floor(candidate, _rate_endpoint_tag(base_url),
                                    provenance=provenance, note=disclaimer):
        return
    target = min(effective_rps, candidate)
    # A blind back-off / table fallback has NO window allowance — keep whatever burst the
    # floor already carried (a decay must tighten the RATE, not quietly re-metronome the
    # burst to 1 when the provider's allowance is still in force). With no learned burst
    # either, fall back to the allowance we KNOW from config/registry rather than to 1.
    # This function EXISTS because the provider just refused us, so a pre-refusal
    # allowance can never justify a burst here. Only a burst learned with the floor.
    bucket.resync_rate(target, _rate_burst_capacity(target, bucket.floor_capacity or None))
    log_crit(log, "llm_rate_limit.fallback_applied",
             key=key, operation=operation,
             provenance=provenance,
             assumed_rpm=round(candidate * 60.0, 2),
             effective_rps=round(target, 4),
             prev_floor_rps=round(existing, 4),
             table_tier=(entry or {}).get("tier", ""),
             table_source=(entry or {}).get("source", ""),
             table_recorded=(entry or {}).get("recorded", ""),
             disclaimer=disclaimer)
    _publish_rate_state({"state": "assumed", "provenance": provenance,
                         "assumed_rpm": round(candidate * 60.0, 2),
                         "tier": (entry or {}).get("tier", ""),
                         "source": (entry or {}).get("source", ""),
                         "recorded": (entry or {}).get("recorded", ""),
                         "disclaimer": disclaimer,
                         "observed_at": str(int(time.time()))})


def _observe_rate_headers(resp: Any, operation: str = "DEFAULT") -> None:
    """LAYER 2: re-pace this account's bucket from the provider's own budget headers.

    Called off EVERY response (success AND 429). Two signals, each honored only as a
    TIGHTENING of the current effective pace (the operator's clamp is the ceiling):

      * a minute-class reset window is present → learned_rpm = remaining /
        seconds-until-reset, scaled by headroom, then target = min(effective_rps,
        learned). With NO reset header (Cerebras' suffixed fixed-window buckets), the
        pace comes from the MINUTE-class windows only (min of limit/window × headroom)
        and the window's request allowance becomes the BURST CAPACITY. Day/hour windows
        are QUOTA signals: their allowance is published to the paced daily budget
        controller (src/api/daily_budget.py) and is NEVER divided into the per-second
        rate — flattening a day (2400/day → 0.028 rps) built a 1-per-36s metronome with
        capacity floored to 1, stricter short-term than the provider's own policy and
        unable to serve any multi-call turn (owner-confirmed defect 2026-08-15; the
        layered two-controller shape follows established rate-limiter practice —
        https://redis.io/tutorials/howtos/ratelimiting/, fetched 2026-08-15). The
        learned target is ALSO persisted as a LEARNER FLOOR on the bucket so the
        acquire-time re-clamp cannot overwrite it; the bucket resyncs DOWN toward what
        the provider is actually
        reporting — and recovers up when the tier/key improves (the effective clamp bounds
        the upward move).
      * remaining_requests <= 1 → the budget is exhausted; the bucket PAUSES (zero
        tokens) until the window reset — ``reset_seconds`` when known, else the paused
        window's length — the hard-pause this layer exists for.

    Every failure path is a no-op: a learning miss must never lose a live call.
    """
    # ONE CHOKEPOINT for the recent-429 counter: this function sees EVERY response (success +
    # failure, sync + async), so a provider 429 is counted exactly once per received response
    # here — BEFORE the rate-config gate below, because the counter is a read-out independent of
    # whether rate pacing is enabled. (The rate_limited EVENT log line is emitted at the
    # non-answer sites + deduped; this is the COUNTER, not the event.)
    if getattr(resp, "status_code", None) == 429:
        _bump_429_counter()

    if not _rate_header_learning_enabled() or not _rate_limit_enabled():
        return
    try:
        key = _resolve_rate_limit_key()
        if not key:
            return
        bucket = _rate_buckets.get(key)
        if bucket is None:
            return  # no pre-flight bucket (limiter off at call time) → nothing to adapt
        parsed = _parse_rate_limit_headers(getattr(resp, "headers", None) or {})
        if parsed is None:
            # LAYER 3: nothing to MEASURE. If this was a 429 it is still evidence that the
            # current estimate is too high — fall back to the published-limits table, or
            # back off blind. Any other status with no headers is simply a no-op, exactly
            # as before. This is the ONLY entry point to the fallback, so a header-bearing
            # 429 can never reach it: the table is consulted when, and only when, the
            # provider told us nothing.
            _adopt_rate_fallback_on_429(resp, key, bucket, operation)
            return
        remaining = parsed.get("remaining_requests")
        reset_secs = parsed.get("reset_seconds")
        window_secs = parsed.get("window_seconds")
        windows = parsed.get("windows") or []
        if reset_secs is not None and reset_secs <= 0:
            reset_secs = None
        if window_secs is not None and window_secs <= 0:
            window_secs = None
        wait = reset_secs if reset_secs is not None else window_secs
        # HARD PAUSE: the budget is exhausted (this call consumed the last allowance).
        if remaining is not None and remaining <= 1:
            if wait is not None:
                bucket.pause_for(wait)
                log.info("llm_rate_limit.bucket_paused",
                         key=key, operation=operation,
                         source=parsed.get("source"),
                         remaining_requests=remaining,
                         pause_seconds=round(wait, 1))
            return
        # Learned rate for the REST of this window, clamped by headroom AND the current
        # effective pace (operator config + per-brain + published cap still all bound us).
        limit_requests = parsed.get("limit_requests")
        learned_rpm = None
        # PACE vs QUOTA (owner-confirmed defect, 2026-08-15): only minute-class windows
        # (≤ _rate_learn_pace_window_max_s) may drive the INSTANTANEOUS rate + burst
        # capacity. Long windows (hour/day) are QUOTA-shaped: flattening a day into a
        # per-second average (2400/day ÷ 86400 ≈ 0.028 rps) turns the token bucket into
        # a 1-per-36s metronome with capacity floored to 1 — stricter short-term than the
        # provider's own policy (all 5 minute-requests may fire in the first second), and
        # no multi-call turn can ever proceed as a unit. Long-window quotas belong to the
        # PAUSE mechanism (their own reset) and the PACED daily budget controller
        # (src/api/daily_budget.py) — published below as a learned day cap.
        #
        # GROUNDED in established practice (constraint 11): a token bucket's PURPOSE is
        # "controlled bursts up to bucket capacity while enforcing a steady average
        # rate", and multi-window limits are handled by LAYERING algorithms — "a token
        # bucket for per-user limits and a fixed window for global limits" — not by
        # min()-flattening every window into one per-second rate. Source:
        # https://redis.io/tutorials/howtos/ratelimiting/ (fetched 2026-08-15). The
        # burst allowance learned here (capacity = the minute window's request count) is
        # exactly the "bursty traffic with average rate caps" shape that table
        # recommends for API clients.
        pace_window_max = _rate_learn_pace_window_max_s()
        learned_allowance = 0.0
        quota_day_cap = 0
        quota_day_window = 86400
        if reset_secs is not None and remaining is not None:
            if reset_secs <= pace_window_max:
                learned_rpm = (remaining / reset_secs) * 60.0 * _rate_headroom()
            elif not windows and limit_requests is not None and reset_secs >= 86400 / 2:
                # BARE family (limit+remaining+reset, no suffixed windows) whose reset is
                # day-scale: quota semantics — publish to the daily controller, learn no
                # per-second rate from it.
                quota_day_cap = limit_requests
                quota_day_window = 86400  # bare-family carries no window field; day-scale by the reset guard
            # A LONG reset (hour/day) carries quota semantics — the PAUSE already gates
            # the wait; learning a per-second rate off it would re-flatten the day into
            # the metronome. Fall through to any minute-class windows instead.
        if learned_rpm is None and windows:
            per_min_caps = []
            for w in windows:
                w_limit = w.get("limit_requests")
                w_secs = w.get("window_seconds")
                if w_limit is None or not w_secs:
                    continue
                if w_secs <= pace_window_max:
                    # PACE window: minute-class → drives the instantaneous rate, and its
                    # allowance (the tightest one) is the BURST CAPACITY.
                    per_min_caps.append((w_limit / w_secs) * 60.0)
                    if not learned_allowance or w_limit < learned_allowance:
                        learned_allowance = w_limit
                elif w_secs >= 86400 / 2:
                    # QUOTA window: day-scale with a published allowance → the daily
                    # budget controller's cap (it paces remaining ÷ time-left; it must
                    # NEVER divide into the per-second rate).
                    if w_limit and w_limit > quota_day_cap:
                        quota_day_cap = w_limit
                        quota_day_window = w_secs
                # Hour-scale windows (600s < w < 43200s): neither — the pause mechanism
                # and the minute pace carry them; a per-second flattening of an hour is
                # the same metronome at 1/60th the scale.
            if per_min_caps:
                learned_rpm = min(per_min_caps) * _rate_headroom()
        if learned_rpm is None and not windows and window_secs is not None and limit_requests is not None:
            # Single fixed-window fallback (kept fail-safe; the windows list normally covers it).
            if window_secs <= pace_window_max:
                learned_rpm = (limit_requests / window_secs) * 60.0 * _rate_headroom()
                learned_allowance = limit_requests
            elif window_secs >= 86400 / 2 and limit_requests > 0:
                quota_day_cap = limit_requests
                quota_day_window = window_secs
        # A learned DAY allowance feeds the PACED daily budget controller — never the
        # per-second rate. Published whether or not a pace signal also exists: a lone
        # day-window header (no minute window present) is still a real quota the pacer
        # must respect on the daily axis.
        if quota_day_cap > 0:
            try:
                from src.api import daily_budget as _DB
                _dscope = _coord_scope_for_rate_key(key)
                if _dscope is not None:
                    _DB.publish_day_cap(_dscope, quota_day_cap, quota_day_window)
                    log.info("llm_rate_limit.day_cap_learned",
                             key=key, day_cap=quota_day_cap,
                             note="daily quota published to the paced budget controller "
                                  "(never divided into the per-second rate)")
            except Exception:  # noqa: BLE001 — publishing must never break learning
                pass
        # No learnable rate signal (a lone limit/remaining with no reset, or limit+reset with
        # no remaining). A 429 here is STILL evidence the current pace is too high — route it
        # to the published-table fallback / blind backoff instead of no-op-ing. The unsafe
        # direction is a 429 that fails to slow us down, and a header the family matched on
        # (limit-or-remaining) that cannot yield a rate must not silently swallow it. A non-429
        # with no learnable signal stays a no-op, exactly as before. This also closes the
        # long-standing limit+reset(no-remaining) hole the pre-fix tree carried.
        if learned_rpm is None:
            if getattr(resp, "status_code", None) == 429:
                _adopt_rate_fallback_on_429(resp, key, bucket, operation)
            return
        base_url, model = _resolve_endpoint_and_model()
        effective_rps, limits = _effective_rate_per_sec(base_url, model)
        target_rps = min(effective_rps, learned_rpm / 60.0)
        prev_rate = bucket.rate
        # DEFECT 1: persist the learned rate as a FLOOR so the next acquire-time resync
        # (which re-clamps toward the registry effective rate) cannot overwrite it. Each
        # observe OVERWRITES the floor — that is what lets the learner recover on an upgrade.
        # The floor is tagged with the endpoint it was learned from so a brain switch to a
        # different provider drops it instead of under-pacing the new provider forever.
        # provenance=MEASURED: this came from the provider's OWN headers, so it outranks and
        # REPLACES any assumption the 429 fallback had adopted, and clears the disclaimer the
        # tenant was shown. This is the "a real header replaces the assumed value and flips
        # the label to measured" half of the precedence contract.
        was_assumed = bucket.floor_provenance in _ASSUMED_PROVENANCES
        bucket.set_learner_floor(target_rps, _rate_endpoint_tag(base_url),
                                 provenance=FLOOR_MEASURED,
                                 capacity=learned_allowance)
        if was_assumed:
            log.info("llm_rate_limit.assumption_replaced_by_measurement",
                     key=key, operation=operation,
                     measured_rpm=round(target_rps * 60.0, 2),
                     note="provider sent real rate headers — assumed ceiling dropped")
            _publish_rate_state(None)
        if bucket.resync_rate(target_rps, _rate_burst_capacity(
                target_rps, bucket.floor_capacity or None)):
            log.info("llm_rate_limit.bucket_learned",
                     key=key, operation=operation,
                     source=parsed.get("source"),
                     remaining_requests=remaining,
                     learned_rpm=round(learned_rpm, 2),
                     prev_rps=round(prev_rate, 3),
                     effective_rps=round(target_rps, 3),
                     provider_source=(limits or {}).get("source"),
                     provider_rpm=(limits or {}).get("rpm"))
    except Exception:  # noqa: BLE001 — learning must never break a live call
        pass


# ──────────────────────────────────────────────────────────────────────────────
# MAIN: call_llm_with_retry_sync()
# ──────────────────────────────────────────────────────────────────────────────

def call_llm_with_retry_sync(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    max_retries: int = 3,
    operation: str = "DEFAULT",
    max_tokens: Optional[int] = None,
    raise_on_unavailable: bool = False,
) -> dict:
    """
    Synchronous LLM call with retry, circuit breaker, and fallback endpoints.

    ``raise_on_unavailable`` (default False = today's behaviour byte-for-byte) makes the
    DIDN'T-HAPPEN cases raise ``LLMUnavailable`` instead of returning an empty/error dict.
    Opt in from any caller that CACHES A VERDICT derived from the answer: without it, "the
    brain was never reached" is indistinguishable from "the brain answered nothing", and
    the caller records a decision it has no evidence for. The re_embedder's ontology-growth
    lanes are the motivating case — see `src.api.llm_lane.LLMUnavailable`.

    This is the primary LLM calling interface for blocking operations:
    - API endpoint handlers (main.py)
    - WGM validation gate (gate.py)
    - Inline extraction (retraction, correction)

    Circuit Breaker:
      If LLM endpoint is experiencing widespread failures, the circuit breaker
      will open and return a safe default without attempting new calls.

    Retry Strategy:
      Tries up to 3 endpoints before giving up. Within each endpoint, respects
      timeout. Between retry attempts, uses exponential backoff (1s, 2s, 4s).

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string (resolved via LLMModels.get(operation))
        user_id: User UUID for logging and context (default "anonymous")
        timeout: Request timeout in seconds (default from LLMTimeouts)
        max_retries: Number of retries across different endpoints (default 3)
        operation: Operation type for timeout selection (default "DEFAULT")

    Returns:
        Parsed JSON response from LLM, or {} on failure

    Raises:
        RuntimeError: If all endpoints exhausted and no response received
        httpx.TimeoutException: If request times out (after retries)
        json.JSONDecodeError: If response cannot be parsed as JSON
    """
    # ── BRAIN-SCOPED INGEST CACHE (Deliverable B; DEV/DESIGN-brain-budget-event-cache.md §2)
    # A cache hit means NO outbound call — no rate-limit token, no POST, no breaker probe.
    # Scoped to the resolved brain (shared within one brain, never across). cache_get/put
    # NEVER raise (Redis down → None/miss). FOSS byte-for-byte: cache_enabled() is False when
    # SAAS_MODE is off. Only the deterministic-input whitelist is cacheable.
    from src.api import llm_cache
    _cacheable = llm_cache.cacheable_operation(operation) and llm_cache.cache_enabled()
    _sys_prompt = ""
    if _cacheable:
        try:
            _sys_prompt = llm_cache.system_prompt_from_messages(messages)
            _hit = llm_cache.cache_get(operation, _sys_prompt, messages)
            if _hit is not None:
                log.info("llm_cache.hit", operation=operation)
                return _hit
            # MISS — the missing half of the picture. With only the hit logged, "the cache is
            # working" and "nothing has exercised it yet" both render as SILENCE, and the hit
            # RATE is underivable from the log stream at all.
            #
            # ⚠️ This log line is DELIBERATELY coarser than the counter that drives the console.
            # It fires whenever the cache did not serve THIS call — including when the tenant
            # has no brain bound, so no brain scope resolved and there was nothing to consult.
            # The metered counter (``brain_meter``, incremented inside ``cache_get`` where the
            # scope is known) counts ONLY a real consulted-and-empty miss, so the displayed hit
            # rate is not dragged down by non-events. Do not "reconcile" the two: the log is a
            # per-call breadcrumb, the counter is the instrument.
            log.info("llm_cache.miss", operation=operation)
        except Exception:  # cache must never break a call
            pass

    # SaaS cost-safe fail-closed: unconfigured tenant → no spend on the global env/cloud brain.
    if _saas_brain_unavailable():
        log.warning("llm_call.saas_no_brain_failclosed", operation=operation, mode="sync_retry")
        _record_llm_nonanswer(operation, "saas_no_brain_failclosed")
        _emit_brain_nonanswer(user_id, _NO_BRAIN_EVENT_REASONS.get(
            no_brain_metric(), "saas_no_brain_failclosed"))
        _health_note(user_id, no_brain_metric(), operation, detail=no_brain_detail())
        return {}

    # Select timeout based on operation type
    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    if max_tokens is None:
        max_tokens = LLMMaxTokens.get(operation)

    # Per-tenant BYO engine-brain override (SaaS). SAAS-off → global breaker, unchanged
    # model/max_tokens, empty extra params (byte-for-byte today's behavior).
    _breaker, model, max_tokens, _extra_params = _apply_tenant_brain(model, max_tokens)
    # Reasoning headroom (dprompt-156): a model that cannot disable reasoning spends
    # reasoning tokens INSIDE the completion budget - pad it so a long fact-dense turn's
    # JSON is not truncated to empty. Computed once per logical call, after the tenant-brain
    # override and BEFORE _context_preflight (which must see the full budget).
    _pre_headroom_max = max_tokens
    max_tokens = _apply_reasoning_headroom(model, max_tokens)
    _headroom_applied = max_tokens - _pre_headroom_max

    # NOTE (2026-08-11): pacing used to happen HERE, once per logical call, and the retry
    # loop below then sent up to `max_retries * len(endpoints)` REQUESTS on that single
    # token. The bucket therefore under-counted real outbound load by the retry factor —
    # measured on prod as one of three compounding causes of a sustained 429 storm. The
    # acquire now sits immediately before each POST (see `_pace_one_request`), so tokens
    # and requests are 1:1. It also no longer paces a call the breaker is about to reject.
    #
    # Check circuit breaker first (per-tenant in SaaS mode; global otherwise)
    if _breaker_is_open(_breaker):
        time_until_reset = (
            _breaker.last_failure_time +
            timedelta(seconds=_breaker.timeout_seconds) -
            datetime.utcnow()
        ).total_seconds()

        error_msg = {
            "error": "circuit_breaker_open",
            "message": f"Language model not responding. Wait {max(1, int(time_until_reset))}s or restart backend.",
            "debug": "Check logs: docker logs faultline --tail=50 | grep circuit_breaker"
        }
        log.warning("call_llm.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation,
                   time_until_reset_seconds=max(1, int(time_until_reset)))
        _record_llm_nonanswer(operation, "circuit_breaker_open")
        _emit_brain_nonanswer(user_id, "circuit_breaker_open")
        _health_note(user_id, "breaker_open", operation)
        if raise_on_unavailable:
            raise LLMUnavailable("breaker_open", operation)
        return error_msg

    # CONTEXT-WINDOW PRE-FLIGHT (token budget). Runs AFTER the tenant-brain override, so
    # the window and the max_tokens checked are the ones this call will actually use.
    max_tokens, _ctx_block = _context_preflight(messages, max_tokens, operation, user_id,
                                                mode="sync_retry")
    if _ctx_block is not None:
        return _ctx_block
    # The window clamp may have shrunk the padded budget — the audit reports the FINAL
    # on-the-wire delta, never the pre-clamp one (a note claiming padding the wire did
    # not carry is exactly the overstatement this lane exists to remove).
    _headroom_applied = max_tokens - _pre_headroom_max

    # Lazy import to avoid circular dependencies
    from src.api.llm_client import build_llm_payload, get_llm_headers

    # Build payload once, reuse across retries
    payload = build_llm_payload(
        messages=messages,
        model=model,
        user_id=user_id,
        max_tokens=max_tokens,
        # temperature defaults to 0.0 in build_llm_payload; a tenant's extra_params may
        # override it. Passed via **_extra_params to avoid a duplicate-kwarg collision.
        **{"temperature": 0.0, **_extra_params},
    )

    # SSRF re-check (audit HIGH #2): re-resolve + re-vet a tenant BYO endpoint immediately
    # before connect so a DNS-rebind after save/resolve cannot smuggle an internal target
    # past the egress guard. Fail-closed — a blocked tenant endpoint aborts the call.
    if _tenant_egress_blocked():
        log.warning("llm_call.tenant_egress_blocked", operation=operation, mode="sync_retry")
        _record_llm_nonanswer(operation, "tenant_egress_blocked")
        _emit_brain_nonanswer(user_id, "tenant_egress_blocked")
        return {}

    endpoints = _get_endpoint_list()
    response = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        for endpoint_idx, endpoint in enumerate(endpoints, 1):
            # PACE PER REQUEST, NOT PER CALL. Every iteration here puts one more request
            # on the wire, so every iteration spends one token. A BACKGROUND caller that
            # cannot get one yields the whole call rather than firing into a provider that
            # is already refusing us (interactive callers still fail open).
            if not apply_rate_limit_sync(operation):
                log.info("llm_call.deferred_no_capacity",
                         user_id=user_id, operation=operation,
                         attempt=attempt, mode="sync_retry")
                _record_llm_nonanswer(operation, "rate_deferred")
                _emit_brain_nonanswer(user_id, "rate_deferred")
                if raise_on_unavailable:
                    raise LLMUnavailable("rate_deferred", operation)
                # DISTINGUISHABLE defer (critic pass 2): a bare {} is byte-identical to
                # "the model answered nothing" downstream — /extract/rewrite's chunk loop
                # relabels it empty_llm_response and the document lane marks the chunk
                # terminally failed on a day that is merely PACED. The error marker lets
                # callers that can wait (documents, reextract) re-pend instead of fail.
                return {"error": "rate_deferred"}
            try:
                # Log attempt details
                log.debug("llm_call.attempt_start",
                         user_id=user_id,
                         operation=operation,
                         attempt=attempt,
                         endpoint_index=endpoint_idx,
                         timeout_seconds=timeout)

                start_time = time.time()

                # Outbound source-IP spreading (opt-in LLM_SOURCE_IPS): rotate the
                # bound source address per call. Unset env → uses _llm_http_client
                # unchanged. Fail-safe: a bad/unbindable IP falls back to it.
                response = _post_llm_sync(
                    _llm_http_client,
                    endpoint,
                    json=payload,
                    headers=get_llm_headers(),
                    timeout=timeout,
                )

                elapsed = time.time() - start_time
                response.raise_for_status()

                # LAYER 2 (adaptive header learning): re-pace the account bucket from the
                # provider's own rate-limit headers on every success. Fail-safe no-op.
                _observe_rate_headers(response, operation)

                log.info("llm_call.attempt_success",
                        user_id=user_id,
                        operation=operation,
                        attempt=attempt,
                        elapsed_seconds=round(elapsed, 2),
                        endpoint_index=endpoint_idx)
                # endpoint/timeout ride along so the alarm stream can name the HOST that
                # answered and the BUDGET it answered inside. `endpoint` is a URL; only its
                # netloc is ever stored or logged (brain_health.endpoint_host) because some
                # providers carry the key in the URL.
                _health_note(user_id, "llm_call", operation, elapsed,
                             endpoint=endpoint, budget_seconds=timeout)

                # Success — reset circuit breaker and parse response robustly (dBug-016)
                _breaker.record_success()
                _clear_shared_breaker()

                result = _parse_llm_response_robust(response)
                if not result:
                    log.warning("llm_call.response_parse_failed",
                               user_id=user_id,
                               operation=operation,
                               endpoint_index=endpoint_idx)
                    continue

                # FREE CALIBRATION: the response reports usage.prompt_tokens — real ground
                # truth for how many tokens our characters actually cost. Ratchets the
                # estimator's chars/token DOWN only (never widens it).
                _context_observe(messages, result)

                content = response_message_text(result, operation, user_id)
                # VERIFY suppression before stripping: once the tags are gone the evidence
                # that the model reasoned is gone too. A 200 is not proof of suppression.
                _audit_reasoning_leak(result, operation, user_id,
                                      expected=_reasoning_expected(model),
                                      budget_padded=_headroom_applied,
                                      model=model)
                _record_call_usage(result, operation)
                # Strip <think>...</think> reasoning blocks (Qwen3 models)
                content = _strip_think_tags(content)
                # Strip a ```json fence BEFORE parsing — a fenced payload is well-formed JSON
                # that json.loads cannot read, and the object-only regex below cannot rescue
                # an array. Without this, a valid empty extraction ("[]") reads as a hard
                # parse failure and feeds the circuit breaker.
                content = _unfence(content)

                if not content:
                    _record_llm_nonanswer(operation, "empty_llm_content")
                    return {}

                try:
                    _parsed = json.loads(content)
                    if _cacheable and _parsed is not None:
                        try: llm_cache.cache_put(operation, _sys_prompt, messages, _parsed)
                        except Exception: pass
                    return _parsed
                except json.JSONDecodeError:
                    match = _re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, _re.DOTALL)
                    if not match:
                        match = _re.search(r'\{.*\}', content, _re.DOTALL)
                    if not match:
                        # ARRAY fallback. The extraction prompt asks for a JSON ARRAY, so an
                        # object-only salvage regex cannot rescue the very shape we request —
                        # a top-level array wrapped in prose was reported as "no JSON".
                        match = _re.search(r'\[.*\]', content, _re.DOTALL)
                    if match:
                        _parsed = json.loads(match.group())
                        if _cacheable and _parsed is not None:
                            try: llm_cache.cache_put(operation, _sys_prompt, messages, _parsed)
                            except Exception: pass
                        return _parsed
                    else:
                        log.warning("llm_call.no_json_in_response",
                                   user_id=user_id,
                                   operation=operation,
                                   content_preview=content[:200])
                        _record_llm_nonanswer(operation, "unparseable_llm_response")
                        return {}

            except Exception as e:
                if _is_egress_blocked(e):
                    # CALL-TIME SSRF refusal: a policy verdict, not a transient fault.
                    # Never retried, never counted against the breaker, never a success shape.
                    _egress_refusal(e, operation, user_id, "sync_retry")
                    if raise_on_unavailable:
                        raise LLMUnavailable("tenant_egress_blocked", operation) from e
                    return {}
                last_error = e
                # A rate-limited response (429) carries the budget too — learn it even
                # though the call failed (remaining==0 → hard pause until the reset).
                _observe_rate_headers(getattr(e, "response", None), operation)
                elapsed = time.time() - start_time
                response_body = ""
                if isinstance(e, httpx.HTTPStatusError) and hasattr(e, 'response'):
                    try:
                        response_body = e.response.text[:500]
                    except Exception:
                        pass
                log.warning("llm_call.attempt_failed",
                           user_id=user_id,
                           operation=operation,
                           attempt=attempt,
                           endpoint_index=endpoint_idx,
                           elapsed_seconds=round(elapsed, 2),
                           error_type=type(e).__name__,
                           error_message=str(e)[:200],
                           response_body=response_body or "(unavailable)")
                # LEARN A REJECTED REASONING FIELD. A provider whose schema refuses one of our
                # suppression fields 400s the WHOLE call, so without this the endpoint is
                # permanently unusable rather than merely un-suppressed — which is exactly how
                # a correctly-configured Gemini brain reported "probe FAILED". Recording it
                # here means the retry below (and every later call) stops sending it, so the
                # next attempt succeeds with reasoning left on instead of failing outright.
                # Suppression is best-effort; the CALL is not.
                try:
                    from src.api.llm_client import (note_reasoning_field_rejected,
                                                    _reasoning_base_url)
                    if response_body:
                        note_reasoning_field_rejected(_reasoning_base_url(), response_body)
                except Exception:
                    pass
                _health_note(user_id, "llm_call_failed", operation, elapsed,
                             endpoint=endpoint, budget_seconds=timeout,
                             detail=_llm_input_echo(messages),
                             exc=e, response_body=response_body or None)

                # OVER-WINDOW REFUSAL: a deterministic verdict, not a transient fault.
                # Retrying re-sends a request that can only fail again, and letting it reach
                # `_breaker.record_failure()` opens the breaker for calls that WOULD have fit.
                # Return now with the server's own numbers, and learn its real window.
                _ctx_rej = _context_rejection(e, operation, user_id, mode="sync_retry")
                if _ctx_rej is not None:
                    return _ctx_rej

        # All endpoints failed for this attempt
        if attempt < max_retries:
            backoff_seconds = 2 ** (attempt - 1)
            # A rate-limit 429 may carry the provider's OWN retry window — honoring it
            # (max with the exponential backoff) stops us from re-hammering an exhausted
            # per-minute budget with retries that can only fail again.
            retry_after = _retry_after_seconds(last_error)
            if retry_after is not None:
                if retry_after > backoff_seconds:
                    log.info("llm_call.backoff_honored_retry_after",
                             user_id=user_id,
                             operation=operation,
                             attempt=attempt,
                             retry_after_seconds=round(retry_after, 1),
                             backoff_seconds=round(backoff_seconds, 1))
                backoff_seconds = max(backoff_seconds, retry_after)
            log.warning("llm_call.attempt_exhausted_endpoints",
                       user_id=user_id,
                       operation=operation,
                       attempt=attempt,
                       backoff_seconds=backoff_seconds)
            time.sleep(backoff_seconds)

    # All retries exhausted. Classify BEFORE counting: a settled 4xx verdict on this payload
    # is not evidence the dependency is unavailable (see "BREAKER EVIDENCE MODEL").
    _fclass, _ftoken = classify_failure(last_error)
    _breaker.record_failure(_fclass, _failure_signature(_fclass, _ftoken, operation),
                            connect_phase=is_connect_phase_failure(last_error))
    _publish_breaker_state(_breaker)
    log.error("llm_call.all_retries_exhausted",
             user_id=user_id,
             operation=operation,
             total_attempts=max_retries,
             total_endpoints=len(endpoints),
              final_error_type=type(last_error).__name__,
              final_error_message=str(last_error)[:200])
    _record_llm_nonanswer(operation, f"all_retries_exhausted:{type(last_error).__name__}")
    _emit_brain_nonanswer(user_id, "all_retries_exhausted", exc=last_error)

    # A caller that CACHES A VERDICT needs to tell "never reached" from "answered nothing".
    # Raising the raw transport error cannot express that — every helper in this repo wraps
    # LLM calls in `except Exception: return None`, which collapses both into the same None.
    if raise_on_unavailable:
        raise LLMUnavailable("retries_exhausted", operation) from last_error
    if last_error is not None:
        raise last_error
    raise RuntimeError("No LLM endpoint responded")


# ──────────────────────────────────────────────────────────────────────────────
# ASYNC: call_llm_with_retry_async()
# ──────────────────────────────────────────────────────────────────────────────

async def call_llm_with_retry_async(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    max_retries: int = 3,
    operation: str = "DEFAULT",
    max_tokens: Optional[int] = None,
    raise_on_unavailable: bool = False,
) -> dict:
    """
    Asynchronous LLM call with retry, circuit breaker, and fallback endpoints.

    Identical to call_llm_with_retry_sync() but uses async/await for non-blocking
    operations. Used in background loops (re_embedder.py).

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string
        user_id: User UUID for logging
        timeout: Request timeout in seconds (default from LLMTimeouts)
        max_retries: Number of retries across different endpoints
        operation: Operation type for timeout selection

    Returns:
        Parsed JSON response from LLM, or {} on failure

    Raises:
        Same exceptions as sync version
    """
    # ── BRAIN-SCOPED INGEST CACHE (Deliverable B; DEV/DESIGN-brain-budget-event-cache.md §2)
    # Identical to the sync twin: a hit = no outbound call. See the comment there.
    from src.api import llm_cache
    _cacheable = llm_cache.cacheable_operation(operation) and llm_cache.cache_enabled()
    _sys_prompt = ""
    if _cacheable:
        try:
            _sys_prompt = llm_cache.system_prompt_from_messages(messages)
            _hit = llm_cache.cache_get(operation, _sys_prompt, messages)
            if _hit is not None:
                log.info("llm_cache.hit", operation=operation)
                return _hit
            # MISS — the missing half of the picture. With only the hit logged, "the cache is
            # working" and "nothing has exercised it yet" both render as SILENCE, and the hit
            # RATE is underivable from the log stream at all.
            #
            # ⚠️ This log line is DELIBERATELY coarser than the counter that drives the console.
            # It fires whenever the cache did not serve THIS call — including when the tenant
            # has no brain bound, so no brain scope resolved and there was nothing to consult.
            # The metered counter (``brain_meter``, incremented inside ``cache_get`` where the
            # scope is known) counts ONLY a real consulted-and-empty miss, so the displayed hit
            # rate is not dragged down by non-events. Do not "reconcile" the two: the log is a
            # per-call breadcrumb, the counter is the instrument.
            log.info("llm_cache.miss", operation=operation)
        except Exception:  # cache must never break a call
            pass

    # SaaS cost-safe fail-closed: unconfigured tenant → no spend on the global env/cloud brain.
    if _saas_brain_unavailable():
        log.warning("llm_call.saas_no_brain_failclosed", operation=operation, mode="async_retry")
        _record_llm_nonanswer(operation, "saas_no_brain_failclosed")
        _emit_brain_nonanswer(user_id, _NO_BRAIN_EVENT_REASONS.get(
            no_brain_metric(), "saas_no_brain_failclosed"))
        _health_note(user_id, no_brain_metric(), operation, detail=no_brain_detail())
        return {}

    # Select timeout based on operation type
    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    if max_tokens is None:
        max_tokens = LLMMaxTokens.get(operation)

    # Per-tenant BYO engine-brain override (SaaS). SAAS-off → global breaker, unchanged
    # model/max_tokens, empty extra params (byte-for-byte today's behavior).
    _breaker, model, max_tokens, _extra_params = _apply_tenant_brain(model, max_tokens)
    # Reasoning headroom (dprompt-156): a model that cannot disable reasoning spends
    # reasoning tokens INSIDE the completion budget - pad it so a long fact-dense turn's
    # JSON is not truncated to empty. Computed once per logical call, after the tenant-brain
    # override and BEFORE _context_preflight (which must see the full budget).
    _pre_headroom_max = max_tokens
    max_tokens = _apply_reasoning_headroom(model, max_tokens)
    _headroom_applied = max_tokens - _pre_headroom_max

    # Pacing moved INTO the retry loop (one token per REQUEST, not per logical call) —
    # see the matching note in call_llm_with_retry_sync.
    #
    # Check circuit breaker first (per-tenant in SaaS mode; global otherwise)
    if _breaker_is_open(_breaker):
        time_until_reset = (
            _breaker.last_failure_time +
            timedelta(seconds=_breaker.timeout_seconds) -
            datetime.utcnow()
        ).total_seconds()

        error_msg = {
            "error": "circuit_breaker_open",
            "message": f"Language model not responding. Wait {max(1, int(time_until_reset))}s or restart backend.",
            "debug": "Check logs: docker logs faultline --tail=50 | grep circuit_breaker"
        }
        log.warning("call_llm_async.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation,
                   time_until_reset_seconds=max(1, int(time_until_reset)))
        _record_llm_nonanswer(operation, "circuit_breaker_open")
        _emit_brain_nonanswer(user_id, "circuit_breaker_open")
        _health_note(user_id, "breaker_open", operation)
        if raise_on_unavailable:
            raise LLMUnavailable("breaker_open", operation)
        return error_msg

    # CONTEXT-WINDOW PRE-FLIGHT (token budget). Runs AFTER the tenant-brain override, so
    # the window and the max_tokens checked are the ones this call will actually use.
    max_tokens, _ctx_block = _context_preflight(messages, max_tokens, operation, user_id,
                                                mode="async_retry")
    _headroom_applied = max_tokens - _pre_headroom_max
    if _ctx_block is not None:
        return _ctx_block

    # Lazy import to avoid circular dependencies
    from src.api.llm_client import build_llm_payload, get_llm_headers

    payload = build_llm_payload(
        messages=messages,
        model=model,
        user_id=user_id,
        max_tokens=max_tokens,
        # temperature defaults to 0.0 in build_llm_payload; a tenant's extra_params may
        # override it. Passed via **_extra_params to avoid a duplicate-kwarg collision.
        **{"temperature": 0.0, **_extra_params},
    )

    # SSRF re-check (audit HIGH #2): re-resolve + re-vet a tenant BYO endpoint immediately
    # before connect so a DNS-rebind after save/resolve cannot smuggle an internal target
    # past the egress guard. Fail-closed — a blocked tenant endpoint aborts the call.
    if _tenant_egress_blocked():
        log.warning("llm_call_async.tenant_egress_blocked", operation=operation, mode="async_retry")
        _record_llm_nonanswer(operation, "tenant_egress_blocked")
        _emit_brain_nonanswer(user_id, "tenant_egress_blocked")
        return {}

    endpoints = _get_endpoint_list()
    response = None
    last_error = None

    for attempt in range(1, max_retries + 1):
        for endpoint_idx, endpoint in enumerate(endpoints, 1):
            # One token per REQUEST (see the sync twin). A background caller with no
            # capacity yields rather than firing uncapped.
            if not await apply_rate_limit_async(operation):
                log.info("llm_call.deferred_no_capacity",
                         user_id=user_id, operation=operation,
                         attempt=attempt, mode="async_retry")
                _record_llm_nonanswer(operation, "rate_deferred")
                _emit_brain_nonanswer(user_id, "rate_deferred")
                if raise_on_unavailable:
                    raise LLMUnavailable("rate_deferred", operation)
                # DISTINGUISHABLE defer — see the sync twin for why a bare {} here
                # becomes a terminal chunk failure on a merely-paced day.
                return {"error": "rate_deferred"}
            try:
                log.debug("llm_call_async.attempt_start",
                         user_id=user_id,
                         operation=operation,
                         attempt=attempt,
                         endpoint_index=endpoint_idx,
                         timeout_seconds=timeout)

                start_time = time.time()

                # Lazy import to avoid circular dependencies
                from src.api.main import _http_client

                # Outbound source-IP spreading (opt-in LLM_SOURCE_IPS): rotate the
                # bound source address per call. Unset env → uses _http_client
                # unchanged. Fail-safe: a bad/unbindable IP falls back to it.
                response = await _post_llm_async(
                    _http_client,
                    endpoint,
                    json=payload,
                    headers=get_llm_headers(),
                    timeout=timeout,
                )

                elapsed = time.time() - start_time
                response.raise_for_status()

                # LAYER 2 (adaptive header learning): same seam as the sync path.
                _observe_rate_headers(response, operation)

                log.info("llm_call_async.attempt_success",
                        user_id=user_id,
                        operation=operation,
                        attempt=attempt,
                        elapsed_seconds=round(elapsed, 2),
                        endpoint_index=endpoint_idx)
                # endpoint/timeout ride along so the alarm stream can name the HOST that
                # answered and the BUDGET it answered inside. `endpoint` is a URL; only its
                # netloc is ever stored or logged (brain_health.endpoint_host) because some
                # providers carry the key in the URL.
                _health_note(user_id, "llm_call", operation, elapsed,
                             endpoint=endpoint, budget_seconds=timeout)

                # Success — reset circuit breaker and parse response robustly (dBug-016)
                _breaker.record_success()
                _clear_shared_breaker()

                result = _parse_llm_response_robust(response)
                if not result:
                    log.warning("llm_call_async.response_parse_failed",
                               user_id=user_id,
                               operation=operation,
                               endpoint_index=endpoint_idx)
                    continue

                # FREE CALIBRATION off usage.prompt_tokens (ratchets chars/token DOWN only).
                _context_observe(messages, result)

                content = response_message_text(result, operation, user_id)
                # VERIFY suppression before stripping: once the tags are gone the evidence
                # that the model reasoned is gone too. A 200 is not proof of suppression.
                _audit_reasoning_leak(result, operation, user_id,
                                      expected=_reasoning_expected(model),
                                      budget_padded=_headroom_applied,
                                      model=model)
                _record_call_usage(result, operation)
                # Strip <think>...</think> reasoning blocks (Qwen3 models)
                content = _strip_think_tags(content)
                # Strip a ```json fence BEFORE parsing — a fenced payload is well-formed JSON
                # that json.loads cannot read, and the object-only regex below cannot rescue
                # an array. Without this, a valid empty extraction ("[]") reads as a hard
                # parse failure and feeds the circuit breaker.
                content = _unfence(content)

                if not content:
                    _record_llm_nonanswer(operation, "empty_llm_content")
                    return {}

                try:
                    _parsed = json.loads(content)
                    if _cacheable and _parsed is not None:
                        try: llm_cache.cache_put(operation, _sys_prompt, messages, _parsed)
                        except Exception: pass
                    return _parsed
                except json.JSONDecodeError:
                    match = _re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, _re.DOTALL)
                    if not match:
                        match = _re.search(r'\{.*\}', content, _re.DOTALL)
                    if not match:
                        # ARRAY fallback. The extraction prompt asks for a JSON ARRAY, so an
                        # object-only salvage regex cannot rescue the very shape we request —
                        # a top-level array wrapped in prose was reported as "no JSON".
                        match = _re.search(r'\[.*\]', content, _re.DOTALL)
                    if match:
                        _parsed = json.loads(match.group())
                        if _cacheable and _parsed is not None:
                            try: llm_cache.cache_put(operation, _sys_prompt, messages, _parsed)
                            except Exception: pass
                        return _parsed
                    else:
                        log.warning("llm_call_async.no_json_in_response",
                                   user_id=user_id,
                                   operation=operation,
                                   content_preview=content[:200])
                        _record_llm_nonanswer(operation, "unparseable_llm_response")
                        return {}

            except Exception as e:
                if _is_egress_blocked(e):
                    # CALL-TIME SSRF refusal — see the sync twin. Loud, unretried, fail-closed.
                    _egress_refusal(e, operation, user_id, "async_retry")
                    if raise_on_unavailable:
                        raise LLMUnavailable("tenant_egress_blocked", operation) from e
                    return {}
                last_error = e
                # LAYER 2 (adaptive header learning): a 429 also carries the budget —
                # learn it even though the call failed (remaining==0 → hard pause).
                _observe_rate_headers(getattr(e, "response", None), operation)
                elapsed = time.time() - start_time
                response_body = ""
                if isinstance(e, httpx.HTTPStatusError) and hasattr(e, 'response'):
                    try:
                        response_body = e.response.text[:500]
                    except Exception:
                        pass
                log.warning("llm_call_async.attempt_failed",
                           user_id=user_id,
                           operation=operation,
                           attempt=attempt,
                           endpoint_index=endpoint_idx,
                           elapsed_seconds=round(elapsed, 2),
                           error_type=type(e).__name__,
                           error_message=str(e)[:200],
                           response_body=response_body or "(unavailable)")
                _health_note(user_id, "llm_call_failed", operation, elapsed,
                             endpoint=endpoint, budget_seconds=timeout,
                             detail=_llm_input_echo(messages),
                             exc=e, response_body=response_body or None)

                # OVER-WINDOW REFUSAL: deterministic — do not retry, do not open the breaker.
                _ctx_rej = _context_rejection(e, operation, user_id, mode="async_retry")
                if _ctx_rej is not None:
                    return _ctx_rej

        # All endpoints failed for this attempt
        if attempt < max_retries:
            backoff_seconds = 2 ** (attempt - 1)
            # A rate-limit 429 may carry the provider's OWN retry window — honoring it
            # (max with the exponential backoff) stops us from re-hammering an exhausted
            # per-minute budget with retries that can only fail again.
            retry_after = _retry_after_seconds(last_error)
            if retry_after is not None:
                if retry_after > backoff_seconds:
                    log.info("llm_call_async.backoff_honored_retry_after",
                             user_id=user_id,
                             operation=operation,
                             attempt=attempt,
                             retry_after_seconds=round(retry_after, 1),
                             backoff_seconds=round(backoff_seconds, 1))
                backoff_seconds = max(backoff_seconds, retry_after)
            log.warning("llm_call_async.attempt_exhausted_endpoints",
                       user_id=user_id,
                       operation=operation,
                       attempt=attempt,
                       backoff_seconds=backoff_seconds)
            await asyncio.sleep(backoff_seconds)

    # All retries exhausted. Classify BEFORE counting (see "BREAKER EVIDENCE MODEL").
    _fclass, _ftoken = classify_failure(last_error)
    _breaker.record_failure(_fclass, _failure_signature(_fclass, _ftoken, operation),
                            connect_phase=is_connect_phase_failure(last_error))
    _publish_breaker_state(_breaker)
    log.error("llm_call_async.all_retries_exhausted",
             user_id=user_id,
             operation=operation,
             total_attempts=max_retries,
             total_endpoints=len(endpoints),
              final_error_type=type(last_error).__name__,
              final_error_message=str(last_error)[:200])
    _record_llm_nonanswer(operation, f"all_retries_exhausted:{type(last_error).__name__}")
    _emit_brain_nonanswer(user_id, "all_retries_exhausted", exc=last_error)

    # See the sync twin: a verdict-caching caller must be able to tell "never reached"
    # from "answered nothing".
    if raise_on_unavailable:
        raise LLMUnavailable("retries_exhausted", operation) from last_error
    if last_error is not None:
        raise last_error
    raise RuntimeError("No LLM endpoint responded")


# ──────────────────────────────────────────────────────────────────────────────
# FAST PATH: call_llm_no_retry_sync()
# ──────────────────────────────────────────────────────────────────────────────

def call_llm_no_retry_sync(
    messages: list[dict],
    model: str,
    user_id: str = "anonymous",
    timeout: Optional[float] = None,
    operation: str = "DEFAULT",
    max_tokens: Optional[int] = None,
) -> Optional[dict]:
    """
    Single-attempt LLM call with graceful failure (no retry).

    Used for non-critical operations where a timeout or failure is acceptable:
    - Optional enrichment (e.g., metadata inference for novel rel_types)
    - Background taxonomy discovery
    - Fallback operations that have alternate code paths

    Returns None on any failure (connection error, timeout, parse error).
    Never raises exceptions — always degrades gracefully.

    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model name string
        user_id: User UUID for logging
        timeout: Request timeout in seconds (default from LLMTimeouts)
        operation: Operation type for timeout selection

    Returns:
        Parsed JSON response, or None on any failure
    """
    # SaaS cost-safe fail-closed: unconfigured tenant → no spend on the global env/cloud brain.
    if _saas_brain_unavailable():
        log.warning("llm_call.saas_no_brain_failclosed", operation=operation, mode="no_retry")
        _emit_brain_nonanswer(user_id, _NO_BRAIN_EVENT_REASONS.get(
            no_brain_metric(), "saas_no_brain_failclosed"))
        _health_note(user_id, no_brain_metric(), operation, detail=no_brain_detail())
        return None

    if timeout is None:
        timeout = LLMTimeouts.get(operation)

    if max_tokens is None:
        max_tokens = LLMMaxTokens.get(operation)

    # Per-tenant BYO engine-brain override (SaaS). SAAS-off → global breaker, unchanged
    # model/max_tokens, empty extra params. Keeps model coherent with the tenant endpoint
    # (get_backend_endpoint / get_llm_headers are already tenant-aware).
    _breaker, model, max_tokens, _extra_params = _apply_tenant_brain(model, max_tokens)
    # Reasoning headroom (dprompt-156): a model that cannot disable reasoning spends
    # reasoning tokens INSIDE the completion budget - pad it so a long fact-dense turn's
    # JSON is not truncated to empty. Computed once per logical call, after the tenant-brain
    # override and BEFORE _context_preflight (which must see the full budget).
    _pre_headroom_max = max_tokens
    max_tokens = _apply_reasoning_headroom(model, max_tokens)
    _headroom_applied = max_tokens - _pre_headroom_max

    # Respectful account-wide pacing BEFORE the breaker check — a paced (waiting) call
    # must not be counted as a breaker failure. No-op when disabled / no key resolvable.
    # This lane sends exactly one request per call, so one token here is already 1:1.
    if not apply_rate_limit_sync(operation):
        log.info("llm_call.deferred_no_capacity",
                 user_id=user_id, operation=operation, mode="no_retry")
        _record_llm_nonanswer(operation, "rate_deferred")
        _emit_brain_nonanswer(user_id, "rate_deferred")
        return None

    # Check circuit breaker (per-tenant in SaaS mode; global otherwise)
    if _breaker_is_open(_breaker):
        log.warning("call_llm_no_retry.circuit_breaker_open",
                   user_id=user_id,
                   operation=operation)
        _emit_brain_nonanswer(user_id, "circuit_breaker_open")
        _health_note(user_id, "breaker_open", operation)
        return None

    # CONTEXT-WINDOW PRE-FLIGHT (token budget). This lane returns None on failure, so an
    # over-window request here degraded with no signal at all — the worst of the three.
    max_tokens, _ctx_block = _context_preflight(messages, max_tokens, operation, user_id,
                                                mode="no_retry")
    if _ctx_block is not None:
        return None
    _headroom_applied = max_tokens - _pre_headroom_max

    try:
        # Lazy import to avoid circular dependencies
        from src.api.llm_client import build_llm_payload, get_llm_headers

        payload = build_llm_payload(
            messages=messages,
            model=model,
            user_id=user_id,
            max_tokens=max_tokens,
            # temperature defaults to 0.0 in build_llm_payload; a tenant's extra_params may
            # override it. Passed via **_extra_params to avoid a duplicate-kwarg collision.
            **{"temperature": 0.0, **_extra_params},
        )

        # SSRF re-check (audit HIGH #2): re-resolve + re-vet a tenant BYO endpoint immediately
        # before connect so a DNS-rebind after save/resolve cannot smuggle an internal target
        # past the egress guard. Fail-closed — a blocked tenant endpoint aborts the call.
        if _tenant_egress_blocked():
            log.warning("call_llm_no_retry.tenant_egress_blocked",
                        user_id=user_id, operation=operation)
            _emit_brain_nonanswer(user_id, "tenant_egress_blocked")
            return None

        endpoints = _get_endpoint_list()
        if not endpoints:
            log.error("call_llm_no_retry.no_endpoints_available",
                     user_id=user_id,
                     operation=operation)
            return None

        # Try primary endpoint only
        endpoint = endpoints[0]

        log.debug("llm_call_no_retry.attempt",
                 user_id=user_id,
                 operation=operation,
                 endpoint=endpoint,
                 timeout_seconds=timeout)

        start_time = time.time()

        # Outbound source-IP spreading (opt-in LLM_SOURCE_IPS): rotate the bound
        # source address per call. Unset env → uses _llm_http_client unchanged.
        # Fail-safe: a bad/unbindable IP falls back to it.
        response = _post_llm_sync(
            _llm_http_client,
            endpoint,
            json=payload,
            headers=get_llm_headers(),
            timeout=timeout,
        )

        elapsed = time.time() - start_time
        response.raise_for_status()

        log.info("llm_call_no_retry.success",
                user_id=user_id,
                operation=operation,
                elapsed_seconds=round(elapsed, 2))

        result = _parse_llm_response_robust(response)
        if not result:
            log.warning("llm_call_no_retry.response_parse_failed",
                       user_id=user_id,
                       operation=operation)
            return None

        # FREE CALIBRATION off usage.prompt_tokens (ratchets chars/token DOWN only).
        _context_observe(messages, result)

        content = response_message_text(result, operation, user_id)
        # VERIFY suppression before stripping (see the retry paths — same reasoning).
        _audit_reasoning_leak(result, operation, user_id,
                              expected=_reasoning_expected(model),
                              budget_padded=_headroom_applied,
                              model=model)
        _record_call_usage(result, operation)
        # Strip <think>...</think> reasoning blocks (Qwen3 models)
        content = _strip_think_tags(content)
        # Strip a ```json fence before parsing (see the retry paths — same reasoning).
        content = _unfence(content)

        if not content:
            return None

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = _re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, _re.DOTALL)
            if not match:
                match = _re.search(r'\{.*\}', content, _re.DOTALL)
            if not match:
                # ARRAY fallback — see the retry paths. The extraction prompt asks for a JSON
                # ARRAY, so an object-only salvage regex cannot rescue the shape we request.
                match = _re.search(r'\[.*\]', content, _re.DOTALL)
            if match:
                return json.loads(match.group())
            else:
                log.warning("llm_call_no_retry.no_json_in_response",
                           user_id=user_id,
                           operation=operation,
                           content_preview=content[:200])
                return None

    except Exception as e:
        if _is_egress_blocked(e):
            # CALL-TIME SSRF refusal — loud, fail-closed, and NOT a "graceful degrade".
            _egress_refusal(e, operation, user_id, "no_retry")
            return None
        response_body = ""
        if isinstance(e, httpx.HTTPStatusError) and hasattr(e, 'response'):
            try:
                response_body = e.response.text[:500]
            except Exception:
                pass
        log.warning("llm_call_no_retry.failed_gracefully",
                   user_id=user_id,
                   operation=operation,
                   error_type=type(e).__name__,
                   error_message=str(e)[:200],
                   response_body=response_body or "(unavailable)")
        # ⚠️ THIS LANE WAS INVISIBLE TO THE ALARM STREAM ENTIRELY. It logs the same three
        # things the retrying lanes log — the type, the message and the body — and then
        # dropped all of it, so a seat whose failures happen HERE has no probe row, no derived
        # cause, and nothing on any tenant-facing surface. "Non-critical" describes the RETRY
        # policy, not the customer's right to know their endpoint is refusing them: a 401 is a
        # 401 on whichever lane it arrives. `endpoint`/`budget` are not in scope here, so the
        # row carries what this lane knows and the renderer names the lane rather than
        # inventing a host.
        _health_note(user_id, "llm_call_failed", operation,
                     budget_seconds=timeout, exc=e,
                     response_body=response_body or None)
        # An over-window refusal on this lane used to be indistinguishable from any other
        # graceful degrade. Name it, count it, and LEARN the endpoint's real window.
        _context_rejection(e, operation, user_id, mode="no_retry")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# STATUS & CONTROL: Circuit Breaker Management
# ──────────────────────────────────────────────────────────────────────────────

def get_circuit_breaker_status() -> dict:
    """
    Get current circuit breaker state and statistics.

    Used by health endpoints and diagnostics to monitor LLM endpoint health.

    Returns:
        Dict with keys:
        - is_open: bool — circuit currently open?
        - failures: int — current failure count
        - threshold: int — failure threshold
        - last_failure_time: Optional[str] — ISO 8601 timestamp of most recent failure
        - timeout_seconds: int — time before attempting reset
        - seconds_until_reset: Optional[float] — seconds until reset attempt (if open)
    """
    status = {
        "is_open": _llm_circuit_breaker.is_open(),
        "failures": _llm_circuit_breaker.failures,
        "threshold": _llm_circuit_breaker.failure_threshold,
        "last_failure_time": None,
        "timeout_seconds": _llm_circuit_breaker.timeout_seconds,
        "seconds_until_reset": None,
    }

    if _llm_circuit_breaker.last_failure_time:
        status["last_failure_time"] = _llm_circuit_breaker.last_failure_time.isoformat()

        time_since = datetime.utcnow() - _llm_circuit_breaker.last_failure_time
        seconds_elapsed = time_since.total_seconds()
        seconds_remaining = _llm_circuit_breaker.timeout_seconds - seconds_elapsed
        if seconds_remaining > 0:
            status["seconds_until_reset"] = max(0.0, seconds_remaining)

    # TRUTHFULNESS: this used to report ONLY the process-global breaker, so under SaaS —
    # where every real call runs on a PER-ACCOUNT breaker — the health surface could read
    # "closed, 0 failures" while a tenant was fully broken. Report both, plus the evidence
    # accounting so a suppressed-but-real problem is visible rather than merely absent.
    status["evidence_model_enabled"] = breaker_evidence_enabled()
    status["suppressed_failures"] = _llm_circuit_breaker.suppressed
    status["distinct_signatures"] = len(_llm_circuit_breaker.signatures)
    # CONNECT-PHASE TRIP observability (BUG-brain-connect-timeout-burns-full-budget-20261026):
    # an operator watching a dead brain sees WHY the breaker opened early — the streak,
    # and the knob that turned the streak into an open circuit.
    status["connect_phase_streak"] = _llm_circuit_breaker.consecutive_connect_failures
    status["connect_trip_n"] = connect_trip_n()
    try:
        status["tenant_breakers"] = {
            # account_id is hashed: the health surface is read by operators, and an account
            # id is tenant-identifying. The digest is stable, so it is still correlatable.
            hashlib.sha256(str(acct).encode()).hexdigest()[:12]: {
                "is_open": b.is_open(),
                "failures": b.failures,
                "suppressed_failures": b.suppressed,
                "distinct_signatures": len(b.signatures),
            }
            for acct, b in list(_tenant_circuit_breakers.items())
        }
    except Exception:  # noqa: BLE001 — observability must never raise
        status["tenant_breakers"] = {}

    return status


def reset_circuit_breaker():
    """
    Force circuit breaker closed (emergency recovery).

    Used by administrators to recover from widespread LLM outages.
    Should only be called after manually verifying LLM endpoint is healthy.

    Returns:
        Updated circuit breaker status (from get_circuit_breaker_status)
    """
    _llm_circuit_breaker.reset()
    return get_circuit_breaker_status()


def close_llm_http_client() -> None:
    """Close the module-level LLM HTTP client. Call from process shutdown paths."""
    _llm_http_client.close()
    # Also close any cached source-IP-bound clients (opt-in LLM_SOURCE_IPS).
    llm_source_ip.close_all()
    # …and the pooled guarded (pinned-egress) clients the tenant BYO path rides on.
    try:
        from saas.llm_config import reset_shared_guarded_clients
        reset_shared_guarded_clients()
    except Exception:  # noqa: BLE001 — public core has no saas/ package
        pass