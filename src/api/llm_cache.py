"""Brain-scoped ingest cache for deterministic-input LLM calls.

See ``DEV/DESIGN-brain-budget-event-cache.md`` §2 for the design contract.

WHY THIS MODULE EXISTS
──────────────────────
The deterministic ingest LLM calls — atomize, relation-extract, aspect-synonym,
intent classification — map the same input to the same output every time: the
same sentence yields the same edges. A Redis-backed cache scoped to the BRAIN
(not the account row, not the process) means the second and later seats on a
shared corporate brain reuse the first seat's work for free, while two seats
with their own brains on a house account NEVER serve each other an answer.

THE SCOPE FOLLOWS THE BRAIN (§0 of the design contract)
…………………………………………………………………………………………………………
* corporate/team account, one shared brain  → every seat resolves the SAME cfg
  → SAME brain scope → SHARED cache. Seats genuinely reuse each other's work.
* house/individual account, per-seat brains  → each seat resolves its OWN cfg
  → DISTINCT brain scopes → ISOLATED caches even on a shared account_id.
* two customers on the same public endpoint+model → DISTINCT api_keys → DISTINCT
  brain scopes → NEVER share a cache (never serve one an answer computed for
  another).

The scope is resolved through ``llm_calls._resolve_brain_scope()`` (the one
source of truth established by Deliverable A) so the cache, the rate budget, and
the circuit breaker all key on the SAME identity. No scope is invented here:
``scope_for_brain`` returns None for SAAS-off / no cfg / the no-brain sentinel,
and the cache no-ops on None.

FOUR HARD CONTRACTS (violating any ships a broken cache)
────────────────────────────────────────────────────────
1. **NEVER raise.** Redis down, parse error, library missing → ``cache_get``
   returns None (miss), ``cache_put`` returns None (skipped). A cache failure
   must NEVER block an LLM call; the call proceeds and the result is just not
   cached.
2. **NEVER weaken extraction.** The key strips EPHEMERAL ids (``_id`` / ``_ts`` /
   ``request_id`` / ``trace_id`` / ``session``) but PRESERVES all semantic content
   (role, content). "Same sentence → same key" holds; "different sentence →
   different key" holds. See ``_canonical_messages``.
3. **FOSS byte-for-byte.** ``cache_enabled()`` defaults to "auto" → ON only when
   ``SAAS_MODE`` is truthy; explicit "true"/"false" overrides. With SAAS off the
   flag is False and the hooks in ``llm_calls.py`` never reach this module's I/O.
4. **Whitelisted operations ONLY.** Only the deterministic-input calls
   (``ATOMIZE``, ``EXTRACT``, ``EXTRACTION``, ``ASPECT_SYNONYM``,
   ``INTENT_CLASSIFICATION``) are cached. Anything else (``VALIDATION``,
   ``RETRACTION_EXTRACTION``, ``CLASSIFY_CHAIN``, ``DEFAULT``, recall-side) is
   NEVER cached — those are either non-deterministic or session-specific.

This module reuses ``redis_coord``'s ONE client and ``redis_coord.key()`` builder
(both already fail-safe) — it does not invent a second Redis connection.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

try:  # pragma: no cover - import guard
    import structlog
    log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("llm_cache")


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG — kill switch + TTL
# ──────────────────────────────────────────────────────────────────────────────

# LLM_INGEST_CACHE: "auto" (default) → ON when SAAS_MODE truthy, OFF otherwise.
# Explicit "true"/"false" overrides. OFF in FOSS = byte-for-byte open-core.
# LLM_INGEST_CACHE_TTL_S: cache entry lifetime (default 24h).

_CACHE_NS = "llmc"

# The operations eligible for caching: deterministic-INPUT calls where the same prompt yields
# the same answer, so a replay cannot weaken extraction or lose a fact. Named by the LITERAL
# string each call site passes as ``operation=`` (verified by grep of src/), NOT by concept —
# an earlier draft used concept names (ATOMIZE/ASPECT_SYNONYM/INTENT_CLASSIFICATION) that no
# call site ever passes, so the cache silently covered 1/4 of intent. The real literals:
#   REFRAME            — atomize a sentence to atomic facts (src/extraction/reframe.py)
#   EXTRACT            — chunked relation extraction (/extract/rewrite, main.py)
#   INTENT_PRECLASSIFY — classify-chain pre-pass (deterministic zero-shot)
#   INTENT_ADJUDICATION — classify-chain adjudication (deterministic)
# DEFERRED (not cached yet, documented residuals):
#   ENRICHMENT — used at ≥5 call sites with DIFFERENT semantics (aspect-synonym at main.py:37786
#     vs bounded what-is classification at main.py:51120 vs re_embedder enrichment at ×3). The
#     operation NAME alone cannot separate the cacheable case from the non-cacheable one, so it
#     is NOT cached until a per-call-site opt-in signal is threaded. It also routes partly
#     through call_llm_no_retry_sync (no cache hook). Safe deferral: the high-volume cross-seat
#     reuse win is REFRAME+EXTRACT, both covered.
_CACHEABLE_OPS = frozenset({
    "REFRAME",
    "EXTRACT",
    "INTENT_PRECLASSIFY",
    "INTENT_ADJUDICATION",
})

# Keys whose names end with any of these are EPHEMERAL (request-scoped ids /
# timestamps) and are stripped before hashing so two turns carrying different
# trace ids but the SAME semantic content hash to the SAME cache key. Semantic
# keys (role, content, name, …) are never stripped.
_EPHEMERAL_SUFFIXES = ("_id", "_ts", "request_id", "trace_id", "session")


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() not in ("", "false", "0", "no", "off")


def cache_enabled() -> bool:
    """True when the ingest cache is active.

    ``LLM_INGEST_CACHE`` env:
      * ``"true"`` / ``"1"`` / …  → ON unconditionally.
      * ``"false"`` / ``"0"`` / … → OFF unconditionally (kill switch).
      * ``"auto"`` (default)      → follows ``SAAS_MODE`` (ON in SaaS, OFF in FOSS).
    """
    raw = os.environ.get("LLM_INGEST_CACHE", "auto").strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    return _truthy(os.environ.get("SAAS_MODE"))


def cacheable_operation(op: str) -> bool:
    """True iff ``op`` is in the deterministic-input whitelist (cacheable)."""
    return bool(op) and op in _CACHEABLE_OPS


def _ttl_seconds() -> int:
    try:
        return max(1, int(os.environ.get("LLM_INGEST_CACHE_TTL_S", "86400")))
    except (TypeError, ValueError):
        return 86400


# ──────────────────────────────────────────────────────────────────────────────
# SCOPE — resolved through the SAME source of truth as the rate budget + breaker
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_scope():
    """The brain ``Scope`` for the current call, or None.

    Lazy import of ``llm_calls._resolve_brain_scope`` (the one source of truth)
    to avoid a module-level circular dependency. Returns None on ANY failure —
    the caller no-ops (FOSS / unbound tenant / import fault).
    """
    try:
        from src.api.llm_calls import _resolve_brain_scope
        return _resolve_brain_scope()
    except Exception:  # noqa: BLE001 — scope resolution must never break a call
        return None


def _meter(scope, kind: str) -> None:
    """Count a cache outcome against the brain scope. NEVER raises, NEVER gates.

    Counted HERE — inside ``cache_get`` — rather than at the two ``llm_calls.py`` call sites,
    so the hit and the miss are recorded by the same statement that decides which one it was.
    A counter placed at the call sites would have to re-derive "was this a miss?" from
    ``_hit is None``, which is also what a disabled cache, an unresolvable scope and a dead
    Redis all look like — three non-events that would inflate the miss count and make the hit
    rate read far worse than the cache actually performs.

    The scope is already resolved by the caller, so this adds no scope resolution — only one
    pipelined INCR+EXPIRE against a local Redis.
    """
    try:
        from src.api import brain_meter
        brain_meter.record(scope, kind)
    except Exception:  # noqa: BLE001 — instrumentation must never break a call
        pass


# ──────────────────────────────────────────────────────────────────────────────
# CANONICALIZATION — "same sentence → same key", semantic content preserved
# ──────────────────────────────────────────────────────────────────────────────

def _is_ephemeral_key(key: Any) -> bool:
    """True if a message-dict key is an ephemeral id/timestamp (stripped before hashing)."""
    if not isinstance(key, str):
        return False
    return any(key.endswith(suf) for suf in _EPHEMERAL_SUFFIXES)


def system_prompt_from_messages(messages: list[dict]) -> str:
    """Extract the concatenated system-prompt text from a messages list.

    The system prompt is the first deterministic part of the cache key. Extracted
    from ``role=system`` entries so the hook in ``llm_calls.py`` (which only has
    the ``messages`` list) does not need a separate parameter.
    """
    if not messages:
        return ""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content", "")
            if c:
                parts.append(str(c))
    return "\n".join(parts)


def _canonical_messages(messages: list[dict]) -> str:
    """Canonical JSON of ``messages`` with ephemeral ids stripped.

    Semantic content (role, content, name, …) is PRESERVED verbatim. Only keys
    ending in ``_id`` / ``_ts`` / ``request_id`` / ``trace_id`` / ``session`` are
    dropped so two turns with different trace ids but the same prompt hash to the
    SAME cache key.
    """
    cleaned: list[Any] = []
    for msg in messages:
        if isinstance(msg, dict):
            cleaned.append({k: v for k, v in msg.items() if not _is_ephemeral_key(k)})
        else:
            cleaned.append(msg)
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False, default=str)


def _prompt_hash(system_prompt: str, messages: list[dict]) -> str:
    """SHA256 of ``(system_prompt + canonical_json(messages))``. Stable for same content."""
    canonical = _canonical_messages(messages)
    raw = f"{system_prompt}{canonical}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _cache_key(scope, operation: str, system_prompt: str, messages: list[dict]) -> Optional[str]:
    """Build the Redis key via ``redis_coord.key()`` (scope-validated, poison-safe)."""
    from src.api import redis_coord
    return redis_coord.key(_CACHE_NS, scope, operation, _prompt_hash(system_prompt, messages))


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API — cache_get / cache_put / invalidate_brain
# ──────────────────────────────────────────────────────────────────────────────

def cache_get(operation: str, system_prompt: str, messages: list[dict]) -> Optional[dict]:
    """Return the cached PARSED RESULT for ``(op, prompt)`` under the current brain
    scope, or None on miss. NEVER raises — Redis down / parse error → None.

    The returned value is the exact dict/list the LLM call would have returned
    (e.g. ``[]`` for an empty extraction, ``[{...}, ...]`` for edges). ``None`` is
    the MISS sentinel and is never a cached value (``cache_put`` refuses to store
    ``None`` so the miss-vs-empty distinction is unambiguous).
    """
    if not cache_enabled():
        return None
    if operation not in _CACHEABLE_OPS:
        return None
    try:
        scope = _resolve_scope()
        if scope is None:
            return None
        from src.api import redis_coord
        c = redis_coord.client()
        if c is None:
            return None
        k = _cache_key(scope, operation, system_prompt, messages)
        if k is None:
            return None
        raw = c.get(k)
        if not raw:
            _meter(scope, "miss")
            return None
        result = json.loads(raw)
        # None is the miss sentinel — a stored "null" is treated as a miss so the
        # call proceeds uncached (defensive: cache_put refuses None anyway).
        if result is None:
            _meter(scope, "miss")
            return None
        _meter(scope, "hit")
        return result
    except Exception:  # noqa: BLE001 — cache must NEVER break a call
        return None


def cache_put(operation: str, system_prompt: str, messages: list[dict],
              result: Any) -> None:
    """Store ``result`` for ``(op, prompt)`` under the current brain scope.

    NEVER raises — failure is logged + skipped. Refuses to store ``None`` (the
    miss sentinel) so a degenerate "null" response is never served as a cached
    answer.
    """
    if not cache_enabled():
        return
    if operation not in _CACHEABLE_OPS:
        return
    if result is None:
        return
    try:
        scope = _resolve_scope()
        if scope is None:
            return
        from src.api import redis_coord
        c = redis_coord.client()
        if c is None:
            return
        k = _cache_key(scope, operation, system_prompt, messages)
        if k is None:
            return
        c.set(k, json.dumps(result, default=str), ex=_ttl_seconds())
    except Exception:  # noqa: BLE001 — a cache write failure must NEVER block the call
        log.debug("llm_cache.put_failed", operation=operation)


def invalidate_brain(scope) -> None:
    """Drop ALL cache entries for one brain (model change, manual flush). NEVER raises.

    No caller yet — wired for future use. Scans ``fl:llmc:brain:<ident>:*`` and
    deletes each key. The scope MUST be a validated ``redis_coord.Scope``; this
    function is a no-op on None (never raises, never touches an unscoped key).
    """
    if scope is None:
        return
    try:
        from src.api import redis_coord
        if not isinstance(scope, redis_coord.Scope):
            return
        c = redis_coord.client()
        if c is None:
            return
        # Build the prefix via the sanctioned key() builder (validates the scope
        # by TYPE) then append the wildcard for SCAN.
        prefix = redis_coord.key(_CACHE_NS, scope)
        pattern = f"{prefix}:*"
        n = 0
        for k in c.scan_iter(match=pattern, count=200):
            c.delete(k)
            n += 1
        if n:
            log.info("llm_cache.invalidated", scope=str(scope), keys_deleted=n)
    except Exception:  # noqa: BLE001
        pass
